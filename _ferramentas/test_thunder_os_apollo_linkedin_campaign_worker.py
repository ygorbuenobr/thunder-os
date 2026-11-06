import io
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import nullcontext
from unittest.mock import patch

from thunder_os.apollo_linkedin_attention import EXCLUDED_TASK
from thunder_os.apollo_linkedin_campaign_worker import CampaignWorker, main
from thunder_os.apollo_linkedin_reader import ApolloLinkedInReader


def contend_cursor(path, barrier, queue):
    """Two processes read the same cursor revision and race to advance it."""
    from thunder_os.db import Repository
    os.environ['THUNDER_APOLLO_LI_OFFICIAL_DB'] = path
    os.environ['THUNDER_APOLLO_LI_WORKER_ENABLED'] = 'true'
    os.environ['THUNDER_APOLLO_LI_EXECUTION_ENABLED'] = 'true'
    repo = Repository(path)
    class Authority:
        def require(self, *args):
            return {'binding': {'team_id': 'team', 'company_id': 'company',
                                'owner_ids': ['owner'], 'sequence_id': 'sequence'}}
    class Reader:
        team_id = 'team'
        owner_ids = frozenset(['owner'])
        def attention_budget(self, **kwargs): return nullcontext()
        def list_open_tasks(self):
            return {'coverage': 'complete', 'reason': None, 'tasks': [
                {'id': 'first', 'type': 'linkedin_step_message', 'status': 'scheduled',
                 'user_id': 'owner', 'emailer_campaign_id': 'sequence'}]}
    class Coordinator:
        authority = Authority()
        apollo = Reader()
        def __init__(self): self.repo = repo
        def _gate(self): return True
        def run_campaign(self, *args, **kwargs):
            barrier.wait(timeout=10)
            return {'state': 'blocked', 'reason': 'coverage_unavailable'}
    try:
        result = CampaignWorker(Coordinator(), campaign_id='campaign', approval_id='approval',
                                campaign_version='v1').run_once(execute=True)
        queue.put((result['state'], result['reason']))
    except Exception:
        queue.put(('unexpected_error', None))


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.outcomes = []
        self.reservations = {}
        self.states = {}
        self.rows = [self.task('task1'), self.task('other', sequence='other')]
        self.total = 2

        def request(method, path, body, headers, timeout):
            self.calls.append((method, path))
            if path.endswith('api_profile'):
                return 200, json.dumps({'user': {'id': 'actor', 'team_id': 'team'}}).encode()
            if path.endswith('/search'):
                return 200, json.dumps({'tasks': self.rows, 'pagination': {'page': 1,
                    'per_page': 100, 'total_entries': self.total,
                    'total_pages': (self.total + 99) // 100}}).encode()
            raise AssertionError('unexpected HTTP path')

        reader = ApolloLinkedInReader(lambda: 'fake', team_id='team', api_actor_id='actor',
            owner_ids=['owner'], request=request, max_pages=3)
        outer = self

        class Authority:
            def require(self, campaign, approval, version):
                if (campaign, approval, version) != ('campaign', 'approval', 'v1'):
                    raise ValueError('approval_invalid')
                return {'binding': {'team_id': 'team', 'company_id': 'company',
                                    'owner_ids': ['owner'], 'sequence_id': 'sequence', 'account_id': 'account'}}

        class Repo:
            def list_state(self, kind):
                return [value for (state_kind, _), value in outer.states.items() if state_kind == kind]
            def get_apollo_linkedin_reservation(self, company, task, action):
                return outer.reservations.get((company, task, action))
            def get_state(self, kind, ident):
                return outer.states.get((kind, ident))
            def put_states_atomic(self, updates):
                output = []
                for kind, ident, payload, expected in updates:
                    previous = outer.states.get((kind, ident))
                    version = previous['version'] if previous else 0
                    if version != expected:
                        raise ValueError('stale state')
                    row = {**payload, 'version': version + 1}
                    outer.states[(kind, ident)] = row
                    output.append(row)
                return output

        class Coordinator:
            authority = Authority()
            apollo = reader
            repo = Repo()
            def _gate(self): return True
            def run_campaign(self, task, campaign, approval, version, *, dry_run):
                outer.outcomes.append((task, campaign, approval, version, dry_run))
                return {'state': 'confirmed', 'reason': None}

        self.coordinator = Coordinator()
        self.worker = CampaignWorker(self.coordinator, campaign_id='campaign',
                                     approval_id='approval', campaign_version='v1')

    @staticmethod
    def task(ident, *, sequence='sequence', action='linkedin_step_message', owner='owner'):
        return {'id': ident, 'type': action, 'status': 'scheduled', 'user_id': owner,
                'emailer_campaign_id': sequence}

    def test_default_scans_only_and_never_dispatches(self):
        with patch.dict(os.environ, {}, clear=True):
            result = self.worker.run_once()
        self.assertEqual(result['coverage'], 'complete')
        self.assertEqual(result['items'], [{'task_id': 'task1', 'state': 'observed', 'reason': 'not_dispatched'}])
        self.assertEqual(self.outcomes, [])
        self.assertEqual(self.states, {})
        self.assertEqual(self.calls, [('GET', '/api/v1/users/api_profile'), ('POST', '/api/v1/tasks/search')])

    def test_two_approved_sequences_are_observed_and_third_is_excluded(self):
        self.rows = [self.task('a', sequence='sequence'), self.task('b', sequence='sequence-two'),
                     self.task('c', sequence='outside')]
        self.total = len(self.rows)
        self.coordinator.authority.require = lambda *args: {'binding': {
            'team_id': 'team', 'company_id': 'company', 'account_id': 'account',
            'owner_ids': ['owner'], 'sequence_ids': ['sequence', 'sequence-two']}}
        result = self.worker.run_once()
        self.assertEqual(result['eligible_observed'], 2)
        self.assertEqual(result['items'], [{'task_id': 'a', 'state': 'observed', 'reason': 'not_dispatched'}])
        self.assertEqual(self.outcomes, [])
        self.rows = [self.task('b', sequence='sequence-two')]
        self.total = 1
        with patch.dict(os.environ, {'THUNDER_APOLLO_LI_WORKER_ENABLED': 'true',
                                    'THUNDER_APOLLO_LI_EXECUTION_ENABLED': 'true'}, clear=True):
            self.assertEqual(self.worker.run_once(execute=True)['items'][0]['state'], 'confirmed')
        self.assertEqual(self.outcomes, [('b', 'campaign', 'approval', 'v1', False)])

    def test_invalid_sequence_list_fails_closed_before_provider_read(self):
        self.coordinator.authority.require = lambda *args: {'binding': {
            'team_id': 'team', 'company_id': 'company', 'account_id': 'account',
            'owner_ids': ['owner'], 'sequence_ids': ['sequence', 'sequence']}}
        result = self.worker.run_once()
        self.assertEqual(result['reason'], 'reader_binding_mismatch')
        self.assertEqual(self.calls, [])

    def test_attempted_skip_remains_visible_when_task_is_no_longer_open(self):
        self.rows = []
        self.total = 0
        self.states[('apollo_linkedin_skip', 'claim')] = {'state': 'attempted', 'company': 'company',
            'campaign_id': 'campaign', 'approval_id': 'approval', 'campaign_version': 'v1',
            'account_id': 'account', 'task_id': 'already-skipped'}
        result = self.worker.run_once()
        self.assertIn({'task_id': 'already-skipped', 'state': 'attention',
                       'reason': 'skip_reconcile_pending'}, result['items'])
        self.assertEqual([], self.outcomes)
        self.assertTrue(all(method == 'GET' or path.endswith('/search') for method, path in self.calls))

    def test_explicit_activation_calls_campaign_coordinator_once(self):
        with patch.dict(os.environ, {'THUNDER_APOLLO_LI_WORKER_ENABLED': 'true',
                                    'THUNDER_APOLLO_LI_EXECUTION_ENABLED': 'true'}, clear=True):
            result = self.worker.run_once(execute=True)
        self.assertEqual(self.outcomes, [('task1', 'campaign', 'approval', 'v1', False)])
        self.assertEqual(result['items'][0]['state'], 'confirmed')

    def test_activation_and_global_gate_prevent_dispatch(self):
        with patch.dict(os.environ, {'THUNDER_APOLLO_LI_EXECUTION_ENABLED': 'true'}, clear=True):
            self.assertEqual(self.worker.run_once(execute=True)['reason'], 'worker_disabled')
        self.coordinator._gate = lambda: False
        with patch.dict(os.environ, {'THUNDER_APOLLO_LI_WORKER_ENABLED': 'true',
                                    'THUNDER_APOLLO_LI_EXECUTION_ENABLED': 'true'}, clear=True):
            self.assertEqual(self.worker.run_once(execute=True)['reason'], 'execution_disabled')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.outcomes, [])

    def test_partial_coverage_never_dispatches(self):
        self.rows = [self.task(str(n)) for n in range(100)]
        self.total = 101
        self.worker.max_pages = 1
        with patch.dict(os.environ, {'THUNDER_APOLLO_LI_WORKER_ENABLED': 'true',
                                    'THUNDER_APOLLO_LI_EXECUTION_ENABLED': 'true'}, clear=True):
            result = self.worker.run_once(execute=True)
        self.assertEqual(result['coverage'], 'partial')
        self.assertEqual(result['reason'], 'page_budget_exhausted')
        self.assertEqual(result['items'][0]['reason'], 'coverage_incomplete')
        self.assertEqual(self.outcomes, [])

    def test_protected_fixture_and_unsupported_action_are_never_dispatched(self):
        self.rows = [self.task(EXCLUDED_TASK), self.task('other', action='linkedin_step_other')]
        self.coordinator.apollo.list_open_tasks = lambda: {'coverage': 'complete', 'reason': None, 'tasks': self.rows}
        with patch.dict(os.environ, {'THUNDER_APOLLO_LI_WORKER_ENABLED': 'true',
                                    'THUNDER_APOLLO_LI_EXECUTION_ENABLED': 'true'}, clear=True):
            result = self.worker.run_once(execute=True)
        self.assertEqual([row['reason'] for row in result['items']], ['fixture_protegida', 'action_not_supported'])
        self.assertEqual(self.outcomes, [])

    def test_invitation_task_is_selected_for_the_coordinator(self):
        self.rows = [self.task('invite', action='linkedin_step_connect')]
        self.total = 1
        with patch.dict(os.environ, {'THUNDER_APOLLO_LI_WORKER_ENABLED': 'true',
                                    'THUNDER_APOLLO_LI_EXECUTION_ENABLED': 'true'}, clear=True):
            result = self.worker.run_once(execute=True)
        self.assertEqual(len(self.outcomes), 1)
        self.assertEqual(result['items'][0]['task_id'], 'invite')

    def test_approval_must_be_verified_before_scan(self):
        self.coordinator.authority.require = lambda *args: (_ for _ in ()).throw(ValueError('invalid'))
        self.assertEqual(self.worker.run_once()['reason'], 'worker_unavailable')
        self.assertEqual(self.calls, [])

    def test_duplicate_discovery_blocks_before_any_dispatch(self):
        self.coordinator.apollo.list_open_tasks = lambda: {'coverage': 'complete', 'reason': None,
            'tasks': [self.task('task1'), self.task('task1')]}
        with patch.dict(os.environ, {'THUNDER_APOLLO_LI_WORKER_ENABLED': 'true',
                                    'THUNDER_APOLLO_LI_EXECUTION_ENABLED': 'true'}, clear=True):
            result = self.worker.run_once(execute=True)
        self.assertEqual(result['reason'], 'duplicate_task')
        self.assertEqual(result['coverage'], 'unknown')
        self.assertEqual(self.outcomes, [])

    def test_kill_switch_is_checked_after_scan_before_dispatch(self):
        read = self.coordinator.apollo.list_open_tasks
        def scan_then_pause():
            result = read()
            self.coordinator._gate = lambda: False
            return result
        self.coordinator.apollo.list_open_tasks = scan_then_pause
        with patch.dict(os.environ, {'THUNDER_APOLLO_LI_WORKER_ENABLED': 'true',
                                    'THUNDER_APOLLO_LI_EXECUTION_ENABLED': 'true'}, clear=True):
            result = self.worker.run_once(execute=True)
        self.assertEqual(result['reason'], 'execution_disabled')
        self.assertEqual(self.outcomes, [])

    def test_unknown_coverage_never_dispatches_observed_rows(self):
        self.coordinator.apollo.list_open_tasks = lambda: {'coverage': 'unknown',
            'reason': 'pagination_incomplete', 'tasks': [self.task('task1')]}
        with patch.dict(os.environ, {'THUNDER_APOLLO_LI_WORKER_ENABLED': 'true',
                                    'THUNDER_APOLLO_LI_EXECUTION_ENABLED': 'true'}, clear=True):
            result = self.worker.run_once(execute=True)
        self.assertEqual(result['reason'], 'pagination_incomplete')
        self.assertEqual(result['items'][0]['reason'], 'coverage_incomplete')
        self.assertEqual(self.outcomes, [])

    def test_existing_reservation_cannot_starve_next_unreserved_task(self):
        self.rows = [self.task('old'), self.task('next')]
        self.reservations[('company', 'old', 'linkedin_step_message')] = {'state': 'uncertain'}
        with patch.dict(os.environ, {'THUNDER_APOLLO_LI_WORKER_ENABLED': 'true',
                                    'THUNDER_APOLLO_LI_EXECUTION_ENABLED': 'true'}, clear=True):
            result = self.worker.run_once(execute=True)
        self.assertEqual(result['items'][0]['reason'], 'reservation_exists')
        self.assertEqual(self.outcomes, [('next', 'campaign', 'approval', 'v1', False)])

    def test_blocked_first_advances_to_next_on_later_cycle(self):
        self.rows = [self.task('first'), self.task('second')]
        def run(task, campaign, approval, version, *, dry_run):
            self.outcomes.append((task, campaign, approval, version, dry_run))
            return {'state': 'blocked', 'reason': 'coverage_unavailable'} if task == 'first' else {'state': 'confirmed'}
        self.coordinator.run_campaign = run
        with patch.dict(os.environ, {'THUNDER_APOLLO_LI_WORKER_ENABLED': 'true',
                                    'THUNDER_APOLLO_LI_EXECUTION_ENABLED': 'true'}, clear=True):
            first = self.worker.run_once(execute=True)
            # New instance simulates a later process, retaining only the repository state.
            second_worker = CampaignWorker(self.coordinator, campaign_id='campaign', approval_id='approval', campaign_version='v1')
            second = second_worker.run_once(execute=True)
        self.assertEqual([row[0] for row in self.outcomes], ['first', 'second'])
        self.assertEqual(first['items'][0]['reason'], 'coverage_unavailable')
        self.assertEqual(second['items'][0]['state'], 'confirmed')
        self.assertEqual(len(self.states), 1)

    def test_cursor_write_failure_is_visible_and_never_retries_in_cycle(self):
        self.coordinator.repo.put_states_atomic = lambda updates: (_ for _ in ()).throw(ValueError('write failed'))
        with patch.dict(os.environ, {'THUNDER_APOLLO_LI_WORKER_ENABLED': 'true',
                                    'THUNDER_APOLLO_LI_EXECUTION_ENABLED': 'true'}, clear=True):
            result = self.worker.run_once(execute=True)
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['reason'], 'cursor_persistence_unavailable')
        self.assertEqual(len(self.outcomes), 1)

    def test_cli_scan_budget_is_passed_to_reader(self):
        with patch.dict(os.environ, {}, clear=True), patch(
                'thunder_os.apollo_linkedin_campaign_worker.build_campaign_runtime', return_value=self.coordinator):
            with patch.object(self.coordinator.apollo, 'attention_budget', wraps=self.coordinator.apollo.attention_budget) as budget:
                with patch('sys.stdout', new_callable=io.StringIO):
                    self.assertEqual(main(['--campaign', 'campaign', '--approval', 'approval',
                        '--campaign-version', 'v1', '--max-pages', '2', '--budget-seconds', '7']), 0)
        budget.assert_called_once_with(seconds=7, max_pages=2)
        self.assertEqual(self.states, {})

    def test_cycle_cannot_be_configured_to_attempt_multiple_actions(self):
        with self.assertRaises(ValueError):
            CampaignWorker(self.coordinator, campaign_id='campaign', approval_id='approval',
                           campaign_version='v1', max_tasks=2)

    def test_company_and_sequence_allow_registry_dotted_identifiers(self):
        self.rows = [self.task('task1', sequence='sequence.sales')]
        self.total = 1
        self.coordinator.authority.require = lambda *args: {'binding': {
            'team_id': 'team', 'company_id': 'company.example',
            'owner_ids': ['owner'], 'sequence_id': 'sequence.sales'}}
        with patch.dict(os.environ, {'THUNDER_APOLLO_LI_WORKER_ENABLED': 'true',
                                    'THUNDER_APOLLO_LI_EXECUTION_ENABLED': 'true'}, clear=True):
            result = self.worker.run_once(execute=True)
        self.assertEqual(result['items'][0]['state'], 'confirmed')
        self.assertEqual(len(self.states), 1)

    def test_two_processes_cannot_both_advance_same_cursor_version(self):
        from thunder_os.db import Repository
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'state.sqlite')
            Path(path).touch()
            with patch.dict(os.environ, {'THUNDER_APOLLO_LI_OFFICIAL_DB': path}):
                repo = Repository(path)
            ctx = multiprocessing.get_context('spawn')
            barrier = ctx.Barrier(2)
            queue = ctx.Queue()
            processes = [ctx.Process(target=contend_cursor, args=(path, barrier, queue)) for _ in range(2)]
            for process in processes:
                process.start()
            for process in processes:
                process.join(15)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
            outcomes = [queue.get(timeout=3) for _ in processes]
            self.assertCountEqual(outcomes, [('observed', None),
                                            ('partial', 'cursor_persistence_unavailable')])
            states = repo.list_state('apollo_linkedin_worker_cursor')
            self.assertEqual(len(states), 1)
            self.assertEqual(states[0]['version'], 1)
            self.assertEqual(states[0]['last_task'], 'first')

    def test_cli_is_disabled_before_constructing_runtime(self):
        with patch.dict(os.environ, {}, clear=True), patch(
                'thunder_os.apollo_linkedin_campaign_worker.build_campaign_runtime') as build:
            with patch('sys.stdout', new_callable=io.StringIO) as output:
                code = main(['--campaign', 'campaign', '--approval', 'approval',
                             '--campaign-version', 'v1', '--execute'])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())['reason'], 'worker_disabled')
        build.assert_not_called()


if __name__ == '__main__':
    unittest.main()
