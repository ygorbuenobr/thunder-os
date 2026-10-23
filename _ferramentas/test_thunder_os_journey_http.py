"""Jornada boundary: isolated HTTP loopback, fake readers, no provider network."""
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
import json
import threading
import time
import unittest
from unittest.mock import Mock

import painel_servidor as panel
from thunder_os.today_readonly import TodayError


class JourneyHTTPTests(unittest.TestCase):
    def setUp(self):
        self.journey = Mock()
        self.journey.snapshot.return_value = {'read_only': True, 'stages': []}
        self.journey.detail.return_value = {'id': 'detail-fixture', 'blocks': []}
        self.journey.account_list.return_value = {'accounts': [], 'limitations': []}
        self.journey.account_detail.return_value = {'id': 'account-fixture', 'tabs': {}}
        self.accounts = Mock()
        self.accounts.list_accounts.return_value = {'accounts': [], 'limitations': []}
        self.accounts.account.return_value = {'id': 'account-fixture', 'tabs': {}}
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), panel.Handler)
        self.server.banco = SimpleNamespace()
        self.server.thunder_service = SimpleNamespace()
        self.server.thunder_journey_readonly = self.journey
        self.server.thunder_account_cadence_readonly = self.accounts
        self.server.thunder_origin = 'https://panel.example.invalid'
        self.server.thunder_nonces = {}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, path, *, actor='ygor', method='GET', body=None, nonce=False, origin=None):
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        self.addCleanup(connection.close)
        headers = {'X-Authenticated-User': actor}
        if nonce:
            token = str(time.monotonic_ns())
            self.server.thunder_nonces[token] = (actor, time.monotonic() + 30)
            headers.update({'X-Thunder-Nonce': token, 'Origin': origin or self.server.thunder_origin})
        if body is not None:
            headers['Content-Type'] = 'application/json'
        connection.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    def step_service(self):
        cadence = {'id': 'cadence-fixture', 'version': 4,
            'execution_context': {'campaign_id': 'BR-TEST'},
            'steps': [{'id': 'D0', 'channel': 'email', 'content': 'Original', 'subject': 'Topic'},
                      {'id': 'D3', 'channel': 'email', 'content': 'Other', 'subject': 'Topic'}]}
        self.service = Mock()
        self.service.repository.get_state.return_value = cadence
        self.service.cadence_review_hash.return_value = 'a' * 64
        self.service.review_step.return_value = {'can_approve': False, 'blockers': ['fixture_hold']}
        self.service.approve_cadence.return_value = {'status': 'active'}
        self.service.revise_cadence.return_value = {'status': 'ready_for_review'}
        self.server.thunder_service = self.service
        return cadence

    def test_J06_step_approval_scopes_one_step_without_dispatch(self):
        self.step_service()
        body = {'cadence_id': 'cadence-fixture', 'step_id': 'D0', 'expected_version': 4,
                'review_hash': 'a' * 64, 'execution_confirmation': {'fixture': 'exact reviewed envelope'}}
        status, value = self.request('/api/os/v1/journey/steps/approve', method='POST', body=body, nonce=True)
        self.assertEqual(status, 200)
        self.assertFalse(value['sent'])
        self.assertIsNone(value['receipt'])
        self.assertEqual(value['status'], 'approved')
        self.assertEqual(self.service.approve_cadence.call_args.kwargs['step_ids'], ['D0'])
        self.service.dispatch_step.assert_not_called()
        self.service.control_agent.assert_not_called()
        status, _ = self.request('/api/os/v1/journey/steps/approve', method='POST',
            body={**body, 'step_ids': ['D0', 'D3']}, nonce=True)
        self.assertEqual(status, 400)

    def test_J06_authenticated_review_returns_exact_step_and_never_dispatches(self):
        self.step_service()
        expected = {'cadence_id': 'cadence-fixture', 'step_id': 'D0',
                    'content': 'Original', 'subject': 'Topic', 'expected_version': 4,
                    'review_hash': 'a' * 64, 'can_approve': True, 'blockers': [],
                    'context_hash': 'b' * 64,
                    'execution_confirmation': {'fixture': 'exact D0 envelope'}}
        self.service.review_step.return_value = expected
        status, value = self.request('/api/os/v1/journey/steps/review', method='POST',
            body={'cadence_id': 'cadence-fixture', 'step_id': 'D0'}, nonce=True)
        self.assertEqual(status, 200)
        self.assertEqual(value, expected)
        self.service.review_step.assert_called_once_with('cadence-fixture', 'D0', 'ygor')
        self.service.approve_cadence.assert_not_called()
        self.service.dispatch_step.assert_not_called()
        self.service.control_agent.assert_not_called()

    def test_J06_authenticated_discard_scopes_exact_step_without_dispatch(self):
        self.step_service()
        self.service.discard_step.return_value = {'id': 'cadence-fixture',
            'version': 5, 'status': 'ready_for_review'}
        body = {'cadence_id': 'cadence-fixture', 'step_id': 'D3',
                'expected_version': 4, 'review_hash': 'a' * 64,
                'reason': 'Human declined this exact touch'}
        status, value = self.request('/api/os/v1/journey/steps/discard', method='POST',
            body=body, nonce=True)
        self.assertEqual(status, 200)
        self.assertTrue(value['applied'])
        self.assertFalse(value['sent'])
        self.assertEqual(value['cadence']['status'], 'ready_for_review')
        self.service.discard_step.assert_called_once_with('cadence-fixture', 'D3', 'ygor',
            expected_version=4, review_hash='a' * 64, reason='Human declined this exact touch')
        self.service.approve_cadence.assert_not_called()
        self.service.dispatch_step.assert_not_called()
        self.service.control_agent.assert_not_called()

    def test_J06_step_actions_require_nonce_origin_actor_and_brazil(self):
        cadence = self.step_service()
        body = {'cadence_id': 'cadence-fixture', 'step_id': 'D0'}
        path = '/api/os/v1/journey/steps/review'
        self.assertEqual(self.request(path, method='POST', body=body)[0], 401)
        self.assertEqual(self.request(path, method='POST', body=body, nonce=True, origin='https://other.invalid')[0], 401)
        self.assertEqual(self.request(path, actor='codex', method='POST', body=body, nonce=True)[0], 403)
        self.service.review_step.assert_not_called()
        cadence['execution_context']['campaign_id'] = 'MX27-CDMX'
        self.assertEqual(self.request(path, method='POST', body=body, nonce=True)[0], 404)
        self.service.review_step.assert_not_called()

    def test_J06_revision_cannot_change_recipient_or_other_step(self):
        self.step_service()
        body = {'cadence_id': 'cadence-fixture', 'step_id': 'D0', 'expected_version': 4,
                'review_hash': 'a' * 64, 'content': 'Changed exact text', 'subject': 'New topic'}
        status, value = self.request('/api/os/v1/journey/steps/revise', method='POST', body=body, nonce=True)
        self.assertEqual(status, 200)
        self.assertFalse(value['sent'])
        revised = self.service.revise_cadence.call_args.args[1]
        self.assertEqual(revised[0]['content'], 'Changed exact text')
        self.assertEqual(revised[1]['content'], 'Other')
        self.assertEqual(self.request('/api/os/v1/journey/steps/revise', method='POST',
            body={**body, 'recipient': 'unauthorized@example.invalid'}, nonce=True)[0], 400)

    def test_J11_routes_call_only_readers_with_valid_campaign(self):
        for path, expected in [('/api/os/v1/journey', self.journey.snapshot),
                ('/api/os/v1/journey/details/detail-fixture', self.journey.detail),
                ('/api/os/v1/journey/accounts', self.journey.account_list),
                ('/api/os/v1/journey/accounts/account-fixture', self.journey.account_detail)]:
            with self.subTest(path=path):
                status, body = self.request(path + '?campaign=BR-APOLLO-01')
                self.assertEqual(status, 200)
                self.assertEqual(expected.call_args.kwargs, {'campaign': 'BR-APOLLO-01'})
                self.assertNotIn('sent', body)

    def test_J11_non_ygor_and_missing_actor_cannot_read_customer_data(self):
        for actor, expected in [('', 401), ('codex', 403), ('hermes', 401)]:
            with self.subTest(actor=actor):
                self.assertEqual(self.request('/api/os/v1/journey', actor=actor)[0], expected)
        self.journey.snapshot.assert_not_called()

    def test_J11_domain_and_namespaced_ids_remain_literal_not_file_paths(self):
        status, _ = self.request('/api/os/v1/journey/accounts/company.example.invalid')
        self.assertEqual(status, 200)
        self.journey.account_detail.assert_called_once_with('company.example.invalid', campaign=None)
        status, _ = self.request('/api/os/v1/journey/accounts/account%3Acompany.example.invalid')
        self.assertEqual(status, 200)
        self.journey.account_detail.assert_called_with('account:company.example.invalid', campaign=None)
        for invalid in ('..%2Fprivate', 'account%252Fprivate', '%2Fprivate'):
            self.assertEqual(self.request('/api/os/v1/journey/accounts/' + invalid)[0], 404)
        cadence = self.step_service()
        cadence['id'] = 'cadence:company.example.invalid'
        status, _ = self.request('/api/os/v1/journey/steps/review', method='POST', nonce=True,
            body={'cadence_id': 'cadence:company.example.invalid', 'step_id': 'D0:email'})
        self.assertEqual(status, 200)
        self.service.review_step.assert_called_once_with('cadence:company.example.invalid', 'D0:email', 'ygor')
        status, _ = self.request('/api/os/v1/journey/steps/review', method='POST', nonce=True,
            body={'cadence_id': '../private', 'step_id': 'D0:email'})
        self.assertEqual(status, 400)

    def test_J11_mutations_and_unknown_filters_are_rejected(self):
        for method in ['POST', 'PATCH', 'PUT', 'DELETE']:
            self.assertEqual(self.request('/api/os/v1/journey', method=method)[0], 405)
        for query in ['?refresh=1', '?campaign=', '?campaign=A&campaign=B', '?campaign=../../private']:
            self.assertEqual(self.request('/api/os/v1/journey' + query)[0], 400)
        self.journey.snapshot.assert_not_called()

    def test_J11_unknown_path_and_missing_reader_visibly_fail(self):
        self.assertEqual(self.request('/api/os/v1/journey/secrets')[0], 404)
        self.journey.detail.side_effect = ValueError('private exception data')
        status, body = self.request('/api/os/v1/journey/details/unknown')
        self.assertEqual(status, 404)
        self.assertNotIn('private exception', str(body))
        self.journey.detail.side_effect = TodayError('journey_detail_stale', 409)
        self.assertEqual(self.request('/api/os/v1/journey/details/unknown')[0], 409)
        self.server.thunder_journey_readonly = None
        self.assertEqual(self.request('/api/os/v1/journey')[0], 503)


if __name__ == '__main__':
    unittest.main()
