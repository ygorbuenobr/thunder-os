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


# Legacy root-only paths referenced by pinned runtime configs (read on the VPS,
# 05/11/2026) and the systemd credential names that replace them.
LEGACY_CREDENTIAL_NAMES = {
    '/etc/thunder-os/connectsafely.token': 'connectsafely_token',
    '/etc/thunder-os/social-db.dsn': 'social_db_dsn',
    '/etc/thunder-os/instantly_api_key': 'instantly_api_key',
    '/root/.config/thunder/credentials/apollo_api_key.txt': 'apollo_api_key',
    '/var/lib/thunder-os-integrations/credentials/hubspot_access': 'hubspot_access',
}


def resolve_legacy(path):
    """Map a known legacy credential path to its systemd credential, if loaded."""
    if isinstance(path, str) and path in LEGACY_CREDENTIAL_NAMES:
        return credential_path(LEGACY_CREDENTIAL_NAMES[path], path)
    return path
