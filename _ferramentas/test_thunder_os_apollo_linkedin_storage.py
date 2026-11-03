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
