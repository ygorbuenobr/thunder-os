import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from thunder_os.connectsafely_application import ConnectSafelyApplication
from thunder_os.connectsafely_console import ConnectSafelyError
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

    def campaign_gate(self):
        from thunder_os.operation_campaign import OperationCampaign
        return OperationCampaign(self.repo, 'connectsafely', 'connectsafely', self.account_id,
                                 'linkedin', clock=self.clock, sheet_store=self.campaign_sheet_store)

    def campaign_review(self, body, actor):
        assert actor == 'ygor'
        return self.campaign_gate().review(body['preview_id'], body['campaign_id'])

    def execute(self, review, actor, *, dispatch_guard, pre_dispatch_check=None):
        assert actor == "ygor"
        if pre_dispatch_check is not None:
            pre_dispatch_check()
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
        "id": ident, "hubspot_contact_id": "101", "cadence_id": "316669709",
        "expires_at": "2026-10-20T15:00:00+00:00",
        "steps": [{"marker": marker, "subject": contract[0], "plan": plan(marker)}
                  for marker, contract in STEP_CONTRACT.items()],
    }


def task(marker="cad_li_1"):
    return {"id": "task-1", "status": "NOT_STARTED", "due_at": "2026-10-19T14:00:00+00:00",
            "subject": STEP_CONTRACT[marker][0], "body": marker, "contact_ids": ["101"],
            "contact_status": "em_cadencia", "cadence_id": "316669709"}


class HermesLinkedInCadenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self.temp.name) / "state.sqlite")
        self.repo.put_state("agent_control", "global", {"enabled": True, "paused": False})
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
        stored = self.runner.approve(value or approval(), "ygor")
        self.bind_task(self.runner, stored)
        return stored

    def bind_task(self, runner, approved):
        runner.identity_verifier = lambda contact, plan: {
            'contact': {'id': contact, 'linkedin_url': 'https://www.linkedin.com/in/' + plan['recipient_public_id'],
                        'name': 'Synthetic Lead', 'company': 'Synthetic Company'},
            'profile': {'account_id': plan['account_id'], 'profile_urn': plan['recipient_profile_urn'],
                        'linkedin_url': 'https://www.linkedin.com/in/' + plan['recipient_public_id'],
                        'name': 'Synthetic Lead', 'company': 'Synthetic Company'}}
        task_value = runner.tasks.task
        runner.repository.put_state('linkedin_cadence_task_binding', task_value['id'], {
            'task_id': task_value['id'], 'approval_id': approved['id'],
            'approval_version': approved['version'], 'review_hash': approved['review_hash'],
            'marker': task_value['body'], 'contact_id': approved['hubspot_contact_id'],
            'cadence_id': approved['cadence_id'], 'actor': 'ygor'})

    def test_approval_rejects_different_profile_between_steps(self):
        value = approval('mixed')
        value['steps'][1]['plan']['recipient_public_id'] = 'different-profile'
        with self.assertRaisesRegex(CadenceBlocked, 'cadence_identity_mismatch'):
            self.runner.approve(value, 'ygor')

    def test_identity_evidence_missing_or_wrong_fails_closed(self):
        self.approve()
        for verifier in (None, lambda _contact, _plan: {'verified': True, 'source': 'unrelated'}):
            self.runner.identity_verifier = verifier
            with self.assertRaisesRegex(CadenceBlocked, 'contact_profile_identity_unverified'):
                self.runner.run_task('task-1')
        self.assertEqual(0, self.console.executes)

    def configure_suppression(self, failing=None, app=None):
        from thunder_os.delegated_suppression import ContactScope, DelegatedSuppression
        app = app or self.app
        def reader(source):
            def read(target):
                if source == failing:
                    raise RuntimeError('synthetic read failure')
                return {'target': target, 'complete': True, 'suppressed': False}
            return read
        app.delegated_contact_scope = lambda job, preview: ContactScope(
            '101', preview['plan']['recipient_profile_urn'], 'ygor@example.test', 'apollo')
        app.delegated_suppression = DelegatedSuppression(linkedin=reader('linkedin'),
            email_readers={'apollo': reader('apollo')}, hubspot=reader('hubspot'))

    def test_delegated_suppression_read_failure_prevents_provider_send(self):
        self.approve(); self.configure_suppression(failing='apollo')
        result = self.execute_with_human_confirmation(self.runner, self.app, self.console, self.temp.name)
        self.assertNotEqual('completed', result['status'])
        self.assertEqual(0, self.console.executes)
        self.assertEqual([], self.tasks.completions)

    def test_delegated_suppression_clean_fixture_allows_simulated_send(self):
        self.approve(); self.configure_suppression()
        result = self.execute_with_human_confirmation(self.runner, self.app, self.console, self.temp.name)
        self.assertEqual('completed', result['status'])
        self.assertEqual(1, self.console.executes)

    def execute_with_human_confirmation(self, runner, app, console, root):
        from test_campaign_sheet_support import make_sheet
        from thunder_os.campaign_registry import CampaignRegistry
        if app.delegated_suppression is None:
            self.configure_suppression(app=app)
        store, sheet = make_sheet(Path(root) / 'sheet')
        console.campaign_sheet_store = store
        if not app.repo.get_state('campaign_config', 'fixture-cadence'):
            CampaignRegistry(app.repo).register({
                'campaign_id': 'fixture-cadence', 'campaign_version': '1', 'rules_version': '1',
                'run_id': 'fixture-run', 'platform': 'thunder_os',
                'providers': {'linkedin': 'connectsafely'}, 'rules': 'Synthetic fixtures only',
                'briefing': sheet,
                'configuration': {'flow_version': '1', 'provider_accounts': {'linkedin': 'cs-account'}},
            }, 'ygor', 0)
        preview = runner.run_task('task-1')
        confirmation = app.campaign_review({'preview_id': preview['preview_id'],
                                            'campaign_id': 'fixture-cadence'}, 'ygor')
        approved = app.approve_delegated_campaign({
            'preview_id': preview['preview_id'], 'expected_version': preview['expected_version'],
            'review_hash': preview['review_hash'], 'execution_confirmation': confirmation}, 'ygor')
        return runner.run_task('task-1', preview_id=preview['preview_id'],
                               campaign_approval_id=approved['campaign_approval_id'])

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

    def test_d0_alone_cannot_create_delegated_job_or_reserve_quota(self):
        self.approve()
        with patch.object(self.repo, "reserve_delegated_action", wraps=self.repo.reserve_delegated_action) as reserve:
            prepared = self.runner.run_task("task-1")
            self.assertEqual("human_confirmation_required", prepared["status"])
            binding = self.repo.get_state('linkedin_cadence_preview_binding', prepared['preview_id'])
            self.assertEqual(binding['identity_hash'], prepared['identity']['observation_hash'])
            self.assertEqual(binding['identity'], prepared['identity'])
            self.assertEqual(set(binding['identity']['contact']), {'id', 'linkedin_url', 'name', 'company'})
            self.assertEqual(set(binding['identity']['profile']), {'account_id', 'profile_urn', 'linkedin_url', 'name', 'company'})
            with self.assertRaisesRegex(CadenceBlocked, "campaign_confirmation_required"):
                self.runner.run_task("task-1", preview_id=prepared["preview_id"])
        reserve.assert_not_called()
        self.assertEqual([], self.repo.list_state("connectsafely_job"))
        self.assertEqual([], self.repo.list_state("connectsafely_queue_approval"))
        self.assertEqual([], self.tasks.completions)
        self.assertEqual(0, self.console.executes)
        self.assertEqual("NOT_STARTED", self.tasks.task["status"])

    def test_changed_task_cannot_resume_approved_preview(self):
        self.approve()
        prepared = self.runner.run_task("task-1")
        self.tasks.task["due_at"] = "2026-10-19T13:00:00+00:00"
        with patch.object(self.app, "enqueue_delegated") as enqueue:
            with self.assertRaisesRegex(CadenceBlocked, "task_preview_binding_changed"):
                self.runner.run_task("task-1", preview_id=prepared["preview_id"],
                                     campaign_approval_id="human-approval")
        enqueue.assert_not_called()

    def test_agent_pause_or_missing_control_prevents_preview(self):
        self.approve()
        for control in ({"enabled": True, "paused": True}, {"enabled": False, "paused": False}, {}):
            with self.subTest(control=control):
                self.repo.put_state("agent_control", "global", control)
                with self.assertRaisesRegex(ConnectSafelyError, "agent_paused"):
                    self.runner.run_task("task-1")
        self.assertEqual(0, self.console.executes)

    def test_dispatch_guard_rechecks_pause_and_task(self):
        self.approve()
        prepared = self.runner.run_task("task-1")
        binding = self.repo.get_state("linkedin_cadence_preview_binding", prepared["preview_id"])
        job = {"preview_id": prepared["preview_id"], "delegated_authority": binding["authority"]}
        self.assertTrue(self.runner._dispatch_task_guard(job))
        self.tasks.task["contact_status"] = "respondeu"
        with self.assertRaisesRegex(CadenceBlocked, "cadence_status_blocked"):
            self.runner._dispatch_task_guard(job)
        self.tasks.task["contact_status"] = "em_cadencia"
        self.repo.put_state("agent_control", "global", {"enabled": True, "paused": True})
        with self.assertRaisesRegex(ConnectSafelyError, "agent_paused"):
            self.runner._dispatch_task_guard(job)
        self.assertEqual(0, self.console.executes)

    def test_task_reply_or_crm_failure_after_queue_prevents_provider(self):
        for scenario in ('reply', 'crm_failure'):
            with self.subTest(scenario=scenario):
                with tempfile.TemporaryDirectory() as root:
                    repo = Repository(Path(root) / 'state.sqlite')
                    repo.put_state('agent_control', 'global', {'enabled': True, 'paused': False})
                    repo.put_state('delegation', 'hermes', {'enabled': True, 'scopes': ['linkedin_cadence'],
                        'daily_limit': 30, 'actor': 'ygor', 'audit_actor': 'hermes (delegado)'})
                    tasks = Tasks(task()); provider = Provider(); console = Console(repo, provider, lambda: NOW)
                    app = ConnectSafelyApplication(repo, direct_console=console, provider=provider,
                        execution_enabled=True, readiness=lambda: {'ready': True},
                        action_readiness=lambda _a: {'ready': True}, execution_scope='hermes-test', clock=lambda: NOW)
                    runner = HermesLinkedInCadence(repo, tasks, app, clock=lambda: NOW)
                    self.bind_task(runner, runner.approve(approval(), 'ygor'))
                    enqueue = app.enqueue_delegated
                    def enqueue_then_change(body, authority):
                        queued = enqueue(body, authority)
                        if scenario == 'reply': tasks.task['contact_status'] = 'respondeu'
                        else:
                            def fail(_task_id): raise RuntimeError('CRM unavailable')
                            tasks.get = fail
                        return queued
                    app.enqueue_delegated = enqueue_then_change
                    self.execute_with_human_confirmation(runner, app, console, root)
                    self.assertEqual(0, console.executes)
                    self.assertEqual([], tasks.completions)

    def test_authority_gates_have_zero_external_writes(self):
        self.approve()
        cases = ("paused", "disabled", "out_of_scope", "cap")
        for case in cases:
            with self.subTest(case=case):
                temp = tempfile.TemporaryDirectory(); repo = Repository(Path(temp.name) / "state.sqlite")
                repo.put_state("agent_control", "global", {"enabled": True, "paused": False})
                scopes = [] if case == "out_of_scope" else ["linkedin_cadence"]
                repo.put_state("delegation", "hermes", {"enabled": case != "disabled", "scopes": scopes,
                               "daily_limit": 1, "actor": "ygor", "audit_actor": "hermes (delegado)"})
                tasks = Tasks(task()); provider = Provider(); console = Console(repo, provider, lambda: NOW)
                app = ConnectSafelyApplication(repo, direct_console=console, provider=provider,
                    execution_enabled=True, readiness=lambda: {"ready": True},
                    action_readiness=lambda _a: {"ready": True}, execution_scope="hermes-test", clock=lambda: NOW)
                runner = HermesLinkedInCadence(repo, tasks, app, clock=lambda: NOW)
                self.bind_task(runner, runner.approve(approval(), "ygor"))
                if case == "paused": repo.put_state("control", "global", {"paused": True})
                if case == "cap": repo.reserve_delegated_action("hermes", "linkedin_cadence", "2026-10-19")
                with self.assertRaises(Exception):
                    if case == 'cap': self.execute_with_human_confirmation(runner, app, console, temp.name)
                    else: runner.run_task("task-1")
                self.assertEqual(0, console.executes); self.assertEqual([], tasks.completions)
                temp.cleanup()

    def test_task_contract_selects_only_exact_due_step(self):
        variants = (
            {"due_at": "2026-10-19T16:00:00+00:00"}, {"status": "COMPLETED"},
            {"body": "wrong"}, {"contact_ids": ["102"]}, {"contact_status": "respondeu"},
            {"cadence_id": "other"},
        )
        for mutation in variants:
            with self.subTest(mutation=mutation):
                temp = tempfile.TemporaryDirectory(); repo = Repository(Path(temp.name) / "s.sqlite")
                repo.put_state("agent_control", "global", {"enabled": True, "paused": False})
                repo.put_state("delegation", "hermes", {"enabled": True, "scopes": ["linkedin_cadence"],
                    "daily_limit": 30, "actor": "ygor", "audit_actor": "hermes (delegado)"})
                tasks = Tasks({**task(), **mutation}); provider = Provider(); console = Console(repo, provider, lambda: NOW)
                app = ConnectSafelyApplication(repo, direct_console=console, provider=provider,
                    execution_enabled=True, readiness=lambda: {"ready": True},
                    action_readiness=lambda _a: {"ready": True}, execution_scope="hermes-test", clock=lambda: NOW)
                runner = HermesLinkedInCadence(repo, tasks, app, clock=lambda: NOW); self.bind_task(runner, runner.approve(approval(), "ygor"))
                with self.assertRaises(CadenceBlocked): runner.run_task("task-1")
                self.assertEqual(0, console.executes); self.assertEqual([], tasks.completions)
                temp.cleanup()

    def test_relationship_gate_matches_step_kind(self):
        for marker, allowed in (("cad_li_0", "NOT_CONNECTED"), ("cad_li_1", "CONNECTED")):
            for observed in ("NOT_CONNECTED", "CONNECTED", "INVITATION_SENT", "INVITATION_RECEIVED"):
                with self.subTest(marker=marker, observed=observed):
                    temp = tempfile.TemporaryDirectory(); repo = Repository(Path(temp.name) / "s.sqlite")
                    repo.put_state("agent_control", "global", {"enabled": True, "paused": False})
                    repo.put_state("delegation", "hermes", {"enabled": True, "scopes": ["linkedin_cadence"],
                        "daily_limit": 30, "actor": "ygor", "audit_actor": "hermes (delegado)"})
                    tasks = Tasks(task(marker)); provider = Provider(); provider.relationship_status = observed
                    console = Console(repo, provider, lambda: NOW)
                    app = ConnectSafelyApplication(repo, direct_console=console, provider=provider,
                        execution_enabled=True, readiness=lambda: {"ready": True},
                        action_readiness=lambda _a: {"ready": True}, execution_scope="hermes-test", clock=lambda: NOW)
                    runner = HermesLinkedInCadence(repo, tasks, app, clock=lambda: NOW); self.bind_task(runner, runner.approve(approval(), "ygor"))
                    if observed == allowed: self.execute_with_human_confirmation(runner, app, console, temp.name)
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
        result = self.execute_with_human_confirmation(self.runner, self.app, self.console, self.temp.name)
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
                repo.put_state("agent_control", "global", {"enabled": True, "paused": False})
                repo.put_state("delegation", "hermes", {"enabled": True, "scopes": ["linkedin_cadence"],
                    "daily_limit": 30, "actor": "ygor", "audit_actor": "hermes (delegado)"})
                tasks = Tasks(task()); provider = Provider(); console = Console(repo, provider, lambda: NOW); console.outcome = outcome
                app = ConnectSafelyApplication(repo, direct_console=console, provider=provider, execution_enabled=True,
                    readiness=lambda: {"ready": True}, action_readiness=lambda _a: {"ready": True},
                    execution_scope="hermes-test", clock=lambda: NOW)
                runner = HermesLinkedInCadence(repo, tasks, app, clock=lambda: NOW); self.bind_task(runner, runner.approve(approval(), "ygor"))
                first = self.execute_with_human_confirmation(runner, app, console, temp.name)
                self.assertEqual([], tasks.completions)
                if first["status"] == "uncertain":
                    self.assertEqual([("101", "em_cadencia")], tasks.uncertain)
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
        self.approve(); first = self.execute_with_human_confirmation(self.runner, self.app, self.console, self.temp.name); second = self.runner.run_task("task-1")
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
            self.execute_with_human_confirmation(self.runner, self.app, self.console, self.temp.name)
        self.assertEqual(1,self.console.executes)
        replay=self.runner.run_task("task-1")
        self.assertEqual("uncertain",replay["status"]);self.assertTrue(replay["replayed"])
        self.assertEqual(1,self.console.executes);self.assertEqual(2,len(calls))
        stored=self.repo.get_state("linkedin_cadence_task","task-1")
        self.assertTrue(stored["crm_uncertain_confirmed"]);self.assertEqual("incerto",self.tasks.task["contact_status"])


if __name__ == "__main__":
    unittest.main()
