import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from thunder_os.apollo_linkedin_completion import ApolloLinkedInCompletion, _request
from thunder_os.apollo_linkedin_attention import EXCLUDED_TASK
from thunder_os.db import Repository


def raw(data):
    return json.dumps(data).encode()


class FakeApollo:
    def __init__(self):
        self.calls = []
        self.status = 'scheduled'
        self.error = False

    def __call__(self, method, path, body, headers, timeout):
        self.calls.append((method, path, body))
        if self.error:
            raise TimeoutError('synthetic timeout')
        if path == '/api/v1/users/api_profile':
            return 200, raw({'user': {'id': 'actor', 'team_id': 'team'}})
        return 200, raw({'task': {'id': 'task1', 'status': self.status,
                                  'emailer_campaign_id': 'wrong-sequence', 'user_id': 'wrong-owner'}})


def completion_contender(path, gate, result_queue):
    with patch('thunder_os.db.OFFICIAL_PANEL_DB', Path(path)), \
         patch.dict(os.environ, {'THUNDER_APOLLO_LI_EXECUTION_ENABLED': 'true'}):
        class SharedFake(FakeApollo):
            def __call__(self, method, target, body, headers, timeout):
                result_queue.put(method)
                return super().__call__(method, target, body, headers, timeout)
        repo = Repository(path)
        port = ApolloLinkedInCompletion(lambda: 'synthetic-key', team_id='team', actor_id='actor',
            repository=repo, enabled=True, request=SharedFake())
        gate.wait(5)
        result_queue.put(port.complete('company', 'task1', 'linkedin_step_connect')['reason'])


class ApolloCompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'painel.db'; self.path.touch()
        self.official = patch('thunder_os.db.OFFICIAL_PANEL_DB', self.path)
        self.official.start(); self.addCleanup(self.official.stop)
        self.env = patch.dict(os.environ, {'THUNDER_APOLLO_LI_EXECUTION_ENABLED': 'true'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.repo = Repository(self.path)
        self.repo.put_state('control', 'global', {'paused': False})
        self.repo.reserve_apollo_linkedin('company', 'task1', 'linkedin_step_connect', 'account',
            text_hash='a' * 64, campaign_version='v1', recipient='lead')
        self.repo.finish_apollo_linkedin('company', 'task1', 'linkedin_step_connect', 'confirmed',
            receipt={'provider_id': 'synthetic-receipt'}, apollo_status='pending')
        self.fake = FakeApollo(); self.load = Mock(return_value='fake-test-key')

    def port(self, enabled=False, repository=None):
        return ApolloLinkedInCompletion(self.load, team_id='team', actor_id='actor',
            repository=self.repo if repository is None else repository, enabled=enabled, request=self.fake)

    def complete(self, port, task='task1'):
        return port.complete('company', task, 'linkedin_step_connect')

    def row(self):
        return self.repo.get_apollo_linkedin_reservation('company', 'task1', 'linkedin_step_connect')

    def test_default_off_does_not_load_credentials_or_request(self):
        self.assertEqual(self.complete(self.port())['reason'], 'execution_disabled')
        self.load.assert_not_called(); self.assertEqual([], self.fake.calls)
        self.assertEqual('pending', self.row()['apollo_status'])

    def test_fixture_and_invalid_id_fail_before_provider(self):
        for task_id in (EXCLUDED_TASK, '../task', 'task?x=1'):
            with self.subTest(task_id=task_id), self.assertRaises(ValueError):
                self.complete(self.port(True), task_id)
        self.assertEqual([], self.fake.calls)

    def test_even_confirmed_receipt_cannot_authorize_post_without_canonical_binding(self):
        self.assertEqual('execution_binding_unverified', self.complete(self.port(True))['reason'])
        self.assertEqual('pending', self.row()['apollo_status'])
        self.assertEqual([], self.fake.calls); self.load.assert_not_called()

    def test_env_off_blocks_before_provider(self):
        with patch.dict(os.environ, {'THUNDER_APOLLO_LI_EXECUTION_ENABLED': 'false'}):
            self.assertEqual('execution_disabled', self.complete(self.port(True))['reason'])
        self.assertEqual([], self.fake.calls)

    def test_pause_or_unknown_control_blocks(self):
        self.repo.put_state('control', 'global', {'paused': True})
        self.assertEqual('execution_disabled', self.complete(self.port(True))['reason'])
        self.repo.put_state('control', 'global', {'paused': 'unknown'})
        self.assertEqual('execution_disabled', self.complete(self.port(True))['reason'])
        self.assertEqual([], self.fake.calls)

    def test_missing_reservation_still_cannot_post(self):
        self.assertEqual('execution_binding_unverified', self.complete(self.port(True), 'unknown')['reason'])
        self.assertEqual([], self.fake.calls)

    def test_two_processes_cannot_make_completion_attempt(self):
        ctx = multiprocessing.get_context('spawn'); gate, q = ctx.Event(), ctx.Queue()
        workers = [ctx.Process(target=completion_contender, args=(str(self.path), gate, q)) for _ in range(2)]
        for worker in workers: worker.start()
        gate.set()
        for worker in workers:
            worker.join(10); self.assertEqual(0, worker.exitcode)
        self.assertEqual(['execution_binding_unverified'] * 2, sorted(q.get(timeout=5) for _ in range(2)))
        self.assertTrue(q.empty()); self.assertEqual('pending', self.row()['apollo_status'])

    def test_two_instances_and_restarts_cannot_bypass_block(self):
        self.assertEqual('execution_binding_unverified', self.complete(self.port(True))['reason'])
        self.assertEqual('execution_binding_unverified', self.complete(self.port(True, Repository(self.path)))['reason'])
        self.assertEqual([], self.fake.calls); self.assertEqual('pending', self.row()['apollo_status'])

    def test_reconciliation_is_read_only_against_apollo(self):
        self.repo.claim_apollo_linkedin_completion('company', 'task1', 'linkedin_step_connect')
        self.fake.status = 'completed'
        result = self.port(True).reconcile('company', 'task1', 'linkedin_step_connect')
        self.assertEqual('apollo_reconcile_pending', result['state'])
        self.assertEqual('execution_binding_unverified', result['reason'])
        self.assertEqual('attempted', self.row()['apollo_status'])
        self.assertFalse(any(method == 'POST' for method, _, _ in self.fake.calls))

    def test_reconciliation_of_open_task_never_posts(self):
        self.assertEqual('apollo_reconcile_pending', self.port(True).reconcile(
            'company', 'task1', 'linkedin_step_connect')['state'])
        self.assertEqual('pending', self.row()['apollo_status'])
        self.assertFalse(any(method == 'POST' for method, _, _ in self.fake.calls))

    def test_reconciliation_timeout_remains_pending(self):
        self.fake.error = True
        self.assertEqual('apollo_reconcile_pending', self.port(True).reconcile(
            'company', 'task1', 'linkedin_step_connect')['state'])
        self.assertEqual('pending', self.row()['apollo_status'])
        self.assertFalse(any(method == 'POST' for method, _, _ in self.fake.calls))

    def test_transport_rejects_all_post_before_network_including_valid_shape(self):
        with patch('http.client.HTTPSConnection') as connection:
            for path in ('/api/v1/tasks/task1', '/api/v1/tasks/task1/complete?x=1',
                         '/api/v1/tasks/task1/complete'):
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'endpoint_blocked'):
                    _request('POST', path, b'{}', {}, 1)
            connection.assert_not_called()

    def test_transport_also_blocks_protected_fixture(self):
        with patch('http.client.HTTPSConnection') as connection:
            with self.assertRaisesRegex(ValueError, 'fixture_protegida'):
                _request('GET', '/api/v1/tasks/' + EXCLUDED_TASK, None, {}, 1)
            connection.assert_not_called()


if __name__ == '__main__':
    unittest.main()
