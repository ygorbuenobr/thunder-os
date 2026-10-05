"""Pure, read-only evaluation of existing panel cards against provider readback.

The caller must bind the ConnectSafely account and LinkedIn identities before
calling this function, and must assemble every page into one complete readback.
The result is a proposed panel disposition, never a database or provider write.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from urllib.parse import urlsplit

from .connectsafely_social_client import COMMENT, post_identity


PROFILE = re.compile(r"urn:li:fsd_profile:[A-Za-z0-9_-]+")
MESSAGE = re.compile(r"urn:li:(?:messagingMessage:[A-Za-z0-9_.=-]+|msg_message:\(urn:li:fsd_profile:[A-Za-z0-9_-]+,[A-Za-z0-9_.=-]+\))")
CHANNEL_TYPES = {"linkedin_inbox": "dm", "linkedin_comment": "comentario", "whatsapp": "whatsapp"}
CLOSED = frozenset(("enviado", "feito"))


def _profile(value: object) -> str | None:
    if isinstance(value, str) and PROFILE.fullmatch(value):
        return value
    if isinstance(value, str) and re.fullmatch(r"ACo[A-Za-z0-9_-]+", value):
        return "urn:li:fsd_profile:" + value
    return None


def _stamp(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _profile_url(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 2048:
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (parsed.scheme == "https" and parsed.netloc == "www.linkedin.com"
            and re.fullmatch(r"/in/[A-Za-z0-9_.-]+/?", parsed.path) is not None
            and not parsed.query and not parsed.fragment)


def _result(card_id: str, reason: str, *, evidence: dict | None = None) -> dict:
    if evidence is None:
        return {"card_id": card_id, "decision": "inconclusive", "reason": reason}
    return {"card_id": card_id, "decision": "close", "proposed_status": "enviado",
            "reason": "positive_provider_readback", "evidence": evidence}


def evaluate_card(card: dict, *, channel: str, account_id: str | None,
                  owner_profile_urn: str | None, observation: dict | None,
                  recipient_profile_urn: str | None = None,
                  recipient_profile_url: str | None = None) -> dict:
    """Evaluate one existing card against caller-supplied ConnectSafely pages.

    `observation` is an assembled raw readback with `source` set to
    `connectsafely`, `accountId`, and `complete=True` only after pagination
    finishes. DM readback needs `conversationUrn`, exactly two `participants`,
    and `messages`. Comment readback needs `postDetails` and `comments` from
    POST /linkedin/posts/comments. The caller must independently verify the
    account and the card-to-recipient profile binding; these fields are not
    inferred from a person's name. Results contain no message body.
    """
    if (not isinstance(card, dict) or not isinstance(card.get("id"), str)
            or not card["id"].strip()):
        raise ValueError("card_identity_invalid")
    card_id = card["id"]
    if channel not in CHANNEL_TYPES or card.get("tipo") != CHANNEL_TYPES[channel]:
        return _result(card_id, "channel_mismatch")
    if channel == "whatsapp":
        return _result(card_id, "whatsapp_readback_unavailable")
    if card.get("status") in CLOSED:
        return {"card_id": card_id, "decision": "unchanged", "reason": "already_closed"}
    if card.get("status") == "descartado":
        return _result(card_id, "card_not_actionable")
    content = card.get("texto")
    if not isinstance(content, str) or not content.strip():
        return _result(card_id, "card_content_unavailable")
    created = card.get("criado")
    try:
        created_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            raise ValueError
        created_ms = int(created_at.timestamp() * 1000)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return _result(card_id, "card_creation_unverified")
    if (not isinstance(account_id, str) or not re.fullmatch(r"[a-f0-9]{24}", account_id)
            or not isinstance(owner_profile_urn, str)
            or _profile(owner_profile_urn) != owner_profile_urn):
        return _result(card_id, "identity_unverified")
    if not isinstance(observation, dict) or observation.get("source") != "connectsafely":
        return _result(card_id, "readback_unverified")
    if observation.get("accountId") != account_id:
        return _result(card_id, "account_mismatch")
    if observation.get("complete") is not True:
        return _result(card_id, "readback_incomplete")
    if channel == "linkedin_inbox":
        if (not isinstance(recipient_profile_urn, str)
                or _profile(recipient_profile_urn) != recipient_profile_urn
                or recipient_profile_urn == owner_profile_urn
                or not _profile_url(recipient_profile_url)
                or card.get("perfil_url") != recipient_profile_url):
            return _result(card_id, "recipient_identity_unverified")
        thread = observation.get("conversationUrn")
        participants = observation.get("participants")
        rows = observation.get("messages")
        if (not isinstance(thread, str) or not thread.startswith("urn:li:msg_conversation:")
                or not isinstance(participants, list) or len(participants) != 2
                or not all(isinstance(p, dict) for p in participants)
                or {_profile(p.get("profileId")) for p in participants}
                   != {owner_profile_urn, recipient_profile_urn}
                or not isinstance(rows, list)):
            return _result(card_id, "conversation_unverified")
        matches = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                return _result(card_id, "readback_incomplete")
            ref = row.get("messageUrn")
            if (not isinstance(ref, str) or not MESSAGE.fullmatch(ref)
                    or _profile(row.get("senderProfileId")) is None
                    or type(row.get("isSentByOwner")) is not bool
                    or not isinstance(row.get("text"), str)
                    or not _stamp(row.get("sentAt"))):
                return _result(card_id, "readback_incomplete")
            sender = _profile(row["senderProfileId"])
            if (ref in seen or sender not in {owner_profile_urn, recipient_profile_urn}
                    or (sender == owner_profile_urn) != row["isSentByOwner"]):
                return _result(card_id, "readback_incomplete")
            seen.add(ref)
            if (row.get("isSentByOwner") is True
                    and sender == owner_profile_urn and row.get("text") == content
                    and row["sentAt"] > created_ms):
                matches.append((ref, row["sentAt"]))
        kind = "dm"
        target = thread
    else:
        try:
            post = post_identity(card.get("post_url"))
        except (ValueError, TypeError):
            return _result(card_id, "post_identity_unverified")
        details = observation.get("postDetails")
        rows = observation.get("comments")
        if (not isinstance(details, dict) or details.get("activityUrn") != post
                or not isinstance(rows, list)):
            return _result(card_id, "post_identity_unverified")
        if observation.get("postUrl") is not None:
            try:
                if post_identity(observation["postUrl"]) != post:
                    return _result(card_id, "post_identity_unverified")
            except (ValueError, TypeError):
                return _result(card_id, "post_identity_unverified")
        parents = {post}
        for key, prefix in (("ugcPostUrn", "urn:li:ugcPost:"),
                            ("shareUrn", "urn:li:share:")):
            value = details.get(key)
            if value is not None:
                if not isinstance(value, str) or not re.fullmatch(prefix + r"[0-9]{1,25}", value):
                    return _result(card_id, "post_identity_unverified")
                parents.add(value)
        matches = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                return _result(card_id, "readback_incomplete")
            ref = row.get("commentId")
            match = COMMENT.fullmatch(ref) if isinstance(ref, str) else None
            author_urn = _profile(row.get("commenterUrn"))
            author_id = _profile(row.get("commenterProfileId"))
            if (match is None or not isinstance(row.get("commentText"), str)
                    or not _stamp(row.get("createdAt"))
                    or (author_urn is None and author_id is None)
                    or (author_urn is not None and author_id is not None
                        and author_urn != author_id)):
                return _result(card_id, "readback_incomplete")
            if ref in seen or match.group(2) not in parents:
                return _result(card_id, "readback_incomplete")
            seen.add(ref)
            if (match and match.group(2) in parents
                    and (author_urn or author_id) == owner_profile_urn
                    and row.get("commentText") == content
                    and row["createdAt"] > created_ms):
                matches.append((ref, row["createdAt"]))
        kind = "comment"
        target = post
    if len(matches) != 1:
        return _result(card_id, "receipt_missing_or_ambiguous")
    ref, observed_at_ms = matches[0]
    return _result(card_id, "positive_provider_readback", evidence={
        "source": "connectsafely_readback", "account_id": account_id,
        "kind": kind, "reference": ref, "target": target,
        "observed_at_ms": observed_at_ms,
        "write_attribution_confirmed": False,
    })
