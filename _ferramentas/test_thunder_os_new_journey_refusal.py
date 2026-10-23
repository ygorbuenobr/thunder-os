"""Real SQLite + pinned TouchLedger; no provider calls or migration by reader."""
from contextlib import closing, contextmanager
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from thunder_os.new_journey_refusal import RegisteredRefusalRead


EMAIL = 'person@example.invalid'
PROFILE = 'https://www.linkedin.com/in/fixture-person'


class RegisteredRefusalReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'ledger.db'
        with closing(sqlite3.connect(self.path)) as con, con:
            con.executescript('''
                CREATE TABLE thunder_suppressions (canal TEXT, pessoa_chave TEXT, reason TEXT);
                CREATE TABLE respostas (id INTEGER, canal TEXT, pessoa_chave TEXT);
                CREATE TABLE toques (id INTEGER,canal TEXT,acao TEXT,pessoa_chave TEXT,
                                     alvo TEXT,status TEXT,provider_id TEXT);
            ''')
        self.reader = RegisteredRefusalRead(self.database)
        for target in ('socket.socket.connect', 'socket.getaddrinfo'):
            p = patch(target, side_effect=AssertionError('network forbidden'))
            self.addCleanup(p.stop)
            self.addCleanup(p.start().assert_not_called)

    @contextmanager
    def database(self):
        con = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)
        try:
            yield con, '?'
        finally:
            con.close()

    def read(self, **kwargs):
        return self.reader.lookup([EMAIL], [PROFILE], **kwargs)

    def test_clear_is_registered_scope_only_and_schema_and_rows_unchanged(self):
        with closing(sqlite3.connect(self.path)) as con, con:
            before = list(con.iterdump())
        result = self.read()
        self.assertEqual(result['status'], 'clear')
        self.assertTrue(result['coverage']['complete'])
        self.assertFalse(result['coverage']['global_inbox_complete'])
        self.assertEqual(result['coverage']['scope'], 'registered_toques_refusals_and_replies')
        with closing(sqlite3.connect(self.path)) as con, con:
            self.assertEqual(before, list(con.iterdump()))

    def test_each_linked_channel_suppression_and_reply_blocks(self):
        for channel, identity in (('email', EMAIL), ('linkedin', PROFILE)):
            for table in ('thunder_suppressions', 'respostas'):
                with self.subTest(channel=channel, table=table):
                    with closing(sqlite3.connect(self.path)) as con, con:
                        con.execute('DELETE FROM thunder_suppressions')
                        con.execute('DELETE FROM respostas')
                        if table == 'thunder_suppressions':
                            con.execute('INSERT INTO thunder_suppressions VALUES (?,?,?)',
                                        (channel, identity, 'opt_out'))
                        else:
                            con.execute('INSERT INTO respostas VALUES (?,?,?)', (1, channel, identity))
                    result = self.read()
                    self.assertEqual(result['status'], 'blocked')
                    self.assertIn(result['reason'], ('registered_suppressed', 'registered_has_reply'))

    def test_missing_linked_identity_or_schema_is_unknown(self):
        for emails, profiles in (([], [PROFILE]), ([EMAIL], []), ([PROFILE], [EMAIL]),
                                 (['bad'], [PROFILE]), ([EMAIL] * 17, [PROFILE])):
            self.assertEqual(self.reader.lookup(emails, profiles)['status'], 'unknown')
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute('DROP TABLE respostas')
        result = self.read()
        self.assertEqual(result['status'], 'unknown')
        self.assertFalse(result['coverage']['complete'])

    def test_deadline_and_clock_change_are_not_fresh_proof(self):
        self.assertEqual(self.read(deadline=0)['status'], 'unknown')
        self.assertEqual(self.read()['source_ref'], self.read()['source_ref'])
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute('INSERT INTO thunder_suppressions VALUES (?,?,?)', ('email', EMAIL, 'negative'))
        self.assertNotEqual(self.read()['source_ref'], self.reader.lookup(['other@example.invalid'], [PROFILE])['source_ref'])

    def test_configured_missing_database_is_not_created(self):
        path = Path(self.tmp.name) / 'missing.db'
        with patch.dict(os.environ, {'TOQUES_SQLITE': str(path)}):
            result = RegisteredRefusalRead().lookup([EMAIL], [PROFILE])
        self.assertEqual(result['status'], 'unknown')
        self.assertFalse(path.exists())

    def test_reader_connection_rejects_write(self):
        @contextmanager
        def malicious():
            with closing(sqlite3.connect(self.path)) as con, con:
                yield con, '?'
                with self.assertRaises(sqlite3.OperationalError):
                    con.execute('DELETE FROM respostas')
        result = RegisteredRefusalRead(malicious).lookup([EMAIL], [PROFILE])
        self.assertEqual(result['status'], 'clear')


if __name__ == '__main__':
    unittest.main()
