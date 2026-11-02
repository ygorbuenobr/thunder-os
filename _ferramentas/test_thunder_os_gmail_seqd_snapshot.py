"""Offline checks for the opt-in Gmail snapshot bridge."""
import json
import os
import subprocess
from contextlib import closing
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from thunder_os.gmail_seqd_snapshot import main, run_snapshot
from thunder_os.inbox_common import InboxError
from thunder_os.seqd_client import MAILBOX, SEQDClient, SEQDError
from thunder_os.db import Repository
from thunder_os.models import payload_hash
from thunder_os.service import ThunderService


class IntegratedSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Repository(Path(self.tmp.name) / 'fixture.db')
        self.service = ThunderService(self.repo)
        self.mode = 'ok'
        self.calls = []
        for target in ('socket.create_connection', 'socket.socket.connect', 'socket.getaddrinfo'):
            active = patch(target, side_effect=AssertionError('network forbidden'))
            active.start()
            self.addCleanup(active.stop)
        def request(method, path, body, headers, timeout):
            self.calls.append(path)
            fixture = Path(__file__).with_name('seqd_bridge') / 'snapshot-fixture.cjs'
            env = {'PATH': os.environ.get('PATH', ''), 'SNAPSHOT_FIXTURE_MODE': self.mode}
            result = subprocess.run(['node', str(fixture), path], env=env, capture_output=True, timeout=5)
            if result.returncode:
                raise OSError('synthetic_bridge_failure')
            return 200, result.stdout
        self.client = SEQDClient(lambda: 'a' * 64, request=request)

    def run_one(self):
        return run_snapshot(self.service, enabled=True, client=self.client, max_pages=1)

    def test_paginate_resume_and_repeat_without_duplicate(self):
        self.assertFalse(self.run_one()['complete'])
        self.assertTrue(self.run_one()['complete'])
        self.assertEqual(len(self.repo.list_state('gmail_thread')), 2)
        self.assertEqual(len(self.repo.list_state('gmail_message')), 2)
        self.assertFalse(self.run_one()['complete'])
        self.assertTrue(self.run_one()['complete'])
        self.assertEqual(len(self.repo.list_state('gmail_message')), 2)

    def test_wrong_account_does_not_write(self):
        self.mode = 'wrong-account'
        with self.assertRaises(InboxError):
            self.run_one()
        self.assertEqual(self.repo.list_state('gmail_thread'), [])
        self.assertEqual(self.repo.list_state('gmail_message'), [])

    def test_thread_error_does_not_write(self):
        self.mode = 'thread-error'
        def state():
            with closing(sqlite3.connect(self.repo.path)) as con:
                tables = ('thunder_schema', 'thunder_work_items', 'thunder_events',
                          'thunder_outbox', 'thunder_state', 'thunder_effect_jobs')
                return {name: con.execute('SELECT * FROM ' + name + ' ORDER BY rowid').fetchall()
                        for name in tables}
        before = state()
        with self.assertRaises(InboxError):
            self.run_one()
        threads = [path for path in self.calls if '/v1/snapshot/threads/' in path]
        self.assertEqual(threads, ['/v1/snapshot/threads/abc123', '/v1/snapshot/threads/def456'])
        self.assertEqual(state(), before)

    def test_bad_cursor_retains_checkpoint(self):
        self.mode = 'bad-cursor'
        self.assertFalse(self.run_one()['complete'])
        scope = payload_hash({'provider': 'gmail', 'mailbox': MAILBOX})
        before = self.repo.get_state('gmail_sync', scope)
        with self.assertRaises(InboxError):
            self.run_one()
        self.assertEqual(self.repo.get_state('gmail_sync', scope), before)


class SnapshotPortTests(unittest.TestCase):
    def test_entrypoint_rejects_invalid_bounds_and_foreign_db_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'foreign.db'
            with closing(sqlite3.connect(path)) as con, con: con.execute('CREATE TABLE unrelated(id INTEGER)')
            before=path.read_bytes()
            env={'THUNDER_OS_GMAIL_SNAPSHOT_ENABLED':'true'}
            for args in (['--db',str(path),'--max-pages','0'],['--db',str(path)]):
                with self.assertRaises(InboxError): main(args,environ=env)
                self.assertEqual(path.read_bytes(),before)
                with closing(sqlite3.connect(path)) as con:
                    self.assertEqual(con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [('unrelated',)])

    def test_entrypoint_defaults_off_before_database_or_service(self):
        factory=Mock(side_effect=AssertionError('must not run'))
        with self.assertRaisesRegex(InboxError,'gmail_snapshot_disabled'):
            main(['--db','/nonexistent'],environ={},service_factory=factory)
        factory.assert_not_called()
    def test_disabled_and_missing_credential_do_not_call_network(self):
        service=Mock()
        request=Mock(side_effect=AssertionError('network forbidden'))
        client=SEQDClient(lambda: '',request=request)
        with self.assertRaisesRegex(InboxError,'gmail_snapshot_disabled'):
            run_snapshot(service,client=client)
        with self.assertRaisesRegex(InboxError,'gmail_read_failed'):
            run_snapshot(service,enabled=True,client=client)
        request.assert_not_called()

    def test_cursor_malformed_and_wrong_mailbox_fail_closed(self):
        request=Mock(return_value=(200,json.dumps({'mailbox':'wrong@example.invalid','status':'ready'}).encode()))
        client=SEQDClient(lambda:'a'*64,request=request)
        with self.assertRaisesRegex(SEQDError,'invalid_cursor'):
            client.snapshot_page('\x00')
        request.assert_not_called()
        with self.assertRaisesRegex(InboxError,'gmail_read_failed'):
            run_snapshot(Mock(),enabled=True,client=client)
        self.assertEqual(request.call_count,1)

    def test_snapshot_requires_explicit_inbox_coverage_and_valid_history(self):
        value={'mailbox':MAILBOX,'status':'ok','receipt_eligible':False,'messages':[],
            'nextPageToken':None,'coverage':{'scope':'inbox','window_days':7,'listing_complete':True}}
        request=Mock(side_effect=lambda *args:(200,json.dumps(value).encode()))
        client=SEQDClient(lambda:'a'*64,request=request)
        with self.assertRaisesRegex(SEQDError,'invalid_snapshot_page'):client.snapshot_page()
        value.clear();value.update(mailbox=MAILBOX,status='ok',receipt_eligible=False,
            thread={'id':'abc123','historyId':'not-a-number','messages':[]})
        with self.assertRaisesRegex(SEQDError,'invalid_snapshot_thread'):client.snapshot_thread('abc123')
