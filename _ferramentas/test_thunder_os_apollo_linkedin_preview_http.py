"""Route stays authenticated and cannot activate any provider effects."""
import json
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

import painel_servidor as panel


class ApolloLinkedinPreviewHttpTests(unittest.TestCase):
    def setUp(self):
        self.peer = patch.object(panel.Handler, 'trusted_confirmation_peer', return_value=True)
        self.peer.start()
        self.addCleanup(self.peer.stop)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), panel.Handler)
        self.server.banco = SimpleNamespace()
        self.server.thunder_service = Mock()
        self.server.thunder_apollo_linkedin_preview = SimpleNamespace(read=Mock(return_value={
            'status': 'blocked', 'execution_enabled': False, 'dry_run': True}))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method='GET', actor='ygor', query=''):
        conn = HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        self.addCleanup(conn.close)
        conn.request(method, '/api/os/v1/integrations/apollo/linkedin-preview' + query,
                     headers={'X-Authenticated-User': actor})
        response = conn.getresponse()
        return response.status, json.loads(response.read())

    def test_read_is_authenticated_queryless_and_does_not_mutate(self):
        self.assertEqual(self.request(actor='codex')[0], 403)
        self.assertEqual(self.request(query='?task=other')[0], 405)
        self.assertNotEqual(self.request(method='POST')[0], 200)
        status, body = self.request()
        self.assertEqual(status, 200)
        self.assertFalse(body['execution_enabled'])
        self.assertTrue(body['dry_run'])
        self.server.thunder_apollo_linkedin_preview.read.assert_called_once()

    def test_missing_configuration_is_visible_and_disabled(self):
        self.server.thunder_apollo_linkedin_preview = None
        status, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(body['blockers'], ['configuration_missing'])
        self.assertFalse(body['execution_enabled'])

    def test_startup_is_offline_and_requires_complete_matching_config(self):
        server = SimpleNamespace(thunder_connectsafely_app=SimpleNamespace(provider=SimpleNamespace(
            direct_client=SimpleNamespace(account_id='acct', expected_public_id='owner', account_status=Mock()))),
            thunder_apollo_linkedin_read_identity=SimpleNamespace(account_id='acct', public_id='owner'))
        loader = Mock(side_effect=AssertionError('must not read at startup'))
        self.assertIsNone(panel.configure_apollo_linkedin_preview(server, loader, {}))
        self.assertIsNone(panel.configure_apollo_linkedin_preview(server, loader, {'THUNDER_APOLLO_LI_TEAM': 'team'}))
        self.assertEqual(server.thunder_apollo_linkedin_preview_error, 'configuration_incomplete')
        env = dict(zip(('THUNDER_APOLLO_LI_TEAM','THUNDER_APOLLO_LI_OWNER','THUNDER_APOLLO_LI_API_ACTOR',
            'THUNDER_APOLLO_LI_SEQUENCE','THUNDER_APOLLO_LI_TASK',
            'THUNDER_APOLLO_LI_ACCOUNT','THUNDER_APOLLO_LI_PUBLIC_ID'),
            ('team','ownerid','api_actor','sequence','task','acct','owner')))
        panel.configure_apollo_linkedin_preview(server, loader, env)
        loader.assert_not_called()
        server.thunder_connectsafely_app.provider.direct_client.account_status.assert_not_called()
        env['THUNDER_APOLLO_LI_ACCOUNT'] = 'other'
        self.assertIsNone(panel.configure_apollo_linkedin_preview(server, loader, env))
        self.assertEqual(server.thunder_apollo_linkedin_preview_error, 'account_mismatch')

    def test_panel_uses_only_credential_free_broker_client(self):
        from thunder_os.connectsafely_read_broker_client import AccountStatusBrokerClient
        server = SimpleNamespace()
        account = 'a' * 24
        env = {'THUNDER_APOLLO_LI_BROKER_SOCKET': '/tmp/thunder-test/broker.sock',
               'THUNDER_APOLLO_LI_BROKER_UID': '12001', 'THUNDER_APOLLO_LI_COMPANY': 'company',
               'THUNDER_APOLLO_LI_ACCOUNT': account, 'THUNDER_APOLLO_LI_PUBLIC_ID': 'sender',
               'THUNDER_APOLLO_LI_TEAM': 'team', 'THUNDER_APOLLO_LI_OWNER': 'owner',
               'THUNDER_APOLLO_LI_API_ACTOR': 'actor', 'THUNDER_APOLLO_LI_SEQUENCE': 'sequence',
               'THUNDER_APOLLO_LI_TASK': 'task'}
        with patch.object(AccountStatusBrokerClient, 'account_status', return_value={
                'id': account, 'publicId': 'sender', 'available': True,
                'source': 'connectsafely', 'write_authorized': False}) as get_status:
            identity = panel.configure_apollo_linkedin_read_broker(server, env)
            preview = panel.configure_apollo_linkedin_preview(server, lambda: 'fake', env)
            self.assertIsNotNone(identity)
            self.assertFalse(hasattr(identity, 'send'))
            self.assertFalse(hasattr(identity, 'credential'))
            self.assertTrue(preview._status()['available'])
            get_status.assert_called_once_with()

    def test_broker_configuration_fails_closed_without_fallback(self):
        server = SimpleNamespace()
        self.assertIsNone(panel.configure_apollo_linkedin_read_broker(server, {}))
        self.assertIsNone(server.thunder_apollo_linkedin_read_identity)
        env = {'THUNDER_APOLLO_LI_BROKER_SOCKET':'/tmp/broker.sock', 'THUNDER_APOLLO_LI_BROKER_UID':'0',
               'THUNDER_APOLLO_LI_COMPANY':'c', 'THUNDER_APOLLO_LI_ACCOUNT':'a'*24,
               'THUNDER_APOLLO_LI_PUBLIC_ID':'sender'}
        self.assertIsNone(panel.configure_apollo_linkedin_read_broker(server, env))

    def test_in_process_provider_wiring_and_http_reads_never_send(self):
        account = 'a' * 24
        direct = SimpleNamespace(account_id=account, expected_public_id='sender',
            account_status=Mock(return_value={'id': account, 'publicId': 'sender',
                'available': True, 'provider_extra': 'not returned'}), send=Mock())
        self.server.thunder_connectsafely_app = SimpleNamespace(provider=SimpleNamespace(direct_client=direct))
        self.server.thunder_service = SimpleNamespace(apollo_linkedin_store_ready=lambda: True,
            list_apollo_linkedin_reservations=lambda: [], list_apollo_linkedin_account_locks=lambda: [])
        env = {'THUNDER_APOLLO_LI_COMPANY': 'company', 'THUNDER_APOLLO_LI_TEAM': 'team',
            'THUNDER_APOLLO_LI_OWNER': 'owner', 'THUNDER_APOLLO_LI_API_ACTOR': 'actor',
            'THUNDER_APOLLO_LI_SEQUENCE': 'sequence', 'THUNDER_APOLLO_LI_TASK': 'test-task',
            'THUNDER_APOLLO_LI_ACCOUNT': account, 'THUNDER_APOLLO_LI_PUBLIC_ID': 'sender',
            'THUNDER_APOLLO_LI_BINDINGS': json.dumps([{'company_id': 'company', 'team_id': 'team',
                'campaign_id': 'sequence', 'account_id': account, 'public_id': 'sender',
                'credential_ref': 'reference', 'owner_ids': ['owner']}])}
        task = {'id': 'test-task', 'type': 'linkedin_step_connect', 'status': 'scheduled',
            'user_id': 'owner', 'emailer_campaign_id': 'sequence', 'contact_id': 'test-contact',
            'contact': {'id': 'test-contact', 'name': 'Test', 'linkedin_url': 'https://linkedin.com/in/test-person/'},
            'linkedin_emailer_template': {'body_text': 'Hello'}}
        calls = []
        def request(method, path, body, headers, timeout):
            calls.append((method, path))
            if path.endswith('api_profile'):
                data = {'user': {'id': 'actor', 'team_id': 'team'}}
            elif path.endswith('/search'):
                data = {'tasks': [task], 'pagination': {'page': 1, 'per_page': 100,
                    'total_entries': 1, 'total_pages': 1}}
            elif path.endswith('/test-task'):
                data = {'task': task}
            else:
                raise AssertionError('unexpected endpoint')
            return 200, json.dumps(data).encode()
        loader = Mock(return_value='fake-token')
        identity = panel.configure_apollo_linkedin_read_broker(self.server, env)
        preview = panel.configure_apollo_linkedin_preview(self.server, loader, env)
        attention = panel.configure_apollo_linkedin_attention(self.server, loader, env)
        self.assertIsNotNone(identity)
        self.assertFalse(hasattr(identity, 'send'))
        self.assertFalse(hasattr(identity, 'direct_client'))
        self.assertFalse(hasattr(identity, '__getattr__'))
        loader.assert_not_called()
        direct.account_status.assert_not_called()
        preview._request = request
        attention.reader._request = request
        status, body = self.request()
        self.assertEqual(200, status)
        self.assertEqual('in_process_accepted', body['read_isolation'])
        self.assertTrue(body['account']['verified'])
        self.assertFalse(body['execution_enabled'])
        conn = HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        self.addCleanup(conn.close)
        conn.request('GET', '/api/os/v1/integrations/apollo/linkedin-attention',
                     headers={'X-Authenticated-User': 'ygor'})
        response = conn.getresponse()
        attention_body = json.loads(response.read())
        self.assertEqual(200, response.status)
        self.assertEqual('in_process_accepted', attention_body['read_isolation'])
        self.assertEqual('execution_binding_unverified', attention_body['items'][0]['reason'])
        self.assertFalse(attention_body['items'][0]['execution_enabled'])
        self.assertEqual('test-task', attention_body['items'][0]['task_id'])
        self.assertTrue(calls)
        direct.send.assert_not_called()

    def test_in_process_provider_rejects_empty_company_and_account_mismatch(self):
        account = 'a' * 24
        direct = SimpleNamespace(account_id=account, expected_public_id='sender', account_status=Mock(), send=Mock())
        server = SimpleNamespace(thunder_connectsafely_app=SimpleNamespace(provider=SimpleNamespace(direct_client=direct)))
        env = {'THUNDER_APOLLO_LI_ACCOUNT': account, 'THUNDER_APOLLO_LI_PUBLIC_ID': 'sender'}
        self.assertIsNone(panel.configure_apollo_linkedin_read_broker(server, env))
        self.assertIsNone(server.thunder_apollo_linkedin_read_isolation)
        env['THUNDER_APOLLO_LI_COMPANY'] = ''
        self.assertIsNone(panel.configure_apollo_linkedin_read_broker(server, env))
        env['THUNDER_APOLLO_LI_COMPANY'] = 'company'
        env['THUNDER_APOLLO_LI_ACCOUNT'] = 'b' * 24
        self.assertIsNone(panel.configure_apollo_linkedin_read_broker(server, env))
        self.assertIsNone(server.thunder_apollo_linkedin_read_identity)
        env['THUNDER_APOLLO_LI_ACCOUNT'] = account
        env['THUNDER_APOLLO_LI_PUBLIC_ID'] = 'wrong'
        self.assertIsNone(panel.configure_apollo_linkedin_read_broker(server, env))
        direct.account_status.assert_not_called()
        direct.send.assert_not_called()

    def test_broker_rejects_panel_with_send_provider(self):
        account = 'a' * 24
        env = {'THUNDER_APOLLO_LI_BROKER_SOCKET':'/tmp/broker.sock', 'THUNDER_APOLLO_LI_BROKER_UID':'12001',
               'THUNDER_APOLLO_LI_COMPANY':'c', 'THUNDER_APOLLO_LI_ACCOUNT':account,
               'THUNDER_APOLLO_LI_PUBLIC_ID':'sender'}
        server = SimpleNamespace(thunder_connectsafely_app=SimpleNamespace(provider=object()))
        self.assertIsNone(panel.configure_apollo_linkedin_read_broker(server, env))
        self.assertIsNone(server.thunder_apollo_linkedin_read_identity)


if __name__ == '__main__':
    unittest.main()
