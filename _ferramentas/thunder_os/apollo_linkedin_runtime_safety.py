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
import re
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
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', recipient):
            raise ValueError('stop_identity_invalid')
        raw = json.dumps([company, recipient.lower()], ensure_ascii=True, separators=(',', ':'))
        return hashlib.sha256(raw.encode()).hexdigest()

    def pause_after_reply(self, binding, plan):
        """Atomically persist the stop and an operator alert; no provider mutation."""
        key = self._key(binding, plan)
        alert_id = 'apollo-linkedin-reply:' + key
        with self.repo.transaction() as con:
            # The caller may catch a callback exception inside an outer dispatch
            # transaction. A savepoint prevents committing only half this stop.
            if con is not None:
                con.execute('SAVEPOINT apollo_linkedin_reply_stop')
            try:
                existing = self.repo.get_state('apollo_linkedin_reply_stop', key)
                if existing is None:
                    self.repo.put_state('apollo_linkedin_reply_stop', key,
                        {'company': binding['company_id'], 'recipient': plan['recipient_public_id'].lower(),
                         'active': True, 'reason': 'human_reply'})
                    self.repo.append_event(key, 'apollo_linkedin_reply_stop', {'reason': 'human_reply'})
                elif existing.get('active') is not True:
                    raise ValueError('reply_stop_conflict')
                alert = self.repo.get_state('operator_alert', alert_id)
                if alert is None:
                    self.repo.put_state('operator_alert', alert_id, {
                        'control_source': 'apollo_linkedin_reply_stop',
                        'stop_id': key, 'active': True, 'priority': 'high',
                        'owner': 'ygor', 'title': 'Revisar resposta e parada dos canais',
                        'reason': 'human_reply', 'company_id': binding['company_id'],
                        'recipient_public_id': plan['recipient_public_id'].lower(),
                        'message': ('Resposta detectada para ' + plan['recipient_public_id'].lower()
                                    + '. Parada externa no Apollo não confirmada; revisar canais.'),
                        'next_action': 'Abrir a conversa e confirmar a parada da cadência no Apollo.',
                        'external_pauses_enabled': False,
                    })
                elif alert.get('control_source') != 'apollo_linkedin_reply_stop' or alert.get('stop_id') != key:
                    raise ValueError('reply_alert_conflict')
            except Exception:
                if con is not None:
                    con.execute('ROLLBACK TO SAVEPOINT apollo_linkedin_reply_stop')
                    con.execute('RELEASE SAVEPOINT apollo_linkedin_reply_stop')
                raise
            if con is not None:
                con.execute('RELEASE SAVEPOINT apollo_linkedin_reply_stop')
        return {'persisted': True, 'alert_id': alert_id}

    def _ledger_time(self, person=None):
        with self.ledger._banco() as (con, mark):
            cur = con.cursor()
            # Legacy rows (observed on the live ledger, 06/11/2026) lack
            # atualizado_em; their creation time is the available bound. A row
            # with neither timestamp still blocks.
            sql = ("SELECT MAX(COALESCE(atualizado_em, criado_em)), COUNT(*), COUNT(COALESCE(atualizado_em, criado_em)) "
                   "FROM toques WHERE canal='linkedin' AND status IN ('feito','incerto')")
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

    def _invitation_quota_ok(self, binding, now):
        """Apply the approved weekly cap to fresh, account-scoped CONNECT usage.

        The provider reports only activity through ConnectSafely. Never infer
        usage in other tools from this number. The approved campaign may set a
        lower cap; the account policy observed by the operator sets 200 as an
        upper bound even if the provider later reports a larger allowance.
        """
        from .linkedin_preparation import validate_policy
        policy = validate_policy(binding['_linkedin_policy'])
        configured = policy['weekly_invite_limit']
        cap = min(200, configured) if configured is not None else 200
        # The existing provider validator checks every applicable window and
        # rejects exhausted/invalid quotas. Re-read weekly usage for the
        # independently approved campaign/account cap.
        self.client._quota_allowed('invite')
        quota = self.client.quota()
        if (not isinstance(quota, dict) or quota.get('account_id') != self.client.account_id
                or not isinstance(quota.get('quotas'), list)):
            return False
        rows = [row for row in quota['quotas'] if isinstance(row, dict)
                and row.get('feature') == 'CONNECT' and row.get('window') == 'weekly']
        if len(rows) != 1:
            return False
        row = rows[0]
        used, limit, remaining = row.get('used'), row.get('limit'), row.get('remaining')
        if (type(used) is not int or type(limit) is not int or type(remaining) is not int
                or used < 0 or limit <= 0 or remaining != max(0, limit - used)
                or remaining <= 0 or row.get('limitReached') is not False):
            return False
        reset = datetime.fromisoformat(row['resetAt'].replace('Z', '+00:00'))
        return (reset.tzinfo is not None and reset.utcoffset() is not None
                and reset.timestamp() > now and used < min(cap, limit))

    def ready(self, binding, plan):
        try:
            if (not isinstance(binding, dict) or not isinstance(plan, dict)
                    or binding.get('account_id') != self.client.account_id
                    or plan.get('account_id') != self.client.account_id
                    or plan.get('action') not in ('dm', 'invite')):
                return False
            if self.repo.get_state('apollo_linkedin_reply_stop', self._key(binding, plan)) is not None:
                return False
            now = self.clock()
            if type(now) not in (float, int) or not math.isfinite(now) or now <= 0:
                return False
            if plan['action'] == 'invite':
                if not self._invitation_quota_ok(binding, now):
                    return False
            else:
                self.client._quota_allowed('dm')
            latest = self._ledger_time()
            for row in self.repo.list_apollo_linkedin_reservations():
                if row.get('account_id') != self.client.account_id or row.get('state') not in ('confirmed', 'uncertain'):
                    continue
                stamp = _seconds(row.get('updated_at'))
                latest = max(stamp, latest) if latest is not None else stamp
            return latest is None or 0 <= now - latest >= self.spacing_seconds
        except Exception:
            return False
