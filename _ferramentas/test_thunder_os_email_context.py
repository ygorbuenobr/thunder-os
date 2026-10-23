"""Read-only context routes; synthetic data and no network or credential access."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import painel_servidor as panel
from thunder_os.seqd_client import MAILBOX, SEQDClient, SEQDError
from thunder_os.seqd_console import SEQDConsole
from thunder_os.service import AuthorizationError


def context():
    return {'mailbox': MAILBOX, 'status': 'ok', 'thread': {
        'id': 'abc123', 'context_only': True, 'receipt_eligible': False,
        'complete': True, 'truncated': False, 'coverage': {'partial': False},
        'messages': [
            {'id': 'aa1', 'from': MAILBOX, 'to': 'lead@example.invalid',
             'bodies': {'text': 'Previous commercial question', 'html': None}},
            {'id': 'aa2', 'from': 'lead@example.invalid', 'to': MAILBOX,
             'bodies': {'text': None, 'html': '<p>Please arrange a meeting</p>'}},
        ]}}


class EmailContextTests(unittest.TestCase):
    def setUp(self):
        for name in ('socket.socket', 'socket.create_connection'):
            guard = patch(name, side_effect=AssertionError('network forbidden'))
            guard.start()
            self.addCleanup(guard.stop)
        self.calls = []
        self.response = context()

        def request(method, path, body, headers, timeout):
            self.calls.append((method, path, body))
            return 200, json.dumps(self.response).encode()

        self.client = SEQDClient(credential_loader=lambda: 'x'*48, request=request)

    def test_conversation_reads_only_exact_thread_without_writing(self):
        observed = self.client.conversation('abc123')
        self.assertEqual([('GET', '/v1/threads/abc123', None)], self.calls)
        self.assertEqual('Previous commercial question', observed['thread']['messages'][0]['bodies']['text'])
        self.assertEqual('<p>Please arrange a meeting</p>', observed['thread']['messages'][1]['bodies']['html'])
        self.assertFalse(observed['thread']['receipt_eligible'])

    def test_invalid_thread_id_is_blocked_before_transport(self):
        for identifier in ('', 'ABC', '../abc', 'abc?x=1', 'x'*65, None, 123):
            with self.subTest(identifier=identifier), self.assertRaises(SEQDError):
                self.client.conversation(identifier)
        self.assertEqual([], self.calls)

    def test_wrong_account_thread_or_context_evidence_is_rejected(self):
        for change in ('account', 'id', 'messages', 'receipt', 'context'):
            self.response = context()
            if change == 'account': self.response['mailbox'] = 'other@example.invalid'
            if change == 'id': self.response['thread']['id'] = 'def456'
            if change == 'messages': self.response['thread']['messages'] = 'not a list'
            if change == 'receipt': self.response['thread']['receipt_eligible'] = True
            if change == 'context': self.response['thread']['context_only'] = False
            with self.subTest(change=change), self.assertRaises(SEQDError):
                self.client.conversation('abc123')

    def test_partial_context_remains_visibly_partial(self):
        self.response['thread'].update(complete=False, truncated=True,
                                      coverage={'partial': True, 'reason': 'bounded_context'})
        result = self.client.conversation('abc123')
        self.assertFalse(result['thread']['complete'])
        self.assertTrue(result['thread']['coverage']['partial'])

    def test_list_limits_and_cursor_remain_get_only(self):
        self.response = {'mailbox': MAILBOX, 'status': 'ok', 'messages': [], 'next_cursor': 'next'}
        self.client.messages(limit=10, cursor='opaque==')
        self.assertEqual([('GET', '/v1/messages?limit=10&cursor=opaque%3D%3D', None)], self.calls)
        for limit in (0, 11, True, '10'):
            with self.subTest(limit=limit), self.assertRaises(SEQDError): self.client.messages(limit=limit)
        self.assertEqual(1, len(self.calls))

    def route(self, actor='ygor', configured=True):
        port = Mock()
        port.conversation.return_value = context()
        console = SEQDConsole(SimpleNamespace(), port, ledger=object(), read_enabled=configured)
        handler = object.__new__(panel.Handler)
        handler.server = SimpleNamespace(thunder_service=SimpleNamespace(repository=object()),
                                         thunder_seqd_console=console)
        handler.command = 'GET'
        handler.client_address = ('127.0.0.1', 1234)
        handler.headers = {'X-Authenticated-User': actor}
        handler.responder = lambda status, body: (status, body)
        return handler, port

    def test_panel_thread_route_uses_existing_ygor_gate(self):
        handler, port = self.route()
        status, observed = handler.thunder_api(urlsplit('/api/os/v1/integrations/seqd/threads/abc123'))
        self.assertEqual(200, status)
        self.assertEqual(context(), observed)
        port.conversation.assert_called_once_with('abc123')

    def test_panel_other_actor_and_disabled_read_never_call_provider(self):
        handler, port = self.route(actor='codex')
        with self.assertRaises(AuthorizationError):
            handler.thunder_api(urlsplit('/api/os/v1/integrations/seqd/threads/abc123'))
        port.conversation.assert_not_called()
        handler, port = self.route(configured=False)
        with self.assertRaises(SEQDError):
            handler.thunder_api(urlsplit('/api/os/v1/integrations/seqd/threads/abc123'))
        port.conversation.assert_not_called()


if __name__ == '__main__':
    unittest.main()
