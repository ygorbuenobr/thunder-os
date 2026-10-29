"""Synthetic authenticated panel-to-delegate path; no network or provider writes."""
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import painel_servidor as panel
from thunder_os.campaign_registry import CampaignRegistry
from thunder_os.service import AuthorizationError
from thunder_os.connectsafely_console import ConnectSafelyError
from test_campaign_sheet_support import make_sheet
import test_thunder_os_hermes_linkedin_cadence as fixtures


class PanelDelegateTests(unittest.TestCase):
    def setUp(self):
        fixtures.HermesLinkedInCadenceTests.setUp(self)
        self.addCleanup(self.temp.cleanup)
        approved = self.runner.approve({**fixtures.approval(), 'hubspot_contact_id': '255570819657'}, 'ygor')
        self.tasks.task['id'] = '12345'
        self.tasks.task['contact_ids'] = ['255570819657']
        # Explicit local synthetic identity evidence; never a provider read.
        self.runner.identity_verifier = lambda contact, plan: {
            'contact': {'id': contact, 'linkedin_url': 'https://www.linkedin.com/in/' + plan['recipient_public_id'],
                        'name': 'Synthetic Lead', 'company': 'Synthetic Company'},
            'profile': {'account_id': plan['account_id'], 'profile_urn': plan['recipient_profile_urn'],
                        'linkedin_url': 'https://www.linkedin.com/in/' + plan['recipient_public_id'],
                        'name': 'Synthetic Lead', 'company': 'Synthetic Company'}}
        # Deterministic in-memory suppression readers, not real provider evidence.
        fixtures.HermesLinkedInCadenceTests.configure_suppression(self)
        from thunder_os.delegated_suppression import ContactScope
        self.app.delegated_contact_scope = lambda job, preview: ContactScope(
            '255570819657', preview['plan']['recipient_profile_urn'], 'synthetic@example.test', 'apollo')
        self.call_route(body={'task_id': '12345', 'approval_id': approved['id'],
                              'approval_version': approved['version'], 'review_hash': approved['review_hash'],
                              'marker': 'cad_li_1'}, route='task-bindings')
        store, sheet = make_sheet(Path(self.temp.name) / 'sheet')
        self.console.campaign_sheet_store = store
        CampaignRegistry(self.repo).register({
            'campaign_id': 'fixture-cadence', 'campaign_version': '1', 'rules_version': '1',
            'run_id': 'fixture-run', 'platform': 'thunder_os',
            'providers': {'linkedin': 'connectsafely'}, 'rules': 'synthetic only', 'briefing': sheet,
            'configuration': {'flow_version': '1', 'provider_accounts': {'linkedin': 'cs-account'}},
        }, 'ygor', 0)
        self.preview = self.runner.run_task('12345')
        self.body = {'task_id': '12345', 'preview_id': self.preview['preview_id'],
                     'expected_version': self.preview['expected_version'], 'review_hash': self.preview['review_hash'],
                     'execution_confirmation': self.app.campaign_review(
                         {'preview_id': self.preview['preview_id'], 'campaign_id': 'fixture-cadence'}, 'ygor')}

    def call_route(self, *, actor='ygor', nonce='test-nonce', origin='http://localhost', body=None,
                   route='campaign-confirmations', trusted_peer=True):
        handler = object.__new__(panel.Handler)
        handler.server = SimpleNamespace(thunder_service=SimpleNamespace(repository=self.repo),
            thunder_connectsafely_app=self.app, thunder_origin='http://localhost',
            thunder_nonces={'test-nonce': ('ygor', time.monotonic() + 30)})
        handler.command = 'POST'; handler.client_address = ('127.0.0.1', 12345)
        handler.headers = {'Origin': origin, 'X-Thunder-Nonce': nonce, 'X-Authenticated-User': actor}
        handler.corpo = lambda: body or self.body
        handler.responder = lambda status, value: (status, value)
        # Unit fixture for a previously verified Unix peer; real peer credentials
        # and TCP rejection have independent socket tests.
        handler.trusted_confirmation_peer = lambda: trusted_peer
        with patch.object(panel, 'restricted_mode', return_value=False):
            return handler.thunder_api(urlsplit('/api/os/v1/linkedin-cadence-' + route))

    def test_authenticated_confirmation_resumes_exact_task_in_memory(self):
        self.assertEqual('human_confirmation_required', self.preview['status'])
        self.assertEqual([], self.tasks.completions)
        self.assertEqual(0, self.console.executes)
        status, confirmation = self.call_route()
        self.assertEqual(201, status)
        self.assertEqual(confirmation['campaign_approval_id'], self.repo.get_state(
            'linkedin_cadence_human_confirmation', '12345')['campaign_approval_id'])
        self.assertEqual([], self.tasks.completions)
        self.assertEqual(0, self.console.executes)
        result = self.runner.run_task('12345', preview_id=confirmation['preview_id'],
                                      campaign_approval_id=confirmation['campaign_approval_id'])
        self.assertEqual('completed', result['status'], result)
        self.assertEqual('12345', result['task_id'])
        self.assertEqual('connectsafely:receipt-1', result['receipt_ref'])
        self.assertEqual([('12345', 'connectsafely:receipt-1')], self.tasks.completions)
        persisted = self.repo.get_state('linkedin_cadence_task', '12345')
        self.assertEqual('12345', persisted['task_id'])
        self.assertEqual('connectsafely:receipt-1', persisted['receipt_ref'])
        self.assertEqual(1, self.console.executes)

    def test_invalid_auth_and_wrong_task_never_approve(self):
        for kwargs in ({'nonce': ''}, {'origin': 'http://evil'}, {'actor': 'claude'}, {'trusted_peer': False}):
            with self.subTest(kwargs=kwargs), self.assertRaises((panel.Erro, AuthorizationError)):
                self.call_route(**kwargs)
        with self.assertRaises(panel.Erro):
            self.call_route(body={**self.body, 'task_id': 'other'})
        self.assertEqual([], self.repo.list_state('operation_approval'))
        self.assertEqual(0, self.console.executes)

    def test_revocation_after_human_approval_blocks_delegate(self):
        _, confirmation = self.call_route()
        self.repo.put_state('control', 'global', {'paused': True})
        with self.assertRaises(ConnectSafelyError):
            self.runner.run_task('12345', preview_id=confirmation['preview_id'],
                                 campaign_approval_id=confirmation['campaign_approval_id'])
        self.assertEqual(0, self.console.executes)

    def test_task_binding_is_immutable_and_rechecks_approval(self):
        from thunder_os.hermes_linkedin_runtime import task_cadence_binding
        binding = self.repo.get_state('linkedin_cadence_task_binding', '12345')
        self.assertEqual(binding['cadence_id'], task_cadence_binding(self.repo, '12345', clock=lambda: fixtures.NOW))
        changed = {'task_id': '12345', 'approval_id': binding['approval_id'],
                   'approval_version': binding['approval_version'], 'review_hash': binding['review_hash'],
                   'marker': 'cad_li_2'}
        with self.assertRaises(panel.Erro): self.call_route(body=changed, route='task-bindings')
        self.assertIsNone(task_cadence_binding(self.repo, 'other', clock=lambda: fixtures.NOW))
        approval = self.repo.get_state('linkedin_cadence_approval', binding['approval_id'])
        self.repo.put_state('linkedin_cadence_approval', binding['approval_id'], {**approval, 'status': 'revoked'})
        self.assertIsNone(task_cadence_binding(self.repo, '12345', clock=lambda: fixtures.NOW))

    def test_mocked_hubspot_task_without_sequence_reaches_confirmed_receipt(self):
        import json
        from thunder_os.hubspot_tasks import HubSpotTasks
        from thunder_os.hermes_linkedin_runtime import task_cadence_binding
        from test_thunder_os_hubspot_tasks import Transport
        transport = Transport()
        def request(method, path, body, headers, timeout):
            status, raw = transport(method, path, body, headers, timeout)
            return status, raw.replace(b'118775080824', b'12345')
        with patch.dict('os.environ', {'HUBSPOT_ACCESS_TOKEN': 'fixture-only'}):
            self.runner.tasks = HubSpotTasks('51412240', '92901049', 'HUBSPOT_ACCESS_TOKEN', request=request,
                cadence_for_task=lambda task_id: task_cadence_binding(self.repo, task_id, clock=lambda: fixtures.NOW))
            # Prepare a fresh preview bound to the real adapter's task contract.
            preview = self.runner.run_task('12345')
            body = {'task_id': '12345', 'preview_id': preview['preview_id'],
                    'expected_version': preview['expected_version'], 'review_hash': preview['review_hash'],
                    'execution_confirmation': self.app.campaign_review(
                        {'preview_id': preview['preview_id'], 'campaign_id': 'fixture-cadence'}, 'ygor')}
            _, confirmation = self.call_route(body=body)
            result = self.runner.run_task('12345', preview_id=confirmation['preview_id'],
                                          campaign_approval_id=confirmation['campaign_approval_id'])
        self.assertEqual('completed', result['status'], result)
        self.assertEqual('COMPLETED', transport.status)
        self.assertIn('connectsafely:receipt-1', transport.body)
        self.assertTrue(all('hs_latest_sequence_enrolled' not in call[1] for call in transport.calls))


if __name__ == '__main__': unittest.main()
