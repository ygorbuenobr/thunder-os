"""Fake Unix-socket contract tests; these do not prove OS isolation."""
import json
import socket
import stat
import struct
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import mock_open, patch

from thunder_os import connectsafely_read_broker_client as client


class FakeSocket:
    def __init__(self, uid=1234, transform=None):
        self.uid, self.transform, self.sent = uid, transform, []
        self.chunks = []
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def settimeout(self, value): pass
    def connect(self, path): self.path = path
    def getsockopt(self, level, name, length): return struct.pack('3i', 99, self.uid, 42)
    def sendall(self, raw):
        self.sent.append(raw)
        req = json.loads(raw)
        response = {'version': 1, 'operation': 'account_status', 'request_id': req['request_id'],
                    'company_id': req['company_id'], 'account_id': req['account_id'],
                    'public_id': 'sender', 'status': 'AVAILABLE', 'available': True,
                    'source': 'connectsafely', 'observed_at': '2026-11-04T12:30:00+00:00'}
        if self.transform:
            response = self.transform(response)
        self.chunks = [response if isinstance(response, bytes) else (json.dumps(response).encode() + b'\n')]
    def recv(self, size): return self.chunks.pop(0) if self.chunks else b''


class ReadBrokerClientTests(unittest.TestCase):
    def setUp(self):
        self.account = 'a' * 24
        self.now = datetime(2026, 11, 4, 12, 30, 30, tzinfo=timezone.utc)
        self.sock = FakeSocket()
        self.client = client.AccountStatusBrokerClient('/tmp/broker/status.sock', broker_uid=1234,
            company_id='company', account_id=self.account, public_id='sender',
            socket_factory=lambda *_: self.sock, clock=lambda: self.now)
        self.file_stat = SimpleNamespace(st_mode=stat.S_IFSOCK | 0o660, st_uid=1234)
        self.dir_stat = SimpleNamespace(st_mode=stat.S_IFDIR | 0o750, st_uid=1234)
        self.patches = [patch.object(client.os, 'getresuid', return_value=(5678, 5678, 5678), create=True),
                        patch('builtins.open', mock_open(read_data='CapEff:\t0000000000000000\nCapPrm:\t0000000000000000\nCapAmb:\t0000000000000000\n')),
                        patch.object(client.os, 'lstat', side_effect=[self.file_stat, self.dir_stat]),
                        patch.object(client.sys, 'platform', 'linux'),
                        patch.object(client.socket, 'SO_PEERCRED', 17, create=True)]
        for p in self.patches: p.start(); self.addCleanup(p.stop)

    def test_valid_observation_has_no_sender_or_secret(self):
        result = self.client.account_status()
        self.assertEqual(result['id'], self.account)
        self.assertTrue(result['available'])
        self.assertFalse(result['write_authorized'])
        self.assertFalse(hasattr(self.client, 'send'))
        self.assertEqual((self.client.company_id, self.client.account_id, self.client.public_id),
                         ('company', self.account, 'sender'))
        with self.assertRaises(AttributeError):
            self.client.public_id = 'other'
        self.assertEqual(json.loads(self.sock.sent[0])['operation'], 'account_status')
        self.assertNotIn('credential', repr(self.client.__dict__))

    def test_wrong_broker_uid_fails_before_request(self):
        self.sock.uid = 9999
        with self.assertRaisesRegex(client.ReadBrokerClientError, 'peer_identity_mismatch'):
            self.client.account_status()
        self.assertEqual(self.sock.sent, [])

    def test_root_and_same_uid_are_not_isolated(self):
        for ids in ((0, 0, 0), (1234, 1234, 1234), (5678, 5678, 0), (0, 5678, 5678), (5678, 1234, 5678)):
            with self.subTest(ids=ids), patch.object(client.os, 'getresuid', return_value=ids, create=True):
                with self.assertRaisesRegex(client.ReadBrokerClientError, 'panel_identity_privileged'):
                    self.client.account_status()
        self.assertEqual(self.sock.sent, [])

    def test_retained_capabilities_or_unverified_status_block_before_socket(self):
        statuses = [
            'CapEff:\t1\nCapPrm:\t0\nCapAmb:\t0\n',
            'CapEff:\t0\nCapPrm:\t1\nCapAmb:\t0\n',
            'CapEff:\t0\nCapPrm:\t0\nCapAmb:\t1\n',
            'CapEff:\t0\nCapPrm:\t0\n',
            'CapEff:\t0\nCapPrm:\txyz\nCapAmb:\t0\n',
            'CapEff:\t0\nCapEff:\t0\nCapPrm:\t0\nCapAmb:\t0\n',
        ]
        for status in statuses:
            with self.subTest(status=status), patch('builtins.open', mock_open(read_data=status)):
                with self.assertRaises(client.ReadBrokerClientError):
                    self.client.account_status()
        with patch('builtins.open', side_effect=OSError('unavailable')):
            with self.assertRaisesRegex(client.ReadBrokerClientError, '^panel_identity_unverified$'):
                self.client.account_status()
        self.assertEqual(self.sock.sent, [])

    def test_peer_credentials_unsupported_fails_closed(self):
        with patch.object(client.sys, 'platform', 'darwin'):
            with self.assertRaisesRegex(client.ReadBrokerClientError, 'peer_identity_unsupported'):
                self.client.account_status()
        self.assertEqual(self.sock.sent, [])

    def test_symlink_or_world_writable_directory_blocked(self):
        for info, parent in [(SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=1234), self.dir_stat),
                             (self.file_stat, SimpleNamespace(st_mode=stat.S_IFDIR | 0o777, st_uid=1234))]:
            with self.subTest(info=info, parent=parent), patch.object(client.os, 'lstat', side_effect=[info, parent]):
                with self.assertRaisesRegex(client.ReadBrokerClientError, 'socket_untrusted'):
                    self.client.account_status()
        self.assertEqual(self.sock.sent, [])

    def test_mismatch_stale_future_extra_field_and_forged_receipt_fail(self):
        changes = [lambda x: {**x, 'company_id': 'other'},
                   lambda x: {**x, 'account_id': 'b' * 24},
                   lambda x: {**x, 'request_id': 'replay'},
                   lambda x: {**x, 'public_id': 'other'},
                   lambda x: {**x, 'observed_at': (self.now - timedelta(minutes=2)).isoformat()},
                   lambda x: {**x, 'observed_at': (self.now + timedelta(seconds=1)).isoformat()},
                   lambda x: {**x, 'credential_ref': 'secret'},
                   lambda x: {**x, 'status': 'ERROR', 'available': True},
                   lambda x: {**x, 'available': 'true'}]
        for change in changes:
            self.sock = FakeSocket(transform=change)
            self.client._socket_factory = lambda *_: self.sock
            with self.subTest(change=change), patch.object(client.os, 'lstat', side_effect=[self.file_stat, self.dir_stat]):
                with self.assertRaisesRegex(client.ReadBrokerClientError, 'observation_unverified'):
                    self.client.account_status()

    def test_malformed_duplicate_and_oversized_response_fail_closed(self):
        for raw in [b'{"id":1,"id":2}\n', b'x' * 4097, b'{}\n{}\n']:
            self.sock = FakeSocket(transform=lambda _: raw)
            self.client._socket_factory = lambda *_: self.sock
            with self.subTest(raw=raw[:20]), patch.object(client.os, 'lstat', side_effect=[self.file_stat, self.dir_stat]):
                with self.assertRaises(client.ReadBrokerClientError): self.client.account_status()

    def test_no_socket_gives_static_error(self):
        with patch.object(client.os, 'lstat', side_effect=FileNotFoundError('sensitive path')):
            with self.assertRaisesRegex(client.ReadBrokerClientError, '^broker_unavailable$'):
                self.client.account_status()

    def test_read_timeout_is_bounded_and_visible(self):
        self.sock.recv = lambda _: (_ for _ in ()).throw(socket.timeout('private'))
        with self.assertRaisesRegex(client.ReadBrokerClientError, '^broker_timeout$'):
            self.client.account_status()


if __name__ == '__main__': unittest.main()
