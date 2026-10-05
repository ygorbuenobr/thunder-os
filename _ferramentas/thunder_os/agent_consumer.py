"""Paused persistent heartbeat and a separately authorized isolated single trial.

`serve` NEVER claims work, loads Hermes, or accepts an enable flag. `trial` creates
its own database and irreversibly consumes an approval receipt before launching
one inference. Restarting either mode cannot drain the real work queue.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import threading
from typing import Callable

from .agent_worker import AgentWorker
from .agents import AgentRunnerError
from .db import Repository
from .hermes_text_adapter import LIMIT, bounded_process_transport, load_catalog_context, single_attempt_runner
from .knowledge import KnowledgeStore
from .service import ThunderService


def enforce_external_gate():
    if any(os.environ.get(key, "false") != "false" for key in ("THUNDER_EXTERNAL_EFFECTS_ENABLED", "THUNDER_OS_EXTERNAL_EFFECTS_ENABLED")):
        raise AgentRunnerError("external effects must remain disabled")


def read_json(path: Path):
    with path.open("rb") as stream:
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError("trial input too large")
    return json.loads(raw)


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def source_revision():
    manifest = Path(__file__).resolve().parents[2] / "thunder-os-manifest.json"
    try:
        value = read_json(manifest).get("source_sha", "")
        return value if re.fullmatch(r"[0-9a-f]{40}", value) else "unknown"
    except (OSError, ValueError, TypeError, AttributeError):
        return "unknown"


def heartbeat(path: Path):
    enforce_external_gate()
    atomic_json(path, {"status": "paused", "pid": os.getpid(), "claims_enabled": False,
        "external_effects_enabled": False, "source_sha": source_revision(), "heartbeat_at": datetime.now(timezone.utc).isoformat()})


def serve(health: Path, *, stop: threading.Event | None = None):
    stop = stop or threading.Event()
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        heartbeat(health)
        stop.wait(10)
    return 0


def consume_approval(directory: Path, approval_id: str, request_hash: str):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", approval_id):
        raise ValueError("invalid explicit approval id")
    receipt = directory / f"{approval_id}.consumed.json"
    fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"approval_id": approval_id, "request_hash": request_hash,
            "consumed_at": datetime.now(timezone.utc).isoformat(), "max_inferences": 1,
            "timeout_seconds": 120, "external_effects_enabled": False}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# Retain only a bounded structural projection; never generated text or unknown
# field names. This is diagnostic evidence, not a reusable commercial artifact.
SAFE_FIELDS = frozenset("artifact evidence account name domain sector size business_model location digital_presence facts fact_id statement source source_area published_at observed_at freshness relevance classification explicit_intent indicators counter_signals counter_signal_assessment status details fact_ids disposition qualification_question source_coverage area query checked_at planned_urls attempted_urls discovered_urls source_urls evidence_chains implication alternative value_hypothesis refuting_question research_gaps gap_id topic description severity blocks_hypothesis inferences opportunity_hypothesis alternative_hypothesis assessment fit moment data_quality risk committee person role confidence role_since career_summary tenure_status committee_gaps thunder_fit capability_ids proof_ids knowledge_source case_evidence case_id source_ref promises_repeat_result missing limitations recommendation favorable_facts risks next_action".split())


def artifact_shape(value, depth=0):
    if depth >= 5:
        return {"type": type(value).__name__, "truncated": True}
    if isinstance(value, dict):
        return {"type": "object", "fields": {key: artifact_shape(child, depth + 1)
            for key, child in value.items() if key in SAFE_FIELDS},
            "unknown_field_count": sum(key not in SAFE_FIELDS for key in value)}
    if isinstance(value, list):
        return {"type": "array", "count": len(value),
            "items": [artifact_shape(child, depth + 1) for child in value[:10]],
            "truncated": len(value) > 10}
    return {"type": "null" if value is None else type(value).__name__}


def trial(*, trial_dir: Path, approvals: Path, approval_id: str, request_file: Path,
          evidence_file: Path, brain: Path, transport_factory: Callable, authorized: bool = False):
    enforce_external_gate()
    if not authorized:
        raise ValueError("explicit one-shot authorization required")
    command, evidence = read_json(request_file), read_json(evidence_file)
    if (not isinstance(command, dict) or set(command) != {"objective", "context"}
            or not isinstance(command["context"], dict)
            or command["context"].get("capability") != "research"
            or not command["context"].get("account_id")
            or not isinstance(evidence, dict) or evidence.get("account_id") != command["context"]["account_id"]
            or not isinstance(evidence.get("research_evidence"), list)
            or not isinstance(evidence.get("source_coverage"), list)):
        raise ValueError("one isolated research request and matching evidence required")
    from .research import SOURCE_AREAS
    coverage = evidence["source_coverage"]
    areas = [row.get("area") for row in coverage if isinstance(row, dict)]
    if (len(coverage) != len(SOURCE_AREAS) or len(areas) != len(coverage)
            or any(not isinstance(area, str) for area in areas) or set(areas) != SOURCE_AREAS):
        raise ValueError("one complete public source-coverage packet required")
    context = load_catalog_context(brain)
    # A trial database is always NEW and under the dedicated trial directory;
    # neither THUNDER_OS_DB nor the production queue is ever opened.
    if trial_dir.exists():
        raise ValueError("new isolated trial directory required")
    approvals.mkdir(parents=True, exist_ok=True, mode=0o700)
    request_hash = hashlib.sha256(json.dumps({"request": command, "evidence": evidence,
        "catalog_hash": context["catalog_hash"]}, sort_keys=True).encode()).hexdigest()
    consume_approval(approvals, approval_id, request_hash)
    trial_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    # Snapshot non-secret input after consumption; child does not read mutable
    # operator request/evidence files while the one-shot job is executing.
    snapshot = trial_dir / "evidence.json"
    atomic_json(snapshot, evidence)
    repository = Repository(trial_dir / "isolated.sqlite")
    service = ThunderService(repository, knowledge_store=KnowledgeStore(brain))
    item = service.request_work(command, "ygor", approval_id)
    items, jobs = repository.list_work(), repository.outbox()
    if len(items) != 1 or len(jobs) != 1 or jobs[0]["entity_id"] != item.id:
        raise ValueError("isolated single job required")
    transport = transport_factory(snapshot)
    def observed_transport(job, timeout):
        raw = transport(job, timeout)
        if isinstance(raw, str) and len(raw.encode("utf-8")) <= LIMIT:
            try:
                shape = artifact_shape(json.loads(raw))
            except (ValueError, TypeError, RecursionError):
                shape = {"type": "invalid_json"}
            atomic_json(trial_dir / "artifact-structure.json", shape)
        return raw
    result = AgentWorker(service, runner=single_attempt_runner(observed_transport, timeout_seconds=120), actor="hermes").run_once()
    final_item = repository.get_work(item.id)
    failure_code = (final_item.result or {}).get("dependency_failure") if final_item else "work_missing"
    receipt = {**result, "failure_code": failure_code, "approval_id": approval_id, "inference_authorization_consumed": True,
        "validation_error": (final_item.result or {}).get("validation_error") if final_item else None,
        "external_effects_enabled": False, "isolated": True}
    atomic_json(trial_dir / "result.json", receipt)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="mode", required=True)
    paused = commands.add_parser("serve")
    paused.add_argument("--health", required=True, type=Path)
    once = commands.add_parser("trial")
    for field in ("trial-dir", "approvals", "request-file", "evidence-file", "brain", "hermes-root", "profile", "python"):
        once.add_argument("--" + field, required=True, type=Path)
    once.add_argument("--approval-id", required=True)
    once.add_argument("--authorize-once", action="store_true")
    args = parser.parse_args(argv)
    try:
        enforce_external_gate()
        if args.mode == "serve":
            return serve(args.health)
        source_root = Path(__file__).resolve().parents[2]
        def factory(snapshot):
            inherited = {key: os.environ[key] for key in ("HOME", "PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy") if key in os.environ}
            env = {**inherited, "HERMES_HOME": str(args.profile.resolve()),
                "PYTHONPATH": str(source_root), "PYTHONDONTWRITEBYTECODE": "1",
                "THUNDER_EXTERNAL_EFFECTS_ENABLED": "false", "THUNDER_OS_EXTERNAL_EFFECTS_ENABLED": "false"}
            command = [str(args.python.absolute()), "-m", "_ferramentas.thunder_os.hermes_text_child",
                "--brain", str(args.brain.resolve()), "--evidence", str(snapshot.resolve()),
                "--diagnostic", str(snapshot.with_name("child-diagnostic.json")),
                "--hermes-root", str(args.hermes_root.resolve()), "--profile", str(args.profile.resolve())]
            return bounded_process_transport(command, cwd=str(source_root), env=env)
        result = trial(trial_dir=args.trial_dir.resolve(), approvals=args.approvals.resolve(),
            approval_id=args.approval_id, request_file=args.request_file, evidence_file=args.evidence_file,
            brain=args.brain, transport_factory=factory, authorized=args.authorize_once)
        print(json.dumps(result))
        return 0 if result["status"] == "succeeded" else 1
    except Exception:
        print(json.dumps({"status": "blocked", "error_code": "agent_consumer_failed", "external_effects_enabled": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
