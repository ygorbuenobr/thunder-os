"""GET-only ConnectSafely connection pages for the connection intake.

The injected transport performs authentication and returns (status, raw JSON
bytes). It must honor the supplied timeout; this module never loads secrets or
constructs a network client. A page is accepted only after account readback.
"""

from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Callable, Mapping

from .connection_intake import (
    ConnectionIntakeError, PROVIDER_CALENDAR, _connected_at, canonical_profile_slug, profile_key,
)


MAX_RESPONSE_BYTES = 1_000_000
PAGE_LIMIT = 10
ENGLISH_MONTHS = {name: month for month, name in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
ENGLISH_MONTHS.update({name: month for month, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"), 1)})


class ConnectionProviderError(ValueError):
    """Static error codes only; provider bodies and transport errors stay private."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ConnectionProviderError(code)


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        _require(key not in result, "provider_schema_invalid")
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    raise ConnectionProviderError("provider_schema_invalid")


def _date(value: object) -> str:
    _require(isinstance(value, (str, int, float)) and type(value) is not bool,
             "connection_row_invalid")
    if isinstance(value, str):
        _require(0 < len(value) <= 100, "connection_row_invalid")
        match = re.fullmatch(r"Connected on ([A-Z][a-z]+) ([1-9]|[12]\d|3[01]), (20\d\d)", value)
        if match:
            try:
                return datetime(int(match[3]), ENGLISH_MONTHS[match[1]], int(match[2]),
                                tzinfo=PROVIDER_CALENDAR).isoformat()
            except (KeyError, ValueError):
                raise ConnectionProviderError("connection_row_invalid") from None
    try:
        return _connected_at(value).isoformat()
    except ConnectionIntakeError:
        raise ConnectionProviderError("connection_row_invalid") from None


class ConnectionProvider:
    def __init__(self, account_id: str, public_id: str,
                 transport: Callable[..., tuple[int, bytes]], *, timeout_seconds: float = 15):
        _require(isinstance(account_id, str) and re.fullmatch(r"[a-f0-9]{24}", account_id) is not None
                 and isinstance(public_id, str)
                 and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", public_id) is not None,
                 "account_configuration_invalid")
        _require(callable(transport) and type(timeout_seconds) in (int, float)
                 and 0 < timeout_seconds <= 30, "transport_configuration_invalid")
        self.account_id = account_id
        self.public_id = public_id
        self.transport = transport
        self.timeout_seconds = timeout_seconds

    def _get(self, path: str, query: Mapping[str, str | int]) -> object:
        try:
            status, raw = self.transport("GET", path, dict(query), None,
                                         timeout=self.timeout_seconds)
        except TimeoutError:
            raise ConnectionProviderError("provider_timeout") from None
        except Exception:
            raise ConnectionProviderError("provider_outcome_unknown") from None
        _require(type(status) is int, "provider_schema_invalid")
        if status != 200:
            code = {401: "provider_unauthorized", 403: "provider_forbidden",
                    429: "provider_rate_limited", 502: "provider_unavailable"}.get(
                        status, "provider_outcome_unknown")
            raise ConnectionProviderError(code)
        _require(type(raw) is bytes and len(raw) <= MAX_RESPONSE_BYTES,
                 "response_too_large" if type(raw) is bytes else "provider_schema_invalid")
        try:
            return json.loads(raw, object_pairs_hook=_object, parse_constant=_reject_constant)
        except (UnicodeError, ValueError):
            raise ConnectionProviderError("provider_schema_invalid") from None

    def _verify_identity(self) -> None:
        accounts = self._get("/linkedin/accounts", {"q": self.account_id})
        _require(type(accounts) is list and len(accounts) <= 100, "provider_schema_invalid")
        _require(all(type(row) is dict for row in accounts), "provider_schema_invalid")
        matches = [row for row in accounts if row.get("id") == self.account_id]
        _require(len(matches) == 1 and matches[0].get("publicId") == self.public_id
                 and sum(row.get("publicId") == self.public_id for row in accounts) == 1,
                 "account_identity_mismatch")

    def fetch_page(self, start_index: int) -> dict[str, object]:
        """Return one validated page; a partial nonterminal page is incomplete."""
        _require(type(start_index) is int and 0 <= start_index <= 1_000_000,
                 "invalid_pagination")
        self._verify_identity()
        payload = self._get("/linkedin/connections", {
            "accountId": self.account_id, "startIndex": start_index,
            "limit": PAGE_LIMIT, "sortBy": "recentlyAdded",
        })
        _require(type(payload) is dict, "provider_schema_invalid")
        _require(payload.get("accountId") == self.account_id, "account_identity_mismatch")
        _require(payload.get("success") is True and payload.get("sortBy") == "recentlyAdded"
                 and type(payload.get("startIndex")) is int
                 and payload["startIndex"] == start_index
                 and ("limit" not in payload or type(payload["limit"]) is int
                      and payload["limit"] == PAGE_LIMIT), "provider_schema_invalid")
        rows = payload.get("connections")
        _require(type(rows) is list and len(rows) <= PAGE_LIMIT
                 and type(payload.get("count")) is int and payload["count"] == len(rows)
                 and type(payload.get("endOfList")) is bool
                 and type(payload.get("nextStartIndex")) is int,
                 "provider_schema_invalid")
        next_index = payload["nextStartIndex"]
        _require(next_index == start_index + len(rows)
                 and (payload["endOfList"] or bool(rows)), "pagination_unverified")
        output: list[dict[str, str]] = []
        seen: set[str] = set()
        for row in rows:
            _require(type(row) is dict, "connection_row_invalid")
            vanity = row.get("vanityName")
            _require(isinstance(vanity, str) and 1 <= len(vanity) <= 100
                     and all(char.isalnum() or char in "._-" for char in vanity),
                     "connection_row_invalid")
            try:
                key = profile_key(row.get("profileUrl"))
            except ConnectionIntakeError:
                raise ConnectionProviderError("connection_row_invalid") from None
            _require(key == "https://www.linkedin.com/in/" + canonical_profile_slug(vanity)
                     and key not in seen, "connection_row_invalid")
            seen.add(key)
            clean = {"vanityName": vanity, "profileUrl": key,
                     "connectedDate": _date(row.get("connectedDate"))}
            for field in ("firstName", "lastName", "headline"):
                value = row.get(field)
                if value is not None:
                    _require(isinstance(value, str) and len(value) <= 8000
                             and not any(ord(char) < 32 for char in value),
                             "connection_row_invalid")
                    clean[field] = value
            output.append(clean)
        return {"success": True, "accountId": self.account_id,
                "sortBy": "recentlyAdded", "startIndex": start_index,
                "count": len(output), "connections": output,
                "endOfList": payload["endOfList"],
                "nextStartIndex": None if payload["endOfList"] else next_index}
