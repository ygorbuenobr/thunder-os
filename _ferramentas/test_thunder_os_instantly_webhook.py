"""Instantly simulation and canonical provider readback over synthetic HTTP."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

from thunder_os.external_email_import import INSTANTLY_CAMPAIGN, INSTANTLY_WORKSPACE
from thunder_os.instantly_webhook import (HEADER_NAME, InstantlyReadOnly, InstantlyWebhook,
                                          contract_status, dry_run)


def payload(kind='email_sent'):
    return {'workspace': INSTANTLY_WORKSPACE, 'campaign_id': INSTANTLY_CAMPAIGN,
            'campaign_name': 'Synthetic Pavlack', 'timestamp': '2026-10-21T12:00:00Z',
            'event_type': kind, 'email_id': '00000000-0000-4000-8000-000000000001',
            'lead_email': 'pavlack@example.invalid', 'email_account': 'sender@example.invalid',
            'email_text': 'Untrusted webhook text.'}


def canonical(kind='email_sent', *, auto=0):
    outgoing = kind == 'email_sent'
    return {'id': payload()['email_id'], 'organization_id': INSTANTLY_WORKSPACE,
            'campaign_id': INSTANTLY_CAMPAIGN, 'lead': 'pavlack@example.invalid',
            'eaccount': 'sender@example.invalid', 'ue_type': 1 if outgoing else 2,
            'is_auto_reply': auto, 'subject': 'Canonical synthetic',
            'body': {'text': 'Provider read, synthetic only.'},
            'timestamp_email': '2026-10-21T11:00:00Z',
            'from_address_email': 'sender@example.invalid' if outgoing else 'pavlack@example.invalid',
            'to_address_email_list': 'pavlack@example.invalid' if outgoing else 'sender@example.invalid'}


class InstantlyWebhookTests(unittest.TestCase):
    def handler(self, row=None):
        calls = []
        def request(method, path, body, headers, timeout):
            calls.append((method, path, body))
            result = {'id': INSTANTLY_WORKSPACE} if path == '/api/v2/workspaces/current' else row or canonical()
            return 200, json.dumps(result).encode()
        port = InstantlyReadOnly(lambda: 'synthetic-key', request=request)
        return InstantlyWebhook(lambda: 'synthetic-secret', provider=port, enabled=True), calls

    def test_disabled_before_secret_or_provider(self):
        secret, provider = Mock(), Mock()
        class Unreadable:
            def __getattribute__(self, name):
                raise AssertionError('must not inspect input while disabled')
        handler = InstantlyWebhook(secret, provider=provider)
        result = handler.handle(Unreadable(), Unreadable())
        self.assertEqual(result['http_status'], 410)
        self.assertEqual(result['writes'], 0)
        secret.assert_not_called()
        self.assertEqual(provider.mock_calls, [])
        self.assertFalse(contract_status()['enabled'])

    def test_authentication_and_pinned_identity(self):
        handler, calls = self.handler()
        for headers in ({}, {HEADER_NAME: 'wrong'}, {HEADER_NAME: ['synthetic-secret']},
                        {HEADER_NAME: 'synthetic-secret', HEADER_NAME.lower(): 'synthetic-secret'}):
            self.assertEqual(handler.handle(headers, payload())['http_status'], 401)
        self.assertEqual(calls, [])
        for field in ('workspace', 'campaign_id', 'email_id', 'event_type'):
            wrong = {**payload(), field: 'wrong'}
            self.assertEqual(handler.handle({HEADER_NAME: 'synthetic-secret'}, wrong)['http_status'], 422)
        self.assertEqual(calls, [])

    def test_real_readback_wire_contract(self):
        handler, calls = self.handler()
        result = handler.handle({HEADER_NAME.lower(): 'synthetic-secret'}, payload())
        self.assertEqual(result['http_status'], 200)
        self.assertTrue(result['provider_verified'])
        self.assertFalse(result['email_logged'])
        self.assertFalse(result['persisted'])
        self.assertEqual(calls, [('GET', '/api/v2/workspaces/current', None),
                                 ('GET', '/api/v2/emails/' + payload()['email_id'], None)])
        self.assertEqual(result['event']['occurred_at'], '2026-10-21T11:00:00.000Z')

    def test_payload_cannot_override_provider(self):
        handler, _ = self.handler()
        wrong_text = {**payload(), 'email_text': 'FORGED BODY', 'provider_verified': True}
        event = handler.handle({HEADER_NAME: 'synthetic-secret'}, wrong_text)['event']
        self.assertEqual(event['text'], 'Provider read, synthetic only.')
        self.assertNotEqual(event['text'], wrong_text['email_text'])
        for field in ('id', 'organization_id', 'campaign_id', 'lead', 'eaccount', 'from_address_email'):
            handler, _ = self.handler({**canonical(), field: 'wrong@example.invalid'})
            result = handler.handle({HEADER_NAME: 'synthetic-secret'}, payload())
            self.assertEqual(result['status'], 'provider_readback_pending')
            self.assertFalse(result['provider_verified'])
            self.assertFalse(result['email_logged'])

    def test_auto_reply_is_not_human(self):
        for kind, auto, expected in (('reply_received', 0, True), ('reply_received', 1, False),
                                     ('reply_received', None, False), ('reply_received', False, False),
                                     ('auto_reply_received', 0, False), ('auto_reply_received', 1, False)):
            with self.subTest(kind=kind, auto=auto):
                handler, _ = self.handler(canonical(kind, auto=auto))
                result = handler.handle({HEADER_NAME: 'synthetic-secret'}, payload(kind))
                self.assertEqual(result['http_status'], 200)
                self.assertEqual(result['event']['human_reply'], expected)
                self.assertFalse(result['external_platforms_stopped'])

    def test_duplicate_and_out_of_order(self):
        handler, _ = self.handler(canonical('reply_received'))
        reply = handler.handle({HEADER_NAME: 'synthetic-secret'}, payload('reply_received'))
        repeat = handler.handle({HEADER_NAME: 'synthetic-secret'}, {
            **payload('reply_received'), 'timestamp': '2026-10-22T12:00:00Z'})
        self.assertEqual(reply['event']['effect_key'], repeat['event']['effect_key'])
        sent_payload = {**payload(), 'email_id': '00000000-0000-4000-8000-000000000002'}
        sent_row = {**canonical(), 'id': sent_payload['email_id']}
        outgoing, _ = self.handler(sent_row)
        late_sent = outgoing.handle({HEADER_NAME: 'synthetic-secret'}, sent_payload)
        self.assertNotEqual(reply['event']['effect_key'], late_sent['event']['effect_key'])
        for result in (reply, repeat, late_sent):
            self.assertEqual(result['writes'], 0)
            self.assertFalse(result['persisted'])

    def test_reclassification_preserves_message_identity(self):
        human, _ = self.handler(canonical('reply_received', auto=0))
        auto, _ = self.handler(canonical('reply_received', auto=1))
        first = human.handle({HEADER_NAME: 'synthetic-secret'}, payload('reply_received'))
        second = auto.handle({HEADER_NAME: 'synthetic-secret'}, payload('reply_received'))
        self.assertEqual(first['event']['effect_key'], second['event']['effect_key'])
        self.assertFalse(second['event']['human_reply'])

    def test_private_payload_simulation_is_structural_and_not_echoed(self):
        with patch('socket.socket', side_effect=AssertionError('network forbidden')):
            self.assertEqual(dry_run(), dry_run())
            private = {**payload(), 'email_text': 'PRIVATE text, do not echo'}
            result = dry_run(private)
            self.assertTrue(result['structurally_valid'])
            self.assertTrue(result['simulation'])
            self.assertTrue(result['needs_provider_readback'])
            self.assertFalse(result['provider_verified'])
            self.assertFalse(result['persisted'])
            self.assertEqual(result['writes'], 0)
            self.assertNotIn('PRIVATE', json.dumps(result))
            self.assertNotIn('pavlack@example.invalid', json.dumps(result))

    def test_missing_email_id_is_actionable_not_verified(self):
        private = payload()
        del private['email_id']
        result = dry_run(private)
        self.assertEqual(result['error_code'], 'missing_or_invalid_email_id')
        self.assertFalse(result['structurally_valid'])
        self.assertFalse(result['provider_verified'])

    def test_configuration_and_provider_failures_are_pending(self):
        self.assertEqual(InstantlyWebhook(enabled=True).handle({}, payload())['status'], 'configuration_pending')
        handler = InstantlyWebhook(lambda: 'synthetic-secret', enabled=True)
        self.assertEqual(handler.handle({HEADER_NAME: 'synthetic-secret'}, payload())['status'], 'configuration_pending')
        request = Mock(side_effect=TimeoutError('PRIVATE token or body'))
        port = InstantlyReadOnly(lambda: 'synthetic-key', request=request)
        handler = InstantlyWebhook(lambda: 'synthetic-secret', provider=port, enabled=True)
        result = handler.handle({HEADER_NAME: 'synthetic-secret'}, payload())
        self.assertEqual(result['http_status'], 503)
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertEqual(request.call_count, 1)


if __name__ == '__main__':
    unittest.main()
