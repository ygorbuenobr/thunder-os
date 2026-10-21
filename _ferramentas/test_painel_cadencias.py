"""Read-only cadence route and presentation boundaries; no live provider calls."""
import unittest
import os
import json
from pathlib import Path
import tempfile
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import painel_servidor as panel
from thunder_os.campaign_registry import CampaignRegistry
from thunder_os.db import Repository
from test_thunder_os_campaign_registry import config
from test_thunder_os_cadence_contact_campaign import ContactProvider, contact


class CadencePanelTests(unittest.TestCase):
    def test_external_email_default_status_and_disabled_ingress(self):
        handler = object.__new__(panel.Handler)
        handler.server = SimpleNamespace(thunder_service=object())
        handler.client_address = ('127.0.0.1',1234)
        handler.headers = {'X-Authenticated-User':'ygor'}
        handler.command = 'GET'
        handler.responder = lambda status,body:(status,body)
        handler.corpo = Mock(side_effect=AssertionError('disabled ingress must not parse body'))
        url = urlsplit('/api/os/v1/integrations/external-email/status')
        with patch('http.client.HTTPSConnection',side_effect=AssertionError('network forbidden')):
            status,body = handler.thunder_api(url)
            self.assertEqual(status,200)
            for key in ('automatic_reader_enabled','crm_import_enabled','webhook_enabled'):
                self.assertIs(body[key],False)
            handler.command = 'POST'
            handler.headers = {}
            status,body = handler.thunder_api(urlsplit('/api/os/v1/integrations/instantly/events'))
            self.assertEqual(status,410)
        handler.corpo.assert_not_called()
        handler.command = 'GET'
        handler.headers = {'X-Authenticated-User':'claude'}
        with self.assertRaises(panel.Erro) as error:
            handler.thunder_api(url)
        self.assertEqual(error.exception.status,403)

    def test_external_email_simulation_requires_backend_authority(self):
        handler = object.__new__(panel.Handler)
        handler.server = SimpleNamespace(thunder_service=object(),thunder_nonces={},thunder_origin='https://painel.r3xdigital.tech')
        handler.client_address = ('127.0.0.1',1234)
        handler.headers = {'X-Authenticated-User':'ygor'}
        handler.command = 'POST'
        handler.corpo = Mock(side_effect=AssertionError('unauthorized body read'))
        with self.assertRaises(panel.Erro) as error:
            handler.thunder_api(urlsplit('/api/os/v1/integrations/instantly/simulate'))
        self.assertEqual(error.exception.status,401)
        handler.corpo.assert_not_called()

    def test_membership_actual_http_dispatch_blocks_mutations(self):
        reader = Mock()
        reader.inspect.return_value = {'status':'ready','mutation_enabled':False}
        server = ThreadingHTTPServer(('127.0.0.1',0),panel.Handler)
        server.banco = object()
        server.thunder_service = object()
        server.thunder_campaign_membership = reader
        thread = Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            for method in ('GET','POST','PATCH','DELETE','PUT'):
                with self.subTest(method=method):
                    connection = HTTPConnection('127.0.0.1',server.server_port,timeout=3)
                    try:
                        connection.request(method,'/api/os/v1/campaign-membership/123/BR-APOLLO-01',headers={'X-Authenticated-User':'ygor'})
                        response = connection.getresponse()
                        self.assertEqual(response.status,200 if method=='GET' else 405)
                        payload = json.loads(response.read())
                        if method=='GET':
                            self.assertFalse(payload['mutation_enabled'])
                    finally:
                        connection.close()
            reader.inspect.assert_called_once_with('123','BR-APOLLO-01')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_membership_route_is_readonly_and_human_scoped(self):
        reader = Mock()
        reader.inspect.return_value = {'status':'blocked','reason':'lead_occupied','mutation_enabled':False}
        handler = object.__new__(panel.Handler)
        handler.server = SimpleNamespace(thunder_service=object(), thunder_campaign_membership=reader)
        handler.client_address = ('127.0.0.1',1234)
        handler.headers = {'X-Authenticated-User':'ygor'}
        handler.command = 'GET'
        handler.responder = lambda status,body:(status,body)
        url = urlsplit('/api/os/v1/campaign-membership/123/BR-APOLLO-01')
        self.assertEqual(handler.thunder_api(url)[1]['reason'],'lead_occupied')
        reader.inspect.assert_called_once_with('123','BR-APOLLO-01')
        reader.reset_mock()
        for method in ('POST','PATCH','DELETE','PUT'):
            handler.command = method
            with self.assertRaises(panel.Erro) as error:
                handler.thunder_api(url)
            self.assertEqual(error.exception.status,405)
        reader.inspect.assert_not_called()
        handler.command = 'GET'
        handler.headers = {'X-Authenticated-User':'claude'}
        with self.assertRaises(panel.Erro) as error:
            handler.thunder_api(url)
        self.assertEqual(error.exception.status,403)

    def route(self, path='/api/os/v1/cadencias', actor='ygor', method='GET', console=True):
        self.reader = Mock()
        self.reader.snapshot.return_value = {'campaigns': [], 'coverage': {'complete': True}}
        self.reader.timeline.return_value = {'contact_id': '123', 'events': []}
        handler = object.__new__(panel.Handler)
        handler.server = SimpleNamespace(thunder_service=object())
        if console:
            handler.server.thunder_cadence_readonly = self.reader
        handler.command = method
        handler.client_address = ('127.0.0.1', 1234)
        handler.headers = {'X-Authenticated-User': actor} if actor else {}
        handler.responder = lambda status, body: (status, body)
        return handler.thunder_api(urlsplit(path))

    def test_authenticated_snapshot_and_contact(self):
        self.assertEqual(self.route()[0], 200)
        self.reader.snapshot.assert_called_once_with()
        self.assertEqual(self.route('/api/os/v1/cadencias/contacts/123')[1]['contact_id'], '123')
        self.reader.timeline.assert_called_once_with('123')

    def test_missing_identity_and_other_actor_are_denied(self):
        for actor, status in ((None, 401), ('hermes', 401), ('other', 401), ('claude', 403)):
            with self.subTest(actor=actor), self.assertRaises(panel.Erro) as error:
                self.route(actor=actor)
            self.assertEqual(error.exception.status, status)
            self.reader.snapshot.assert_not_called()

    def test_mutation_methods_are_denied(self):
        for method in ('POST', 'PATCH', 'DELETE', 'PUT'):
            with self.subTest(method=method), self.assertRaises(panel.Erro) as error:
                self.route(method=method)
            self.assertEqual(error.exception.status, 405)
            self.reader.snapshot.assert_not_called()

    def test_query_invalid_contact_and_missing_reader(self):
        for path in ('/api/os/v1/cadencias?owner=1', '/api/os/v1/cadencias/contacts/name', '/api/os/v1/cadencias/contacts/0',
                     '/api/os/v1/cadencias/contacts/123/execute'):
            with self.subTest(path=path), self.assertRaises(panel.Erro):
                self.route(path)
            self.reader.snapshot.assert_not_called()
            self.reader.timeline.assert_not_called()
        with self.assertRaises(panel.Erro) as error:
            self.route(console=False)
        self.assertEqual(error.exception.status, 503)

    def test_contact_not_found_and_provider_error_are_sanitized(self):
        for cause, message, status in ((LookupError('private'), 'Contato não encontrado', 404),
                               (RuntimeError('SECRET PROVIDER BODY'), 'Não foi possível', 503)):
            handler = object.__new__(panel.Handler)
            reader = Mock()
            reader.timeline.side_effect = cause
            handler.server = SimpleNamespace(thunder_service=object(), thunder_cadence_readonly=reader)
            handler.command = 'GET'
            handler.client_address = ('127.0.0.1', 1234)
            handler.headers = {'X-Authenticated-User': 'ygor'}
            with self.assertRaisesRegex(panel.Erro, message) as error:
                handler.thunder_api(urlsplit('/api/os/v1/cadencias/contacts/123'))
            self.assertNotIn('SECRET', str(error.exception))
            self.assertEqual(error.exception.status, status)

    def test_presentation_has_readonly_refresh_and_no_action_controls(self):
        html = Path(__file__).with_name('painel_modelo.html').read_text()
        section = html.split('<main id="sec-cadencias"', 1)[1].split('</main>', 1)[0]
        self.assertIn('Sem vínculo', section)
        self.assertIn('cad-read-events', section)
        self.assertNotIn('data-os-approve', section)
        self.assertNotIn('data-os-cancel', section)
        self.assertIn("document.body.dataset.aba==='cadencias'", html)
        self.assertIn('60000', html)

    def test_main_wires_existing_repository_registry_into_read_console(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            brain, database = root / 'brain', root / 'state' / 'panel.db'
            brain.mkdir()
            database.parent.mkdir()
            registry = CampaignRegistry(Repository(database))
            registry.register({**config(), 'campaign_id': 'REGISTERED', 'platform': 'codex_local'}, 'ygor', 0)
            registry.register({**config(), 'campaign_id': 'APOLLO-TEST', 'platform': 'apollo'}, 'ygor', 0)
            before = registry.repo.list_state('campaign_config')
            server = SimpleNamespace(serve_forever=Mock())
            provider = ContactProvider()
            provider.tasks = []
            provider.contacts['100'] = contact(campaign='APOLLO-TEST')
            binding = {'campaign_id': 'APOLLO-TEST', 'provider': 'apollo', 'campaign_guid': 'test-guid', 'source_id': 'test-app'}
            def read(method, path, body, headers, timeout):
                if path == '/crm/v3/objects/emails/search':
                    self.assertEqual(body['filterGroups'], [])
                    provider.email_links['901'] = ['100']
                    return 200, json.dumps({'results': [{'id': '901', 'properties': {
                        'hs_campaign_guid': 'test-guid', 'hs_engagement_source_id': 'test-app',
                        'hs_email_status': 'SENT', 'hs_email_direction': 'EMAIL', 'hs_timestamp': '2026-10-21T10:00:00Z'}}], 'total': 1}).encode()
                return provider(method, path, body, headers, timeout)
            with patch.dict(os.environ, {'PAINEL_DB': str(database), 'THUNDER_OS_HUBSPOT_READ_ENABLED': 'true',
                                         'THUNDER_OS_CADENCE_ACTIVITY_BINDINGS': json.dumps([binding]),
                                         'THUNDER_EXTERNAL_EFFECTS_ENABLED': 'false'}, clear=True), \
                    patch.object(panel, 'RAIZ', brain), \
                    patch.object(panel, 'ThreadingHTTPServer', return_value=server), \
                    patch.object(panel, 'systemd_hubspot_credential', return_value='synthetic-token'):
                self.assertEqual(panel.main(['servir']), 0)
                console = server.thunder_cadence_readonly
                self.assertIs(console.campaign_registry.repo, server.thunder_service.repository)
                self.assertEqual(console.activity_bindings_loader(), [binding])
                console.adapter.request = read
                console.adapter.sleep = lambda _: None
                handler = object.__new__(panel.Handler)
                handler.server = server
                handler.command = 'GET'
                handler.client_address = ('127.0.0.1', 1234)
                handler.headers = {'X-Authenticated-User': 'ygor'}
                handler.responder = lambda status, body: (status, body)
                status, result = handler.thunder_api(urlsplit('/api/os/v1/cadencias'))
            self.assertEqual(status, 200)
            groups = {group['id']: group for group in result['campaigns']}
            self.assertEqual(groups['REGISTERED']['task_state'], 'empty')
            self.assertEqual(groups['APOLLO-TEST']['metrics']['external_sent'], 1)
            self.assertEqual(groups['APOLLO-TEST']['activities'][0]['provider'], 'apollo')
            self.assertEqual(groups['APOLLO-TEST']['activities'][0]['attribution'], 'contact_current_campaign')
            self.assertIsNone(groups['APOLLO-TEST']['activities'][0]['origin_provider'])
            self.assertEqual(registry.repo.list_state('campaign_config'), before)
            self.assertEqual(registry.repo.effects(), [])
            server.serve_forever.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
