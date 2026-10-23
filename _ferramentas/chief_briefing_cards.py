"""Project trusted BriefingStore batch.document into restricted panel cards.

No runner integration, provider calls, delivery receipt, schema or outbox writes.
Only document.events are priorities; analyzed/maintenance never become cards.
Writes are atomic per card, not per document. Partial results are explicit and
safe to replay; CAS conflicts are not retried. Caller owns connection/access.
"""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import sqlite3

from chief_briefing import ACCOUNT_SCOPE, DOCUMENT_SCHEMA, validate
from thunder_os.briefing_cards import BriefingError, briefing_id, put_briefing


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":"))


def _observed_time(context):
    received = context.get("received")
    if type(received) is not dict:
        raise BriefingError("missing_observed_time")
    for key in ("sent_at_ms", "internal_date", "date"):
        value = received.get(key)
        if value is None:
            continue
        try:
            if key != "date":
                if type(value) not in (str, int) or (isinstance(value, str) and not value.isdigit()):
                    continue
                stamp = datetime.fromtimestamp(int(value) / 1000, timezone.utc)
            elif type(value) is str:
                try:
                    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    stamp = parsedate_to_datetime(value)
            else:
                continue
            if stamp.tzinfo is not None and stamp.utcoffset() is not None:
                return stamp.isoformat()
        except (ValueError, TypeError, OverflowError, OSError):
            continue
    raise BriefingError("missing_observed_time")


def _messages(context):
    thread, received = context.get("thread"), context.get("received")
    if type(thread) is not dict or type(received) is not dict:
        raise BriefingError("invalid_message_context")
    messages = thread.get("messages")
    if type(messages) is not list or any(type(m) is not dict for m in messages):
        raise BriefingError("invalid_message_context")
    result, seen = [], {}
    for message in messages + [received]:
        identity = message.get("message_id") or message.get("id")
        if identity is not None and type(identity) is not str:
            raise BriefingError("invalid_message_identity")
        if identity and identity in seen:
            if message != seen[identity]:
                raise BriefingError("conflicting_message_context")
            continue
        if not identity and message in result:
            continue
        if identity:
            seen[identity] = message
        result.append(message)
    return result


def _human_context(event):
    context = []
    for message in _messages(event["context"]):
        direction = {"incoming": "Recebida", "outgoing": "Enviada"}.get(message.get("direction"))
        if direction is None:
            raise BriefingError("invalid_message_direction")
        author = message.get("from")
        if not isinstance(author, str) or not author.strip():
            author = "Sua conta" if direction == "Enviada" else "Autoria nao informada"
        when = _observed_time({"received": message})
        text = message.get("text")
        if text is None or text == "":
            text = "Conteudo da mensagem indisponivel nesta amostra."
        if type(text) is not str:
            raise BriefingError("invalid_message_text")
        if message.get("text_truncated"):
            text += "\n[Texto parcial na leitura de origem.]"
        # Split long messages without dropping characters. The private ledger
        # remains the full raw source; the card contains only human-facing text.
        for offset in range(0, len(text), 7800):
            continuation = " (continuacao)" if offset else ""
            context.append({"quem": direction + " - " + author + continuation,
                            "quando": when, "texto": text[offset:offset + 7800]})
    return context


def _card(event):
    when = _observed_time(event["context"])
    context = _human_context(event)
    context.extend({"quem": label, "quando": when, "texto": event[key]}
                   for label, key in (("Interpretacao", "interpretation"),
                                      ("Recomendacao", "recommendation"),
                                      ("Decisao solicitada", "decision")))
    thread = event["context"]["thread"]
    partial = (thread.get("context_truncated") or thread.get("window_complete") is False
               or any(m.get("text_truncated") for m in _messages(event["context"])))
    note = "Amostra limitada das mensagens observadas; pode haver outras mensagens fora desta leitura."
    if partial:
        note += " O historico ou algum texto ja veio parcial da origem."
    context.append({"quem": "Cobertura da conversa", "quando": when, "texto": note})
    return {"id": event["event_id"], "origem": "briefing", "tipo": "pedido",
            "titulo": event["telegram"]["who"],
            "porque": event["telegram"]["what"] + "\nRecomendacao: " + event["telegram"]["recommendation"],
            "contexto": context,
            "fatos": ["Fonte: " + row["source"] + " | Conversa: " + row["thread_id"]
                      + " | Mensagem: " + row["message_id"] for row in event["evidence"]],
            "rascunho": "", "status": "pendente"}


def _read(con, item_id):
    row = con.execute("SELECT dados FROM main.itens WHERE id=?", (item_id,)).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row[0])
    except (ValueError, TypeError, RecursionError):
        raise BriefingError("invalid_stored_item") from None
    if type(data) is not dict:
        raise BriefingError("invalid_stored_item")
    return dict(data, id=item_id)


def _terminal(data):
    return (data is not None and data.get("origem") == "briefing"
            and data.get("tipo") == "pedido"
            and (data.get("status") != "pendente" or "_descarte" in data))


