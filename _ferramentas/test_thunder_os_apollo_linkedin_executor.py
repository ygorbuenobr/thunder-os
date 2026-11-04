import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from thunder_os.apollo_linkedin_executor import DryRunExecutor
from thunder_os.apollo_linkedin_reader import CampaignBindings
from thunder_os.db import Repository, RepositoryConflict
from thunder_os.service import ThunderService

class Apollo:
    def __init__(self, task): self.task = task
    def get(self, task_id): return self.task

class ExecutorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.patcher = patch('thunder_os.db.OFFICIAL_PANEL_DB', Path(self.tmp.name) / 'painel.db')
        self.patcher.start(); self.addCleanup(self.patcher.stop)
        (Path(self.tmp.name) / 'painel.db').touch()
        self.service = ThunderService(Repository(str(Path(self.tmp.name) / 'painel.db')))
        self.task = {'id': 'task', 'type': 'linkedin_step_connect', 'status': 'scheduled', 'emailer_campaign_id': 'campaign', 'user_id': 'owner', 'contact': {'linkedin_url': 'https://www.linkedin.com/in/person/'}}
        self.bindings = CampaignBindings([{'company_id':'company', 'team_id':'team', 'campaign_id':'campaign', 'account_id':'account', 'public_id':'sender', 'credential_ref':'credential', 'owner_ids':['owner']}])
        from types import SimpleNamespace
        credential = lambda: 'token'
        credential.credential_ref = 'credential'
        self.client = SimpleNamespace(account_id='account', expected_public_id='sender', request=SimpleNamespace(account_id='account', credential=credential))
        self.status = lambda account: {'id': account, 'available': True}
    def executor(self, task=None, status=None):
        return DryRunExecutor(self.service, Apollo(task or self.task), self.bindings, status or self.status, client=self.client)
    def prepare(self, executor): return executor.prepare(self.task, company_id='company', team_id='team')
    def test_missing_store_and_enabled_execution_refused(self):
        with self.assertRaises(ValueError): DryRunExecutor(self.service, Apollo(self.task), self.bindings, self.status, execution_enabled=True)
        self.service.repository.path = str(Path(self.tmp.name) / 'missing')
        with self.assertRaises(RepositoryConflict): self.executor()
    def test_dry_run_does_not_consume_reservation_or_send(self):
        result = self.prepare(self.executor())
        self.assertEqual(result['state'], 'dry_run')
        self.assertFalse(result['call_preview']['executable'])
        self.assertEqual(self.service.list_apollo_linkedin_reservations(), [])
        self.assertEqual(self.prepare(self.executor())['state'], 'dry_run')
    def test_changed_task_blocks_after_reservation(self):
        result = self.prepare(self.executor(dict(self.task, status='completed')))
        self.assertEqual(result['reason'], 'task_changed')
        self.assertEqual(self.service.list_apollo_linkedin_reservations(), [])
    def test_credential_must_match_client_and_account(self):
        from types import SimpleNamespace
        good = self.client
        for client in (SimpleNamespace(account_id='other', expected_public_id='sender', request=good.request),
                       SimpleNamespace(account_id='account', expected_public_id='other', request=good.request),
                       SimpleNamespace(account_id='account', expected_public_id='sender', request=None)):
            self.client = client
            self.assertEqual(self.prepare(self.executor())['reason'], 'credential_unbound')
        self.client = good
        self.assertEqual(self.prepare(self.executor())['state'], 'dry_run')

    def test_unavailable_account_does_not_reserve(self):
        result = self.prepare(self.executor(status=lambda _: None))
        self.assertEqual(result['reason'], 'default_account_unavailable')
        self.assertEqual(self.service.list_apollo_linkedin_reservations(), [])
    def test_owner_and_company_must_match_config(self):
        task = dict(self.task, user_id='other')
        result = self.executor().prepare(task, company_id='company', team_id='team')
        self.assertEqual(result['reason'], 'owner_not_configured')
        result = self.executor().prepare(self.task, company_id='other', team_id='team')
        self.assertEqual(result['reason'], 'campaign_binding_missing')

    def test_attention_wires_reader_executor_and_exposes_locks(self):
        from thunder_os.apollo_linkedin_attention import ApolloLinkedInAttention
        from unittest.mock import Mock
        task = dict(self.task, created_at='2026-11-03T10:00:00Z')
        reader = Mock()
        reader.list_open_tasks.return_value = {'tasks': [task], 'coverage': 'unknown'}
        reader.get.return_value = task
        executor = DryRunExecutor(self.service, reader, self.bindings, lambda _: None, client=self.client)
        attention = ApolloLinkedInAttention(self.service, reader, executor, company_id='company', team_id='team')
        with self.service.apollo_linkedin_account_lock('abandoned'):
            result = attention.snapshot()
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['coverage'], 'unknown')
        self.assertEqual({item['reason'] for item in result['items']},
                         {'default_account_unavailable', 'Lock de conta presente: abandoned'})
        self.assertTrue(all(item['stopped_at'] and item['next_action'] for item in result['items']))

if __name__ == '__main__': unittest.main()
