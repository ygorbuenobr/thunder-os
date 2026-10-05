"""Bounded historical connection triage; no outreach or model invocation."""

from __future__ import annotations

from datetime import date
from typing import Any, Callable, Mapping

from .connection_intake import ConnectionIntakeError, PROVIDER_CALENDAR, _connected_at, profile_key
from .connection_ledger import profile_kind, reclassify_pending, research_request_kind
from .db import Repository, RepositoryConflict


BACKFILL_CHECKPOINT = "connection_backfill_checkpoint"
DAILY_PROFILE_LIMIT = 100
DAILY_RESEARCH_LIMIT = 5
PAGE_LIMIT = 10
ANCHOR_OVERLAP = 100


def _page(fetch_page: Callable[[int], Mapping[str, Any]], cursor: int, account_id: str):
    response = fetch_page(cursor)
    if (not isinstance(response, Mapping) or response.get("success") is not True
            or response.get("accountId") != account_id
            or response.get("sortBy") != "recentlyAdded"
            or type(response.get("startIndex")) is not int or response["startIndex"] != cursor
            or type(response.get("count")) is not int
            or not isinstance(response.get("connections"), list)
            or response["count"] != len(response["connections"])
            or len(response["connections"]) > PAGE_LIMIT
            or type(response.get("endOfList")) is not bool):
        raise ConnectionIntakeError("connection_page_invalid")
    rows = response["connections"]
    next_cursor = response.get("nextStartIndex")
    if response["endOfList"]:
        if next_cursor not in (None, cursor + len(rows)):
            raise ConnectionIntakeError("connection_cursor_invalid")
    elif not rows or type(next_cursor) is not int or next_cursor != cursor + len(rows):
        raise ConnectionIntakeError("connection_cursor_invalid")
    return rows, response["endOfList"], next_cursor


