import os
import sqlite3
import tempfile
import unittest
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from thunder_os.apollo_linkedin_live_executor import MessageCoordinator, runtime_preflight, build_runtime
from test_thunder_os_apollo_linkedin_message_coordinator import Repo, Authority, Apollo, Message, Coverage, Safety, Completion


class RuntimeFactoryTests(unittest.TestCase):
    def test_wrong_db_is_not_migrated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'other.sqlite'
            official = Path(directory) / 'official.sqlite'
            for file in (path, official):
                con = sqlite3.connect(file)
                con.execute('CREATE TABLE unrelated(value TEXT)')
                con.close()
            before = path.read_bytes()
            with patch.dict(os.environ, {'PAINEL_DB': str(path),
                                        'THUNDER_APOLLO_LI_OFFICIAL_DB': str(official)}, clear=False):
                with self.assertRaises(ValueError):
                    build_runtime('task', 'a' * 32)
            self.assertEqual(before, path.read_bytes())

    def test_dry_run_does_not_create_missing_db(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'missing.sqlite'
            with patch.dict(os.environ, {'PAINEL_DB': str(path)}, clear=False):
                result = runtime_preflight('task', 'authorization')
            self.assertEqual(result['checks']['official_store'], 'unavailable')
            self.assertFalse(result['executable'])
            self.assertFalse(path.exists())

    def test_dry_run_reports_pause_from_existing_database_without_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db.sqlite'
            with sqlite3.connect(path) as con:
                con.execute('CREATE TABLE thunder_state(kind TEXT,entity_id TEXT,payload TEXT)')
                con.execute('INSERT INTO thunder_state VALUES (?,?,?)', ('control', 'global', '{"paused":true}'))
            original = path.read_bytes()
            with patch.dict(os.environ, {'PAINEL_DB': str(path)}, clear=False):
                result = runtime_preflight('task', 'authorization')
            self.assertEqual(result['checks']['global_pause'], 'paused')
            self.assertEqual(path.read_bytes(), original)

    def test_contact_without_email_key_reaches_message_flow(self):
        class TaskWithoutEmail(Apollo):
            def get(self, task_id):
                result = super().get(task_id)
                del result['contact']['email']
                return result
        repo = Repo()
        message = Message(repo)
        coordinator = MessageCoordinator(repository=repo, authority=Authority(), apollo=TaskWithoutEmail(),
            message=message, coverage=Coverage(), safety=Safety(), completion=Completion())
        with patch.dict(os.environ, {'THUNDER_APOLLO_LI_EXECUTION_ENABLED': 'true'}):
            result = coordinator.run('task', 'authorization', dry_run=False)
        self.assertEqual(result['state'], 'confirmed')
        self.assertEqual(message.sent, 1)

    def test_preflight_observes_port_gates_without_reservation_or_dispatch(self):
        class ReaderCoverage(Coverage):
            def bind_plan(self, plan): self.plan = plan
        repo = Repo()
        repo.auth['actor'] = 'test-user'
        repo.auth['issued_at'] = datetime.now(timezone.utc).isoformat()
        repo.auth['expires_at'] = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
        repo.auth.update(id='a' * 32, version=1, updated_at=repo.auth['issued_at'])
        message = Message(repo)
        subject = MessageCoordinator(repository=repo, authority=Authority(), apollo=Apollo(),
            message=message, coverage=ReaderCoverage(), safety=Safety(), completion=Completion())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db.sqlite'
            with sqlite3.connect(path) as con:
                con.execute('CREATE TABLE thunder_state(kind TEXT,entity_id TEXT,payload TEXT)')
                con.execute('INSERT INTO thunder_state VALUES (?,?,?)', ('control', 'global', '{"paused":false}'))
                con.execute('INSERT INTO thunder_state VALUES (?,?,?)', ('apollo_linkedin_canary', 'a' * 32, json.dumps(repo.auth)))
                expired = {**repo.auth, 'id': 'b' * 32,
                           'issued_at': (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat(),
                           'expires_at': (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()}
                con.execute('INSERT INTO thunder_state VALUES (?,?,?)', ('apollo_linkedin_canary', 'b' * 32, json.dumps(expired)))
            before = path.read_bytes()
            with patch.dict(os.environ, {'PAINEL_DB': str(path)}, clear=False), \
                    patch('thunder_os.apollo_linkedin_live_executor.build_runtime', return_value=subject), \
                    patch('thunder_os.apollo_linkedin_live_executor.hermes_unit_inactive', return_value=True):
                result = runtime_preflight('task')
            self.assertTrue(result['checks']['executable_after_enable'])
            self.assertEqual(result['authorization_id'], 'a' * 32)
            self.assertEqual(message.sent, 0)
            self.assertIsNone(repo.row)
            self.assertEqual(before, path.read_bytes())
            with sqlite3.connect(path) as con:
                con.execute('INSERT INTO thunder_state VALUES (?,?,?)', ('apollo_linkedin_canary', 'c' * 32, json.dumps(repo.auth)))
            with patch.dict(os.environ, {'PAINEL_DB': str(path)}, clear=False):
                ambiguous = runtime_preflight('task')
            self.assertNotIn('authorization_id', ambiguous)
            repo.auth['actor'] = ''
            with patch.dict(os.environ, {'PAINEL_DB': str(path)}, clear=False), \
                    patch('thunder_os.apollo_linkedin_live_executor.build_runtime', return_value=subject), \
                    patch('thunder_os.apollo_linkedin_live_executor.hermes_unit_inactive', return_value=True):
                invalid = runtime_preflight('task', 'a' * 32)
            self.assertFalse(invalid['checks'].get('executable_after_enable', False))


if __name__ == '__main__':
    unittest.main()
