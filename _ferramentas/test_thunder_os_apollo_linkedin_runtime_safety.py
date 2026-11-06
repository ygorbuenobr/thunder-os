import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from thunder_os.apollo_linkedin_runtime_safety import RuntimeSafety


class FakeRepo:
    def __init__(self):
        self.states = {}
        self.events = []
        self.reservations = []

    @contextmanager
    def transaction(self):
        yield

    def get_state(self, kind, key):
        return self.states.get((kind, key))

    def put_state(self, kind, key, payload):
        self.states[(kind, key)] = payload

    def append_event(self, key, event, payload):
        self.events.append((key, event, payload))

    def list_apollo_linkedin_reservations(self):
        return self.reservations


class FakeClient:
    account_id = 'account_synthetic'
    quota_available = True
    quota_used = 0
    quota_limit = 200
    quota_account = 'account_synthetic'
    quota_window = 'weekly'
    last_action = None

    def _quota_allowed(self, action):
        self.last_action = action
        if action not in ('dm', 'invite') or not self.quota_available:
            raise ValueError('quota_exhausted')

    def quota(self):
        return {'account_id': self.quota_account, 'quotas': [{
            'feature': 'CONNECT', 'window': self.quota_window,
            'used': self.quota_used, 'limit': self.quota_limit,
            'remaining': max(0, self.quota_limit-self.quota_used),
            'limitReached': self.quota_used >= self.quota_limit,
            'resetAt': datetime.fromtimestamp(2000003600, timezone.utc).isoformat()}]}


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'ledger.sqlite'
        with sqlite3.connect(self.path) as con:
            con.execute('CREATE TABLE toques(canal TEXT,status TEXT,pessoa_chave TEXT,atualizado_em TEXT,criado_em TEXT)')
        path = self.path
        class Ledger:
            @contextmanager
            def _banco(self):
                with sqlite3.connect(path) as con:
                    yield con, '?'
        self.repo, self.client = FakeRepo(), FakeClient()
        self.now = 2000000000.0
        self.safety = RuntimeSafety(self.repo, self.client, Ledger(), unit_check=lambda: True,
                                   clock=lambda: self.now, spacing_seconds=40)
        self.binding = {'company_id': 'company_synthetic', 'account_id': 'account_synthetic'}
        self.plan = {'account_id': 'account_synthetic', 'recipient_public_id': 'synthetic-person', 'action': 'dm'}

    def touch(self, timestamp):
        with sqlite3.connect(self.path) as con:
            con.execute('INSERT INTO toques VALUES(?,?,?,?,?)', ('linkedin', 'feito', 'https://www.linkedin.com/in/synthetic-person', timestamp, timestamp))

    def test_quota_and_spacing_are_checked(self):
        self.assertTrue(self.safety.ready(self.binding, self.plan))
        self.client.quota_available = False
        self.assertFalse(self.safety.ready(self.binding, self.plan))
        self.client.quota_available = True
        self.touch(datetime.fromtimestamp(self.now - 39, timezone.utc).isoformat())
        self.assertFalse(self.safety.ready(self.binding, self.plan))
        self.now += 2
        self.assertTrue(self.safety.ready(self.binding, self.plan))

    def test_invitation_requires_approved_policy_and_connect_quota(self):
        self.plan['action'] = 'invite'
        self.assertFalse(self.safety.ready(self.binding, self.plan))
        self.binding['_linkedin_policy'] = {'timezone': 'UTC', 'send_hour': 10,
            'weekly_invite_limit': 200, 'holidays': []}
        self.assertTrue(self.safety.ready(self.binding, self.plan))
        self.assertEqual(self.client.last_action, 'invite')
        self.client.quota_used = 200
        self.client.quota_limit = 250
        self.assertFalse(self.safety.ready(self.binding, self.plan))

    def test_invitation_applies_lower_campaign_and_provider_limits(self):
        self.plan['action'] = 'invite'
        self.binding['_linkedin_policy'] = {'timezone': 'UTC', 'send_hour': 10,
            'weekly_invite_limit': 50, 'holidays': []}
        self.client.quota_used = 49
        self.assertTrue(self.safety.ready(self.binding, self.plan))
        self.client.quota_used = 50
        self.assertFalse(self.safety.ready(self.binding, self.plan))
        self.client.quota_used = 39
        self.client.quota_limit = 40
        self.assertTrue(self.safety.ready(self.binding, self.plan))
        self.client.quota_used = 40
        self.assertFalse(self.safety.ready(self.binding, self.plan))

    def test_invitation_requires_weekly_quota_from_exact_account(self):
        self.plan['action'] = 'invite'
        self.binding['_linkedin_policy'] = {'timezone': 'UTC', 'send_hour': 10,
            'weekly_invite_limit': 200, 'holidays': []}
        self.client.quota_account = 'different_account'
        self.assertFalse(self.safety.ready(self.binding, self.plan))
        self.client.quota_account = self.client.account_id
        self.client.quota_window = 'daily'
        self.assertFalse(self.safety.ready(self.binding, self.plan))

    def test_invitation_rejects_inconsistent_or_expired_quota(self):
        self.plan['action'] = 'invite'
        self.binding['_linkedin_policy'] = {'timezone': 'UTC', 'send_hour': 10,
            'weekly_invite_limit': None, 'holidays': []}
        actual = self.client.quota
        bad = actual()
        bad['quotas'][0]['remaining'] = 199
        with patch.object(self.client, 'quota', return_value=bad):
            self.assertFalse(self.safety.ready(self.binding, self.plan))
        bad = actual()
        bad['quotas'][0]['resetAt'] = datetime.fromtimestamp(self.now - 1, timezone.utc).isoformat()
        with patch.object(self.client, 'quota', return_value=bad):
            self.assertFalse(self.safety.ready(self.binding, self.plan))

    def test_invitation_reply_stop_and_pacing_still_block(self):
        self.plan['action'] = 'invite'
        self.binding['_linkedin_policy'] = {'timezone': 'UTC', 'send_hour': 10,
            'weekly_invite_limit': 200, 'holidays': []}
        self.touch(datetime.fromtimestamp(self.now - 39, timezone.utc).isoformat())
        self.assertFalse(self.safety.ready(self.binding, self.plan))
        self.now += 2
        self.assertTrue(self.safety.ready(self.binding, self.plan))
        self.safety.pause_after_reply(self.binding, self.plan)
        self.assertFalse(self.safety.ready(self.binding, self.plan))

    def test_historic_other_account_is_conservatively_counted(self):
        self.touch(datetime.fromtimestamp(self.now - 10, timezone.utc).isoformat())
        self.assertFalse(self.safety.ready(self.binding, self.plan))

    def test_legacy_row_without_update_time_uses_creation_time(self):
        with sqlite3.connect(self.path) as con:
            con.execute('INSERT INTO toques VALUES(?,?,?,?,?)', ('linkedin', 'feito', 'https://www.linkedin.com/in/legacy',
                        None, datetime.fromtimestamp(self.now - 10, timezone.utc).isoformat()))
        self.assertFalse(self.safety.ready(self.binding, self.plan))
        self.now += 31
        self.assertTrue(self.safety.ready(self.binding, self.plan))

    def test_unknown_timestamp_and_ledger_failure_block(self):
        self.touch(None)
        self.assertFalse(self.safety.ready(self.binding, self.plan))
        with self.assertRaises(ValueError):
            self.safety.last_touch_at(self.plan)

    def test_reply_stop_is_durable_idempotent_and_blocks(self):
        self.assertTrue(self.safety.pause_after_reply(self.binding, self.plan)['persisted'])
        self.assertTrue(self.safety.pause_after_reply(self.binding, self.plan)['persisted'])
        self.assertEqual(len(self.repo.events), 1)
        self.assertFalse(self.safety.ready(self.binding, self.plan))

    def test_reply_stop_is_read_back_from_real_repository(self):
        from thunder_os.db import Repository
        repo = Repository(Path(self.temp.name) / 'states.sqlite')
        self.safety.repo = repo
        key = self.safety._key(self.binding, self.plan)
        self.assertTrue(self.safety.pause_after_reply(self.binding, self.plan)['persisted'])
        self.assertTrue(repo.get_state('apollo_linkedin_reply_stop', key)['active'])
        alert = repo.get_state('operator_alert', 'apollo-linkedin-reply:' + key)
        self.assertTrue(alert['active'])
        self.assertEqual(alert['priority'], 'high')
        self.assertEqual(alert['recipient_public_id'], 'synthetic-person')
        self.assertIn('não confirmada', alert['message'])
        self.assertFalse(alert['external_pauses_enabled'])
        self.assertEqual(len(repo.events(key)), 1)
        self.assertFalse(self.safety.ready(self.binding, self.plan))

    def test_repeat_observation_does_not_duplicate_or_reopen_alert(self):
        from thunder_os.db import Repository
        repo = Repository(Path(self.temp.name) / 'states.sqlite')
        self.safety.repo = repo
        first = self.safety.pause_after_reply(self.binding, self.plan)
        alert = repo.get_state('operator_alert', first['alert_id'])
        repo.put_state('operator_alert', first['alert_id'], {**alert, 'active': False})
        self.safety.pause_after_reply(self.binding, self.plan)
        self.assertFalse(repo.get_state('operator_alert', first['alert_id'])['active'])
        self.assertEqual(len(repo.list_state('operator_alert')), 1)
        self.assertEqual(len(repo.events(self.safety._key(self.binding, self.plan))), 1)

    def test_stop_and_event_roll_back_if_alert_persistence_fails(self):
        from thunder_os.db import Repository
        repo = Repository(Path(self.temp.name) / 'states.sqlite')
        self.safety.repo = repo
        key = self.safety._key(self.binding, self.plan)
        put = repo.put_state
        def fail_alert(kind, ident, payload):
            if kind == 'operator_alert':
                raise OSError('simulated failure')
            return put(kind, ident, payload)
        with patch.object(repo, 'put_state', side_effect=fail_alert):
            with self.assertRaises(OSError):
                self.safety.pause_after_reply(self.binding, self.plan)
        self.assertIsNone(repo.get_state('apollo_linkedin_reply_stop', key))
        self.assertEqual(repo.events(key), [])

    def test_reply_identity_rejects_unsafe_recipient(self):
        self.plan['recipient_public_id'] = 'person\nmessage'
        with self.assertRaises(ValueError):
            self.safety.pause_after_reply(self.binding, self.plan)

    def test_stop_rolls_back_even_if_outer_transaction_catches_alert_error(self):
        from thunder_os.db import Repository
        repo = Repository(Path(self.temp.name) / 'states.sqlite')
        self.safety.repo = repo
        key = self.safety._key(self.binding, self.plan)
        put = repo.put_state
        def fail_alert(kind, ident, payload):
            if kind == 'operator_alert':
                raise OSError('simulated failure')
            return put(kind, ident, payload)
        with repo.transaction():
            with patch.object(repo, 'put_state', side_effect=fail_alert):
                with self.assertRaises(OSError):
                    self.safety.pause_after_reply(self.binding, self.plan)
            repo.put_state('control_test', 'separate', {'committed': True})
        self.assertIsNone(repo.get_state('apollo_linkedin_reply_stop', key))
        self.assertEqual(repo.events(key), [])
        self.assertTrue(repo.get_state('control_test', 'separate')['committed'])

    def test_systemd_failure_blocks_and_history_is_not_inferred(self):
        self.safety.unit_check = lambda: False
        self.assertFalse(self.safety.other_writers_inactive())
        self.assertEqual(self.safety.last_touch_at(self.plan), 1.0)

    def test_last_touch_uses_recorded_reservation_time(self):
        self.touch(datetime.fromtimestamp(self.now - 100, timezone.utc).isoformat())
        self.assertEqual(self.safety.last_touch_at(self.plan), self.now - 100)


    def test_invitation_requires_approved_policy_and_connect_quota(self):
        self.plan['action'] = 'invite'
        self.assertFalse(self.safety.ready(self.binding, self.plan))
        self.binding['_linkedin_policy'] = {'timezone': 'UTC', 'send_hour': 10,
            'weekly_invite_limit': 200, 'holidays': []}
        self.assertTrue(self.safety.ready(self.binding, self.plan))
        self.assertEqual(self.client.last_action, 'invite')
        self.client.quota_used = 200
        self.client.quota_limit = 250
        self.assertFalse(self.safety.ready(self.binding, self.plan))

    def test_invitation_applies_lower_campaign_and_provider_limits(self):
        self.plan['action'] = 'invite'
        self.binding['_linkedin_policy'] = {'timezone': 'UTC', 'send_hour': 10,
            'weekly_invite_limit': 50, 'holidays': []}
        self.client.quota_used = 49
        self.assertTrue(self.safety.ready(self.binding, self.plan))
        self.client.quota_used = 50
        self.assertFalse(self.safety.ready(self.binding, self.plan))
        self.client.quota_used = 39
        self.client.quota_limit = 40
        self.assertTrue(self.safety.ready(self.binding, self.plan))
        self.client.quota_used = 40
        self.assertFalse(self.safety.ready(self.binding, self.plan))

    def test_invitation_requires_weekly_quota_from_exact_account(self):
        self.plan['action'] = 'invite'
        self.binding['_linkedin_policy'] = {'timezone': 'UTC', 'send_hour': 10,
            'weekly_invite_limit': 200, 'holidays': []}
        self.client.quota_account = 'different_account'
        self.assertFalse(self.safety.ready(self.binding, self.plan))
        self.client.quota_account = self.client.account_id
        self.client.quota_window = 'daily'
        self.assertFalse(self.safety.ready(self.binding, self.plan))

    def test_invitation_rejects_inconsistent_or_expired_quota(self):
        self.plan['action'] = 'invite'
        self.binding['_linkedin_policy'] = {'timezone': 'UTC', 'send_hour': 10,
            'weekly_invite_limit': None, 'holidays': []}
        actual = self.client.quota
        bad = actual()
        bad['quotas'][0]['remaining'] = 199
        with patch.object(self.client, 'quota', return_value=bad):
            self.assertFalse(self.safety.ready(self.binding, self.plan))
        bad = actual()
        bad['quotas'][0]['resetAt'] = datetime.fromtimestamp(self.now - 1, timezone.utc).isoformat()
        with patch.object(self.client, 'quota', return_value=bad):
            self.assertFalse(self.safety.ready(self.binding, self.plan))

    def test_invitation_reply_stop_and_pacing_still_block(self):
        self.plan['action'] = 'invite'
        self.binding['_linkedin_policy'] = {'timezone': 'UTC', 'send_hour': 10,
            'weekly_invite_limit': 200, 'holidays': []}
        self.touch(datetime.fromtimestamp(self.now - 39, timezone.utc).isoformat())
        self.assertFalse(self.safety.ready(self.binding, self.plan))
        self.now += 2
        self.assertTrue(self.safety.ready(self.binding, self.plan))
        self.safety.pause_after_reply(self.binding, self.plan)
        self.assertFalse(self.safety.ready(self.binding, self.plan))


if __name__ == '__main__':
    unittest.main()
