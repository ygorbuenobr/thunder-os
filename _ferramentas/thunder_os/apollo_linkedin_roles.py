"""Root-managed actor roles for Apollo/LinkedIn actions; no identity discovery."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat

_ACTOR = re.compile(r'[A-Za-z0-9_-]{1,100}\Z')
_ALLOWED = frozenset({'admin', 'manager', 'operator'})


class RolesUnavailable(ValueError):
    pass


def _root_controlled_parents(target):
    for parent in (target.parent, *target.parent.parents):
        info = parent.stat(follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            return False
    return True


def load_roles(path):
    """Read a root-owned regular file and reject writable/untrusted paths."""
    try:
        target = Path(path)
        if not target.is_absolute() or target.resolve(strict=True) != target:
            raise ValueError
        # Without root-controlled parent directories, a non-root owner can
        # replace an otherwise root-owned file after verification.
        if not _root_controlled_parents(target):
            raise ValueError
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                    or info.st_mode & 0o022 or not 0 < info.st_size <= 65536):
                raise ValueError
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if (not isinstance(value, dict) or not value or len(value) > 1000
                or any(not isinstance(k, str) or not _ACTOR.fullmatch(k)
                       or not isinstance(v, str) or v not in _ALLOWED for k, v in value.items())):
            raise ValueError
        return value
    except (ValueError, TypeError, OSError, UnicodeError, RecursionError):
        raise RolesUnavailable('roles_unavailable') from None


def authorize_manager(actor, path):
    roles = load_roles(path)
    return isinstance(actor, str) and roles.get(actor) in {'admin', 'manager'}
