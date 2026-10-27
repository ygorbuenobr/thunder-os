"""Effect coordinator that reserves once and reconciles before any provider call."""

from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import argparse
import json
import os
from pathlib import Path
import signal
import time
from threading import Event, Thread
from contextlib import contextmanager
from typing import Any, Mapping

from .connectsafely_console import ConnectSafelyError
from .approvals import ApprovalPack, ApprovalService, ApprovalError, _step_hash
from .cadence import effective_due
from .service import ThunderService
from .knowledge import KnowledgeStore
from .db import Repository, RepositoryConflict
from .models import effect_payload_hash, payload_hash
from .providers.email import EmailAdapter
from .providers.result import ProviderResult
from .touch_ledger import InvalidTouchScope, TouchBlocked, TouchLedger


class EffectBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class EffectResult:
    touch_id: str
    effect_hash: str
    status: str
    receipt_ref: str | None = None
    error_code: str | None = None


class EffectCoordinator:
    def __init__(self, ledger: Any, approvals: ApprovalService, adapters: Mapping[str, Any]):
        self.ledger = ledger
        self.approvals = approvals
        self.adapters = dict(adapters)

    def execute_due(
        self, step: Mapping[str, Any], pack: ApprovalPack, now: datetime, *,
        suppressed: bool = False, has_reply: bool = False,
    ) -> EffectResult:
        # Payload-only callers have no canonical campaign confirmation yet.
        raise EffectBlocked("campaign_confirmation_integration_required")
        if suppressed:
            raise EffectBlocked("suppressed")
        if has_reply:
            raise EffectBlocked("has_reply")
        authorization = self.approvals.authorize_step(step, pack, now)
        channel = str(step["channel"])
        adapter = self.adapters.get(channel)
        if adapter is None:
            raise EffectBlocked("channel unavailable")
        try:
            created, touch = self.ledger.reserve(authorization.effect_hash, authorization.step_id, step)
        except TypeError:  # Compatibility for bounded in-memory ledgers.
            created, touch = self.ledger.reserve(authorization.effect_hash, authorization.step_id)
        if not created:
            status = str(touch["status"])
            if status == "reserved":
                status = "needs_reconciliation"
            return EffectResult(
                str(touch["id"]), authorization.effect_hash, status,
                touch.get("receipt_ref"), touch.get("error_code"),
            )
        effect = {"target": step["recipient"], "content": step["content"], "effect_id": step["id"]}
        if channel == "email":
            effect["subject"] = step.get("subject")
        provider = adapter.send(effect)
        status = {
            "confirmed": "sent", "rejected": "failed", "uncertain": "uncertain",
            "not_configured": "failed",
        }.get(provider.status, "uncertain")
        stored = self.ledger.finish(
            authorization.effect_hash, status, provider.receipt_ref, provider.error_code
        )
        return EffectResult(
            str(stored["id"]), authorization.effect_hash, str(stored["status"]),
            stored.get("receipt_ref"), stored.get("error_code"),
        )


