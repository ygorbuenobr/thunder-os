"""Observe the running panel's resource switches, never authorize or change them.

Run locally as root on srv1433640: python3 scripts/read_panel_resource_gates.py
--expected-sha <40 lowercase hex>. Only systemctl show and bounded local reads
are used. No shell, provider, credentials, database, restart, flag writes or
environment dump. Enabled, disabled and unknown observations all exit zero;
only invalid CLI input exits two. This is not an additional publication gate.

Config/policy paths and hashes come only from allowlisted process environment
keys. Credential references inside JSON are never followed or printed. A pin
match observes an artifact, not policy approval, validity or provider readiness.
PGOPTIONS observes options only, not an actual database session's state.
For the seven known flags, effective_enabled describes only the documented
flag predicate: absence keeps value=None/status=unknown but is effectively off.
Invalid/duplicate values stay effectively unknown. Other gates still apply.
"""
from contextlib import contextmanager
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import socket
import stat
import subprocess


HOST = 'srv1433640'
UNIT = 'painel-thunder.service'
SYSTEMCTL = ['/usr/bin/systemctl', 'show', UNIT, '--no-pager',
             '--property=MainPID,ActiveState,FragmentPath,DropInPaths']
FLAGS = ('THUNDER_EXTERNAL_EFFECTS_ENABLED', 'THUNDER_OS_GMAIL_CADENCE_ENABLED',
         'THUNDER_OS_GMAIL_CRM_ENABLED', 'THUNDER_OS_GMAIL_RECONCILE_ENABLED',
         'THUNDER_OS_GMAIL_REPLY_INGEST_ENABLED', 'THUNDER_OS_CODEX_ASSISTED_DISPATCH',
         'THUNDER_OS_CODEX_ASSISTED_WRITER_ENABLED')
FLAG_ACTIVATION_SOURCES = {
    'THUNDER_EXTERNAL_EFFECTS_ENABLED': '_ferramentas/thunder_os/worker.py:build_runtime;'
        '_ferramentas/thunder_os/connectsafely_runtime.py:build',
    'THUNDER_OS_GMAIL_CADENCE_ENABLED': '_ferramentas/thunder_os/thunder_email_runtime.py:configured_adapter',
    'THUNDER_OS_GMAIL_CRM_ENABLED': '_ferramentas/thunder_os/thunder_email_runtime.py:configured_cycle',
    'THUNDER_OS_GMAIL_RECONCILE_ENABLED': '_ferramentas/thunder_os/thunder_email_runtime.py:configured_cycle',
    'THUNDER_OS_GMAIL_REPLY_INGEST_ENABLED': '_ferramentas/thunder_os/thunder_email_runtime.py:configured_cycle',
    'THUNDER_OS_CODEX_ASSISTED_DISPATCH': '_ferramentas/thunder_os/codex_assisted.py:configured_assisted',
    'THUNDER_OS_CODEX_ASSISTED_WRITER_ENABLED': '_ferramentas/thunder_os/codex_assisted.py:configured_assisted',
}
POLICIES = {'codex_assisted': 'THUNDER_OS_CODEX_ASSISTED_POLICY',
            'gmail_cadence': 'THUNDER_OS_GMAIL_CADENCE_POLICY',
            'gmail_crm': 'THUNDER_OS_GMAIL_CRM_POLICY'}
CONFIG_KEY = 'THUNDER_OS_CONNECTSAFELY_CONFIG'
KEYS = (*FLAGS, CONFIG_KEY, CONFIG_KEY + '_SHA256', 'PGOPTIONS',
        *(key + suffix for key in POLICIES.values() for suffix in ('_FILE', '_SHA256')))
WRITER_DROPIN = '/etc/systemd/system/painel-thunder.service.d/60-codex-assisted-writer.conf'
_SHA = re.compile(r'[0-9a-f]{40}\Z')
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')


class ObservationError(Exception):
    """Codes are constants; never propagate file contents or OS error strings."""


