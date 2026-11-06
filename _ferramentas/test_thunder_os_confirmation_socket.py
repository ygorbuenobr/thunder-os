import os
import contextlib
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import painel_servidor as panel


class ConfirmationSocketTests(unittest.TestCase):
    def test_preview_read_requires_exact_task_and_current_confirmation_hashes(self):
        binding = {'task_id': '123', 'preview_id': 'p', 'review_hash': 'r',
                   'identity_hash': 'i', 'identity': {'matches': False}}
        confirmation = {'actor': 'ygor', 'review_hash': 'r', 'observation_hash': 'i'}
        repo = SimpleNamespace(list_state=lambda kind: [binding],
                               get_state=lambda kind, key: confirmation)
        handler = object.__new__(panel.Handler)
        handler.server = SimpleNamespace(thunder_service=SimpleNamespace(repository=repo),
            thunder_connectsafely_app=SimpleNamespace(preview=lambda ident, actor: {'id': ident}))
        handler.command = 'GET'
        handler.thunder_proxy_actor = lambda: 'ygor'
        handler.responder = lambda status, value: (status, value)
        route = '/api/os/v1/linkedin-cadence-previews?task_id='
        for invalid in ('', 'other', '123&task_id=456', '123&extra=1'):
            with self.assertRaises(panel.Erro):
                handler.thunder_api(urlsplit(route + invalid))
        result = handler.thunder_api(urlsplit(route + '123'))[1]
        self.assertTrue(result['read_only'])
        self.assertEqual(result['previews'][0]['identity_confirmation'], confirmation)
        self.assertEqual(handler.thunder_api(urlsplit(route + '456'))[1]['previews'], [])
        confirmation['observation_hash'] = 'stale'
        self.assertIsNone(handler.thunder_api(urlsplit(route + '123'))[1]['previews'][0]['identity_confirmation'])

    def test_peer_credentials_and_executable_are_required(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        handler = object.__new__(panel.Handler)
        handler.connection = left
        handler.server = SimpleNamespace(thunder_nginx_uid=os.getuid(),
                                         thunder_nginx_executable=sys.executable)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        pidfile = Path(temp.name) / 'nginx.pid'
        pidfile.write_text(str(os.getpid()))
        handler.server.thunder_nginx_pid_file = str(pidfile)
        self.assertTrue(handler.trusted_confirmation_peer())
        handler.server.thunder_nginx_uid = os.getuid() + 1
        self.assertFalse(handler.trusted_confirmation_peer())
        handler.server.thunder_nginx_uid = os.getuid()
        handler.server.thunder_nginx_executable = '/dev/null'
        self.assertFalse(handler.trusted_confirmation_peer())

    def test_non_root_panel_trusts_root_master_without_exe_access(self):
        from unittest import mock
        import stat as stat_module
        import struct
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        pidfile = Path(temp.name) / 'nginx.pid'
        pidfile.write_text('4242')
        handler = object.__new__(panel.Handler)
        handler.connection = SimpleNamespace(family=socket.AF_UNIX,
            getsockopt=lambda *a: struct.pack('3i', 4243, 33, 33))
        handler.server = SimpleNamespace(thunder_nginx_uid=33, thunder_nginx_executable='/usr/sbin/nginx',
                                         thunder_nginx_pid_file=str(pidfile))
        real_lstat = Path.lstat

        def owned_by(uid):
            def fake(self):
                info = real_lstat(self)
                return SimpleNamespace(st_mode=stat_module.S_IFREG | 0o644, st_uid=uid)
            return fake

        def read_text(self, *a, **k):
            if str(self) == '/proc/4243/stat':
                return '4243 (nginx) S 4242 0'
            if str(self) == '/proc/4242/status':
                return 'Name:\tnginx\nUid:\t%s\t0\t0\t0\n' % master_uid[0]
            return Path.read_bytes(self).decode()
        master_uid = ['0']
        if not hasattr(socket, 'SO_PEERCRED'):
            self.skipTest('SO_PEERCRED unavailable')
        with mock.patch.object(panel.os, 'geteuid', return_value=990), \
             mock.patch.object(panel.os.path, 'samefile', side_effect=PermissionError), \
             mock.patch.object(Path, 'read_text', read_text):
            with mock.patch.object(Path, 'lstat', owned_by(0)):
                self.assertTrue(handler.trusted_confirmation_peer())
                master_uid[0] = '33'
                self.assertFalse(handler.trusted_confirmation_peer())
                master_uid[0] = '0'
                handler.connection.getsockopt = lambda *a: struct.pack('3i', 4243, 34, 34)
                self.assertFalse(handler.trusted_confirmation_peer())
                handler.connection.getsockopt = lambda *a: struct.pack('3i', 4243, 33, 33)
            with mock.patch.object(Path, 'lstat', owned_by(990)):
                self.assertFalse(handler.trusted_confirmation_peer())
        with mock.patch.object(panel.os, 'geteuid', return_value=0), \
             mock.patch.object(panel.os.path, 'samefile', side_effect=PermissionError), \
             mock.patch.object(Path, 'read_text', read_text), \
             mock.patch.object(Path, 'lstat', owned_by(0)):
            self.assertFalse(handler.trusted_confirmation_peer())

    def test_socket_parent_and_existing_target_are_checked(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'confirmation.sock'
            server = panel.confirmation_server(SimpleNamespace(thunder_nonces={}),
                                               path, os.getuid(), sys.executable)
            self.addCleanup(server.server_close)
            self.assertEqual(path.stat().st_mode & 0o777, 0o660)
            with self.assertRaises(ValueError):
                panel.confirmation_server(SimpleNamespace(), path, os.getuid(), sys.executable)

    def test_tcp_and_missing_peer_fail_closed(self):
        handler = object.__new__(panel.Handler)
        handler.server = SimpleNamespace(thunder_nginx_uid=os.getuid(),
                                         thunder_nginx_executable=sys.executable)
        handler.connection = SimpleNamespace(family=socket.AF_INET)
        self.assertFalse(handler.trusted_confirmation_peer())
        handler.server.thunder_nginx_uid = None
        self.assertFalse(handler.trusted_confirmation_peer())

    def test_cleanup_removes_only_owned_inode(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'confirmation.sock'
            server = panel.confirmation_server(SimpleNamespace(), path, os.getuid(), sys.executable)
            server.server_close()
            self.assertFalse(path.exists())
            server = panel.confirmation_server(SimpleNamespace(), path, os.getuid(), sys.executable)
            path.unlink()
            path.write_text('different file', encoding='utf-8')
            server.server_close()
            self.assertEqual(path.read_text(encoding='utf-8'), 'different file')

    def test_identity_confirmation_binds_preview_hashes_and_peer(self):
        class Repo:
            records = {('linkedin_cadence_preview_binding', 'p'): {
                'preview_id': 'p', 'review_hash': 'review', 'identity_hash': 'observation',
                'preview_version': 1}, ('connectsafely_preview', 'p'): {
                'review_hash': 'review', 'version': 1, 'state': 'review_required', 'expires_at': 200}}
            def get_state(self, kind, key): return self.records.get((kind, key))
            def append_state(self, kind, key, value): self.records[(kind, key)] = value
            def transaction(self): return contextlib.nullcontext()
        repo = Repo()
        handler = object.__new__(panel.Handler)
        handler.server = SimpleNamespace(thunder_service=SimpleNamespace(repository=repo),
                                        thunder_connectsafely_app=SimpleNamespace(clock=lambda: 100))
        handler.command = 'POST'
        handler.thunder_actor = lambda: 'ygor'
        handler.trusted_confirmation_peer = lambda: True
        handler.responder = lambda status, value: (status, value)
        body = {'preview_id': 'p', 'review_hash': 'review', 'observation_hash': 'observation',
                'identity_confirmation': 'CONFIRMO A IDENTIDADE DIVERGENTE'}
        handler.corpo = lambda: body
        route = urlsplit('/api/os/v1/linkedin-cadence-identity-confirmations')
        for key in ('review_hash', 'observation_hash', 'identity_confirmation'):
            original = body[key]
            body[key] = 'wrong'
            with self.assertRaises(panel.Erro): handler.thunder_api(route)
            body[key] = original
        handler.trusted_confirmation_peer = lambda: False
        with self.assertRaises(panel.Erro): handler.thunder_api(route)
        handler.trusted_confirmation_peer = lambda: True
        self.assertEqual(handler.thunder_api(route)[0], 201)
        self.assertEqual(repo.get_state('linkedin_cadence_identity_confirmation', 'p')['actor'], 'ygor')
        repo.records[('connectsafely_preview', 'p')]['expires_at'] = 99
        with self.assertRaises(panel.Erro): handler.thunder_api(route)


if __name__ == '__main__':
    unittest.main()
