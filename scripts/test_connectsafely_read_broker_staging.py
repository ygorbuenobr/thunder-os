#!/usr/bin/env python3
"""Linux staging: broker/panel/third UID, fake provider, private network ns.

Run as root inside `unshare -n`; copies of the package must be beside scripts/.
This starts only task-owned subprocesses and uses a synthetic credential. It
does not start or modify the actual panel, broker, systemd units or providers.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / '_ferramentas'))

COMPANY = 'staging-company'
ACCOUNT = 'a' * 24
PUBLIC_ID = 'staging-person'
SYNTHETIC_CREDENTIAL = 'synthetic-credential-no-provider-access'
BROKER_UID, PANEL_UID, THIRD_UID, SHARED_GID = 273401, 273402, 273403, 273404


def require(condition, reason):
    if not condition:
        raise AssertionError(reason)


def assert_no_credential(path):
    try:
        Path(path).read_bytes()
    except PermissionError:
        return
    raise AssertionError('credential_was_readable')


def broker(socket_path, credential_path):
    from thunder_os.connectsafely_read_broker_contract import AccountReadBrokerContract
    from thunder_os.connectsafely_read_broker_runtime import AccountReadBrokerServer

    def fake_get(path, query):
        require(Path(credential_path).read_text() == SYNTHETIC_CREDENTIAL, 'fake_credential_unavailable')
        if path == '/linkedin/accounts' and query == {'q': ACCOUNT}:
            return 200, [{'id': ACCOUNT, 'publicId': PUBLIC_ID, 'token': SYNTHETIC_CREDENTIAL}]
        if path == '/linkedin/account/' + ACCOUNT + '/status' and query == {}:
            return 200, {'id': ACCOUNT, 'publicId': PUBLIC_ID, 'status': 'AVAILABLE',
                         'enabled': True, 'hasTokens': True, 'token': SYNTHETIC_CREDENTIAL}
        raise AssertionError('unexpected_fake_provider_route')

    contract = AccountReadBrokerContract(COMPANY, ACCOUNT, PUBLIC_ID, fake_get)
    server = AccountReadBrokerServer(socket_path, allowed_uid=PANEL_UID, socket_gid=SHARED_GID,
                                     contract=contract, timeout=3.0)
    try:
        server.start()
        for _ in range(6):
            server.serve_once()
    finally:
        server.close()


def raw_request(socket_path, payload):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(4)
        peer.connect(socket_path)
        peer.sendall(json.dumps(payload, separators=(',', ':')).encode() + b'\n')
        data = b''
        while not data.endswith(b'\n') and len(data) <= 4096:
            chunk = peer.recv(4096)
            if not chunk:
                break
            data += chunk
    return json.loads(data)


def panel(socket_path, credential_path, broker_pid, panel_socket_path):
    import painel_servidor as panel_server
    from thunder_os.connectsafely_read_broker_client import AccountStatusBrokerClient

    require(os.geteuid() == PANEL_UID, 'panel_uid_mismatch')
    assert_no_credential(credential_path)
    try:
        Path(f'/proc/{broker_pid}/mem').open('rb').close()
    except PermissionError:
        pass
    else:
        raise AssertionError('broker_memory_readable')
    try:
        (Path(socket_path).parent / 'unauthorized-file').write_text('no')
    except PermissionError:
        pass
    else:
        raise AssertionError('broker_directory_writable')
    client = AccountStatusBrokerClient(socket_path, broker_uid=BROKER_UID, company_id=COMPANY,
                                       account_id=ACCOUNT, public_id=PUBLIC_ID)
    result = client.account_status()
    require(result['id'] == ACCOUNT and result['publicId'] == PUBLIC_ID and result['available'] is True,
            'account_status_unverified')
    require('credential_ref' not in result and SYNTHETIC_CREDENTIAL not in str(result), 'secret_in_response')

    class ForbiddenSender:
        @property
        def direct_client(self):
            raise AssertionError('panel_accessed_send_client')

    service = SimpleNamespace(apollo_linkedin_store_ready=lambda: True,
                              list_apollo_linkedin_reservations=lambda: [],
                              list_apollo_linkedin_account_locks=lambda: [])
    class StagingHandler(panel_server.Handler):
        def trusted_confirmation_peer(self):
            return True  # Simula apenas a fronteira do proxy autenticado.

    server = panel_server.ConfirmationUnixServer(panel_socket_path, StagingHandler)
    server.thunder_service = service
    server.banco = SimpleNamespace()
    server.thunder_connectsafely_app = SimpleNamespace(provider=ForbiddenSender())
    env = {
        'THUNDER_APOLLO_LI_BROKER_SOCKET': socket_path, 'THUNDER_APOLLO_LI_BROKER_UID': str(BROKER_UID),
        'THUNDER_APOLLO_LI_COMPANY': COMPANY, 'THUNDER_APOLLO_LI_ACCOUNT': ACCOUNT,
        'THUNDER_APOLLO_LI_PUBLIC_ID': PUBLIC_ID, 'THUNDER_APOLLO_LI_TEAM': 'team-staging',
        'THUNDER_APOLLO_LI_OWNER': 'owner-staging', 'THUNDER_APOLLO_LI_API_ACTOR': 'actor-staging',
        'THUNDER_APOLLO_LI_SEQUENCE': 'sequence-staging', 'THUNDER_APOLLO_LI_TASK': 'task-staging',
        'THUNDER_APOLLO_LI_BINDINGS': json.dumps([{'company_id': COMPANY, 'team_id': 'team-staging',
            'campaign_id': 'sequence-staging', 'account_id': ACCOUNT, 'public_id': PUBLIC_ID,
            'credential_ref': 'fake-execution-ref', 'owner_ids': ['owner-staging']}]),
    }
    task = {'id': 'task-staging', 'type': 'linkedin_step_connect', 'status': 'scheduled',
            'user_id': 'owner-staging', 'emailer_campaign_id': 'sequence-staging', 'contact_id': 'contact-staging',
            'contact': {'id': 'contact-staging', 'name': 'Staging',
                        'linkedin_url': 'https://www.linkedin.com/in/staging-recipient/'}}
    apollo_calls = []

    def fake_apollo(method, path, body, headers, timeout):
        require(method == 'GET' and body is None, 'unexpected_apollo_operation')
        require(path in ('/api/v1/users/api_profile', '/api/v1/tasks/task-staging'), 'unexpected_apollo_path')
        apollo_calls.append(path)
        data = {'user': {'id': 'actor-staging', 'team_id': 'team-staging'}} if path.endswith('api_profile') else {'task': task}
        return 200, json.dumps(data).encode()

    require(panel_server.configure_apollo_linkedin_read_broker(server, env) is None,
            'panel_accepted_live_sender_in_read_process')
    server.thunder_connectsafely_app = SimpleNamespace(provider=None)
    identity = panel_server.configure_apollo_linkedin_read_broker(server, env)
    require(identity is not None, 'panel_broker_configuration_failed')
    preview = panel_server.configure_apollo_linkedin_preview(server, lambda: 'fake-apollo-token', env)
    require(preview is not None, 'panel_preview_configuration_failed')
    preview._request = fake_apollo
    attention = panel_server.configure_apollo_linkedin_attention(server, lambda: 'fake-apollo-token', env)
    require(attention is not None, 'panel_attention_configuration_failed')

    class FakeReader:
        def list_open_tasks(self):
            return {'tasks': [task], 'coverage': 'complete'}
        def get(self, task_id):
            require(task_id == task['id'], 'unexpected_task')
            return task

    attention.reader = FakeReader()
    class UnixHTTPConnection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(4)
            self.sock.connect(panel_socket_path)

    def get_json(path):
        connection = UnixHTTPConnection('localhost', timeout=4)
        try:
            connection.request('GET', path, headers={'X-Authenticated-User': 'ygor'})
            response = connection.getresponse()
            data = json.loads(response.read())
            require(response.status == 200, 'panel_http_status_' + str(response.status))
            return data
        finally:
            connection.close()

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        projected = get_json('/api/os/v1/integrations/apollo/linkedin-preview')
        require(projected['account']['verified'] is True and projected['execution_enabled'] is False,
                'panel_preview_did_not_verify_readonly_account')
        require(projected['account']['id'] == ACCOUNT, 'panel_http_account_mismatch')
        require(projected['call_preview']['executable'] is False and 'text_unverified' in projected['blockers'],
                'panel_preview_incorrectly_authorized')
        require(len(apollo_calls) == 2, 'fake_apollo_was_not_used')
        observed = get_json('/api/os/v1/integrations/apollo/linkedin-attention')
        require(observed['items'][0]['reason'] == 'execution_binding_unverified',
                'attention_claimed_execution_binding')
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=4)
        require(not thread.is_alive(), 'panel_http_shutdown_failed')
    valid = {'version': 1, 'operation': 'account_status', 'company_id': COMPANY,
             'account_id': ACCOUNT, 'request_id': 'request-staging'}
    bad_operation = dict(valid, operation='send_message')
    require(raw_request(socket_path, bad_operation) == {'error': 'invalid_request'}, 'write_not_rejected')
    wrong_company = dict(valid, company_id='other-company')
    require(raw_request(socket_path, wrong_company) == {'error': 'scope_mismatch'}, 'tenant_not_rejected')
    print('painel: HTTP GET real: preview Apollo falso e atencao com socket real; binding de execucao bloqueado')
    print('painel: segredo e alteracao do broker negados; escrita e outro tenant rejeitados')


def third(socket_path, credential_path):
    require(os.geteuid() == THIRD_UID, 'third_uid_mismatch')
    assert_no_credential(credential_path)
    result = raw_request(socket_path, {'version': 1, 'operation': 'account_status', 'company_id': COMPANY,
                                      'account_id': ACCOUNT, 'request_id': 'third-party'})
    require(result == {'error': 'unauthorized'}, 'third_peer_not_rejected')
    print('terceiro: segredo inacessivel e conexao rejeitada por UID')


def child(role, uid, *args):
    def demote():
        os.setgroups([])
        os.setgid(SHARED_GID)
        os.setuid(uid)
    env = {'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(ROOT / '_ferramentas')}
    return subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--role', role, *map(str, args)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env, preexec_fn=demote)


def checked_output(process, timeout=12):
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise AssertionError('child_timeout') from None
    if process.returncode != 0:
        raise AssertionError(f'child_exit_{process.returncode}: {err[-1500:]}')
    return out.strip()


def orchestrate():
    require(sys.platform == 'linux' and os.geteuid() == 0 and hasattr(socket, 'SO_PEERCRED'),
            'linux_root_peercred_required')
    require(os.stat('/proc/self/ns/net').st_ino != os.stat('/proc/1/ns/net').st_ino,
            'private_network_namespace_required')
    interfaces = {line.split(':', 1)[0].strip() for line in Path('/proc/net/dev').read_text().splitlines()[2:] if ':' in line}
    require(interfaces == {'lo'}, 'network_interfaces_present')
    with tempfile.TemporaryDirectory(prefix='thunder-read-stage-', dir='/tmp') as temp:
        root = Path(temp)
        os.chmod(root, 0o711)
        secret = root / 'fake-credential'
        secret.write_text(SYNTHETIC_CREDENTIAL)
        os.chown(secret, BROKER_UID, SHARED_GID)
        os.chmod(secret, 0o600)
        sock_dir = root / 'socket'
        sock_dir.mkdir()
        os.chown(sock_dir, BROKER_UID, SHARED_GID)
        os.chmod(sock_dir, 0o750)
        panel_dir = root / 'panel'
        panel_dir.mkdir()
        os.chown(panel_dir, PANEL_UID, SHARED_GID)
        os.chmod(panel_dir, 0o700)
        panel_sock = str(panel_dir / 'panel.sock')
        sock = str(sock_dir / 'read.sock')
        server = child('broker', BROKER_UID, sock, secret)
        try:
            deadline = time.monotonic() + 5
            while not Path(sock).exists() and time.monotonic() < deadline:
                if server.poll() is not None:
                    checked_output(server)
                    raise AssertionError('broker_exited_before_socket')
                time.sleep(0.02)
            require(Path(sock).exists(), 'broker_socket_timeout')
            print(checked_output(child('panel', PANEL_UID, sock, secret, server.pid, panel_sock)))
            print(checked_output(child('third', THIRD_UID, sock, secret)))
            checked_output(server)
            print('staging Linux: 3 UIDs, SO_PEERCRED e namespace sem interfaces externas verificados')
        finally:
            if server.poll() is None:
                server.terminate()
                try:
                    server.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.communicate()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--role', choices=('broker', 'panel', 'third'))
    parser.add_argument('args', nargs='*')
    options = parser.parse_args()
    if options.role == 'broker':
        broker(*options.args)
    elif options.role == 'panel':
        panel(*options.args)
    elif options.role == 'third':
        third(*options.args)
    else:
        orchestrate()
