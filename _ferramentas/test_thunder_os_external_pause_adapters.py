"""Offline stop/readback contracts; no provider or runtime mutation."""
from copy import deepcopy
import json
import unittest
import tempfile
from pathlib import Path
from thunder_os.db import Repository
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
from thunder_os.external_pause_adapters import ApolloExternalPause, ExternalPauseError, UnsupportedExternalPause

BINDING = {'campaign_id': 'BR-PILOT', 'account_id': 'team-1', 'sequence_id': 'sequence-1'}
COMMAND = {**BINDING, 'contact_id': 'contact-1', 'email': 'person@example.invalid'}


class ExternalPauseAdapterTests(unittest.TestCase):
    def setUp(self):
        guard = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        guard.start(); self.addCleanup(guard.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Repository(Path(self.temp.name) / "stop.db")
        self.calls = []
        self.state, self.team, self.status, self.error = 'active', 'team-1', 200, None
        self.contact, self.sequence, self.address = 'contact-1', 'sequence-1', COMMAND['email']
        self.complete = True

    def wire(self, method, path, body, headers, timeout):
        self.calls.append((method, path, deepcopy(body)))
        if path == '/api/v1/users/api_profile':
            data = {'id': 'user-1', 'team_id': self.team}
        elif path == '/api/v1/contacts/search':
            data = {'contacts': [{'id': self.contact, 'email': self.address, 'team_id': self.team,
                'contact_campaign_statuses': [{'emailer_campaign_id': self.sequence, 'status': self.state}]}],
                'pagination': {'page': 1, 'per_page': 100, 'total_entries': 1 if self.complete else 101,
                               'total_pages': 1 if self.complete else 2}}
        else:
            if self.error: raise self.error
            return self.status, b'{"api_key":"must-never-be-returned"}'
        return 200, json.dumps(data).encode()

    def client(self, **kwargs):
        return ApolloExternalPause(BINDING, credential_loader=lambda: 'synthetic-secret',
            request=self.wire, **{'mutation_enabled': True, 'read_enabled': True,
                                 'authorize': lambda command: command == COMMAND, 'repository': self.repo, **kwargs})

    def test_default_off_never_loads_credentials_or_calls_transport(self):
        loader = Mock(side_effect=AssertionError('credential access forbidden'))
        client = ApolloExternalPause(BINDING, credential_loader=loader, request=self.wire)
        with self.assertRaisesRegex(ExternalPauseError, 'disabled'): client.stop(COMMAND)
        with self.assertRaisesRegex(ExternalPauseError, 'read_disabled'): client.reconcile(COMMAND)
        loader.assert_not_called(); self.assertEqual(self.calls, [])

    def test_stop_is_exact_single_contact_sequence_and_acceptance_is_not_readback(self):
        result = self.client().stop(COMMAND)
        self.assertEqual(result['status'], 'readback_required')
        self.assertFalse(result['paused']); self.assertFalse(result['readback_verified'])
        method, path, body = self.calls[-1]
        self.assertEqual(method, 'POST'); self.assertIsNone(body)
        self.assertEqual(urlsplit(path).path, '/api/v1/emailer_campaigns/remove_or_stop_contact_ids')
        self.assertEqual(parse_qs(urlsplit(path).query), {
            'emailer_campaign_ids[]': ['sequence-1'], 'contact_ids[]': ['contact-1'], 'mode': ['mark_as_finished']})
        self.assertEqual(len(self.calls), 3)
        self.assertNotIn('api_key', json.dumps(result)); self.assertFalse(result['retry_allowed'])

    def test_fresh_reconcile_confirms_only_finished_not_active_or_paused(self):
        for state in ('active', 'paused', 'finished'):
            with self.subTest(state=state):
                self.state = state; self.calls.clear()
                result = self.client().reconcile(COMMAND)
                self.assertEqual(result['readback_verified'], state == 'finished')
                self.assertFalse(result['write_attempted'])
                self.assertEqual([c[1] for c in self.calls], ['/api/v1/users/api_profile', '/api/v1/contacts/search'])

    def test_timeout_uncertain_and_429_no_retry_or_automatic_readback(self):
        for error, status, expected in ((TimeoutError(), 200, 'uncertain'), (None, 429, 'failed'),
                                        (None, 503, 'uncertain'), (None, 202, 'readback_required')):
            with self.subTest(status=status, error=error):
                self.error, self.status = error, status; self.calls.clear()
                self.repo = Repository(Path(self.temp.name) / ('case-' + str(status) + '.db'))
                result = self.client().stop(COMMAND)
                self.assertEqual(result['status'], expected)
                self.assertFalse(result['paused']); self.assertFalse(result['retry_allowed'])
                self.assertEqual(len(self.calls), 3)

    def test_target_identity_incomplete_or_ambiguous_blocks_before_write(self):
        for field, value in (('team','other'), ('contact','other'), ('sequence','other'),
                             ('address','other@example.invalid'), ('complete',False)):
            with self.subTest(field=field):
                old = getattr(self, field); setattr(self, field, value); self.calls.clear()
                self.repo = Repository(Path(self.temp.name) / (field + '.db'))
                with self.assertRaises(ExternalPauseError): self.client().stop(COMMAND)
                self.assertFalse(any('remove_or_stop' in c[1] for c in self.calls))
                result = self.client().reconcile(COMMAND)
                self.assertEqual(result['status'], 'unverified')
                self.assertFalse(result['paused']); setattr(self, field, old)

    def test_changed_binding_and_auth_revoked_after_reads_block(self):
        for change in ({'account_id':'other'}, {'sequence_id':'other'}, {'contact_id':'bad/path'},
                       {'email':'person@example.invalid&contact_ids[]=other'}, {'extra':'value'}):
            with self.subTest(change=change):
                with self.assertRaises(ExternalPauseError): self.client().stop({**COMMAND, **change})
        self.assertEqual(self.calls, [])
        authority = Mock(side_effect=[True, False])
        with self.assertRaisesRegex(ExternalPauseError, 'not_authorized'):
            self.client(authorize=authority).stop(COMMAND)
        self.assertEqual(authority.call_count, 2)
        self.assertFalse(any('remove_or_stop' in c[1] for c in self.calls))

    def test_already_finished_does_not_repeat_mutation(self):
        self.state = 'finished'
        result = self.client().stop(COMMAND)
        self.assertEqual(result['status'], 'already_stopped')
        self.assertTrue(result['readback_verified']); self.assertFalse(result['write_attempted'])
        self.assertEqual(len(self.calls), 2)

    def test_uncertain_attempt_survives_restart_and_never_writes_twice(self):
        self.error = TimeoutError()
        first = self.client().stop(COMMAND)
        self.assertEqual(first['status'], 'uncertain')
        self.repo = Repository(Path(self.temp.name) / 'stop.db')
        self.error = None
        second = self.client().stop(COMMAND)
        self.assertEqual(second['status'], 'reconciliation_required')
        self.assertEqual(second['previous_status'], 'uncertain')
        self.assertEqual(len(self.calls), 3)
        with self.assertRaisesRegex(ExternalPauseError, 'idempotency_conflict'):
            self.client(authorize=lambda c: True).stop({**COMMAND, 'email': 'changed@example.invalid'})
        self.assertEqual(len(self.calls), 3)
        changed = ApolloExternalPause({**BINDING, 'campaign_id': 'RENAMED'},
            credential_loader=lambda: 'synthetic-secret', request=self.wire,
            mutation_enabled=True, authorize=lambda c: True, repository=self.repo)
        with self.assertRaisesRegex(ExternalPauseError, 'idempotency_conflict'):
            changed.stop({**COMMAND, 'campaign_id': 'RENAMED'})
        self.assertEqual(len(self.calls), 3)

    def test_concurrent_attempts_send_once(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.client().stop(COMMAND), range(2)))
        self.assertEqual(sorted(r['status'] for r in results), ['readback_required', 'reconciliation_required'])
        self.assertEqual(sum('remove_or_stop' in c[1] for c in self.calls), 1)

    def test_unsupported_providers_have_no_transport_fallback(self):
        for provider in ('instantly', 'hubspot', 'hubspot_seq', 'unknown'):
            result = UnsupportedExternalPause(provider).stop(COMMAND)
            self.assertEqual(result['status'], 'blocked')
            self.assertFalse(result['readback_verified']); self.assertFalse(result['write_attempted'])
            self.assertFalse(result['runtime_enabled'])


