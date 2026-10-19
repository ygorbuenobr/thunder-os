"""Bounded CLI runtime for the deterministic Hermes LinkedIn cadence executor."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

from .connectsafely_runtime import ExistingRepository, build as build_connectsafely, load_config as load_cs_config
from .hermes_linkedin_cadence import HermesLinkedInCadence
from .hubspot_tasks import HubSpotTasks


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
               "hubspot", "connectsafely_config", "connectsafely_identity", "synthetic"}
    if set(cfg) - allowed or type(cfg.get("execution_enabled")) is not bool:
        raise ValueError("runtime_config_invalid")
    _absolute(cfg.get("repository"))
    if not isinstance(cfg.get("source_revision"), str) or not cfg["source_revision"]:
        raise ValueError("runtime_config_invalid")
    if type(cfg.get("poll_minutes")) is not int or not 5 <= cfg["poll_minutes"] <= 60:
        raise ValueError("runtime_config_invalid")
    if type(cfg.get("max_tasks_per_run")) is not int or not 1 <= cfg["max_tasks_per_run"] <= 30:
        raise ValueError("runtime_config_invalid")
    if type(cfg.get("daily_cap")) is not int or not 1 <= cfg["daily_cap"] <= 30:
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
    def complete(self, task_id, receipt):
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
        hub=cfg["hubspot"];tasks=HubSpotTasks(hub["portal_id"],hub["owner_id"],hub["token_env"])
        return HermesLinkedInCadence(repository,tasks,app),tasks,datetime.now(timezone.utc).timestamp()
    raise ValueError("external_effects_disabled")


def run_once(cfg):
    runner,tasks,now=build(cfg)
    if not _business_window(cfg,now):
        return {"source_revision":cfg["source_revision"],"mode":cfg["mode"],"status":"outside_business_hours","processed":[]}
    outcomes=[]
    for task_id in tasks.due(limit=cfg["max_tasks_per_run"]):
        try: outcomes.append(runner.run_task(task_id))
        except Exception as exc: outcomes.append({"task_id":task_id,"status":"blocked","error_code":_error_code(exc)})
    return {"source_revision":cfg["source_revision"],"config_sha256":cfg["_digest"],
            "mode":cfg["mode"],"status":"ok","processed":outcomes}


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
