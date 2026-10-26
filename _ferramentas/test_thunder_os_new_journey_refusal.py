"""Real SQLite + pinned TouchLedger; no provider calls or migration by reader."""
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from thunder_os.new_journey_refusal import RegisteredRefusalRead
from thunder_os.touch_ledger import TouchLedger, toques


EMAIL = 'person@example.invalid'
PROFILE = 'https://www.linkedin.com/in/fixture-person'


class SQLStateFailure(Exception):
    def __init__(self, state):
        super().__init__('secret DSN/query/alias must never be returned')
        self.pgcode = state


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
        self.assertNotIn('failure_code', result['coverage'])
        # Literal baseline from HEAD before the diagnostic field was added.
        self.assertEqual(result['source_ref'],
                         'toques:registered:1de8c4de9dcd5b2c34c05ec355aa69ba392f826218f2645d5237844bc366a9af')
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
                    self.assertNotIn('failure_code', result['coverage'])

    def assert_failure(self, result, code):
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['reason'], 'registered_refusal_coverage_unavailable')
        self.assertFalse(result['coverage']['complete'])
        self.assertFalse(result['coverage']['global_inbox_complete'])
        self.assertEqual(result['coverage']['failure_code'], code)
        self.assertEqual(set(result['coverage']), {
            'complete', 'scope', 'global_inbox_complete', 'failure_code'})
        for forbidden in ('secret', 'DSN', 'query', 'alias', EMAIL, PROFILE):
            self.assertNotIn(forbidden, json.dumps(result))

    def test_identity_and_deadline_failures_have_typed_codes(self):
        for emails, profiles in (([], [PROFILE]), ([EMAIL], []), (['bad'], [PROFILE])):
            with self.subTest(emails=emails, profiles=profiles):
                self.assert_failure(self.reader.lookup(emails, profiles), 'identity_incomplete')
        self.assert_failure(self.read(deadline='secret'), 'deadline_invalid')
        self.assert_failure(self.read(deadline=0), 'deadline_exceeded')

    def test_deadline_requires_finite_monotonic_number_not_bool(self):
        for deadline in (float('nan'), float('inf'), float('-inf'), True, False):
            with self.subTest(deadline=deadline):
                self.assert_failure(self.read(deadline=deadline), 'deadline_invalid')

    def test_database_and_unknown_exceptions_have_only_allowlisted_codes(self):
        for exc, code in (
                (SQLStateFailure('42501'), 'database_permission_denied'),
                (SQLStateFailure('25006'), 'readonly_violation'),
                (sqlite3.OperationalError('secret DSN/query/alias'), 'database_unavailable'),
                (toques.ErroBanco('secret DSN/query/alias'), 'database_unavailable'),
                (RuntimeError('secret DSN/query/alias'), 'unknown_failure'),
                (SQLStateFailure('secret'), 'unknown_failure'),
                (ValueError('registered_identity_incomplete secret'), 'unknown_failure')):
            with self.subTest(code=code, exception=type(exc).__name__):
                with patch.object(TouchLedger, 'preflight', side_effect=exc):
                    self.assert_failure(self.read(), code)

    def test_default_postgres_context_preserves_sqlstate_before_toques_masks_it(self):
        # The pinned context intentionally replaces generic driver exceptions.
        @contextmanager
        def masking_database():
            try:
                yield connection, '%s'
            except toques.ErroBanco:
                raise
            except Exception:
                raise toques.ErroBanco('masked driver failure') from None

        connection = unittest.mock.Mock()
        for state, code in (('42501', 'database_permission_denied'), ('25006', 'readonly_violation')):
            for setup_failure in (False, True):
                with self.subTest(state=state, setup_failure=setup_failure):
                    connection.cursor.return_value.execute.side_effect = (
                        SQLStateFailure(state) if setup_failure else None)
                    with patch.dict(os.environ, {'TOQUES_SQLITE': ''}), \
                            patch.object(toques, '_banco', masking_database), \
                            patch.object(TouchLedger, 'preflight', side_effect=SQLStateFailure(state)):
                        self.assert_failure(RegisteredRefusalRead().lookup([EMAIL], [PROFILE]), code)

    def test_invalid_proof_is_unknown_and_does_not_become_clear(self):
        for proof in (None, {}, {'channel': 'email', 'identity': EMAIL},
                      {'channel': 'email', 'identity': EMAIL, 'blocked': 'secret'},
                      {'channel': 'email', 'identity': 'other@example.invalid', 'blocked': None}):
            with self.subTest(proof=proof), patch.object(TouchLedger, 'preflight', return_value=proof):
                self.assert_failure(self.read(), 'invalid_proof')

    def test_positive_block_survives_later_failure_with_diagnostic(self):
        for table, reason in (('thunder_suppressions', 'registered_suppressed'),
                              ('respostas', 'registered_has_reply')):
            with self.subTest(table=table):
                with closing(sqlite3.connect(self.path)) as con, con:
                    con.execute('DELETE FROM thunder_suppressions')
                    con.execute('DELETE FROM respostas')
                    con.execute('INSERT INTO ' + table + ' VALUES (?,?,?)',
                                ('email', EMAIL, 'opt_out') if table == 'thunder_suppressions'
                                else (1, 'email', EMAIL))
                original, calls = TouchLedger.preflight, []

                def fail_after_first(ledger, *args, **kwargs):
                    calls.append(True)
                    if len(calls) > 1:
                        raise SQLStateFailure('42501')
                    return original(ledger, *args, **kwargs)

                with patch.object(TouchLedger, 'preflight', fail_after_first):
                    result = self.read()
                self.assertEqual(result['status'], 'blocked')
                self.assertEqual(result['reason'], reason)
                self.assertFalse(result['coverage']['complete'])
                self.assertFalse(result['coverage']['global_inbox_complete'])
                self.assertEqual(result['coverage']['failure_code'], 'database_permission_denied')

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
        self.assertEqual(result['coverage']['failure_code'], 'database_unavailable')
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
