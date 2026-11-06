"""Create an immutable identity observation from the two configured readers.

This is an internal producer, not an HTTP authority or an action approval.
The caller supplies no contact, account, slug, URN or provider response. A
consumer must additionally verify a confirmed reservation and source event.
"""
from __future__ import annotations

import http.client
import re

from .apollo_linkedin_authority import CampaignApprovalAuthority
from .apollo_linkedin_attention import EXCLUDED_TASK
from .apollo_linkedin_preview import _slug
from .apollo_linkedin_reader import ApolloLinkedInReader, _https_request
from .connectsafely_client import ConnectSafelyClient
from .connectsafely_read_targets import ReadTargetResolver, TargetReadHTTPS
from .models import payload_hash

KIND = 'apollo_linkedin_identity_binding'
_URN = re.compile(r'urn:li:fsd_profile:[A-Za-z0-9_-]{1,200}\Z')
_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')


def binding_key(company, campaign_id, task_id, account_id, approval_id):
    return payload_hash({'company': company, 'campaign_id': campaign_id,
                         'task_id': task_id, 'account_id': account_id,
                         'approval_id': approval_id})


def _task_identity(task, task_id, binding):
    contact = task.get('contact') if isinstance(task, dict) else None
    if (not isinstance(task, dict) or task.get('id') != task_id
            or task.get('type') not in ('linkedin_step_message', 'linkedin_step_connect')
            or task.get('status') != 'scheduled'
            or task.get('emailer_campaign_id') != binding['sequence_id']
            or task.get('user_id') not in binding['owner_ids']
            or not isinstance(contact, dict) or not isinstance(contact.get('id'), str)
            or not _ID.fullmatch(contact['id']) or task.get('contact_id') != contact['id']):
        raise ValueError('task_identity_unverified')
    recipient = _slug(contact.get('linkedin_url'))
    if not recipient or recipient == binding['public_id'].lower():
        raise ValueError('recipient_unverified')
    return contact['id'], recipient, task['user_id'], task['type']


def create_identity_binding(repo, *, reader, resolver, campaign_id, approval_id,
                            campaign_version, task_id):
    """Read and persist one immutable identity; never dispatch provider actions.

    The concrete, allowlisted transport classes are required; test doubles may
    replace HTTPS connections but do not replace the identity authorities.
    """
    if (not isinstance(task_id, str) or not _ID.fullmatch(task_id) or task_id == EXCLUDED_TASK
            or type(reader) is not ApolloLinkedInReader or reader._request is not _https_request
            or type(resolver) is not ReadTargetResolver or resolver.repo is not repo
            or type(resolver.client) is not ConnectSafelyClient
            or type(resolver.client.request) is not TargetReadHTTPS
            or resolver.client.request.factory is not http.client.HTTPSConnection):
        raise ValueError('identity_authority_unverified')
    repo.apollo_linkedin_store_ready()
    authority = CampaignApprovalAuthority(repo)
    approved = authority.require(campaign_id, approval_id, campaign_version)
    binding = approved['binding']
    client = resolver.client
    if (reader.team_id != binding['team_id'] or reader.owner_ids != frozenset(binding['owner_ids'])
            or client.account_id != binding['account_id'] or client.expected_public_id != binding['public_id']):
        raise ValueError('identity_authority_mismatch')
    observed_task = _task_identity(reader.get(task_id), task_id, binding)
    contact_id, recipient = observed_task[:2]
    observation = resolver({'action': 'dm', 'profile_url': 'https://www.linkedin.com/in/' + recipient + '/'})
    target = observation.get('target') if isinstance(observation, dict) else None
    preview = observation.get('preview') if isinstance(observation, dict) else None
    if (not isinstance(observation, dict) or observation.get('resolved') is not True
            or observation.get('read_only') is not True or observation.get('source') != 'provider_observation'
            or not isinstance(preview, dict) or preview.get('cached') is not False
            or not isinstance(target, dict) or target.get('account_id') != binding['account_id']
            or target.get('account_public_id') != binding['public_id']
            or target.get('recipient_public_id') != recipient
            or not isinstance(target.get('recipient_profile_urn'), str)
            or not _URN.fullmatch(target['recipient_profile_urn'])):
        raise ValueError('profile_identity_unverified')
    if _task_identity(reader.get(task_id), task_id, binding) != observed_task:
        raise ValueError('task_identity_changed')
    value = {'company': binding['company_id'], 'campaign_id': campaign_id,
             'approval_id': approval_id, 'campaign_version': campaign_version,
             'scope_hash': approved['scope_hash'], 'team_id': binding['team_id'],
             'sequence_id': binding['sequence_id'], 'task_id': task_id,
             'action_type': observed_task[3],
             'contact_id': contact_id, 'account_id': binding['account_id'],
             'recipient_public_id': recipient, 'member_urn': target['recipient_profile_urn']}
    key = binding_key(value['company'], campaign_id, task_id, value['account_id'], approval_id)
    with repo.transaction():
        if authority.require(campaign_id, approval_id, campaign_version) != approved:
            raise ValueError('campaign_approval_changed')
        existing = repo.get_state(KIND, key)
        if existing is not None:
            data = {k: v for k, v in existing.items() if k not in ('id', 'version', 'updated_at')}
            if existing.get('version') != 1 or data != value:
                raise ValueError('identity_binding_conflict')
            return existing
        return repo.append_state(KIND, key, value)
