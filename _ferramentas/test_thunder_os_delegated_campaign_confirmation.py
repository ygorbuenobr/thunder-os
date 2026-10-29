"""Isolated contract tests for delegated campaign approvals; no provider writes."""
import unittest
from unittest.mock import Mock, patch

from thunder_os.approvals import ApprovalError
from thunder_os.connectsafely_console import ConnectSafelyError
import test_thunder_os_hermes_linkedin_cadence as fixtures
import test_thunder_os_migrated_operations as migrated


class DelegatedConfirmationTests(unittest.TestCase):
    def setUp(self):
        fixtures.HermesLinkedInCadenceTests.setUp(self)
        self.addCleanup(self.temp.cleanup)
        approval = self.runner.approve(fixtures.approval(), 'ygor')
        step = approval['steps'][1]
        self.authority = {'id': approval['id'], 'version': approval['version'],
                          'review_hash': approval['review_hash'], 'marker': step['marker']}
        self.preview = self.app.prepare_delegated(step['plan'], self.authority)
        self.body = {'preview_id': self.preview['id'], 'expected_version': self.preview['version'],
                     'review_hash': self.preview['review_hash']}
        self.gate = Mock()
        self.console.campaign_gate = lambda: self.gate
        self.record = self.repo.append_state('operation_approval', 'human-record', {
            'preview_id': self.preview['id'], 'preview_version': self.preview['version'],
            'execution_confirmation': {'request': {'confirmed': True, 'context': {}}}})

    def test_confirmed_record_bound_to_preview_authorizes_delegate(self):
        result = self.app.enqueue_delegated({**self.body, 'campaign_approval_id': self.record['id']}, self.authority)
        job = self.repo.get_state('connectsafely_job', result['job_id'])
        self.app._job_authorized(job)
        self.assertEqual('hermes (delegado)', job['actor'])
        self.assertEqual(self.record['execution_confirmation']['request'], job['review']['execution_confirmation'])
        self.assertGreaterEqual(self.gate.validate.call_count, 2)
        self.assertEqual(0, self.console.executes)

    def test_missing_wrong_or_invalid_record_cannot_reserve(self):
        wrong = self.repo.append_state('operation_approval', 'wrong', {
            'preview_id': 'other', 'preview_version': self.preview['version']})
        with patch.object(self.repo, 'reserve_delegated_action', wraps=self.repo.reserve_delegated_action) as reserve:
            for ident in (None, 'missing', wrong['id']):
                with self.subTest(ident=ident), self.assertRaises(ConnectSafelyError):
                    self.app.enqueue_delegated({**self.body, 'campaign_approval_id': ident}, self.authority)
            self.gate.validate.side_effect = ApprovalError('campaign_confirmation_scope_changed')
            with self.assertRaisesRegex(ConnectSafelyError, 'campaign_confirmation_scope_changed'):
                self.app.enqueue_delegated({**self.body, 'campaign_approval_id': self.record['id']}, self.authority)
            reserve.assert_not_called()
        self.assertEqual([], self.repo.list_state('connectsafely_job'))

    def test_human_entry_rejects_other_actor(self):
        with self.assertRaises(ConnectSafelyError):
            self.app.approve_delegated_campaign({**self.body, 'execution_confirmation': {}}, 'hermes')
        self.gate.approve.assert_not_called()

    def test_revocation_after_enqueue_blocks_worker(self):
        queued = self.app.enqueue_delegated({**self.body, 'campaign_approval_id': self.record['id']}, self.authority)
        job = self.repo.get_state('connectsafely_job', queued['job_id'])
        self.gate.validate.side_effect = ApprovalError('campaign_confirmation_scope_changed')
        with self.assertRaisesRegex(ConnectSafelyError, 'campaign_confirmation_scope_changed'):
            self.app._job_authorized(job)
        self.assertEqual(0, self.console.executes)

    def test_different_delegated_authority_is_rejected(self):
        with patch.object(self.repo, 'reserve_delegated_action', wraps=self.repo.reserve_delegated_action) as reserve:
            with self.assertRaisesRegex(ConnectSafelyError, 'delegated_authority_changed'):
                self.app.enqueue_delegated({**self.body, 'campaign_approval_id': self.record['id']},
                                           {**self.authority, 'version': self.authority['version'] + 1})
            reserve.assert_not_called()

    def test_delegated_dispatch_stays_closed_without_fresh_reply_and_optout_proof(self):
        queued = self.app.enqueue_delegated({**self.body, 'campaign_approval_id': self.record['id']}, self.authority)
        calls = []
        self.app.delegated_task_guard = lambda job: calls.append(job['id']) or True
        def synthetic_execute(review, actor, *, dispatch_guard, pre_dispatch_check):
            pre_dispatch_check()
            self.fail('provider must not run without fresh negative evidence')
        self.console.execute = synthetic_execute
        self.app.process_one(job_id=queued['job_id'])
        job = self.repo.get_state('connectsafely_job', queued['job_id'])
        self.assertEqual([queued['job_id']], calls)
        self.assertEqual('blocked', job['state'])
        self.assertEqual(0, self.console.executes)


class PreDispatchGuardTests(unittest.TestCase):
    def setUp(self):
        migrated.MigratedOperationsTests.setUp(self)

    def test_denied_guard_runs_outside_transaction_without_provider_call(self):
        console, port, _, plan = migrated.MigratedOperationsTests.build(self, 'connectsafely')
        preview = console.prepare(plan, 'ygor')
        body = migrated.MigratedOperationsTests.body(self, console, preview, 'connectsafely')
        def reject():
            self.assertIsNone(getattr(self.repo._local, 'connection', None))
            return False
        result = console.execute(body, 'ygor', pre_dispatch_check=reject)
        self.assertEqual('blocked', result['status'])
        self.assertEqual('dispatch_guard_denied', result['error_code'])
        self.assertEqual(0, port.calls)
        attempt = self.repo.list_state('connectsafely_attempt')[0]
        self.assertEqual('not_dispatched', attempt['phase'])


if __name__ == '__main__':
    unittest.main()
