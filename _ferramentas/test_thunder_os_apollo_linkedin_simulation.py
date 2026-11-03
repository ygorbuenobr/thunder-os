import copy
import multiprocessing
from pathlib import Path
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from thunder_os.apollo_linkedin_simulation import FakeApollo, FakeLinkedIn, FakeAuthority, FakeStore, Simulation


def fixture():
    return {
        'id': 'task-1', 'type': 'linkedin_step_connect', 'status': 'scheduled',
        'due_at': '2020-11-03T15:00:00Z', 'contact': {'id': 'contact-1', 'linkedin_url': 'https://www.linkedin.com/in/lead-one'},
        'sequence': {'id': 'seq-1', 'step_position': 1}, 'user_id': 'user-1', 'body': 'Olá, Ana!',
        'company_id': 'company-1', 'apollo_org_id': 'org-1',
    }


def policy():
    return {'company_id': 'company-1', 'apollo_org_id': 'org-1', 'sequence_id': 'seq-1',
            'campaign_id': 'campaign-1', 'campaign_version': 1, 'account_id': 'account-1',
            'authorized': True, 'allowed_types': ['linkedin_step_connect', 'linkedin_step_message'],
            'owner_id': 'user-1', 'reply_checked': True, 'reply_age_seconds': 0,
            'has_reply': False, 'approved_body': 'Olá, Ana!', 'allowed_account_ids': ['account-1']}


def attempt_in_other_process(sim, result):
    try:
        sim.execute('task-1', policy())
    except RuntimeError as exc:
        result.send(str(exc))
    else:
        result.send('not_blocked')
    finally:
        result.close()


def process_attempt(path, ready, start, results, task_id='task-1', hold=None, release=None, account='account-1'):
    store = FakeStore(path)
    task = fixture(); task['id'] = task_id
    p = policy(); p['account_id'] = account; p['allowed_account_ids'] = [account]
    apollo, linkedin, authority = FakeApollo([task]), FakeLinkedIn(), FakeAuthority(p)
    if hold is not None:
        dispatch = linkedin.dispatch
        def delayed(*args):
            hold.set()
            if not release.wait(5):
                raise RuntimeError('test_release_timeout')
            return dispatch(*args)
        linkedin.dispatch = delayed
    try:
        sim = Simulation(apollo, linkedin, authority, store)
        ready.put(True)
        if not start.wait(5):
            raise RuntimeError('test_start_timeout')
        result = sim.execute(task_id, p)
        results.put((result, linkedin.attempts))
    finally:
        store.close()