class LocalFiles:
    """Logical absolute paths; a private fixture root is available only to tests."""
    def __init__(self, root=Path('/')):
        self.root = Path(root)

    @contextmanager
    def parent(self, path):
        parts = Path(path).parts
        if not parts or parts[0] != '/' or any(p in ('.', '..') for p in parts[1:]):
            raise ObservationError('path_invalid')
        fd = None
        try:
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            for part in parts[1:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd, parts[-1]
        except OSError:
            raise ObservationError('unavailable') from None
        finally:
            if fd is not None:
                os.close(fd)

    def read(self, path, limit):
        with self.parent(path) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ObservationError('not_regular')
                raw = stream.read(limit + 1)
                if len(raw) > limit:
                    raise ObservationError('oversized')
                return raw

    def link(self, path):
        with self.parent(path) as (parent, name):
            return os.readlink(name, dir_fd=parent)

    def present(self, path):
        try:
            with self.parent(path) as (parent, name):
                try:
                    return stat.S_ISREG(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode)
                except FileNotFoundError:
                    return False
        except (OSError, ObservationError):
            return None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate')
        result[key] = value
    return result


def _json(raw):
    try:
        def invalid_constant(_):
            raise ValueError('nonfinite')
        value = json.loads(raw, object_pairs_hook=_unique, parse_constant=invalid_constant)
        if not isinstance(value, dict):
            raise ValueError('object_required')
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise ObservationError('json_invalid') from None


def _service():
    try:
        result = subprocess.run(SYSTEMCTL, shell=False, capture_output=True, text=True,
            timeout=5, check=False, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        if result.returncode != 0 or len(result.stdout) > 65536:
            raise ObservationError('service_unavailable')
        data = {}
        for line in result.stdout.splitlines():
            key, sep, value = line.partition('=')
            if not sep or key not in ('MainPID', 'ActiveState', 'FragmentPath', 'DropInPaths') or key in data:
                raise ObservationError('service_properties_invalid')
            data[key] = value
        if set(data) != {'MainPID', 'ActiveState', 'FragmentPath', 'DropInPaths'}:
            raise ObservationError('service_properties_invalid')
        if data['ActiveState'] != 'active' or not re.fullmatch(r'[1-9][0-9]{0,8}', data['MainPID']):
            raise ObservationError('service_not_active')
        return data
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise ObservationError('service_unavailable') from None


def _environment(raw):
    """Retain only exact allowed keys, without decoding unrelated environment."""
    selected = {}
    allowed = {key.encode(): key for key in KEYS}
    for entry in raw.split(b'\0'):
        key, sep, value = entry.partition(b'=')
        if sep and key in allowed:
            name = allowed[key]
            if name in selected:
                selected[name] = (None, 'duplicate_key')
                continue
            try:
                decoded = value.decode('utf-8')
                selected[name] = (decoded, None)
            except UnicodeError:
                selected[name] = (None, 'invalid_encoding')
    return selected


def _entry(env, key):
    return env.get(key, (None, 'missing'))


def _boolean(env, key, source):
    value, error = _entry(env, key)
    lower = key == 'THUNDER_EXTERNAL_EFFECTS_ENABLED'
    known = key in FLAG_ACTIVATION_SOURCES
    details = {'activation_rule': 'requires_lower_true' if lower else 'requires_true',
        'definition': ('Flag predicate only, not authorization; absent => false; '
                       + ("value.lower() == 'true' in worker/ConnectSafely; Gmail/assisted also require literal true."
                          if lower else "requires literal 'true'; other gates still apply.")),
        'activation_source': FLAG_ACTIVATION_SOURCES.get(key)} if known else {}
    normalized = value.lower() if lower and value is not None else value
    if error or normalized not in ('true', 'false'):
        return {'value': None, 'status': 'unknown', 'source': source,
                'reason': error or 'boolean_invalid',
                'effective_enabled': False if known and error == 'missing' else None, **details}
    return {'value': normalized == 'true', 'status': 'observed', 'source': source,
            'effective_enabled': normalized == 'true' if known else None, **details}


def _artifact_path(path):
    if not isinstance(path, str) or len(path) > 512 or not re.fullmatch(
            r'/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.json', path):
        return False
    parts = path.split('/')[1:]
    return not any(part in ('.', '..', 'proc', 'sys', 'dev', '.ssh')
                   or re.search(r'credential|secret|token|password', part, re.I) for part in parts)


def _artifact(files, env, path_key, hash_key, source, limit):
    result = {'status': 'unknown', 'present': None, 'source': source + ':' + path_key}
    path, error = _entry(env, path_key)
    if error or not path:
        return {**result, 'reason': error or 'path_missing'}, None
    if not _artifact_path(path):
        return {**result, 'reason': 'path_invalid'}, None
    # Presence is metadata only, even when no usable pin was configured.
    result['present'] = files.present(path)
    digest, error = _entry(env, hash_key)
    if error or not digest:
        return {**result, 'reason': 'pin_missing' if error == 'missing' or not digest else error}, None
    if not _DIGEST.fullmatch(digest):
        return {**result, 'reason': 'pin_invalid'}, None
    try:
        raw = files.read(path, limit)
        observed = hashlib.sha256(raw).hexdigest()
        if observed != digest:
            return {**result, 'reason': 'hash_mismatch'}, None
        data = _json(raw)
        return {**result, 'present': True, 'status': 'verified', 'sha256': observed}, data
    except ObservationError as error:
        return {**result, 'reason': str(error)}, None


def _config(files, env, source):
    result, data = _artifact(files, env, CONFIG_KEY, CONFIG_KEY + '_SHA256', source, 1048576)
    value = data.get('execution_enabled') if data is not None else None
    reason = result.get('reason')
    if data is not None and type(value) is not bool:
        reason = 'boolean_missing' if 'execution_enabled' not in data else 'boolean_invalid'
        result.update(status='unknown', reason=reason)
    result['execution_enabled'] = {'value': value if type(value) is bool else None,
        'status': 'observed' if type(value) is bool else 'unknown',
        'source': result['source'] + ':json.execution_enabled'}
    if reason:
        result['execution_enabled']['reason'] = reason
    return result


def _pgoptions(env, source):
    result = {'status': 'unknown', 'read_only': None, 'source': source + ':PGOPTIONS'}
    value, error = _entry(env, 'PGOPTIONS')
    if error:
        return {**result, 'reason': error}
    try:
        tokens = shlex.split(value)
        settings = {}
        while tokens:
            token = tokens.pop(0)
            if token == '-c':
                if not tokens:
                    raise ValueError('incomplete')
                setting = tokens.pop(0)
            elif token.startswith('-c'):
                setting = token[2:]
            elif token.startswith('--'):
                setting = token[2:]
            else:
                continue
            key, sep, raw = setting.partition('=')
            key = key.strip().replace('-', '_')
            if key not in ('default_transaction_read_only', 'transaction_read_only'):
                continue
            values = {'true': True, 'on': True, '1': True, 'false': False, 'off': False, '0': False}
            if not sep or raw.strip().lower() not in values:
                raise ValueError('invalid_boolean')
            settings[key] = values[raw.strip().lower()]
        if not settings:
            return {**result, 'reason': 'read_only_not_explicit'}
        if len(set(settings.values())) != 1:
            return {**result, 'reason': 'conflicting_read_only_settings'}
        return {**result, 'status': 'observed', 'read_only': next(iter(settings.values()))}
    except ValueError:
        return {**result, 'reason': 'options_invalid'}


def _dropins(files, properties):
    result = {'status': 'unknown', 'source': 'systemctl.show:DropInPaths'}
    try:
        paths = shlex.split(properties['DropInPaths'])
        if len(paths) > 100 or any(not re.fullmatch(
                r'/(?:etc|run|usr/lib)/systemd/system/painel-thunder\.service\.d/[A-Za-z0-9_.-]+\.conf', p)
                or Path(p).name.startswith('.') for p in paths):
            raise ValueError('invalid_paths')
        present = [files.present(path) for path in paths]
        return {**result, 'status': 'observed', 'loaded_count': len(paths),
            'present_count': sum(p is True for p in present),
            'unknown_count': sum(p is None for p in present),
            'writer': {'loaded': WRITER_DROPIN in paths, 'present': files.present(WRITER_DROPIN),
                       'source': WRITER_DROPIN}}
    except ValueError:
        return {**result, 'reason': 'dropin_paths_invalid'}


def _process(files, pid):
    root = '/proc/' + pid
    raw = files.read(root + '/stat', 16384).decode('ascii')
    try:
        # comm can contain spaces/parentheses; starttime is field 22.
        start = raw.rsplit(')', 1)[1].split()[19]
        if not start.isdecimal():
            raise ValueError('invalid_starttime')
    except (IndexError, ValueError):
        raise ObservationError('process_stat_invalid') from None
    return files.link(root + '/cwd'), start


def _current(files):
    target = files.link('/opt/thunder-os/current')
    return target if target.startswith('/') else os.path.normpath('/opt/thunder-os/' + target)


def observe(expected_sha, *, files=None, hostname=None, system=None, uid=None):
    """One bounded observation; no transition, enablement or provider health claim."""
    report = {'schema_version': 1, 'read_only': True, 'authorization_granted': False,
              'status': 'unknown', 'expected_host': HOST, 'service': UNIT}
    if not isinstance(expected_sha, str) or not _SHA.fullmatch(expected_sha):
        return {**report, 'reason': 'expected_sha_invalid'}
    report['expected_sha'] = expected_sha
    if ((socket.gethostname() if hostname is None else hostname) != HOST
            or (platform.system() if system is None else system) != 'Linux'
            or (os.geteuid() if uid is None else uid) != 0):
        return {**report, 'status': 'mismatch', 'reason': 'host_platform_or_user_mismatch'}
    files = files or LocalFiles()
    try:
        properties = _service()
        pid = properties['MainPID']
        process = _process(files, pid)
        release = '/opt/thunder-os/releases/' + expected_sha
        if process[0] != release or _current(files) != release:
            return {**report, 'status': 'mismatch', 'reason': 'process_release_mismatch'}
        manifest_raw = files.read(release + '/thunder-os-manifest.json', 1048576)
        manifest = _json(manifest_raw)
        if type(manifest.get('schema_version')) is not int or manifest.get('schema_version') != 1 or manifest.get('source_sha') != expected_sha:
            return {**report, 'status': 'mismatch', 'reason': 'manifest_source_mismatch'}
        envpath = '/proc/' + pid + '/environ'
        env = _environment(files.read(envpath, 1048576))
        resources = {'flags': {key: _boolean(env, key, envpath + ':' + key) for key in FLAGS},
            'connectsafely': _config(files, env, envpath),
            'policies': {name: _artifact(files, env, prefix + '_FILE', prefix + '_SHA256',
                envpath, 16384)[0] for name, prefix in POLICIES.items()},
            'pgoptions': _pgoptions(env, envpath), 'dropins': _dropins(files, properties)}
        # A reload/restart, PID reuse, cwd move or changed selected environment
        # invalidates the whole sample. Never combine two processes' resources.
        if (_service() != properties or _process(files, pid) != process
                or _environment(files.read(envpath, 1048576)) != env
                or _current(files) != release):
            return {**report, 'reason': 'process_changed'}
        return {**report, 'status': 'observed', 'pid': int(pid), 'release': release,
            'source_sha': manifest['source_sha'], 'manifest_sha256': hashlib.sha256(manifest_raw).hexdigest(),
            **resources}
    except ObservationError as error:
        return {**report, 'reason': str(error)}
    except (OSError, UnicodeError, ValueError):
        return {**report, 'reason': 'local_read_unavailable'}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default embeds invalid arguments, potentially secrets.
        raise ObservationError('arguments_invalid')


def main(argv=None):
    parser = _Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--expected-sha', required=True)
    try:
        args = parser.parse_args(argv)
        if not _SHA.fullmatch(args.expected_sha):
            raise ObservationError('expected_sha_invalid')
    except ObservationError as error:
        print(json.dumps({'status': 'unknown', 'read_only': True,
                          'authorization_granted': False, 'reason': str(error)}))
        return 2
    print(json.dumps(observe(args.expected_sha), sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
