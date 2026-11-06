"""Scoped invitation port for the Apollo/LinkedIn coordinator.

The caller owns campaign authority, durable reservations, account locking,
reply coverage, pacing and pause. A synchronous provider confirmation plus a
complete matching invitation listing is required to create a receipt.
"""
from __future__ import annotations

import math
import re
import time
from copy import deepcopy

from .connectsafely_client import ConnectSafelyClient


class InvitationAdapter:
    def __init__(self, client, *, clock=time.monotonic, page_budget=30, time_budget=20):
        if (not callable(clock) or type(page_budget) is not int or not 3 <= page_budget <= 30
                or type(time_budget) not in (int, float) or not 0 < time_budget <= 30):
            raise ValueError('invitation_budget_invalid')
        self.client = client
        self.clock, self.page_budget, self.time_budget = clock, page_budget, time_budget

    def relationship(self, plan):
        """Allow invitations only for an exact, not-yet-connected recipient."""
        try:
            self.client._plan(plan)
            if plan.get('action') != 'invite':
                return False
            self.client.verify_identity()
            value = self.client.relationship(plan)
            return (isinstance(value, dict) and value.get('account_id') == plan['account_id']
                    and value.get('recipient_public_id') == plan['recipient_public_id']
                    and value.get('recipient_profile_urn') == plan['recipient_profile_urn']
                    and value.get('relationship_status') == 'NOT_CONNECTED')
        except Exception:
            return False

    def _observation(self, plan):
        """Return relationship and exact matches, or fail closed on incomplete reads."""
        self.client._plan(plan)
        if plan.get('action') != 'invite':
            raise ValueError('unsupported_action')
        self.client.verify_identity()
        relation = self.client.relationship(plan)
        if (not isinstance(relation, dict)
                or relation.get('account_id') != plan['account_id']
                or relation.get('recipient_public_id') != plan['recipient_public_id']
                or relation.get('recipient_profile_urn') != plan['recipient_profile_urn']):
            raise ValueError('relationship_unverified')
        started, start, matches, seen = self.clock(), 0, [], set()
        for _ in range(self.page_budget // 3):
            if self.clock() - started > self.time_budget:
                raise ValueError('invitation_history_budget_exceeded')
            page = self.client.sent_invitations(max_pages=3, start_index=start)
            if self.clock() - started > self.time_budget:
                raise ValueError('invitation_history_budget_exceeded')
            if (not isinstance(page, dict) or page.get('account_id') != plan['account_id']
                    or type(page.get('complete')) is not bool or not isinstance(page.get('invitations'), list)):
                raise ValueError('invitation_history_incomplete')
            for row in page['invitations']:
                if not isinstance(row, dict):
                    raise ValueError('invitation_history_unverified')
                identity = row.get('invitationId') or row.get('invitationUrn')
                if not isinstance(identity, str) or not identity or identity in seen:
                    raise ValueError('invitation_history_unverified')
                seen.add(identity)
                if self._is_target(row, plan):
                    matches.append(row)
            if page['complete'] is True:
                return relation, matches
            following = page.get('next_start_index')
            if type(following) is not int or following <= start:
                raise ValueError('invitation_history_incomplete')
            start = following
        raise ValueError('invitation_history_budget_exceeded')

    @staticmethod
    def _is_target(row, plan):
        if not isinstance(row, dict) or not isinstance(row.get('profile'), dict):
            raise ValueError('invitation_history_unverified')
        profile = row['profile']
        slug = profile.get('publicIdentifier')
        member = row.get('memberUrn')
        if not isinstance(slug, str) or not slug:
            raise ValueError('invitation_identity_unverified')
        if slug.lower() == plan['recipient_public_id'].lower() or member == plan['recipient_profile_urn']:
            if (slug.lower() != plan['recipient_public_id'].lower()
                    or member not in (None, plan['recipient_profile_urn'])):
                raise ValueError('invitation_identity_conflict')
            return True
        return False

    def send(self, plan, reserved_at, *, before_dispatch):
        """Dispatch at most once; never infer success from a list after timeout."""
        if (type(reserved_at) not in (int, float) or not math.isfinite(reserved_at)
                or reserved_at <= 0 or not callable(before_dispatch)):
            return {'status': 'blocked', 'reason': 'reservation_unverified'}
        if not isinstance(plan, dict):
            return {'status': 'blocked', 'reason': 'plan_unverified'}
        plan = deepcopy(plan)
        try:
            relation, matches = self._observation(plan)
        except Exception:
            return {'status': 'blocked', 'reason': 'invitation_history_unverified'}
        # Only the concrete client implements the validated synchronous POST
        # signal. A caller-supplied observation or generic port cannot grant it.
        if type(self.client) is not ConnectSafelyClient:
            return {'status': 'blocked', 'reason': 'provider_freshness_unverified'}
        if relation.get('relationship_status') != 'NOT_CONNECTED':
            return {'status': 'blocked', 'reason': 'relationship_not_eligible'}
        if matches:
            return {'status': 'blocked', 'reason': 'prior_invitation'}
        entered = False
        reason = 'dispatch_not_authorized'

        def authority():
            nonlocal entered, reason
            if before_dispatch() is not True:
                return False
            try:
                current, prior = self._observation(plan)
                if current.get('relationship_status') != 'NOT_CONNECTED':
                    reason = 'relationship_not_eligible'
                    return False
                if prior:
                    reason = 'prior_invitation'
                    return False
            except Exception:
                reason = 'invitation_history_unverified'
                return False
            if before_dispatch() is not True:
                return False
            entered = True
            return True

        try:
            result = self.client.send(plan, before_dispatch=authority)
        except Exception:
            return ({'status': 'uncertain', 'reason': 'send_outcome_unknown'} if entered
                    else {'status': 'blocked', 'reason': reason})
        if not entered:
            return {'status': 'blocked', 'reason': reason}
        # A 200 alone, a timeout or a later matching cached list cannot prove
        # this attempt. The client's marker requires success and exact URN.
        if not isinstance(result, dict) or result.get('provider_send_confirmed') is not True:
            return {'status': 'uncertain', 'reason': 'send_outcome_unknown'}
        try:
            _, matches = self._observation(plan)
            if len(matches) != 1:
                raise ValueError('invitation_unverified')
            row = matches[0]
            ident = row.get('invitationId')
            urn = row.get('invitationUrn')
            if (not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', ident)
                    or row.get('id') not in (None, ident)
                    or urn != 'urn:li:invitation:' + ident
                    or row.get('memberUrn') != plan['recipient_profile_urn']):
                raise ValueError('invitation_unverified')
            return {'status': 'sent', 'receipt_ref': 'connectsafely:invitation-id:' + ident,
                    'provider_id': ident, 'invitation_urn': urn,
                    'confirmation_kind': 'provider_success_and_directory', 'accepted': False}
        except Exception:
            return {'status': 'uncertain', 'reason': 'invitation_receipt_unverified'}