class ApolloReplyPauseCompositionTests(unittest.TestCase):
    wire = ExternalPauseAdapterTests.wire

    def setUp(self):
        ExternalPauseAdapterTests.setUp(self)
        from types import SimpleNamespace
        from thunder_os.campaign_registry import CampaignRegistry
        from thunder_os.new_journey import NewJourney
        from thunder_os.reply_control import ReplyControl
        from thunder_os.hubspot_executor import HubSpotHTTP
        from thunder_os.new_journey_hubspot import HubSpotEligibilityRead
        from thunder_os.new_journey_qualification import candidate
        from test_thunder_os_campaign_registry import config
        from test_thunder_os_new_journey_hubspot import FakeHTTP, EMAIL, PROFILE
        self.registry = CampaignRegistry(self.repo)
        self.registry.register({**config(), 'campaign_id': BINDING['campaign_id'], 'platform': 'apollo',
            'providers': {'email': 'apollo'}, 'configuration': {'flow_version': '1',
                'provider_accounts': {'email': BINDING['account_id']}}}, 'ygor', 0)
        self.http = FakeHTTP()
        self.hubspot = HubSpotHTTP(self.http.loader, request=self.http)
        qualification = candidate(HubSpotEligibilityRead(self.hubspot).lookup(EMAIL, contact_id='123', profile=PROFILE))
        self.assertIsNotNone(qualification)
        self.address = EMAIL
        self.journey = object.__new__(NewJourney)
        self.journey.repo = self.repo
        self.journey.commercial_accounts = None
        self.journey.service = SimpleNamespace(global_paused=lambda: False)
        draft = {'campaign_id': BINDING['campaign_id'], 'campaign_context': self.registry.load(BINDING['campaign_id']),
            'rows': [{'source_key': 'source-1', 'email': EMAIL, 'linkedin_url': PROFILE}],
            'checks': [{'source_key': 'source-1', 'qualification_context': qualification, 'checks': []}],
            'sequence': {'apollo_sequence_id': 'sequence-1', 'account_id': 'team-1'}}
        draft['export_snapshot'] = {'scope_hash': self.journey._scope(draft),
            'sequence_id': 'sequence-1', 'account_id': 'team-1', 'source_keys': ['source-1'],
            'recipient_bindings': {'source-1': {'contact_id': 'contact-1', 'email': EMAIL, 'lookup_complete': True}}}
        self.repo.put_state('journey_draft', 'journey-1', draft)
        self.repo.put_state('cadence', 'cadence-1', {'hubspot_contact_id': '123', 'person_key': 'hubspot:123',
            'status': 'active', 'steps': [{'id': 'E2', 'status': 'approved'}]})
        result = ReplyControl(self.repo, self.registry).ingest({
            'id': 'manual-reply-1', 'channel': 'phone', 'content': 'Synthetic human reply',
            'received_at': '2026-10-25T12:00:00+00:00', 'hubspot_contact_id': '123',
            'campaign_id': BINDING['campaign_id'], 'human_reply': True, 'identity_verified': True,
            'source': {'provider': 'manual', 'account_id': 'ygor', 'provider_message_id': 'reply-1'}}, 'ygor')
        self.reply_id = result['reply']['id']
        self.assertEqual(result['status'], 'paused_internal')
        self.http.calls.clear()

    def composition(self, **kwargs):
        from thunder_os.external_pause_adapters import ApolloReplyPauseComposition
        return ApolloReplyPauseComposition(self.repo, self.registry, self.journey, self.hubspot,
            credential_loader=lambda: 'synthetic-secret', request=self.wire,
            **{'execution_enabled': True, 'read_enabled': True, 'authorize': lambda envelope: True, **kwargs})

    def test_default_off_does_not_read_sources_or_touch_runtime(self):
        from thunder_os.external_pause_adapters import ApolloReplyPauseComposition
        composition = ApolloReplyPauseComposition(None, None, None, None)
        with self.assertRaisesRegex(ExternalPauseError, 'disabled'):
            composition.dispatch(self.reply_id, 'journey-1', 'source-1')
        with self.assertRaisesRegex(ExternalPauseError, 'read_disabled'):
            composition.reconcile(self.reply_id, 'journey-1', 'source-1')
        self.assertEqual(self.calls, [])

    def test_real_reply_pipeline_dispatch_and_readback_keep_other_channels_unconfirmed(self):
        composition = self.composition()
        first = composition.dispatch(self.reply_id, 'journey-1', 'source-1')
        self.assertEqual(first['status'], 'readback_required')
        self.assertFalse(first['all_channels_stopped'])
        self.assertTrue(first['observation_persisted'])
        self.assertEqual(self.repo.get_state('cadence', 'cadence-1')['status'], 'paused_reply')
        self.state = 'finished'
        second = composition.reconcile(self.reply_id, 'journey-1', 'source-1')
        self.assertTrue(second['readback_verified'])
        alert = self.repo.get_state('operator_alert', self.reply_id)
        self.assertTrue(alert['active'])
        self.assertFalse(alert['external_pauses_enabled'])
        self.assertTrue(alert['external_pause_observations']['apollo']['readback_verified'])
        self.assertEqual(sum('remove_or_stop' in c[1] for c in self.calls), 1)

    def test_unauthorized_or_global_pause_never_reads_provider(self):
        with self.assertRaisesRegex(ExternalPauseError, 'not_authorized'):
            self.composition(authorize=lambda envelope: False).dispatch(self.reply_id, 'journey-1', 'source-1')
        self.journey.service.global_paused = lambda: True
        with self.assertRaisesRegex(ExternalPauseError, 'not_authorized'):
            self.composition().dispatch(self.reply_id, 'journey-1', 'source-1')
        self.assertEqual(self.calls, []); self.assertEqual(self.http.calls, [])

    def test_stale_journey_and_reply_identity_cannot_select_another_target(self):
        for change in ({'identity_confirmed': False}, {'human_reply': None}, {'hubspot_contact_id': '999'}):
            original = self.repo.get_state('reply', self.reply_id)
            self.repo.put_state('reply', self.reply_id, {**original, **change})
            with self.assertRaises(ExternalPauseError):
                self.composition().dispatch(self.reply_id, 'journey-1', 'source-1')
            self.repo.put_state('reply', self.reply_id, original)
        draft = self.repo.get_state('journey_draft', 'journey-1')
        draft['rows'][0]['email'] = 'changed@example.invalid'
        self.repo.put_state('journey_draft', 'journey-1', draft)
        with self.assertRaisesRegex(ExternalPauseError, 'export_binding'):
            self.composition().dispatch(self.reply_id, 'journey-1', 'source-1')
        self.assertEqual(self.calls, [])

    def test_fresh_crm_identity_change_blocks_apollo_and_preserves_internal_stop(self):
        self.http.contacts['123']['properties']['email'] = 'changed@example.invalid'
        with self.assertRaisesRegex(ExternalPauseError, 'canonical_identity_changed'):
            self.composition().dispatch(self.reply_id, 'journey-1', 'source-1')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.repo.get_state('cadence', 'cadence-1')['status'], 'paused_reply')

    def test_version_change_during_authority_check_blocks_before_write(self):
        def changed(envelope):
            draft = self.repo.get_state('journey_draft', 'journey-1')
            self.repo.put_state('journey_draft', 'journey-1', draft)
            return True
        with self.assertRaisesRegex(ExternalPauseError, 'not_authorized'):
            self.composition(authorize=changed).dispatch(self.reply_id, 'journey-1', 'source-1')
        self.assertFalse(any('remove_or_stop' in c[1] for c in self.calls))

    def test_authority_callback_changing_final_version_cannot_dispatch(self):
        calls = []
        def changed(envelope):
            calls.append(envelope)
            if len(calls) == 3:
                draft = self.repo.get_state('journey_draft', 'journey-1')
                self.repo.put_state('journey_draft', 'journey-1', draft)
            return True
        with self.assertRaisesRegex(ExternalPauseError, 'not_authorized'):
            self.composition(authorize=changed).dispatch(self.reply_id, 'journey-1', 'source-1')
        self.assertEqual(len(calls), 3)
        self.assertFalse(any('remove_or_stop' in c[1] for c in self.calls))

    def _assert_final_kill_switch(self, switch):
        composition = self.composition()
        calls = []
        def revoke(envelope):
            calls.append(envelope)
            if len(calls) == 3:
                if switch == 'global':
                    self.journey.service.global_paused = lambda: True
                else:
                    composition.execution_enabled = False
            return True
        composition.authorize = revoke
        with self.assertRaisesRegex(ExternalPauseError, 'not_authorized'):
            composition.dispatch(self.reply_id, 'journey-1', 'source-1')
        self.assertEqual(len(calls), 3)
        self.assertFalse(any('remove_or_stop' in c[1] for c in self.calls))

    def test_final_authority_callback_global_pause_blocks_dispatch(self):
        self._assert_final_kill_switch('global')

    def test_final_authority_callback_execution_off_blocks_dispatch(self):
        self._assert_final_kill_switch('execution')

    def test_alert_concurrent_change_is_not_overwritten_after_stop(self):
        original = self.wire
        def wire(method, path, body, headers, timeout):
            result = original(method, path, body, headers, timeout)
            if 'remove_or_stop' in path:
                alert = self.repo.get_state('operator_alert', self.reply_id)
                self.repo.put_state('operator_alert', self.reply_id, {**alert, 'message': 'concurrent operator change'})
            return result
        self.wire = wire
        result = self.composition().dispatch(self.reply_id, 'journey-1', 'source-1')
        self.assertFalse(result['observation_persisted'])
        self.assertEqual(self.repo.get_state('operator_alert', self.reply_id)['message'], 'concurrent operator change')
        self.assertEqual(sum('remove_or_stop' in c[1] for c in self.calls), 1)

    def test_pending_attempt_reentry_routes_to_reconciliation_not_second_write(self):
        self.error = TimeoutError()
        composition = self.composition()
        first = composition.dispatch(self.reply_id, 'journey-1', 'source-1')
        self.assertEqual(first['status'], 'uncertain')
        second = composition.dispatch(self.reply_id, 'journey-1', 'source-1')
        self.assertEqual(second['status'], 'reconciliation_required')
        self.assertEqual(sum('remove_or_stop' in c[1] for c in self.calls), 1)


if __name__ == '__main__': unittest.main()
