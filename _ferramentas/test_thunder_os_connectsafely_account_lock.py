import multiprocessing
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from thunder_os.connectsafely_console import ConnectSafelyConsole, ConnectSafelyError
from thunder_os.db import Repository, RepositoryConflict


def _hold_lock(path, ready, release):
    os.environ['THUNDER_APOLLO_LI_OFFICIAL_DB'] = path
    repo = Repository(path)
    with repo.apollo_linkedin_account_lock('account-1'):
        ready.set()
        release.wait(10)


class ManualAccountLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = str(Path(self.tmp.name).resolve() / 'official.db')
        Path(path).touch()
        self.env = patch.dict(os.environ, {'THUNDER_APOLLO_LI_OFFICIAL_DB': path})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.path = path
        self.repo = Repository(path)
        self.console = ConnectSafelyConsole(self.repo, None, None,
                                            account_id='account-1', account_public_id='sender')

    def test_lock_is_held_through_manual_execution_and_released(self):
        def execute(*args, **kwargs):
            with self.assertRaises(RepositoryConflict):
                with self.repo.apollo_linkedin_account_lock('account-1'):
                    pass
            return {'status': 'sent'}
        with patch.object(self.console, '_execute', side_effect=execute):
            self.assertEqual({'status': 'sent'}, self.console.execute({'execution_confirmation': {}}, actor='ygor'))
        with self.repo.apollo_linkedin_account_lock('account-1'):
            pass

    def test_exception_does_not_leave_account_locked(self):
        with patch.object(self.console, '_execute', side_effect=RuntimeError('synthetic')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic'):
                self.console.execute({'execution_confirmation': {}}, actor='ygor')
        with self.repo.apollo_linkedin_account_lock('account-1'):
            pass

    def test_other_process_holding_account_prevents_any_manual_execution(self):
        ctx = multiprocessing.get_context('spawn')
        ready, release = ctx.Event(), ctx.Event()
        process = ctx.Process(target=_hold_lock, args=(self.path, ready, release))
        process.start()
        try:
            self.assertTrue(ready.wait(10))
            with patch.object(self.console, '_execute') as execute:
                with self.assertRaisesRegex(ConnectSafelyError, 'account_lock_unavailable'):
                    self.console.execute({'execution_confirmation': {}}, actor='ygor')
                execute.assert_not_called()
        finally:
            release.set()
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join()
        self.assertEqual(0, process.exitcode)

    def test_invalid_official_store_fails_before_execution(self):
        with patch.dict(os.environ, {'THUNDER_APOLLO_LI_OFFICIAL_DB': 'relative'}):
            with patch.object(self.console, '_execute') as execute:
                with self.assertRaisesRegex(ConnectSafelyError, 'account_lock_unavailable'):
                    self.console.execute({'execution_confirmation': {}}, actor='ygor')
                execute.assert_not_called()

    def test_native_with_database_fence_fails_fast_if_other_process_holds_lock(self):
        ctx = multiprocessing.get_context('spawn')
        ready, release = ctx.Event(), ctx.Event()
        process = ctx.Process(target=_hold_lock, args=(self.path, ready, release))
        process.start()
        job = {'id': 'job', 'lease_owner': 'owner', 'payload': {'synthetic': True}}
        try:
            self.assertTrue(ready.wait(10))
            with self.repo.transaction():
                self.repo._local.dispatch_fence = ('job', 'owner')
                try:
                    with patch.object(self.repo, 'effect', return_value=job), patch.object(self.console, '_execute') as execute:
                        started = time.monotonic()
                        with self.assertRaisesRegex(ConnectSafelyError, 'account_lock_unavailable'):
                            self.console.execute_native({}, job, before_dispatch=lambda: True)
                        self.assertLess(time.monotonic()-started, 2)
                        execute.assert_not_called()
                finally:
                    self.repo._local.dispatch_fence = None
        finally:
            release.set()
            process.join(10)
            if process.is_alive():
                process.terminate(); process.join()
        self.assertEqual(0, process.exitcode)

    def test_native_and_manual_use_same_account_lock(self):
        job = {'id': 'job', 'lease_owner': 'owner', 'payload': {}}
        with self.repo.apollo_linkedin_account_lock('account-1'):
            self.repo._local.dispatch_fence = ('job', 'owner')
            try:
                with patch.object(self.repo, 'effect', return_value=job), patch.object(self.console, '_execute') as execute:
                    with self.assertRaisesRegex(ConnectSafelyError, 'account_lock_unavailable'):
                        self.console.execute_native({}, job, before_dispatch=lambda: True)
                    execute.assert_not_called()
            finally:
                self.repo._local.dispatch_fence = None


if __name__ == '__main__':
    unittest.main()
