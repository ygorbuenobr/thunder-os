"""Capture ingress HTTP contract; isolated server, no providers or credentials."""
import http.client
import importlib.util
import json
import os
import stat
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import painel_servidor as panel

spec = importlib.util.spec_from_file_location('provision_ingress',
    Path(__file__).resolve().parents[1] / 'scripts/provision_instantly_ingress.py')
provision = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provision)


class InstantlyNginxTests(unittest.TestCase):
    def test_only_exact_ingress_bypasses_panel_login(self):
        original = 'server {\n    location = /saude { health; }\n    location / {\n' \
            'auth_basic "Painel Thunder";\nproxy_set_header X-Authenticated-User $remote_user;\n}\n}'
        updated = provision.render(original)
        self.assertEqual(provision.render(updated), updated)
        self.assertEqual(updated.replace(provision.STANZA, ''), original)
        self.assertIn('proxy_set_header X-Authenticated-User "";', provision.STANZA)
        self.assertIn('access_log off;', provision.STANZA)
        self.assertIn('include /etc/thunder-os/instantly_webhook_auth.conf;', provision.STANZA)
        self.assertIn('proxy_request_buffering off;', provision.STANZA)
        self.assertIn('location = /api/os/v1/integrations/instantly/events', provision.STANZA)

    def test_unknown_or_preexisting_conflicting_route_is_not_overwritten(self):
        for text in ('', '    location = /saude {',
                     'integrations/instantly/events\n    location = /saude {'):
            with self.assertRaises(ValueError):
                provision.render(text)

    def test_header_rule_cannot_inject_nginx_configuration(self):
        self.assertIn('return 401;', provision.secret_header_rule('s' * 64))
        for secret in ('short', 'x' * 129, 'a' * 40 + '"; return 200; #', 'a' * 40 + '\n'):
            with self.assertRaises(ValueError):
                provision.secret_header_rule(secret)

    def test_private_files_rechecked_after_prepare(self):
        real_fstat = os.fstat
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'auth'
            path.write_text('synthetic')
            path.chmod(0o600)
            def root_owner(fd):
                info = real_fstat(fd)
                return SimpleNamespace(st_mode=info.st_mode, st_uid=0, st_size=info.st_size)
            with patch.object(provision.os, 'fstat', side_effect=root_owner):
                self.assertEqual(provision.private_root_text(path), 'synthetic')
                path.chmod(0o644)
                with self.assertRaisesRegex(SystemExit, 'unsafe_private_file'):
                    provision.private_root_text(path)
                path.chmod(0o600)
                link = Path(directory) / 'link'
                link.symlink_to(path)
                with self.assertRaisesRegex(SystemExit, 'unsafe_private_file'):
                    provision.private_root_text(link)
            wrong_owner = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=501, st_size=9)
            with patch.object(provision.os, 'fstat', return_value=wrong_owner), \
                    self.assertRaisesRegex(SystemExit, 'unsafe_private_file'):
                provision.private_root_text(path)

    def test_rollback_without_secret_and_failed_reload_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, backup = root / 'nginx.conf', root / 'backup'
            config.write_text('current')
            backup.mkdir()
            (backup / 'nginx-before.conf').write_text('original')
            import hashlib
            argv = ['provision', 'rollback', '--expected-config-sha256',
                    hashlib.sha256(b'current').hexdigest()]
            for failure in (False, True):
                config.write_text('current')
                calls = Mock(side_effect=[None, RuntimeError(), None, None] if failure else None)
                with patch.object(provision, 'CONFIG', config), patch.object(provision, 'BACKUP', backup), \
                        patch.object(provision, 'SECRET', root / 'missing-secret'), \
                        patch.object(provision.os, 'uname', return_value=SimpleNamespace(nodename='srv1433640')), \
                        patch.object(provision.os, 'getuid', return_value=0), \
                        patch.object(provision.os, 'umask'), patch.object(provision.fcntl, 'flock'), \
                        patch('builtins.open', return_value=Mock()), patch('sys.argv', argv), \
                        patch.object(provision.subprocess, 'run', calls), patch('builtins.print'):
                    if failure:
                        with self.assertRaisesRegex(SystemExit, 'nginx_validation_failed_restored'):
                            provision.main()
                    else:
                        provision.main()
                self.assertEqual(config.read_text(), 'current' if failure else 'original')
                self.assertEqual(calls.call_count, 4 if failure else 2)


class InstantlyIngressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.receiver = Mock()
        self.receiver.enabled = True
        self.receiver.authenticate.return_value = None
        self.receiver.capture.return_value = {'http_status': 202, 'persisted': True,
            'provider_verified': False, 'email_logged': False}
        self.server = panel.ThreadingHTTPServer(('127.0.0.1', 0), panel.Handler)
        self.server.banco = panel.Banco(Path(self.tmp.name) / 'brain', Path(self.tmp.name) / 'panel.db')
        self.server.thunder_service = SimpleNamespace()
        self.server.thunder_instantly_capture = self.receiver
        self.server.thunder_nonces = {}
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, body='{}', method='POST', suffix='', headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request(method, '/api/os/v1/integrations/instantly/events' + suffix,
                body, headers or {'Content-Type': 'application/json'})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_authenticated_capture_without_panel_actor_or_nonce(self):
        status, result = self.request('{"fixture": true}')
        self.assertEqual(status, 202)
        self.receiver.capture.assert_called_once_with({'fixture': True})
        self.assertFalse(result['email_logged'])

    def test_authentication_precedes_body_parsing(self):
        self.receiver.authenticate.return_value = {'http_status': 401, 'status': 'unauthorized'}
        self.assertEqual(self.request('not-json')[0], 401)
        self.receiver.capture.assert_not_called()

    def test_missing_composition_fails_closed(self):
        del self.server.thunder_instantly_capture
        self.assertEqual(self.request('not-json')[0], 410)
        self.receiver.capture.assert_not_called()

    def test_query_and_get_do_not_reach_receiver(self):
        for method, suffix in (('GET', ''), ('POST', '?secret=never-accept')):
            self.assertEqual(self.request(method=method, suffix=suffix)[0], 405)
        self.receiver.authenticate.assert_not_called()

    def test_oversized_or_malformed_body_never_persists(self):
        self.assertEqual(self.request('', headers={'Content-Type': 'application/json',
            'Content-Length': str(panel.LIMITE + 1)})[0], 413)
        self.assertEqual(self.request('not-json')[0], 400)
        self.receiver.capture.assert_not_called()


if __name__ == '__main__':
    unittest.main()
