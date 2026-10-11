"""Local acceptance fixtures: no runtime credentials, network, messages or CRM."""
import copy
import hashlib
import json
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

# The separately owned toques dependency is not vendored here. These paths never
# use it; a temporary import stub permits source-only, offline contract tests.
_had_toques = "toques" in sys.modules
if not _had_toques:
    sys.modules["toques"] = SimpleNamespace(ErroBanco=RuntimeError)
try:
    from thunder_os.agent_gateway import AgentGateway
    from thunder_os.agent_worker import AgentWorker
    from thunder_os.service import ThunderService
finally:
    if not _had_toques:
        sys.modules.pop("toques", None)
from thunder_os.agents import AgentRunner
from thunder_os.agents import AgentJob
from thunder_os.db import Repository, RepositoryConflict
from thunder_os.knowledge import KnowledgeStore, KnowledgeError
from thunder_os.models import ContractError, WorkStatus
from thunder_os.shared_preparation import (
    SharedContext, SharedPreparation, SharedPrincipal, LocalOSAdapter, BrainHandoff,
    COMMERCIAL_PATHS, PACKAGE_PATHS, MANIFEST_PATH,
    selected_transport_context,
)
from test_thunder_os_research import base_artifact


RULES = [
    "Somente Ygor como owner; outro owner excluído.",
    "Histórico completo e dedupe antes de cada ação.",
    "Vaga é necessidade possível, não interesse em agência.",
    "Proposta de parceria contínua.",
    "Dossiê com fato, fonte, data, hipótese e contrassinal.",
    "Texto curto humano.",
    "Case de terceiro atribuído claramente.",
    "Aprovação exata; parar em resposta/recusa/opt-out/inválido; receipts só reais.",
]


class SharedPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "knowledge"
        self.root.mkdir()
        for relative in COMMERCIAL_PATHS:
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("status: validado\n" + "\n".join(RULES), encoding="utf-8")
        for relative in ("eu/estilo-e-regras.md", "eu/aprendizados.md"):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("SELECTED: curto e humano\nPRIVATE: segredo pessoal\n", encoding="utf-8")
        self.publish_manifest()
        (self.root / "thunder/catalog.json").write_text(json.dumps({
            "version": "fixture-1", "status": "validado", "capabilities": ["cap-1"],
            "proofs": ["proof-1"],
        }), encoding="utf-8")
        self.store = KnowledgeStore(self.root)
        self.commit = "a" * 40
        self.context = SharedContext(self.store, brain_commit=lambda: self.commit, selected={
            "eu/estilo-e-regras.md": "SELECTED: curto e humano",
            "eu/aprendizados.md": "SELECTED: curto e humano",
        })
        self.repo = Repository(Path(self.temp.name) / "state.sqlite")
        self.service = ThunderService(self.repo, knowledge_store=self.store)
        self.repo.put_state("agent_control", "global", {"enabled": True, "paused": False,
            "activation_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()})
        self.snapshot = {"account_id": "acme.example", "identity": "confirmed", "owner": "ygor",
            "history": {source: {"complete": True, "observed_at": datetime.now(timezone.utc).isoformat()}
                for source in ("hubspot", "gmail", "linkedin")}, "stop_reason": None}
        self.core = SharedPreparation(self.service, context=self.context,
            account_reader=lambda ref: copy.deepcopy(self.snapshot) if ref.lower() in {"acme", "acme.example", "acme ltda"} else None)
        self.principals = {actor: SharedPrincipal("ygor", actor,
            "telegram" if actor == "hermes" else "mcp" if actor == "dotin" else "local",
            "ygor-personal" if actor == "hermes" else "existing",
            frozenset({"read", "prepare"})) for actor in ("dotin", "hermes", "claude", "codex")}
        self.calls = []
        self.lock = threading.Lock()
        # Any accidental provider process/socket is an immediate failure.
        for target in ("socket.socket", "socket.create_connection", "subprocess.run", "subprocess.Popen"):
            guard = patch(target, side_effect=AssertionError("external effect forbidden"))
            guard.start()
            self.addCleanup(guard.stop)

    def publish_manifest(self):
        path = self.root / MANIFEST_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "package_id": "thunder-sales-playbook",
            "version": "1.0.0", "not_a_send_authorization": True,
            "files": [{"publish_path": relative, "sha256": hashlib.sha256((self.root / relative).read_bytes()).hexdigest()}
                for relative in PACKAGE_PATHS]}), encoding="utf-8")

    def envelope(self, request_id="r1", **changes):
        return {"request_id": request_id, "account": "acme", "scope": "fixture-campaign",
            "capability": "research", "revision": "1", **changes}

    def adapter(self, actor, principal=None):
        return LocalOSAdapter(self.core, principal_resolver=lambda _: principal or self.principals[actor],
            actor=actor, channel=self.principals[actor].channel, profile=self.principals[actor].profile)

    def generate(self, job, timeout):
        with self.lock:
            self.calls.append(job)
        if job.type == "copy":
            return json.dumps({"copy": "Vi a expansão. Faz sentido conversar sobre uma parceria contínua?",
                "angle_id": "angle-1", "evidence": [{"id": "cap-1"}, {"id": "proof-1"}]})
        return json.dumps({"artifact": base_artifact(), "evidence": [{"source": "https://acme.example/company"}]})

    def worker(self, transport=None, max_attempts=1):
        return AgentWorker(self.service, runner=AgentRunner(transport or self.generate, max_attempts=max_attempts), actor="codex")

    def test_cross_channel_continuation_both_directions(self):
        for first, second in (("dotin", "hermes"), ("hermes", "dotin")):
            scope = f"direction-{first}"
            one = self.adapter(first).call("request_preparation", self.envelope(f"{scope}-{first}", scope=scope))["body"]
            two = self.adapter(second).call("request_preparation", self.envelope(f"{scope}-{second}", scope=scope, account="acme ltda"))["body"]
            self.assertEqual(one["id"], two["id"])
            self.assertEqual("ygor", one["logical_owner"])
            self.assertEqual(first, one["actor"])
            self.assertEqual("succeeded", self.worker().run_once()["status"])
            a = self.adapter(first).call("get_work", {"work_id": one["id"]})["body"]
            b = self.adapter(second).call("get_work", {"work_id": one["id"]})["body"]
            self.assertEqual(a["result"], b["result"])
            self.assertEqual({"dotin", "hermes"}, {origin["actor"] for origin in a["origins"]})
        self.assertEqual(2, len(self.calls))

    def test_concurrent_equivalent_requests_generate_once(self):
        barrier = threading.Barrier(8)
        def request(i):
            barrier.wait()
            return self.core.prepare(self.principals["dotin" if i % 2 else "hermes"],
                self.envelope(f"parallel-{i}", account="acme ltda" if i % 2 else "acme.example", note=f"wording {i}"))
        with ThreadPoolExecutor(max_workers=8) as pool:
            items = list(pool.map(request, range(8)))
        self.assertEqual(1, len({item.id for item in items}))
        self.assertEqual(1, len([row for row in self.repo.outbox() if row["kind"] == "agent_job"]))
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.worker().run_once(), range(4)))
        self.assertEqual(1, sum(result["status"] == "succeeded" for result in results))
        self.assertEqual(1, len(self.calls))
        self.core.prepare(self.principals["claude"], self.envelope("later"))
        self.worker().run_once()
        self.assertEqual(1, len(self.calls))
        self.assertEqual(9, len(self.core.get_work(self.principals["dotin"], items[0].id)["origins"]))

    def test_replay_conflict_and_explicit_revision(self):
        item = self.core.prepare(self.principals["dotin"], self.envelope())
        replay = self.core.prepare(self.principals["dotin"], self.envelope())
        self.assertEqual(item.id, replay.id)
        self.assertEqual(1, len(self.repo.list_state("shared_origin")))
        with self.assertRaises(RepositoryConflict):
            self.core.prepare(self.principals["dotin"], self.envelope(note="different payload"))
        new = self.core.prepare(self.principals["dotin"], self.envelope("r2", revision="2"))
        self.assertNotEqual(item.id, new.id)
        self.assertEqual(2, len(self.repo.list_work()))
        self.assertEqual("1", self.repo.get_work(item.id).context["shared_revision"])

    def test_context_receipts_four_actors_eight_rules_no_private_data(self):
        packets = [self.adapter(actor).call("get_context", {"account": "acme"})["body"]["context"]
            for actor in ("dotin", "hermes", "claude", "codex")]
        self.assertEqual(1, len({packet["hash"] for packet in packets}))
        self.assertEqual({"1.0.0"}, {packet["package_version"] for packet in packets})
        for rule in RULES:
            self.assertIn(rule, packets[0]["documents"][PACKAGE_PATHS[0]]["content"])
        self.assertNotIn("PRIVATE", json.dumps(packets))
        self.assertFalse(packets[0]["send_authorized"])
        self.assertEqual({"dotin", "hermes", "claude", "codex"},
            {receipt["actor"] for receipt in self.repo.list_state("shared_loading")})
        self.assertEqual(0, len(self.calls))

    def test_context_bounds_manifest_and_selected_projection(self):
        before = self.context.packet()["hash"]
        path = self.root / PACKAGE_PATHS[0]
        path.write_text("tampered", encoding="utf-8")
        with self.assertRaises(KnowledgeError): self.context.packet()
        self.publish_manifest()
        self.assertNotEqual(before, self.context.packet()["hash"])
        (self.root / "SISTEMA.md").write_text("x" * 100_001, encoding="utf-8")
        with self.assertRaises(KnowledgeError): self.context.packet()
        (self.root / "SISTEMA.md").write_text("ok", encoding="utf-8")
        self.context.selected["eu/estilo-e-regras.md"] = "not in source"
        with self.assertRaises(KnowledgeError): self.context.packet()
        with self.assertRaises(KnowledgeError):
            SharedContext(self.store, brain_commit=lambda: self.commit, selected={"private/sessions.json": "secret"}).packet()

    def test_text_child_receives_selected_packet(self):
        packet = self.core.get_context(self.principals["hermes"], "acme")["context"]
        job = AgentJob("fixture", "research", {"shared_commercial_context": packet})
        projected = selected_transport_context(job, {"shared_commercial_context": copy.deepcopy(packet)})
        self.assertEqual(packet["hash"], projected["shared_commercial_context"]["hash"])
        self.assertNotIn("PRIVATE", json.dumps(projected))
        with self.assertRaises(ContractError): selected_transport_context(job, {})
        with self.assertRaises(ContractError):
            selected_transport_context(AgentJob("fixture", "research", {}), {"shared_commercial_context": packet})
        self.assertEqual({}, selected_transport_context(AgentJob("legacy", "research", {}), {}))

    def test_context_changes_between_and_during_work(self):
        old = self.core.prepare(self.principals["dotin"], self.envelope())
        path = self.root / "SISTEMA.md"
        path.write_text(path.read_text() + "\nNew rule", encoding="utf-8")
        new = self.core.prepare(self.principals["dotin"], self.envelope("r2", revision="2"))
        self.assertNotEqual(old.context["shared_context_hash"], new.context["shared_context_hash"])
        self.assertEqual(old.context["shared_context_hash"], self.repo.get_work(old.id).context["shared_context_hash"])
        self.assertEqual("needs_human", self.worker().run_once()["status"])
        self.assertEqual(0, len(self.calls))
        def change_during(job, timeout):
            result = self.generate(job, timeout)
            path.write_text(path.read_text() + "\nChanged again", encoding="utf-8")
            return result
        self.assertEqual("needs_human", self.worker(change_during).run_once()["status"])
        self.assertIsNone(self.repo.get_state("research", "acme.example"))
        self.assertEqual(1, len(self.calls))
        # A fresh copy request cannot reuse a dossier tied to a previous Brain.
        self.snapshot["stop_reason"] = None
        stable = self.core.prepare(self.principals["dotin"], self.envelope("stable", revision="stable"))
        self.assertEqual("succeeded", self.worker().run_once()["status"])
        path.write_text(path.read_text() + "\nNext version", encoding="utf-8")
        stale_copy = self.core.prepare(self.principals["dotin"], self.envelope("stale-copy", capability="copy", revision="stale-copy"))
        calls = len(self.calls)
        self.assertEqual("needs_human", self.worker().run_once()["status"])
        self.assertEqual("research_context_changed", self.repo.get_work(stale_copy.id).result["dependency_failure"])
        self.assertEqual(calls, len(self.calls))

    def test_owner_identity_history_fail_closed(self):
        cases = [({"owner": "other"}, "other_owner"), ({"identity": "ambiguous"}, "identity_unverified"),
            ({"history": {}}, "history_incomplete"), ({"stop_reason": "invalid_address"}, "invalid_address")]
        stale = copy.deepcopy(self.snapshot["history"])
        stale["gmail"]["observed_at"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        cases.append(({"history": stale}, "history_stale"))
        for i, (change, expected) in enumerate(cases):
            original = copy.deepcopy(self.snapshot)
            self.snapshot.update(change)
            item = self.core.prepare(self.principals["hermes"], self.envelope(f"blocked-{i}", revision=str(i + 1)))
            self.assertEqual(WorkStatus.NEEDS_HUMAN, item.status)
            self.assertEqual(expected, self.core.get_work(self.principals["dotin"], item.id)["blocked_reason"])
            self.assertEqual("idle", self.worker().run_once()["status"])
            self.snapshot = original
        self.assertEqual(0, len(self.calls))
        self.assertEqual([], self.repo.effects())

    def test_result_and_idempotent_handoff_with_conflict(self):
        item = self.core.prepare(self.principals["dotin"], self.envelope())
        self.assertEqual("succeeded", self.worker().run_once()["status"])
        self.assertEqual("pending", self.core.get_work(self.principals["hermes"], item.id)["handoff"]["status"])
        exporter = BrainHandoff(self.core, self.store)
        with patch.object(self.repo, "finish_outbox", side_effect=RepositoryConflict("lost lease")):
            self.assertEqual("pending", exporter.run_once()["status"])
        target = self.root / f"_entrada/dotin/shared-{item.id}.json"
        content = target.read_bytes()
        with self.repo.transaction() as con:
            con.execute("UPDATE thunder_outbox SET lease_until=NULL WHERE kind='shared_brain_handoff'")
        self.assertEqual("synced", exporter.run_once()["status"])
        self.assertEqual(content, target.read_bytes())
        self.assertEqual("idle", exporter.run_once()["status"])
        self.assertEqual("synced", self.core.get_work(self.principals["hermes"], item.id)["handoff"]["status"])
        next_item = self.core.prepare(self.principals["dotin"], self.envelope("new", revision="2"))
        self.worker().run_once()
        conflict = self.root / f"_entrada/dotin/shared-{next_item.id}.json"
        conflict.write_text("conflict", encoding="utf-8")
        self.assertEqual("conflict", exporter.run_once()["status"])
        self.assertEqual("conflict", conflict.read_text())
        self.assertEqual("pending", next(row for row in self.repo.outbox() if row["entity_id"] == next_item.id and row["kind"] == "shared_brain_handoff")["state"])

    def test_learning_is_proposal_not_rule(self):
        item = self.core.prepare(self.principals["codex"], self.envelope())
        rule_hash = self.context.packet()["hash"]
        proposal = self.core.propose_learning(self.principals["codex"], item.id, "Teste hipótese de CTA", "internal:fixture")
        self.assertEqual("proposed", proposal["status"])
        self.assertEqual("pending", self.core.get_work(self.principals["dotin"], item.id)["learning"][0]["handoff"]["status"])
        with self.assertRaises(ContractError):
            self.core.record_learning_decision(self.principals["codex"], proposal["id"], "approved", "fixture-decision")
        human = SharedPrincipal("ygor", "dotin", "mcp", "existing", frozenset({"read", "decision"}))
        approved = self.core.record_learning_decision(human, proposal["id"], "approved", "human:fixture-approval")
        rejected = self.core.record_learning_decision(human, proposal["id"], "rejected", "human:fixture-rejection", supersedes=approved["id"])
        self.assertEqual(approved["id"], rejected["supersedes"])
        self.assertEqual("human:fixture-rejection", rejected["origin"])
        self.assertEqual(rule_hash, self.context.packet()["hash"])
        self.assertEqual("proposed", self.repo.get_state("shared_learning", proposal["id"])["status"])
        exporter = BrainHandoff(self.core, self.store)
        self.assertEqual("synced", exporter.run_once()["status"])
        exported = json.loads((self.root / f"_entrada/codex/shared-learning-{proposal['id']}.json").read_text())
        self.assertEqual("hypothesis_not_approved_rule", exported["classification"])
        self.assertEqual("proposed", exported["proposal"]["status"])
        self.assertEqual("synced", self.core.get_work(self.principals["hermes"], item.id)["learning"][0]["handoff"]["status"])
        self.assertEqual(rule_hash, self.context.packet()["hash"])

    def test_cached_output_after_lease_loss_no_second_inference(self):
        item = self.core.prepare(self.principals["dotin"], self.envelope())
        with patch.object(self.repo, "outbox_completion", side_effect=RepositoryConflict("expired")):
            self.assertEqual("lease_lost", self.worker().run_once()["status"])
        self.assertEqual(1, len(self.calls))
        self.assertEqual("completed", self.repo.get_state("shared_generation", item.id)["status"])
        with self.repo.transaction() as con:
            con.execute("UPDATE thunder_outbox SET lease_until=NULL WHERE kind='agent_job'")
        self.assertEqual("succeeded", self.worker().run_once()["status"])
        self.assertEqual(1, len(self.calls))
        self.assertEqual(1, len(self.repo.list_state("research")))
        self.assertEqual(1, len([row for row in self.repo.outbox() if row["kind"] == "shared_brain_handoff"]))

    def test_interruption_timeout_and_unsafe_runner_no_retry(self):
        item = self.core.prepare(self.principals["dotin"], self.envelope())
        def timeout(job, seconds):
            self.calls.append(job)
            raise TimeoutError("unknown cost")
        self.assertEqual("needs_human", self.worker(timeout).run_once()["status"])
        self.assertEqual(1, len(self.calls))
        self.assertEqual("uncertain", self.repo.get_state("shared_generation", item.id)["status"])
        self.core.prepare(self.principals["hermes"], self.envelope("continue"))
        self.worker(timeout).run_once()
        self.assertEqual(1, len(self.calls))
        unsafe = self.core.prepare(self.principals["dotin"], self.envelope("unsafe", revision="2"))
        self.assertEqual("needs_human", self.worker(max_attempts=2).run_once()["status"])
        self.assertIsNone(self.repo.get_state("shared_generation", unsafe.id))
        self.assertEqual(1, len(self.calls))
        interrupted = self.core.prepare(self.principals["dotin"], self.envelope("interrupted", revision="3"))
        def crash(job, seconds):
            self.calls.append(job)
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): self.worker(crash).run_once()
        with self.repo.transaction() as con:
            con.execute("UPDATE thunder_outbox SET lease_until=NULL WHERE kind='agent_job'")
        self.assertEqual("needs_human", self.worker().run_once()["status"])
        self.assertEqual(2, len(self.calls))
        self.assertEqual("reserved", self.repo.get_state("shared_generation", interrupted.id)["status"])

    def test_stops_before_and_during_generation_zero_effects(self):
        for i, stop in enumerate(("reply", "refusal", "opt_out", "invalid_address", "receipt_exists", "uncertain_receipt")):
            self.snapshot["stop_reason"] = stop
            item = self.core.prepare(self.principals["dotin"], self.envelope(f"stop-{i}", revision=str(i)))
            self.assertEqual(WorkStatus.NEEDS_HUMAN, item.status)
            self.assertEqual(stop, self.core.get_work(self.principals["hermes"], item.id)["blocked_reason"])
        self.snapshot["stop_reason"] = None
        item = self.core.prepare(self.principals["dotin"], self.envelope("mid", revision="mid"))
        def reply_during(job, timeout):
            result = self.generate(job, timeout)
            self.snapshot["stop_reason"] = "reply"
            return result
        self.assertEqual("needs_human", self.worker(reply_during).run_once()["status"])
        self.assertIsNone(self.repo.get_state("research", "acme.example"))
        self.assertEqual([], self.repo.effects())
        self.assertEqual([], self.repo.list_state("receipt"))
        self.assertEqual([], self.repo.list_state("deal"))
        self.snapshot["stop_reason"] = None
        self.core.prepare(self.principals["hermes"], self.envelope("ok", revision="ok"))
        self.worker().run_once()
        self.core.prepare(self.principals["dotin"], self.envelope("copy", capability="copy", revision="copy"))
        self.assertEqual("succeeded", self.worker().run_once()["status"])
        self.assertEqual([], self.repo.effects())
        self.assertEqual([], self.repo.list_state("receipt"))
        self.assertEqual([], self.repo.list_state("deal"))

    def test_adapter_authorization_and_forged_fields(self):
        missing = LocalOSAdapter(self.core, principal_resolver=lambda _: None, actor="dotin", channel="mcp", profile="existing")
        self.assertEqual(401, missing.call("get_context", {"account": "acme"})["status"])
        unauthorized = SharedPrincipal("other", "dotin", "mcp", "existing", frozenset({"read", "prepare"}))
        self.assertEqual(403, self.adapter("dotin", unauthorized).call("request_preparation", self.envelope())["status"])
        read_only = SharedPrincipal("ygor", "dotin", "mcp", "existing", frozenset({"read"}))
        self.assertEqual(403, self.adapter("dotin", read_only).call("request_preparation", self.envelope())["status"])
        self.assertEqual(200, self.adapter("dotin").call("get_context", {"account": "acme"})["status"])
        for field in ("actor", "approved", "receipt", "model", "command", "brain_path"):
            self.assertEqual(422, self.adapter("dotin").call("request_preparation", self.envelope(**{field: "forged"}))["status"])
        response = self.adapter("dotin").call("request_preparation", self.envelope())
        self.assertEqual(201, response["status"])
        self.assertEqual(409, self.adapter("dotin").call("request_preparation", self.envelope(note="change"))["status"])
        self.assertEqual(404, self.adapter("dotin").call("get_work", {"work_id": "work-missing"})["status"])
        self.assertEqual(422, self.adapter("dotin").call("send", {})["status"])
        self.assertEqual(1, len(self.repo.list_work()))
        self.assertEqual([], self.repo.list_state("identity"))

    def test_existing_os_reply_and_receipt_also_stop(self):
        self.repo.put_state("reply", "fixture-reply", {"account_id": "acme.example", "state": "classified", "classification": "positive"})
        blocked = self.core.prepare(self.principals["hermes"], self.envelope("reply"))
        self.assertEqual(WorkStatus.NEEDS_HUMAN, blocked.status)
        self.assertEqual("reply", self.core.get_work(self.principals["dotin"], blocked.id)["blocked_reason"])
        # Use another synthetic repository to avoid deleting historic reply records.
        repo = Repository(Path(self.temp.name) / "receipts.sqlite")
        service = ThunderService(repo, knowledge_store=self.store)
        core = SharedPreparation(service, context=self.context, account_reader=lambda _: self.snapshot)
        repo.put_state("agent_control", "global", {"enabled": True, "paused": False})
        repo.put_state("cadence", "fixture-cadence", {"account_id": "acme.example"})
        effect = repo.enqueue_effect("fixture-effect", {"cadence_id": "fixture-cadence"})
        with repo.transaction() as con:
            con.execute("UPDATE thunder_effect_jobs SET state='sent',receipt_ref='synthetic-completed' WHERE id=?", (effect["id"],))
        blocked = core.prepare(self.principals["dotin"], self.envelope("receipt"))
        self.assertEqual(WorkStatus.NEEDS_HUMAN, blocked.status)
        self.assertEqual("receipt_exists", core.get_work(self.principals["hermes"], blocked.id)["blocked_reason"])
        self.assertEqual(0, len(self.calls))

    def test_paused_and_continuous_consumer_eligibility(self):
        from thunder_os.continuous_consumer import ContinuousConsumer
        self.repo.put_state("agent_control", "global", {"enabled": False, "paused": True,
            "activation_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()})
        item = self.core.prepare(self.principals["dotin"], self.envelope())
        self.assertEqual(WorkStatus.NEEDS_HUMAN, item.status)
        self.assertEqual("agent_paused", self.core.get_work(self.principals["hermes"], item.id)["blocked_reason"])
        self.assertEqual("idle", self.worker().run_once()["status"])
        self.assertFalse(self.repo.get_state("agent_control", "global")["enabled"])
        self.assertEqual(0, len(self.calls))
        self.repo.put_state("agent_control", "global", {"enabled": True, "paused": False,
            "activation_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()})
        gateway = AgentGateway(self.service, self.root)
        trusted = gateway.ingest_shared(self.envelope("trusted", revision="2"), self.principals["hermes"])
        legacy = gateway.ingest({"id": "untrusted", "actor": "hermes", "capability": "research", "objective": "Pesquisa", "account_id": "acme.example"})
        consumer = ContinuousConsumer(self.service, evidence_loader=lambda job: {},
            transport_factory=lambda job, evidence, cancel: self.generate)
        with patch("thunder_os.continuous_consumer.enforce_external_gate"):
            entry = consumer.worker.repository.claim_outbox("fixture", datetime.now(timezone.utc), timedelta(minutes=1), kind="agent_job")
        self.assertEqual(trusted.id, entry["entity_id"])
        self.assertNotEqual(legacy.id, entry["entity_id"])
        self.assertEqual(0, len(self.calls))
        with self.repo.transaction() as con:
            con.execute("UPDATE thunder_outbox SET lease_until=NULL WHERE id=?", (entry["id"],))
        with patch("thunder_os.continuous_consumer.enforce_external_gate"):
            self.assertEqual("succeeded", consumer.run_once()["status"])
            self.assertEqual("idle", consumer.run_once()["status"])
        self.assertEqual(1, len(self.calls))
        self.assertEqual(trusted.context["shared_context_hash"], self.calls[0].payload["shared_commercial_context"]["hash"])
        self.assertEqual(WorkStatus.REQUESTED, self.repo.get_work(legacy.id).status)
        self.assertEqual([], self.repo.effects())

    def test_continuous_context_drift_is_visible_and_finalized(self):
        from thunder_os.continuous_consumer import ContinuousConsumer
        item = self.core.prepare(self.principals["hermes"], self.envelope())
        path = self.root / "SISTEMA.md"
        path.write_text(path.read_text() + "\nChanged after ingress", encoding="utf-8")
        before = self.core.get_work(self.principals["dotin"], item.id)
        self.assertEqual("context_changed", before["blocked_reason"])
        consumer = ContinuousConsumer(self.service, evidence_loader=lambda job: {},
            transport_factory=lambda job, evidence, cancel: self.generate)
        with patch("thunder_os.continuous_consumer.enforce_external_gate"):
            self.assertEqual("needs_human", consumer.run_once()["status"])
        state = self.core.get_work(self.principals["dotin"], item.id)
        self.assertEqual("needs_human", state["status"])
        self.assertEqual("context_changed", state["blocked_reason"])
        self.assertEqual(0, len(self.calls))

    def test_guarded_runner_rechecks_stops_owner_history_context_after_collection(self):
        from thunder_os.continuous_consumer import ContinuousConsumer
        pristine = copy.deepcopy(self.snapshot)
        for index, expected in enumerate(("reply", "other_owner", "history_incomplete", "context_changed")):
            self.snapshot = copy.deepcopy(pristine)
            self.core.prepare(self.principals["hermes"], self.envelope(f"collection-{index}", revision=str(index)))
            def evidence_loader(job):
                if expected == "reply": self.snapshot["stop_reason"] = "reply"
                if expected == "other_owner": self.snapshot["owner"] = "other"
                if expected == "history_incomplete": self.snapshot["history"]["gmail"]["complete"] = False
                if expected == "context_changed":
                    path = self.root / "SISTEMA.md"
                    path.write_text(path.read_text() + "\nChanged during collection", encoding="utf-8")
                return {}
            consumer = ContinuousConsumer(self.service, evidence_loader=evidence_loader,
                transport_factory=lambda job, evidence, cancel: self.generate)
            with patch("thunder_os.continuous_consumer.enforce_external_gate"):
                result = consumer.run_once()
            self.assertEqual("needs_human", result["status"])
            self.assertEqual(expected, self.repo.get_work(result["item_id"]).result["dependency_failure"])
            self.assertEqual(0, len(self.calls))
        self.assertEqual([], self.repo.effects())

    def test_handoff_rejects_actor_and_entry_directory_symlinks(self):
        (self.root / "_entrada").mkdir()
        private = self.root / "private-profile"
        private.mkdir()
        actor = self.root / "_entrada/dotin"
        actor.symlink_to(private, target_is_directory=True)
        target = "_entrada/dotin/shared-work-" + "1" * 32 + ".json"
        with self.assertRaises(KnowledgeError): self.store.write_selected_handoff(target, {"fixture": True})
        self.assertEqual([], list(private.iterdir()))
        actor.unlink()
        (self.root / "_entrada").rmdir()
        (self.root / "_entrada").symlink_to(private, target_is_directory=True)
        with self.assertRaises(KnowledgeError): self.store.write_selected_handoff(target, {"fixture": True})
        self.assertEqual([], list(private.iterdir()))


if __name__ == "__main__":
    unittest.main()
