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
            direct_client=SimpleNamespace(account_id='acct', expected_public_id='owner', account_status=Mock()))))
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


if __name__ == '__main__':
    unittest.main()