def record_backfill_batch(
    repository: Repository,
    fetch_page: Callable[[int], Mapping[str, Any]], *,
    account_id: str,
    today: date,
    account_by_profile: Mapping[str, str],
    fit_evidence_by_account: Mapping[str, str],
    max_pages: int = 25,
) -> dict[str, Any]:
    """Advance one bounded batch only after contiguous pages validate.

    The anchor protects offset pagination against a shifted or deleted prefix.
    If it disappears from the overlap, the batch fails without writing state.
    """
    if (not isinstance(account_id, str) or not account_id.strip()
            or type(today) is not date or type(max_pages) is not int or not 1 <= max_pages <= 100):
        raise ConnectionIntakeError("backfill_configuration_invalid")
    canonical_accounts = {profile_key(raw): account for raw, account in account_by_profile.items()}
    if (len(canonical_accounts) != len(account_by_profile)
            or any(not isinstance(account, str) or not account for account in canonical_accounts.values())
            or any(not isinstance(ref, str) or not ref.strip() for ref in fit_evidence_by_account.values())):
        raise ConnectionIntakeError("backfill_identity_invalid")
    checkpoint = repository.get_state(BACKFILL_CHECKPOINT, account_id)
    processed_today = checkpoint.get("processed_today", 0) if checkpoint and checkpoint.get("day") == today.isoformat() else 0
    research_today = checkpoint.get("research_today", 0) if checkpoint and checkpoint.get("day") == today.isoformat() else 0

    def reclassify(remaining: int) -> list[str]:
        transitions = reclassify_pending(
            repository, account_id=account_id,
            account_by_profile=canonical_accounts,
            fit_evidence_by_account=fit_evidence_by_account,
            max_new_research=max(0, remaining),
        )
        return [row["profile"] for row in transitions if row["status"] == "research"]

    if checkpoint and (checkpoint.get("complete") or processed_today >= DAILY_PROFILE_LIMIT):
        with repository.transaction():
            if repository.get_state(BACKFILL_CHECKPOINT, account_id) != checkpoint:
                raise RepositoryConflict("backfill_checkpoint_changed")
            promoted = reclassify(DAILY_RESEARCH_LIMIT - research_today)
            if promoted:
                research_today += len(promoted)
                repository.put_state(BACKFILL_CHECKPOINT, account_id, {
                    **{key: value for key, value in checkpoint.items()
                       if key not in {"id", "version", "updated_at"}},
                    "day": today.isoformat(), "processed_today": processed_today,
                    "research_today": research_today,
                })
        return {"processed": 0, "research": len(promoted),
                "complete": bool(checkpoint.get("complete")), "promoted": promoted}
    cursor = max(0, checkpoint["next_start_index"] - ANCHOR_OVERLAP) if checkpoint else 0
    anchor = checkpoint.get("anchor_profile") if checkpoint else None
    anchor_found = anchor is None
    last_day = None
    seen_batch: set[str] = set()
    selected: list[tuple[str, str, int]] = []
    end_of_list = False
    pages = 0
    while pages < max_pages and len(selected) < DAILY_PROFILE_LIMIT - processed_today:
        rows, end_of_list, next_cursor = _page(fetch_page, cursor, account_id)
        pages += 1
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                raise ConnectionIntakeError("connection_row_invalid")
            key = profile_key(row.get("profileUrl"))
            day = _connected_at(row.get("connectedDate")).astimezone(PROVIDER_CALENDAR).date()
            if key in seen_batch or (last_day is not None and day > last_day):
                raise ConnectionIntakeError("connection_order_invalid")
            seen_batch.add(key)
            last_day = day
            if not anchor_found:
                if key == anchor:
                    anchor_found = True
                continue
            if key == anchor:
                continue
            selected.append((key, row["connectedDate"], cursor + index + 1))
            if len(selected) == DAILY_PROFILE_LIMIT - processed_today:
                break
        if end_of_list or len(selected) == DAILY_PROFILE_LIMIT - processed_today:
            break
        cursor = next_cursor
    if not anchor_found:
        raise ConnectionIntakeError("backfill_anchor_missing")
    if not end_of_list and len(selected) < DAILY_PROFILE_LIMIT - processed_today:
        raise ConnectionIntakeError("backfill_window_incomplete")
    if not selected and not end_of_list:
        raise ConnectionIntakeError("backfill_window_incomplete")

    decisions = []
    profiles_kind = profile_kind(account_id)
    requests_kind = research_request_kind(account_id)
    with repository.transaction():
        current = repository.get_state(BACKFILL_CHECKPOINT, account_id)
        if (current or None) != (checkpoint or None):
            raise RepositoryConflict("backfill_checkpoint_changed")
        promoted = reclassify(DAILY_RESEARCH_LIMIT - research_today)
        research_today += len(promoted)
        for key, raw_date, _ in selected:
            if repository.get_state(profiles_kind, key) is not None:
                continue
            account = canonical_accounts.get(key)
            if account is None:
                status = "needs_identity"
            elif not isinstance(account, str) or not account:
                raise ConnectionIntakeError("connection_account_ambiguous")
            elif not fit_evidence_by_account.get(account):
                status = "needs_fit"
            elif research_today < DAILY_RESEARCH_LIMIT:
                status = "research"
                research_today += 1
            else:
                status = "qualified_waiting_budget"
            record = {"profile": key, "connectsafely_account_id": account_id,
                      "connected_at": _connected_at(raw_date).isoformat(),
                      "status": status, "source": "connectsafely_backfill"}
            if account is not None:
                record["account_id"] = account
            repository.append_state(profiles_kind, key, record)
            if status == "research":
                repository.append_state(requests_kind, key, {
                    "profile": key, "account_id": account,
                    "connectsafely_account_id": account_id,
                    "fit_evidence": fit_evidence_by_account[account],
                    "status": "pending", "source": "historical_backfill",
                })
            decisions.append({"profile": key, "status": status})
        total = processed_today + len(selected)
        complete = end_of_list and len(selected) < DAILY_PROFILE_LIMIT - processed_today
        repository.put_state(BACKFILL_CHECKPOINT, account_id, {
            "connectsafely_account_id": account_id,
            "day": today.isoformat(),
            "processed_today": total,
            "research_today": research_today,
            "next_start_index": selected[-1][2] if selected else (current["next_start_index"] if current else 0),
            "anchor_profile": selected[-1][0] if selected else (current["anchor_profile"] if current else None),
            "complete": complete,
        })
    return {"processed": len(selected), "research": len(promoted) + sum(d["status"] == "research" for d in decisions),
            "complete": complete, "decisions": decisions, "promoted": promoted}
