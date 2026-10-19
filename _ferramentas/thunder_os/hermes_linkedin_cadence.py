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
        "sent_at": datetime.fromtimestamp(clock(), timezone.utc).isoformat(),
    }


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
    return current


class HermesLinkedInCadence:
    def __init__(self, repository, task_port, connectsafely_app, *, clock=None):
        self.repository = repository
        self.tasks = task_port
        self.app = connectsafely_app
        self.clock = clock or (lambda: datetime.now(timezone.utc).timestamp())
        if self.app.repo is not self.repository:
            raise ValueError("cadence_and_connectsafely_must_share_repository")

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
        if task.get("cadence_id") != approval.get("cadence_id"):
            raise CadenceBlocked("task_cadence_mismatch")
        if _iso(approval["expires_at"]).timestamp() <= self.clock():
            raise CadenceBlocked("approval_expired")
        step = next(step for step in approval["steps"] if step["marker"] == marker)
        return approval, step

    def _complete_confirmed(self, state: Mapping[str, Any], *, replayed: bool) -> dict[str, Any]:
        task_id, receipt = str(state["task_id"]), str(state["receipt_ref"])
        self.tasks.complete(task_id, receipt, state["task_contract"])
        completed = {**_clean_state(state), "status": "completed"}
        self.repository.put_state("linkedin_cadence_task", task_id, completed)
        audit_id = payload_hash({"task": task_id, "receipt": receipt})
        if self.repository.get_state("linkedin_cadence_audit", audit_id) is None:
            self.repository.append_state("linkedin_cadence_audit", audit_id, completed)
        return {"task_id": task_id, "status": "completed", "receipt_ref": receipt,
                "actor": "hermes (delegado)", **({"replayed": True} if replayed else {})}

    def _record_alert(self, task_id: str, state: Mapping[str, Any]) -> None:
        value = {**_clean_state(state), "task_id": task_id, "actor": "hermes (delegado)"}
        self.repository.put_state("linkedin_cadence_alert", task_id, value)

    def run_task(self, task_id: str) -> dict[str, Any]:
        prior = self.repository.get_state("linkedin_cadence_task", task_id)
        if prior and prior.get("status") == "completed":
            return {"task_id": task_id, "status": "completed", "replayed": True,
                    "receipt_ref": prior.get("receipt_ref")}
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
            job = self.repository.get_state("connectsafely_job", str(prior.get("job_id", "")))
            result = job.get("result", {}) if job else {}
            if job and job.get("state") == "done" and result.get("status") == "sent" and result.get("receipt_ref"):
                confirmed = {**_clean_state(prior), "status": "provider_confirmed",
                             "receipt_ref": result["receipt_ref"]}
                self.repository.put_state("linkedin_cadence_task", task_id, confirmed)
                try:
                    return self._complete_confirmed(confirmed, replayed=True)
                except Exception:
                    return {"task_id": task_id, "status": "provider_confirmed",
                            "receipt_ref": result["receipt_ref"], "hubspot_completion": "pending"}
            return {"task_id": task_id, "status": "uncertain", "replayed": True,
                    "error_code": "reconciliation_required"}
        if prior and prior.get("status") == "provider_confirmed":
            return self._complete_confirmed(prior, replayed=True)
        task = self.tasks.get(task_id)
        approval, step = self._match(task)
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
        preview = self.app.prepare_delegated(step["plan"], authority)
        queued = self.app.enqueue_delegated({
            "preview_id": preview["id"], "expected_version": preview["version"],
            "review_hash": preview["review_hash"],
        }, authority)
        self.app.process_one(job_id=queued["job_id"])
        job = self.repository.get_state("connectsafely_job", queued["job_id"])
        result = job.get("result", {}) if job else {}
        if job and job.get("state") == "done" and result.get("status") == "sent" and result.get("receipt_ref"):
            receipt = result["receipt_ref"]
            state = {"status": "provider_confirmed", "task_id": task_id,
                     "approval_id": approval["id"], "marker": step["marker"],
                     "job_id": queued["job_id"], "receipt_ref": receipt,
                     "actor": "hermes (delegado)",
                     "task_contract": _task_contract(task, step, self.clock)}
            self.repository.put_state("linkedin_cadence_task", task_id, state)
            try:
                return self._complete_confirmed(state, replayed=False)
            except Exception:
                return {"task_id": task_id, "status": "provider_confirmed",
                        "receipt_ref": receipt, "hubspot_completion": "pending"}
        uncertain = bool(job and job.get("state") == "needs_reconciliation")
        status = "uncertain" if uncertain else "blocked"
        error = result.get("error_code", "connectsafely_not_confirmed")
        self.repository.put_state("linkedin_cadence_task", task_id, {
            "status": status, "task_id": task_id, "approval_id": approval["id"],
            "marker": step["marker"], "job_id": queued["job_id"], "error_code": error,
            "actor": "hermes (delegado)",
            "task_contract": _task_contract(task, step, self.clock),
        })
        self._record_alert(task_id, {
            "status": status, "approval_id": approval["id"], "marker": step["marker"],
            "job_id": queued["job_id"], "error_code": error,
        })
        if uncertain and hasattr(self.tasks, "mark_uncertain"):
            self.tasks.mark_uncertain(task["contact_ids"][0], task.get("contact_status"))
        return {"task_id": task_id, "status": status, "error_code": error}
