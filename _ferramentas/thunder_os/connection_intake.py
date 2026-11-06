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
    def __init__(self, code: str, *, reason: str | None = None):
        super().__init__(code)
        self.reason = reason


def canonical_profile_slug(value: str) -> str:
    if not isinstance(value, str):
        raise ConnectionIntakeError("connection_profile_invalid")
    slug = value
    # Real slugs also carry other symbols (category So, e.g. a check mark): one
    # such row in a sent-invitation page failed the whole read on 2026-11-06.
    if not (1 <= len(slug) <= 100 and all(
            char.isalnum() or char in "._-\u2018\u2019" or unicodedata.category(char) in ("Sm", "So")
            for char in slug)):
        raise ConnectionIntakeError("connection_profile_invalid")
    return "".join(char.lower() if char.isascii() else char for char in slug)


# Offset pagination over a `recentlyAdded` list can repeat one row at a page
# boundary when the provider list shifts between two page reads (a connection is
# accepted or removed mid-read). Observed in production on 2026-10-31: the same
# profile, same day, last row of page N and first row of page N+1, 8.7 s apart.
PAGE_DRIFT_TOLERANCE = 2


class PageOrderGuard:
    """Validate profile uniqueness and date order across contiguous offset pages.

    Only a boundary repeat (same profile and provider day, within the last
    PAGE_DRIFT_TOLERANCE rows of the previous page and the first rows of the
    next) is skipped. Every other duplicate or order violation still blocks.
    """

    def __init__(self) -> None:
        self._first: dict[str, tuple[int, int, int, object]] = {}
        self._previous_day = None
        self._page = -1
        self._page_size = 0
        self.tolerated = 0

    def start_page(self, size: int) -> None:
        self._page += 1
        self._page_size = size

    def accept(self, key: str, day, index: int) -> bool:
        """Return True for a new row, False for a tolerated boundary repeat."""
        first = self._first.get(key)
        if first is not None:
            page, first_index, first_size, first_day = first
            if (page == self._page - 1 and first_day == day
                    and first_index >= first_size - PAGE_DRIFT_TOLERANCE
                    and index < PAGE_DRIFT_TOLERANCE):
                self.tolerated += 1
                return False
            raise ConnectionIntakeError("connection_order_invalid", reason="duplicate_profile")
        if self._previous_day is not None and day > self._previous_day:
            raise ConnectionIntakeError("connection_order_invalid", reason="date_out_of_order")
        self._first[key] = (self._page, index, self._page_size, day)
        self._previous_day = day
        return True


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


KNOWN_FRONTIER_RUN = 20


def collect_recent_connections(
    fetch_page: Callable[[int], Mapping[str, Any]], *, account_id: str,
    activation_at: datetime,
    max_pages: int = 20,
    is_known: Callable[[str], bool] | None = None,
    known_run: int = KNOWN_FRONTIER_RUN,
) -> list[Mapping[str, Any]]:
    """Read the provider window from the newest connection backwards.

    Without `is_known` (baseline), read back through the activation day. With
    `is_known`, stop after `known_run` consecutive already-persisted profiles:
    in `recentlyAdded` order everything below that frontier was observed by an
    earlier complete poll, so the window of new connections is complete without
    re-reading the whole history every poll. The provider promises
    `recentlyAdded` order; validate it before any cursor or work advances.
    """
    if (not isinstance(activation_at, datetime) or activation_at.tzinfo is None
            or type(max_pages) is not int or not 1 <= max_pages <= 20
            or not isinstance(account_id, str) or not account_id.strip()
            or (is_known is not None and not callable(is_known))
            or type(known_run) is not int or not 1 <= known_run <= 100):
        raise ConnectionIntakeError("connection_window_invalid")
    activation_day = activation_at.astimezone(PROVIDER_CALENDAR).date()
    rows: list[Mapping[str, Any]] = []
    cursor = 0
    guard = PageOrderGuard()
    known_streak = 0
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
        guard.start_page(len(batch))
        for index, row in enumerate(batch):
            if not isinstance(row, Mapping):
                raise ConnectionIntakeError("connection_row_invalid")
            key = profile_key(row.get("profileUrl"))
            day = _connected_at(row.get("connectedDate")).astimezone(PROVIDER_CALENDAR).date()
            if not guard.accept(key, day, index):
                continue
            older |= day < activation_day
            rows.append(row)
            if is_known is not None:
                known_streak = known_streak + 1 if is_known(key) else 0
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
        if older or (is_known is not None and known_streak >= known_run):
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
