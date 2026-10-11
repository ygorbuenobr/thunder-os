"""Durable internal work consumer; no LLM/provider is selected by this module.

Wire ``AgentWorker(service, runner=AgentRunner(trusted_transport))`` in a trusted
composition root and call ``run_once`` from its scheduler. A transport must honor
its timeout and must perform internal research/copy only, without external writes
or paid queries. HTTP/envelope context is data, never an executable command.
Without an explicitly injected runner no job is leased or executed. Completion
validates the domain artifact, persists it and updates the original work item in
one fenced transaction with the outbox acknowledgement. A crash before that
transaction may repeat legacy computation, but cannot publish a partial result.
Opt-in shared jobs additionally reserve one dispatch and cache its parsed output
before completion; interrupted inference requires reconciliation without retry.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping
from uuid import uuid4

from .agents import AgentJob, AgentResult, AgentRunner
from .db import RepositoryConflict
from .models import ContractError, WorkStatus
from .service import ThunderService
from .shared_preparation import SharedBlocked
from .validation_diagnostics import validation_diagnostic


class AgentWorker:
    def __init__(
        self, service: ThunderService, *, runner: AgentRunner | None = None,
        worker_id: str | None = None, actor: str = "codex",
        clock: Callable[[], datetime] | None = None,
    ):
        if actor not in {"codex", "claude", "hermes"}:
            raise ValueError("internal agent actor required")
        self.service = service
        self.repository = service.repository
        self.runner = runner
        self.worker_id = worker_id or f"agent-{uuid4().hex}"
        self.actor = actor
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _job(self, item: Any) -> AgentJob:
        kind = item.context.get("capability")
        if kind not in {"research", "copy"}:
            raise ContractError("explicit research or copy capability required")
        # Discard client-provided authority/context overrides. Only the canonical
        # service supplies research and commercial references to the runner.
        catalog = self.service.canonical_catalog()
        payload = {
            "objective": item.objective,
            "account_id": item.context.get("account_id"),
            "approved_capabilities": catalog["capabilities"],
            "approved_proofs": catalog["proofs"],
            "catalog_hash": catalog["content_hash"],
        }
        if item.context.get("shared_operation"):
            if self.service.shared_preparation is None:
                raise SharedBlocked("shared_preparation_not_configured")
            payload["shared_commercial_context"] = self.service.shared_preparation.check_current(item)
        if kind == "research":
            existing = self.repository.get_state("research", str(item.context.get("account_id", "")))
            payload["expected_research_version"] = existing["version"] if existing else 0
        if kind == "copy":
            account = item.context.get("account_id")
            if not isinstance(account, str) or not account:
                raise ContractError("copy requires canonical account_id")
            payload["validated_research"] = self.service.current_research(account)
            if item.context.get("shared_operation") and payload["validated_research"].get("shared_context_hash") != item.context["shared_context_hash"]:
                raise SharedBlocked("research_context_changed")
        return AgentJob(item.id, kind, payload)

    def _failure(self, item: Any, code: str, validation_error: Mapping[str, str] | None = None) -> None:
        self.service.record_job_result(item.id, item.version, {
            "dependency_failure": code,
            **({"validation_error": dict(validation_error)} if validation_error is not None else {}),
            "evidence": [{"source": f"internal:agent_job:{item.id}", "error_code": code}],
        }, self.actor)

    def run_once(self) -> dict[str, Any]:
        if self.runner is None:
            return {"status": "disabled", "reason": "agent_runner_not_configured"}
        lease = timedelta(seconds=self.runner.timeout_seconds * self.runner.max_attempts + 30)
        entry = self.repository.claim_outbox(self.worker_id, self.clock(), lease, kind="agent_job")
        if entry is None:
            return {"status": "idle"}
        item = self.repository.get_work(entry["entity_id"])
        if item is None:
            # Referential corruption is visible, and never invokes a runner.
            try:
                self.repository.finish_outbox(entry["id"], entry["lease_owner"], state="failed")
            except RepositoryConflict:
                return {"status": "lease_lost", "item_id": entry["entity_id"]}
            return {"status": "failed", "reason": "work_item_missing"}
        if item.status in {WorkStatus.SUCCEEDED, WorkStatus.FAILED, WorkStatus.NEEDS_HUMAN}:
            try:
                self.repository.finish_outbox(entry["id"], entry["lease_owner"])
            except RepositoryConflict:
                return {"status": "lease_lost", "item_id": item.id}
            return {"status": "already_completed", "item_id": item.id}
        try:
            if item.status == WorkStatus.REQUESTED:
                item = self.service.claim(item.id, item.version, self.actor)
            job = self._job(item)
            if item.context.get("shared_operation"):
                result = self.service.shared_preparation.generate_once(job, self.runner, lease_owner=entry["lease_owner"])
            else:
                result = self.runner.run(job)
        except RepositoryConflict:
            return {"status": "lease_lost", "item_id": item.id}
        except SharedBlocked as exc:
            result = AgentResult("needs_human", error_code=exc.code)
        except (ValueError, TypeError, KeyError):
            result = AgentResult("needs_human", error_code="invalid_agent_context")
        except Exception:
            # Never persist transport exception text: it may contain credentials
            # or arbitrary provider output. Process termination is not swallowed.
            result = AgentResult("needs_human", error_code="agent_transport_failed")
        try:
            with self.repository.outbox_completion(entry["id"], entry["lease_owner"]):
                current = self.repository.get_work(item.id)
                if current is None or current.version != item.version:
                    raise RepositoryConflict("work changed during agent execution")
                if result.status != "succeeded" or not result.output:
                    self._failure(current, result.error_code or "agent_failed")
                    return {"status": "needs_human", "item_id": item.id}
                if item.context.get("shared_operation"):
                    try:
                        self.service.shared_preparation.check_current(current)
                    except SharedBlocked as exc:
                        self._failure(current, exc.code)
                        return {"status": "needs_human", "item_id": item.id}
                if self.service.canonical_catalog()["content_hash"] != job.payload["catalog_hash"]:
                    raise ContractError("canonical catalog changed during execution")
                output = result.output
                if not output.get("evidence"):
                    raise ContractError("result evidence required")
                if job.type == "research":
                    expected = item.context.get("account_id")
                    account = output["artifact"].get("account")
                    if not isinstance(account, Mapping) or not isinstance(account.get("domain"), str):
                        raise ContractError("research account identity required")
                    actual = account["domain"].lower().strip()
                    if expected and actual != expected:
                        raise ContractError("research account does not match request")
                    existing = self.repository.get_state("research", actual)
                    current_version = existing["version"] if existing else 0
                    if current_version != job.payload["expected_research_version"]:
                        raise ContractError("research changed during agent execution")
                    artifact = self.service.submit_research({"dossier": output["artifact"]}, self.actor,
                        shared_work_id=item.id if item.context.get("shared_operation") else None)
                else:
                    # Re-read at completion as evidence may change while running.
                    research = self.service.current_research(str(job.payload["account_id"]))
                    if research != job.payload["validated_research"]:
                        raise ContractError("research changed during copy execution")
                    artifact = self.service.request_copy({
                        "account_id": job.payload["account_id"],
                        "capability_ids": research["capability_ids"],
                        "proof_ids": research["proof_ids"],
                        "final_copy": output["copy"],
                    }, self.actor)
                self.service.record_job_result(current.id, current.version, {
                    **dict(output), "artifact_ref": {"kind": job.type, "id": artifact["id"], "version": artifact["version"]},
                }, self.actor)
            return {"status": "succeeded", "item_id": item.id}
        except RepositoryConflict:
            return {"status": "lease_lost", "item_id": item.id}
        except (ValueError, TypeError, KeyError) as exc:
            diagnostic = validation_diagnostic(exc)
            # Domain rejection rolled back both artifact and result; record a
            # truthful actionable failure under the same fencing check.
            try:
                with self.repository.outbox_completion(entry["id"], entry["lease_owner"]):
                    current = self.repository.get_work(item.id)
                    if current is None or current.version != item.version:
                        raise RepositoryConflict("work changed during validation")
                    self._failure(current, "invalid_agent_artifact", diagnostic)
            except RepositoryConflict:
                return {"status": "lease_lost", "item_id": item.id}
            return {"status": "needs_human", "item_id": item.id, "validation_error": diagnostic}
