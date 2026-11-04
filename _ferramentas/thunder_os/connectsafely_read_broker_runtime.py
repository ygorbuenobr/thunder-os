"""Linux-only local account-read broker and fixed GET-only provider transport.

The caller must inject credential loading and run this in a dedicated,
unprivileged, single-threaded process. This module does not establish operating
system isolation or authorize the executor; staging must verify those controls.
"""
from __future__ import annotations

import json
import http.client
import os
from pathlib import Path
import re
import select
import signal
import socket
import ssl
import stat
import struct
import sys
import threading
import time

from .connectsafely_read_broker_contract import AccountReadBrokerContract, ReadBrokerError

MAX_FRAME = 4096
_SAFE_ERRORS = frozenset({'invalid_request', 'scope_mismatch', 'provider_unavailable',
                          'invalid_response', 'identity_mismatch', 'observation_time_invalid'})
_MAX_PROVIDER_RESPONSE = 1_000_000


class BrokerRuntimeError(RuntimeError):
    pass


def _verify_process_identity():
    """Reject privileged or privilege-retaining Linux service identities."""
    try:
        if sys.platform != 'linux' or not hasattr(socket, 'SO_PEERCRED'):
            raise ValueError('linux_peer_credentials_required')
        uids, gids = os.getresuid(), os.getresgid()
        if (len(uids) != 3 or len(gids) != 3
                or any(type(value) is not int or value <= 0 for value in (*uids, *gids))
                or len(set(uids)) != 1 or len(set(gids)) != 1
                or 0 in os.getgroups()):
            raise ValueError('privileged_or_ambiguous_identity')
        required = {'CapEff', 'CapPrm', 'CapAmb'}
        found = {}
        with open('/proc/self/status', encoding='ascii') as status:
            for line in status:
                name, sep, value = line.partition(':')
                if sep and name in required:
                    if name in found:
                        raise ValueError('duplicate_capabilities')
                    value = value.strip()
                    if not re.fullmatch(r'[0-9a-fA-F]{1,16}', value):
                        raise ValueError('invalid_capabilities')
                    found[name] = int(value, 16)
        if set(found) != required or any(found.values()):
            raise ValueError('capabilities_present_or_unknown')
    except (AttributeError, OSError, ValueError, TypeError, UnicodeError):
        raise BrokerRuntimeError('unsupported_isolation') from None


class AccountReadHTTPS:
    """Fixed-host, GET-only transport intended to run inside the broker only.

    No credential is loaded until a valid allowed request. Real credential scope
    is not inferred and must be isolated from the panel by the operating system.
    """

    def __init__(self, account_id, credential_loader, *, connection_factory=None, timeout=2.0):
        if (not isinstance(account_id, str) or not re.fullmatch(r'[a-f0-9]{24}', account_id)
                or not callable(credential_loader) or type(timeout) not in (int, float)
                or not 0 < timeout <= 5 or (connection_factory is not None and not callable(connection_factory))):
            raise BrokerRuntimeError('invalid_configuration')
        self._account_id = account_id
        self._credential_loader = credential_loader
        self._factory = connection_factory or http.client.HTTPSConnection
        self._timeout = float(timeout)

    def __call__(self, path, query):
        from urllib.parse import urlencode
        if (not isinstance(query, dict) or not (
                path == '/linkedin/accounts' and query == {'q': self._account_id}
                or path == '/linkedin/account/' + self._account_id + '/status' and query == {})):
            raise BrokerRuntimeError('route_blocked')
        if os.environ.get('SSL_CERT_FILE') or os.environ.get('SSL_CERT_DIR'):
            raise BrokerRuntimeError('tls_configuration_rejected')
        conn = None
        try:
            secret = self._credential_loader()
            if (not isinstance(secret, str) or not re.fullmatch(r'[A-Za-z0-9._~-]{16,4096}', secret)):
                raise BrokerRuntimeError('credential_unavailable')
            conn = self._factory('api.connectsafely.ai', 443, timeout=self._timeout,
                                 context=ssl.create_default_context())
            target = path + ('?' + urlencode(query) if query else '')
            conn.request('GET', target, body=None,
                         headers={'Authorization': 'Bearer ' + secret, 'Accept': 'application/json'})
            secret = None
            response = conn.getresponse()
            if type(response.status) is not int or response.status != 200:
                return response.status if type(response.status) is int else 0, {}
            raw = response.read(_MAX_PROVIDER_RESPONSE + 1)
            if not isinstance(raw, bytes) or len(raw) > _MAX_PROVIDER_RESPONSE:
                raise BrokerRuntimeError('invalid_response')
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError('duplicate')
                    result[key] = value
                return result
            payload = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                                 parse_constant=lambda _: (_ for _ in ()).throw(ValueError('constant')))
            if not isinstance(payload, (dict, list)):
                raise BrokerRuntimeError('invalid_response')
            return 200, payload
        except BrokerRuntimeError:
            raise
        except Exception:
            raise BrokerRuntimeError('provider_unavailable') from None
        finally:
            if conn is not None:
                conn.close()


