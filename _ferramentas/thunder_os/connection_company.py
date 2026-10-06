"""Bounded company candidates; name-only evidence never authorizes work."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from .connection_catalog import company_name_key
from .connection_intake import ConnectionIntakeError, profile_key
from .connection_ledger import profile_kind
from .connection_provider import ConnectionProviderError
from .db import Repository


DAILY_LOOKUP_LIMIT = 100
PER_RUN_LOOKUP_LIMIT = 2


def resolution_kind(account_id: str) -> str:
    return f"connection_company_resolution:{account_id}"


def observe_pending_companies(repository: Repository, account_id: str,
                              catalog: dict[str, tuple[str, str]],
                              fetch_company: Callable[[str], tuple[str | None, str | None]],
                              now: datetime) -> dict[str, object]:
    """Reserve provider budget before each GET; failures never create a match."""
    if now.tzinfo is None:
        raise ConnectionIntakeError("company_observation_invalid")
    day = now.astimezone(ZoneInfo("America/Sao_Paulo")).date().isoformat()
    budget_kind = "connection_company_lookup_budget"
    budget_id = f"{account_id}:{day}"
    initial_budget = repository.get_state(budget_kind, budget_id)
    if initial_budget and initial_budget.get("rate_limited") is True:
        return {"checked": 0, "candidates": 0, "error": "provider_rate_limited"}
    if initial_budget and initial_budget.get("in_flight") is True:
        return {"checked": 0, "candidates": 0, "error": "company_lookup_interrupted"}
    records = sorted(repository.list_state(profile_kind(account_id)), key=lambda row: row["id"])
    checked = candidates = 0
    error = None
    for source in records:
        if checked >= PER_RUN_LOOKUP_LIMIT:
            break
        if source.get("status") != "needs_identity" or source.get("connectsafely_account_id") != account_id:
            continue
        profile = source["id"]
        if source.get("profile") != profile or profile_key(profile) != profile:
            raise ConnectionIntakeError("company_observation_invalid")
        prior = repository.get_state(resolution_kind(account_id), profile)
        if prior is not None:
            if prior.get("status") == "lookup_error" and prior.get("day") != day:
                pass
            else:
                if (prior.get("status") in {"needs_identity", "candidate"}
                        and prior.get("company_consistent") is True):
                    match = catalog.get(prior.get("company_name"))
                    payload = {key: value for key, value in prior.items()
                               if key not in {"id", "version", "updated_at"}}
                    payload.pop("candidate_domain", None)
                    payload.pop("catalog_ref", None)
                    payload["status"] = "candidate" if match else "needs_identity"
                    if match:
                        payload.update(candidate_domain=match[0], catalog_ref=match[1])
                    if payload != {key: value for key, value in prior.items()
                                   if key not in {"id", "version", "updated_at"}}:
                        repository.put_state(resolution_kind(account_id), profile, payload)
                continue
        with repository.transaction():
            budget = repository.get_state(budget_kind, budget_id)
            used = budget["used"] if budget else 0
            if (used >= DAILY_LOOKUP_LIMIT or budget and
                    (budget.get("rate_limited") is True or budget.get("in_flight") is True)):
                break
            repository.put_state(budget_kind, budget_id, {
                "connectsafely_account_id": account_id, "day": day,
                "used": used + 1, "in_flight": True})
        checked += 1
        try:
            current, experience = fetch_company(profile)
        except ConnectionProviderError as exc:
            error = str(exc)
            failure = {"profile": profile, "connectsafely_account_id": account_id,
                       "day": day, "status": "lookup_error", "error_code": error}
            with repository.transaction():
                if prior is None:
                    repository.append_state(resolution_kind(account_id), profile, failure)
                else:
                    repository.put_state(resolution_kind(account_id), profile, failure)
                repository.put_state(budget_kind, budget_id, {
                        "connectsafely_account_id": account_id, "day": day,
                        "used": used + 1, "in_flight": False,
                        "rate_limited": error == "provider_rate_limited"})
            if error == "provider_rate_limited":
                break
            continue
        name = company_name_key(current) if current else None
        previous = company_name_key(experience) if experience else None
        match = catalog.get(name) if name and name == previous else None
        payload: dict[str, object] = {
            "profile": profile, "connectsafely_account_id": account_id,
            "observed_at": now.astimezone(timezone.utc).isoformat(),
            "status": "candidate" if match else "needs_identity",
        }
        if name:
            payload["company_name"] = name
        payload["company_consistent"] = bool(name and name == previous)
        if match:
            payload.update(candidate_domain=match[0], catalog_ref=match[1])
            candidates += 1
        with repository.transaction():
            if prior is None:
                repository.append_state(resolution_kind(account_id), profile, payload)
            else:
                repository.put_state(resolution_kind(account_id), profile, payload)
            repository.put_state(budget_kind, budget_id, {
                "connectsafely_account_id": account_id, "day": day,
                "used": used + 1, "in_flight": False})
    return {"checked": checked, "candidates": candidates, "error": error}
