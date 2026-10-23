"""Read registered CRM/toques refusal state, not global inbox completeness.

This port cannot reserve, record, suppress or send. All aliases must come from
the same freshly verified HubSpot contact. Unread provider conversations remain
outside this evidence scope and the send-time preflight is not replaced.
"""
from contextlib import contextmanager
from pathlib import Path
import os
import sqlite3
import time

from .models import payload_hash
from .touch_ledger import TouchLedger, toques


@contextmanager
def _existing_database():
    configured = os.environ.get('TOQUES_SQLITE', '').strip()
    if configured:
        path = Path(configured)
        if configured == ':memory:' or not path.is_file() or path.is_symlink():
            raise ValueError('registered_refusal_unavailable')
        connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
        try:
            connection.execute('PRAGMA query_only=ON')
            yield connection, '?'
        finally:
            connection.close()
    else:
        with toques._banco() as (connection, marker):
            if marker != '%s':
                raise ValueError('registered_refusal_unavailable')
            connection.cursor().execute('SET TRANSACTION READ ONLY')
            connection.cursor().execute('SET LOCAL statement_timeout = 2000')
            yield connection, marker


class RegisteredRefusalRead:
    def __init__(self, database=None, clock=None):
        self.database = database or _existing_database
        self.clock = clock or time.monotonic

    def lookup(self, emails, profiles, *, deadline=None):
        result = {'status': 'unknown', 'reason': 'registered_refusal_coverage_unavailable',
                  'coverage': {'complete': False, 'scope': 'registered_toques_refusals_and_replies',
                               'global_inbox_complete': False}, 'source_ref': None}
        observations = []
        try:
            if (not isinstance(emails, list) or not isinstance(profiles, list)
                    or not emails or not profiles or len(emails) + len(profiles) > 16):
                raise ValueError('registered_identity_incomplete')
            identities = []
            for channel, values in (('email', emails), ('linkedin', profiles)):
                for value in values:
                    destination = TouchLedger.identity_destination(value)
                    if destination is None or destination[0] != channel:
                        raise ValueError('registered_identity_incomplete')
                    identities.append(destination)
            identities = sorted(set(identities))
            end = self.clock() + 5
            if deadline is not None:
                if type(deadline) not in (float, int):
                    raise ValueError('registered_refusal_deadline_invalid')
                end = min(end, deadline)
            with self.database() as (connection, marker):
                if marker == '?':
                    connection.execute('PRAGMA query_only=ON')
                elif marker != '%s':
                    raise ValueError('registered_refusal_unavailable')

                @contextmanager
                def borrowed():
                    yield connection, marker

                ledger = TouchLedger(db_context=borrowed)
                for channel, identity in identities:
                    if self.clock() >= end:
                        raise ValueError('registered_refusal_deadline')
                    proof = ledger.preflight('new-journey-refusal-read', {
                        'channel': channel, 'recipient': identity,
                        'subject': 'New journey refusal read'})
                    if proof.get('channel') != channel or proof.get('identity') != identity:
                        raise ValueError('registered_identity_incomplete')
                    observations.append({'channel': channel, 'identity': identity,
                                         'blocked': proof.get('blocked')})
                    if proof.get('blocked') in ('suppressed', 'has_reply'):
                        result.update(status='blocked', reason='registered_' + proof['blocked'])
                if self.clock() >= end:
                    raise ValueError('registered_refusal_deadline')
            result['coverage']['complete'] = True
            if result['status'] != 'blocked':
                result.update(status='clear', reason='registered_refusals_clear')
        except Exception:
            if not any(row['blocked'] in ('suppressed', 'has_reply') for row in observations):
                result.update(status='unknown', reason='registered_refusal_coverage_unavailable')
        result['source_ref'] = 'toques:registered:' + payload_hash({
            'observations': observations, 'status': result['status'], 'coverage': result['coverage']})
        return result
