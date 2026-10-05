"""Bounded ConnectSafely readback and sanitized existing-card audit.

No panel/database access or writes. A caller supplies exact card bindings and
an explicitly configured ConnectSafelySocialClient. The only POST used here is
/linkedin/posts/comments, whose documented operation reads post comments.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote, urlsplit

from .connection_reconcile import CHANNEL_TYPES, MESSAGE, _profile, _stamp, evaluate_card
from .connectsafely_client import ConnectSafelyError, public_id, require, urn
from .connectsafely_social_client import post_identity


EXPECTED_CARDS = {"dm": 21, "comentario": 18, "whatsapp": 2}
MAX_CARDS = sum(EXPECTED_CARDS.values())
MAX_PAGES = 3
PAGE_SIZE = 10


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("card_snapshot_invalid")
        result[key] = value
    return result


def read_panel_cards(db_path, card_ids):
    """Read only explicit panel IDs from one SQLite snapshot, without migration."""
    if (not isinstance(card_ids, list) or not 1 <= len(card_ids) <= MAX_CARDS
            or not all(isinstance(value, str)
                       and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value)
                       for value in card_ids)
            or len(card_ids) != len(set(card_ids))):
        raise ValueError("card_ids_invalid")
    try:
        path = Path(db_path).expanduser().resolve()
    except (TypeError, ValueError, OSError):
        raise ValueError("panel_snapshot_unavailable") from None
    if not path.is_file():
        raise ValueError("panel_snapshot_unavailable")
    try:
        con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            con.execute("PRAGMA query_only=ON")
            con.execute("BEGIN")
            slots = ",".join("?" for _ in card_ids)
            rows = con.execute("SELECT id,dados FROM itens WHERE id IN (" + slots + ")",
                               card_ids).fetchall()
            if len(rows) != len(card_ids):
                raise ValueError("card_snapshot_incomplete")
            cards = {}
            for card_id, raw in rows:
                if not isinstance(raw, str) or len(raw) > 200_000:
                    raise ValueError("card_snapshot_invalid")
                data = json.loads(raw, object_pairs_hook=_unique_object,
                                  parse_constant=lambda _: (_ for _ in ()).throw(
                                      ValueError("card_snapshot_invalid")))
                if (not isinstance(data, dict)
                        or data.get("id") not in (None, card_id)):
                    raise ValueError("card_snapshot_invalid")
                cards[card_id] = {**data, "id": card_id}
            return [cards[card_id] for card_id in card_ids]
        finally:
            con.close()
    except (sqlite3.Error, UnicodeError, TypeError, json.JSONDecodeError):
        raise ValueError("panel_snapshot_unavailable") from None


def _card_id(card):
    value = card.get("id") if isinstance(card, dict) else None
    return value if isinstance(value, str) and value.strip() and len(value) <= 200 else None


def _inconclusive(card_id, reason, channel=None):
    return {"card_id": card_id, "channel": channel, "decision": "inconclusive",
            "reason": reason}


def _card_public_slug(card):
    url = card.get("perfil_url")
    require(isinstance(url, str), "recipient_binding_unverified")
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise ConnectSafelyError("recipient_binding_unverified") from None
    match = re.fullmatch(r"/in/([A-Za-z0-9_-]{1,100})/?", parsed.path)
    require(parsed.scheme == "https" and parsed.netloc == "www.linkedin.com"
            and match is not None and public_id(match.group(1))
            and not parsed.query and not parsed.fragment,
            "recipient_binding_unverified")
    return match.group(1)


def _binding(card, binding, client):
    """Validate account scope and optional pins; never resolve by name."""
    require(isinstance(binding, dict) and binding.get("card_id") == card["id"],
            "card_binding_unverified")
    channel = binding.get("channel")
    require(channel in CHANNEL_TYPES and card.get("tipo") == CHANNEL_TYPES[channel],
            "channel_mismatch")
    require(binding.get("account_id") == client.account_id
            and binding.get("account_public_id") == client.expected_public_id
            and binding.get("owner_profile_urn") == client.self_profile_id
            and _profile(client.self_profile_id) == client.self_profile_id,
            "account_binding_unverified")
    if channel == "linkedin_inbox":
        slug = _card_public_slug(card)
        require(binding.get("recipient_public_id") in (None, slug)
                and binding.get("recipient_profile_url") in (None, card["perfil_url"]),
                "recipient_binding_unverified")
        person = binding.get("recipient_profile_urn")
        require(person is None or isinstance(person, str)
                and _profile(person) == person and person != client.self_profile_id,
                "recipient_binding_unverified")
    elif channel == "linkedin_comment":
        try:
            require(post_identity(card.get("post_url")) == binding.get("post_urn"),
                    "post_binding_unverified")
        except (ConnectSafelyError, TypeError):
            raise ConnectSafelyError("post_binding_unverified") from None
    return channel


def _dm_readback(client, card, binding, max_pages):
    public = _card_public_slug(card)
    resolved = client._call("GET", "/linkedin/conversations/exists/" + quote(public, safe=""),
                            {"accountId": client.account_id})
    client._scope(resolved)
    person = resolved.get("profileUrn")
    require(resolved.get("accountId") == client.account_id
            and isinstance(person, str) and _profile(person) == person
            and person != client.self_profile_id
            and binding.get("recipient_profile_urn") in (None, person)
            and resolved.get("publicIdentifier", public) == public,
            "recipient_identity_mismatch")
    thread = resolved.get("conversationUrn")
    require(resolved.get("hasConversation") is True and urn(thread)
            and thread.startswith("urn:li:msg_conversation:"),
            "conversation_unresolved")
    if binding.get("conversation_urn") is not None:
        require(binding["conversation_urn"] == thread, "conversation_identity_mismatch")
    rows = []
    participants = None
    cursor = None
    seen_cursors = set()
    seen_messages = set()
    for _ in range(max_pages):
        page = client.messages(thread, limit=PAGE_SIZE, cursor=cursor)
        require(page.get("account_id") == client.account_id
                and page.get("conversation_urn") == thread,
                "conversation_identity_mismatch")
        current = page.get("participants")
        require(isinstance(current, list) and 1 <= len(current) <= 2
                and all(isinstance(p, dict) for p in current)
                and len({_profile(p.get("profileId")) for p in current}) == len(current)
                and {_profile(p.get("profileId")) for p in current}
                    <= {client.self_profile_id, person}, "participants_unverified")
        if participants is None:
            participants = list(current)
        else:
            known = {_profile(p.get("profileId")) for p in participants}
            participants.extend(p for p in current
                                if _profile(p.get("profileId")) not in known)
        batch = page.get("messages")
        require(isinstance(batch, list) and len(batch) <= PAGE_SIZE,
                "pagination_unverified")
        stamps = []
        for row in batch:
            require(isinstance(row, dict), "pagination_unverified")
            ref = row.get("messageUrn")
            sender = _profile(row.get("senderProfileId"))
            require(isinstance(ref, str) and MESSAGE.fullmatch(ref)
                    and ref not in seen_messages
                    and sender in {client.self_profile_id, person}
                    and type(row.get("isSentByOwner")) is bool
                    and row["isSentByOwner"] == (sender == client.self_profile_id)
                    and isinstance(row.get("text"), str)
                    and _stamp(row.get("sentAt")),
                    "pagination_unverified")
            seen_messages.add(ref)
            stamp = row.get("sentAt")
            millis = int(client._seconds(stamp) * 1000)
            require(cursor is None or millis < int(cursor), "pagination_unverified")
            stamps.append(millis)
            rows.append(row)
        observation = {"source": "connectsafely", "accountId": client.account_id,
                       "complete": page.get("has_more") is False
                       and page.get("fetching_more") is False,
                       "conversationUrn": thread,
                       "participants": participants, "messages": rows}
        if observation["complete"]:
            return observation, person
        # A positive later event can obsolete the draft without proving its
        # suggested text was sent or requiring older pages to be fetched.
        if _superseded_dm(card, observation, client) is not None:
            return observation, person
        if page.get("fetching_more") is not False:
            return observation, person
        next_cursor = page.get("next_cursor")
        if (not batch or not isinstance(next_cursor, str)
                or not re.fullmatch(r"[0-9]{1,16}", next_cursor)
                or next_cursor in seen_cursors
                or int(next_cursor) != min(stamps)
                or cursor is not None and int(next_cursor) >= int(cursor)):
            return observation, person
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return {**observation, "complete": False}, person


def _single_participant_exact_dm(card, observation, client):
    """Use the verified exists binding when the provider omits one participant."""
    if observation.get("complete") is not True:
        return None
    created_ms = _card_created_ms(card)
    if created_ms is None:
        return None
    matches = [row for row in observation["messages"]
               if (row["isSentByOwner"] and row["text"] == card["texto"]
                   and row["sentAt"] > created_ms)]
    if len(matches) != 1:
        return None
    row = matches[0]
    return {"card_id": card["id"], "decision": "close",
            "proposed_status": "enviado", "reason": "positive_provider_readback",
            "evidence": {"source": "connectsafely_readback",
                         "account_id": client.account_id, "kind": "dm",
                         "reference": row["messageUrn"],
                         "target": observation["conversationUrn"],
                         "observed_at_ms": row["sentAt"],
                         "write_attribution_confirmed": False}}


def _comment_readback(client, card, binding, max_pages):
    post = binding["post_urn"]
    url = card["post_url"]
    rows = []
    details = None
    token = None
    seen_tokens = set()
    start = 0
    for _ in range(max_pages):
        body = {"accountId": client.account_id, "postUrl": url,
                "commentCount": PAGE_SIZE, "start": start}
        if token is not None:
            body["paginationToken"] = token
        page = client._call("POST", "/linkedin/posts/comments", body=body)
        client._scope(page)
        require(page.get("accountId") == client.account_id
                and post_identity(page.get("postUrl")) == post,
                "post_identity_mismatch")
        current = page.get("postDetails")
        require(isinstance(current, dict) and current.get("activityUrn") == post,
                "post_identity_mismatch")
        if details is None:
            details = current
        else:
            require(current == details, "post_identity_mismatch")
        batch = page.get("comments")
        pagination = page.get("pagination")
        require(isinstance(batch, list) and len(batch) <= PAGE_SIZE
                and isinstance(pagination, dict)
                and type(pagination.get("hasNextPage")) is bool
                and type(pagination.get("count")) is int
                and pagination["count"] == len(batch)
                and type(pagination.get("start")) is int
                and pagination["start"] == start,
                "pagination_unverified")
        rows.extend(batch)
        if "total" in pagination:
            require(type(pagination["total"]) is int
                    and pagination["total"] >= len(rows), "pagination_unverified")
        if pagination["hasNextPage"] is False:
            require(pagination.get("nextPaginationToken") is None
                    and ("total" not in pagination or pagination["total"] == len(rows)),
                    "pagination_unverified")
            return {"source": "connectsafely", "accountId": client.account_id,
                    "complete": True, "postUrl": page["postUrl"],
                    "postDetails": details, "comments": rows}
        next_token = pagination.get("nextPaginationToken")
        require(batch and isinstance(next_token, str) and 0 < len(next_token) <= 2048
                and next_token not in seen_tokens
                and ("nextStart" not in pagination
                     or pagination["nextStart"] == start + len(batch)),
                "pagination_unverified")
        seen_tokens.add(next_token)
        token = next_token
        start += len(batch)
    raise ConnectSafelyError("readback_incomplete")


def _card_created_ms(card):
    created = card.get("criado")
    if not isinstance(created, str) or len(created) > 64:
        return None
    try:
        anchor = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if anchor.tzinfo is None:
            return None
        return int(anchor.timestamp() * 1000)
    except (ValueError, OverflowError):
        return None


def _superseded_dm(card, observation, client):
    """A later thread event makes a draft stale without proving its copy sent."""
    anchor_ms = _card_created_ms(card)
    if anchor_ms is None:
        return None
    candidates = []
    for row in observation["messages"]:
        stamp_ms = int(client._seconds(row["sentAt"]) * 1000)
        if stamp_ms <= anchor_ms:
            continue
        direction = "outbound_other" if row["isSentByOwner"] else "inbound_response"
        if direction == "outbound_other" and card.get("texto") and row["text"] == card["texto"]:
            continue
        candidates.append((stamp_ms, row["messageUrn"], direction))
    if not candidates:
        return None
    stamp_ms, ref, direction = min(candidates)
    return {"card_id": card["id"], "channel": "linkedin_inbox",
            "decision": "superseded", "reason": "later_thread_event",
            "suggested_copy_sent_confirmed": False,
            "evidence": {"source": "connectsafely_readback",
                         "account_id": client.account_id,
                         "kind": direction, "reference": ref,
                         "target": observation["conversationUrn"],
                         "observed_at_ms": stamp_ms,
                         "write_attribution_confirmed": False}}


def _dm_since_event(client, card, observation, recipient, cache):
    """One scoped GET for newer evidence; this filtered page can never close."""
    anchor_ms = _card_created_ms(card)
    if anchor_ms is None or not 0 < anchor_ms < 10**16:
        return None
    thread = observation["conversationUrn"]
    key = (client.account_id, thread, recipient, anchor_ms)
    if key not in cache:
        try:
            page = client.messages(thread, limit=PAGE_SIZE, since=anchor_ms)
            require(page.get("account_id") == client.account_id
                    and page.get("conversation_urn") == thread,
                    "conversation_identity_mismatch")
            participants = page.get("participants")
            require(isinstance(participants, list) and 1 <= len(participants) <= 2
                    and all(isinstance(p, dict) for p in participants)
                    and len({_profile(p.get("profileId")) for p in participants})
                        == len(participants)
                    and {_profile(p.get("profileId")) for p in participants}
                        <= {client.self_profile_id, recipient},
                    "participants_unverified")
            rows = page.get("messages")
            require(isinstance(rows, list) and len(rows) <= PAGE_SIZE,
                    "pagination_unverified")
            seen = set()
            for row in rows:
                ref = row.get("messageUrn") if isinstance(row, dict) else None
                sender = _profile(row.get("senderProfileId")) if isinstance(row, dict) else None
                require(isinstance(ref, str) and MESSAGE.fullmatch(ref) and ref not in seen
                        and sender in {client.self_profile_id, recipient}
                        and type(row.get("isSentByOwner")) is bool
                        and row["isSentByOwner"] == (sender == client.self_profile_id)
                        and isinstance(row.get("text"), str)
                        and _stamp(row.get("sentAt")), "readback_incomplete")
                seen.add(ref)
            cache[key] = {"source": "connectsafely", "accountId": client.account_id,
                          "complete": False, "conversationUrn": thread,
                          "participants": participants, "messages": rows}
        except ConnectSafelyError as exc:
            cache[key] = exc
    if isinstance(cache[key], ConnectSafelyError):
        raise cache[key]
    return _superseded_dm(card, cache[key], client)


def audit_cards(cards, bindings, client, *, max_pages=MAX_PAGES):
    """Return one sanitized disposition per supplied card, with baseline counts.

    `cards` must be a read-only snapshot from the panel. `bindings` is keyed by
    exact card ID. No database loader, write callback, credential lookup, retry,
    or automatic provider synchronization is present in this module.
    """
    if (not isinstance(cards, list) or len(cards) > MAX_CARDS
            or not isinstance(bindings, dict)
            or type(max_pages) is not int or not 1 <= max_pages <= MAX_PAGES):
        raise ValueError("audit_input_invalid")
    counts = Counter(card.get("tipo") for card in cards if isinstance(card, dict))
    seen_cards = set()
    results = []
    cache = {}
    since_cache = {}
    for card in cards:
        card_id = _card_id(card)
        if card_id is None:
            results.append(_inconclusive(None, "card_identity_invalid"))
            continue
        if card_id in seen_cards:
            results.append(_inconclusive(card_id, "duplicate_card_id"))
            continue
        seen_cards.add(card_id)
        binding = bindings.get(card_id)
        channel = binding.get("channel") if isinstance(binding, dict) else None
        if card.get("tipo") == "whatsapp":
            results.append(_inconclusive(card_id, "whatsapp_readback_unavailable", "whatsapp"))
            continue
        try:
            require(card.get("tipo") in ("dm", "comentario"), "unsupported_card_type")
            channel = _binding(card, binding, client)
            if card.get("status") in ("enviado", "feito", "descartado"):
                observation = None
            else:
                key = (channel, client.account_id,
                       _card_public_slug(card) if channel == "linkedin_inbox"
                       else binding["post_urn"])
                if key not in cache:
                    try:
                        client.verify_identity()
                        cache[key] = (_dm_readback(client, card, binding, max_pages)
                                      if channel == "linkedin_inbox"
                                      else _comment_readback(client, card, binding, max_pages))
                    except ConnectSafelyError as exc:
                        cache[key] = exc
                if isinstance(cache[key], ConnectSafelyError):
                    raise cache[key]
                observation = cache[key][0] if channel == "linkedin_inbox" else cache[key]
                recipient = cache[key][1] if channel == "linkedin_inbox" else None
                if channel == "linkedin_inbox":
                    require(binding.get("recipient_profile_urn") in (None, recipient),
                            "recipient_identity_mismatch")
                    require(binding.get("conversation_urn") in
                            (None, observation["conversationUrn"]),
                            "conversation_identity_mismatch")
            if observation is None:
                recipient = binding.get("recipient_profile_urn")
            result = evaluate_card(
                card, channel=channel, account_id=client.account_id,
                owner_profile_urn=client.self_profile_id, observation=observation,
                recipient_profile_urn=recipient,
                recipient_profile_url=card.get("perfil_url") if channel == "linkedin_inbox" else None)
            if (channel == "linkedin_inbox" and observation is not None
                    and result["decision"] == "inconclusive"
                    and result["reason"] == "conversation_unverified"
                    and len(observation["participants"]) == 1):
                result = _single_participant_exact_dm(card, observation, client) or result
            if (channel == "linkedin_inbox" and observation is not None
                    and result["decision"] == "inconclusive"
                    and result["reason"] in ("receipt_missing_or_ambiguous",
                                             "card_content_unavailable",
                                             "readback_incomplete",
                                             "conversation_unverified")):
                # Ambiguous copies may include an exact send; never supersede it.
                exact = [row for row in observation["messages"]
                         if row["isSentByOwner"] and card.get("texto")
                         and row["text"] == card["texto"]]
                replacement = None if exact else _superseded_dm(card, observation, client)
                if replacement is None and not exact:
                    replacement = _dm_since_event(client, card, observation,
                                                  recipient, since_cache)
                results.append(replacement or {"channel": channel, **result})
            else:
                results.append({"channel": channel, **result})
        except ConnectSafelyError as exc:
            # The local client emits static diagnostics. Never include bodies,
            # tokens, raw exception text, names, or message content in the audit.
            code = str(exc)
            allowed = {"card_binding_unverified", "channel_mismatch",
                       "account_binding_unverified", "account_identity_mismatch",
                       "recipient_binding_unverified",
                       "post_binding_unverified", "recipient_identity_mismatch",
                       "conversation_unresolved", "conversation_identity_mismatch",
                       "participants_unverified", "pagination_unverified",
                       "readback_incomplete", "post_identity_mismatch",
                       "provider_unauthorized", "provider_forbidden",
                       "provider_rate_limited", "provider_not_found",
                       "provider_outcome_unknown", "provider_schema_invalid",
                       "response_too_large"}
            results.append(_inconclusive(card_id,
                                         code if code in allowed else "provider_outcome_unknown",
                                         channel))
        except (TypeError, ValueError, KeyError, AttributeError):
            results.append(_inconclusive(card_id, "readback_unverified", channel))
    receipt_owners = Counter(
        (item["evidence"]["account_id"], item["evidence"]["reference"])
        for item in results if item["decision"] == "close")
    for item in results:
        if (item["decision"] == "close"
                and receipt_owners[(item["evidence"]["account_id"],
                                    item["evidence"]["reference"])] > 1):
            item.pop("evidence")
            item.pop("proposed_status")
            item.update(decision="inconclusive", reason="shared_receipt_ambiguous")
        if "evidence" in item:
            evidence = item["evidence"]
            account = evidence.pop("account_id")
            reference = evidence.pop("reference")
            target = evidence.pop("target")
            evidence["account_sha256"] = sha256(account.encode()).hexdigest()
            evidence["reference_sha256"] = sha256(
                (account + "\0" + reference).encode()).hexdigest()
            evidence["target_sha256"] = sha256(
                (account + "\0" + target).encode()).hexdigest()
    observed = {kind: counts.get(kind, 0) for kind in EXPECTED_CARDS}
    return {"expected": dict(EXPECTED_CARDS), "observed": observed,
            "baseline_complete": observed == EXPECTED_CARDS
            and len(cards) == MAX_CARDS and len(seen_cards) == MAX_CARDS,
            "items": results, "read_only": True}


def audit_panel_snapshot(db_path, card_ids, bindings, client, *, max_pages=MAX_PAGES):
    """Read specified panel cards and evaluate them; return only sanitized data."""
    return audit_cards(read_panel_cards(db_path, card_ids), bindings, client,
                       max_pages=max_pages)
