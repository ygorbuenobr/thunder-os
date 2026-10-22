"""Explicit, bounded webhook exposure; never enables a CRM/provider writer."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import tempfile
import urllib.error
import urllib.request

CONFIG = Path('/etc/nginx/sites-available/painel.r3xdigital.tech')
SECRET = Path('/etc/thunder-os/instantly_webhook_secret')
AUTH = Path('/etc/thunder-os/instantly_webhook_auth.conf')
BACKUP = Path('/opt/thunder-os/staging/panel-cadencias/instantly-ingress-config')
STANZA = '''    # Thunder Instantly capture-only ingress; header authentication in backend.
    location = /api/os/v1/integrations/instantly/events {
        auth_basic off;
        include /etc/thunder-os/instantly_webhook_auth.conf;
        access_log off;
        error_log /dev/null crit;
        client_max_body_size 200k;
        client_body_timeout 10s;
        limit_except POST { deny all; }
        proxy_pass http://127.0.0.1:8790;
        proxy_http_version 1.1;
        proxy_request_buffering off;
        proxy_set_header Host $host;
        proxy_set_header X-Authenticated-User "";
        proxy_set_header X-Thunder-Instantly-Secret $http_x_thunder_instantly_secret;
        proxy_read_timeout 15s;
    }
'''


def secret_header_rule(secret):
    if not isinstance(secret, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', secret):
        raise ValueError('unsafe_secret_format')
    return 'if ($http_x_thunder_instantly_secret != "' + secret + '") { return 401; }\n'


def private_root_text(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'r', encoding='ascii') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096):
                raise ValueError()
            value = stream.read(4097)
        if len(value) > 4096:
            raise ValueError()
        return value
    except Exception:
        raise SystemExit('unsafe_private_file') from None


def render(original):
    marker = '    location = /saude {'
    if STANZA in original:
        return original
    if original.count(marker) != 1 or 'integrations/instantly/events' in original:
        raise ValueError('nginx_topology_changed')
    if 'auth_basic "Painel Thunder";' not in original:
        raise ValueError('panel_login_not_found')
    return original.replace(marker, STANZA + marker, 1)


def atomic_config(value):
    fd, name = tempfile.mkstemp(prefix='.instantly-', dir=CONFIG.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o644)
        os.replace(name, CONFIG)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=('prepare', 'expose', 'rollback'))
    parser.add_argument('--expected-config-sha256', required=True)
    parser.add_argument('--expected-source-sha')
    args = parser.parse_args()
    if os.uname().nodename != 'srv1433640' or os.getuid() != 0:
        raise SystemExit('wrong_destination')
    os.umask(0o077)
    lock = open('/run/lock/thunder-os-panel-promotion.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if CONFIG.is_symlink() or not CONFIG.is_file():
        raise SystemExit('unsafe_nginx_config')
    original = CONFIG.read_text()
    observed = hashlib.sha256(original.encode()).hexdigest()
    if observed != args.expected_config_sha256:
        raise SystemExit('nginx_config_changed')
    BACKUP.mkdir(mode=0o700, exist_ok=True)
    saved = BACKUP / 'nginx-before.conf'
    if args.operation == 'prepare':
        render(original)
        if not saved.exists():
            saved.write_text(original)
        elif saved.read_text() != original:
            raise SystemExit('backup_conflict')
        if not SECRET.exists() and not SECRET.is_symlink():
            fd = os.open(SECRET, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(secrets.token_urlsafe(48) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
        info = SECRET.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise SystemExit('unsafe_secret_file')
        rule = secret_header_rule(private_root_text(SECRET).strip())
        if not AUTH.exists() and not AUTH.is_symlink():
            fd = os.open(AUTH, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(rule)
                stream.flush()
                os.fsync(stream.fileno())
        info = AUTH.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077
                or private_root_text(AUTH) != rule):
            raise SystemExit('unsafe_auth_include')
        print(json.dumps({'prepared': True, 'secret_path': str(SECRET), 'secret_printed': False,
                          'nginx_changed': False, 'backup': str(saved)}))
        return
    if not saved.is_file():
        raise SystemExit('preparation_missing')
    if args.operation == 'expose':
        secret = private_root_text(SECRET).strip()
        if private_root_text(AUTH) != secret_header_rule(secret):
            raise SystemExit('authentication_not_prepared')
        if (not args.expected_source_sha or
                Path('/opt/thunder-os/current').resolve().name != args.expected_source_sha):
            raise SystemExit('panel_revision_changed')
        with urllib.request.urlopen('http://127.0.0.1:8790/saude', timeout=5) as response:
            if json.load(response).get('source_sha') != args.expected_source_sha:
                raise SystemExit('panel_health_revision_mismatch')
        request = urllib.request.Request('http://127.0.0.1:8790/api/os/v1/integrations/instantly/events',
            data=b'{}', headers={'Content-Type': 'application/json',
                                'X-Thunder-Instantly-Secret': secret})
        try:
            urllib.request.urlopen(request, timeout=5).close()
        except urllib.error.HTTPError as error:
            if error.code != 422:
                raise SystemExit('capture_authentication_not_ready') from None
        else:
            raise SystemExit('capture_validation_not_ready')
        if (private_root_text(SECRET).strip() != secret or
                private_root_text(AUTH) != secret_header_rule(secret)):
            raise SystemExit('authentication_changed_during_preflight')
    desired = render(original) if args.operation == 'expose' else saved.read_text()
    atomic_config(desired)
    try:
        subprocess.run(['nginx', '-t'], check=True, capture_output=True)
        subprocess.run(['systemctl', 'reload', 'nginx'], check=True, capture_output=True)
    except BaseException:
        atomic_config(original)
        subprocess.run(['nginx', '-t'], check=True, capture_output=True)
        subprocess.run(['systemctl', 'reload', 'nginx'], check=True, capture_output=True)
        raise SystemExit('nginx_validation_failed_restored') from None
    print(json.dumps({'operation': args.operation, 'config_sha256': hashlib.sha256(desired.encode()).hexdigest(),
                      'backup': str(saved), 'crm_writer_enabled': False, 'send_enabled': False}))


if __name__ == '__main__':
    main()
