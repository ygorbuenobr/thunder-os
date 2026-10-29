"""Bounded CLI runtime for the deterministic Hermes LinkedIn cadence executor."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time

from .connectsafely_runtime import ExistingRepository, build as build_connectsafely, load_config as load_cs_config
from .hermes_linkedin_cadence import HermesLinkedInCadence
from .hubspot_tasks import HubSpotTasks


def task_cadence_binding(repository, task_id, *, clock=time.time):
    """Resolve only a human-bound task against the exact current D0 authority."""
    from .hermes_linkedin_cadence import approval_payload_hash
    binding = repository.get_state('linkedin_cadence_task_binding', task_id)
    if not binding or binding.get('task_id') != task_id or binding.get('actor') != 'ygor':
        return None
    approval = repository.get_state('linkedin_cadence_approval', binding.get('approval_id', ''))
    if (not approval or approval.get('approved_by') != 'ygor' or approval.get('status') != 'active'
            or approval.get('version') != binding.get('approval_version')
            or approval.get('review_hash') != binding.get('review_hash')
            or approval.get('review_hash') != approval_payload_hash(approval)
            or approval.get('hubspot_contact_id') != binding.get('contact_id')
            or approval.get('cadence_id') != binding.get('cadence_id')
            or len([step for step in approval.get('steps', []) if step.get('marker') == binding.get('marker')]) != 1):
        return None
    try:
        expiry = datetime.fromisoformat(approval['expires_at'].replace('Z', '+00:00'))
        if expiry.tzinfo is None or expiry.timestamp() <= clock(): return None
    except (ValueError, TypeError, KeyError):
        return None
    return binding['cadence_id']


def _absolute(value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError("absolute_explicit_path_required")
    return Path(value).resolve()


def load_config(path):
    target = _absolute(str(path)); raw = target.read_bytes(); cfg = json.loads(raw)
    if not isinstance(cfg, dict) or cfg.get("version") != 1 or cfg.get("mode") not in {"disabled", "synthetic", "live"}:
        raise ValueError("runtime_config_invalid")
    allowed = {"version", "mode", "execution_enabled", "repository", "source_revision", "poll_minutes",
               "max_tasks_per_run", "timezone", "business_hours", "daily_cap", "kill_switch",
               "action_spacing_seconds", "source_revision_file", "hubspot", "connectsafely_config",
               "connectsafely_identity", "synthetic"}
    if set(cfg) - allowed or type(cfg.get("execution_enabled")) is not bool:
        raise ValueError("runtime_config_invalid")
    _absolute(cfg.get("repository")); _absolute(cfg.get("source_revision_file"))
    if not isinstance(cfg.get("source_revision"), str) or not cfg["source_revision"]:
        raise ValueError("runtime_config_invalid")
    if type(cfg.get("poll_minutes")) is not int or not 5 <= cfg["poll_minutes"] <= 60:
        raise ValueError("runtime_config_invalid")
    if type(cfg.get("max_tasks_per_run")) is not int or not 1 <= cfg["max_tasks_per_run"] <= 30:
        raise ValueError("runtime_config_invalid")
    if type(cfg.get("daily_cap")) is not int or not 1 <= cfg["daily_cap"] <= 30:
        raise ValueError("runtime_config_invalid")
    spacing = cfg.get("action_spacing_seconds")
    if (not isinstance(spacing, dict) or set(spacing) != {"min", "max"}
            or type(spacing.get("min")) is not int or type(spacing.get("max")) is not int
            or spacing["min"] < 30 or spacing["max"] > 90 or spacing["min"] > spacing["max"]):
        raise ValueError("runtime_config_invalid")
    hours = cfg.get("business_hours")
    if (cfg.get("timezone") != "America/Sao_Paulo" or not isinstance(hours, dict)
            or set(hours) != {"start", "end", "weekdays"} or not isinstance(hours["weekdays"], list)
            or not all(isinstance(value,str) and len(value)==5 and value[2]==":" and value.replace(":","").isdigit()
                       for value in (hours.get("start"),hours.get("end")))
            or not all(type(day) is int and 0<=day<=6 for day in hours["weekdays"])
            or len(set(hours["weekdays"]))!=len(hours["weekdays"])
            or hours["start"]>=hours["end"]
            or cfg.get("kill_switch")!="thunder_state:delegation/hermes.enabled"):
        raise ValueError("runtime_config_invalid")
    def reject_secrets(value):
        if isinstance(value,dict):
            for key,item in value.items():
                lowered=key.lower()
                if lowered in {"token","secret","password","api_key","access_token"}:
                    raise ValueError("embedded_secret_forbidden")
                reject_secrets(item)
        elif isinstance(value,list):
            for item in value:reject_secrets(item)
    reject_secrets(cfg)
    if cfg["mode"] == "disabled" and cfg["execution_enabled"]:
        raise ValueError("disabled_mode_cannot_execute")
    if cfg["mode"] == "live":
        if not isinstance(cfg.get("hubspot"), dict) or set(cfg["hubspot"]) != {"portal_id", "owner_id", "token_env"}:
            raise ValueError("runtime_config_invalid")
        identity=cfg.get("connectsafely_identity")
        if (not isinstance(identity,dict) or set(identity)!={"account_id","public_id"}
                or not all(isinstance(value,str) and value for value in identity.values())):
            raise ValueError("runtime_config_invalid")
        _absolute(cfg.get("connectsafely_config"))
    if cfg["mode"] == "synthetic" and not isinstance(cfg.get("synthetic"), dict):
        raise ValueError("runtime_config_invalid")
    cfg["_digest"] = hashlib.sha256(raw).hexdigest()
    return cfg


class SyntheticTasks:
    def __init__(self, fixture):
        self.task = dict(fixture["task"]); self.completed = []
    def due(self, limit=20): return [self.task["id"]][:limit]
    def get(self, task_id): return dict(self.task)
    def complete(self, task_id, receipt, expected_task):
        if any(self.task[key]!=expected_task[key] for key in ("subject","body","contact_ids")):
            raise ValueError("synthetic_task_changed_after_dispatch")
        self.completed.append([task_id, receipt]); self.task["status"] = "COMPLETED"


class SyntheticProvider:
    def __init__(self, fixture): self.fixture = fixture
    def relationship(self, _plan): return {"relationship_status": self.fixture["relationship_status"]}


class SyntheticCadenceApp:
    """No socket, credential loader or dynamic code path exists in synthetic mode."""
    def __init__(self, repo, fixture): self.repo=repo; self.provider=SyntheticProvider(fixture); self.fixture=fixture; self.jobs={}
    class _Console:
        @staticmethod
        def _plan(plan): return dict(plan)
    def _console(self, _action): return self._Console()
    def prepare_delegated(self, plan, authority):
        from .hermes_linkedin_cadence import validate_delegated_plan
        validate_delegated_plan(self.repo,authority,plan,lambda:self.fixture["clock"])
        ident="synthetic-preview";review=hashlib.sha256(json.dumps(plan,sort_keys=True).encode()).hexdigest()
        return {"id":ident,"version":1,"review_hash":review}
    def enqueue_delegated(self, body, authority):
        day=datetime.fromtimestamp(self.fixture["clock"],timezone.utc).date().isoformat()
        if self.repo.get_state("connectsafely_job","synthetic-job") is None:
            self.repo.reserve_delegated_action("hermes","linkedin_cadence",day)
            self.repo.append_state("connectsafely_job","synthetic-job",{"state":"queued","authority":dict(authority)})
        return {"job_id":"synthetic-job","state":"queued"}
    def process_one(self,job_id=None):
        result=dict(self.fixture["provider_result"])
        state="done" if result.get("status")=="sent" else "needs_reconciliation" if result.get("status")=="uncertain" else "blocked"
        self.repo.put_state("connectsafely_job",job_id,{"state":state,"result":result});return True


def _business_window(cfg, now):
    # Fixed HH:MM lexical comparison is valid after strict shape validation.
    from zoneinfo import ZoneInfo
    local=datetime.fromtimestamp(now,ZoneInfo(cfg["timezone"]));hours=cfg["business_hours"]
    return local.weekday() in hours["weekdays"] and hours["start"] <= local.strftime("%H:%M") < hours["end"]


def build(cfg):
    try: installed_revision=_absolute(cfg["source_revision_file"]).read_text().strip()
    except OSError as exc: raise ValueError("source_revision_unavailable") from exc
    if installed_revision != cfg["source_revision"]:
        raise ValueError("source_revision_mismatch")
    repository=ExistingRepository(_absolute(cfg["repository"])); repository.migrate()
    delegation=repository.get_state("delegation","hermes")
    if (not delegation or delegation.get("daily_limit")!=cfg["daily_cap"]
            or cfg["kill_switch"]!="thunder_state:delegation/hermes.enabled"):
        raise ValueError("delegation_configuration_mismatch")
    if cfg["mode"] == "synthetic":
        fixture=cfg["synthetic"]; tasks=SyntheticTasks(fixture); app=SyntheticCadenceApp(repository,fixture)
        return HermesLinkedInCadence(repository,tasks,app,clock=lambda:fixture["clock"]),tasks,fixture["clock"]
    if cfg["mode"] == "live":
        if not cfg["execution_enabled"] or os.environ.get("THUNDER_EXTERNAL_EFFECTS_ENABLED","").lower()!="true":
            raise ValueError("external_effects_disabled")
        cs_cfg=load_cs_config(_absolute(cfg["connectsafely_config"]));composition=build_connectsafely(cs_cfg,repository=repository)
        app=composition.application
        identity=cfg["connectsafely_identity"]
        if (app.provider.account_id != cs_cfg["live"]["account_id"] or app.provider.expected_public_id != cs_cfg["live"]["public_id"]
                or identity!={"account_id":app.provider.account_id,"public_id":app.provider.expected_public_id}):
            raise ValueError("connectsafely_identity_mismatch")
        hub=cfg["hubspot"];tasks=HubSpotTasks(hub["portal_id"],hub["owner_id"],hub["token_env"],
            cadence_for_task=lambda task_id: task_cadence_binding(repository, task_id))
        readers = getattr(app, 'canary_readers', None)
        return HermesLinkedInCadence(repository,tasks,app,
            identity_verifier=readers.identity if readers is not None else None),tasks,datetime.now(timezone.utc).timestamp()
    raise ValueError("external_effects_disabled")


def run_once(cfg):
    runner,tasks,now=build(cfg)
    if not _business_window(cfg,now):
        return {"source_revision":cfg["source_revision"],"mode":cfg["mode"],"status":"outside_business_hours","processed":[]}
    outcomes=[]
    pending = [state["id"] for state in runner.repository.list_state("linkedin_cadence_task")
               if state.get("status") in {"pending", "provider_confirmed", "uncertain"}]
    scheduler = runner.repository.get_state("linkedin_cadence_poll_scheduler", "global") or {}
    maximum = cfg["max_tasks_per_run"]
    # Alternate when the budget is one; otherwise reserve capacity for both
    # classes and rotate pending IDs across runs. Stuck reconciliation must not
    # starve unrelated due tasks, nor should a busy due queue starve recovery.
    if pending and maximum == 1:
        pending_slots = 0 if scheduler.get("next_single") == "due" else 1
    elif pending:
        pending_slots = min(len(pending), (maximum + 1) // 2)
    else:
        pending_slots = 0
    cursor = int(scheduler.get("pending_cursor", 0)) % len(pending) if pending else 0
    chosen_pending = [pending[(cursor + index) % len(pending)] for index in range(pending_slots)]
    due_slots = maximum - len(chosen_pending)
    due_error = None
    due = []
    due_after = scheduler.get("due_after")
    if due_slots:
        try:
            if callable(getattr(type(tasks), "due_page", None)):
                # Advancing a cursor past a page larger than the available due
                # capacity would silently skip some of that page indefinitely.
                page = tasks.due_page(limit=due_slots, after=due_after)
                due = page["ids"]
                due_after = page["next_after"]
            else:
                due = tasks.due(limit=maximum)
        except Exception as exc:
            due_error = _error_code(exc)
    due = [task_id for task_id in dict.fromkeys(due) if task_id not in pending]
    due_cursor = int(scheduler.get("due_cursor", 0)) % len(due) if due else 0
    chosen_due = [due[(due_cursor + index) % len(due)] for index in range(min(due_slots, len(due)))]
    # If the due queue is empty, spend the remaining budget on more pending IDs.
    remaining = maximum - len(chosen_pending) - len(chosen_due)
    if remaining:
        chosen_pending += [pending[(cursor + index) % len(pending)]
                           for index in range(pending_slots, min(len(pending), pending_slots + remaining))]
    selected = chosen_pending + chosen_due
    runner.repository.put_state("linkedin_cadence_poll_scheduler", "global", {
        "next_single": "due" if maximum == 1 and chosen_pending else "pending",
        "pending_cursor": cursor + len(chosen_pending), "due_cursor": due_cursor + len(chosen_due),
        "due_after": due_after})
    for index,task_id in enumerate(selected):
        try:
            confirmation = runner.repository.get_state("linkedin_cadence_human_confirmation", task_id)
            if confirmation:
                outcomes.append(runner.run_task(task_id, preview_id=confirmation["preview_id"],
                                                campaign_approval_id=confirmation["campaign_approval_id"]))
            else:
                outcomes.append(runner.run_task(task_id))
        except Exception as exc:
            code=_error_code(exc)
            runner._record_alert(task_id,{"status":"blocked","error_code":code})
            outcomes.append({"task_id":task_id,"status":"blocked","error_code":code})
        if index + 1 < len(selected):
            spacing=cfg["action_spacing_seconds"]
            time.sleep(spacing["min"] + secrets.randbelow(spacing["max"] - spacing["min"] + 1))
    summary = {"source_revision":cfg["source_revision"],"config_sha256":cfg["_digest"],
               "mode":cfg["mode"],"status":"ok","processed":outcomes}
    if due_error:
        summary.update(status="partial", due_error=due_error)
    return summary


def _error_code(exc):
    value=str(exc)
    return value if re.fullmatch(r"[a-z0-9_]{1,100}",value) else "execution_blocked"


def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument("--config",required=True);parser.add_argument("--once",action="store_true")
    args=parser.parse_args(argv)
    try:
        if not args.once: raise ValueError("only_bounded_once_mode_supported")
        print(json.dumps(run_once(load_config(args.config)),ensure_ascii=False,sort_keys=True));return 0
    except Exception as exc:
        print(json.dumps({"status":"blocked","error_code":_error_code(exc)},sort_keys=True));return 2


if __name__ == "__main__": raise SystemExit(main())
