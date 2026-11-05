"""Explicit launcher for the isolated ConnectSafely account-read broker.

Runs only the existing fixed GET-only protocol. The caller must supply a private
credential file and verified OS isolation; this module does not install a unit or
prove the service's deployment boundary.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import signal
import stat
import sys

from .connectsafely_read_broker_contract import AccountReadBrokerContract
from .connectsafely_read_broker_runtime import AccountReadBrokerServer, AccountReadHTTPS


class ServiceConfigurationError(ValueError):
    pass


class _StopService(BaseException):
    pass


_FIELDS = {
    'company': 'THUNDER_LI_BROKER_COMPANY',
    'account': 'THUNDER_LI_BROKER_ACCOUNT',
    'public_id': 'THUNDER_LI_BROKER_PUBLIC_ID',
    'panel_uid': 'THUNDER_LI_BROKER_PANEL_UID',
    'socket_gid': 'THUNDER_LI_BROKER_SOCKET_GID',
    'socket': 'THUNDER_LI_BROKER_SOCKET',
    'credential_file': 'THUNDER_LI_BROKER_CREDENTIAL_FILE',
}


def _absolute_path(value):
    if (not isinstance(value, str) or not value.startswith('/') or '\x00' in value
            or any(part in ('.', '..') for part in value.split('/'))
            or '//' in value or value.endswith('/')):
        raise ServiceConfigurationError('configuration_invalid')
    return value


def parse_configuration(environ):
    try:
        values = {name: environ[key] for name, key in _FIELDS.items()}
        if any(not isinstance(value, str) or not value for value in values.values()):
            raise ValueError
        for name in ('panel_uid', 'socket_gid'):
            if not re.fullmatch(r'[1-9][0-9]{0,9}', values[name]):
                raise ValueError
            values[name] = int(values[name])
            if values[name] > 2**31 - 1:
                raise ValueError
        for name in ('socket', 'credential_file'):
            values[name] = _absolute_path(values[name])
        if values['socket'] == values['credential_file']:
            raise ValueError
        if (not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}', values['company'])
                or not re.fullmatch(r'[a-f0-9]{24}', values['account'])
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', values['public_id'])):
            raise ValueError
        return values
    except (KeyError, TypeError, ValueError):
        raise ServiceConfigurationError('configuration_invalid') from None


def private_credential(path):
    """Read only a private regular file owned by this service, without symlinks."""
    try:
        candidate = Path(_absolute_path(path))
        if candidate.resolve(strict=True) != candidate:
            raise ValueError
        fd = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or not 0 < info.st_size <= 4096):
                raise ValueError
            data = stream.read(4097)
        if len(data) > 4096:
            raise ValueError
        value = data.decode('ascii').strip()
        if not re.fullmatch(r'[A-Za-z0-9._~-]{16,4096}', value):
            raise ValueError
        return value
    except (OSError, UnicodeError, ValueError):
        raise ServiceConfigurationError('credential_unavailable') from None


def create_server(environ):
    config = parse_configuration(environ)
    if config['panel_uid'] == os.geteuid() or config['socket_gid'] != os.getegid():
        raise ServiceConfigurationError('configuration_invalid')
    # The secret is opened only after an authenticated, allowed read request.
    transport = AccountReadHTTPS(config['account'], lambda: private_credential(config['credential_file']))
    contract = AccountReadBrokerContract(config['company'], config['account'], config['public_id'], transport)
    return AccountReadBrokerServer(config['socket'], allowed_uid=config['panel_uid'],
                                   socket_gid=config['socket_gid'], contract=contract)


def run(environ):
    server = create_server(environ)
    previous = {}

    def stop(_signum, _frame):
        raise _StopService()

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, stop)
        server.start()
        while True:
            server.serve_once()
    except _StopService:
        return 0
    finally:
        server.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main():
    try:
        return run(os.environ)
    except Exception:
        # Static output: neither environment, paths, exception nor secret leaks.
        print('connectsafely_read_broker_unavailable', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
