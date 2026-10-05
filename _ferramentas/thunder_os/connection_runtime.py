"""GET-only scheduled connection intake, separate from outreach workers."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import sys
from typing import Callable, Mapping, Any
from zoneinfo import ZoneInfo

from .connection_backfill import record_backfill_batch
from .connection_catalog import load_connection_catalog
from .connection_intake import ConnectionIntakeError
from .connection_ledger import LIVE_CHECKPOINT, record_live_poll, research_request_kind
from .connection_provider import ConnectionProvider, ConnectionProviderError
from .connectsafely_transport import ConnectSafelyHTTPS
from .db import Repository
from .connection_work import WORK_LINK_KIND, enqueue_connection_research
from .service import ThunderService


class ConnectionRuntimeError(ValueError):
    pass


def _protected_file(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError:
        raise ConnectionRuntimeError("protected_file_unavailable") from None
    if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
            or info.st_uid not in {0, os.geteuid()}):
        raise ConnectionRuntimeError("protected_file_invalid")


def load_config(path: Path) -> dict[str, Any]:
    _protected_file(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise ConnectionRuntimeError("connection_config_invalid") from None
    required = {"version", "repository", "account_id", "account_public_id", "api_credential_file"}
    if not isinstance(data, dict) or not required.issubset(data) or data["version"] != 1:
        raise ConnectionRuntimeError("connection_config_invalid")
    for key in required - {"version"}:
        if not isinstance(data[key], str) or not data[key]:
            raise ConnectionRuntimeError("connection_config_invalid")
    credential = Path(data["api_credential_file"])
    if not credential.is_absolute():
        raise ConnectionRuntimeError("connection_config_invalid")
    _protected_file(credential)
    database = Path(data["repository"])
    if not database.is_absolute() or database.is_symlink() or not database.is_file():
        raise ConnectionRuntimeError("connection_database_unavailable")
    return data


def _provider(config: Mapping[str, Any]) -> ConnectionProvider:
    credential_path = Path(config["api_credential_file"])

    def credential() -> str:
        _protected_file(credential_path)
        try:
            return credential_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            raise ConnectionRuntimeError("credential_unavailable") from None

    transport = ConnectSafelyHTTPS(config["account_id"], credential)

    def get_only(method, path, query, body, *, timeout):
        if method != "GET" or body is not None or timeout != 15:
            raise ConnectionRuntimeError("read_only_transport_required")
        status, decoded = transport(method, path, query, None)
        return status, json.dumps(decoded, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    return ConnectionProvider(config["account_id"], config["account_public_id"], get_only)


def run_once(config: Mapping[str, Any], *, brain: Path, mode: str,
             fetch_page: Callable[[int], Mapping[str, Any]],
             now: datetime | None = None) -> dict[str, Any]:
    if mode not in {"live", "backfill"} or not brain.is_dir() or brain.is_symlink():
        raise ConnectionRuntimeError("connection_runtime_invalid")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ConnectionRuntimeError("connection_runtime_invalid")
    repository = Repository(config["repository"])
    mapping, fit = load_connection_catalog(brain)
    account_id = config["account_id"]
    if mode == "live":
        checkpoint = repository.get_state(LIVE_CHECKPOINT, account_id)
        activation_at = datetime.fromisoformat(checkpoint["activation_at"]) if checkpoint else now
        decisions = record_live_poll(
            repository, fetch_page, account_id=account_id,
            activation_at=activation_at, observed_at=now,
            account_by_profile=mapping, fit_evidence_by_account=fit, contacted=set(),
        )
        return {"mode": mode, "observed": len(decisions), "research": sum(
            row["status"] == "research" for row in decisions),
            "jobs_ensured": _enqueue_pending(repository, account_id, now)}
    result = record_backfill_batch(
        repository, fetch_page, account_id=account_id,
        today=now.astimezone(ZoneInfo("America/Sao_Paulo")).date(),
        account_by_profile=mapping, fit_evidence_by_account=fit,
    )
    return {"mode": mode, "observed": result["processed"],
            "research": result["research"], "complete": result["complete"],
            "jobs_ensured": _enqueue_pending(repository, account_id, now)}


def _enqueue_pending(repository: Repository, account_id: str, now: datetime) -> int:
    service = ThunderService(repository)
    count = 0
    day = now.astimezone(ZoneInfo("America/Sao_Paulo")).date().isoformat()
    with repository.transaction():
        budget = repository.get_state("connection_research_budget", day)
        used = budget["used"] if budget else 0
        linked = {(row["connectsafely_account_id"], row["profile"])
                  for row in repository.list_state(WORK_LINK_KIND)}
        for request in repository.list_state(research_request_kind(account_id)):
            if used >= 5:
                break
            profile = request.get("profile")
            if request.get("status") != "pending" or (account_id, profile) in linked:
                continue
            enqueue_connection_research(
                service, connectsafely_account_id=account_id, profile=profile)
            linked.add((account_id, profile))
            used += 1
            count += 1
        if count:
            repository.put_state("connection_research_budget", day, {"day": day, "used": used})
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--brain", type=Path, required=True)
    parser.add_argument("--mode", choices=("live", "backfill"), required=True)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        provider = _provider(config)
        result = run_once(config, brain=args.brain, mode=args.mode, fetch_page=provider.fetch_page)
    except (ConnectionRuntimeError, ConnectionProviderError, ConnectionIntakeError) as exc:
        print(json.dumps({"status": "blocked", "error_code": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({"status": "ok", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
