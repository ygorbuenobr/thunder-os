"""Persist complete ConnectSafely observations in the Thunder OS state store.

The provider read finishes before any write. Each account has its own profile
and research namespaces; the live checkpoint never shares a backfill cursor.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any, Callable, Mapping

from .connection_intake import (
    ConnectionIntakeError, _connected_at, classify_connections,
    collect_recent_connections, profile_key,
)
from .db import Repository


LIVE_CHECKPOINT = "connection_live_checkpoint"


def profile_kind(account_id: str) -> str:
    return f"connection_profile:{account_id}"


def research_request_kind(account_id: str) -> str:
    return f"connection_research_request:{account_id}"


def reclassify_pending(
    repository: Repository, *, account_id: str,
    account_by_profile: Mapping[str, str],
    fit_evidence_by_account: Mapping[str, str],
    max_new_research: int | None = None,
) -> list[dict[str, str]]:
    """Advance persisted identity/fit work against one versioned catalog snapshot.

    The caller may hold a transaction with a validated provider window. A
    changed or missing exact mapping never silently replaces a known account.
    """
    if (not isinstance(account_id, str) or not account_id.strip()
            or max_new_research is not None
            and (type(max_new_research) is not int or max_new_research < 0)):
        raise ConnectionIntakeError("connection_transition_invalid")
    canonical: dict[str, str] = {}
    for raw_profile, account in account_by_profile.items():
        key = profile_key(raw_profile)
        if (not isinstance(account, str)
                or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}", account)
                or key in canonical and canonical[key] != account):
            raise ConnectionIntakeError("connection_account_ambiguous")
        canonical[key] = account
    if any(not isinstance(ref, str) or not ref.strip()
           for ref in fit_evidence_by_account.values()):
        raise ConnectionIntakeError("connection_fit_evidence_invalid")
    mapping_provenance = "account_by_profile#sha256:" + sha256(json.dumps(
        canonical, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    catalog_version = "sha256:" + sha256(json.dumps(
        {"accounts": canonical, "fit_evidence": dict(fit_evidence_by_account)},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    kind = profile_kind(account_id)
    request_kind = research_request_kind(account_id)
    changed: list[dict[str, str]] = []
    research_count = 0

    with repository.transaction():
        records = repository.list_state(kind)
        records.sort(key=lambda row: row.get("status") != "qualified_waiting_budget")
        for record in records:
            status = record.get("status")
            if status not in {"historical", "needs_identity", "needs_fit", "qualified_waiting_budget"}:
                continue
            profile = record["id"]
            if (record.get("profile") != profile
                    or record.get("connectsafely_account_id") != account_id
                    or record.get("source") not in {"connectsafely", "connectsafely_backfill"}):
                raise ConnectionIntakeError("connection_transition_origin_invalid")
            account = canonical.get(profile)
            previous_account = record.get("account_id")
            if account is not None and previous_account is not None and account != previous_account:
                raise ConnectionIntakeError("connection_account_ambiguous")
            if account is None:
                if status == "historical":
                    payload = {key: value for key, value in record.items()
                               if key not in {"id", "version", "updated_at"}}
                    payload.update(status="needs_identity", catalog_version=catalog_version,
                                   mapping_provenance=mapping_provenance)
                    repository.put_states_atomic([(kind, profile, payload, record["version"])])
                    changed.append({"profile": profile, "status": "needs_identity",
                                    "catalog_version": catalog_version})
                continue
            evidence = fit_evidence_by_account.get(account)
            if status == "qualified_waiting_budget" and not evidence:
                continue
            if not evidence:
                next_status = "needs_fit"
            elif max_new_research is not None and research_count >= max_new_research:
                next_status = "qualified_waiting_budget"
            else:
                next_status = "research"
            if status == next_status and previous_account == account:
                continue
            if repository.get_state(request_kind, profile) is not None:
                raise ConnectionIntakeError("connection_request_conflict")
            payload = {key: value for key, value in record.items()
                       if key not in {"id", "version", "updated_at"}}
            payload.update(status=next_status, account_id=account,
                           catalog_version=catalog_version,
                           mapping_provenance=mapping_provenance)
            if evidence:
                payload["fit_evidence"] = evidence
            updated = repository.put_states_atomic([(kind, profile, payload, record["version"])])[0]
            if next_status == "research":
                repository.append_state(request_kind, profile, {
                    "profile": profile,
                    "connectsafely_account_id": account_id,
                    "account_id": account,
                    "connected_at": record["connected_at"],
                    "fit_evidence": evidence,
                    "catalog_version": catalog_version,
                    "mapping_provenance": mapping_provenance,
                    "profile_version": updated["version"],
                    "status": "pending",
                    "source": ("exact_profile_mapping" if record["source"] == "connectsafely"
                               else "historical_backfill"),
                })
                research_count += 1
            changed.append({"profile": profile, "status": next_status,
                            "account_id": account, "catalog_version": catalog_version})
    return changed


def record_live_poll(
    repository: Repository,
    fetch_page: Callable[[int], Mapping[str, Any]], *,
    account_id: str,
    activation_at: datetime,
    observed_at: datetime,
    account_by_profile: Mapping[str, str],
    fit_evidence_by_account: Mapping[str, str],
    contacted: set[str],
    max_pages: int = 20,
) -> list[dict[str, str]]:
    """Store one complete live window and return decisions for newly seen profiles.

    `account_by_profile` must be the exact profile mapping for this ConnectSafely
    account. A first complete poll establishes the baseline on activation day.
    """
    if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
        raise ConnectionIntakeError("connection_observation_invalid")
    if not isinstance(activation_at, datetime) or activation_at.tzinfo is None:
        raise ConnectionIntakeError("activation_date_invalid")
    observed = observed_at.astimezone(timezone.utc).isoformat()
    activation = activation_at.astimezone(timezone.utc).isoformat()
    rows = collect_recent_connections(
        fetch_page, account_id=account_id, activation_at=activation_at, max_pages=max_pages,
    )
    normalized = [
        (profile_key(row["profileUrl"]), _connected_at(row["connectedDate"]).isoformat())
        for row in rows
    ]
    digest = sha256(json.dumps(normalized, separators=(",", ":")).encode("utf-8")).hexdigest()
    kind = profile_kind(account_id)
    request_kind = research_request_kind(account_id)

    with repository.transaction():
        checkpoint = repository.get_state(LIVE_CHECKPOINT, account_id)
        if checkpoint is not None:
            if checkpoint["activation_at"] != activation:
                raise ConnectionIntakeError("connection_activation_changed")
            if observed < checkpoint["observed_at"]:
                raise ConnectionIntakeError("connection_poll_stale")
            if observed == checkpoint["observed_at"] and digest != checkpoint["window_digest"]:
                raise ConnectionIntakeError("connection_poll_conflict")

        seen = {key for key, _ in normalized if repository.get_state(kind, key) is not None}
        decisions, _ = classify_connections(
            rows, activation_at=activation_at, seen=seen,
            account_by_profile=account_by_profile, contacted=contacted,
            coverage_complete=True, baseline=checkpoint is None,
        )
        for decision in decisions:
            if decision["status"] == "research":
                evidence = fit_evidence_by_account.get(decision["account_id"])
                if not isinstance(evidence, str) or not evidence.strip():
                    decision["status"] = "needs_fit"
        transitions = reclassify_pending(
            repository, account_id=account_id,
            account_by_profile=account_by_profile,
            fit_evidence_by_account=fit_evidence_by_account,
        )
        if checkpoint is not None and observed == checkpoint["observed_at"]:
            if len(seen) != len(normalized):
                raise ConnectionIntakeError("connection_poll_conflict")
            return transitions

        by_profile = {decision["profile"]: decision for decision in decisions}
        for key, connected_at in normalized:
            if key in seen:
                continue
            decision = by_profile.get(key)
            status = decision["status"] if decision else "historical"
            record = {
                "profile": key,
                "connectsafely_account_id": account_id,
                "connected_at": connected_at,
                "first_observed_at": observed,
                "status": status,
                "source": "connectsafely",
            }
            if decision and "account_id" in decision:
                record["account_id"] = decision["account_id"]
            repository.append_state(kind, key, record)
            if status == "research":
                repository.append_state(request_kind, key, {
                    "profile": key,
                    "connectsafely_account_id": account_id,
                    "account_id": decision["account_id"],
                    "fit_evidence": fit_evidence_by_account[decision["account_id"]],
                    "connected_at": connected_at,
                    "status": "pending",
                    "source": "exact_profile_mapping",
                })
        repository.put_state(LIVE_CHECKPOINT, account_id, {
            "connectsafely_account_id": account_id,
            "activation_at": activation,
            "observed_at": observed,
            "window_digest": digest,
            "window_count": len(normalized),
        })
    return decisions + transitions
