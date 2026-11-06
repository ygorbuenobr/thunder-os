import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

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

    def _quota_allowed(self, action):
        if action != 'dm' or not self.quota_available:
            raise ValueError('quota_exhausted')


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
        self.assertEqual(len(repo.events(key)), 1)
        self.assertFalse(self.safety.ready(self.binding, self.plan))

    def test_systemd_failure_blocks_and_history_is_not_inferred(self):
        self.safety.unit_check = lambda: False
        self.assertFalse(self.safety.other_writers_inactive())
        self.assertEqual(self.safety.last_touch_at(self.plan), 1.0)

    def test_last_touch_uses_recorded_reservation_time(self):
        self.touch(datetime.fromtimestamp(self.now - 100, timezone.utc).isoformat())
        self.assertEqual(self.safety.last_touch_at(self.plan), self.now - 100)


if __name__ == '__main__':
    unittest.main()
