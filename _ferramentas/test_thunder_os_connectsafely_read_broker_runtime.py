"""Offline tests; real Unix peer/UID isolation must be verified on Linux."""
import os
import socket
import time
import unittest
from unittest.mock import Mock, mock_open, patch

from thunder_os.connectsafely_read_broker_contract import AccountReadBrokerContract
from thunder_os.connectsafely_read_broker_runtime import (
    AccountReadBrokerServer, AccountReadHTTPS, BrokerRuntimeError, MAX_FRAME, _decode,
    _dispatch_bounded, _frame, _read_frame, _verify_process_identity,
)


class ReadBrokerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.account = 'a' * 24
        self.request = {'version': 1, 'operation': 'account_status', 'company_id': 'company',
                        'account_id': self.account, 'request_id': 'req-1'}

    def provider(self, path, query):
        if path == '/linkedin/accounts' and query == {'q': self.account}:
            return 200, [{'id': self.account, 'publicId': 'owner', 'token': 'secret'}]
        if path == '/linkedin/account/' + self.account + '/status' and query == {}:
            return 200, {'id': self.account, 'publicId': 'owner', 'status': 'AVAILABLE',
                         'enabled': True, 'hasTokens': True, 'secret': 'omit'}
        raise AssertionError('unexpected endpoint')

    def contract(self, provider=None):
        return AccountReadBrokerContract('company', self.account, 'owner', provider or self.provider)

    def test_protocol_rejects_duplicate_fields_invalid_json_and_large_frame(self):
        for raw in (b'{"a":1,"a":2}\n', b'{NaN}\n', b'\xff\n', b'{}', b' ' * MAX_FRAME + b'\n'):
            with self.subTest(raw=raw[:20]), self.assertRaisesRegex(BrokerRuntimeError, 'invalid_request'):
                _decode(raw)

    def test_frame_reader_enforces_single_message_and_byte_limit(self):
        for raw in (b'{}\n{}\n', b' ' * (MAX_FRAME + 1)):
            left, right = socket.socketpair()
            try:
                right.sendall(raw)
                with self.subTest(length=len(raw)), self.assertRaises(BrokerRuntimeError):
                    _read_frame(left, time.monotonic() + 1)
            finally:
                left.close(); right.close()

    def test_frame_read_obeys_wall_clock_deadline(self):
        left, right = socket.socketpair()
        try:
            started = time.monotonic()
            with self.assertRaisesRegex(BrokerRuntimeError, 'timeout'):
                _read_frame(left, started + .05)
            self.assertLess(time.monotonic() - started, 1)
        finally:
            left.close(); right.close()

    def test_fake_provider_process_returns_only_sanitized_account_observation(self):
        result = _dispatch_bounded(self.contract(), self.request, time.monotonic() + 2)
        self.assertEqual(result['account_id'], self.account)
        self.assertEqual(result['company_id'], 'company')
        self.assertTrue(result['available'])
        self.assertNotIn('secret', str(result))
        self.assertNotIn('credential', str(result))

    def test_scope_and_write_attempts_are_rejected_before_fake_provider(self):
        def forbidden(_path, _query):
            raise AssertionError('provider must not be called')
        for changes, expected in [({'company_id': 'other'}, 'scope_mismatch'),
                                  ({'account_id': 'b' * 24}, 'scope_mismatch'),
                                  ({'operation': 'send_message'}, 'invalid_request'),
                                  ({'method': 'POST'}, 'invalid_request')]:
            with self.subTest(changes=changes):
                response = _dispatch_bounded(self.contract(forbidden), {**self.request, **changes},
                                             time.monotonic() + 2)
                self.assertEqual(response, {'error': expected})

    def test_blocked_fake_provider_is_killed_on_deadline(self):
        def stuck(_path, _query):
            time.sleep(5)
        started = time.monotonic()
        with self.assertRaisesRegex(BrokerRuntimeError, 'timeout'):
            _dispatch_bounded(self.contract(stuck), self.request, started + .1)
        self.assertLess(time.monotonic() - started, 1.5)

    def test_error_and_response_frames_do_not_leak_internal_details(self):
        self.assertEqual(_frame({'error': 'ok'}), b'{"error":"ok"}\n')
        self.assertEqual(_frame({'x': 'x' * MAX_FRAME}), b'{"error":"invalid_response"}\n')

    def test_provider_transport_restricts_host_get_and_response_without_leaking_token(self):
        token = 'fake-credential-123456789'
        connection = Mock()
        connection.getresponse.return_value = Mock(status=200, read=Mock(return_value=b'{"ok":true}'))
        factory = Mock(return_value=connection)
        transport = AccountReadHTTPS(self.account, lambda: token, connection_factory=factory)
        with patch.dict(os.environ, {'SSL_CERT_FILE': '', 'SSL_CERT_DIR': ''}):
            status, payload = transport('/linkedin/accounts', {'q': self.account})
        self.assertEqual((status, payload), (200, {'ok': True}))
        self.assertEqual(factory.call_args.args, ('api.connectsafely.ai', 443))
        self.assertEqual(connection.request.call_args.args[0], 'GET')
        self.assertEqual(connection.request.call_args.kwargs['body'], None)
        self.assertNotIn(token, str(payload))
        connection.close.assert_called_once()

    def test_provider_transport_rejects_send_and_other_account_before_loading_secret(self):
        loader, factory = Mock(), Mock()
        transport = AccountReadHTTPS(self.account, loader, connection_factory=factory)
        for path, query in [('/linkedin/connect', {}), ('/linkedin/conversations/send', {}),
                            ('/linkedin/accounts', {'q': 'b' * 24}),
                            ('/linkedin/account/' + 'b' * 24 + '/status', {}),
                            ('https://example.test/', {}),
                            ('/linkedin/accounts', {'q': self.account, 'method': 'POST'})]:
            with self.subTest(path=path), self.assertRaisesRegex(BrokerRuntimeError, 'route_blocked'):
                transport(path, query)
        loader.assert_not_called(); factory.assert_not_called()

    def test_provider_transport_timeout_and_errors_are_static(self):
        connection = Mock()
        connection.request.side_effect = TimeoutError('fake-credential-123456789')
        factory = Mock(return_value=connection)
        transport = AccountReadHTTPS(self.account, lambda: 'fake-credential-123456789',
                                    connection_factory=factory, timeout=.1)
        with patch.dict(os.environ, {'SSL_CERT_FILE': '', 'SSL_CERT_DIR': ''}):
            with self.assertRaisesRegex(BrokerRuntimeError, '^provider_unavailable$') as caught:
                transport('/linkedin/account/' + self.account + '/status', {})
        self.assertNotIn('fake-credential', str(caught.exception))
        self.assertEqual(factory.call_args.kwargs['timeout'], .1)
        connection.close.assert_called_once()

    def test_runtime_never_claims_linux_uid_isolation_on_mac_or_as_root(self):
        if os.name != 'posix':
            self.skipTest('Unix-only check')
        with patch('thunder_os.connectsafely_read_broker_runtime.sys.platform', 'darwin'):
            with self.assertRaisesRegex(BrokerRuntimeError, 'unsupported_isolation'):
                AccountReadBrokerServer('/tmp/not-created.sock', allowed_uid=123, socket_gid=123,
                                        contract=self.contract())
        with patch('thunder_os.connectsafely_read_broker_runtime.sys.platform', 'linux'), \
             patch('thunder_os.connectsafely_read_broker_runtime.socket.SO_PEERCRED', 17, create=True), \
             patch('thunder_os.connectsafely_read_broker_runtime.os.getresuid', return_value=(0, 0, 0), create=True), \
             patch('thunder_os.connectsafely_read_broker_runtime.os.getresgid', return_value=(0, 0, 0), create=True):
            with self.assertRaisesRegex(BrokerRuntimeError, 'unsupported_isolation'):
                AccountReadBrokerServer('/tmp/not-created.sock', allowed_uid=123, socket_gid=123,
                                        contract=self.contract())

    def test_linux_identity_rejects_saved_root_divergent_ids_and_capabilities(self):
        zero_status = 'Name:\tbroker\nCapPrm:\t0000000000000000\nCapEff:\t0000000000000000\nCapAmb:\t0000000000000000\n'
        with patch('thunder_os.connectsafely_read_broker_runtime.sys.platform', 'linux'), \
             patch('thunder_os.connectsafely_read_broker_runtime.socket.SO_PEERCRED', 17, create=True), \
             patch('thunder_os.connectsafely_read_broker_runtime.os.getresuid', create=True) as get_uids, \
             patch('thunder_os.connectsafely_read_broker_runtime.os.getresgid', create=True) as get_gids, \
             patch('thunder_os.connectsafely_read_broker_runtime.os.getgroups', return_value=[222]), \
             patch('builtins.open', mock_open(read_data=zero_status)):
            get_uids.return_value = (111, 111, 111)
            get_gids.return_value = (222, 222, 222)
            _verify_process_identity()
            for uids, gids in [((111, 111, 0), (222, 222, 222)),
                               ((111, 112, 111), (222, 222, 222)),
                               ((111, 111, 111), (222, 222, 0)),
                               ((111, 111, 111), (222, 223, 222))]:
                get_uids.return_value, get_gids.return_value = uids, gids
                with self.subTest(uids=uids, gids=gids), self.assertRaisesRegex(BrokerRuntimeError, 'unsupported_isolation'):
                    _verify_process_identity()
            get_uids.return_value, get_gids.return_value = (111, 111, 111), (222, 222, 222)
            for field in ('CapEff', 'CapPrm', 'CapAmb'):
                altered = zero_status.replace(field + ':\t0000000000000000', field + ':\t0000000000000001')
                with self.subTest(field=field), patch('builtins.open', mock_open(read_data=altered)), \
                     self.assertRaisesRegex(BrokerRuntimeError, 'unsupported_isolation'):
                    _verify_process_identity()

    def test_unreadable_or_incomplete_capability_status_and_root_group_are_denied(self):
        with patch('thunder_os.connectsafely_read_broker_runtime.sys.platform', 'linux'), \
             patch('thunder_os.connectsafely_read_broker_runtime.socket.SO_PEERCRED', 17, create=True), \
             patch('thunder_os.connectsafely_read_broker_runtime.os.getresuid', return_value=(111, 111, 111), create=True), \
             patch('thunder_os.connectsafely_read_broker_runtime.os.getresgid', return_value=(222, 222, 222), create=True), \
             patch('thunder_os.connectsafely_read_broker_runtime.os.getgroups', return_value=[222]):
            for data in ('CapEff:\t0000\nCapPrm:\t0000\n',
                         'CapEff:\tnothex\nCapPrm:\t0000\nCapAmb:\t0000\n',
                         'CapEff:\t0000\nCapEff:\t0000\nCapPrm:\t0000\nCapAmb:\t0000\n'):
                with self.subTest(status=data), patch('builtins.open', mock_open(read_data=data)), \
                     self.assertRaisesRegex(BrokerRuntimeError, 'unsupported_isolation'):
                    _verify_process_identity()
            with patch('builtins.open', side_effect=PermissionError), \
                 self.assertRaisesRegex(BrokerRuntimeError, 'unsupported_isolation'):
                _verify_process_identity()
            with patch('thunder_os.connectsafely_read_broker_runtime.os.getgroups', return_value=[0, 222]), \
                 self.assertRaisesRegex(BrokerRuntimeError, 'unsupported_isolation'):
                _verify_process_identity()

    def test_identity_is_rechecked_before_accept_and_before_processing(self):
        server = object.__new__(AccountReadBrokerServer)
        server._socket = Mock()
        server.timeout = .5
        server.allowed_uid = 111
        server.contract = Mock()
        with patch('thunder_os.connectsafely_read_broker_runtime._verify_process_identity',
                   side_effect=BrokerRuntimeError('unsupported_isolation')):
            with self.assertRaisesRegex(BrokerRuntimeError, 'unsupported_isolation'):
                server.serve_once()
        server._socket.accept.assert_not_called()
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        server._socket.accept.return_value = (connection, None)
        with patch('thunder_os.connectsafely_read_broker_runtime._verify_process_identity',
                   side_effect=[None, BrokerRuntimeError('unsupported_isolation')]), \
             patch('thunder_os.connectsafely_read_broker_runtime.threading.active_count', return_value=1):
            server.serve_once()
        connection.getsockopt.assert_not_called()
        server.contract.dispatch.assert_not_called()
if __name__ == '__main__':
    unittest.main()
