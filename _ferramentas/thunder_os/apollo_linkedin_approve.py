"""Root-only local issuer of an approved Apollo/LinkedIn campaign version.

The JSON is the complete CampaignRegistry configuration, not a provider
credential. This command never contacts a provider or dispatches a message.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys

from .apollo_linkedin_roles import load_roles
from .db import Repository, _official_apollo_linkedin_db
from .service import ThunderService

_ID = re.compile(r'[A-Za-z0-9_-]{1,100}\Z')


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate_json_key')
        value[key] = item
    return value


def read_config(path):
    """Read a bounded regular file without following its final symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 262144:
            raise ValueError('configuration_unavailable')
        raw = stream.read(262145)
    if len(raw) > 262144:
        raise ValueError('configuration_unavailable')
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid_json')))


def approve_campaign(repository, campaign, config, actor):
    """Atomically register a complete config and issue its existing approval pack."""
    if actor != 'ygor' or not isinstance(campaign, str) or not _ID.fullmatch(campaign):
        raise ValueError('authorization_invalid')
    if not isinstance(config, dict) or config.get('campaign_id') != campaign:
        raise ValueError('campaign_mismatch')
    service = ThunderService(repository)
    repository.apollo_linkedin_store_ready()
    with repository.transaction():
        current = repository.get_state('campaign_config', campaign)
        expected = current['version'] if current is not None else 0
        # A repeat with identical configuration must not invalidate existing
        # approvals by creating a spurious new campaign revision.
        if current is None or not current.get('active') or current.get('config') != config:
            service.configure_campaign(config, actor, expected_version=expected)
        else:
            service.campaign_registry._read(campaign)
        review = service.apollo_campaign_approval_review(campaign)
        approved = service.approve_apollo_campaign_version(
            campaign, actor, execution_confirmation=review['execution_confirmation'])
        return {'status': 'approved', 'campaign_id': campaign,
                'campaign_version': approved['campaign_version'], 'approval_id': approved['id'],
                'scope_hash': approved['scope_hash'], 'expires_at': approved['expires_at']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--campaign', required=True)
    parser.add_argument('--actor', required=True, choices=['ygor'])
    parser.add_argument('--config', required=True, help='JSON da configuração completa da campanha')
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise ValueError('root_required')
        roles = load_roles(os.environ.get('THUNDER_APOLLO_LI_ROLES_FILE', '/etc/thunder-os/roles.json'))
        if roles.get(args.actor) != 'admin':
            raise ValueError('admin_required')
        path = os.environ.get('PAINEL_DB', '')
        if (not Path(path).is_absolute() or not Path(path).is_file()
                or Path(path).resolve() != _official_apollo_linkedin_db().resolve()):
            raise ValueError('official_store_unavailable')
        config = read_config(args.config)
        result = approve_campaign(Repository(path), args.campaign, config, args.actor)
    except Exception as exc:
        # Avoid echoing paths, input content, or internal/provider details.
        reason = str(exc) if str(exc) in {'root_required', 'admin_required', 'roles_unavailable',
                  'official_store_unavailable', 'campaign_mismatch', 'authorization_invalid'} else 'approval_unavailable'
        print(json.dumps({'status': 'blocked', 'reason': reason}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