class WorkerRuntime:
    def __init__(
        self, repository: Repository, ledger: TouchLedger, adapters: Mapping[str, Any],
        *, worker_id: str, health_path: Path, revision: str, knowledge_store=None, approved_catalog=None, connectsafely_app=None, campaign_context_loader=None, adapter_provider_ids=None, adapter_account_ids=None, native_executor=None,
    ):
        self.repository = repository
        self.connectsafely_app = connectsafely_app
        self.native_executor = native_executor
        native_readers = dict(native_executor.native_readers) if native_executor is not None else {}
        if native_readers and set(native_readers) != {"source", "research", "crm"}:
            raise ValueError("native_reader_composition_invalid")
        native_options = ({"native_readers":native_readers} if native_executor is not None else {})
        if native_executor is not None and getattr(native_executor, "native_calendar", None) is not None:
            native_options["native_calendar"] = deepcopy(native_executor.native_calendar)
        self.authorization_service = ThunderService(repository, knowledge_store=knowledge_store, approved_catalog=approved_catalog, campaign_context_loader=campaign_context_loader, **native_options)
        self.adapter_provider_ids = dict(adapter_provider_ids or {k:getattr(v,"provider_name",None) for k,v in adapters.items()})
        self.adapter_account_ids = dict(adapter_account_ids or {k:getattr(v,"account_id",None) for k,v in adapters.items()})
        self.ledger = ledger
        self.adapters = dict(adapters)
        self.worker_id = worker_id
        self.health_path = health_path
        self.revision = revision
        self.running = True
        self.processed = 0
        self.last_progress_at = None
        self._last_health = 0.0
        self._reconciliation_cursor = 0
        self._native_reconciliation_seen = set()

    def stop(self, *_args: Any) -> None:
        self.running = False

    def _health(self, state: str) -> None:
        value = {
            "status": state, "pid": os.getpid(), "source_sha": self.revision,
            "worker_id": self.worker_id, "uncertain_retry": False,
            "external_effects_enabled": True,
            "processed": self.processed, "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            "readiness": state == "ok", "last_progress_at": self.last_progress_at,
            "campaign_confirmation_required": True,
            "capabilities": {channel: getattr(adapter, "capability", "injected") for channel, adapter in self.adapters.items()},
        }
        self.health_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.health_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        temporary.replace(self.health_path)
        self._last_health = time.monotonic()

    def process_one(self) -> bool:
        self._reconcile_registered_effects()
        if self.connectsafely_app is not None:
            try:
                if self.connectsafely_app.process_one():
                    self.processed += 1
                    self.last_progress_at = datetime.now(timezone.utc).isoformat()
                    self._health("ok")
                    return True
            except ConnectSafelyError:
                # A retained legacy job cannot starve an independently approved batch.
                pass
        job = self.repository.claim_effect(
            self.worker_id, datetime.now(timezone.utc), timedelta(seconds=30)
        )
        if not job:
            return False
        if self._is_native(job):
            return self._process_native(job)
        if self._reconcile_duplicate_effect(job, claimed=True):
            return True
        payload = job["payload"]
        def finish(state, receipt=None, error=None):
            self.repository.finish_effect(job["id"], state, receipt, error, lease_owner=job["lease_owner"])
        step = payload.get("step")
        if not isinstance(step, dict):
            finish("failed", None, "invalid_effect_job")
            return True
        try:
            expected = (payload_hash({
                "action": "like", "person_key": step.get("recipient"), "post_url": step.get("post_url"),
            }) if step.get("action") == "like" else effect_payload_hash(
                target=step.get("recipient"), channel=step.get("channel"),
                content=step.get("content"), order=step.get("order"),
                interval_hours=step.get("interval_hours"), subject=step.get("subject"),
            ))
        except (TypeError, ValueError):
            finish("failed", None, "invalid_effect_job")
            self.processed += 1
            self._health("ok")
            return True
        if payload.get("approval_hash") != expected:
            finish("failed", None, "approval_hash_mismatch")
            return True
        gate_error = self._runtime_gate(payload, step, expected)
        if gate_error:
            if gate_error in {"global_pause", "delegation_disabled_or_out_of_scope", "step_not_due", "predecessor_not_confirmed"}:
                self.repository.defer_effect(job["id"], job["lease_owner"], gate_error)
            else:
                finish("failed", None, gate_error)
            self.processed += 1
            self._health("ok")
            return True
        adapter = self.adapters.get(str(step.get("channel")))
        if adapter is None or getattr(adapter, "capability", None) == "disabled":
            finish("failed", None, "channel_not_configured")
            return True
        prepare = getattr(adapter, "prepare_job", None)
        if callable(prepare):
            try:
                prepare(job)
            except Exception:
                finish("failed", None, "adapter_scope_not_authorized")
                return True
        try:
            created, touch = self.ledger.reserve(expected, str(step["id"]), step)
            if not created:
                state = "needs_reconciliation" if touch["status"] == "reserved" else touch["status"]
                finish(state, touch.get("receipt_ref"), touch.get("error_code"))
                return True
            effect = {
                "target": step.get("post_url", step["recipient"]),
                "content": step["content"], "effect_id": step["id"]
            }
            if step["channel"] == "email":
                effect["subject"] = step.get("subject")
            # Same SQLite write transaction fences canonical campaign config,
            # approvals, pause/revocation, job ownership and receipt persistence.
            # Do not heartbeat from another connection while owning this fence.
            with self.repository.effect_dispatch_fence(job["id"], job["lease_owner"]):
                gate_error = self._runtime_gate(payload, step, expected)
                if gate_error:
                    finish("needs_reconciliation", None, gate_error)
                    return True
                try:
                    result = adapter.send(effect)
                    state = {"confirmed": "sent", "rejected": "failed", "uncertain": "uncertain", "not_configured": "failed"}.get(result.status, "uncertain")
                    stored = self.ledger.finish(expected, state, result.receipt_ref, result.error_code)
                    finish(stored["status"], stored.get("receipt_ref"), stored.get("error_code"))
                except Exception:
                    # Commit uncertainty inside the fence; never roll authority
                    # back into a replayable job after a possible remote effect.
                    finish("uncertain", None, "worker_execution_uncertain")
        except TouchBlocked as exc:
            finish("failed", None, str(exc))
        except RepositoryConflict:
            # Another claimant owns the job; the touch remains the reconciliation authority.
            pass
        except Exception:
            # Never turn an unknown post-reservation failure into a safe retry.
            try:
                finish("uncertain", None, "worker_execution_uncertain")
            except RepositoryConflict:
                pass
        self.processed += 1
        self.last_progress_at = datetime.now(timezone.utc).isoformat()
        self._health("ok")
        return True

    def _is_native(self, job):
        payload = job.get("payload", {})
        step = payload.get("step", {})
        if isinstance(step, dict) and "native" in step:
            return True
        cadence = self.repository.get_state("cadence", str(payload.get("cadence_id", "")))
        return bool(cadence and "native_context" in cadence)

    def _native_runtime_gate(self, job):
        if self.native_executor is None:
            return "native_executor_not_configured"
        payload = job["payload"]
        step = payload.get("step")
        if not isinstance(step, dict) or "native" not in step:
            return "native_scope_missing"
        from .native_contract import validate_native_context, validate_native_step, evaluate_native_schedule
        try:
            cadence = self.repository.get_state("cadence", str(payload.get("cadence_id", "")))
            approval = self.repository.get_state("approval", str(payload.get("cadence_id", "")))
            if not cadence or cadence.get("status") != "approved":
                return "cadence_not_approved"
            if not approval or approval.get("status") != "active":
                return "approval_not_active"
            if self.authorization_service.global_paused():
                return "global_pause"
            if payload.get("actor") not in {"ygor", "hermes (delegado)"}:
                return "native_actor_invalid"
            if payload.get("actor") == "hermes (delegado)":
                delegation = self.repository.get_state("delegation", "hermes") or {}
                if not delegation.get("enabled") or "followup" not in delegation.get("scopes", []):
                    return "delegation_disabled_or_out_of_scope"
            context = cadence.get("native_context")
            if payload.get("native_context") != context:
                return "native_context_changed"
            validate_native_context(context)
            validate_native_step(step["native"], context)
            current = next((s for s in cadence.get("steps", []) if s.get("id") == step.get("id")), None)
            expected = _step_hash(ThunderService._approval_step(step))
            if (not current or _step_hash(ThunderService._approval_step(current)) != expected
                    or expected != payload.get("approval_hash") or expected != job["effect_key"]
                    or approval.get("step_hashes", {}).get(str(step["id"])) != expected):
                return "approval_scope_changed"
            if payload.get("execution_confirmation_hash") != payload_hash(approval.get("execution_confirmation")):
                return "campaign_confirmation_scope_changed"
            if datetime.now(timezone.utc) >= datetime.fromisoformat(approval["expires_at"].replace("Z", "+00:00")):
                return "approval_expired"
            binding = self.native_executor.binding_for(job, context)
            self.authorization_service.validate_campaign_execution(cadence, approval,
                channel=step["channel"], provider=binding["provider"])
            registry = self.authorization_service.campaign_registry.binding(cadence["execution_context"]["campaign_id"])
            if (registry["provider_accounts"].get(step["channel"]) != binding["account_id"]
                    or step["native"]["sender_account"] != binding["account_id"]):
                return "campaign_account_mismatch"
            error = self.authorization_service.identity_execution_error(cadence)
            if error:
                return error
            research = self.authorization_service.current_research(str(cadence["account_id"]))
            if cadence.get("research_hash") != payload_hash({"version": research["version"], "facts": research["facts"]}):
                return "research_scope_changed"
            schedule = evaluate_native_schedule(cadence, current, self.repository.effects(), datetime.now(timezone.utc))
            if schedule["state"] != "due":
                return schedule.get("reason") or "step_not_due"
            return None
        except (ValueError, KeyError, TypeError) as exc:
            return str(exc) if isinstance(exc, ValueError) and str(exc) else "native_scope_invalid"

    def _process_native(self, job):
        def finish(state, receipt=None, error=None):
            self.repository.finish_effect(job["id"], state, receipt, error, lease_owner=job["lease_owner"])
        try:
            error = self._native_runtime_gate(job)
            if error:
                if error == 'native_calendar_wait' and self.native_executor.preparation_absent(job):
                    from .native_contract import evaluate_native_schedule
                    cadence = self.repository.get_state('cadence',job['payload']['cadence_id'])
                    step = next(s for s in cadence['steps'] if s['id']==job['payload']['step']['id'])
                    now = datetime.now(timezone.utc)
                    schedule = evaluate_native_schedule(cadence,step,self.repository.effects(),now)
                    due = datetime.fromisoformat(schedule['due_at']) if schedule.get('due_at') else None
                    if schedule['state']=='approved' and due is not None and due>now:
                        self.repository.defer_effect(job['id'],job['lease_owner'],error,delay=due-now)
                        return True
                # No unknown scope is ever converted into a safe automatic retry.
                finish("needs_reconciliation" if self.native_executor is not None else "failed", error=error)
                return True
            self.native_executor.prepare_job(job)
            def before_dispatch():
                error = self._native_runtime_gate(job)
                current = self.repository.effect(job["id"])
                if (not current or current["state"] != "pending"
                        or current.get("lease_owner") != job["lease_owner"]
                        or not current.get("lease_until")
                        or datetime.fromisoformat(current["lease_until"]) <= datetime.now(timezone.utc)
                        or current["payload"] != job["payload"]):
                    raise EffectBlocked("native_job_changed")
                if error:
                    raise EffectBlocked(error)
                return True
            with self.repository.effect_dispatch_fence(job["id"], job["lease_owner"]):
                try:
                    before_dispatch()
                    result = self.native_executor.execute_job(job, before_dispatch)
                    if not isinstance(result, ProviderResult) or result.effect_id != job['payload']['step']['id']:
                        raise EffectBlocked('native_result_scope_mismatch')
                    state = {"confirmed":"sent", "rejected":"failed", "uncertain":"uncertain"}.get(result.status, "uncertain")
                    finish(state, result.receipt_ref, result.error_code)
                except Exception:
                    finish("uncertain", error="native_execution_uncertain")
        except RepositoryConflict:
            pass
        except Exception as exc:
            # Preparation can have a durable reservation even if local persistence failed.
            try:
                finish("needs_reconciliation", error=str(exc) if isinstance(exc, ValueError) else "native_preparation_uncertain")
            except RepositoryConflict:
                pass
        finally:
            self.processed += 1
            self.last_progress_at = datetime.now(timezone.utc).isoformat()
            self._health("ok")
        return True

    def _reconcile_duplicate_effect(self, job, *, claimed=False) -> bool:
        if self._is_native(job):
            # Native LI owns its only reservation in the CS ledger, not this ledger.
            return False
        reader = getattr(self.ledger, "duplicate_evidence", None)
        payload = job["payload"]
        step = payload.get("step") if isinstance(payload, dict) else None
        if reader is None:
            return False
        try:
            if not isinstance(step, dict):
                raise InvalidTouchScope("invalid_effect_touch_scope")
            evidence = reader(job["effect_key"], step)
        except InvalidTouchScope:
            try:
                self._record_invalid_effect_scope(job, claimed=claimed)
            except RepositoryConflict:
                pass
            return True
        # Database/unknown reader failures propagate and block dispatch.
        if evidence is None:
            return False
        try:
            self.repository.mark_effect_reconciliation(job, evidence)
        except RepositoryConflict:
            # A changed claim/receipt invalidates this snapshot, never permits a send.
            pass
        return True

    def _record_invalid_effect_scope(self, job, *, claimed):
        code = "invalid_effect_touch_scope"
        alert_id = "invalid-effect-scope:" + job["id"]
        with self.repository.transaction():
            current = self.repository.effect(job["id"])
            fields = ("effect_key", "payload", "state", "updated_at", "lease_owner",
                      "lease_until", "receipt_ref", "error_code")
            if not current or any(current[field] != job[field] for field in fields):
                raise RepositoryConflict("effect changed during scope validation")
            if claimed:
                self.repository.finish_effect(job["id"], "needs_reconciliation",
                    job["receipt_ref"], code, lease_owner=job["lease_owner"])
            if not self.repository.get_state("operator_alert", alert_id):
                self.repository.append_state("operator_alert", alert_id, {
                    "active": True, "priority": "high", "owner": "ygor", "code": code,
                    "reason": code, "effect_id": job["id"], "effect_key": job["effect_key"],
                    "title": "Revisar escopo de toque invalido",
                    "message": "Efeito registrado com escopo invalido. Historico e recibos preservados.",
                    "next_action": "Ygor: revisar o efeito e reconciliar o historico sem reenviar.",
                })

    def _reconcile_registered_effects(self) -> None:
        jobs = self.repository.reconciliation_effects(after_rowid=self._reconciliation_cursor)
        for job in jobs:
            if self._is_native(job):
                if self.native_executor is not None and job['state'] in {'uncertain','needs_reconciliation'}:
                    self._reconcile_native_registered(job)
            else:
                self._reconcile_duplicate_effect(job)
            self._reconciliation_cursor = job["reconciliation_cursor"]
        if len(jobs) < 100:
            self._reconciliation_cursor = 0

    def _reconcile_native_registered(self, job):
        snapshot = self.repository.effect(job['id'])
        marker = (snapshot['id'], snapshot['updated_at'], snapshot['state'])
        if marker in self._native_reconciliation_seen:
            return
        self._native_reconciliation_seen.add(marker)
        cadence_id = snapshot['payload'].get('cadence_id')
        cadence = self.repository.get_state('cadence',cadence_id)
        try:
            result = self.native_executor.reconcile_job(snapshot)
            if (not isinstance(result, ProviderResult) or result.effect_id != snapshot['payload']['step']['id']
                    or result.status != 'confirmed' or not result.receipt_ref):
                return
            with self.repository.transaction():
                if self.repository.get_state('cadence',cadence_id) != cadence:
                    return
                self.repository.confirm_uncertain_receipt(snapshot,result.receipt_ref)
        except Exception:
            # A failed/ambiguous read never renews approval or requeues a send.
            return

    @contextmanager
    def _keep_lease(self, job):
        stop = Event()
        def maintain():
            while not stop.wait(5):
                try:
                    self.repository.renew_lease("thunder_effect_jobs", job["id"], job["lease_owner"])
                    self._health("ok")
                except Exception:
                    return
        thread = Thread(target=maintain, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=1)

    def _final_campaign_gate(self, payload, step):
        # Re-read after all preflight I/O, including authority/control changes.
        if self.authorization_service.global_paused():
            return "global_pause"
        cadence = self.repository.get_state("cadence", str(payload.get("cadence_id", "")))
        approval = self.repository.get_state("approval", str(payload.get("cadence_id", "")))
        if not cadence or cadence.get("status") != "approved" or not approval or approval.get("status") != "active":
            return "approval_not_active"
        if payload.get("execution_confirmation_hash") != payload_hash(approval.get("execution_confirmation")):
            return "campaign_confirmation_scope_changed"
        try:
            self.authorization_service.validate_campaign_execution(
                cadence, approval, channel=str(step.get("channel")),
                provider=self.adapter_provider_ids.get(str(step.get("channel"))))
            identity_error = self.authorization_service.identity_execution_error(cadence)
            if identity_error:
                return identity_error
            binding = self.authorization_service.campaign_registry.binding(cadence["execution_context"]["campaign_id"])
            channel = str(step.get("channel"))
            if (not self.adapter_account_ids.get(channel)
                    or binding["provider_accounts"].get(channel) != self.adapter_account_ids[channel]):
                return "campaign_account_mismatch"
            adapter = self.adapters.get(channel)
            if (getattr(adapter,"provider_name",None) != self.adapter_provider_ids.get(channel)
                    or getattr(adapter,"account_id",None) != self.adapter_account_ids.get(channel)):
                return "campaign_adapter_identity_unverified"
        except ApprovalError as exc:
            return str(exc)
        return None

    def _runtime_gate(self, payload: Mapping[str, Any], step: Mapping[str, Any], expected: str) -> str | None:
        control = self.repository.get_state("control", "global")
        if control and control.get("paused"):
            return "global_pause"
        if payload.get("actor") == "hermes (delegado)":
            action = payload.get("delegated_action", "followup" if payload.get("cadence_id") else None)
            delegation = self.repository.get_state("delegation", "hermes")
            if (not delegation or not delegation.get("enabled")
                    or action not in delegation.get("scopes", [])):
                return "delegation_disabled_or_out_of_scope"
        cadence_id = payload.get("cadence_id")
        if not cadence_id:
            return "campaign_confirmation_integration_required"
            # Likes have a separate, immutable producer authorization. Neither
            # actor strings nor fake adapters are authority for orphan jobs.
            grant=self.repository.get_state("effect_authorization",expected)
            if (payload.get("delegated_action")!="like" or step.get("action")!="like"
                    or step.get("channel")!="linkedin" or step.get("content")!="like"
                    or not grant or grant.get("action")!="like" or grant.get("authorized_by")!="ygor"
                    or grant.get("actor")!=payload.get("actor") or grant.get("payload_hash")!=payload_hash(payload)
                    or grant.get("day")!=datetime.now(timezone.utc).date().isoformat()):
                return "approval_not_active"
            if payload.get("actor")=="hermes (delegado)":
                if delegation.get("actor")!="ygor" or grant.get("delegation_version")!=delegation["version"]:
                    return "delegation_authorization_changed"
            elif payload.get("actor")!="ygor":return "approval_not_active"
            try:self.ledger.preflight(expected,step,raise_blocked=True)
            except TouchBlocked as exc:return str(exc)
            return None
        cadence = self.repository.get_state("cadence", str(cadence_id))
        approval = self.repository.get_state("approval", str(cadence_id))
        if not cadence or cadence.get("status") != "approved":
            return "cadence_not_approved"
        if not approval or approval.get("status") != "active":
            return "approval_not_active"
        if payload.get("execution_confirmation_hash") != payload_hash(approval.get("execution_confirmation")):
            return "campaign_confirmation_scope_changed"
        try:
            self.authorization_service.validate_campaign_execution(
                cadence, approval, channel=str(step.get("channel")),
                provider=self.adapter_provider_ids.get(str(step.get("channel"))))
            identity_error = self.authorization_service.identity_execution_error(cadence)
            if identity_error:
                return identity_error
        except ApprovalError as exc:
            return str(exc)
        if datetime.now(timezone.utc) >= datetime.fromisoformat(str(approval["expires_at"])):
            return "approval_expired"
        current = next((value for value in cadence.get("steps", []) if value.get("id") == step.get("id")), None)
        if not current or approval.get("step_hashes", {}).get(str(step.get("id"))) != expected:
            return "approval_scope_changed"
        if _step_hash(ThunderService._approval_step(current)) != expected:
            return "approval_scope_changed"
        try:
            research = self.authorization_service.current_research(str(cadence.get("account_id", "")))
            if cadence.get("research_hash") != payload_hash({"version": research["version"], "facts": research["facts"]}):
                return "research_scope_changed"
        except ValueError:
            return "research_not_current"
        due = effective_due(current, self.repository.effects(), str(cadence_id))
        if due is None:
            return "predecessor_not_confirmed"
        if datetime.now(timezone.utc) < due:
            return "step_not_due"
        if current.get("dependency_status") in {"failed", "uncertain", "blocked"}:
            return "dependency_blocked"
        predecessor_id = current.get("predecessor_id")
        if predecessor_id:
            predecessor = next((effect for effect in self.repository.effects()
                                if effect["payload"].get("cadence_id") == cadence_id
                                and effect["payload"].get("step", {}).get("id") == predecessor_id), None)
            if not predecessor or predecessor["state"] != "sent":
                return "predecessor_not_confirmed"
        try:
            self.ledger.preflight(expected, current, raise_blocked=True)
        except (TouchBlocked, ApprovalError) as exc:
            return str(exc)
        return self._final_campaign_gate(payload, step)

    def run(self, poll_seconds: float = 0.1) -> None:
        self._health("ok")
        while self.running:
            try:
                worked = self.process_one()
            except Exception:
                self._health("degraded")
                time.sleep(min(1, poll_seconds))
                continue
            if time.monotonic() - self._last_health >= 5:
                self._health("ok")
            if not worked:
                time.sleep(poll_seconds)
        self._health("stopped")


