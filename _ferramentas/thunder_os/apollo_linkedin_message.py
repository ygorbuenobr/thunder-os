"""Scoped ConnectSafely message port for the Apollo canary.

The caller owns authorization, durable reservation, locking, pause and rate
limits. This port never retries a write: an ambiguous result is reconciled by
readback and otherwise remains uncertain.
"""
from __future__ import annotations

import math
import re
from urllib.parse import quote

from .connectsafely_client import ConnectSafelyError


def _time(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


class MessageAdapter:
    def __init__(self, client):
        self.client = client

    def relationship(self, plan):
        try:
            self.client._plan(plan)
            if plan['action'] != 'dm':
                return False
            evidence = self.client.relationship(plan)
            return (evidence.get('relationship_status') == 'CONNECTED'
                    and evidence.get('account_id') == plan['account_id']
                    and evidence.get('recipient_public_id') == plan['recipient_public_id']
                    and evidence.get('recipient_profile_urn') == plan['recipient_profile_urn'])
        except Exception:
            return False

    def send(self, plan, reserved_at, *, before_dispatch):
        """Send once and reconcile ambiguity; requires an already durable reservation."""
        if not _time(reserved_at) or not callable(before_dispatch):
            return {'status': 'blocked', 'reason': 'reservation_unverified'}
        if not self.relationship(plan):
            return {'status': 'blocked', 'reason': 'not_connected_or_unknown'}
        entered = False

        def authority():
            nonlocal entered
            allowed = before_dispatch() is True
            entered = allowed
            return allowed

        try:
            result = self.client.send(plan, before_dispatch=authority)
        except Exception:
            if not entered:
                return {'status': 'blocked', 'reason': 'dispatch_not_authorized'}
            result = {'status': 'uncertain'}
        if not entered:
            return {'status': 'blocked', 'reason': 'dispatch_not_authorized'}
        if isinstance(result, dict) and result.get('status') == 'sent' and result.get('receipt_ref'):
            return result
        try:
            # Reconciliation must prove the message was *after* reservation.
            result = self.client.reconcile(plan, math.nextafter(reserved_at, math.inf))
            if isinstance(result, dict) and result.get('status') == 'sent' and result.get('receipt_ref'):
                return result
        except Exception:
            pass
        return {'status': 'uncertain', 'reason': 'receipt_unverified'}

    def reply_source(self, plan, last_touch_at):
        """Return a callable for ReplyCoverage, with a pinned last-touch time.

        Only complete, account-scoped history with verified participants and
        valid message timestamps can establish absence of a subsequent reply.
        """
        def read(profile_urn):
            unknown = {'target': profile_urn, 'complete': False, 'suppressed': False,
                       'coverage': 'exact_profile_conversation'}
            try:
                client = self.client
                client._plan(plan)
                if (plan['action'] != 'dm' or profile_urn != plan['recipient_profile_urn']
                        or not _time(last_touch_at)):
                    return unknown
                client.verify_identity()
                resolved = client._call('GET', '/linkedin/conversations/exists/'
                                        + quote(plan['recipient_public_id'], safe=''),
                                        {'accountId': client.account_id})
                if (resolved.get('success') is not True or resolved.get('accountId') != plan['account_id']
                        or resolved.get('profileUrn') != profile_urn
                        or type(resolved.get('hasConversation')) is not bool):
                    return unknown
                if not resolved['hasConversation']:
                    return {**unknown, 'complete': True, 'no_record': True}
                thread = resolved.get('conversationUrn')
                if not isinstance(thread, str) or not thread.startswith('urn:li:msg_conversation:'):
                    return unknown
                if plan.get('conversation_urn') not in (None, thread):
                    return unknown
                owner = client._profile_identity(client.self_profile_id)
                recipient = client._profile_identity(profile_urn)
                if not owner or not recipient or owner == recipient:
                    return unknown
                aliases = {client.expected_public_id: owner, plan['recipient_public_id']: recipient}
                seen, cursor = set(), None
                for _ in range(3):
                    query = {'accountId': client.account_id, 'count': 10, 'sync': 'true'}
                    if cursor is not None:
                        query['cursor'] = cursor
                    data = client._call('GET', '/linkedin/conversations/' + quote(thread, safe='') + '/messages', query)
                    if (data.get('success') is not True or data.get('accountId', client.account_id if cursor else None) != client.account_id
                            or data.get('conversationUrn') != thread or type(data.get('hasMore')) is not bool
                            or data.get('fetchingMore', False) is not False):
                        return unknown
                    people, rows = data.get('participants'), data.get('messages')
                    if (not isinstance(people, list) or not 2 <= len(people) <= 20
                            or not all(isinstance(p, dict) and isinstance(p.get('profileId'), str) for p in people)
                            or {aliases.get(p['profileId'], client._profile_identity(p['profileId'])) for p in people} != {owner, recipient}
                            or not isinstance(rows, list) or len(rows) > 10):
                        return unknown
                    times = []
                    for row in rows:
                        if (not isinstance(row, dict) or not isinstance(row.get('messageUrn'), str)
                                or not row['messageUrn'] or row['messageUrn'] in seen
                                or type(row.get('isSentByOwner')) is not bool or not _time(row.get('sentAt'))):
                            return unknown
                        seen.add(row['messageUrn'])
                        sender = client._sender_identity(row)
                        sender = aliases.get(sender, sender)
                        stamp = client._seconds(row['sentAt'])
                        if (sender not in {owner, recipient} or row['isSentByOwner'] != (sender == owner)
                                or cursor is not None and stamp * 1000 >= int(cursor)):
                            return unknown
                        times.append(stamp)
                        if sender == recipient and stamp > last_touch_at:
                            return {**unknown, 'complete': True, 'suppressed': True}
                    next_cursor = data.get('nextCursor')
                    if data['hasMore'] is False and next_cursor is None:
                        return {**unknown, 'complete': True, 'no_record': not seen}
                    if (not rows or not isinstance(next_cursor, str) or not re.fullmatch(r'[0-9]{1,16}', next_cursor)
                            or int(next_cursor) != int(min(times) * 1000)
                            or cursor is not None and int(next_cursor) >= int(cursor)):
                        return unknown
                    cursor = next_cursor
                return unknown
            except Exception:
                return unknown
        return read