def _incoming_time(event):
    received = event["context"].get("received")
    if (type(received) is not dict or received.get("direction") != "incoming"
            or (received.get("message_id") or received.get("id")) != event["source_event_id"]):
        raise BriefingError("incoming_evidence_mismatch")
    return datetime.fromisoformat(_observed_time(event["context"]))


def _check_order(previous, item, event):
    if previous is None:
        return
    # Leave collision/protection/schema checks to the storage boundary.
    if previous.get("origem") != "briefing" or previous.get("tipo") != "pedido":
        return
    if {k: v for k, v in previous.items() if k not in ("version", "atualizado")} == item:
        return
    facts = previous.get("fatos")
    entries = previous.get("contexto")
    if (type(facts) is not list or not facts or any(type(f) is not str for f in facts)
            or type(entries) is not list or not entries):
        raise BriefingError("previous_order_unknown")
    reference = next(row for row in event["evidence"]
                     if row["message_id"] == event["source_event_id"])
    prefix = "Fonte: " + reference["source"] + " | Conversa: " + reference["thread_id"] + " | Mensagem: "
    if any(not fact.startswith(prefix) or not fact[len(prefix):] for fact in facts):
        raise BriefingError("previous_order_unknown")
    source_ref = prefix + reference["message_id"]
    if source_ref in facts:
        raise BriefingError("same_message_requires_reconciliation")
    dates = []
    for entry in entries:
        try:
            stamp = datetime.fromisoformat(entry["quando"].replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError
        except (TypeError, KeyError, AttributeError, ValueError):
            raise BriefingError("previous_order_unknown") from None
        dates.append(stamp)
    # atualizado is a persistence clock, never evidence of conversation order.
    if _incoming_time(event) <= max(dates):
        raise BriefingError("incoming_not_newer")


def project_document(conn, document):
    """Return {state, cards, analyzed_count, pending_count}.

    state: projected, partial (one or more conflict/error), or no_priorities.
    cards: [{id, state: persisted, version} | {id, state: skipped_terminal} |
            {id, state: conflict/error, code}]. No payloads/secrets in errors.
    Invalid document/identity/duplicate conversation raises BriefingError before
    any write. Other failures are per-card results, allowing independent cards
    to progress. Terminal cards are skipped with their entire audit untouched.
    Timestamps come from the observed incoming message, never the current clock;
    missing timestamps and over-budget cards fail visibly, without truncation.
    Changed content requires a different incoming message strictly later than
    every prior context timestamp. Same-message reanalysis, equal/older dates,
    or unknown prior order require reconciliation; no producer order is guessed.
    """
    if not isinstance(conn, sqlite3.Connection):
        raise BriefingError("invalid_connection")
    if conn.in_transaction:
        raise BriefingError("active_transaction")
    try:
        raw = _json(document)
        if len(raw.encode("utf-8")) > 2_000_000:
            raise BriefingError("document_too_large")
        document = json.loads(raw)
        validate(document, DOCUMENT_SCHEMA)
    except (ValueError, TypeError, RecursionError):
        raise BriefingError("invalid_document") from None
    seen = set()
    for event in document["events"]:
        item_id = event["event_id"]
        if item_id != briefing_id(event["source"], event["thread_namespace"]):
            raise BriefingError("identity_mismatch")
        if item_id in seen:
            raise BriefingError("duplicate_conversation")
        seen.add(item_id)
        for evidence in event["evidence"]:
            if (evidence["source"] != event["source"] or
                    ACCOUNT_SCOPE + ":" + evidence["thread_id"] != event["thread_namespace"]):
                raise BriefingError("evidence_identity_mismatch")
        if not any(e["message_id"] == event["source_event_id"] for e in event["evidence"]):
            raise BriefingError("evidence_identity_mismatch")
    results = []
    for event in document["events"]:
        item_id = event["event_id"]
        result = {"id": item_id}
        try:
            previous = _read(conn, item_id)
            if _terminal(previous):
                result["state"] = "skipped_terminal"
            else:
                _incoming_time(event)
                item = _card(event)
                _check_order(previous, item, event)
                saved = put_briefing(conn, item, previous.get("version") if previous else None)
                current = _read(conn, item_id)
                if _terminal(current):
                    result["state"] = "skipped_terminal"
                elif current != saved:
                    result.update(state="conflict", code="readback_changed")
                else:
                    result.update(state="persisted", version=saved["version"])
        except BriefingError as exc:
            if exc.code == "terminal_or_protected":
                result["state"] = "skipped_terminal"
            else:
                result.update(state="conflict" if exc.code in (
                    "version_conflict", "incoming_not_newer", "previous_order_unknown",
                    "same_message_requires_reconciliation") else "error",
                              code=exc.code)
        except sqlite3.Error:
            result.update(state="error", code="storage_error")
        results.append(result)
    state = ("no_priorities" if not results else "partial"
             if any(r["state"] in ("conflict", "error") for r in results) else "projected")
    return {"state": state, "cards": results, "analyzed_count": len(document["analyzed"]),
            "pending_count": document["pending_count"]}
