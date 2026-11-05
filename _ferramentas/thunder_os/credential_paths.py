"""Resolve named systemd credentials without weakening file validation."""
import os
from pathlib import Path
import re


def credential_path(name, legacy_path):
    if (not isinstance(name, str) or name in ('.', '..')
            or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', name)):
        raise ValueError('credential_name_invalid')
    directory = os.environ.get('CREDENTIALS_DIRECTORY')
    if directory:
        root = Path(directory)
        if not root.is_absolute():
            raise ValueError('credential_directory_invalid')
        candidate = root / name
        # A malformed present entry, including a dangling symlink, must be
        # checked by the consumer; never fall back around it.
        if os.path.lexists(candidate):
            return str(candidate)
    return str(legacy_path)
