"""Real isolated nginx/socket checks; no provider, production state or credentials."""
import base64
import http.client
import json
import os
import pwd
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

import painel_servidor as panel


class ProxySessionTests(unittest.TestCase):
    def test_stale_socket_restart_and_active_listener_preservation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'panel.sock'
            with socket.socket(socket.AF_UNIX) as stale:
                stale.bind(str(path))
            server = panel.confirmation_server(SimpleNamespace(), path, os.getuid(), sys.executable)
            try:
                inode = path.stat().st_ino
                self.assertEqual(path.stat().st_mode & 0o777, 0o660)
                with self.assertRaises(ValueError):
                    panel.confirmation_server(SimpleNamespace(), path, os.getuid(), sys.executable)
                self.assertEqual(path.stat().st_ino, inode)
            finally:
                server.server_close()
            self.assertFalse(path.exists())
            server = panel.confirmation_server(SimpleNamespace(), path, os.getuid(), sys.executable)
            server.server_close()

    @unittest.skipUnless(shutil.which('nginx') and shutil.which('openssl'), 'nginx/openssl required')
    def test_nginx_basic_auth_session_post_and_direct_forgery(self):
        with tempfile.TemporaryDirectory(prefix='thunder-session-') as temp:
            root = Path(temp)
            root.chmod(0o755)
            nginx = str(Path(shutil.which('nginx')).resolve())
            nginx_uid = pwd.getpwnam('www-data').pw_uid if os.geteuid() == 0 else os.getuid()
            nginx_user = 'user www-data;' if os.geteuid() == 0 else ''
            tcp = panel.ThreadingHTTPServer(('127.0.0.1', 0), panel.Handler)
            tcp.banco = SimpleNamespace()
            tcp.thunder_nonces = {}
            tcp.thunder_origin = 'http://localhost'
            calls = []
            tcp.thunder_service = SimpleNamespace(repository=SimpleNamespace(),
                control_agent=lambda body, actor: calls.append((body, actor)) or {'paused': True})
            unix = panel.confirmation_server(tcp, root / 'panel.sock', nginx_uid, nginx,
                                              root / 'nginx.pid')
            threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (tcp, unix)]
            for thread in threads:
                thread.start()
            password = 'synthetic-test-only'
            encoded = subprocess.check_output(['openssl', 'passwd', '-apr1', password], text=True).strip()
            (root / 'htpasswd').write_text('ygor:' + encoded + '\n')
            with socket.socket() as port_socket:
                port_socket.bind(('127.0.0.1', 0))
                port = port_socket.getsockname()[1]
            config = root / 'nginx.conf'
            config.write_text(f'''daemon off;
master_process on;
worker_processes 1;
{nginx_user}
pid {root}/nginx.pid;
error_log {root}/nginx.log;
events {{ worker_connections 32; }}
http {{ access_log off; server {{ listen 127.0.0.1:{port};
location / {{ auth_basic "isolated"; auth_basic_user_file {root}/htpasswd;
proxy_pass http://unix:{root}/panel.sock:;
proxy_set_header Host localhost;
proxy_set_header X-Authenticated-User $remote_user;
}} }} }}
''')
            subprocess.run([nginx, '-t', '-c', str(config), '-p', temp], check=True, capture_output=True)
            process = subprocess.Popen([nginx, '-c', str(config), '-p', temp],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            auth = 'Basic ' + base64.b64encode(f'ygor:{password}'.encode()).decode()
            def request(target_port, method='GET', path='/api/os/v1/session', body=None, headers=None):
                connection = http.client.HTTPConnection('127.0.0.1', target_port, timeout=3)
                try:
                    connection.request(method, path, body=body,
                        headers={'Host': 'localhost', **(headers or {})})
                    response = connection.getresponse()
                    return response.status, response.read()
                finally:
                    connection.close()
            try:
                for _ in range(100):
                    try:
                        status, _ = request(port)
                        break
                    except ConnectionRefusedError:
                        time.sleep(.02)
                else:
                    self.fail('isolated nginx did not start')
                self.assertEqual(status, 401)
                status, raw = request(tcp.server_port, headers={'X-Authenticated-User': 'ygor'})
                self.assertEqual(status, 401)
                status, raw = request(port, headers={'Authorization': auth, 'X-Authenticated-User': 'claude'})
                self.assertEqual(status, 200, raw)
                session = json.loads(raw)
                self.assertEqual(session['actor'], 'ygor')
                headers = {'Authorization': auth, 'Origin': 'http://localhost',
                           'Content-Type': 'application/json', 'X-Thunder-Nonce': session['nonce']}
                status, raw = request(port, 'POST', '/api/os/v1/agent/control', '{"paused":true}', headers)
                self.assertEqual(status, 200, raw)
                self.assertEqual(calls, [({'paused': True}, 'ygor')])
                self.assertEqual(request(port, 'POST', '/api/os/v1/agent/control',
                                         '{"paused":true}', headers)[0], 401)
                self.assertEqual(request(tcp.server_port, 'POST', '/api/os/v1/agent/control',
                    '{"paused":true}', {**headers, 'X-Authenticated-User': 'ygor'})[0], 401)
                with socket.socket(socket.AF_UNIX) as direct:
                    direct.settimeout(3)
                    direct.connect(str(root / 'panel.sock'))
                    direct.sendall(b'GET /api/os/v1/session HTTP/1.0\r\nHost: localhost\r\nX-Authenticated-User: ygor\r\n\r\n')
                    self.assertIn(b' 403 ', direct.recv(4096))
                self.assertEqual(len(calls), 1)
                # A second nginx using the same binary and UID is not the
                # configured master/worker, even with valid Basic Auth.
                with socket.socket() as shadow_port_socket:
                    shadow_port_socket.bind(('127.0.0.1', 0))
                    shadow_port = shadow_port_socket.getsockname()[1]
                shadow_config = root / 'shadow.conf'
                shadow_config.write_text(config.read_text().replace(
                    f'127.0.0.1:{port}', f'127.0.0.1:{shadow_port}').replace(
                    f'pid {root}/nginx.pid;', f'pid {root}/shadow.pid;'))
                shadow = subprocess.Popen([nginx, '-c', str(shadow_config), '-p', temp],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                try:
                    for _ in range(100):
                        try:
                            shadow_status, _ = request(shadow_port, headers={'Authorization': auth})
                            break
                        except ConnectionRefusedError:
                            time.sleep(.02)
                    else:
                        self.fail('isolated shadow nginx did not start')
                    self.assertEqual(shadow_status, 403)
                finally:
                    shadow.terminate()
                    shadow.communicate(timeout=5)
            finally:
                process.terminate()
                process.communicate(timeout=5)
                for server in (tcp, unix):
                    server.shutdown()
                    server.server_close()
                for thread in threads:
                    thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
