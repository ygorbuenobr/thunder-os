"""Focused local checks for the read-only connection panel projection."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import painel_servidor
from thunder_os.connection_ledger import LIVE_CHECKPOINT, profile_kind, research_request_kind
from thunder_os.connection_company import resolution_kind
from thunder_os.connection_work import WORK_LINK_KIND, enqueue_connection_research
from thunder_os.db import Repository
from thunder_os.models import ContractError
from thunder_os.models import payload_hash
from thunder_os.service import AuthorizationError, ThunderService


ACCOUNT = "connectsafely-fixture"


class ConnectionPanelTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.repo = Repository(Path(scratch.name) / "state.db")
        self.repo.put_state(LIVE_CHECKPOINT, ACCOUNT, {"connectsafely_account_id": ACCOUNT})
        self.current = {"dossier.example", "approach.example"}
        def current_research(account):
            if account not in self.current:
                raise ContractError("research stale")
            return self.repo.get_state("research", account)
        self.service = SimpleNamespace(repository=self.repo, current_research=current_research)

    def profile(self, slug, status, account=None, connected_at=None):
        url = "https://www.linkedin.com/in/" + slug
        row = {"profile": url, "connectsafely_account_id": ACCOUNT,
               "status": status, "source": "connectsafely", "private_note": "SECRET_BRAIN_BODY"}
        if account:
            row["account_id"] = account
        if connected_at:
            row["connected_at"] = connected_at
        self.repo.put_state(profile_kind(ACCOUNT), url, row)
        if status == "research" and account:
            self.repo.put_state(research_request_kind(ACCOUNT), url, {
                "profile": url, "connectsafely_account_id": ACCOUNT,
                "account_id": account, "status": "pending", "source": "exact_profile_mapping",
                "fit_evidence": "fixture:fit-reference"})

    def complete_research(self, slug, account, *, artifact_version=None):
        service = ThunderService(self.repo)
        profile = "https://www.linkedin.com/in/" + slug
        work = enqueue_connection_research(service, connectsafely_account_id=ACCOUNT, profile=profile)
        claimed = service.claim(work.id, work.version, "hermes")
        research = self.repo.get_state("research", account)
        return service.record_job_result(claimed.id, claimed.version, {
            "artifact": {"account": {"domain": account}},
            "evidence": [{"source": "fixture:research"}],
            "artifact_ref": {"kind": "research", "id": account,
                             "version": research["version"] if artifact_version is None else artifact_version},
        }, "hermes")

    def put_research(self, account, **overrides):
        payload = {
            "account": {"domain": account}, "status": "validated",
            "recommendation": "abordar", "capability_ids": ["c1"], "proof_ids": ["p1"],
            "knowledge_source": "fixture:catalog",
            "facts": [{"source": "fixture:research", "published_at": "2026-10-04",
                       "observed_at": "2026-10-05"}],
            **overrides,
        }
        stored = self.repo.put_state("research", account, payload)
        self.repo.append_state("research_history", f"{account}:v{stored['version']}", stored)
        return stored

    def route(self, actor="ygor", method="GET", path="/api/os/v1/connections", body=None, banco=None,
              agenttrail_ingress=None):
        handler = object.__new__(painel_servidor.Handler)
        handler.server = SimpleNamespace(thunder_service=self.service, banco=banco)
        handler.command = method
        handler.client_address = ("127.0.0.1", 1234)
        handler.headers = {"X-Authenticated-User": actor}
        if agenttrail_ingress is not None:
            handler.headers["X-Thunder-Agenttrail-Ingress"] = agenttrail_ingress
        handler.responder = lambda status, body: (status, body)
        if method != "GET":
            handler.thunder_actor = lambda: actor
            handler.corpo = lambda: {} if body is None else body
        return handler.thunder_api(urlsplit(path))

    def test_agenttrail_is_read_only_and_ygor_only(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            painel_servidor, "AGENTTRAIL_INGRESS_TOKEN", Path(tmp) / "token"
        ), patch("thunder_os.agenttrail_observer.panel_state", return_value={"hosts": {}}):
            (Path(tmp) / "token").write_text("test-only-ingress")
            self.assertEqual(self.route(path="/api/os/v1/agenttrail", agenttrail_ingress="test-only-ingress"),
                             (200, {"hosts": {}}))
            with self.assertRaises(painel_servidor.Erro):
                self.route(path="/api/os/v1/agenttrail")
            with self.assertRaises(painel_servidor.Erro):
                self.route(path="/api/os/v1/agenttrail", agenttrail_ingress="forged")
            with self.assertRaises(AuthorizationError):
                self.route(actor="claude", path="/api/os/v1/agenttrail", agenttrail_ingress="test-only-ingress")
            with self.assertRaises(painel_servidor.Erro):
                self.route(path="/api/os/v1/agenttrail?extra=1", agenttrail_ingress="test-only-ingress")

    def test_states_and_approval_gate(self):
        self.profile("identity", "needs_identity")
        self.profile("fit", "needs_fit", "fit.example")
        self.profile("research", "research", "research.example")
        self.profile("dossier", "research", "dossier.example")
        self.profile("approach", "research", "approach.example")
        self.profile("contacted", "already_contacted", "contacted.example")
        self.profile("historical", "historical", "historical.example")
        self.profile("mystery", "unexpected", "unknown.example")
        for account in ("dossier.example", "approach.example"):
            self.put_research(account, raw_facts="SECRET_BRAIN_BODY")
        self.repo.put_state("copy", "approach.example", {
            "status": "ready_for_review", "capability_ids": ["c1"],
            "proof_ids": ["p1"], "final_copy": "SECRET_PRIVATE_COPY"})
        self.complete_research("dossier", "dossier.example")
        self.complete_research("approach", "approach.example")
        status, body = self.route()
        self.assertEqual(200, status)
        stages = {item["profile"].rsplit("/", 1)[-1]: item["stage"]
                  for group in body["accounts"] for item in group["connections"]}
        self.assertEqual({"identity": "needs_identity", "fit": "needs_fit",
                          "research": "research", "dossier": "dossier_review",
                          "approach": "dossier_review", "mystery": "unknown"}, stages)
        self.assertEqual(1, body["counts"]["already_contacted"])
        self.assertEqual(1, body["counts"]["no_action"])
        historical = self.route(path="/api/os/v1/connections?view=history")[1]
        self.assertEqual({"contacted": "already_contacted", "historical": "no_action"},
                         {item["profile"].rsplit("/", 1)[-1]: item["stage"]
                          for group in historical["accounts"] for item in group["connections"]})
        self.assertNotIn("approach_approval", stages.values())
        self.assertNotIn("SECRET_", json.dumps(body))
        self.current.remove("approach.example")
        downgraded = self.route()[1]
        stages = {item["profile"].rsplit("/", 1)[-1]: item["stage"]
                  for group in downgraded["accounts"] for item in group["connections"]}
        self.assertEqual("dossier_review", stages["approach"])
        self.assertIn("incompleta", next(item["detail"] for group in downgraded["accounts"]
                          for item in group["connections"] if item["profile"].endswith("approach")))
        self.current.add("approach.example")
        self.repo.put_state("copy", "approach.example", {
            "status": "ready_for_review", "capability_ids": ["stale-capability"],
            "proof_ids": ["p1"], "final_copy": "SECRET_PRIVATE_COPY"})
        stale_copy = self.route()[1]
        item = next(item for group in stale_copy["accounts"] for item in group["connections"]
                    if item["profile"].endswith("approach"))
        self.assertEqual("dossier_review", item["stage"])

    def test_company_candidate_is_visible_but_not_approval_ready(self):
        self.profile("identity", "needs_identity")
        profile = "https://www.linkedin.com/in/identity"
        self.repo.put_state(resolution_kind(ACCOUNT), profile, {
            "profile": profile, "connectsafely_account_id": ACCOUNT,
            "status": "candidate", "company_name": "alpha logistics",
            "candidate_domain": "alpha.example", "catalog_ref": "fixture:source",
        })
        status, body = self.route()
        self.assertEqual(200, status)
        item = body["accounts"][0]["connections"][0]
        self.assertEqual("needs_identity", item["stage"])
        self.assertIn("alpha logistics", item["detail"])
        self.assertIn("confirme", item["detail"])
        self.assertIsNone(item["account_id"])

    def test_same_domain_requires_exact_successful_profile_work(self):
        for slug in ("finished", "pending", "stale-result"):
            self.profile(slug, "research", "approach.example")
        self.put_research("approach.example", private_dossier="SECRET_BRAIN_BODY")
        self.repo.put_state("copy", "approach.example", {
            "status": "ready_for_review", "capability_ids": ["c1"],
            "proof_ids": ["p1"], "final_copy": "SECRET_PRIVATE_COPY"})
        self.complete_research("finished", "approach.example")
        enqueue_connection_research(ThunderService(self.repo), connectsafely_account_id=ACCOUNT,
                                    profile="https://www.linkedin.com/in/pending")
        self.complete_research("stale-result", "approach.example", artifact_version=0)
        body = self.route()[1]
        stages = {item["profile"].rsplit("/", 1)[-1]: item["stage"]
                  for group in body["accounts"] for item in group["connections"]}
        self.assertEqual("dossier_review", stages["finished"])
        self.assertEqual("research", stages["pending"])
        self.assertEqual("research", stages["stale-result"])
        self.assertNotIn("SECRET_", json.dumps(body))
        self.assertEqual([], self.repo.effects())

    def test_domain_copy_without_profile_target_never_promotes_connections(self):
        for slug in ("person-one", "person-two"):
            self.profile(slug, "research", "shared.example")
        self.put_research("shared.example")
        self.current.add("shared.example")
        self.repo.put_state("copy", "shared.example", {
            "account_id": "shared.example", "status": "ready_for_review",
            "capability_ids": ["c1"], "proof_ids": ["p1"],
            "final_copy": "SECRET_UNTARGETED_COPY"})
        self.complete_research("person-one", "shared.example")
        self.complete_research("person-two", "shared.example")
        body = self.route()[1]
        stages = {item["profile"].rsplit("/", 1)[-1]: item["stage"]
                  for group in body["accounts"] for item in group["connections"]}
        self.assertEqual({"person-one": "dossier_review", "person-two": "dossier_review"}, stages)
        self.assertNotIn("approach_approval", stages.values())
        self.assertNotIn("SECRET_", json.dumps(body))
        self.assertEqual([], self.repo.effects())

    def test_only_current_profile_targeted_cadence_reaches_approach_approval(self):
        for slug in ("person-one", "person-two"):
            self.profile(slug, "research", "shared.example")
        self.current.add("shared.example")
        research = self.put_research("shared.example")
        self.complete_research("person-one", "shared.example")
        self.complete_research("person-two", "shared.example")
        profile = "https://www.linkedin.com/in/person-one"
        cadence_service = ThunderService(self.repo)
        cadence_service.current_research = lambda account: self.repo.get_state("research", account)
        cadence = cadence_service.create_cadence({
            "id": "cadence-person-one", "account_id": "shared.example",
            "person_key": profile, "objective": "Review approach",
            "steps": [{"id": "step-one", "recipient": profile, "channel": "linkedin",
                       "content": "Draft only", "order": 1, "interval_hours": 0}],
        }, "ygor")
        stages = {item["profile"].rsplit("/", 1)[-1]: item["stage"]
                  for group in self.route()[1]["accounts"] for item in group["connections"]}
        self.assertEqual("approach_approval", stages["person-one"])
        self.assertEqual("dossier_review", stages["person-two"])
        self.repo.put_state("cadence", cadence["id"], {
            **cadence, "research_hash": payload_hash({"version": research["version"] + 1,
                                                        "facts": research["facts"]})})
        stages = {item["profile"].rsplit("/", 1)[-1]: item["stage"]
                  for group in self.route()[1]["accounts"] for item in group["connections"]}
        self.assertEqual("dossier_review", stages["person-one"])
        self.assertEqual([], self.repo.effects())

    def test_later_research_for_same_account_keeps_earlier_profile_dossier(self):
        for slug in ("first", "second"):
            self.profile(slug, "research", "shared.example")
        self.current.add("shared.example")
        self.put_research("shared.example", private_dossier="SECRET_FIRST_DOSSIER")
        self.complete_research("first", "shared.example")
        self.put_research("shared.example", private_dossier="SECRET_SECOND_DOSSIER")
        self.complete_research("second", "shared.example")
        body = self.route()[1]
        entries = {item["profile"].rsplit("/", 1)[-1]: item
                   for group in body["accounts"] for item in group["connections"]}
        self.assertEqual("dossier_review", entries["first"]["stage"])
        self.assertIn("atualizada depois", entries["first"]["detail"])
        self.assertEqual("dossier_review", entries["second"]["stage"])
        self.assertNotIn("atualizada depois", entries["second"]["detail"])
        self.assertNotIn("SECRET_", json.dumps(body))
        self.assertEqual([], self.repo.effects())

    def test_mismatched_link_or_request_cannot_claim_dossier(self):
        for slug, account in (("wrong-link", "link.example"), ("wrong-request", "request.example")):
            self.profile(slug, "research", account)
            self.put_research(account, capability_ids=[], proof_ids=[])
        linked = self.complete_research("wrong-link", "link.example")
        self.complete_research("wrong-request", "request.example")
        self.repo.put_state(WORK_LINK_KIND, linked.id, {
            "connectsafely_account_id": ACCOUNT,
            "profile": "https://www.linkedin.com/in/another-profile", "request_version": 1})
        request_url = "https://www.linkedin.com/in/wrong-request"
        self.repo.put_state(research_request_kind(ACCOUNT), request_url, {
            "profile": request_url, "connectsafely_account_id": ACCOUNT,
            "account_id": "other.example", "status": "pending",
            "source": "exact_profile_mapping", "fit_evidence": "fixture:fit-reference"})
        stages = {item["profile"].rsplit("/", 1)[-1]: item["stage"]
                  for group in self.route()[1]["accounts"] for item in group["connections"]}
        self.assertEqual("research", stages["wrong-link"])
        self.assertEqual("research", stages["wrong-request"])
        self.assertEqual([], self.repo.effects())

    def test_route_requires_owner_and_is_read_only(self):
        with self.assertRaises(painel_servidor.Erro):
            self.route("")
        with self.assertRaises(AuthorizationError):
            self.route("claude")
        with self.assertRaises(painel_servidor.Erro):
            self.route(method="POST")
        empty = self.route()[1]
        self.assertEqual(1, empty["schema_version"])
        self.assertEqual([], empty["accounts"])
        self.assertEqual(0, empty["total"])
        self.assertFalse(empty["has_more"])
        for path in ("/api/os/v1/connections?limit=101", "/api/os/v1/connections?offset=-1",
                     "/api/os/v1/connections?view=private", "/api/os/v1/connections?limit=1&limit=2"):
            with self.subTest(path=path), self.assertRaises(painel_servidor.Erro):
                self.route(path=path)

    def test_recent_action_page_is_bounded_and_history_remains_accessible(self):
        start = datetime(2026, 10, 5, tzinfo=timezone.utc)
        for index in range(105):
            self.profile(f"person-{index:03}", "needs_identity",
                         connected_at=(start + timedelta(seconds=index)).isoformat())
        for index in range(7):
            self.profile(f"historical-{index}", "historical",
                         connected_at=(start + timedelta(days=1, seconds=index)).isoformat())
        self.profile("contacted", "already_contacted",
                     connected_at=(start + timedelta(days=2)).isoformat())
        first = self.route()[1]
        first_items = [item for group in first["accounts"] for item in group["connections"]]
        self.assertEqual(100, len(first_items))
        self.assertEqual(105, first["total"])
        self.assertEqual(105, first["counts"]["needs_identity"])
        self.assertEqual(7, first["counts"]["no_action"])
        self.assertEqual(1, first["counts"]["already_contacted"])
        self.assertEqual("https://www.linkedin.com/in/person-104", first_items[0]["profile"])
        self.assertEqual("https://www.linkedin.com/in/person-005", first_items[-1]["profile"])
        self.assertTrue(first["has_more"])
        self.assertEqual(100, first["next_offset"])
        self.assertTrue(all(item["stage"] == "needs_identity" for item in first_items))
        second = self.route(path="/api/os/v1/connections?offset=100")[1]
        second_items = [item for group in second["accounts"] for item in group["connections"]]
        self.assertEqual(5, len(second_items))
        self.assertEqual("https://www.linkedin.com/in/person-004", second_items[0]["profile"])
        self.assertFalse(second["has_more"])
        history = self.route(path="/api/os/v1/connections?view=history&limit=3")[1]
        self.assertEqual(8, history["total"])
        self.assertEqual(3, sum(len(group["connections"]) for group in history["accounts"]))
        self.assertTrue(history["has_more"])
        self.assertEqual("already_contacted", history["accounts"][0]["connections"][0]["stage"])
        later_history = self.route(path="/api/os/v1/connections?view=history&offset=3&limit=3")[1]
        self.assertEqual(8, later_history["total"])
        self.assertEqual(3, sum(len(group["connections"]) for group in later_history["accounts"]))
        self.assertEqual(6, later_history["next_offset"])
        self.assertEqual([], self.repo.effects())

    def test_manual_reconcile_keeps_old_cards_without_legacy_materialization(self):
        class BancoFixture:
            def __init__(self):
                self.cards = list(range(41))
                self.materialized = 0
                self.reconciled = 0

            def materializar_aceites(self):
                self.materialized += 1
                self.cards.append("new-dm")

            def listar(self, name, *, reconciliar=False):
                if name != "itens" or reconciliar is not True:
                    raise AssertionError("existing cards must be reconciled")
                self.reconciled += 1
                return list(self.cards)

        banco = BancoFixture()
        route = "/api/os/v1/control/reconcile"
        with (patch.object(painel_servidor, "external_effects_enabled", return_value=True),
              patch.object(painel_servidor, "restricted_mode", return_value=False),
              patch.dict(painel_servidor.os.environ, {"THUNDER_OS_COPILOT_MODE": ""})):
            painel_servidor.os.environ.pop("THUNDER_OS_LEGACY_ACEITES_MATERIALIZATION_ENABLED", None)
            status, result = self.route(method="POST", path=route,
                                        body={"confirm": True}, banco=banco)
            self.assertEqual((200, {"status": "reconciled",
                                    "legacy_aceites_materialization": "skipped"}), (status, result))
            self.assertEqual(41, len(banco.cards))
            self.assertEqual(0, banco.materialized)
            self.assertEqual(1, banco.reconciled)
            with patch.dict(painel_servidor.os.environ, {
                    "THUNDER_OS_LEGACY_ACEITES_MATERIALIZATION_ENABLED": "true"}):
                status, result = self.route(method="POST", path=route,
                                            body={"confirm": True}, banco=banco)
            self.assertEqual("executed", result["legacy_aceites_materialization"])
            self.assertEqual(1, banco.materialized)
            self.assertEqual(2, banco.reconciled)
        self.assertEqual([], self.repo.effects())

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable for UI projection check")
    def test_key_ui_states_and_text_safety(self):
        html = (Path(__file__).resolve().parent / "painel_modelo.html").read_text(encoding="utf-8")
        self.assertIn('id="conexoes-status" role="status"', html)
        self.assertIn('id="conexoes-refresh"', html)
        self.assertIn("apiServidor('/api/os/v1/connections')", html)
        script = r'''
const fs=require('fs'),vm=require('vm'),html=fs.readFileSync(process.argv[1],'utf8');
const start=html.indexOf('const ThunderConnectionsView=');
const end=html.indexOf('let conexoesLoading=false;',start);
if(start<0||end<0)throw Error('connection view missing');
class Element{constructor(tag,doc){this.tag=tag;this.ownerDocument=doc;this.children=[];this.textContent='';}
appendChild(child){this.children.push(child);return child;}replaceChildren(...children){this.children=children;}
setAttribute(){}set href(value){this.url=value;}set target(value){}set rel(value){}}
const doc={createElement(tag){return new Element(tag,doc)},createDocumentFragment(){return new Element('fragment',doc)}};
const root=new Element('div',doc);
const render=vm.runInNewContext(html.slice(start,end)+';ThunderConnectionsView.render');
render(root,{schema_version:1,accounts:[{account_id:'example.com',connections:[
{profile:'https://www.linkedin.com/in/person',connectsafely_account_id:'fixture',stage:'dossier_review',detail:'<script>unsafe</script>'}]}]});
function all(node){return [node,...node.children.flatMap(all)];}
const nodes=all(root);
if(!nodes.some(n=>n.textContent==='Revisar dossiê'))throw Error('stage missing');
if(!nodes.some(n=>n.textContent==='<script>unsafe</script>'))throw Error('detail missing');
if(nodes.some(n=>n.tag==='script'||n.tag==='button'))throw Error('unexpected executable or action node');
render(root,{schema_version:1,accounts:[]});
if(!all(root).some(n=>n.textContent.includes('Nenhuma conexão canônica')))throw Error('empty state missing');
'''
        result = subprocess.run(["node", "-e", script, str(Path(__file__).resolve().parent / "painel_modelo.html")],
                                capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
