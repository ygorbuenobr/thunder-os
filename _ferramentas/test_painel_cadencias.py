"""Read-only cadence route and presentation boundaries; no live provider calls."""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit

import painel_servidor as panel


class CadencePanelTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
