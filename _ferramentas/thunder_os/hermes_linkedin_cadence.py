"""Deterministic Hermes executor for human-approved LinkedIn cadence tasks.

The module owns no provider credentials and generates no copy.  A caller injects
the HubSpot task port and the already composed ConnectSafely application.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from .connectsafely_console import ConnectSafelyError
from .db import RepositoryConflict
from .models import ContractError, payload_hash


class CadenceBlocked(ContractError):
    """Stable fail-closed outcome; the HubSpot task must remain open."""


STEP_CONTRACT = {
    "cad_li_0": ("LinkedIn: convite", "invite"),
    "cad_li_1": ("LinkedIn: msg 1", "dm"),
    "cad_li_2": ("LinkedIn: msg 2", "dm"),
    "cad_li_3": ("LinkedIn: msg 3", "dm"),
}
ACTIVE_CADENCE_STATUSES = {"aprovado", "em_cadencia"}


def _clean_state(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key not in {"id", "version", "updated_at"}}


def _iso(value: Any) -> datetime:
    if not isinstance(value, str):
        raise CadenceBlocked("invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CadenceBlocked("invalid_timestamp") from exc
    if parsed.tzinfo is None:
        raise CadenceBlocked("invalid_timestamp")
    return parsed


def plan_hash(plan: Mapping[str, Any]) -> str:
    return payload_hash({"connectsafely_plan": dict(plan)})


def approval_payload_hash(payload: Mapping[str, Any]) -> str:
    return payload_hash({key: payload[key] for key in (
        "hubspot_contact_id", "cadence_id", "expires_at", "steps",
    )})


def _task_contract(task: Mapping[str, Any], step: Mapping[str, Any], clock) -> dict[str, Any]:
    return {
        "subject": task["subject"], "body": task["body"], "contact_ids": list(task["contact_ids"]),
        "content": step["plan"]["content"],
        # The queue time and attempt time do not prove the provider's send time.
        "sent_at": "nao_comprovado",
    }


def revoke_approval(repository, approval_id: Any, expected_version: Any, actor: str) -> dict[str, Any]:
    """Human undo for a D0 approval; every dispatch guard requires status active."""
    if actor != "ygor":
        raise CadenceBlocked("human_authority_required")
    if not isinstance(approval_id, str) or not approval_id or type(expected_version) is not int:
        raise CadenceBlocked("invalid_cadence_revocation")
    with repository.transaction():
        current = repository.get_state("linkedin_cadence_approval", approval_id)
        if not current:
            raise CadenceBlocked("approval_missing")
        if current.get("version") != expected_version:
            raise RepositoryConflict("stale cadence approval")
        if current.get("status") == "revoked":
            return current
        # A queued or attempted effect needs this authority to reconcile its
        # receipt; revoking it would strand the task as uncertain. Use the
        # delegation kill switch for in-flight work instead.
        for task in repository.list_state("linkedin_cadence_task"):
            if (task.get("approval_id") == approval_id
                    and task.get("status") in {"pending", "uncertain", "provider_confirmed"}):
                raise CadenceBlocked("approval_has_task_in_flight")
        return repository.put_state("linkedin_cadence_approval", approval_id,
                                    {**_clean_state(current), "status": "revoked", "revoked_by": actor})


def validate_delegated_plan(repository, authority: Mapping[str, Any], plan: Mapping[str, Any], clock) -> dict[str, Any]:
    """Recheck persisted D0 authority immediately before queue and dispatch."""
    if not isinstance(authority, Mapping) or not isinstance(authority.get("id"), str):
        raise ConnectSafelyError("delegated_authority_invalid")
    current = repository.get_state("linkedin_cadence_approval", authority["id"])
    if (not current or current.get("approved_by") != "ygor" or current.get("status") != "active"
            or current.get("review_hash") != authority.get("review_hash")
            or current.get("version") != authority.get("version")):
        raise ConnectSafelyError("delegated_authority_changed")
    if current.get("review_hash") != approval_payload_hash(current):
        raise ConnectSafelyError("delegated_authority_changed")
    if _iso(current.get("expires_at")).timestamp() <= clock():
        raise ConnectSafelyError("delegated_authority_expired")
    marker = authority.get("marker")
    steps = [step for step in current.get("steps", []) if step.get("marker") == marker]
    if len(steps) != 1 or steps[0].get("plan_hash") != plan_hash(plan):
        raise ConnectSafelyError("delegated_plan_not_approved")
    delegation = repository.get_state("delegation", "hermes")
    if (not delegation or delegation.get("enabled") is not True
            or "linkedin_cadence" not in delegation.get("scopes", [])):
        raise ConnectSafelyError("delegation_disabled_or_out_of_scope")
    if (repository.get_state("control", "global") or {}).get("paused") is True:
        raise ConnectSafelyError("global_pause")
    agent_control = repository.get_state("agent_control", "global") or {}
    if agent_control.get("enabled") is not True or agent_control.get("paused") is not False:
        raise ConnectSafelyError("agent_paused")
    return current


class HermesLinkedInCadence:
    def __init__(self, repository, task_port, connectsafely_app, *, clock=None, identity_verifier=None):
        self.repository = repository
        self.tasks = task_port
        self.app = connectsafely_app
        self.clock = clock or (lambda: datetime.now(timezone.utc).timestamp())
        self.identity_verifier = identity_verifier
        if self.app.repo is not self.repository:
            raise ValueError("cadence_and_connectsafely_must_share_repository")
        self.app.delegated_task_guard = self._dispatch_task_guard

    def _verify_identity(self, approval: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
        if self.identity_verifier is None:
            raise CadenceBlocked("contact_profile_identity_unverified")
        from .delegated_identity import compare_identity
        try:
            evidence = self.identity_verifier(approval["hubspot_contact_id"], dict(plan))
            if not isinstance(evidence, Mapping) or set(evidence) != {"contact", "profile"}:
                raise ValueError("invalid evidence")
            compared = compare_identity(approval["hubspot_contact_id"], evidence["contact"],
                                        evidence["profile"], dict(plan))
        except Exception:
            raise CadenceBlocked("contact_profile_identity_unverified") from None
        contact, profile = evidence["contact"], evidence["profile"]
        return {**compared,
                "contact": {key: contact[key] for key in ("id", "linkedin_url", "name", "company")},
                "profile": {key: profile[key] for key in ("account_id", "profile_urn", "linkedin_url", "name", "company")}}

    def _check_identity_binding(self, binding: Mapping[str, Any], identity: Mapping[str, Any]) -> None:
        if binding.get("identity_hash") != identity["observation_hash"]:
            raise CadenceBlocked("identity_observation_changed")
        if identity["matches"]:
            return
        confirmation = self.repository.get_state("linkedin_cadence_identity_confirmation", binding["preview_id"])
        if (not confirmation or confirmation.get("actor") != "ygor"
                or confirmation.get("preview_id") != binding["preview_id"]
                or confirmation.get("review_hash") != binding["review_hash"]
                or confirmation.get("observation_hash") != identity["observation_hash"]):
            raise CadenceBlocked("identity_human_confirmation_required")

    def _dispatch_task_guard(self, job: Mapping[str, Any]) -> bool:
        binding = self.repository.get_state("linkedin_cadence_preview_binding", str(job.get("preview_id", "")))
        if not binding or not isinstance(binding.get("task_id"), str):
            raise CadenceBlocked("task_preview_binding_changed")
        task = self.tasks.get(binding["task_id"])
        approval, step = self._match(task)
        identity = self._verify_identity(approval, step["plan"])
        self._check_identity_binding(binding, identity)
        authority = {"id": approval["id"], "version": approval["version"],
                     "review_hash": approval["review_hash"], "marker": step["marker"]}
        if (binding.get("task_hash") != payload_hash(dict(task))
                or binding.get("authority") != authority
                or job.get("delegated_authority") != authority):
            raise CadenceBlocked("task_preview_binding_changed")
        validate_delegated_plan(self.repository, authority, step["plan"], self.clock)
        return True

    def approve(self, data: Mapping[str, Any], actor: str) -> dict[str, Any]:
        if actor != "ygor":
            raise CadenceBlocked("human_authority_required")
        required = {"id", "hubspot_contact_id", "cadence_id", "expires_at", "steps"}
        if not isinstance(data, Mapping) or set(data) != required:
            raise CadenceBlocked("invalid_cadence_approval")
        if not all(isinstance(data.get(key), str) and data[key].strip()
                   for key in ("id", "hubspot_contact_id", "cadence_id", "expires_at")):
            raise CadenceBlocked("invalid_cadence_approval")
        if _iso(data["expires_at"]).timestamp() <= self.clock():
            raise CadenceBlocked("approval_expired")
        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list) or len(raw_steps) != len(STEP_CONTRACT):
            raise CadenceBlocked("invalid_cadence_steps")
        normalized = []
        seen = set()
        for raw in raw_steps:
            if not isinstance(raw, Mapping) or set(raw) != {"marker", "subject", "plan"}:
                raise CadenceBlocked("invalid_cadence_steps")
            marker = raw.get("marker")
            if marker not in STEP_CONTRACT or marker in seen:
                raise CadenceBlocked("invalid_cadence_steps")
            subject, action = STEP_CONTRACT[marker]
            plan = raw.get("plan")
            if (raw.get("subject") != subject or not isinstance(plan, Mapping)
                    or plan.get("action") != action or plan.get("channel") != "linkedin"
                    or plan.get("messaging_channel") != "linkedin_inbox"):
                raise CadenceBlocked("invalid_cadence_steps")
            # The canonical console performs the complete identity/content check.
            self.app._console(action)._plan(dict(plan))
            normalized.append({"marker": marker, "subject": subject,
                               "plan": dict(plan), "plan_hash": plan_hash(plan)})
            seen.add(marker)
        if seen != set(STEP_CONTRACT):
            raise CadenceBlocked("invalid_cadence_steps")
        identities = {(s["plan"].get("account_id"), s["plan"].get("account_public_id"),
                       s["plan"].get("recipient_public_id"), s["plan"].get("recipient_profile_urn"))
                      for s in normalized}
        if len(identities) != 1:
            raise CadenceBlocked("cadence_identity_mismatch")
        normalized.sort(key=lambda step: step["marker"])
        value = {
            "hubspot_contact_id": data["hubspot_contact_id"], "cadence_id": data["cadence_id"],
            "expires_at": data["expires_at"], "steps": normalized, "approved_by": actor,
            "status": "active",
        }
        value["review_hash"] = approval_payload_hash(value)
        existing = self.repository.get_state("linkedin_cadence_approval", data["id"])
        if existing:
            if _clean_state(existing) != value:
                raise RepositoryConflict("cadence approval id already has another payload")
            return existing
        return self.repository.append_state("linkedin_cadence_approval", data["id"], value)

    def _match(self, task: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(task, Mapping) or not isinstance(task.get("id"), str):
            raise CadenceBlocked("task_invalid")
        if task.get("status") != "NOT_STARTED":
            raise CadenceBlocked("task_not_open")
        if _iso(task.get("due_at")).timestamp() > self.clock():
            raise CadenceBlocked("task_not_due")
        contacts = task.get("contact_ids")
        if not isinstance(contacts, list) or len(contacts) != 1 or not isinstance(contacts[0], str):
            raise CadenceBlocked("task_contact_ambiguous")
        self._check_pending_contact(contacts[0], task["id"])
        if task.get("contact_status") not in ACTIVE_CADENCE_STATUSES:
            raise CadenceBlocked("cadence_status_blocked")
        marker = task.get("body")
        if marker not in STEP_CONTRACT or task.get("subject") != STEP_CONTRACT[marker][0]:
            raise CadenceBlocked("task_contract_mismatch")
        matches = []
        for approval in self.repository.list_state("linkedin_cadence_approval"):
            if (approval.get("status") == "active" and approval.get("hubspot_contact_id") == contacts[0]
                    and any(step.get("marker") == marker for step in approval.get("steps", []))):
                matches.append(approval)
        if len(matches) != 1:
            raise CadenceBlocked("task_approval_ambiguous_or_missing")
        approval = matches[0]
        binding = self.repository.get_state("linkedin_cadence_task_binding", task["id"])
        if (not binding or binding.get("actor") != "ygor"
                or binding.get("task_id") != task["id"]
                or binding.get("approval_id") != approval["id"]
                or binding.get("approval_version") != approval["version"]
                or binding.get("review_hash") != approval["review_hash"]
                or binding.get("marker") != marker or binding.get("contact_id") != contacts[0]
                or binding.get("cadence_id") != approval["cadence_id"]):
            raise CadenceBlocked("task_human_binding_missing_or_changed")
        if task.get("cadence_id") != approval.get("cadence_id"):
            raise CadenceBlocked("task_cadence_mismatch")
        if _iso(approval["expires_at"]).timestamp() <= self.clock():
            raise CadenceBlocked("approval_expired")
        step = next(step for step in approval["steps"] if step["marker"] == marker)
        return approval, step

    def _check_pending_contact(self, contact_id: str, task_id: str) -> None:
        for state in self.repository.list_state("linkedin_cadence_task"):
            contact = state.get("contact_id")
            if not contact and len((state.get("task_contract") or {}).get("contact_ids", [])) == 1:
                contact = state["task_contract"]["contact_ids"][0]
            if (state.get("task_id", state.get("id")) != task_id
                    and contact == contact_id
                    and state.get("status") in {"pending", "uncertain", "provider_confirmed"}):
                raise CadenceBlocked("contact_reconciliation_pending")

    def _complete_confirmed(self, state: Mapping[str, Any], *, replayed: bool) -> dict[str, Any]:
        task_id, receipt = str(state["task_id"]), str(state["receipt_ref"])
        self.tasks.complete(task_id, receipt, state["task_contract"])
        audit_id = payload_hash({"task": task_id, "receipt": receipt})
        with self.repository.transaction():
            current = self.repository.get_state("linkedin_cadence_task", task_id)
            if not current or current.get("status") not in {"provider_confirmed", "completed"} or current.get("receipt_ref") != receipt:
                raise CadenceBlocked("task_reconciliation_changed")
            completed = {**_clean_state(current), "status": "completed"}
            if current.get("status") != "completed":
                self.repository.put_state("linkedin_cadence_task", task_id, completed)
            if self.repository.get_state("linkedin_cadence_audit", audit_id) is None:
                self.repository.append_state("linkedin_cadence_audit", audit_id, completed)
        return {"task_id": task_id, "status": "completed", "receipt_ref": receipt,
                "actor": "hermes (delegado)", **({"replayed": True} if replayed else {})}

    def _record_alert(self, task_id: str, state: Mapping[str, Any]) -> None:
        value = {**_clean_state(state), "task_id": task_id, "actor": "hermes (delegado)"}
        self.repository.put_state("linkedin_cadence_alert", task_id, value)

    def _resume_job(self, prior: Mapping[str, Any], *, replayed: bool) -> dict[str, Any]:
        """Recover the persisted job without replaying a possible provider effect."""
        task_id = str(prior["task_id"])
        job = self.repository.get_state("connectsafely_job", str(prior.get("job_id", "")))
        if not job:
            self._record_alert(task_id, {**_clean_state(prior), "error_code": "job_missing"})
            return {"task_id": task_id, "status": "uncertain", "error_code": "job_missing"}
        if job.get("state") == "queued" and prior.get("status") == "uncertain":
            return {"task_id": task_id, "status": "uncertain", "replayed": replayed,
                    "error_code": "reconciliation_required"}
        if job.get("state") in {"queued", "running"}:
            # The application's claim/lease guard is the sole dispatch authority.
            # A running, expired claim becomes uncertain, never queued again.
            self.app.process_one(job_id=job["id"])
            job = self.repository.get_state("connectsafely_job", job["id"])
        if job and job.get("state") in {"queued", "running"}:
            return {"task_id": task_id, "status": "pending", "replayed": replayed}
        # Serialize against job reconciliation and concurrent task completion.
        # Re-read both while holding the write lock, so stale observations cannot
        # regress a completed task to uncertain or replace its receipt.
        with self.repository.transaction():
            current = self.repository.get_state("linkedin_cadence_task", task_id)
            job = self.repository.get_state("connectsafely_job", str(prior.get("job_id", "")))
            if not current or current.get("job_id") != prior.get("job_id"):
                raise CadenceBlocked("task_reconciliation_changed")
            if current.get("status") == "completed":
                return {"task_id": task_id, "status": "completed", "replayed": True,
                        "receipt_ref": current.get("receipt_ref")}
            result = (job or {}).get("result", {})
            sent = bool(job and job.get("state") == "done" and result.get("status") == "sent" and result.get("receipt_ref"))
            if current.get("status") == "provider_confirmed":
                confirmed = _clean_state(current)
                sent = True
            elif sent:
                confirmed = {**_clean_state(current), "status": "provider_confirmed", "receipt_ref": result["receipt_ref"]}
                self.repository.put_state("linkedin_cadence_task", task_id, confirmed)
            else:
                uncertain = bool(job and job.get("state") == "needs_reconciliation")
                status = "uncertain" if uncertain else "blocked"
                state = {**_clean_state(current), "status": status,
                         "error_code": result.get("error_code") or (job or {}).get("error_code") or "connectsafely_not_confirmed"}
                self.repository.put_state("linkedin_cadence_task", task_id, state)
        if sent:
            try:
                return self._complete_confirmed(confirmed, replayed=replayed)
            except Exception:
                return {"task_id": task_id, "status": "provider_confirmed",
                        "receipt_ref": confirmed["receipt_ref"], "hubspot_completion": "pending"}
        self._record_alert(task_id, state)
        if uncertain and not state.get("crm_uncertain_confirmed") and hasattr(self.tasks, "mark_uncertain"):
            try:
                self.tasks.mark_uncertain(state["contact_id"], state["expected_contact_status"])
            except Exception:
                current = self.repository.get_state("linkedin_cadence_task", task_id)
                if current and current.get("status") == "completed":
                    return {"task_id": task_id, "status": "completed", "replayed": True,
                            "receipt_ref": current.get("receipt_ref")}
                self._record_alert(task_id, {**state, "error_code": "hubspot_uncertain_status_pending"})
                return {"task_id": task_id, "status": status, "replayed": replayed,
                        "error_code": "hubspot_uncertain_status_pending", "hubspot_update": "pending"}
            with self.repository.transaction():
                current = self.repository.get_state("linkedin_cadence_task", task_id)
                if current and current.get("status") == "uncertain" and current.get("job_id") == state.get("job_id"):
                    state = {**_clean_state(current), "crm_uncertain_confirmed": True}
                    self.repository.put_state("linkedin_cadence_task", task_id, state)
        return {"task_id": task_id, "status": status, "replayed": replayed,
                "error_code": state["error_code"]}

    def _recover_legacy_job(self, task_id: str) -> dict[str, Any] | None:
        """Recover a pre-upgrade orphan only with its original immutable bindings."""
        bindings = [item for item in self.repository.list_state("linkedin_cadence_preview_binding")
                    if item.get("task_id") == task_id]
        candidates = [(binding, job) for binding in bindings
                      for job in self.repository.list_state("connectsafely_job")
                      if job.get("preview_id") == binding.get("preview_id")]
        if not candidates:
            return None
        if len(candidates) != 1:
            raise CadenceBlocked("legacy_task_job_ambiguous")
        binding, job = candidates[0]
        authority = binding.get("authority") or {}
        preview = self.repository.get_state("connectsafely_preview", binding["preview_id"])
        queue = self.repository.get_state("connectsafely_queue_approval", job["id"])
        approval = self.repository.get_state("linkedin_cadence_approval", str(authority.get("id", "")))
        task = self.tasks.get(task_id)
        steps = [step for step in (approval or {}).get("steps", []) if step.get("marker") == authority.get("marker")]
        review = job.get("review") or {}
        if (not preview or not queue or not approval or len(steps) != 1
                or job.get("actor") != "hermes (delegado)" or job.get("delegated_authority") != authority
                or queue.get("actor") != "ygor" or queue.get("executor") != "hermes (delegado)"
                or queue.get("delegated_authority") != authority or queue.get("review") != review
                or job.get("campaign_confirmation_hash") != payload_hash(queue.get("campaign_confirmation"))
                or approval.get("version") != authority.get("version")
                or approval.get("review_hash") != authority.get("review_hash")
                or review.get("preview_id") != binding.get("preview_id")
                or review.get("expected_version") != binding.get("preview_version")
                or review.get("review_hash") != binding.get("review_hash")
                or steps[0].get("plan_hash") != plan_hash(preview.get("plan", {}))
                or task.get("body") != authority.get("marker")
                or task.get("contact_ids") != [approval.get("hubspot_contact_id")]
                or payload_hash(dict(task)) != binding.get("task_hash")):
            raise CadenceBlocked("legacy_task_job_unverified")
        state = {"status": "pending", "task_id": task_id, "approval_id": approval["id"],
                 "marker": steps[0]["marker"], "job_id": job["id"], "actor": "hermes (delegado)",
                 "task_contract": _task_contract(task, steps[0], self.clock),
                 "contact_id": task["contact_ids"][0], "expected_contact_status": task.get("contact_status"),
                 "crm_uncertain_confirmed": False}
        with self.repository.transaction():
            current = self.repository.get_state("linkedin_cadence_task", task_id)
            if current:
                return current
            self.repository.append_state("linkedin_cadence_task", task_id, state)
        return state

    def run_task(self, task_id: str, *, preview_id: str | None = None,
                 campaign_approval_id: str | None = None) -> dict[str, Any]:
        prior = self.repository.get_state("linkedin_cadence_task", task_id)
        if prior and prior.get("status") == "completed":
            receipt = prior.get("receipt_ref")
            if isinstance(receipt, str) and receipt:
                audit_id = payload_hash({"task": task_id, "receipt": receipt})
                with self.repository.transaction():
                    if self.repository.get_state("linkedin_cadence_audit", audit_id) is None:
                        self.repository.append_state("linkedin_cadence_audit", audit_id, _clean_state(prior))
            return {"task_id": task_id, "status": "completed", "replayed": True,
                    "receipt_ref": receipt}
        if prior and prior.get("status") == "uncertain":
            if prior.get("marker") == "cad_li_0":
                approval = self.repository.get_state("linkedin_cadence_approval", str(prior.get("approval_id", "")))
                step = next((item for item in (approval or {}).get("steps", [])
                             if item.get("marker") == prior.get("marker")), None)
                if approval and step:
                    authority = {"id": approval["id"], "version": approval["version"],
                                 "review_hash": approval["review_hash"], "marker": step["marker"]}
                    job = self.repository.get_state("connectsafely_job", str(prior.get("job_id", "")))
                    if job:
                        try:
                            self.app.reconcile_delegated({"preview_id": job["preview_id"]}, authority)
                        except Exception:
                            pass
            return self._resume_job(prior, replayed=True)
        if prior and prior.get("status") == "provider_confirmed":
            return self._complete_confirmed(prior, replayed=True)
        if prior and prior.get("status") == "pending":
            return self._resume_job(prior, replayed=True)
        if prior and prior.get("status") == "blocked":
            return {"task_id": task_id, "status": "blocked", "replayed": True,
                    "error_code": prior.get("error_code", "connectsafely_not_confirmed")}
        if not prior:
            legacy = self._recover_legacy_job(task_id)
            if legacy:
                return self._resume_job(legacy, replayed=True)
        task = self.tasks.get(task_id)
        approval, step = self._match(task)
        identity = self._verify_identity(approval, step["plan"])
        authority = {"id": approval["id"], "version": approval["version"],
                     "review_hash": approval["review_hash"], "marker": step["marker"]}
        validate_delegated_plan(self.repository, authority, step["plan"], self.clock)
        relationship = self.app.provider.relationship(step["plan"])
        required = "NOT_CONNECTED" if step["plan"]["action"] == "invite" else "CONNECTED"
        if relationship.get("relationship_status") != required:
            raise CadenceBlocked("relationship_gate_blocked")
        # Re-read the CRM authority immediately before queueing so a reply or
        # sequence/status change observed during relationship lookup wins.
        latest = self.tasks.get(task_id)
        latest_approval, latest_step = self._match(latest)
        if (latest_approval["id"] != approval["id"] or latest_approval["version"] != approval["version"]
                or latest_step["plan_hash"] != step["plan_hash"]):
            raise CadenceBlocked("task_authority_changed")
        task = latest
        if preview_id is None:
            if campaign_approval_id is not None:
                raise CadenceBlocked("preview_required")
            preview = self.app.prepare_delegated(step["plan"], authority)
            binding = {"task_id": task_id, "task_hash": payload_hash(dict(task)),
                       "authority": authority, "preview_id": preview["id"],
                       "review_hash": preview["review_hash"], "preview_version": preview["version"],
                       "identity_hash": identity["observation_hash"], "identity": identity}
            self.repository.append_state("linkedin_cadence_preview_binding", preview["id"], binding)
            return {"task_id": task_id, "status": "human_confirmation_required",
                    "preview_id": preview["id"], "review_hash": preview["review_hash"],
                    "expected_version": preview["version"], "identity": identity}
        if not isinstance(campaign_approval_id, str) or not campaign_approval_id:
            raise CadenceBlocked("campaign_confirmation_required")
        binding = self.repository.get_state("linkedin_cadence_preview_binding", preview_id)
        if (not binding or binding.get("task_id") != task_id
                or binding.get("task_hash") != payload_hash(dict(task))
                or binding.get("authority") != authority):
            raise CadenceBlocked("task_preview_binding_changed")
        self._check_identity_binding(binding, identity)
        # Queue and task context commit together. A process death cannot leave an
        # actionable queued job without its task/receipt reconciliation context.
        with self.repository.transaction():
            prior = self.repository.get_state("linkedin_cadence_task", task_id)
            if prior:
                raise CadenceBlocked("task_already_has_job")
            self._check_pending_contact(task["contact_ids"][0], task_id)
            queued = self.app.enqueue_delegated({
                "preview_id": preview_id, "expected_version": binding["preview_version"],
                "review_hash": binding["review_hash"], "campaign_approval_id": campaign_approval_id,
            }, authority)
            state = {"status": "pending", "task_id": task_id, "approval_id": approval["id"],
                     "marker": step["marker"], "job_id": queued["job_id"],
                     "actor": "hermes (delegado)", "task_contract": _task_contract(task, step, self.clock),
                     "contact_id": task["contact_ids"][0],
                     "expected_contact_status": task.get("contact_status"), "crm_uncertain_confirmed": False}
            self.repository.append_state("linkedin_cadence_task", task_id, state)
        return self._resume_job(state, replayed=False)
