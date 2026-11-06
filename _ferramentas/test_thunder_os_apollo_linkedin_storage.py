import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from thunder_os.db import Repository, RepositoryConflict
from thunder_os.service import ThunderService


def contend(path, queue, gate):
    patcher = patch("thunder_os.db.OFFICIAL_PANEL_DB", Path(path)); patcher.start()
    service = ThunderService(Repository(path))
    gate.wait()
    try:
        service.reserve_apollo_linkedin('company', 'task', 'linkedin_step_connect', 'account')
        queue.put('reserved')
    except RepositoryConflict:
        queue.put('conflict')


def contend_account(path, queue):
    patcher = patch("thunder_os.db.OFFICIAL_PANEL_DB", Path(path)); patcher.start()
    service = ThunderService(Repository(path))
    try:
        with service.apollo_linkedin_account_lock('account'):
            queue.put('acquired')
    except RepositoryConflict:
        queue.put('conflict')


class ApolloLinkedinStorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'painel.db')
        Path(self.path).touch()
        self.patcher = patch("thunder_os.db.OFFICIAL_PANEL_DB", Path(self.path))
        self.patcher.start(); self.addCleanup(self.patcher.stop)
        self.service = ThunderService(Repository(self.path))

    def test_reservation_is_unique_and_uncertain_never_reopened(self):
        self.service.reserve_apollo_linkedin('company', 'task', 'linkedin_step_connect', 'account')
        self.service.finish_apollo_linkedin('company', 'task', 'linkedin_step_connect', 'uncertain', 'timeout')
        with self.assertRaises(RepositoryConflict):
            self.service.reserve_apollo_linkedin('company', 'task', 'linkedin_step_connect', 'other')
        with self.assertRaises(RepositoryConflict):
            self.service.finish_apollo_linkedin('company', 'task', 'linkedin_step_connect', 'confirmed')
        self.assertEqual(self.service.list_apollo_linkedin_reservations()[0]['state'], 'uncertain')
        self.service.reserve_apollo_linkedin('company', 'task', 'linkedin_step_message', 'account')
        self.service.reserve_apollo_linkedin('other', 'task', 'linkedin_step_connect', 'account')

    def test_independent_processes_cannot_reserve_same_identity(self):
        ctx = multiprocessing.get_context('spawn')
        queue, gate = ctx.Queue(), ctx.Event()
        processes = [ctx.Process(target=contend, args=(self.path, queue, gate)) for _ in range(2)]
        for process in processes:
            process.start()
        gate.set()
        results = [queue.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(15)
            self.assertEqual(process.exitcode, 0)
        self.assertCountEqual(results, ['reserved', 'conflict'])

    def test_account_lock_is_shared_by_independent_repositories(self):
        other = ThunderService(Repository(self.path))
        with self.service.apollo_linkedin_account_lock('account'):
            with self.assertRaises(RepositoryConflict):
                with other.apollo_linkedin_account_lock('account'):
                    self.fail('account lock was not exclusive')
            with other.apollo_linkedin_account_lock('different'):
                pass
        with other.apollo_linkedin_account_lock('account'):
            pass

    def test_account_lock_is_shared_across_processes(self):
        ctx = multiprocessing.get_context('spawn')
        queue = ctx.Queue()
        with self.service.apollo_linkedin_account_lock('account'):
            process = ctx.Process(target=contend_account, args=(self.path, queue))
            process.start()
            self.assertEqual(queue.get(timeout=15), 'conflict')
            process.join(15)
            self.assertEqual(process.exitcode, 0)

    def test_expired_lease_is_recovered_only_after_owner_exits(self):
        import sqlite3
        ctx = multiprocessing.get_context('spawn')
        queue = ctx.Queue()
        with self.service.apollo_linkedin_account_lock('account'):
            with sqlite3.connect(self.path) as con:
                con.execute("UPDATE apollo_linkedin_account_locks SET lease_until=? WHERE account_id=?",
                            ('2000-01-01T00:00:00+00:00', 'account'))
            process = ctx.Process(target=contend_account, args=(self.path, queue))
            process.start()
            self.assertEqual(queue.get(timeout=15), 'conflict')
            process.join(15)
            self.assertEqual(process.exitcode, 0)
        with sqlite3.connect(self.path) as con:
            con.execute('INSERT INTO apollo_linkedin_account_locks (account_id, owner, created_at, lease_until) VALUES (?,?,?,?)',
                        ('account', 'dead-owner', '2000-01-01T00:00:00+00:00', '2000-01-01T00:01:00+00:00'))
        with self.service.apollo_linkedin_account_lock('account'):
            self.assertEqual(len(self.service.list_apollo_linkedin_account_locks()), 1)

    def test_cleanup_failure_does_not_mask_original_exception(self):
        import sqlite3
        with self.assertLogs('thunder_os.db', level='ERROR'):
            with self.assertRaisesRegex(ValueError, 'original error'):
                with self.service.apollo_linkedin_account_lock('account'):
                    with sqlite3.connect(self.path) as con:
                        con.execute('DELETE FROM apollo_linkedin_account_locks WHERE account_id=?', ('account',))
                    raise ValueError('original error')

    def test_unknown_or_malformed_lease_fails_closed(self):
        import sqlite3
        for value in (None, '', '0', '2000-01-01T00:00:00'):
            with self.subTest(value=value):
                with sqlite3.connect(self.path) as con:
                    con.execute('DELETE FROM apollo_linkedin_account_locks')
                    con.execute('INSERT INTO apollo_linkedin_account_locks (account_id, owner, created_at, lease_until) VALUES (?,?,?,?)',
                                ('account', 'unknown-owner', '2000-01-01T00:00:00+00:00', value))
                with self.assertRaises(RepositoryConflict):
                    with self.service.apollo_linkedin_account_lock('account'):
                        self.fail('unverifiable lease was recovered')

    def test_configured_store_requires_existing_absolute_database(self):
        import os
        missing = Path(self.tmp.name) / 'not-created.db'
        with patch.dict(os.environ, {'THUNDER_APOLLO_LI_OFFICIAL_DB': str(missing)}):
            with self.assertRaisesRegex(RepositoryConflict, 'unavailable'):
                Repository(missing)
            self.assertFalse(missing.exists())
            missing.touch()
            configured = Repository(missing)
            self.assertTrue(configured.apollo_linkedin_store_ready())
            with self.assertRaises(RepositoryConflict):
                self.service.apollo_linkedin_store_ready()
        with patch.dict(os.environ, {'THUNDER_APOLLO_LI_OFFICIAL_DB': 'relative.db'}):
            with self.assertRaisesRegex(RepositoryConflict, 'configuration invalid'):
                self.service.apollo_linkedin_store_ready()
            # Invalid T3 configuration disables only T3; unrelated repositories still open.
            unrelated = Repository(Path(self.tmp.name) / 'unrelated.db')
            with self.assertRaisesRegex(RepositoryConflict, 'configuration invalid'):
                unrelated.apollo_linkedin_store_ready()

    def test_wrong_existing_database_refuses(self):
        other = Path(self.tmp.name) / 'other.db'
        other.touch()
        other_repo = Repository(other)
        import sqlite3
        with sqlite3.connect(other) as con:
            self.assertIsNone(con.execute("SELECT name FROM sqlite_master WHERE name='apollo_linkedin_reservations'").fetchone())
        self.service.repository.path = str(other)
        with self.assertRaises(RepositoryConflict):
            self.service.apollo_linkedin_store_ready()

    def test_missing_or_unmigrated_store_refuses(self):
        self.service.repository.path = str(Path(self.tmp.name) / 'missing.db')
        with self.assertRaises(RepositoryConflict):
            self.service.apollo_linkedin_store_ready()
        self.assertFalse(Path(self.service.repository.path).exists())
        self.service.repository.path = ':memory:'
        with self.assertRaises(RepositoryConflict):
            self.service.apollo_linkedin_store_ready()


if __name__ == '__main__':
    unittest.main()