class ApolloLinkedinSimulationTests(unittest.TestCase):
    def setUp(self):
        self.task = fixture()
        self.apollo = FakeApollo([self.task])
        self.linkedin = FakeLinkedIn()
        self.authority = FakeAuthority(policy())
        self.store = FakeStore.provision_test_store()
        self.sim = Simulation(self.apollo, self.linkedin, self.authority, self.store)
        self.addCleanup(self.store.close)

    def test_reader_keeps_actual_sequence_contact_and_type(self):
        self.assertEqual(self.sim.read_tasks(), [self.task])
        self.assertEqual(self.sim.get_task('task-1')['sequence']['step_position'], 1)

    def test_empty_source(self):
        self.apollo.tasks.clear()
        self.assertEqual(self.sim.read_tasks(), [])
        self.assertEqual(self.sim.execute('task-1', policy())['state'], 'blocked')

    def test_confirmed_receipt_then_complete_and_replay_no_duplicate(self):
        result = self.sim.execute('task-1', policy())
        self.assertEqual(result['state'], 'apollo_completed')
        self.assertEqual(result['receipt'], 'fake-receipt-1')
        self.assertEqual(self.apollo.completions, 1)
        self.assertEqual(self.linkedin.attempts, 1)
        self.assertEqual(self.sim.execute('task-1', policy())['state'], 'apollo_completed')
        self.assertEqual(self.linkedin.attempts, 1)

    def test_real_shape_missing_content_fails_closed(self):
        self.apollo.tasks['task-1'].pop('user_id')
        self.apollo.tasks['task-1'].pop('body')
        self.assertEqual(self.sim.execute('task-1', policy())['state'], 'blocked')
        self.assertEqual(self.linkedin.attempts, 0)

    def test_explicit_empty_invite(self):
        self.apollo.tasks['task-1']['body'] = ''
        p = policy(); p['approved_body'] = ''
        self.authority.policy = copy.deepcopy(p)
        self.assertEqual(self.sim.execute('task-1', p)['state'], 'apollo_completed')

    def test_missing_owner_fails_closed(self):
        self.apollo.tasks['task-1'].pop('user_id')
        self.assertEqual(self.sim.execute('task-1', policy())['state'], 'blocked')
        self.assertEqual(self.linkedin.attempts, 0)

    def test_campaign_and_reply_fail_closed(self):
        for field, value in [('authorized', False), ('account_id', ''), ('account_id', 'other'),
                             ('sequence_id', 'other'), ('company_id', 'other'),
                             ('apollo_org_id', 'other'), ('reply_checked', False),
                             ('reply_age_seconds', 61), ('has_reply', True)]:
            with self.subTest(field=field):
                p = policy(); p[field] = value
                self.authority.policy = copy.deepcopy(p)
                self.assertEqual(self.sim.execute('task-1', p)['state'], 'blocked')
        self.assertEqual(self.linkedin.attempts, 0)

    def test_reject_unsupported_or_unrendered(self):
        for key, value in [('type', 'linkedin_step_view_profile'), ('body', 'Oi {{first_name}}')]:
            with self.subTest(key=key):
                self.apollo.tasks['task-1'] = fixture(); self.apollo.tasks['task-1'][key] = value
                self.assertEqual(self.sim.execute('task-1', policy())['state'], 'blocked')
        self.assertEqual(self.linkedin.attempts, 0)

    def test_changed_task_between_reservation_and_dispatch_blocks(self):
        self.apollo.change_on_get = 3
        self.assertEqual(self.sim.execute('task-1', policy())['reason'], 'task_changed')
        self.assertEqual(self.linkedin.attempts, 0)

    def test_task_skipped_after_dispatch_is_not_completed(self):
        self.apollo.change_on_get = 4
        result = self.sim.execute('task-1', policy())
        self.assertEqual(result['state'], 'apollo_unconfirmed')
        self.assertEqual((self.linkedin.attempts, self.apollo.completions), (1, 0))

    def test_closed_task_blocks(self):
        self.apollo.tasks['task-1']['status'] = 'completed'
        self.assertEqual(self.sim.execute('task-1', policy())['state'], 'blocked')
        self.assertEqual(self.linkedin.attempts, 0)

    def test_uncertain_provider_never_replays_or_completes(self):
        self.linkedin.outcome = 'uncertain'
        self.assertEqual(self.sim.execute('task-1', policy())['state'], 'provider_uncertain')
        self.assertEqual(self.sim.execute('task-1', policy())['state'], 'provider_uncertain')
        self.assertEqual((self.linkedin.attempts, self.apollo.completions), (1, 0))

    def test_provider_timeout_is_uncertain(self):
        self.linkedin.outcome = 'timeout'
        self.assertEqual(self.sim.execute('task-1', policy())['state'], 'provider_uncertain')
        self.assertEqual(self.apollo.completions, 0)

    def test_failed_completion_never_replays_linkedin(self):
        self.apollo.complete_outcome = 'uncertain'
        self.assertEqual(self.sim.execute('task-1', policy())['state'], 'apollo_uncertain')
        self.assertEqual(self.sim.execute('task-1', policy())['state'], 'apollo_uncertain')
        self.assertEqual(self.linkedin.attempts, 1)

    def test_apollo_timeout_does_not_repeat_dispatch(self):
        self.apollo.complete_outcome = 'timeout'
        self.assertEqual(self.sim.execute('task-1', policy())['state'], 'apollo_uncertain')
        self.assertEqual(self.sim.execute('task-1', policy())['state'], 'apollo_uncertain')
        self.assertEqual(self.linkedin.attempts, 1)

    def test_retry_reconciles_apollo_without_replaying_linkedin(self):
        self.apollo.complete_outcome = 'uncertain'
        self.assertEqual(self.sim.execute('task-1', policy())['state'], 'apollo_uncertain')
        self.apollo.tasks['task-1']['status'] = 'completed'
        self.assertEqual(self.sim.execute('task-1', policy())['state'], 'apollo_completed')
        self.assertEqual(self.linkedin.attempts, 1)

    def test_renewed_reply_observation_allows_reconciliation(self):
        self.apollo.complete_outcome = 'uncertain'
        self.assertEqual(self.sim.execute('task-1', policy())['state'], 'apollo_uncertain')
        self.apollo.tasks['task-1']['status'] = 'completed'
        p = policy(); p['reply_age_seconds'] = 2
        self.authority.policy = copy.deepcopy(p)
        self.assertEqual(self.sim.execute('task-1', p)['state'], 'apollo_completed')
        self.assertEqual(self.linkedin.attempts, 1)

    def test_changed_policy_does_not_reuse_receipt(self):
        self.assertEqual(self.sim.execute('task-1', policy())['state'], 'apollo_completed')
        p = policy(); p['campaign_version'] = 2
        self.assertEqual(self.sim.execute('task-1', p)['state'], 'blocked')

    def test_changed_task_after_dispatch_does_not_reuse_receipt(self):
        self.assertEqual(self.sim.execute('task-1', policy())['state'], 'apollo_completed')
        self.apollo.tasks['task-1']['body'] = 'Different content'
        self.assertEqual(self.sim.execute('task-1', policy())['state'], 'blocked')

    def test_approved_message(self):
        self.apollo.tasks['task-1']['type'] = 'linkedin_step_message'
        self.assertEqual(self.sim.execute('task-1', policy())['state'], 'apollo_completed')

    def test_rejected_provider_does_not_complete_apollo(self):
        self.linkedin.outcome = 'rejected'
        self.assertEqual(self.sim.execute('task-1', policy())['state'], 'provider_rejected')
        self.assertEqual(self.apollo.completions, 0)

    def test_deleted_task_not_confirmed(self):
        self.apollo.complete_outcome = 'deleted'
        self.assertEqual(self.sim.execute('task-1', policy())['state'], 'apollo_unconfirmed')

    def test_concurrent_execution_reserves_once(self):
        barrier = threading.Barrier(2)
        def run():
            barrier.wait()
            return self.sim.execute('task-1', copy.deepcopy(policy()))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: run(), range(2)))
        self.assertEqual(self.linkedin.attempts, 1)
        self.assertEqual(self.apollo.completions, 1)
        self.assertTrue(all(r['state'] in ('reserved', 'provider_confirmed', 'apollo_completed') or
                            (r['state'], r.get('reason')) == ('blocked', 'account_locked') for r in results))

    def test_concurrent_separate_instances_reserve_once(self):
        other = Simulation(self.apollo, self.linkedin, self.authority, self.store)
        barrier = threading.Barrier(2)
        def run(sim):
            barrier.wait()
            return sim.execute('task-1', copy.deepcopy(policy()))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, [self.sim, other]))
        self.assertEqual(self.linkedin.attempts, 1)
        self.assertEqual(self.apollo.completions, 1)
        self.assertTrue(all(r['state'] in ('reserved', 'provider_confirmed', 'apollo_completed') or
                            (r['state'], r.get('reason')) == ('blocked', 'account_locked') for r in results))

    def test_missing_official_store_refuses_to_start(self):
        with self.assertRaisesRegex(RuntimeError, 'official_store_missing'):
            FakeStore()
        with self.assertRaisesRegex(RuntimeError, 'official_store_missing'):
            Simulation(self.apollo, self.linkedin, self.authority, None)
        missing = Path(self.store.path.parent) / 'missing.sqlite'
        with self.assertRaisesRegex(RuntimeError, 'official_store_missing'):
            FakeStore(missing)
        self.assertFalse(missing.exists())
        self.assertEqual(self.linkedin.attempts, 0)

    def test_reservation_is_persisted_before_dispatch(self):
        original = self.linkedin.dispatch
        def inspect_reservation(*args):
            rows = self.store.repo.list_state('apollo_linkedin_simulation')
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['state'], 'reserved')
            return original(*args)
        self.linkedin.dispatch = inspect_reservation
        self.assertEqual(self.sim.execute('task-1', policy())['state'], 'apollo_completed')

    def test_two_processes_share_one_durable_reservation(self):
        context = multiprocessing.get_context('spawn')
        ready, results = context.Queue(), context.Queue()
        start = context.Event()
        # Different account locks ensure SQLite reservation, not the account lock,
        # contains the same task across independently running processes.
        processes = [context.Process(target=process_attempt, args=(str(self.store.path), ready, start, results,
                                                                    'task-1', None, None, account))
                     for account in ('account-1', 'account-2')]
        for process in processes:
            process.start()
        self.assertTrue(ready.get(timeout=10)); self.assertTrue(ready.get(timeout=10))
        start.set()
        outcomes = [results.get(timeout=10), results.get(timeout=10)]
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(sum(attempts for _, attempts in outcomes), 1)
        self.assertTrue(any(result['state'] == 'apollo_completed' for result, _ in outcomes))
        third = FakeStore(self.store.path)
        try:
            apollo, linkedin = FakeApollo([fixture()]), FakeLinkedIn()
            sim = Simulation(apollo, linkedin, FakeAuthority(policy()), third)
            sim.execute('task-1', policy())
            self.assertEqual(linkedin.attempts, 0)
        finally:
            third.close()

    def test_two_processes_same_linkedin_account_lock_different_tasks(self):
        context = multiprocessing.get_context('spawn')
        ready, results = context.Queue(), context.Queue()
        start, entered, release = context.Event(), context.Event(), context.Event()
        process = context.Process(target=process_attempt, args=(str(self.store.path), ready, start, results,
                                                               'task-1', entered, release))
        process.start()
        self.assertTrue(ready.get(timeout=10)); start.set()
        try:
            self.assertTrue(entered.wait(10), 'first process did not start dispatch')
            task = fixture(); task['id'] = 'task-2'
            other_linkedin = FakeLinkedIn()
            other = Simulation(FakeApollo([task]), other_linkedin, FakeAuthority(policy()), self.store)
            result = other.execute('task-2', policy())
            self.assertEqual((result['state'], result['reason']), ('blocked', 'account_locked'))
            self.assertEqual(other_linkedin.attempts, 0)
        finally:
            release.set()
            process.join(10)
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(results.get(timeout=10)[1], 1)

    def test_same_fake_provider_cannot_be_bound_to_independent_stores(self):
        other = FakeStore.provision_test_store()
        try:
            with self.assertRaisesRegex(ValueError, 'simulation_store_mismatch'):
                Simulation(self.apollo, self.linkedin, self.authority, other)
            with self.assertRaisesRegex(ValueError, 'simulation_store_mismatch'):
                Simulation(FakeApollo([fixture()]), self.linkedin, self.authority, other)
            with self.assertRaisesRegex(ValueError, 'simulation_store_mismatch'):
                Simulation(self.apollo, FakeLinkedIn(), self.authority, other)
            self.assertEqual(self.linkedin.attempts, 0)
        finally:
            other.close()

    def test_store_cannot_be_used_in_another_process(self):
        context = multiprocessing.get_context('fork')
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=attempt_in_other_process, args=(self.sim, sender))
        process.start()
        sender.close()
        self.assertTrue(receiver.poll(5), 'child did not return')
        self.assertEqual(receiver.recv(), 'simulation_store_wrong_process')
        process.join(5)
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(self.linkedin.attempts, 0)

    def test_reply_or_revocation_during_dispatch_does_not_complete_apollo(self):
        for change in ({'has_reply': True}, {'authorized': False}, {'reply_checked': False},
                       {'reply_age_seconds': 61}):
            with self.subTest(change=change):
                apollo, linkedin = FakeApollo([fixture()]), FakeLinkedIn()
                authority, store = FakeAuthority(policy()), FakeStore.provision_test_store()
                authority.change_on_get = 3
                authority.change = change
                try:
                    sim = Simulation(apollo, linkedin, authority, store)
                    result = sim.execute('task-1', policy())
                    self.assertEqual(result['state'], 'apollo_unconfirmed')
                    self.assertEqual(result['reason'], 'authority_changed_after_dispatch')
                    self.assertEqual(result['receipt'], 'fake-receipt-1')
                    self.assertEqual((linkedin.attempts, apollo.completions), (1, 0))
                    sim.execute('task-1', policy())
                    self.assertEqual((linkedin.attempts, apollo.completions), (1, 0))
                finally:
                    store.close()

    def test_reply_or_revocation_after_reservation_blocks(self):
        for change in ({'has_reply': True}, {'authorized': False}, {'reply_checked': False}):
            with self.subTest(change=change):
                apollo = FakeApollo([fixture()])
                linkedin = FakeLinkedIn()
                authority = FakeAuthority(policy())
                authority.change_on_get = 2
                authority.change = change
                store = FakeStore.provision_test_store()
                try:
                    sim = Simulation(apollo, linkedin, authority, store)
                    self.assertEqual(sim.execute('task-1', policy())['reason'], 'authority_changed')
                    self.assertEqual(linkedin.attempts, 0)
                finally:
                    store.close()

    def test_real_provider_object_rejected(self):
        with self.assertRaises(TypeError):
            Simulation(self.apollo, object(), self.authority, self.store)


if __name__ == '__main__':
    unittest.main()
