"""Classify observed LinkedIn connections without authorizing outreach.

The caller owns provider pagination and persistence. An incomplete observation
must not advance its cursor or create work.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo
import re
import unicodedata


class ConnectionIntakeError(ValueError):
    pass


def canonical_profile_slug(value: str) -> str:
    if not isinstance(value, str):
        raise ConnectionIntakeError("connection_profile_invalid")
    slug = unicodedata.normalize("NFC", value)
    if not (1 <= len(slug) <= 100 and all(char.isalnum() or char in "._-" for char in slug)):
        raise ConnectionIntakeError("connection_profile_invalid")
    return "".join(char.lower() if char.isascii() else char for char in slug)


MONTHS_PT = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8,
    "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}
PROVIDER_CALENDAR = ZoneInfo("America/Sao_Paulo")


def profile_key(value: str) -> str:
    if not isinstance(value, str):
        raise ConnectionIntakeError("connection_profile_invalid")
    try:
        parsed = urlsplit(value.strip())
        valid_authority = parsed.username is None and parsed.password is None and parsed.port is None
    except ValueError as exc:
        raise ConnectionIntakeError("connection_profile_invalid") from exc
    if (parsed.scheme != "https" or parsed.hostname not in {"linkedin.com", "www.linkedin.com"}
            or not valid_authority):
        raise ConnectionIntakeError("connection_profile_invalid")
    match = re.fullmatch(r"/in/([^/]+)/?", unquote(parsed.path))
    if not match:
        raise ConnectionIntakeError("connection_profile_invalid")
    return "https://www.linkedin.com/in/" + canonical_profile_slug(match.group(1))


def _connected_at(value: Any) -> datetime:
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            timestamp = value / 1000 if value > 10_000_000_000 else value
            result = datetime.fromtimestamp(timestamp, timezone.utc)
        elif isinstance(value, str):
            match = re.fullmatch(r"Conexão feita em ([1-9]|[12]\d|3[01]) de ([a-zç]+) de (20\d\d)", value)
            if match:
                month = MONTHS_PT.get(match.group(2))
                if month is None:
                    raise ValueError
                result = datetime(int(match.group(3)), month, int(match.group(1)), tzinfo=PROVIDER_CALENDAR)
            else:
                result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            raise ValueError
        if result.tzinfo is None:
            raise ValueError
        return result
    except (ValueError, OverflowError, OSError) as exc:
        raise ConnectionIntakeError("connection_date_invalid") from exc


def collect_recent_connections(
    fetch_page: Callable[[int], Mapping[str, Any]], *, account_id: str,
    activation_at: datetime,
    max_pages: int = 5,
) -> list[Mapping[str, Any]]:
    """Read the complete provider window back through the activation day.

    The provider promises `recentlyAdded` order. Validate that order and its
    pagination before allowing a cursor or any downstream work to advance.
    """
    if (not isinstance(activation_at, datetime) or activation_at.tzinfo is None
            or type(max_pages) is not int or not 1 <= max_pages <= 20
            or not isinstance(account_id, str) or not account_id.strip()):
        raise ConnectionIntakeError("connection_window_invalid")
    activation_day = activation_at.astimezone(PROVIDER_CALENDAR).date()
    rows: list[Mapping[str, Any]] = []
    cursor = 0
    previous = None
    profiles: set[str] = set()
    for _ in range(max_pages):
        response = fetch_page(cursor)
        if (not isinstance(response, Mapping) or response.get("success") is not True
                or response.get("accountId") != account_id
                or response.get("sortBy") != "recentlyAdded"
                or type(response.get("startIndex")) is not int or response["startIndex"] != cursor
                or not isinstance(response.get("connections"), list)):
            raise ConnectionIntakeError("connection_page_invalid")
        batch = response["connections"]
        if (len(batch) > 100 or type(response.get("count")) is not int
                or response["count"] != len(batch)):
            raise ConnectionIntakeError("connection_page_invalid")
        older = False
        for row in batch:
            if not isinstance(row, Mapping):
                raise ConnectionIntakeError("connection_row_invalid")
            key = profile_key(row.get("profileUrl"))
            day = _connected_at(row.get("connectedDate")).astimezone(PROVIDER_CALENDAR).date()
            if key in profiles or (previous is not None and day > previous):
                raise ConnectionIntakeError("connection_order_invalid")
            profiles.add(key)
            previous = day
            older |= day < activation_day
            rows.append(row)
        end = response.get("endOfList")
        next_cursor = response.get("nextStartIndex")
        if type(end) is not bool:
            raise ConnectionIntakeError("connection_page_invalid")
        if end:
            if next_cursor is not None:
                raise ConnectionIntakeError("connection_cursor_invalid")
            return rows
        if type(next_cursor) is not int or next_cursor != cursor + len(batch) or not batch:
            raise ConnectionIntakeError("connection_cursor_invalid")
        if older:
            return rows
        cursor = next_cursor
    raise ConnectionIntakeError("connection_window_incomplete")


def classify_connections(
    rows: list[Mapping[str, Any]], *, activation_at: datetime,
    seen: set[str], account_by_profile: Mapping[str, str],
    contacted: set[str], coverage_complete: bool, baseline: bool,
) -> tuple[list[dict[str, str]], set[str]]:
    """Return decisions and a new seen set; never mutate caller state.

    Exact profile-to-account mappings are the only authority for research.
    Connections without one remain visible as identity work, not copy/approval.
    """
    if coverage_complete is not True or type(baseline) is not bool:
        raise ConnectionIntakeError("connection_window_incomplete")
    if not isinstance(activation_at, datetime) or activation_at.tzinfo is None:
        raise ConnectionIntakeError("activation_date_invalid")
    canonical_accounts: dict[str, str] = {}
    for raw_profile, account in account_by_profile.items():
        key = profile_key(raw_profile)
        if (not isinstance(account, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}", account)
                or (key in canonical_accounts and canonical_accounts[key] != account)):
            raise ConnectionIntakeError("connection_account_ambiguous")
        canonical_accounts[key] = account
    canonical_contacted = {profile_key(value) for value in contacted}
    next_seen = {profile_key(value) for value in seen}
    decisions: list[dict[str, str]] = []
    activation_day = activation_at.astimezone(PROVIDER_CALENDAR).date()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ConnectionIntakeError("connection_row_invalid")
        key = profile_key(row.get("profileUrl"))
        connected_at = _connected_at(row.get("connectedDate"))
        if key in next_seen:
            continue
        next_seen.add(key)
        # Provider dates have day precision. The first complete poll must seed
        # `seen`; later polls can process same-day connections absent there.
        provider_day = connected_at.astimezone(PROVIDER_CALENDAR).date()
        if provider_day < activation_day or (baseline and provider_day == activation_day):
            continue
        status = ("already_contacted" if key in canonical_contacted else
                  "research" if key in canonical_accounts else "needs_identity")
        decision = {"profile": key, "connected_at": connected_at.isoformat(), "status": status}
        if status == "research":
            decision["account_id"] = canonical_accounts[key]
        decisions.append(decision)
    return decisions, next_seen
