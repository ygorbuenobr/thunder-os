"""Consumed-effect incidents on the real core with isolated SQLite and fake email."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Barrier
import sqlite3
import unittest
from unittest.mock import patch

import test_thunder_os_eval_trajectory as trajectory
from thunder_os.db import Repository, RepositoryConflict
from thunder_os.touch_ledger import TouchBlocked, TouchLedger
from thunder_os.worker import WorkerRuntime


class EffectReconciliationTests(unittest.TestCase):
    def setUp(self):
        # Reuse the offline fixture, not its test methods or assertions.
        self.scenario = trajectory.EvalTrajectoryTests()
        self.addCleanup(self.scenario.doCleanups)
        self.scenario.setUp()
        self.repo = self.scenario.repo
        self.ledger = self.scenario.ledger
        self.worker = self.scenario.worker
        self.scenario.service.resolve_identity({
            'id': 'reconciliation-person', 'records': [{
                'domain': self.scenario.research['account']['domain'],
                'person_key': 'person@example.invalid', 'source': source}
                for source in ('company_site', 'hubspot')]}, 'ygor')

    def seed_history(self, *, copies=2, terminal='sent', mode='confirmed'):
        self.scenario.approve()
        job = self.scenario.queue()
        step = job['payload']['step']
        command = {'target': step['recipient'], 'subject': step['subject'],
                   'content': step['content'], 'effect_id': step['id']}
        self.ledger.reserve(job['effect_key'], step['id'], step)
        self.scenario.mailbox.mode = mode
        result = self.scenario.adapter.send(command)
        status = {'confirmed': 'sent', 'rejected': 'failed'}.get(result.status, 'uncertain')
        self.ledger.finish(job['effect_key'], status, result.receipt_ref, result.error_code)
        if terminal != 'pending':
            self.repo.finish_effect(job['id'], terminal, result.receipt_ref, result.error_code)
        for _ in range(copies - 1):
            result = self.scenario.adapter.send(command)
            self.insert_duplicate(result.receipt_ref)
        return self.repo.effect(job['id'])

    def insert_duplicate(self, receipt):
        with self.scenario.database() as (connection, _):
            connection.execute('''
                INSERT INTO toques(canal,acao,pessoa_chave,alvo,status,provider_id,agente,ref)
                SELECT canal,acao,pessoa_chave,alvo,'feito',?,'historical-duplicate',ref
                FROM toques ORDER BY id LIMIT 1''', (receipt,))

    def touches(self):
        with self.scenario.database() as (connection, _):
            return connection.execute('SELECT * FROM toques ORDER BY id').fetchall()

    def evidence(self, job):
        return self.ledger.duplicate_evidence(job['effect_key'], job['payload']['step'])

    def interleaved_ledger(self, after_read, *, read_number=1):
        class Cursor:
            def __init__(self, cursor):
                self.cursor = cursor
                self.reads = 0

            def __getattr__(self, name):
                return getattr(self.cursor, name)

            def fetchone(self):
                row = self.cursor.fetchone()
                self.reads += 1
                if row and self.reads == read_number:
                    after_read()
                return row

        class Connection:
            def __init__(self, connection):
                self.connection = connection

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def cursor(self):
                return Cursor(self.connection.cursor())

        @contextmanager
        def database():
            with self.scenario.database() as (connection, marker):
                yield Connection(connection), marker

        return TouchLedger(db_context=database)

    def test_consumed_duplicate_preserves_history_receipts_and_stable_alert_after_restart(self):
        job = self.seed_history()
        before = self.touches()
        self.assertFalse(self.worker.process_one())
        stored = Repository(self.repo.path).effect(job['id'])
        self.assertEqual(stored['state'], 'needs_reconciliation')
        self.assertEqual(stored['error_code'], 'duplicate_consumed_effect')
        self.assertEqual(stored['receipt_ref'], job['receipt_ref'])
        alerts = self.scenario.service.today()['operator_alerts']
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert['owner'], 'ygor')
        self.assertTrue(alert['active'])
        self.assertEqual(alert['effect_id'], job['id'])
        self.assertEqual(alert['duplicate_evidence']['touches'], [
            {'touch_id': '1', 'status': 'sent', 'receipt_ref': 'email:eval-mail:message-1'},
            {'touch_id': '2', 'status': 'sent', 'receipt_ref': 'email:eval-mail:message-2'}])
        restarted = WorkerRuntime(Repository(self.repo.path), self.ledger,
            {'email': self.scenario.adapter}, worker_id='restart',
            health_path=self.scenario.root / 'restart.json', revision='offline',
            approved_catalog=trajectory.CATALOG)
        for worker in (self.worker, restarted):
            self.assertFalse(worker.process_one())
        self.assertEqual(self.repo.list_state('operator_alert'), alerts)
        self.assertEqual(self.touches(), before)
        self.assertEqual(len(self.scenario.mailbox.commands), 2)
        self.assertEqual(len(self.scenario.mailbox.messages), 2)
        with self.assertRaisesRegex(RepositoryConflict, 'not safely retryable'):
            self.repo.retry_effect(job['id'])

    def test_pending_duplicate_is_detected_before_claim(self):
        job = self.seed_history(terminal='pending')
        with patch.object(self.repo, 'claim_effect', wraps=self.repo.claim_effect) as claim:
            self.assertFalse(self.worker.process_one())
        self.assertEqual(claim.call_count, 1)
        stored = self.repo.effect(job['id'])
        self.assertEqual(stored['state'], 'needs_reconciliation')
        self.assertIsNone(stored['lease_owner'])
        self.assertEqual(len(self.scenario.mailbox.commands), 2)

    def test_claimed_duplicate_outside_current_page_never_dispatches(self):
        job = self.seed_history(terminal='pending')
        with patch.object(self.repo, 'reconciliation_effects', return_value=[]):
            self.assertTrue(self.worker.process_one())
        self.assertEqual(self.repo.effect(job['id'])['state'], 'needs_reconciliation')
        self.assertEqual(len(self.repo.list_state('operator_alert')), 1)
        self.assertEqual(len(self.scenario.mailbox.commands), 2)

    def test_one_touch_repeated_in_evidence_cannot_become_two_executions(self):
        job = self.seed_history(copies=1)
        touch = {'touch_id': '1', 'status': 'sent', 'receipt_ref': job['receipt_ref']}
        with self.assertRaisesRegex(ValueError, 'two confirmed touches required'):
            self.repo.mark_effect_reconciliation(job, {'touches': [touch, touch]})
        self.assertEqual(self.repo.effect(job['id']), job)
        self.assertEqual(self.repo.list_state('operator_alert'), [])

    def test_single_confirmation_is_not_a_duplicate_incident(self):
        job = self.seed_history(copies=1)
        self.assertIsNone(self.evidence(job))
        self.assertFalse(self.worker.process_one())
        self.assertEqual(self.repo.effect(job['id']), job)
        self.assertEqual(self.repo.list_state('operator_alert'), [])
        self.assertEqual(len(self.scenario.mailbox.commands), 1)

    def test_uncertain_history_is_never_retried_or_promoted(self):
        job = self.seed_history(copies=1, terminal='uncertain', mode='timeout_after_effect')
        before = self.touches()
        self.assertFalse(self.worker.process_one())
        self.assertEqual(self.repo.effect(job['id']), job)
        self.assertEqual(self.repo.list_state('operator_alert'), [])
        self.assertEqual(self.touches(), before)
        self.assertEqual(len(self.scenario.mailbox.messages), 1)
        with self.assertRaisesRegex(RepositoryConflict, 'not safely retryable'):
            self.repo.retry_effect(job['id'])

    def test_evidence_is_normalized_bounded_readonly_and_scoped_to_confirmations(self):
        job = self.seed_history(copies=3)
        before = self.touches()
        step = {**job['payload']['step'], 'recipient': ' PERSON@EXAMPLE.INVALID ',
                'subject': '  SYNTHETIC   E1  '}
        evidence = self.ledger.duplicate_evidence(job['effect_key'], step, limit=2)
        self.assertEqual(len(evidence['touches']), 2)
        self.assertTrue(evidence['truncated'])
        self.assertEqual(evidence['identity'], 'person@example.invalid')
        self.assertEqual(self.touches(), before)
        for changes in ({'recipient': 'someone@example.invalid'}, {'subject': 'Another subject'}):
            self.assertIsNone(self.ledger.duplicate_evidence(job['effect_key'], {**step, **changes}))
        with self.scenario.database() as (connection, _):
            connection.execute("UPDATE toques SET status='incerto' WHERE id>1")
        self.assertIsNone(self.evidence(job))
        for limit in (True, 0, 1, 101):
            with self.assertRaises(ValueError):
                self.ledger.duplicate_evidence(job['effect_key'], step, limit=limit)

    def test_unregistered_duplicate_history_is_not_scanned(self):
        job = self.seed_history()
        with self.repo.transaction() as con:
            con.execute('DELETE FROM thunder_effect_jobs WHERE id=?', (job['id'],))
        self.assertFalse(self.worker.process_one())
        self.assertEqual(self.repo.list_state('operator_alert'), [])
        self.assertEqual(len(self.touches()), 2)

    def test_cas_rejects_stale_claim_and_blocks_its_late_completion(self):
        snapshot = self.seed_history(terminal='pending')
        evidence = self.evidence(snapshot)
        claimed = self.repo.claim_effect('other', datetime.now(timezone.utc), timedelta(seconds=30))
        with self.assertRaisesRegex(RepositoryConflict, 'changed during reconciliation'):
            self.repo.mark_effect_reconciliation(snapshot, evidence)
        self.assertEqual(self.repo.effect(snapshot['id'])['lease_owner'], claimed['lease_owner'])
        self.assertEqual(self.repo.list_state('operator_alert'), [])
        self.assertTrue(self.repo.mark_effect_reconciliation(claimed, evidence))
        with self.assertRaises(RepositoryConflict):
            self.repo.finish_effect(claimed['id'], 'sent', 'late-receipt', None,
                                    lease_owner=claimed['lease_owner'])
        self.assertEqual(self.repo.effect(snapshot['id'])['state'], 'needs_reconciliation')

    def test_alert_failure_rolls_back_effect_transition(self):
        job = self.seed_history()
        with patch.object(self.repo, 'append_state', side_effect=RuntimeError('synthetic write failure')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic write failure'):
                self.repo.mark_effect_reconciliation(job, self.evidence(job))
        self.assertEqual(self.repo.effect(job['id']), job)
        self.assertEqual(self.repo.list_state('operator_alert'), [])

    def test_concurrent_reconciliation_writers_leave_one_incident(self):
        job = self.seed_history()
        evidence = self.evidence(job)
        barrier = Barrier(2)

        def reconcile():
            repository = Repository(self.repo.path)
            barrier.wait(timeout=5)
            try:
                return repository.mark_effect_reconciliation(job, evidence)
            except RepositoryConflict:
                return 'conflict'

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: reconcile(), range(2)))
        self.assertCountEqual(results, [True, 'conflict'])
        self.assertEqual(len(self.repo.list_state('operator_alert')), 1)
        self.assertEqual(self.repo.effect(job['id'])['receipt_ref'], job['receipt_ref'])

    def test_registered_effect_paging_does_not_starve_old_or_late_incidents(self):
        job = self.seed_history()
        for index in range(101):
            self.repo.enqueue_effect('other-' + str(index), {'step': {
                **job['payload']['step'], 'recipient': 'other@example.invalid'}, 'cadence_id': 'other'})
            other = self.repo.reconciliation_effects(after_rowid=index + 1, limit=1)[0]
            self.repo.finish_effect(other['id'], 'sent', 'other-receipt', None)
        # Put the incident on the second page, then verify the cursor wraps too.
        with self.repo.transaction() as con:
            con.execute('UPDATE thunder_effect_jobs SET rowid=200 WHERE id=?', (job['id'],))
        self.assertFalse(self.worker.process_one())
        self.assertEqual(self.repo.effect(job['id'])['state'], 'sent')
        self.assertFalse(self.worker.process_one())
        self.assertEqual(self.repo.effect(job['id'])['state'], 'needs_reconciliation')
        self.assertEqual(self.worker._reconciliation_cursor, 0)
        self.assertEqual(len(self.repo.list_state('operator_alert')), 1)

    def test_identity_change_after_queue_blocks_before_reservation(self):
        self.scenario.approve()
        job = self.scenario.queue()
        self.repo.put_state('identity', 'reconciliation-person', {
            'actor': 'ygor', 'status': 'identity_resolution',
            'records': [{'person_key': 'person@example.invalid', 'domain': 'different.invalid'}]})
        self.assertTrue(self.worker.process_one())
        self.assertEqual(self.repo.effect(job['id'])['error_code'], 'identity_resolution_required')
        self.assertEqual(self.scenario.mailbox.commands, [])
        self.assertEqual(self.touches(), [])

    def test_duplicate_history_read_errors_block_before_claim_or_provider(self):
        self.scenario.approve()
        job = self.scenario.queue()
        for error in (TouchBlocked('history unavailable'), ValueError('invalid history'),
                      RuntimeError('read failure')):
            with self.subTest(error=type(error).__name__), patch.object(
                    self.ledger, 'duplicate_evidence', side_effect=error):
                with self.assertRaises(type(error)):
                    self.worker.process_one()
            self.assertEqual(self.repo.effect(job['id'])['state'], 'pending')
            self.assertIsNone(self.repo.effect(job['id'])['lease_owner'])
            self.assertEqual(self.scenario.mailbox.commands, [])
            self.assertEqual(self.touches(), [])

    def test_campaign_validation_precedes_identity_error_in_both_gates(self):
        self.scenario.approve()
        job = self.scenario.queue()
        cadence = self.repo.get_state('cadence', 'batch')
        cadence['steps'][0]['recipient'] = 'changed@example.invalid'
        self.repo.put_state('cadence', 'batch', cadence)
        self.assertEqual(self.worker.authorization_service.identity_execution_error(cadence),
                         'identity_resolution_required')
        step = job['payload']['step']
        for result in (
            self.worker._runtime_gate(job['payload'], step, job['effect_key']),
            self.worker._final_campaign_gate(job['payload'], step),
        ):
            self.assertEqual(result, 'campaign_confirmation_scope_changed')

    def test_identity_change_during_reservation_is_rechecked_inside_dispatch_fence(self):
        self.scenario.approve()
        job = self.scenario.queue()
        reserve = self.ledger.reserve

        def reserve_then_change(*args):
            result = reserve(*args)
            identity = self.repo.get_state('identity', 'reconciliation-person')
            self.repo.put_state('identity', identity['id'], {**identity, 'status': 'identity_resolution'})
            return result

        with patch.object(self.ledger, 'reserve', side_effect=reserve_then_change):
            self.assertTrue(self.worker.process_one())
        stored = self.repo.effect(job['id'])
        self.assertEqual(stored['state'], 'needs_reconciliation')
        self.assertEqual(stored['error_code'], 'identity_resolution_required')
        self.assertEqual(self.scenario.mailbox.commands, [])
        with self.scenario.database() as (connection, _):
            self.assertEqual(connection.execute('SELECT status FROM toques').fetchall(), [('reservado',)])

    def test_confirmed_retry_changes_only_confirmation_payload_and_resets_safe_job(self):
        job = self.seed_history(copies=1, terminal='failed', mode='rejected')
        before = self.touches()
        confirmation_hash = 'a' * 64
        payload = {**job['payload'], 'execution_confirmation_hash': confirmation_hash}
        stored = self.repo.requeue_confirmed_effect(job, confirmation_hash)
        self.assertEqual(stored['payload'], payload)
        self.assertEqual(stored['state'], 'pending')
        self.assertEqual(stored['id'], job['id'])
        self.assertEqual(stored['effect_key'], job['effect_key'])
        for field in ('receipt_ref', 'error_code', 'lease_owner', 'lease_until'):
            self.assertIsNone(stored[field])
        self.assertEqual(self.touches(), before)
        self.assertEqual(self.repo.get_state('approval', 'batch')['status'], 'active')
        self.assertEqual(len(self.scenario.mailbox.commands), 1)
        self.assertEqual(self.scenario.mailbox.messages, {})

    def test_confirmed_retry_rejects_invalid_or_old_confirmation_without_mutation(self):
        job = self.seed_history(copies=1, terminal='failed', mode='rejected')
        for confirmation in (None, True, '', 'a' * 63, 'a' * 65, 'g' * 64, 'A' * 64):
            with self.subTest(confirmation=confirmation), self.assertRaises(ValueError):
                self.repo.requeue_confirmed_effect(job, confirmation)
        with self.assertRaisesRegex(RepositoryConflict, 'fresh campaign confirmation required'):
            self.repo.requeue_confirmed_effect(job, job['payload']['execution_confirmation_hash'])
        self.assertEqual(self.repo.effect(job['id']), job)

    def test_confirmed_retry_rejects_stale_payload_and_state_snapshots(self):
        job = self.seed_history(copies=1, terminal='failed', mode='rejected')
        changed_payload = deepcopy(job)
        changed_payload['payload']['actor'] = 'different-actor'
        for snapshot in (changed_payload, {**job, 'state': 'pending'},
                         {**job, 'receipt_ref': 'forged-receipt'}):
            with self.subTest(snapshot=snapshot), self.assertRaisesRegex(
                    RepositoryConflict, 'changed during confirmed retry'):
                self.repo.requeue_confirmed_effect(snapshot, 'a' * 64)
        self.assertEqual(self.repo.effect(job['id']), job)

    def test_confirmed_retry_rejects_uncertain_sent_reconciliation_and_unsafe_failures(self):
        job = self.seed_history(copies=1, terminal='failed', mode='rejected')
        for state, error in (('uncertain', 'provider_rejected'), ('sent', 'provider_rejected'),
                             ('needs_reconciliation', 'provider_rejected'),
                             ('pending', 'provider_rejected'), ('failed', 'provider_timeout'),
                             ('failed', 'worker_execution_uncertain'), ('failed', None)):
            with self.repo.transaction() as con:
                con.execute('UPDATE thunder_effect_jobs SET state=?,error_code=? WHERE id=?',
                            (state, error, job['id']))
            snapshot = self.repo.effect(job['id'])
            with self.subTest(state=state, error=error), self.assertRaisesRegex(
                    RepositoryConflict, 'not safely retryable'):
                self.repo.requeue_confirmed_effect(snapshot, 'a' * 64)
            self.assertEqual(self.repo.effect(job['id']), snapshot)

    def test_confirmed_retry_participates_in_callers_transaction(self):
        job = self.seed_history(copies=1, terminal='failed', mode='rejected')
        approval = self.repo.get_state('approval', 'batch')
        with self.assertRaisesRegex(RuntimeError, 'synthetic caller failure'):
            with self.repo.transaction():
                self.repo.put_state('approval', 'batch', {**approval, 'fresh_marker': 'test'})
                self.repo.requeue_confirmed_effect(job, 'a' * 64)
                raise RuntimeError('synthetic caller failure')
        self.assertEqual(self.repo.effect(job['id']), job)
        self.assertEqual(self.repo.get_state('approval', 'batch'), approval)

    def test_release_rereads_confirmation_committed_after_its_initial_select(self):
        job = self.seed_history(copies=1, terminal='failed', mode='rejected')
        confirmed = []

        def confirm_after_select():
            self.scenario.mailbox.mode = 'confirmed'
            result = self.scenario.adapter.send(self.scenario.mailbox.commands[0])
            confirmed.append(self.ledger.finish(job['effect_key'], 'sent', result.receipt_ref))

        racing = self.interleaved_ledger(confirm_after_select)
        with self.assertRaises(TouchBlocked):
            racing.release_failed_for_retry(job['effect_key'])
        self.assertEqual(confirmed[0]['status'], 'sent')
        created, touch = self.ledger.reserve(job['effect_key'], 'E1', job['payload']['step'])
        self.assertFalse(created)
        self.assertEqual(touch, confirmed[0])
        with self.scenario.database() as (connection, _):
            self.assertEqual(connection.execute('SELECT status,provider_id FROM toques').fetchall(),
                             [('feito', confirmed[0]['receipt_ref'])])
            self.assertEqual(connection.execute('SELECT effect_key,touch_id FROM thunder_effect_touches').fetchall(),
                             [(job['effect_key'], int(confirmed[0]['id']))])
        self.assertEqual(len(self.scenario.mailbox.commands), 2)
        self.assertEqual(len(self.scenario.mailbox.messages), 1)

    def test_release_preserves_a_replacement_mapping_created_before_lock(self):
        job = self.seed_history(copies=1, terminal='failed', mode='rejected')
        replacement = []

        def replace_after_select():
            self.ledger.release_failed_for_retry(job['effect_key'])
            created, touch = self.ledger.reserve(job['effect_key'], 'E1', job['payload']['step'])
            self.assertTrue(created)
            replacement.append(touch)

        with self.assertRaises(TouchBlocked):
            self.interleaved_ledger(replace_after_select).release_failed_for_retry(job['effect_key'])
        with self.scenario.database() as (connection, _):
            self.assertEqual(connection.execute('SELECT status FROM toques ORDER BY id').fetchall(),
                             [('liberado',), ('reservado',)])
            self.assertEqual(connection.execute('SELECT touch_id FROM thunder_effect_touches').fetchall(),
                             [(int(replacement[0]['id']),)])

    def test_release_update_cas_rejects_confirmation_after_locked_status_read(self):
        job = self.seed_history(copies=1, terminal='failed', mode='rejected')
        confirmed = []

        def confirm_after_status_read():
            confirmed.append(self.ledger.finish(job['effect_key'], 'sent', 'late-confirmed-receipt'))

        racing = self.interleaved_ledger(confirm_after_status_read, read_number=2)
        # Isolate the SQL CAS: SQLite's write lock otherwise prevents this
        # interleaving. This is not a PostgreSQL/advisory-lock integration test.
        with patch.object(racing, '_lock') as lock, self.assertRaises(TouchBlocked):
            racing.release_failed_for_retry(job['effect_key'])
        lock.assert_called_once()
        self.assertEqual(confirmed[0]['status'], 'sent')
        with self.scenario.database() as (connection, _):
            self.assertEqual(connection.execute('SELECT status,provider_id FROM toques').fetchall(),
                             [('feito', 'late-confirmed-receipt')])
            self.assertEqual(connection.execute('SELECT touch_id FROM thunder_effect_touches').fetchall(), [(1,)])

    def test_release_reuses_an_existing_sqlite_immediate_transaction(self):
        job = self.seed_history(copies=1, terminal='failed', mode='rejected')

        @contextmanager
        def database():
            with self.scenario.database() as (connection, marker):
                connection.execute('BEGIN IMMEDIATE')
                yield connection, marker

        ledger = TouchLedger(db_context=database)
        self.assertEqual(ledger.release_failed_for_retry(job['effect_key']),
                         {'touch_id': '1', 'status': 'released'})
        with self.scenario.database() as (connection, _):
            self.assertEqual(connection.execute('SELECT status FROM toques').fetchall(), [('liberado',)])
            self.assertEqual(connection.execute('SELECT * FROM thunder_effect_touches').fetchall(), [])

    def test_two_actual_retries_and_workers_produce_one_confirmed_effect(self):
        job = self.seed_history(copies=1, terminal='failed', mode='rejected')
        review = self.scenario.service.retry_execution_review(job['id'])
        barrier = Barrier(2)

        def retry():
            service = trajectory.ThunderService(Repository(self.repo.path), self.ledger,
                                                approved_catalog=trajectory.CATALOG)
            barrier.wait(timeout=5)
            try:
                return service.retry_effect(job['id'], 'ygor', execution_confirmation=review)['state']
            except RepositoryConflict:
                return 'blocked'

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: retry(), range(2)))
        self.assertCountEqual(results, ['pending', 'blocked'])
        self.scenario.mailbox.mode = 'confirmed'
        workers = [WorkerRuntime(Repository(self.repo.path), self.ledger,
            {'email': self.scenario.adapter}, worker_id='retry-' + str(index),
            health_path=self.scenario.root / ('retry-' + str(index) + '.json'),
            revision='offline', approved_catalog=trajectory.CATALOG) for index in range(2)]
        with ThreadPoolExecutor(max_workers=2) as executor:
            worked = list(executor.map(lambda worker: worker.process_one(), workers))
        self.assertCountEqual(worked, [True, False])
        effect = self.repo.effect(job['id'])
        self.assertEqual(effect['state'], 'sent')
        self.assertEqual(len(self.scenario.mailbox.commands), 2)
        self.assertEqual(len(self.scenario.mailbox.messages), 1)
        with self.scenario.database() as (connection, _):
            self.assertEqual(connection.execute('SELECT status,provider_id FROM toques ORDER BY id').fetchall(),
                             [('liberado', None), ('feito', effect['receipt_ref'])])
            self.assertEqual(connection.execute('SELECT touch_id FROM thunder_effect_touches').fetchall(), [(2,)])

    def test_invalid_consumed_history_does_not_starve_independent_valid_job(self):
        invalid = self.repo.enqueue_effect('malformed-history', {'cadence_id': 'legacy', 'step': {
            'id': 'legacy-E1', 'channel': 'email', 'recipient': 'not-an-email',
            'subject': 'Legacy', 'content': 'Historical content', 'order': 1, 'interval_hours': 0}})
        self.repo.finish_effect(invalid['id'], 'sent', 'preserved-legacy-receipt', None)
        invalid = self.repo.effect(invalid['id'])
        self.scenario.approve()
        valid = self.scenario.queue()
        self.assertEqual([self.worker.process_one() for _ in range(3)], [True, False, False])
        self.assertEqual(self.repo.effect(valid['id'])['state'], 'sent')
        self.assertEqual(self.repo.effect(invalid['id']), invalid)
        self.assertEqual(len(self.scenario.mailbox.commands), 1)
        self.assertEqual(len(self.scenario.mailbox.messages), 1)
        alerts = self.repo.list_state('operator_alert')
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['effect_id'], invalid['id'])
        self.assertEqual(alerts[0]['code'], 'invalid_effect_touch_scope')
        self.assertEqual(alerts[0]['owner'], 'ygor')
        self.assertEqual(alerts[0]['version'], 1)
        self.assertTrue(alerts[0]['active'])

    def test_invalid_pending_scope_is_quarantined_and_existing_alert_preserved(self):
        invalid = self.repo.enqueue_effect('invalid-pending', {'step': {
            'id': 'invalid', 'channel': 'email', 'recipient': 'not-an-email'}})
        alert_id = 'invalid-effect-scope:' + invalid['id']
        existing = self.repo.append_state('operator_alert', alert_id, {
            'owner': 'ygor', 'active': True, 'message': 'Prior human incident annotation'})
        self.scenario.approve()
        valid = self.scenario.queue()
        self.assertTrue(self.worker.process_one())
        self.assertEqual(self.repo.effect(invalid['id'])['state'], 'needs_reconciliation')
        self.assertEqual(self.scenario.mailbox.commands, [])
        self.assertTrue(self.worker.process_one())
        self.assertEqual(self.repo.effect(valid['id'])['state'], 'sent')
        self.assertEqual(self.repo.get_state('operator_alert', alert_id), existing)
        self.assertEqual(len(self.scenario.mailbox.commands), 1)

    def test_cursor_advances_past_invalid_scope_but_database_failure_still_blocks(self):
        invalid = self.repo.enqueue_effect('invalid-before-db-error', {'step': {
            'channel': 'email', 'recipient': 'not-an-email'}})
        self.repo.finish_effect(invalid['id'], 'sent', 'preserved-receipt', None)
        self.scenario.approve()
        valid = self.scenario.queue()
        for error in (sqlite3.OperationalError('synthetic database read failure'),
                      ValueError('synthetic database context failure')):
            with self.subTest(error=type(error).__name__), patch.object(
                    self.ledger, '_banco', side_effect=error):
                with self.assertRaises(type(error)):
                    self.worker.process_one()
            self.assertEqual(self.worker._reconciliation_cursor, 1)
            self.assertEqual(self.repo.effect(valid['id'])['state'], 'pending')
            self.assertIsNone(self.repo.effect(valid['id'])['lease_owner'])
            self.assertEqual(self.scenario.mailbox.commands, [])
            alerts = self.repo.list_state('operator_alert')
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0]['effect_id'], invalid['id'])
            self.assertEqual(alerts[0]['version'], 1)
        self.assertTrue(self.worker.process_one())
        self.assertEqual(self.repo.effect(valid['id'])['state'], 'sent')
        self.assertEqual(len(self.scenario.mailbox.commands), 1)


if __name__ == '__main__':
    unittest.main()
