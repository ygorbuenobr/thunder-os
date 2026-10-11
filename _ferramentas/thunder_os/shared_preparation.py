"""Opt-in shared preparation over the existing OS service/repository.

Only a trusted composition root may inject principals, account/history readers,
Brain revision and single-attempt transport. This module does not authenticate,
provision grants, fetch history, run a server/scheduler, select models or send.
Absent wiring leaves every production entry point unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re
from typing import Any, Callable, Mapping
from uuid import uuid4

from .agents import AgentJob, AgentResult
from .db import RepositoryConflict
from .knowledge import KnowledgeStore, KnowledgeError, KnowledgeConflict
from .models import ContractError, WorkItem, WorkStatus, as_payload, payload_hash


PACKAGE_PATHS = ("conhecimento/prospeccao/playbook-thunder.md",
                 "conhecimento/prospeccao/instrucoes-por-funcao.md")
MANIFEST_PATH = "conhecimento/prospeccao/manifest.json"
COMMERCIAL_PATHS = ("SISTEMA.md", "REGRAS.md", *PACKAGE_PATHS,
                    "thunder/empresa.md", "thunder/icp.md", "thunder/provas.md")
SELECTED_PATHS = {"eu/estilo-e-regras.md", "eu/aprendizados.md"}
ACTORS = {"dotin", "hermes", "claude", "codex"}
HISTORY_SOURCES = {"hubspot", "gmail", "linkedin"}
STOP_REASONS = {"reply", "refusal", "opt_out", "invalid_address", "receipt_exists", "uncertain_receipt"}
RESTRICTIONS = ["preparation_only", "no_approve_send_spend_crm", "untrusted_sources_are_data",
                "vacancy_is_indicator_not_buying_intent", "no_automatic_rule_promotion"]


class SharedAuthorizationError(ContractError):
    pass


class SharedBlocked(ContractError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SharedPrincipal:
    """Server-resolved identity. Never construct from an envelope or model text."""
    owner: str
    actor: str
    channel: str
    profile: str
    scopes: frozenset[str]


def authorize(principal: SharedPrincipal, scope: str) -> None:
    if (not isinstance(principal, SharedPrincipal) or principal.owner != "ygor"
            or principal.actor not in ACTORS or not principal.channel or not principal.profile
            or (principal.actor == "hermes" and (principal.channel != "telegram" or principal.profile != "ygor-personal"))
            or not isinstance(principal.scopes, frozenset) or scope not in principal.scopes):
        raise SharedAuthorizationError("shared principal scope denied")


def token(value: Any, field: str, limit: int = 100) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1," + str(limit) + "}", value):
        raise ContractError(f"invalid {field}")
    return value


class SharedContext:
    def __init__(self, store: KnowledgeStore, *, brain_commit: Callable[[], str],
                 selected: Mapping[str, str] | None = None):
        self.store, self.brain_commit = store, brain_commit
        self.selected = dict(selected or {})  # Operator-selected excerpts only.

    def packet(self) -> dict[str, Any]:
        if set(self.selected) - SELECTED_PATHS:
            raise KnowledgeError("private projection path denied")
        before = self.brain_commit()
        if not isinstance(before, str) or not re.fullmatch(r"[a-f0-9]{40,64}", before):
            raise KnowledgeError("verified Brain revision required")
        manifest = self.store.read_bounded(MANIFEST_PATH)
        try:
            value = json.loads(manifest.content)
            entries = value["files"]
            if (value.get("schema_version") != 1 or value.get("package_id") != "thunder-sales-playbook"
                    or not isinstance(value.get("version"), str) or not value["version"]
                    or value.get("not_a_send_authorization") is not True or not isinstance(entries, list)
                    or len(entries) != 2 or {entry["publish_path"] for entry in entries} != set(PACKAGE_PATHS)):
                raise ValueError()
            expected = {entry["publish_path"]: entry["sha256"] for entry in entries}
        except (ValueError, KeyError, TypeError) as exc:
            raise KnowledgeError("invalid commercial manifest") from exc
        documents, total = {}, 0
        for relative in (*COMMERCIAL_PATHS, *sorted(self.selected)):
            snapshot = self.store.read_bounded(relative)
            content = self.selected.get(relative, snapshot.content)
            if relative in self.selected and (not isinstance(content, str) or not content.strip()
                                             or content not in snapshot.content):
                raise KnowledgeError("selected excerpt not in canonical source")
            if relative in expected and snapshot.content_hash != expected[relative]:
                raise KnowledgeError("commercial package hash mismatch")
            total += len(content.encode("utf-8"))
            if total > 300_000:
                raise KnowledgeError("commercial projection too large")
            documents[relative] = {"content": content, "hash": KnowledgeStore._hash(content),
                "source_hash": snapshot.content_hash, "state": "validated" if snapshot.validated else "documented",
                "projection": "selected" if relative in self.selected else "commercial_allowlist"}
        # Detect a concurrent document change, including uncommitted modifications.
        for relative, document in documents.items():
            if self.store.read_bounded(relative).content_hash != document["source_hash"]:
                raise KnowledgeConflict("Brain changed during projection")
        if self.brain_commit() != before or self.store.read_bounded(MANIFEST_PATH).content_hash != manifest.content_hash:
            raise KnowledgeConflict("Brain changed during projection")
        stable = {"schema_version": 1, "package_version": value["version"], "brain_commit": before,
            "manifest_hash": manifest.content_hash, "documents": documents,
            "restrictions": list(RESTRICTIONS), "send_authorized": False, "spend_authorized": False,
            "crm_write_authorized": False}
        return {**stable, "hash": payload_hash(stable), "read_at": datetime.now(timezone.utc).isoformat()}


class SharedPreparation:
    def __init__(self, service, *, context: SharedContext,
                 account_reader: Callable[[str], Mapping[str, Any] | None],
                 clock: Callable[[], datetime] | None = None, history_max_age: timedelta = timedelta(minutes=30)):
        self.service, self.repository, self.context = service, service.repository, context
        self.account_reader = account_reader
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.history_max_age = history_max_age
        service.shared_preparation = self  # Explicit operator injection, never default wiring.

    def _account(self, reference: str) -> dict[str, Any]:
        if not isinstance(reference, str) or not reference.strip() or len(reference) > 253:
            raise ContractError("invalid account reference")
        try:
            account = self.account_reader(reference.strip().lower())
        except Exception as exc:
            raise SharedBlocked("history_unavailable") from exc
        if not isinstance(account, Mapping):
            raise SharedBlocked("identity_unverified")
        domain = account.get("account_id")
        if not isinstance(domain, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,251}[a-z0-9]", domain):
            raise SharedBlocked("identity_unverified")
        history = {}
        for source in HISTORY_SOURCES:
            row = account.get("history", {}).get(source, {}) if isinstance(account.get("history"), Mapping) else {}
            if not isinstance(row, Mapping): row = {}
            history[source] = {"complete": row.get("complete") is True, "observed_at": row.get("observed_at")}
        return {"account_id": domain, "identity": account.get("identity"), "owner": account.get("owner", "unverified"),
                "history": history, "stop_reason": account.get("stop_reason")}

    def _account_block(self, account: Mapping[str, Any]) -> str | None:
        if account["identity"] != "confirmed": return "identity_unverified"
        if account["owner"] not in {None, "ygor"}: return "other_owner"
        if account["stop_reason"] is not None:
            return account["stop_reason"] if account["stop_reason"] in STOP_REASONS else "history_unverified_stop"
        for source in sorted(HISTORY_SOURCES):
            row = account["history"][source]
            if row["complete"] is not True: return "history_incomplete"
            try:
                observed = datetime.fromisoformat(row["observed_at"])
                if observed.tzinfo is None or not timedelta(0) <= self.clock() - observed <= self.history_max_age:
                    return "history_stale"
            except (ValueError, TypeError):
                return "history_stale"
        # Existing OS effect records also block known completed/uncertain touches.
        account_id = account["account_id"]
        for reply in self.repository.list_state("reply"):
            if reply.get("account_id") == account_id: return "reply"
        cadence_ids = {row["id"] for row in self.repository.list_state("cadence") if row.get("account_id") == account_id}
        for effect in self.repository.effects():
            data = effect["payload"]
            if (data.get("account_id") == account_id or data.get("cadence_id") in cadence_ids) and effect["state"] in {"sent", "uncertain", "needs_reconciliation"}:
                return "receipt_exists" if effect["state"] == "sent" else "uncertain_receipt"
        return None

    def paused_reason(self) -> str | None:
        control = self.repository.get_state("agent_control", "global") or {}
        if self.service.global_paused() or control.get("enabled") is not True or control.get("paused") is not False:
            return "agent_paused"
        circuit = self.repository.get_state("agent_circuit", "global") or {}
        if circuit.get("reset_version") == control.get("circuit_reset_version", 0):
            if circuit.get("open"): return "circuit_open"
            try:
                if circuit.get("blocked_until") and self.clock() < datetime.fromisoformat(circuit["blocked_until"]):
                    return "rate_backoff"
            except (ValueError, TypeError):
                return "circuit_state_invalid"
        return None

    def _packet(self) -> dict[str, Any]:
        packet = self.context.packet()
        catalog = self.service.canonical_catalog()
        packet["catalog_hash"] = catalog["content_hash"]
        packet["hash"] = payload_hash({"commercial_hash": packet["hash"], "catalog_hash": catalog["content_hash"]})
        return packet

    def _loading(self, principal, packet):
        key = payload_hash({"actor": principal.actor, "channel": principal.channel,
                            "profile": principal.profile, "context_hash": packet["hash"]})
        if not self.repository.get_state("shared_loading", key):
            self.repository.append_state("shared_loading", key, {
                "actor": principal.actor, "channel": principal.channel, "profile": principal.profile,
                "owner": principal.owner, "context_hash": packet["hash"], "brain_commit": packet["brain_commit"],
                "package_version": packet["package_version"], "state": "delivered_not_model_compliance"})

    def get_context(self, principal, reference):
        authorize(principal, "read")
        account, packet = self._account(reference), self._packet()
        with self.repository.transaction(): self._loading(principal, packet)
        research, issue = None, None
        try:
            research = self.service.current_research(account["account_id"])
            if research.get("shared_context_hash") != packet["hash"]:
                research, issue = None, "dossier_context_unverified"
        except ContractError:
            issue = "current_dossier_missing"
        return {"context": packet, "account": account, "blocked_reason": self._account_block(account) or self.paused_reason(),
                "dossier": research, "dossier_issue": issue, "historical_data": "coverage_metadata_only"}

    def prepare(self, principal, envelope):
        authorize(principal, "prepare")
        required = {"request_id", "account", "scope", "capability", "revision"}
        if not isinstance(envelope, Mapping) or set(envelope) - required - {"note"} or not required <= set(envelope):
            raise ContractError("invalid shared envelope fields")
        normalized = {field: token(envelope[field], field) for field in ("request_id", "scope", "revision")}
        if envelope["capability"] not in {"research", "copy"}: raise ContractError("invalid capability")
        if "note" in envelope and (not isinstance(envelope["note"], str) or len(envelope["note"]) > 2000):
            raise ContractError("invalid note")
        normalized.update({"account": str(envelope["account"]).lower().strip(), "capability": envelope["capability"],
                           "note": envelope.get("note", "")})
        origin_key = payload_hash({"owner": principal.owner, "actor": principal.actor,
            "channel": principal.channel, "profile": principal.profile, "request_id": normalized["request_id"]})
        fingerprint = payload_hash(normalized)
        # Replay is answered without fresh dependency/inference calls.
        with self.repository.transaction():
            previous = self.repository.get_state("shared_origin", origin_key)
            if previous:
                if previous["request_hash"] != fingerprint: raise RepositoryConflict("request_id payload mismatch")
                return self.repository.get_work(previous["work_id"])
        account, packet = self._account(envelope["account"]), self._packet()
        operation = payload_hash({"owner": principal.owner, "account_id": account["account_id"],
            "scope": normalized["scope"], "capability": normalized["capability"], "revision": normalized["revision"]})
        with self.repository.transaction():
            # Recheck origin after dependencies; competing ingress may have committed.
            previous = self.repository.get_state("shared_origin", origin_key)
            if previous:
                if previous["request_hash"] != fingerprint: raise RepositoryConflict("request_id payload mismatch")
                return self.repository.get_work(previous["work_id"])
            existing = self.repository.get_state("shared_operation", operation)
            if existing:
                item = self.repository.get_work(existing["work_id"])
                if existing["context_hash"] != packet["hash"]:
                    raise RepositoryConflict("context changed; explicit reviewed revision required")
            else:
                block = self._account_block(account) or self.paused_reason()
                item = WorkItem(id=f"work-{uuid4().hex}", objective=f"Preparar {normalized['capability']} de {account['account_id']}",
                    actor=principal.actor, logical_owner=principal.owner, idempotency_key=f"shared:{operation}",
                    status=WorkStatus.NEEDS_HUMAN if block else WorkStatus.REQUESTED,
                    next_action=f"resolver bloqueio: {block}" if block else "aguardar preparação interna",
                    context={"capability": normalized["capability"], "account_id": account["account_id"],
                        "shared_operation": operation, "shared_revision": normalized["revision"],
                        "shared_scope": normalized["scope"], "shared_context_hash": packet["hash"]})
                self.repository.save_with_event(item, 0, "shared_work_blocked" if block else "work_requested",
                                                None if block else "agent_job")
                self.repository.append_state("shared_operation", operation, {"work_id": item.id, "context_hash": packet["hash"]})
                self.repository.append_state("shared_link", item.id, {"operation": operation, "owner": "ygor",
                    "actor": principal.actor, "channel": principal.channel, "profile": principal.profile,
                    "account_id": account["account_id"], "context": packet, "initial_block": block})
            self.repository.append_state("shared_origin", origin_key, {"work_id": item.id, "request_hash": fingerprint,
                "request_id": normalized["request_id"], "actor": principal.actor, "channel": principal.channel,
                "profile": principal.profile, "owner": principal.owner})
            self._loading(principal, packet)
            return item

    def trusted_link(self, item) -> bool:
        link = self.repository.get_state("shared_link", item.id)
        operation = self.repository.get_state("shared_operation", item.context.get("shared_operation", ""))
        return bool(link and operation and operation["work_id"] == item.id and link["owner"] == item.logical_owner == "ygor"
            and link["operation"] == item.context.get("shared_operation")
            and link["account_id"] == item.context.get("account_id")
            and link["context"]["hash"] == item.context.get("shared_context_hash"))

    def check_current(self, item) -> dict[str, Any]:
        if not self.trusted_link(item): raise SharedBlocked("shared_link_invalid")
        if self.paused_reason(): raise SharedBlocked("agent_paused")
        account = self._account(item.context["account_id"])
        if account["account_id"] != item.context["account_id"]: raise SharedBlocked("identity_changed")
        block = self._account_block(account)
        if block: raise SharedBlocked(block)
        cancel = self.repository.get_state("agent_cancel", item.id)
        if cancel and cancel.get("actor") == "ygor" and cancel.get("cancelled") is True:
            raise SharedBlocked("execution_cancelled")
        packet = self._packet()
        if packet["hash"] != item.context["shared_context_hash"]: raise SharedBlocked("context_changed")
        return self.repository.get_state("shared_link", item.id)["context"]

    def generate_once(self, job: AgentJob, runner, *, lease_owner: str) -> AgentResult:
        if runner.max_attempts != 1: return AgentResult("needs_human", error_code="single_attempt_required")
        fingerprint = payload_hash({"type": job.type, "payload": dict(job.payload)})
        dispatch = uuid4().hex
        with self.repository.transaction() as con:
            lease = con.execute("SELECT id FROM thunder_outbox WHERE entity_id=? AND kind='agent_job' AND state='pending' AND lease_owner=? AND lease_until>?",
                                (job.id, lease_owner, self.clock().isoformat())).fetchone()
            if not lease: raise RepositoryConflict("generation lease lost")
            self.check_current(self.repository.get_work(job.id))
            previous = self.repository.get_state("shared_generation", job.id)
            if previous:
                if previous["job_hash"] != fingerprint: return AgentResult("needs_human", error_code="generation_input_changed")
                if previous["status"] == "completed":
                    return AgentResult("succeeded", output=previous["output"])
                return AgentResult("needs_human", error_code="inference_needs_reconciliation")
            self.repository.append_state("shared_generation", job.id, {"status": "reserved", "job_hash": fingerprint,
                "dispatch": dispatch, "max_attempts": 1, "external_effects_enabled": False})
        try:
            result = runner.run(job)
        except Exception:
            result = AgentResult("needs_human", error_code="agent_transport_failed")
        # Persist parsed output before workflow completion, for deterministic recovery.
        with self.repository.transaction():
            state = self.repository.get_state("shared_generation", job.id)
            if state["dispatch"] != dispatch: raise RepositoryConflict("generation fence changed")
            self.repository.put_state("shared_generation", job.id, {
                "status": "completed" if result.status == "succeeded" else "uncertain", "job_hash": fingerprint,
                "dispatch": dispatch, "max_attempts": 1, "external_effects_enabled": False,
                "output": dict(result.output) if result.output is not None else None, "error_code": result.error_code})
        return result

    def get_work(self, principal, work_id):
        authorize(principal, "read")
        item = self.repository.get_work(work_id)
        if item is None or not self.trusted_link(item): raise KeyError("shared work not found")
        link = self.repository.get_state("shared_link", item.id)
        block = None
        try:
            self.check_current(item)
        except SharedBlocked as exc:
            block = exc.code
        except (ContractError, KnowledgeError):
            block = "context_unavailable"
        if item.status == WorkStatus.NEEDS_HUMAN:
            block = block or (item.result or {}).get("dependency_failure") or link["initial_block"]
        handoff = self.repository.get_state("shared_handoff", item.id)
        generation = self.repository.get_state("shared_generation", item.id)
        generation = {key: value for key, value in generation.items() if key != "output"} if generation else None
        learning = [row for row in self.repository.list_state("shared_learning") if row["work_id"] == item.id]
        for row in learning:
            row["handoff"] = self.repository.get_state("shared_handoff", f"learning-{row['id']}")
            row["decisions"] = [decision for decision in self.repository.list_state("shared_learning_decision")
                                if decision["proposal_id"] == row["id"]]
            for decision in row["decisions"]:
                decision["handoff"] = self.repository.get_state("shared_handoff", f"decision-{decision['id']}")
        return {**as_payload(item), "origins": [row for row in self.repository.list_state("shared_origin") if row["work_id"] == item.id],
            "blocked_reason": block, "context_hash": link["context"]["hash"],
            "handoff": handoff or {"status": "not_ready"}, "send_authorized": False,
            "generation": generation, "learning": learning}

    def enqueue_handoff(self, item) -> None:
        link = self.repository.get_state("shared_link", item.id)
        if not link or item.status != WorkStatus.SUCCEEDED: return
        result = item.result or {}
        ref = result.get("artifact_ref", {})
        artifact = self.repository.get_state(ref.get("kind", ""), ref.get("id", "")) or {}
        selected = {key: artifact[key] for key in ("account", "facts", "counter_signals", "research_gaps",
            "opportunity_hypothesis", "alternative_hypothesis", "next_action", "final_copy") if key in artifact}
        payload = {"schema_version": 1, "work_id": item.id, "account_id": item.context["account_id"],
            "owner": "ygor", "origin_actor": link["actor"], "executor": item.actor, "outcome": item.status.value,
            "context_hash": item.context["shared_context_hash"], "brain_commit": link["context"]["brain_commit"],
            "artifact_ref": ref, "artifact_hash": payload_hash(artifact), "selected_artifact": selected,
            "classification": "observed_result_not_rule_approval", "next_action": item.next_action,
            "send_authorized": False, "origins": [row for row in self.repository.list_state("shared_origin") if row["work_id"] == item.id]}
        self._queue_handoff(item.id, payload)

    def _queue_handoff(self, entity_id, payload):
        with self.repository.transaction() as con:
            if self.repository.get_state("shared_handoff", entity_id): return
            self.repository.append_state("shared_handoff", entity_id, {"status": "pending", "payload_hash": payload_hash(payload)})
            con.execute("INSERT INTO thunder_outbox(id,entity_id,kind,payload,created_at) VALUES (?,?,?,?,?)",
                (uuid4().hex, entity_id, "shared_brain_handoff", json.dumps(payload, sort_keys=True, ensure_ascii=False), self.clock().isoformat()))

    def propose_learning(self, principal, work_id, hypothesis, source):
        authorize(principal, "prepare")
        self.get_work(principal, work_id)
        if not all(isinstance(v, str) and v.strip() and len(v) <= 2000 for v in (hypothesis, source)):
            raise ContractError("selected learning hypothesis/source required")
        key = payload_hash({"work_id": work_id, "hypothesis": hypothesis, "source": source, "actor": principal.actor})
        with self.repository.transaction():
            existing = self.repository.get_state("shared_learning", key)
            if existing: return existing
            proposal = self.repository.append_state("shared_learning", key, {"work_id": work_id, "hypothesis": hypothesis,
                "source": source, "actor": principal.actor, "owner": principal.owner, "status": "proposed"})
            self._queue_handoff(f"learning-{key}", {"schema_version": 1, "export_id": f"learning-{key}",
                "work_id": work_id, "origin_actor": principal.actor, "owner": principal.owner,
                "classification": "hypothesis_not_approved_rule", "proposal": proposal, "send_authorized": False})
            return proposal

    def record_learning_decision(self, principal, proposal_id, decision, origin, *, supersedes=None):
        authorize(principal, "decision")
        if decision not in {"approved", "rejected"} or not isinstance(origin, str) or not origin.strip() or len(origin) > 2000:
            raise ContractError("attributed human decision required")
        with self.repository.transaction():
            if not self.repository.get_state("shared_learning", proposal_id): raise ContractError("proposal missing")
            if supersedes:
                previous = self.repository.get_state("shared_learning_decision", supersedes)
                if not previous or previous["proposal_id"] != proposal_id: raise ContractError("invalid supersession")
            key = payload_hash({"proposal_id": proposal_id, "decision": decision, "origin": origin, "supersedes": supersedes})
            existing = self.repository.get_state("shared_learning_decision", key)
            if existing: return existing
            decision_record = self.repository.append_state("shared_learning_decision", key, {"proposal_id": proposal_id,
                "decision": decision, "origin": origin, "owner": principal.owner, "actor": principal.actor,
                "supersedes": supersedes, "canonical_promotion": False})
            self._queue_handoff(f"decision-{key}", {"schema_version": 1, "export_id": f"decision-{key}",
                "work_id": self.repository.get_state("shared_learning", proposal_id)["work_id"],
                "origin_actor": principal.actor, "owner": principal.owner, "decision_record": decision_record,
                "classification": "attributed_decision_without_canonical_promotion", "send_authorized": False})
            return decision_record


class LocalOSAdapter:
    """Thin callable boundary for an existing authenticated route; no server/grants."""
    def __init__(self, core, *, principal_resolver, actor, channel, profile):
        self.core, self.principal_resolver = core, principal_resolver
        self.actor, self.channel, self.profile = actor, channel, profile

    def call(self, method, arguments, transport_context=None):
        try:
            principal = self.principal_resolver(transport_context)
        except Exception:
            return {"status": 401, "body": {"error": "principal_unavailable"}}
        if principal is None: return {"status": 401, "body": {"error": "principal_required"}}
        try:
            authorize(principal, "prepare" if method == "request_preparation" else "read")
            if (principal.actor, principal.channel, principal.profile) != (self.actor, self.channel, self.profile):
                raise SharedAuthorizationError("principal route mismatch")
            if not isinstance(arguments, Mapping): raise ContractError("arguments must be object")
            if method == "get_context" and set(arguments) == {"account"}:
                return {"status": 200, "body": self.core.get_context(principal, arguments["account"])}
            if method == "get_work" and set(arguments) == {"work_id"}:
                return {"status": 200, "body": self.core.get_work(principal, arguments["work_id"])}
            if method == "request_preparation":
                return {"status": 201, "body": as_payload(self.core.prepare(principal, arguments))}
            raise ContractError("unsupported method or fields")
        except SharedAuthorizationError:
            return {"status": 403, "body": {"error": "scope_denied"}}
        except RepositoryConflict:
            return {"status": 409, "body": {"error": "request_conflict"}}
        except KeyError:
            return {"status": 404, "body": {"error": "shared_work_not_found"}}
        except (ContractError, KnowledgeError, TypeError):
            return {"status": 422, "body": {"error": "invalid_or_incomplete_context"}}


class BrainHandoff:
    """Explicitly called local exporter over the existing outbox, not a scheduler."""
    def __init__(self, core, store):
        self.core, self.repository, self.store = core, core.repository, store
        self.worker_id = f"shared-handoff-{uuid4().hex}"

    def run_once(self):
        entry = self.repository.claim_outbox(self.worker_id, self.core.clock(), timedelta(minutes=1), kind="shared_brain_handoff")
        if not entry: return {"status": "idle"}
        payload = json.loads(entry["payload"])
        relative = f"_entrada/{payload['origin_actor']}/shared-{payload.get('export_id', payload['work_id'])}.json"
        try:
            file_hash = self.store.write_selected_handoff(relative, payload)
            with self.repository.transaction():
                self.repository.finish_outbox(entry["id"], entry["lease_owner"])
                self.repository.put_state("shared_handoff", entry["entity_id"], {"status": "synced",
                    "payload_hash": payload_hash(payload), "path": relative, "file_hash": file_hash,
                    "transport": "local_readback_only_not_Git_sync"})
            return {"status": "synced", "work_id": entry["entity_id"]}
        except (KnowledgeError, OSError, RepositoryConflict) as exc:
            status = "conflict" if isinstance(exc, KnowledgeConflict) else "pending"
            with self.repository.transaction() as con:
                # A stale exporter cannot overwrite a newer successful readback.
                row = con.execute("SELECT state,lease_owner,lease_until FROM thunder_outbox WHERE id=?", (entry["id"],)).fetchone()
                if (row and row["state"] == "pending" and row["lease_owner"] == entry["lease_owner"]
                        and row["lease_until"] > self.core.clock().isoformat()):
                    self.repository.put_state("shared_handoff", entry["entity_id"], {"status": status,
                        "payload_hash": payload_hash(payload), "error_code": "handoff_conflict" if status == "conflict" else "handoff_pending"})
            return {"status": status, "work_id": entry["entity_id"]}


def selected_transport_context(job: AgentJob, bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Shared packet binding used by the existing textual child, without inference."""
    packet = job.payload.get("shared_commercial_context")
    incoming = bundle.get("shared_commercial_context")
    if packet is None:
        if incoming is not None: raise ContractError("unexpected shared evidence context")
        return {}
    if (not isinstance(packet, Mapping) or packet != incoming or packet.get("send_authorized") is not False
            or not packet.get("hash") or not packet.get("restrictions")):
        raise ContractError("shared context transport mismatch")
    return {"shared_commercial_context": dict(packet)}
