"""Instantly simulation and canonical provider readback over synthetic HTTP."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
import json
import os
from pathlib import Path
import tempfile
from threading import Barrier
import unittest
from unittest.mock import Mock, patch

from thunder_os.db import Repository, RepositoryConflict
from thunder_os.external_email_import import (ExternalEmailError, INSTANTLY_CAMPAIGN,
                                              INSTANTLY_WORKSPACE)
from thunder_os.instantly_webhook import (HEADER_NAME, INSTANTLY_CAPTURE_SECRET_FILE,
    InstantlyCapture, InstantlyReadOnly, InstantlyWebhook, contract_status, dry_run,
    load_capture_secret)
from thunder_os.models import payload_hash


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


class InstantlyCaptureTests(unittest.TestCase):
    secret = 'synthetic-capture-secret-32-characters'
    kind = 'instantly_webhook_inbox'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'capture.db'
        self.repo = Repository(self.path)
        self.headers = {HEADER_NAME: self.secret}
        self.receiver = InstantlyCapture(self.repo, lambda: self.secret, enabled=True)

    def assert_held(self, result):
        for flag in ('provider_verified', 'email_logged', 'external_platforms_stopped', 'writer_enabled'):
            self.assertIs(result[flag], False)

    def test_disabled_before_secret_input_or_database(self):
        repo, secret = Mock(), Mock()
        class Unreadable:
            def __getattribute__(self, name):
                raise AssertionError('input must remain unread')
        receiver = InstantlyCapture(repo, secret)
        for result in (receiver.authenticate(Unreadable()),
                       receiver.handle(Unreadable(), Unreadable()), receiver.capture(Unreadable())):
            self.assertEqual(result['http_status'], 410)
            self.assertEqual(result['writes'], 0)
            self.assertFalse(result['persisted'])
            self.assert_held(result)
        secret.assert_not_called()
        self.assertEqual(repo.mock_calls, [])

    def test_authentication_precedes_payload_and_database(self):
        repo = Mock()
        receiver = InstantlyCapture(repo, lambda: self.secret, enabled=True)
        class Unreadable(dict):
            def get(self, *args):
                raise AssertionError('body inspected before authentication')
        for headers in ({}, {HEADER_NAME: 'wrong'}, {HEADER_NAME: [self.secret]},
                        {HEADER_NAME: self.secret, HEADER_NAME.lower(): self.secret}):
            result = receiver.handle(headers, Unreadable())
            self.assertEqual(result['http_status'], 401)
            self.assertEqual(result['writes'], 0)
        self.assertIsNone(receiver.authenticate(self.headers))
        self.assertEqual(repo.mock_calls, [])

    def test_message_headers_case_insensitive_and_duplicate_rejected(self):
        headers = Message()
        headers[HEADER_NAME.lower()] = self.secret
        self.assertIsNone(self.receiver.authenticate(headers))
        headers[HEADER_NAME.upper()] = self.secret
        self.assertEqual(self.receiver.authenticate(headers)['http_status'], 401)
        self.assertEqual(self.repo.list_state(self.kind), [])

    def test_secret_errors_are_sanitized_and_strong(self):
        repo = Mock()
        for loader in (None, lambda: '', lambda: 'short', lambda: 'x' * 4097,
                       lambda: 'x' * 32 + ' space', lambda: '\u00e9' * 32,
                       Mock(side_effect=OSError('PRIVATE SECRET'))):
            receiver = InstantlyCapture(repo, loader, enabled=True)
            result = receiver.handle(self.headers, payload())
            self.assertEqual(result['http_status'], 503)
            self.assertNotIn('PRIVATE', json.dumps(result))
            self.assertFalse(result['persisted'])
        self.assertEqual(repo.mock_calls, [])

    def test_pinned_identity_and_malformed_payload_before_database(self):
        repo = Mock()
        receiver = InstantlyCapture(repo, lambda: self.secret, enabled=True)
        invalid = [None, [], 'body']
        for field in ('workspace', 'campaign_id', 'event_type', 'email_id',
                      'lead_email', 'email_account', 'timestamp'):
            invalid.extend([{**payload(), field: 'wrong'}, {**payload(), field: None}])
        invalid.append({**payload(), 'timestamp': '9999-12-31T23:59:59-23:59'})
        for value in invalid:
            with self.subTest(value=value):
                self.assertEqual(receiver.handle(self.headers, value)['http_status'], 422)
        self.assertEqual(repo.mock_calls, [])

    def test_capture_retains_only_normalized_metadata_and_held_flags(self):
        value = {**payload(), 'lead_email': ' PAVLACK@example.invalid ',
                 'email_account': ' SENDER@example.invalid ',
                 'timestamp': '2026-10-21T09:00:00-03:00',
                 'provider_verified': True, 'email_logged': True,
                 'body': {'secret': 'PRIVATE'}, 'delivery_id': 'retry-1'}
        result = self.receiver.handle(self.headers, value)
        self.assertEqual(result['http_status'], 202)
        self.assertEqual(result['status'], 'awaiting_provider_readback')
        self.assertEqual(result['writes'], 1)
        self.assertTrue(result['persisted'])
        self.assertFalse(result['deduplicated'])
        self.assert_held(result)
        rows = self.repo.list_state(self.kind)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        metadata = {key: payload()[key] for key in ('workspace', 'campaign_id', 'event_type',
                    'email_id', 'lead_email', 'email_account', 'timestamp')}
        metadata['timestamp'] = '2026-10-21T12:00:00.000Z'
        self.assertEqual({key: row[key] for key in metadata}, metadata)
        self.assertEqual(row['payload_hash'], payload_hash(metadata))
        self.assertEqual(row['id'], payload_hash({key: metadata[key] for key in
                                               ('workspace', 'event_type', 'email_id')}))
        self.assertEqual(set(row), set(metadata) | {'payload_hash', 'status', 'persisted',
            'provider_verified', 'email_logged', 'external_platforms_stopped', 'writer_enabled',
            'id', 'version', 'updated_at'})
        self.assertEqual(row['status'], 'awaiting_provider_readback')
        self.assertTrue(row['persisted'])
        self.assert_held(row)
        self.assertNotIn('PRIVATE', json.dumps(row))

    def test_restart_duplicate_ignores_raw_body_and_extra_fields(self):
        first = self.receiver.handle(self.headers, payload())
        before = self.repo.list_state(self.kind)
        restarted_repo = Repository(self.path)
        restarted = InstantlyCapture(restarted_repo, lambda: self.secret, enabled=True)
        value = {**payload(), 'email_text': 'DIFFERENT PRIVATE BODY',
                 'extra': object(), 'lead_email': ' PAVLACK@example.invalid ',
                 'timestamp': '2026-10-21T09:00:00-03:00'}
        with patch.object(restarted_repo, 'append_state', wraps=restarted_repo.append_state) as append:
            again = restarted.handle(self.headers, value)
        self.assertEqual(again['http_status'], 202)
        self.assertTrue(again['deduplicated'])
        self.assertEqual(again['writes'], 0)
        self.assertTrue(again['persisted'])
        self.assertEqual(again['id'], first['id'])
        append.assert_not_called()
        self.assertEqual(restarted_repo.list_state(self.kind), before)

    def test_same_key_conflicting_metadata_is_not_overwritten(self):
        self.receiver.handle(self.headers, payload())
        before = self.repo.list_state(self.kind)
        for field, value in (('lead_email', 'other@example.invalid'),
                             ('email_account', 'other@example.invalid'),
                             ('timestamp', '2026-10-22T12:00:00Z')):
            result = self.receiver.handle(self.headers, {**payload(), field: value})
            self.assertEqual(result['http_status'], 409)
            self.assertEqual(result['writes'], 0)
            self.assertFalse(result['persisted'])
        self.assertEqual(self.repo.list_state(self.kind), before)

    def test_racing_insert_reconciles_identical_or_conflicting_readback(self):
        for conflict in (False, True):
            with self.subTest(conflict=conflict):
                repo = Repository(Path(self.temp.name) / ('race-' + str(conflict) + '.db'))
                observer = Repository(repo.path)
                receiver = InstantlyCapture(repo, lambda: self.secret, enabled=True)
                def race(kind, key, row):
                    winner = {**row, 'lead_email': 'other@example.invalid'} if conflict else row
                    observer.append_state(kind, key, winner)
                    raise RepositoryConflict('PRIVATE race details')
                with patch.object(repo, 'append_state', side_effect=race):
                    result = receiver.handle(self.headers, payload())
                self.assertEqual(result['http_status'], 409 if conflict else 202)
                self.assertEqual(result['writes'], 0)
                self.assertEqual(result['deduplicated'], not conflict)
                self.assertNotIn('PRIVATE', json.dumps(result))
                self.assertEqual(len(observer.list_state(self.kind)), 1)

    def test_database_failures_are_sanitized(self):
        for failure in ('read', 'append', 'race_read', 'race_missing'):
            repo = Mock()
            repo.get_state.return_value = None
            if failure == 'read':
                repo.get_state.side_effect = OSError('PRIVATE read')
            elif failure == 'append':
                repo.append_state.side_effect = OSError('PRIVATE write')
            else:
                repo.append_state.side_effect = RepositoryConflict('PRIVATE conflict')
                if failure == 'race_read':
                    repo.get_state.side_effect = [None, OSError('PRIVATE readback')]
            result = InstantlyCapture(repo, lambda: self.secret, enabled=True).handle(self.headers, payload())
            self.assertEqual(result['http_status'], 503)
            self.assertNotIn('PRIVATE', json.dumps(result))
            self.assertFalse(result['persisted'])

    def test_concurrent_receivers_use_immutable_repository_constraint(self):
        for conflict in (False, True):
            with self.subTest(conflict=conflict):
                barrier = Barrier(2)
                class RacingRepository(Repository):
                    def append_state(self, kind, key, row):
                        barrier.wait(timeout=5)
                        return super().append_state(kind, key, row)
                path = Path(self.temp.name) / ('concurrent-' + str(conflict) + '.db')
                receivers = [InstantlyCapture(RacingRepository(path), lambda: self.secret,
                                             enabled=True) for _ in range(2)]
                values = [payload(), {**payload(), 'lead_email': 'other@example.invalid'}
                          if conflict else payload()]
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(receiver.handle, self.headers, value)
                               for receiver, value in zip(receivers, values)]
                    results = [future.result(timeout=10) for future in futures]
                self.assertEqual(sorted(result['http_status'] for result in results),
                                 [202, 409] if conflict else [202, 202])
                self.assertEqual(sum(result['writes'] for result in results), 1)
                self.assertEqual(sum(result['deduplicated'] for result in results),
                                 0 if conflict else 1)
                self.assertEqual(len(Repository(path).list_state(self.kind)), 1)

    def test_event_types_capture_out_of_order_without_provider_or_crm(self):
        with patch('socket.socket', side_effect=AssertionError('network forbidden')), \
             patch.object(InstantlyReadOnly, 'read_event', side_effect=AssertionError('provider forbidden')), \
             patch('thunder_os.external_email_import.ExternalEmailImport.execute',
                   side_effect=AssertionError('CRM forbidden')) as crm:
            for kind in ('reply_received', 'email_sent', 'auto_reply_received'):
                result = self.receiver.capture(payload(kind))
                self.assertEqual(result['http_status'], 202)
                self.assert_held(result)
            crm.assert_not_called()
        self.assertEqual(len(self.repo.list_state(self.kind)), 3)
        self.assertEqual(self.repo.list_state('hubspot_attempt'), [])


class CaptureSecretTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'secret'
        self.secret = 'synthetic-capture-secret-32-characters'

    def load(self):
        with patch('thunder_os.instantly_webhook.INSTANTLY_CAPTURE_SECRET_FILE', str(self.path)):
            return load_capture_secret()

    def write(self, data, mode=0o600):
        self.path.write_bytes(data)
        self.path.chmod(mode)

    def test_fixed_path_private_ascii_secret_and_trailing_newline(self):
        self.assertEqual(INSTANTLY_CAPTURE_SECRET_FILE, '/etc/thunder-os/instantly_webhook_secret')
        self.write((self.secret + '\n').encode())
        self.assertEqual(self.load(), self.secret)

    def test_missing_symlink_group_readable_or_nonregular_rejected(self):
        with self.assertRaises(ExternalEmailError):
            self.load()
        target = Path(self.temp.name) / 'target'
        target.write_text(self.secret)
        target.chmod(0o600)
        self.path.symlink_to(target)
        with self.assertRaises(ExternalEmailError):
            self.load()
        self.path.unlink()
        self.write(self.secret.encode(), 0o640)
        with self.assertRaises(ExternalEmailError):
            self.load()
        self.path.unlink()
        self.path.mkdir(mode=0o700)
        with self.assertRaises(ExternalEmailError):
            self.load()

    def test_malformed_or_oversized_secret_rejected(self):
        for data in (b'', b'x' * 31, b'x' * 4097, b'x' * 32 + b' space',
                     b'x' * 32 + b'\x00', b'\xff' * 32):
            self.write(data)
            with self.subTest(data=data[:40]), self.assertRaises(ExternalEmailError):
                self.load()
        for count in (32, 4096):
            self.write(b'x' * count)
            self.assertEqual(self.load(), 'x' * count)

    def test_unrelated_owner_rejected(self):
        self.write(self.secret.encode())
        observed = os.stat(self.path)
        with patch('thunder_os.instantly_webhook.os.fstat', return_value=Mock(
                st_uid=max(0, os.getuid()) + 100000, st_mode=observed.st_mode,
                st_size=observed.st_size)):
            with self.assertRaises(ExternalEmailError):
                self.load()


if __name__ == '__main__':
    unittest.main()
