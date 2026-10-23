"""Offline briefing contracts and durable delivery state; no model/provider executor.

Only a trusted integration may call begin_delivery/record_receipt. Model output
cannot supply source context, identity, destination, or a delivery receipt.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import unicodedata
import uuid

from jsonschema import Draft202012Validator

DESTINATION = "telegram:6619531976"
ACCOUNT_SCOPE = "thunder-mustard"
MANUAL_TEST_HEADER = "TESTE de leitura - evento historico de 20/10/2026. Nao e uma resposta nova."


class BriefingError(ValueError):
    pass


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()


def conversation_id(channel, thread_id):
    return "briefing-" + digest([channel, ACCOUNT_SCOPE + ":" + thread_id])


def obj(properties, required=None):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False}


TEXT = {"type": "string", "minLength": 1, "maxLength": 4000}
SHORT = {"type": "string", "minLength": 1, "maxLength": 600}
TELEGRAM = obj({key: SHORT for key in ("who", "what", "recommendation", "decision")})
CHOICE = obj({"event_key": TEXT, "recommendation": TEXT, "decision": TEXT,
              "interpretation": TEXT, "telegram": TELEGRAM})
DISPOSITION = obj({"event_key": TEXT, "category": {"enum": [
    "newsletter", "automatic_reply", "delivery_failure", "insufficient_evidence", "non_commercial"]},
    "reason": TEXT})
MODEL_SCHEMA = obj({"priorities": {"type": "array", "maxItems": 3, "items": CHOICE},
                    "dispositions": {"type": "array", "maxItems": 1000, "items": DISPOSITION}})
EVENT_SCHEMA = obj({
    "event_id": TEXT, "event_key": TEXT, "thread_namespace": TEXT,
    "source": {"enum": ["email", "linkedin"]}, "source_event_id": TEXT,
    "context": {"type": "object"},
    "recommendation": TEXT, "decision": TEXT, "interpretation": TEXT,
    "evidence": {"type": "array", "minItems": 1, "items": obj({
        "source": TEXT, "thread_id": TEXT, "message_id": TEXT})},
    "telegram": TELEGRAM,
})
MAINTENANCE = obj({"source": {"enum": ["email", "linkedin", "hubspot"]},
                   "state": TEXT, "reason": TEXT})
DOCUMENT_SCHEMA = obj({
    "schema": {"const": "thunder_briefing_v1"},
    "events": {"type": "array", "maxItems": 3, "items": EVENT_SCHEMA},
    "analyzed": {"type": "array", "maxItems": 1000, "items": obj({
        "event_key": TEXT, "category": DISPOSITION["properties"]["category"], "reason": TEXT,
        "source_event": {"type": "object"}})},
    "maintenance": {"type": "array", "maxItems": 3, "items": MAINTENANCE},
    "pending_count": {"type": "integer", "minimum": 0},
    "coverage": {"const": "bounded_observed_sample_not_global"},
})


def validate(value, schema):
    if not Draft202012Validator(schema).is_valid(value):
        raise BriefingError("schema_invalid")


FORBIDDEN = re.compile(
    r"urn:|https?://|\b(?:UTC|skills?|logs?|traceback|job_id|event_id|message_id)\b"
    r"|\b[0-9a-f]{12,}\b|\b\d{8,}\b"
    r"|\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b"
    r"|/(?:root|opt|etc|var)/|[*`<>\[\]#]|\b(?:ID|IDs)\s*[:=]", re.I)


def validate_plaintext(text):
    if not isinstance(text, str) or not text.strip():
        raise BriefingError("text_empty")
    if len(text.split()) > 180 or len(text.encode("utf-16-le")) // 2 > 1200:
        raise BriefingError("text_budget_exceeded")
    if FORBIDDEN.search(text) or any(
            unicodedata.category(c).startswith("C") and c != "\n" for c in text):
        raise BriefingError("text_not_plain")
    return text


def technical_identifiers(value, field=""):
    """Collect reference fields only; names and prose are never denylisted."""
    identifiers = set()
    if isinstance(value, dict):
        for key, child in value.items():
            identifiers.update(technical_identifiers(child, key))
    elif isinstance(value, list):
        for child in value:
            identifiers.update(technical_identifiers(child, field))
    elif (field in {"id", "threadId", "messageId", "profileId", "event_key",
                    "thread_namespace", "in_reply_to"}
          or field.endswith(("_id", "_ids", "_urn", "_urns"))):
        if isinstance(value, str) and value.strip():
            identifiers.add(value)
        elif type(value) is int:
            identifiers.add(str(value))
    return identifiers


def render(document, *, selection=(), manual_test=False):
    validate(document, DOCUMENT_SCHEMA)
    if not document["events"]:
        return ""
    identifiers = technical_identifiers(document) | technical_identifiers(list(selection))
    blocks = []
    for event in document["events"]:
        fields = event["telegram"]
        if any("\n" in value or not value.strip() for value in fields.values()):
            raise BriefingError("text_field_invalid")
        block = (f"{fields['who']}\n{fields['what']}\n"
                 f"Sugestao: {fields['recommendation']}\nDecisao: {fields['decision']}")
        # Technical references stay exclusively in the internal document.
        for identifier in identifiers:
            if re.search(r"(?<!\w)" + re.escape(identifier) + r"(?!\w)", block):
                raise BriefingError("source_id_in_text")
        blocks.append(block)
    coverage = "Cobertura: amostra limitada; total de respostas desconhecido."
    if document["maintenance"]:
        names = {"email": "email", "linkedin": "LinkedIn", "hubspot": "CRM"}
        coverage += " Leitura parcial/indisponivel: " + ", ".join(
            names[row["source"]] for row in document["maintenance"]) + "."
    if document["pending_count"]:
        coverage += f" Mais {document['pending_count']} eventos aguardam analise."
    parts = ([MANUAL_TEST_HEADER] if manual_test else []) + blocks + [coverage]
    return validate_plaintext("\n\n".join(parts))


def snapshot_events(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get("schema") != "thunder_chief_readonly_v1":
        raise BriefingError("snapshot_schema_invalid")
    if len(encoded(snapshot).encode("utf-8")) > 2_000_000:
        raise BriefingError("snapshot_budget_exceeded")
    events, maintenance = [], []
    for channel in ("email", "linkedin", "hubspot"):
        source = snapshot.get(channel, {})
        if not isinstance(source, dict):
            raise BriefingError("source_schema_invalid")
        state = source.get("state", "unavailable")
        verified = channel == "hubspot" or source.get("identity_verified") is True
        if state != "ok" or source.get("listing_complete") is not True or not verified:
            maintenance.append({"source": channel, "state": str(state),
                                "reason": "partial_or_unverified_read"})
        if channel == "hubspot" or not verified or state not in ("ok", "partial"):
            continue
        items = source.get("items", [])
        if not isinstance(items, list) or len(items) > 1000:
            raise BriefingError("items_schema_invalid")
        for thread in items:
            if not isinstance(thread, dict):
                raise BriefingError("thread_schema_invalid")
            tid = thread.get("conversation_id") if channel == "linkedin" else (
                thread.get("threadId") or thread.get("thread_id") or thread.get("id"))
            messages = thread.get("messages", [])
            if not isinstance(messages, list):
                raise BriefingError("messages_schema_invalid")
            for message in messages:
                if not isinstance(message, dict):
                    raise BriefingError("message_schema_invalid")
                if message.get("direction") != "incoming":
                    continue
                mid = message.get("message_id") if channel == "linkedin" else message.get("id")
                if not isinstance(tid, str) or not tid or not isinstance(mid, str) or not mid:
                    raise BriefingError("event_identity_missing")
                events.append({
                    "event_id": conversation_id(channel, tid),
                    "event_key": digest([channel, ACCOUNT_SCOPE, tid, mid]), "source": channel,
                    "thread_namespace": ACCOUNT_SCOPE + ":" + tid,
                    "source_event_id": mid,
                    "context": {"thread": thread, "received": message,
                                "untrusted_data": True},
                    "evidence": [{"source": channel, "thread_id": tid, "message_id": mid}],
                })
    return events, maintenance


class BriefingStore:
    """One private ledger per briefing target. Transactions serialize reservations.

    No TTL/delete or automatic retry after sending. A crash after begin_delivery
    remains sending and requires operator reconciliation using provider evidence.
    """
    def __init__(self, path, *, manual_test=False):
        self.manual_test = manual_test
        path = Path(path).absolute()
        if any((parent / ".git").exists() for parent in path.parents):
            raise BriefingError("state_must_be_outside_git")
        if path.parent.is_symlink() or path.parent.stat().st_mode & 0o077:
            raise BriefingError("state_directory_not_private")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise BriefingError("state_file_not_private")
        finally:
            os.close(fd)
        self.db = sqlite3.connect(path, timeout=3, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS events(
                ordinal INTEGER PRIMARY KEY, event_key TEXT UNIQUE NOT NULL,
                data TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                state TEXT NOT NULL DEFAULT 'pending');
            CREATE TABLE IF NOT EXISTS batches(
                batch_id TEXT PRIMARY KEY, document TEXT NOT NULL, text TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL, destination TEXT NOT NULL,
                state TEXT NOT NULL, receipt TEXT);
            CREATE TABLE IF NOT EXISTS members(
                batch_id TEXT NOT NULL REFERENCES batches(batch_id),
                event_key TEXT NOT NULL REFERENCES events(event_key),
                PRIMARY KEY(batch_id,event_key));
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)

    def close(self):
        self.db.close()

    def ingest(self, snapshot):
        events, maintenance = snapshot_events(snapshot)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for event in events:
                data = encoded(event)
                self.db.execute("""INSERT INTO events(event_key,data) VALUES (?,?)
                    ON CONFLICT(event_key) DO UPDATE SET data=excluded.data, version=version+1, state='pending'
                    WHERE events.state IN ('pending','analyzed') AND events.data<>excluded.data""",
                    (event["event_key"], data))
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('maintenance',?)",
                            (encoded(maintenance),))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def pending(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise BriefingError("selection_budget_invalid")
        return [{**json.loads(row["data"]), "version": row["version"]}
                for row in self.db.execute(
                    "SELECT data,version FROM events WHERE state='pending' ORDER BY ordinal LIMIT ?",
                    (limit,))]

    def prepare(self, response, selection, destination, cancelled=False):
        """Validate model JSON, bind trusted evidence, persist before returning text.

        selection is the exact pending() projection given to the model, kept by
        the caller (never accepted back from model text). No model retries here.
        """
        if cancelled:
            raise BriefingError("cancelled")
        if destination != DESTINATION:
            raise BriefingError("destination_invalid")
        if not isinstance(response, str) or len(response.encode("utf-8")) > 64000:
            raise BriefingError("response_budget_exceeded")
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise BriefingError("duplicate_json_key")
                result[key] = value
            return result
        try:
            proposal = json.loads(response, object_pairs_hook=unique)
        except (ValueError, RecursionError):
            raise BriefingError("model_json_invalid") from None
        validate(proposal, MODEL_SCHEMA)
        candidates = {event["event_key"]: event for event in selection}
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.db.execute("SELECT 1 FROM batches WHERE state IN ('prepared','sending','uncertain')").fetchone():
                raise BriefingError("delivery_reconciliation_required")
            events, used, conversations, analyzed = [], set(), set(), []
            for choice in proposal["priorities"]:
                key = choice["event_key"]
                source = candidates.get(key)
                if source is None or key in used or source["event_id"] in conversations:
                    raise BriefingError("selection_invalid")
                current = self.db.execute("SELECT * FROM events WHERE event_key=?", (key,)).fetchone()
                if current is None or current["state"] != "pending" or current["version"] != source["version"]:
                    raise BriefingError("selection_stale")
                event = json.loads(current["data"])
                event.update({k: choice[k] for k in ("recommendation", "decision", "interpretation", "telegram")})
                events.append(event)
                used.add(key)
                conversations.add(event["event_id"])
            for choice in proposal["dispositions"]:
                key = choice["event_key"]
                source = candidates.get(key)
                current = self.db.execute("SELECT * FROM events WHERE event_key=?", (key,)).fetchone()
                if source is None or key in used:
                    raise BriefingError("selection_invalid")
                if current is None or current["state"] != "pending" or current["version"] != source["version"]:
                    raise BriefingError("selection_stale")
                analyzed.append({**choice, "source_event": json.loads(current["data"])})
                used.add(key)
            if not used:
                raise BriefingError("analysis_empty")
            pending = self.db.execute("SELECT count(*) FROM events WHERE state='pending'").fetchone()[0]
            maintenance = self.db.execute("SELECT value FROM metadata WHERE key='maintenance'").fetchone()
            document = {"schema": "thunder_briefing_v1", "events": events,
                        "maintenance": json.loads(maintenance[0]) if maintenance else [],
                        "analyzed": analyzed, "pending_count": pending - len(used),
                        "coverage": "bounded_observed_sample_not_global"}
            text = render(document, selection=selection, manual_test=self.manual_test)
            batch_id = str(uuid.uuid4())
            payload_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            state = "prepared" if events else "analyzed"
            self.db.execute("INSERT INTO batches VALUES (?,?,?,?,?,?,NULL)",
                            (batch_id, encoded(document), text, payload_hash, destination, state))
            for item in analyzed:
                self.db.execute("UPDATE events SET state='analyzed' WHERE event_key=?", (item["event_key"],))
            for key in (event["event_key"] for event in events):
                self.db.execute("INSERT INTO members VALUES (?,?)", (batch_id, key))
                self.db.execute("UPDATE events SET state='prepared' WHERE event_key=?", (key,))
            self.db.commit()
            return self.batch(batch_id)
        except BaseException:
            self.db.rollback()
            raise

    def batch(self, batch_id):
        row = self.db.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise BriefingError("batch_unknown")
        return {**dict(row), "document": json.loads(row["document"])}

    def begin_delivery(self, batch_id):
        # Committed BEFORE the trusted caller attempts a send; a crash cannot retry.
        self._transition(batch_id, ("prepared",), "sending")
        return self.batch(batch_id)

    def cancel_prepared(self, batch_id):
        self._transition(batch_id, ("prepared",), "cancelled", event_state="pending")

    def _transition(self, batch_id, expected, state, *, event_state=None, receipt=None):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.batch(batch_id)
            if row["state"] not in expected:
                raise BriefingError("transition_blocked")
            self.db.execute("UPDATE batches SET state=?,receipt=? WHERE batch_id=?",
                            (state, encoded(receipt) if receipt else None, batch_id))
            self.db.execute("""UPDATE events SET state=? WHERE event_key IN
                (SELECT event_key FROM members WHERE batch_id=?)""", (event_state or state, batch_id))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def record_receipt(self, batch_id, receipt):
        row = self.batch(batch_id)
        valid = (isinstance(receipt, dict) and receipt.get("delivered") is True
                 and receipt.get("batch_id") == batch_id
                 and receipt.get("payload_sha256") == row["payload_sha256"]
                 and receipt.get("destination") == row["destination"]
                 and isinstance(receipt.get("message_id"), str)
                 and re.fullmatch(r"[1-9][0-9]*", receipt["message_id"]) is not None)
        state = "delivered" if valid else "uncertain"
        # Store only receipt identity, never provider bodies/errors or model text.
        safe_receipt = {k: receipt[k] for k in (
            "batch_id", "payload_sha256", "destination", "message_id", "delivered")} if valid else None
        self._transition(batch_id, ("sending", "uncertain"), state, receipt=safe_receipt)
        return state
