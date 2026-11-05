import contextlib
import io
import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import patch

from thunder_os import connectsafely_read_broker_service as service


def config():
    return {
        'THUNDER_LI_BROKER_COMPANY': 'company-1',
        'THUNDER_LI_BROKER_ACCOUNT': 'a' * 24,
        'THUNDER_LI_BROKER_PUBLIC_ID': 'owner-1',
        'THUNDER_LI_BROKER_PANEL_UID': str(os.geteuid() + 1),
        'THUNDER_LI_BROKER_SOCKET_GID': str(os.getegid()),
        'THUNDER_LI_BROKER_SOCKET': '/run/thunder-li-reader/account.sock',
        'THUNDER_LI_BROKER_CREDENTIAL_FILE': '/etc/thunder-os/broker.key',
    }


class ServiceTests(unittest.TestCase):
    def test_configuration_requires_all_bindings_and_safe_paths(self):
        good = config()
        self.assertEqual('company-1', service.parse_configuration(good)['company'])
        for key, invalid in [('THUNDER_LI_BROKER_PANEL_UID', '0'),
                             ('THUNDER_LI_BROKER_SOCKET_GID', '-1'),
                             ('THUNDER_LI_BROKER_SOCKET', '/run/../other'),
                             ('THUNDER_LI_BROKER_ACCOUNT', 'INVALID'),
                             ('THUNDER_LI_BROKER_COMPANY', ''),
                             ('THUNDER_LI_BROKER_PUBLIC_ID', 'two words')]:
            bad = dict(good, **{key: invalid})
            with self.subTest(key=key), self.assertRaisesRegex(service.ServiceConfigurationError, '^configuration_invalid$'):
                service.parse_configuration(bad)
        del good['THUNDER_LI_BROKER_COMPANY']
        with self.assertRaises(service.ServiceConfigurationError):
            service.parse_configuration(good)

    def test_private_credential_rejects_links_and_weak_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            # On macOS /tmp is a symlink; resolve the test directory first.
            root = Path(directory).resolve()
            key = root / 'key'
            key.write_text('abcdefghijklmnop')
            key.chmod(0o600)
            self.assertEqual('abcdefghijklmnop', service.private_credential(str(key)))
            key.chmod(0o640)
            with self.assertRaisesRegex(service.ServiceConfigurationError, '^credential_unavailable$'):
                service.private_credential(str(key))
            key.chmod(0o600)
            link = root / 'link'
            link.symlink_to(key)
            with self.assertRaises(service.ServiceConfigurationError):
                service.private_credential(str(link))
            hardlink = root / 'hardlink'
            os.link(key, hardlink)
            with self.assertRaises(service.ServiceConfigurationError):
                service.private_credential(str(key))

    def test_server_does_not_open_credential_during_configuration(self):
        env = config()
        if os.getegid() == 0:  # constructor test does not require real privileges
            env['THUNDER_LI_BROKER_SOCKET_GID'] = '1'
        with patch.object(service.os, 'geteuid', return_value=10), \
             patch.object(service.os, 'getegid', return_value=int(env['THUNDER_LI_BROKER_SOCKET_GID'])), \
             patch.object(service, 'private_credential') as loader, \
             patch.object(service, 'AccountReadBrokerServer') as server:
            env['THUNDER_LI_BROKER_PANEL_UID'] = '11'
            service.create_server(env)
            loader.assert_not_called()
            self.assertEqual(11, server.call_args.kwargs['allowed_uid'])

    def test_denied_operation_never_loads_credential(self):
        env = dict(config(), THUNDER_LI_BROKER_PANEL_UID='11', THUNDER_LI_BROKER_SOCKET_GID='20')
        with patch.object(service.os, 'geteuid', return_value=10), \
             patch.object(service.os, 'getegid', return_value=20), \
             patch.object(service, 'private_credential') as loader, \
             patch.object(service, 'AccountReadBrokerServer') as server:
            service.create_server(env)
            contract = server.call_args.kwargs['contract']
            with self.assertRaisesRegex(ValueError, '^invalid_request$'):
                contract.dispatch({'version': 1, 'operation': 'send', 'company_id': 'company-1',
                                   'account_id': 'a' * 24, 'request_id': 'request-1'})
            loader.assert_not_called()

    def test_graceful_stop_closes_server(self):
        class Server:
            closed = False
            def start(self): pass
            def serve_once(self): raise service._StopService()
            def close(self): self.closed = True
        server = Server()
        with patch.object(service, 'create_server', return_value=server):
            self.assertEqual(0, service.run(config()))
        self.assertTrue(server.closed)

    def test_main_does_not_print_details_of_failures(self):
        output = io.StringIO()
        with patch.object(service, 'run', side_effect=ValueError('secret-should-not-appear')), \
             contextlib.redirect_stderr(output):
            self.assertEqual(1, service.main())
        self.assertEqual('connectsafely_read_broker_unavailable\n', output.getvalue())


if __name__ == '__main__':
    unittest.main()
