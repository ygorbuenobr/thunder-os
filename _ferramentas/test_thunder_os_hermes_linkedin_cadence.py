import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from thunder_os.connectsafely_application import ConnectSafelyApplication
from thunder_os.db import Repository, RepositoryConflict
from thunder_os.hermes_linkedin_cadence import CadenceBlocked, HermesLinkedInCadence, STEP_CONTRACT
from thunder_os.models import payload_hash


NOW = datetime(2026, 10, 19, 15, 0, tzinfo=timezone.utc).timestamp()


class Tasks:
    def __init__(self, task):
        self.task = dict(task)
        self.completions = []
        self.uncertain = []

    def get(self, task_id):
        assert task_id == self.task["id"]
        return dict(self.task)

    def complete(self, task_id, receipt, expected_task):
        assert all(self.task[key] == expected_task[key] for key in ("subject", "body", "contact_ids"))
        self.completions.append((task_id, receipt))
        self.task["status"] = "COMPLETED"

    def mark_uncertain(self, contact_id, expected_status):
        self.uncertain.append((contact_id, expected_status))
        self.task["contact_status"] = "incerto"


class Provider:
    def __init__(self):
        self.relationship_status = "CONNECTED"
        self.reads = 0
        self.after_relationship = None

    def relationship(self, _plan):
        self.reads += 1
        if self.after_relationship:self.after_relationship()
        return {"relationship_status": self.relationship_status}


class Console:
    account_id = "cs-account"
    account_public_id = "ygorbuenobr"

    def __init__(self, repo, provider, clock):
        self.repo = repo
        self.client = provider
        self.provider = provider
        self.clock = clock
        self.executes = 0
        self.outcome = {"status": "sent", "receipt_ref": "connectsafely:receipt-1", "retry_write": False}

    def _plan(self, plan):
        required = {"account_id", "account_public_id", "action", "recipient_public_id",
                    "recipient_profile_urn", "channel", "messaging_channel", "content", "conversation_urn"}
        if set(plan) != required or plan["action"] not in {"invite", "dm"}:
            raise ValueError("invalid_plan")
        return dict(plan)

    def prepare(self, body, actor):
        assert actor == "ygor"
        plan = self._plan(body)
        ident = uuid4().hex
        return self.repo.append_state("connectsafely_preview", ident, {
            "plan": plan, "review_hash": payload_hash({"plan": plan, "version": 1}),
            "expires_at": self.clock() + 300, "state": "review_required",
        })

    def read_preview(self, ident, actor):
        assert actor == "ygor"
        return self.repo.get_state("connectsafely_preview", ident)

    def execute(self, review, actor, *, dispatch_guard):
        assert actor == "ygor"
        assert dispatch_guard() is True
        self.executes += 1
        return dict(self.outcome)


def plan(marker):
    action = STEP_CONTRACT[marker][1]
    return {
        "account_id": "cs-account", "account_public_id": "ygorbuenobr", "action": action,
        "recipient_public_id": "lead-public", "recipient_profile_urn": "urn:li:fsd_profile:lead123",
        "channel": "linkedin", "messaging_channel": "linkedin_inbox",
        "content": "Convite aprovado" if action == "invite" else "Mensagem aprovada " + marker,
        "conversation_urn": None if action == "invite" else "urn:li:msg_conversation:(lead123,owner123)",
    }


def approval(ident="approval-1"):
    return {
        "id": ident, "hubspot_contact_id": "contact-1", "cadence_id": "316669709",
        "expires_at": "2026-10-20T15:00:00+00:00",
        "steps": [{"marker": marker, "subject": contract[0], "plan": plan(marker)}
                  for marker, contract in STEP_CONTRACT.items()],
    }


def task(marker="cad_li_1"):
    return {"id": "task-1", "status": "NOT_STARTED", "due_at": "2026-10-19T14:00:00+00:00",
            "subject": STEP_CONTRACT[marker][0], "body": marker, "contact_ids": ["contact-1"],
            "contact_status": "em_cadencia", "cadence_id": "316669709"}


class HermesLinkedInCadenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self.temp.name) / "state.sqlite")
        self.repo.put_state("delegation", "hermes", {"enabled": True, "scopes": ["linkedin_cadence"],
                            "daily_limit": 30, "actor": "ygor", "audit_actor": "hermes (delegado)"})
        self.tasks = Tasks(task())
        self.provider = Provider()
        self.console = Console(self.repo, self.provider, lambda: NOW)
        self.app = ConnectSafelyApplication(
            self.repo, direct_console=self.console, provider=self.provider,
            execution_enabled=True, readiness=lambda: {"ready": True},
            action_readiness=lambda _action: {"ready": True}, execution_scope="hermes-cadence-test",
            clock=lambda: NOW,
        )
        self.runner = HermesLinkedInCadence(self.repo, self.tasks, self.app, clock=lambda: NOW)

    def tearDown(self):
        self.temp.cleanup()

    def approve(self, value=None):
        return self.runner.approve(value or approval(), "ygor")

    def test_exact_d0_approval_is_immutable(self):
        stored = self.approve()
        self.assertEqual("ygor", stored["approved_by"])
        self.assertEqual(4, len(stored["steps"]))
        self.assertEqual(stored, self.approve())
        with self.assertRaisesRegex(CadenceBlocked, "human_authority_required"):
            self.runner.approve(approval("other"), "hermes")
        broken = approval("broken"); broken["steps"] = broken["steps"][:3]
        with self.assertRaisesRegex(CadenceBlocked, "invalid_cadence_steps"):
            self.runner.approve(broken, "ygor")
        changed = approval(); changed["steps"][1]["plan"]["content"] = "mudou"
        with self.assertRaisesRegex(RepositoryConflict, "another payload"):
            self.runner.approve(changed, "ygor")
        expired = approval("expired"); expired["expires_at"] = "2026-10-19T14:59:59+00:00"
        with self.assertRaisesRegex(CadenceBlocked, "approval_expired"):
            self.runner.approve(expired, "ygor")

    def test_authority_gates_have_zero_external_writes(self):
        self.approve()
        cases = ("paused", "disabled", "out_of_scope", "cap")
        for case in cases:
            with self.subTest(case=case):
                temp = tempfile.TemporaryDirectory(); repo = Repository(Path(temp.name) / "state.sqlite")
                scopes = [] if case == "out_of_scope" else ["linkedin_cadence"]
                repo.put_state("delegation", "hermes", {"enabled": case != "disabled", "scopes": scopes,
                               "daily_limit": 1, "actor": "ygor", "audit_actor": "hermes (delegado)"})
                tasks = Tasks(task()); provider = Provider(); console = Console(repo, provider, lambda: NOW)
                app = ConnectSafelyApplication(repo, direct_console=console, provider=provider,
                    execution_enabled=True, readiness=lambda: {"ready": True},
                    action_readiness=lambda _a: {"ready": True}, execution_scope="hermes-test", clock=lambda: NOW)
                runner = HermesLinkedInCadence(repo, tasks, app, clock=lambda: NOW)
                runner.approve(approval(), "ygor")
                if case == "paused": repo.put_state("control", "global", {"paused": True})
                if case == "cap": repo.reserve_delegated_action("hermes", "linkedin_cadence", "2026-10-19")
                with self.assertRaises(Exception): runner.run_task("task-1")
                self.assertEqual(0, console.executes); self.assertEqual([], tasks.completions)
                temp.cleanup()

    def test_task_contract_selects_only_exact_due_step(self):
        variants = (
            {"due_at": "2026-10-19T16:00:00+00:00"}, {"status": "COMPLETED"},
            {"body": "wrong"}, {"contact_ids": ["contact-2"]}, {"contact_status": "respondeu"},
            {"cadence_id": "other"},
        )
        for mutation in variants:
            with self.subTest(mutation=mutation):
                temp = tempfile.TemporaryDirectory(); repo = Repository(Path(temp.name) / "s.sqlite")
                repo.put_state("delegation", "hermes", {"enabled": True, "scopes": ["linkedin_cadence"],
                    "daily_limit": 30, "actor": "ygor", "audit_actor": "hermes (delegado)"})
                tasks = Tasks({**task(), **mutation}); provider = Provider(); console = Console(repo, provider, lambda: NOW)
                app = ConnectSafelyApplication(repo, direct_console=console, provider=provider,
                    execution_enabled=True, readiness=lambda: {"ready": True},
                    action_readiness=lambda _a: {"ready": True}, execution_scope="hermes-test", clock=lambda: NOW)
                runner = HermesLinkedInCadence(repo, tasks, app, clock=lambda: NOW); runner.approve(approval(), "ygor")
                with self.assertRaises(CadenceBlocked): runner.run_task("task-1")
                self.assertEqual(0, console.executes); self.assertEqual([], tasks.completions)
                temp.cleanup()

    def test_relationship_gate_matches_step_kind(self):
        for marker, allowed in (("cad_li_0", "NOT_CONNECTED"), ("cad_li_1", "CONNECTED")):
            for observed in ("NOT_CONNECTED", "CONNECTED", "INVITATION_SENT", "INVITATION_RECEIVED"):
                with self.subTest(marker=marker, observed=observed):
                    temp = tempfile.TemporaryDirectory(); repo = Repository(Path(temp.name) / "s.sqlite")
                    repo.put_state("delegation", "hermes", {"enabled": True, "scopes": ["linkedin_cadence"],
                        "daily_limit": 30, "actor": "ygor", "audit_actor": "hermes (delegado)"})
                    tasks = Tasks(task(marker)); provider = Provider(); provider.relationship_status = observed
                    console = Console(repo, provider, lambda: NOW)
                    app = ConnectSafelyApplication(repo, direct_console=console, provider=provider,
                        execution_enabled=True, readiness=lambda: {"ready": True},
                        action_readiness=lambda _a: {"ready": True}, execution_scope="hermes-test", clock=lambda: NOW)
                    runner = HermesLinkedInCadence(repo, tasks, app, clock=lambda: NOW); runner.approve(approval(), "ygor")
                    if observed == allowed: runner.run_task("task-1")
                    else:
                        with self.assertRaisesRegex(CadenceBlocked, "relationship_gate_blocked"): runner.run_task("task-1")
                    self.assertEqual(1 if observed == allowed else 0, console.executes)
                    temp.cleanup()

    def test_reply_arriving_during_relationship_read_blocks_dispatch(self):
        self.approve()
        self.provider.after_relationship=lambda:self.tasks.task.update(contact_status="respondeu")
        with self.assertRaisesRegex(CadenceBlocked,"cadence_status_blocked"):
            self.runner.run_task("task-1")
        self.assertEqual(0,self.console.executes);self.assertEqual([],self.tasks.completions)

    def test_confirmed_receipt_completes_task_and_audits_delegate(self):
        self.approve()
        result = self.runner.run_task("task-1")
        self.assertEqual("completed", result["status"])
        self.assertEqual([("task-1", "connectsafely:receipt-1")], self.tasks.completions)
        audit = self.repo.list_state("linkedin_cadence_audit")
        self.assertEqual("hermes (delegado)", audit[0]["actor"])

    def test_nonconfirmed_outcomes_stay_open_and_uncertain_is_fenced(self):
        for outcome in (
            {"status": "blocked", "error_code": "provider_rejected", "retry_write": False},
            {"status": "blocked", "error_code": "provider_denied", "retry_write": False},
            {"status": "uncertain", "error_code": "provider_outcome_unknown", "retry_write": False},
        ):
            with self.subTest(outcome=outcome):
                temp = tempfile.TemporaryDirectory(); repo = Repository(Path(temp.name) / "s.sqlite")
                repo.put_state("delegation", "hermes", {"enabled": True, "scopes": ["linkedin_cadence"],
                    "daily_limit": 30, "actor": "ygor", "audit_actor": "hermes (delegado)"})
                tasks = Tasks(task()); provider = Provider(); console = Console(repo, provider, lambda: NOW); console.outcome = outcome
                app = ConnectSafelyApplication(repo, direct_console=console, provider=provider, execution_enabled=True,
                    readiness=lambda: {"ready": True}, action_readiness=lambda _a: {"ready": True},
                    execution_scope="hermes-test", clock=lambda: NOW)
                runner = HermesLinkedInCadence(repo, tasks, app, clock=lambda: NOW); runner.approve(approval(), "ygor")
                first = runner.run_task("task-1")
                self.assertEqual([], tasks.completions)
                if first["status"] == "uncertain":
                    self.assertEqual([("contact-1", "em_cadencia")], tasks.uncertain)
                    self.assertEqual("uncertain", repo.get_state("linkedin_cadence_alert", "task-1")["status"])
                    second = runner.run_task("task-1")
                    self.assertTrue(second["replayed"]); self.assertEqual(1, console.executes)
                    prior=repo.get_state("linkedin_cadence_task","task-1")
                    repo.put_state("connectsafely_job",prior["job_id"],{
                        "state":"done","result":{"status":"sent","receipt_ref":"connectsafely:reconciled-1"}})
                    reconciled=runner.run_task("task-1")
                    self.assertEqual("completed",reconciled["status"]);self.assertEqual(1,console.executes)
                temp.cleanup()

    def test_confirmed_replay_is_idempotent(self):
        self.approve(); first = self.runner.run_task("task-1"); second = self.runner.run_task("task-1")
        self.assertEqual("completed", first["status"]); self.assertTrue(second["replayed"])
        self.assertEqual(1, self.console.executes); self.assertEqual(1, len(self.tasks.completions))
        self.assertEqual(1, self.repo.get_state("delegation_usage", "hermes:2026-10-19")["count"])

    def test_uncertain_crm_status_write_retries_without_provider_redelivery(self):
        self.approve();self.console.outcome={"status":"uncertain","error_code":"provider_outcome_unknown","retry_write":False}
        original=self.tasks.mark_uncertain;calls=[]
        def flaky(contact_id,status):
            calls.append((contact_id,status))
            if len(calls)==1:
                self.tasks.task["contact_status"]="incerto"
                raise RuntimeError("fixture crm readback unavailable")
            return original(contact_id,status)
        self.tasks.mark_uncertain=flaky
        with self.assertRaisesRegex(RuntimeError,"fixture crm readback unavailable"):
            self.runner.run_task("task-1")
        self.assertEqual(1,self.console.executes)
        replay=self.runner.run_task("task-1")
        self.assertEqual("uncertain",replay["status"]);self.assertTrue(replay["replayed"])
        self.assertEqual(1,self.console.executes);self.assertEqual(2,len(calls))
        stored=self.repo.get_state("linkedin_cadence_task","task-1")
        self.assertTrue(stored["crm_uncertain_confirmed"]);self.assertEqual("incerto",self.tasks.task["contact_status"])


if __name__ == "__main__":
    unittest.main()
