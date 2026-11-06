"""Panel-side account-status IPC: no provider credentials or send capability.

The broker must run under a separate unprivileged Linux identity. A successful
read proves only the returned account observation, never executor binding.
"""
from __future__ import annotations

import json
import math
import os
import re
import socket
import stat
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class ReadBrokerClientError(ValueError):
    """Static diagnostics only, never include peer/provider contents."""


def _identifier(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,160}', value) is not None


def _unique(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError('duplicate')
        obj[key] = value
    return obj


def _peer_uid(sock):
    if sys.platform != 'linux' or not hasattr(socket, 'SO_PEERCRED'):
        raise ReadBrokerClientError('peer_identity_unsupported')
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
    if not isinstance(raw, bytes) or len(raw) != struct.calcsize('3i'):
        raise ReadBrokerClientError('peer_identity_unverified')
    _, uid, _ = struct.unpack('3i', raw)
    return uid


def _verify_panel_identity(broker_uid):
    """Reject retained root, saved IDs, or Linux process capabilities."""
    if sys.platform != 'linux' or not callable(getattr(os, 'getresuid', None)):
        raise ReadBrokerClientError('peer_identity_unsupported')
    try:
        uids = os.getresuid()
        if (not isinstance(uids, tuple) or len(uids) != 3
                or any(type(uid) is not int or uid <= 0 for uid in uids)
                or len(set(uids)) != 1 or uids[0] == broker_uid):
            raise ReadBrokerClientError('panel_identity_privileged')
        with open('/proc/self/status', encoding='ascii') as stream:
            data = stream.read(16385)
        if len(data) > 16384:
            raise ValueError('oversized status')
        capabilities = {}
        for line in data.splitlines():
            match = re.fullmatch(r'(CapEff|CapPrm|CapAmb):\s*([0-9a-fA-F]{1,16})', line)
            if match:
                if match.group(1) in capabilities:
                    raise ValueError('duplicate capability')
                capabilities[match.group(1)] = int(match.group(2), 16)
        if set(capabilities) != {'CapEff', 'CapPrm', 'CapAmb'} or any(capabilities.values()):
            raise ReadBrokerClientError('panel_identity_privileged')
    except ReadBrokerClientError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError):
        raise ReadBrokerClientError('panel_identity_unverified') from None


class AccountStatusBrokerClient:
    """Perform a bounded, authenticated read of one company/account binding."""

    MAX_WIRE = 4096
    _FIELDS = frozenset({'version', 'operation', 'request_id', 'company_id', 'account_id',
                         'public_id', 'status', 'available', 'source', 'observed_at'})

    def __init__(self, socket_path, *, broker_uid, company_id, account_id, public_id,
                 timeout=3.0, max_age_seconds=60, socket_factory=socket.socket,
                 clock=lambda: datetime.now(timezone.utc), monotonic=time.monotonic):
        if (not isinstance(socket_path, str) or not Path(socket_path).is_absolute()
                or '\x00' in socket_path or len(socket_path.encode()) > 100
                or type(broker_uid) is not int or broker_uid <= 0
                or not _identifier(company_id) or not isinstance(account_id, str)
                or not re.fullmatch(r'[a-f0-9]{24}', account_id)
                or not isinstance(public_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', public_id)
                or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 10
                or type(max_age_seconds) not in (int, float) or not math.isfinite(max_age_seconds)
                or not 0 < max_age_seconds <= 300 or not callable(socket_factory)
                or not callable(clock) or not callable(monotonic)):
            raise ReadBrokerClientError('configuration_invalid')
        self._socket_path, self._broker_uid = socket_path, broker_uid
        self._company_id, self._account_id, self._public_id = company_id, account_id, public_id
        self._timeout, self._max_age = timeout, max_age_seconds
        self._socket_factory, self._clock, self._monotonic = socket_factory, clock, monotonic

    @property
    def company_id(self):
        return self._company_id

    @property
    def account_id(self):
        return self._account_id

    @property
    def public_id(self):
        return self._public_id

    def account_status(self):
        _verify_panel_identity(self._broker_uid)
        # Refuse symlinks and sockets writable by unauthorised users. Peer UID
        # is also checked after connect, before any request is transmitted.
        try:
            info = os.lstat(self._socket_path)
            parent = os.lstat(str(Path(self._socket_path).parent))
        except OSError:
            raise ReadBrokerClientError('broker_unavailable') from None
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != self._broker_uid
                or info.st_mode & 0o007 or not stat.S_ISDIR(parent.st_mode)
                or parent.st_uid != self._broker_uid or parent.st_mode & 0o022):
            raise ReadBrokerClientError('socket_untrusted')
        request_id = uuid4().hex
        request = {'version': 1, 'operation': 'account_status', 'company_id': self._company_id,
                   'account_id': self._account_id, 'request_id': request_id}
        wire = (json.dumps(request, separators=(',', ':')) + '\n').encode('utf-8')
        deadline = self._monotonic() + self._timeout
        try:
            with self._socket_factory(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                self._remaining(sock, deadline)
                sock.connect(self._socket_path)
                if _peer_uid(sock) != self._broker_uid:
                    raise ReadBrokerClientError('peer_identity_mismatch')
                self._remaining(sock, deadline)
                sock.sendall(wire)
                data = b''
                while b'\n' not in data:
                    self._remaining(sock, deadline)
                    chunk = sock.recv(self.MAX_WIRE + 1 - len(data))
                    if not chunk or len(data) + len(chunk) > self.MAX_WIRE:
                        raise ReadBrokerClientError('response_invalid')
                    data += chunk
                if not data.endswith(b'\n') or data.count(b'\n') != 1:
                    raise ReadBrokerClientError('response_invalid')
        except ReadBrokerClientError:
            raise
        except socket.timeout:
            raise ReadBrokerClientError('broker_timeout') from None
        except (OSError, ValueError, TimeoutError):
            raise ReadBrokerClientError('broker_unavailable') from None
        try:
            result = json.loads(data.decode('utf-8'), object_pairs_hook=_unique,
                                parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid number')))
            if not isinstance(result, dict) or set(result) != self._FIELDS:
                raise ValueError('unexpected fields')
            if (type(result['version']) is not int or result['version'] != 1
                    or result['operation'] != 'account_status' or result['request_id'] != request_id
                    or result['company_id'] != self._company_id or result['account_id'] != self._account_id
                    or result['public_id'] != self._public_id or result['source'] != 'connectsafely'
                    or result['status'] not in ('AVAILABLE', 'IN_USE', 'ERROR', 'WARMUP', 'UNKNOWN')
                    or type(result['available']) is not bool
                    or (result['available'] and result['status'] != 'AVAILABLE')):
                raise ValueError('binding mismatch')
            observed = datetime.fromisoformat(result['observed_at'])
            now = self._clock()
            if (observed.tzinfo is None or observed.utcoffset() is None or not isinstance(now, datetime)
                    or now.tzinfo is None or now.utcoffset() is None):
                raise ValueError('invalid time')
            age = (now - observed).total_seconds()
            if not 0 <= age <= self._max_age:
                raise ValueError('stale')
        except (ValueError, TypeError, KeyError, OverflowError):
            raise ReadBrokerClientError('observation_unverified') from None
        return {'id': self._account_id, 'publicId': self._public_id, 'available': result['available'],
                'observed_at': result['observed_at'], 'source': result['source'], 'write_authorized': False}

    def _remaining(self, sock, deadline):
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise ReadBrokerClientError('broker_timeout')
        sock.settimeout(remaining)
