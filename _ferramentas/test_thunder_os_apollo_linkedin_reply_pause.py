"""Offline-only composition tests; all identities and transports synthetic."""
import json
import io
import os
from pathlib import Path
import time
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from thunder_os.apollo_linkedin_reply_coverage import ApolloContactReplyReader
from thunder_os.apollo_linkedin_reply_pause import ApolloLinkedInReplyPause, ApolloReplyPauseWorker, main
import test_thunder_os_reply_control as fixtures


class ReplyPauseTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict('os.environ', {'THUNDER_APOLLO_LI_REPLY_STOP_ENABLED': 'true'})
        env.start(); self.addCleanup(env.stop)
        self.fixture = fixtures.ApolloLinkedInReplyIdentityTests('test_webhook_binding_stops_t4_and_records_unclassified_reply_once')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.repo = self.fixture.repo
        self.reply = self.fixture.control.ingest(self.fixture.payload, 'verified_source',
            provider_correlation=self.fixture.correlation)['reply']
        self.calls = []
        self.email = 'person@example.invalid'
        self.timeout = False
        self.complete_membership = False
        self.other_team_for_other_key = False
        self.disable_on_search = False

    def wire(self, method, path, body, headers, timeout):
        self.calls.append((method, path))
        if path == '/api/v1/users/api_profile':
            team = 'other-team' if self.other_team_for_other_key and headers.get('x-api-key') == 'other' else 'team'
            return 200, json.dumps({'user': {'id': 'actor', 'team_id': team},
                                    'id': 'actor', 'team_id': team}).encode()
        if path == '/api/v1/contacts/contact1':
            return 200, json.dumps({'contact': {'id': 'contact1', 'email': self.email,
                'contact_campaign_statuses_complete': self.complete_membership,
                'contact_campaign_statuses': [{'emailer_campaign_id': 'seq', 'status': 'active'}]}}).encode()
        if path == '/api/v1/contacts/search':
            if self.disable_on_search:
                os.environ['THUNDER_APOLLO_LI_REPLY_STOP_ENABLED'] = 'false'
            return 200, json.dumps({'contacts': [{'id': 'contact1', 'email': self.email,
                'team_id': 'team', 'contact_campaign_statuses': [
                    {'emailer_campaign_id': 'seq', 'status': 'active'}]}],
                'pagination': {'page': 1, 'per_page': 100, 'total_entries': 1, 'total_pages': 1}}).encode()
        if self.timeout:
            raise TimeoutError('synthetic')
        return 200, b'{}'

    def port(self, enabled=False):
        reader = ApolloContactReplyReader(lambda: 'synthetic', team_id='team',
                                         api_actor_id='actor', request=self.wire)
        return ApolloLinkedInReplyPause(self.repo, contact_reader=reader,
            credential_loader=lambda: 'synthetic', request=self.wire, enabled=enabled)

    def test_default_off_has_no_read_or_mutation(self):
        self.assertEqual(self.port().stop(self.reply['id'])['reason'], 'external_pause_disabled')
        self.assertEqual(self.calls, [])

    def test_cli_default_and_disabled_key_never_construct_runtime(self):
        with patch.dict('os.environ', {'THUNDER_APOLLO_LI_REPLY_STOP_ENABLED': 'false'}):
            with patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(main(['--reply', 'reply-control:synthetic']), 0)
                self.assertEqual(json.loads(output.getvalue())['status'], 'dry_run')
            with patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(main(['--reply', 'reply-control:synthetic', '--execute']), 2)
                self.assertEqual(json.loads(output.getvalue())['reason'], 'external_pause_disabled')

    def test_verified_reply_requests_stop_once_and_keeps_pending(self):
        result = self.port(enabled=True).stop(self.reply['id'])
        self.assertEqual(result['status'], 'external_pause_pending')
        self.assertTrue(result['write_attempted'])
        self.assertFalse(result['readback_verified'])
        writes = [path for method, path in self.calls if method == 'POST' and 'remove_or_stop' in path]
        self.assertEqual(len(writes), 1)
        self.assertEqual(parse_qs(urlsplit(writes[0]).query)['mode'], ['stop'])
        self.assertEqual(self.repo.get_state('operator_alert', self.reply['id'])['external_pause_status'],
                         'external_pause_pending')
        again = self.port(enabled=True).stop(self.reply['id'])
        self.assertFalse(again['write_attempted'])
        self.assertEqual(len([1 for method, path in self.calls if method == 'POST' and 'remove_or_stop' in path]), 1)

    def test_missing_email_never_calls_stop(self):
        self.email = None
        result = self.port(enabled=True).stop(self.reply['id'])
        self.assertEqual(result['status'], 'external_pause_pending')
        self.assertFalse(result['write_attempted'])
        self.assertTrue(result['retry_allowed'])
        self.assertFalse(any(method == 'POST' for method, _ in self.calls))

    def test_key_disabled_during_provider_read_blocks_stop(self):
        self.disable_on_search = True
        result = self.port(enabled=True).stop(self.reply['id'])
        self.assertEqual(result['status'], 'external_pause_pending')
        self.assertFalse(any('remove_or_stop' in path for _, path in self.calls))

    def test_missing_email_can_stop_only_with_complete_membership(self):
        self.email = None
        self.complete_membership = True
        result = self.port(enabled=True).stop(self.reply['id'])
        self.assertTrue(result['write_attempted'])
        self.assertFalse(result['readback_verified'])
        self.assertEqual(len([p for m, p in self.calls if m == 'POST' and 'remove_or_stop' in p]), 1)

    def test_global_pause_and_expired_approval_do_not_block_containment(self):
        self.repo.put_state('control', 'global', {'paused': True, 'actor': 'ygor'})
        old = self.repo.get_state('approval', self.fixture.binding['approval_id'])
        value = {k: v for k, v in old.items() if k not in ('id', 'version', 'updated_at')}
        value.update(status='revoked', expires_at='2001-01-01T00:00:00+00:00')
        self.repo.put_state('approval', self.fixture.binding['approval_id'], value)
        result = self.port(enabled=True).stop(self.reply['id'])
        self.assertTrue(result['write_attempted'])
        self.assertEqual(result['status'], 'external_pause_pending')

    def test_other_write_credential_cannot_use_readers_team_proof(self):
        self.email = None
        self.complete_membership = True
        self.other_team_for_other_key = True
        reader = ApolloContactReplyReader(lambda: 'synthetic', team_id='team',
                                         api_actor_id='actor', request=self.wire)
        port = ApolloLinkedInReplyPause(self.repo, contact_reader=reader,
            credential_loader=lambda: 'other', request=self.wire, enabled=True)
        self.assertEqual(port.stop(self.reply['id'])['status'], 'external_pause_pending')
        self.assertFalse(any('remove_or_stop' in path for _, path in self.calls))

    def test_worker_default_off_scans_one_and_throttles(self):
        now = [time.time() + 120]
        port = self.port(enabled=True)
        worker = ApolloReplyPauseWorker(port, company='company', team='team', clock=lambda: now[0])
        with patch.dict('os.environ', {'THUNDER_APOLLO_LI_REPLY_STOP_ENABLED': 'false'}):
            self.assertFalse(worker.process_one())
            self.assertEqual(self.calls, [])
        with patch.dict('os.environ', {'THUNDER_APOLLO_LI_REPLY_STOP_ENABLED': 'true'}):
            self.assertTrue(worker.process_one())
            self.assertEqual(len([p for m, p in self.calls if m == 'POST' and 'remove_or_stop' in p]), 1)
            self.assertFalse(worker.process_one())
            now[0] += 120
            self.assertFalse(worker.process_one())
            self.assertFalse(worker.process_one())
            self.assertEqual(len([p for m, p in self.calls if m == 'POST' and 'remove_or_stop' in p]), 1)

    def test_existing_worker_processes_injected_reply_pause_without_webhook_network(self):
        from thunder_os.worker import WorkerRuntime
        now = time.time() + 120
        worker = WorkerRuntime.__new__(WorkerRuntime)
        worker.reply_pause_worker = ApolloReplyPauseWorker(self.port(enabled=True), company='company',
                                                            team='team', clock=lambda: now)
        with patch.dict('os.environ', {'THUNDER_APOLLO_LI_REPLY_STOP_ENABLED': 'true'}):
            self.assertTrue(worker.process_one())
        self.assertEqual(len([p for m, p in self.calls if m == 'POST' and 'remove_or_stop' in p]), 1)

    def test_containment_runtime_operates_when_outreach_globally_disabled(self):
        from thunder_os.worker import DisabledRuntime, build_runtime
        from unittest.mock import Mock
        fake_stop = Mock()
        fake_stop.process_one.return_value = True
        env = {'THUNDER_EXTERNAL_EFFECTS_ENABLED': 'false',
               'THUNDER_APOLLO_LI_REPLY_STOP_ENABLED': 'true',
               'THUNDER_OS_DB': str(self.repo.path),
               'THUNDER_OS_WORKER_HEALTH': str(Path(self.repo.path).parent / 'health.json'),
               'THUNDER_OS_FAKE_PROVIDERS': '0'}
        with patch.dict('os.environ', env), \
                patch('thunder_os.worker._build_reply_pause_worker', return_value=fake_stop), \
                patch('thunder_os.worker.TouchLedger', side_effect=AssertionError('outreach ledger forbidden')):
            runtime = build_runtime()
            self.assertIsInstance(runtime, DisabledRuntime)
            self.assertTrue(runtime.process_one())
            fake_stop.process_one.assert_called_once_with()
            health = json.loads(runtime.health_path.read_text())
            self.assertFalse(health['external_effects_enabled'])
            self.assertTrue(health['reply_containment_enabled'])

    def test_disabled_outreach_runtime_recovers_after_transient_containment_failure(self):
        from thunder_os.worker import DisabledRuntime
        from unittest.mock import Mock
        port = Mock()
        runtime = DisabledRuntime(port)
        runtime.health_path = Path(self.repo.path).parent / 'health-recovery.json'
        calls = []
        def work():
            calls.append('containment')
            if len(calls) == 1:
                raise OSError('temporary')
            runtime.running = False
            return True
        port.process_one.side_effect = work
        with patch.object(runtime, '_health', wraps=runtime._health) as health, \
                patch('thunder_os.worker.time.sleep'):
            runtime.run()
        self.assertEqual(calls, ['containment', 'containment'])
        health.assert_any_call('degraded')
        self.assertFalse(json.loads(runtime.health_path.read_text())['external_effects_enabled'])

    def test_ambiguous_timeout_does_not_retry(self):
        self.timeout = True
        first = self.port(enabled=True).stop(self.reply['id'])
        self.assertEqual(first['status'], 'external_pause_pending')
        self.assertTrue(first['write_attempted'])
        self.assertFalse(first['retry_allowed'])
        self.port(enabled=True).stop(self.reply['id'])
        self.assertEqual(len([1 for method, path in self.calls if method == 'POST' and 'remove_or_stop' in path]), 1)

    def test_missing_or_changed_binding_never_calls_provider(self):
        binding = self.repo.get_state('apollo_linkedin_identity_binding', self.fixture.binding_id)
        self.repo.put_state('apollo_linkedin_identity_binding', self.fixture.binding_id,
                            {**binding, 'contact_id': 'other'})
        result = self.port(enabled=True).stop(self.reply['id'])
        self.assertEqual(result['status'], 'external_pause_pending')
        self.assertEqual(self.calls, [])
