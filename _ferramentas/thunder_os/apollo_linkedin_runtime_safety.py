"""Account pacing, quota and durable reply stop for the Apollo canary.

The touch ledger does not consistently attribute historic records to a sending
account. Pacing therefore uses all LinkedIn touches as a conservative upper
bound. The caller owns the account lock and the SQLite dispatch transaction.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import time

from .touch_ledger import toques


def _seconds(value):
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, str):
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    else:
        raise ValueError('ledger_timestamp_unavailable')
    # Ledger timestamps without offset are written as UTC by SQLite.
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


class RuntimeSafety:
    def __init__(self, repository, client, ledger, *, unit_check, clock=time.time, spacing_seconds=90):
        if (not callable(unit_check) or not callable(clock) or type(spacing_seconds) is not int
                or not 40 <= spacing_seconds <= 90 or not callable(getattr(ledger, '_banco', None))):
            raise ValueError('safety_configuration_invalid')
        self.repo, self.client, self.ledger = repository, client, ledger
        self.unit_check, self.clock, self.spacing_seconds = unit_check, clock, spacing_seconds

    def other_writers_inactive(self):
        try:
            return self.unit_check() is True
        except Exception:
            return False

    @staticmethod
    def _key(binding, plan):
        company, recipient = binding.get('company_id'), plan.get('recipient_public_id')
        if not all(isinstance(v, str) and v and v == v.strip() for v in (company, recipient)):
            raise ValueError('stop_identity_invalid')
        raw = json.dumps([company, recipient.lower()], ensure_ascii=True, separators=(',', ':'))
        return hashlib.sha256(raw.encode()).hexdigest()

    def pause_after_reply(self, binding, plan):
        """Persist a recipient stop; no provider mutation is performed."""
        key = self._key(binding, plan)
        with self.repo.transaction():
            existing = self.repo.get_state('apollo_linkedin_reply_stop', key)
            if existing is None:
                self.repo.put_state('apollo_linkedin_reply_stop', key,
                    {'company': binding['company_id'], 'recipient': plan['recipient_public_id'].lower(),
                     'active': True, 'reason': 'human_reply'})
                self.repo.append_event(key, 'apollo_linkedin_reply_stop', {'reason': 'human_reply'})
            elif existing.get('active') is not True:
                raise ValueError('reply_stop_conflict')
        return {'persisted': True}

    def _ledger_time(self, person=None):
        with self.ledger._banco() as (con, mark):
            cur = con.cursor()
            sql = "SELECT MAX(atualizado_em), COUNT(*), COUNT(atualizado_em) FROM toques WHERE canal='linkedin' AND status IN ('feito','incerto')"
            args = ()
            if person is not None:
                sql += ' AND pessoa_chave=' + mark
                args = (person,)
            cur.execute(sql, args)
            row = cur.fetchone()
        if not row or row[1] != row[2]:
            raise ValueError('ledger_timestamp_unavailable')
        return _seconds(row[0]) if row[1] else None

    def last_touch_at(self, plan):
        """A lower bound for reply lookup; unknown history raises, never guesses."""
        public_id = plan['recipient_public_id']
        if not isinstance(public_id, str) or not public_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in public_id):
            raise ValueError('recipient_invalid')
        person = toques.normalizar_pessoa('linkedin', 'https://www.linkedin.com/in/' + public_id).lower()
        with self.ledger._banco() as (con, mark):
            cur = con.cursor()
            cur.execute("SELECT MAX(criado_em), COUNT(*), COUNT(criado_em) FROM toques "
                        "WHERE canal='linkedin' AND status='feito' AND pessoa_chave=" + mark, (person,))
            row = cur.fetchone()
        if not row or row[1] != row[2]:
            raise ValueError('ledger_timestamp_unavailable')
        latest = _seconds(row[0]) if row[1] else None
        for reservation in self.repo.list_apollo_linkedin_reservations():
            if (reservation.get('account_id') != plan['account_id']
                    or reservation.get('recipient', '').lower() != public_id.lower()
                    or reservation.get('state') != 'confirmed'):
                continue
            # Reservation precedes dispatch and is a safe lower bound for send time.
            stamp = _seconds(reservation.get('created_at'))
            latest = max(stamp, latest) if latest is not None else stamp
        now = self.clock()
        if latest is not None and (latest <= 0 or latest > now):
            raise ValueError('ledger_timestamp_invalid')
        return latest if latest is not None else 1.0

    def ready(self, binding, plan):
        try:
            if (not isinstance(binding, dict) or not isinstance(plan, dict)
                    or binding.get('account_id') != self.client.account_id
                    or plan.get('account_id') != self.client.account_id
                    or plan.get('action') != 'dm'):
                return False
            if self.repo.get_state('apollo_linkedin_reply_stop', self._key(binding, plan)) is not None:
                return False
            self.client._quota_allowed('dm')
            now = self.clock()
            if type(now) not in (float, int) or not math.isfinite(now) or now <= 0:
                return False
            latest = self._ledger_time()
            for row in self.repo.list_apollo_linkedin_reservations():
                if row.get('account_id') != self.client.account_id or row.get('state') not in ('confirmed', 'uncertain'):
                    continue
                stamp = _seconds(row.get('updated_at'))
                latest = max(stamp, latest) if latest is not None else stamp
            return latest is None or 0 <= now - latest >= self.spacing_seconds
        except Exception:
            return False
