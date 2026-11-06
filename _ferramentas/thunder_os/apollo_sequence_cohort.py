"""Bounded read-only discovery of Apollo contacts in an approved sequence.

The result is an observation, not a reply, enrollment authorization, or a
dispatch queue. Missing membership or incomplete pagination never means an
empty cohort. This module performs no persistence or provider mutation.
"""
from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timezone

from .apollo_readonly import _https_request
from .approvals import execution_request
from .models import payload_hash

_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')


class ApolloSequenceCohort:
    def __init__(self, repository, credential_loader, *, api_actor_id, request=None,
                 timeout=8, clock=time.monotonic):
        if (not callable(credential_loader) or not isinstance(api_actor_id, str)
                or not _ID.fullmatch(api_actor_id) or not callable(clock)
                or type(timeout) not in (int, float) or not math.isfinite(timeout)
                or not 0 < timeout <= 15 or request is not None and not callable(request)):
            raise ValueError('cohort_configuration_invalid')
        self.repository = repository
        self.loader, self.actor = credential_loader, api_actor_id
        self.request, self.timeout, self.clock = request or _https_request, timeout, clock

    def _approved_binding(self, campaign_id, approval_id, campaign_version):
        """Read the historical approved snapshot even when sending is paused.

        This is read authority only. Expiry or revocation must not hide a reply
        from an already-enrolled contact and never authorizes any dispatch.
        """
        with self.repository.transaction():
            approval = self.repository.get_state('approval', approval_id)
            if (not isinstance(approval, dict) or approval.get('actor') != 'ygor'
                    or approval.get('scope') != 'campaign_version'
                    or approval.get('campaign_id') != campaign_id
                    or approval.get('campaign_version') != campaign_version
                    or type(approval.get('version')) is not int or approval['version'] < 1):
                raise ValueError('cohort_approval_unverified')
            digest = approval.get('scope_hash')
            if (not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest)
                    or approval.get('snapshot_id') != 'apollo-campaign-' + digest):
                raise ValueError('cohort_approval_unverified')
            snapshot = self.repository.get_state('campaign_version_snapshot', approval['snapshot_id'])
            scope = snapshot.get('scope') if isinstance(snapshot, dict) else None
            if (not isinstance(snapshot, dict) or snapshot.get('version') != 1
                    or snapshot.get('scope_hash') != digest or not isinstance(scope, dict)
                    or set(scope) != {'context', 'configuration', 'campaign_config'}
                    or payload_hash(scope) != digest or not isinstance(scope['context'], dict)
                    or scope['context'].get('platform') != 'apollo'
                    or scope['context'].get('campaign_id') != campaign_id
                    or scope['context'].get('campaign_version') != campaign_version
                    or not isinstance(scope['configuration'], dict)
                    or not isinstance(scope['configuration'].get('apollo_linkedin'), dict)):
                raise ValueError('cohort_snapshot_unverified')
            confirmation = approval.get('execution_confirmation')
            if (not isinstance(confirmation, dict) or approval.get('approved_at') != confirmation.get('confirmed_at')
                    or approval.get('expires_at') != confirmation.get('expires_at')
                    or confirmation.get('actor') != 'ygor' or not isinstance(confirmation.get('request'), dict)):
                raise ValueError('cohort_confirmation_unverified')
            request = confirmation['request']
            expected = execution_request(scope['context'], campaign_id, approval['campaign_revision'],
                {'campaign_version': digest}, request.get('control_version'))
            if request != expected:
                raise ValueError('cohort_confirmation_unverified')
            return {'binding': dict(scope['configuration']['apollo_linkedin']), 'scope_hash': digest}

    def _read(self, method, path, body, token, timeout):
        if not ((method, path, body) == ('GET', '/api/v1/users/api_profile', None)
                or (method == 'POST' and path == '/api/v1/contacts/search'
                    and isinstance(body, dict) and set(body) == {'page', 'per_page', 'sort_by_field', 'sort_ascending'})):
            raise ValueError('cohort_endpoint_blocked')
        status, raw = self.request(method, path, body,
            {'x-api-key': token, 'Accept': 'application/json', 'Content-Type': 'application/json'}, timeout)
        if status != 200 or not isinstance(raw, bytes) or len(raw) > 1_000_000:
            raise ValueError('cohort_provider_unavailable')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('cohort_response_invalid')
        return value

    def scan(self, campaign_id, approval_id, campaign_version, *, max_pages=3,
             start_page=1, per_page=100, budget_seconds=10):
        if (type(max_pages) is not int or not 1 <= max_pages <= 500
                or type(start_page) is not int or not 1 <= start_page <= 500
                or type(per_page) is not int or not 1 <= per_page <= 100
                or type(budget_seconds) not in (int, float) or not math.isfinite(budget_seconds)
                or not 0 < budget_seconds <= 30):
            raise ValueError('cohort_budget_invalid')
        result = {'coverage': 'unknown', 'reason': 'cohort_unavailable', 'members': [],
                  'pages': 0, 'observed_at': datetime.now(timezone.utc).isoformat(),
                  'next_page': None, 'source_total': None, 'start_page': start_page,
                  'consistency': 'provider_snapshot_unverified', 'history_complete': False,
                  'all_pages_observed': False}
        deadline = self.clock() + budget_seconds
        try:
            approved = self._approved_binding(campaign_id, approval_id, campaign_version)
            binding = approved['binding']
            team = binding['team_id']
            sequences = binding.get('sequence_ids')
            if sequences is None:
                sequences = [binding.get('sequence_id')]
            if (not isinstance(sequences, list) or not sequences
                    or any(not isinstance(x, str) or not _ID.fullmatch(x) for x in sequences)
                    or len(set(sequences)) != len(sequences)
                    or ('sequence_ids' in binding and 'sequence_id' in binding)
                    or not isinstance(team, str) or not _ID.fullmatch(team)):
                raise ValueError('cohort_scope_invalid')
            token = self.loader()
            if not isinstance(token, str) or not token or any(c.isspace() for c in token):
                raise ValueError('cohort_credential_unavailable')
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise TimeoutError('cohort_time_budget')
            profile = self._read('GET', '/api/v1/users/api_profile', None, token, min(self.timeout, remaining))
            profile = profile.get('user', profile)
            if not isinstance(profile, dict) or profile.get('id') != self.actor or profile.get('team_id') != team:
                raise ValueError('cohort_team_mismatch')
            seen, total = set(), None
            for page in range(start_page, min(start_page + max_pages, 501)):
                remaining = deadline - self.clock()
                if remaining <= 0:
                    result.update(coverage='partial', reason='cohort_time_budget', next_page=page)
                    return result
                if self._approved_binding(campaign_id, approval_id, campaign_version) != approved:
                    raise ValueError('cohort_approval_changed')
                data = self._read('POST', '/api/v1/contacts/search',
                    {'page': page, 'per_page': per_page, 'sort_by_field': 'contact_created_at',
                     'sort_ascending': True}, token, min(self.timeout, remaining))
                rows, pagination = data.get('contacts'), data.get('pagination')
                if (not isinstance(rows, list) or len(rows) > per_page or not isinstance(pagination, dict)
                        or pagination.get('page') != page or pagination.get('per_page') != per_page
                        or type(pagination.get('total_entries')) is not int
                        or not 0 <= pagination['total_entries'] <= 50000
                        or total is not None and pagination['total_entries'] != total):
                    raise ValueError('cohort_pagination_invalid')
                total = pagination['total_entries']
                for row in rows:
                    if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                            or not _ID.fullmatch(row['id']) or row['id'] in seen
                            or 'team_id' in row and row['team_id'] != team):
                        raise ValueError('cohort_identity_invalid')
                    seen.add(row['id'])
                    memberships = row.get('contact_campaign_statuses')
                    if not isinstance(memberships, list) or any(not isinstance(m, dict)
                            or not isinstance(m.get('emailer_campaign_id'), str) for m in memberships):
                        raise ValueError('cohort_membership_unknown')
                    matches = [m for m in memberships if m['emailer_campaign_id'] in sequences]
                    if len({m['emailer_campaign_id'] for m in matches}) != len(matches):
                        raise ValueError('cohort_membership_ambiguous')
                    for membership in matches:
                        state = membership.get('status')
                        if state not in ('active', 'paused', 'failed', 'finished'):
                            raise ValueError('cohort_membership_unknown')
                        result['members'].append({'contact_id': row['id'], 'sequence_id': membership['emailer_campaign_id'],
                                                  'membership_status': state})
                result.update(pages=page, source_total=total)
                if self.clock() > deadline:
                    result.update(coverage='partial', reason='cohort_time_budget', next_page=page + 1)
                    return result
                observed_through = (page - 1) * per_page + len(rows)
                if observed_through == total and (start_page != 1 or len(seen) == total):
                    if self._approved_binding(campaign_id, approval_id, campaign_version) != approved:
                        raise ValueError('cohort_approval_changed')
                    # A resumed page is an observation, not proof that earlier
                    # pages were preserved or that Apollo provides a snapshot.
                    complete = start_page == 1 and page == 1
                    result.update(coverage='complete' if complete else 'partial',
                        all_pages_observed=start_page == 1,
                        reason=None if complete else ('cohort_prefix_unverified' if start_page > 1
                                                      else 'cohort_snapshot_unverified'))
                    return result
                if len(rows) != per_page or observed_through > total:
                    raise ValueError('cohort_pagination_invalid')
            following = start_page + max_pages
            result.update(coverage='partial', reason='cohort_page_budget',
                          next_page=following if following <= 500 else None)
        except Exception:
            # Never let an incomplete source erase observations already read.
            result.update(coverage='partial' if result['members'] or result['pages'] else 'unknown',
                          reason='cohort_unavailable')
        return result
