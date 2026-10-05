"""Bridge canonical connection research requests into internal work only."""

from __future__ import annotations

from hashlib import sha256
import re

from .connection_intake import ConnectionIntakeError, profile_key
from .connection_ledger import profile_kind, research_request_kind
from .db import RepositoryConflict
from .models import ContractError, WorkItem, payload_hash
from .service import ThunderService


CONNECTION_ACTOR = "connection-monitor"
WORK_LINK_KIND = "connection_work_link"


def canonical_connection_request(repository, connectsafely_account_id: str, profile: str):
    if (not isinstance(connectsafely_account_id, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}", connectsafely_account_id)
            or not isinstance(profile, str)):
        raise ContractError("invalid connection origin")
    try:
        if profile_key(profile) != profile:
            raise ContractError("canonical connection profile required")
    except ConnectionIntakeError as exc:
        raise ContractError("canonical connection profile required") from exc
    request = repository.get_state(research_request_kind(connectsafely_account_id), profile)
    observed = repository.get_state(profile_kind(connectsafely_account_id), profile)
    if (request is None or observed is None
            or request.get("status") != "pending" or observed.get("status") != "research"
            or request.get("profile") != profile or observed.get("profile") != profile
            or request.get("connectsafely_account_id") != connectsafely_account_id
            or observed.get("connectsafely_account_id") != connectsafely_account_id
            or request.get("account_id") != observed.get("account_id")
            or request.get("source") not in {"historical_backfill", "exact_profile_mapping"}
            or (request.get("source") == "historical_backfill"
                and observed.get("source") != "connectsafely_backfill")
            or (request.get("source") == "exact_profile_mapping"
                and observed.get("source") != "connectsafely")
            or not isinstance(request.get("fit_evidence"), str)
            or not request["fit_evidence"].strip()):
        raise ContractError("canonical connection research and fit evidence required")
    account_id = request["account_id"]
    if (not isinstance(account_id, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,251}[a-z0-9]", account_id)):
        raise ContractError("canonical account domain required")
    return request


def enqueue_connection_research(
    service: ThunderService, *, connectsafely_account_id: str, profile: str,
) -> WorkItem:
    """Create one durable research job per exact account/profile request."""
    repository = service.repository
    with repository.transaction():
        request = canonical_connection_request(repository, connectsafely_account_id, profile)
        context = {
            "capability": "research",
            "account_id": request["account_id"],
            "connectsafely_account_id": connectsafely_account_id,
            "connection_profile": profile,
            "connection_request_version": request["version"],
        }
        stable_key = sha256(f"{connectsafely_account_id}\0{profile}".encode("utf-8")).hexdigest()
        command = {"objective": f"Pesquisar conta {request['account_id']} da conexao {profile}", "context": context}
        base_key = f"connection-research:{stable_key}"
        fingerprint = payload_hash({"actor": CONNECTION_ACTOR, **command})
        existing = repository.get_by_idempotency_key(f"{base_key}:{fingerprint}")
        if existing is not None:
            link = repository.get_state(WORK_LINK_KIND, existing.id)
            if (link is None or link.get("connectsafely_account_id") != connectsafely_account_id
                    or link.get("profile") != profile
                    or link.get("request_version") != request["version"]):
                raise RepositoryConflict("connection work lacks canonical bridge link")
        item = service.request_work(command, CONNECTION_ACTOR, base_key)
        if existing is None:
            repository.append_state(WORK_LINK_KIND, item.id, {
                "connectsafely_account_id": connectsafely_account_id,
                "profile": profile,
                "request_version": request["version"],
            })
        return item