class _FakeTransport:
    def __init__(self, prefix: str):
        self.prefix = prefix

    def __call__(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return {"ok": True, "provider_id": f"{self.prefix}-{payload['effect_id']}"}


class DisabledRuntime:
    """Health-only runtime: composition gate runs before any database/provider setup."""
    def __init__(self):
        self.running = True
        self.health_path = Path(os.environ.get("THUNDER_OS_WORKER_HEALTH", "/var/lib/thunder-os/worker-health.json"))
        self.revision = os.environ.get("THUNDER_OS_SOURCE_SHA", "unknown")

    def stop(self, *_args):
        self.running = False

    def _health(self, _state=None):
        value = {
            "status": "disabled", "readiness": False, "pid": os.getpid(),
            "source_sha": self.revision, "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            "external_effects_enabled": False, "disabled_reason": "external_effects_disabled",
            "processed": 0, "uncertain_retry": False,
            "capabilities": {"linkedin": "disabled", "email": "disabled", "hubspot": "disabled"},
        }
        self.health_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.health_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        temporary.replace(self.health_path)

    def process_one(self):
        self._health()
        return False

    def run(self, poll_seconds=0.1):
        last_health = 0.0
        while self.running:
            if time.monotonic() - last_health >= 5:
                self._health()
                last_health = time.monotonic()
            time.sleep(poll_seconds)


class _SimulatedLinkedInAdapter:
    """Offline effect result. Cannot accept or call a provider transport."""
    def send(self, effect):
        allowed = {"target", "content", "effect_id"}
        if set(effect) != allowed or not all(isinstance(effect.get(k), str) and effect[k].strip() for k in allowed):
            raise ValueError("invalid linkedin effect")
        return ProviderResult(effect["effect_id"], "confirmed", "simulation:linkedin:" + effect["effect_id"])


def build_runtime(*, connectsafely_app=None) -> WorkerRuntime | DisabledRuntime:
    if os.environ.get("THUNDER_EXTERNAL_EFFECTS_ENABLED", "").lower() != "true":
        return DisabledRuntime()
    database = os.environ.get("THUNDER_OS_DB") or os.environ.get("PAINEL_DB")
    if not database:
        raise RuntimeError("THUNDER_OS_DB or PAINEL_DB is required")
    ledger = TouchLedger()
    ledger.migrate()
    repository = Repository(database)
    fake = os.environ.get("THUNDER_OS_FAKE_PROVIDERS") == "1"
    adapters: dict[str, Any]
    if fake:
        adapters = {
            "linkedin": _SimulatedLinkedInAdapter(),
            "email": EmailAdapter("fake", _FakeTransport("email")),
        }
    else:
        from .thunder_email_runtime import configured_adapter
        adapters = {"email": configured_adapter(repository)}
    if fake:
        for adapter in adapters.values():
            adapter.capability = "simulated"
    return WorkerRuntime(
        repository, ledger, adapters,
        worker_id=os.environ.get("THUNDER_OS_WORKER_ID", "worker-1"),
        health_path=Path(os.environ.get("THUNDER_OS_WORKER_HEALTH", "/var/lib/thunder-os/worker-health.json")),
        revision=os.environ.get("THUNDER_OS_SOURCE_SHA", "unknown"),
        connectsafely_app=connectsafely_app,
        knowledge_store=KnowledgeStore(os.environ.get("THUNDER_BRAIN_ROOT", str(Path(__file__).resolve().parents[2]))),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Thunder OS durable effect worker")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    runtime = build_runtime()
    signal.signal(signal.SIGTERM, runtime.stop)
    signal.signal(signal.SIGINT, runtime.stop)
    if args.once:
        runtime._health("ok")
        runtime.process_one()
        runtime._health("stopped")
    else:
        runtime.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
