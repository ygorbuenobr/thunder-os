import importlib.util
import io
import json
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from thunder_os.apollo_linkedin_attention import EXCLUDED_TASK
from thunder_os.apollo_linkedin_live_executor import execution_status, main

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('prepare_canary', _ROOT / 'scripts/prepare_apollo_linkedin_canary.py')
_prepare = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_prepare)


class SafeCompositionTest(unittest.TestCase):
    def test_no_flag_or_environment_enables_dispatch(self):
        with patch.dict('os.environ', {'THUNDER_APOLLO_LI_EXECUTION_ENABLED': '1'}):
            with patch('sys.stdout', new_callable=io.StringIO) as out:
                self.assertEqual(main(['--task', 'task1', '--authorization', 'auth1', '--execute']), 2)
                self.assertEqual(json.loads(out.getvalue())['reason'], 'live_composition_unavailable')

    def test_dry_run_default_and_protected_fixture(self):
        with patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(main(['--task', EXCLUDED_TASK, '--authorization', 'auth1']), 2)
            response = json.loads(out.getvalue())
        self.assertEqual(response['reason'], 'fixture_protegida')
        self.assertEqual(response['mode'], 'dry_run')
        self.assertFalse(response['executable'])

    def test_invalid_values_are_blocked(self):
        self.assertEqual(execution_status('task;id', 'auth1')['reason'], 'configuration_invalid')
        with self.assertRaises(ValueError):
            _prepare.prepare('task1', 'a;whoami', 'auth1')

    def test_database_reader_never_creates_or_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db.sqlite'
            with self.assertRaises(ValueError):
                _prepare.ReadOnlyStates(path)
            self.assertFalse(path.exists())
            with sqlite3.connect(path) as con:
                con.execute('CREATE TABLE thunder_state(kind TEXT, entity_id TEXT, version INTEGER, payload TEXT, updated_at TEXT)')
                con.execute('INSERT INTO thunder_state VALUES (?, ?, ?, ?, ?)', ('control', 'global', 1, '{"paused":false}', 'now'))
            reader = _prepare.ReadOnlyStates(path)
            try:
                self.assertFalse(reader.get_state('control', 'global')['paused'])
                with self.assertRaises(sqlite3.OperationalError):
                    reader.connection.execute('DELETE FROM thunder_state')
            finally:
                reader.close()
            with sqlite3.connect(path) as con:
                self.assertEqual(con.execute('SELECT count(*) FROM thunder_state').fetchone()[0], 1)

    def test_real_reader_interface_observes_bound_task_without_authorizing_it(self):
        from thunder_os.apollo_linkedin_reader import ApolloLinkedInReader
        calls = []
        def transport(method, path, body, headers, timeout):
            calls.append((method, path))
            if path.endswith('api_profile'):
                value = {'user': {'id': 'actor', 'team_id': 'team'}}
            else:
                value = {'task': {'id': 'task1', 'type': 'linkedin_step_connect',
                    'status': 'scheduled', 'emailer_campaign_id': 'sequence', 'user_id': 'owner',
                    'contact': {'linkedin_url': 'https://www.linkedin.com/in/person'},
                    'linkedin_emailer_template': {'body_text': 'Olá'}}}
            return 200, json.dumps(value).encode()
        reader = ApolloLinkedInReader(lambda: 'fake', team_id='team', api_actor_id='actor',
                                      owner_ids=['owner'], request=transport)
        binding = {'team_id': 'team', 'owner_ids': ['owner'], 'sequence_id': 'sequence',
                   'account_id': 'account', 'public_id': 'sender'}
        class FakeAuthority:
            def __init__(self, store): pass
            def require(self, *args): return {'binding': binding}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db.sqlite'
            sqlite3.connect(path).close()
            with patch.object(_prepare, 'CampaignApprovalAuthority', FakeAuthority):
                result = _prepare.inspect_observations(_prepare.prepare('task1', 'person', 'auth1'),
                    db=str(path), campaign='campaign', cadence='cadence', version='v1', reader=reader)
        self.assertEqual(result['text'], 'Olá')
        self.assertEqual(result['checks']['text'], 'observed_not_approved')
        self.assertEqual(result['checks']['quota'], 'unverified')
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual([x[0] for x in calls], ['GET', 'GET'])

    def test_preparation_never_claims_unknowns_are_verified(self):
        result = _prepare.prepare('task1', 'recipient', 'auth1')
        self.assertEqual(set(result['checks'].values()), {'unverified'})
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('--task task1 --authorization auth1 --execute', result['command'])
        self.assertFalse(result['execution']['executable'])


if __name__ == '__main__':
    unittest.main()
