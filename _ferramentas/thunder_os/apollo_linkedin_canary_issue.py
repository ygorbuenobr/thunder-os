"""Issue one scoped Apollo/LinkedIn canary; this command never sends."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace

from .apollo_linkedin_attention import EXCLUDED_TASK
from .apollo_linkedin_authority import CampaignApprovalAuthority
from .apollo_linkedin_reader import ApolloLinkedInReader
from .apollo_linkedin_roles import load_roles
from .approvals import ApprovalError
from .connectsafely_live import credential
from .connectsafely_read_targets import build_read_resolver
from .credential_paths import credential_path
from .db import Repository, _official_apollo_linkedin_db

_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
_APOLLO_KEY = '/root/.config/thunder/credentials/apollo_api_key.txt'


def authorize_actor(actor, roles_file):
    if os.geteuid() != 0 or actor != 'ygor' or load_roles(roles_file).get(actor) != 'admin':
        raise ValueError('root_admin_required')


def issue(repo, *, task_id, actor, company, team, api_actor, loader, resolver, roles_file):
    """Discover the single approved binding, then reuse the panel's issuance path."""
    authorize_actor(actor, roles_file)
    if (not isinstance(task_id, str) or not _ID.fullmatch(task_id)
            or task_id == EXCLUDED_TASK or not all(isinstance(v, str) and v for v in
                                                   (company, team, api_actor))):
        raise ValueError('task_or_configuration_invalid')
    repo.apollo_linkedin_store_ready()
    authority = CampaignApprovalAuthority(repo)
    candidates = []
    for approval in repo.list_state('approval'):
        if (approval.get('scope') != 'campaign_version' or approval.get('status') != 'active'
                or not isinstance(approval.get('id'), str)
                or not isinstance(approval.get('campaign_id'), str)):
            continue
        try:
            approved = authority.require(approval['campaign_id'], approval['id'], approval['campaign_version'])
            binding = approved['binding']
            if binding['company_id'] == company and binding['team_id'] == team:
                candidates.append((approval, binding))
        except (ApprovalError, KeyError, TypeError):
            continue
    if not candidates or len(candidates) > 100:
        raise ValueError('approved_campaign_unavailable')
    owners = sorted({owner for _, binding in candidates for owner in binding['owner_ids']})
    reader = ApolloLinkedInReader(loader, team_id=team, api_actor_id=api_actor, owner_ids=owners)
    task = reader.get(task_id)
    matches = [(approval, binding) for approval, binding in candidates
               if task.get('emailer_campaign_id') == binding['sequence_id']
               and task.get('user_id') in binding['owner_ids']]
    # Multiple active approvals of the same exact campaign version and hash
    # are equivalent. Different campaigns or revisions remain ambiguous.
    scopes = {(approval['campaign_id'], approval.get('campaign_version'), approval.get('scope_hash'))
              for approval, _ in matches}
    if not matches or len(scopes) != 1:
        raise ValueError('approved_campaign_ambiguous_or_missing')
    approval, binding = max(matches, key=lambda item: (item[0].get('approved_at', ''), item[0]['id']))
    scoped_reader = ApolloLinkedInReader(loader, team_id=team, api_actor_id=api_actor,
                                         owner_ids=binding['owner_ids'])
    if not callable(resolver):
        raise ValueError('target_reader_unavailable')
    attention = SimpleNamespace(reader=scoped_reader, team_id=team, company_id=company)
    server = SimpleNamespace(thunder_apollo_linkedin_attention=attention,
                             thunder_connectsafely_read_resolver=resolver)
    from painel_servidor import issue_apollo_linkedin_panel_canary
    return issue_apollo_linkedin_panel_canary(server, repo, {
        'company': company, 'task_id': task_id, 'campaign_id': approval['campaign_id'],
        'approval_id': approval['id'], 'campaign_version': approval['campaign_version'],
    }, actor)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', required=True)
    parser.add_argument('--actor', required=True)
    args = parser.parse_args(argv)
    try:
        roles_file = os.environ.get('THUNDER_APOLLO_LI_ROLES_FILE', '/etc/thunder-os/roles.json')
        authorize_actor(args.actor, roles_file)
        db = Path(os.environ.get('PAINEL_DB', ''))
        official = _official_apollo_linkedin_db()
        if (not db.is_absolute() or not db.is_file() or not official.is_file()
                or db.resolve(strict=True) != official.resolve(strict=True)):
            raise ValueError('official_database_unavailable')
        repo = Repository(db)
        repo.apollo_linkedin_store_ready()
        loader = credential(credential_path('apollo_api_key', _APOLLO_KEY))
        result = issue(repo, task_id=args.task, actor=args.actor,
                       company=os.environ.get('THUNDER_APOLLO_LI_COMPANY'),
                       team=os.environ.get('THUNDER_APOLLO_LI_TEAM'),
                       api_actor=os.environ.get('THUNDER_APOLLO_LI_API_ACTOR'),
                       loader=loader, resolver=build_read_resolver(repo), roles_file=roles_file)
        print(json.dumps({'status': 'issued', 'authorization_id': result['id'],
                          'expires_at': result['expires_at']}, ensure_ascii=False))
        return 0
    except Exception:
        # Do not reveal secrets or provider payloads through an operator CLI.
        print(json.dumps({'status': 'blocked', 'reason': 'canary_issuance_unavailable'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