def _decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate')
            result[key] = value
        return result
    try:
        if not isinstance(raw, bytes) or not raw.endswith(b'\n') or len(raw) > MAX_FRAME:
            raise ValueError('frame')
        return json.loads(raw[:-1].decode('utf-8'), object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError('constant')))
    except (ValueError, UnicodeError, RecursionError):
        raise BrokerRuntimeError('invalid_request') from None


def _frame(value):
    try:
        wire = json.dumps(value, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii') + b'\n'
        if len(wire) > MAX_FRAME:
            raise ValueError('large')
        return wire
    except (TypeError, ValueError, RecursionError):
        return b'{"error":"invalid_response"}\n'


def _read_frame(conn, deadline):
    data = bytearray()
    while b'\n' not in data:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BrokerRuntimeError('timeout')
        conn.settimeout(remaining)
        try:
            chunk = conn.recv(MAX_FRAME + 1 - len(data))
        except (TimeoutError, OSError):
            raise BrokerRuntimeError('timeout') from None
        if not chunk:
            raise BrokerRuntimeError('invalid_request')
        data.extend(chunk)
        if len(data) > MAX_FRAME:
            raise BrokerRuntimeError('invalid_request')
    if data.count(b'\n') != 1 or not data.endswith(b'\n'):
        raise BrokerRuntimeError('invalid_request')
    return _decode(bytes(data))


def _dispatch_bounded(contract, request, deadline, close_fds=()):
    """Fork a child so a stalled provider cannot hold the broker indefinitely."""
    read_fd, write_fd = os.pipe()
    try:
        pid = os.fork()
    except OSError:
        os.close(read_fd)
        os.close(write_fd)
        raise BrokerRuntimeError('provider_unavailable') from None
    if pid == 0:
        try:
            os.close(read_fd)
            for fd in close_fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                response = contract.dispatch(request)
            except ReadBrokerError as exc:
                code = str(exc)
                response = {'error': code if code in _SAFE_ERRORS else 'provider_unavailable'}
            except BaseException:
                response = {'error': 'provider_unavailable'}
            wire = _frame(response)
            while wire:
                wire = wire[os.write(write_fd, wire):]
        except BaseException:
            pass
        finally:
            os._exit(0)
    os.close(write_fd)
    data = bytearray()
    try:
        while b'\n' not in data:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([read_fd], [], [], remaining)[0]:
                raise BrokerRuntimeError('timeout')
            chunk = os.read(read_fd, MAX_FRAME + 1 - len(data))
            if not chunk:
                raise BrokerRuntimeError('provider_unavailable')
            data.extend(chunk)
            if len(data) > MAX_FRAME:
                raise BrokerRuntimeError('provider_unavailable')
        if data.count(b'\n') != 1 or not data.endswith(b'\n'):
            raise BrokerRuntimeError('provider_unavailable')
        return _decode(bytes(data))
    finally:
        os.close(read_fd)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(pid, 0)


class AccountReadBrokerServer:
    """Single-request-at-a-time Linux Unix socket server, deny by default."""

    def __init__(self, socket_path, *, allowed_uid, socket_gid, contract, timeout=3.0):
        _verify_process_identity()
        if threading.active_count() != 1:
            raise BrokerRuntimeError('unsupported_isolation')
        if (type(allowed_uid) is not int or allowed_uid <= 0 or allowed_uid == os.geteuid()
                or type(socket_gid) is not int or socket_gid <= 0
                or type(contract) is not AccountReadBrokerContract
                or type(timeout) not in (int, float) or not 0 < timeout <= 10):
            raise BrokerRuntimeError('invalid_configuration')
        self.path = Path(socket_path)
        if not self.path.is_absolute() or len(os.fsencode(self.path)) > 100:
            raise BrokerRuntimeError('invalid_socket_path')
        self.allowed_uid, self.socket_gid = allowed_uid, socket_gid
        self.contract, self.timeout = contract, float(timeout)
        self._socket = None
        self._owned_inode = None

    def start(self):
        _verify_process_identity()
        if self._socket is not None:
            raise BrokerRuntimeError('already_started')
        parent = self.path.parent
        try:
            info = parent.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_gid != self.socket_gid or stat.S_IMODE(info.st_mode) != 0o750
                    or parent.resolve(strict=True) != parent):
                raise BrokerRuntimeError('unsafe_socket_directory')
            # Never unlink or replace a previous socket or any other file.
            if os.path.lexists(self.path):
                raise BrokerRuntimeError('socket_path_exists')
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            old_umask = os.umask(0o117)
            try:
                listener.bind(str(self.path))
                bound = self.path.lstat()
                self._owned_inode = (bound.st_dev, bound.st_ino)
            finally:
                os.umask(old_umask)
            os.chown(self.path, os.geteuid(), self.socket_gid)
            os.chmod(self.path, 0o660)
            info = self.path.lstat()
            if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_gid != self.socket_gid or stat.S_IMODE(info.st_mode) != 0o660):
                raise BrokerRuntimeError('unsafe_socket')
            listener.listen(8)
            self._socket = listener
        except Exception:
            if 'listener' in locals():
                listener.close()
            self._cleanup_path()
            raise

    def _cleanup_path(self):
        if self._owned_inode is None:
            return
        try:
            info = self.path.lstat()
            if (info.st_dev, info.st_ino) == self._owned_inode and stat.S_ISSOCK(info.st_mode):
                self.path.unlink()
        except FileNotFoundError:
            pass
        finally:
            self._owned_inode = None

    def serve_once(self):
        if self._socket is None:
            raise BrokerRuntimeError('not_started')
        _verify_process_identity()
        if threading.active_count() != 1:
            raise BrokerRuntimeError('unsupported_isolation')
        conn, _ = self._socket.accept()
        deadline = time.monotonic() + self.timeout
        with conn:
            try:
                _verify_process_identity()
                pid, uid, _gid = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if pid <= 0 or uid != self.allowed_uid:
                    raise BrokerRuntimeError('unauthorized')
                request = _read_frame(conn, deadline)
                result = _dispatch_bounded(self.contract, request, deadline,
                                           close_fds=(self._socket.fileno(), conn.fileno()))
            except BrokerRuntimeError as exc:
                code = str(exc)
                result = {'error': code if code in {'unauthorized', 'timeout', 'invalid_request',
                          'provider_unavailable'} else 'provider_unavailable'}
            except Exception:
                result = {'error': 'provider_unavailable'}
            try:
                conn.settimeout(max(0.001, deadline - time.monotonic()))
                conn.sendall(_frame(result))
            except OSError:
                pass

    def close(self):
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        self._cleanup_path()
