"""Fail closed on drift of the selected read-only Hermes dependency/runtime.

This pins Hermes source and interpreter bootstrap metadata, not every installed
third-party package. The release and dependency directories must be immutable.
No authentication material is inspected.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

LOCK_PATH = Path('/etc/thunder-os/hermes-ro-runtime-lock-v2.json')
PRODUCTION_LOCK_PATH = LOCK_PATH


def _identity(record):
    path = Path(record['path'])
    if not path.is_file() or path.is_symlink():
        raise ValueError()
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
            size += len(block)
    if size != record['size'] or digest.hexdigest() != record['sha256']:
        raise ValueError()


def verify_dependency(root, python=None):
    """Validate release lock before importing Hermes; errors contain no paths/data."""
    try:
        lock_info = LOCK_PATH.lstat()
        required_owner = 0 if LOCK_PATH == PRODUCTION_LOCK_PATH else os.geteuid()
        if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_mode & 0o077
                or lock_info.st_uid != required_owner):
            raise ValueError()
        if LOCK_PATH == PRODUCTION_LOCK_PATH:
            parent_info = LOCK_PATH.parent.lstat()
            if (not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != 0
                    or parent_info.st_mode & 0o022):
                raise ValueError()
        lock = json.loads(LOCK_PATH.read_text())
        selected = Path(root)
        if (lock['schema_version'] != 1 or not selected.is_absolute()
                or str(selected) != lock['dependency_path']
                or selected.resolve(strict=True) != selected):
            raise ValueError()
        executable = Path(sys.executable if python is None else python)
        if (str(executable) != lock['interpreter_path']
                or str(executable.resolve(strict=True)) != lock['interpreter_realpath']
                or lock['interpreter_binary']['path'] != lock['interpreter_realpath']
                or lock['pyvenv_config']['path'] != str(executable.parent.parent / 'pyvenv.cfg')):
            raise ValueError()
        for record in (lock['interpreter_binary'], lock['pyvenv_config'], *lock['startup_files']):
            _identity(record)
        # Reject additions to startup hooks, including hooks not known by the lock.
        site = executable.parent.parent / 'lib' / ('python' + '.'.join(map(str, sys.version_info[:2]))) / 'site-packages'
        expected_hooks = {item['path'] for item in lock['startup_files']}
        actual_hooks = {str(p) for pattern in ('*.pth', '__editable__*.py', 'sitecustomize.py', 'usercustomize.py') for p in site.glob(pattern)}
        if actual_hooks != expected_hooks:
            raise ValueError()
        manifest_record = lock['dependency_manifest']
        if manifest_record['path'] != str(selected / 'thunder-hermes-ro-manifest.json'):
            raise ValueError()
        _identity(manifest_record)
        manifest = json.loads(Path(manifest_record['path']).read_text())
        if manifest['upstream_sha'] != lock['upstream_sha']:
            raise ValueError()
        expected = {manifest_record['path']}
        for item in manifest['files']:
            path = selected / item['path']
            if path.resolve(strict=True) != path or not path.is_relative_to(selected):
                raise ValueError()
            if str(path) in expected:
                raise ValueError()
            expected.add(str(path))
            _identity({**item, 'path': str(path)})
        actual = set()
        for path in selected.rglob('*'):
            if path.is_symlink():
                raise ValueError()
            if path.is_file():
                actual.add(str(path))
        if actual != expected:
            raise ValueError()
    except Exception:
        raise RuntimeError('hermes_dependency_identity_mismatch') from None
