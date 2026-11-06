import hashlib
import os
import threading
import tempfile
from pathlib import Path
import unittest
import io
import json
from contextlib import redirect_stdout
from contextlib import contextmanager
from unittest.mock import patch

from thunder_os.apollo_linkedin_t4 import T4SimulationCoordinator, FakeT4LinkedIn, FakeT4Apollo, FakeT4Store, FakeT4Campaigns, FakeT4Safety, FakeT4Canary, BeforeDispatchError, main
from thunder_os.apollo_linkedin_attention import EXCLUDED_TASK

TEXT = 'Convite de teste'
HASH = hashlib.sha256(TEXT.encode()).hexdigest()
BINDING = {'approved': True, 'campaign_id': 'camp', 'version': 3, 'company_id': 'co',
           'sequence_id': 'seq', 'account_id': 'account', 'account_public_id': 'owner',
           'owner_ids': ['owner-id'], 'weekly_limit': 200, 'spacing_seconds': 60}
PREVIEW = {'text_hash': HASH, 'recipient': 'lead', 'version': 3}


class T4SimulationTests(unittest.TestCase):
    def setUp(self):
        self.store, self.campaigns, self.apollo = FakeT4Store(), FakeT4Campaigns(BINDING), FakeT4Apollo()
        self.linkedin, self.safety, self.canary = FakeT4LinkedIn(), FakeT4Safety(), FakeT4Canary()
        self.linkedin.response = {'status': 'confirmed', 'provider_id': 'receipt'}
        self.coordinator = T4SimulationCoordinator(repo=self.store, campaigns=self.campaigns, apollo=self.apollo,
            linkedin=self.linkedin, safety=self.safety, canary=self.canary, clock=lambda: 1000, simulation=True)
        self.env = patch.dict(os.environ, {'THUNDER_APOLLO_LI_EXECUTION_ENABLED':'true'})
        self.env.start(); self.addCleanup(self.env.stop)
    def run_task(self, task='task'): return self.coordinator.run(task, 'camp', dict(PREVIEW), dry_run=False)
    def test_simulation_only_and_default_dry_run(self):
        with self.assertRaises(ValueError):
            T4SimulationCoordinator(repo=self.store,campaigns=self.campaigns,apollo=self.apollo,linkedin=self.linkedin,safety=self.safety,canary=self.canary)
        with self.assertRaises(ValueError):
            T4SimulationCoordinator(repo=self.store,campaigns=self.campaigns,apollo=self.apollo,linkedin=object(),safety=self.safety,canary=self.canary,simulation=True)
        self.assertEqual('dry_run', self.coordinator.run('task','camp',dict(PREVIEW))['state'])
        self.assertFalse(self.store.rows); self.assertEqual(0,self.linkedin.calls)
    def test_real_or_unknown_ports_are_rejected(self):
        ports = {'repo':self.store, 'campaigns':self.campaigns, 'apollo':self.apollo,
                 'linkedin':self.linkedin, 'safety':self.safety, 'canary':self.canary}
        for name in ports:
            with self.subTest(port=name), self.assertRaisesRegex(ValueError, 'simulation_only'):
                T4SimulationCoordinator(**{**ports, name:object()}, simulation=True)
    def test_cli_defaults_dry_run_and_never_enables_live(self):
        output=io.StringIO()
        with redirect_stdout(output): self.assertEqual(0,main(['--task','task']))
        self.assertEqual(False,json.loads(output.getvalue())['executable'])
        output=io.StringIO()
        with redirect_stdout(output): self.assertEqual(2,main(['--task','task','--execute']))
        self.assertEqual('live_composition_unavailable',json.loads(output.getvalue())['reason'])
    def test_disabled_fixture_and_unapproved(self):
        os.environ['THUNDER_APOLLO_LI_EXECUTION_ENABLED']='false'
        self.assertEqual('execution_disabled',self.run_task()['reason'])
        os.environ['THUNDER_APOLLO_LI_EXECUTION_ENABLED']='true'
        self.assertEqual('fixture_protegida',self.run_task(EXCLUDED_TASK)['reason'])
        self.campaigns.binding['approved']=False
        self.assertEqual('campaign_unapproved',self.run_task()['reason'])
        self.assertEqual(0,self.linkedin.calls)
    def test_confirm_and_reconcile_after_apollo_failure_without_resending(self):
        self.apollo.fail_complete=True
        self.assertEqual('apollo_reconcile_pending',self.run_task()['state'])
        self.assertEqual(1,self.linkedin.calls)
        self.assertEqual('blocked',self.run_task()['state'])
        self.apollo.fail_complete=False
        self.assertEqual('apollo_reconcile_pending',self.coordinator.reconcile('co','task','linkedin_step_connect')['state'])
        self.assertEqual(1,self.apollo.complete_calls)
        self.apollo.completed=True
        self.assertEqual('confirmed',self.coordinator.reconcile('co','task','linkedin_step_connect')['state'])
        self.assertEqual(1,self.linkedin.calls)
    def test_success_durable_receipt(self):
        self.assertEqual('confirmed', self.run_task()['state'])
        row = next(iter(self.store.rows.values()))
        self.assertEqual({'provider_id':'receipt'}, row['receipt'])
        self.assertEqual('confirmed',row['apollo_status'])
    def test_rejected_before_dispatch(self):
        self.linkedin.error = BeforeDispatchError()
        self.assertEqual('rejected',self.run_task()['state'])
        self.assertFalse(self.apollo.completed)
    def test_timeout_unknown_and_no_replay(self):
        self.linkedin.error = TimeoutError()
        self.assertEqual('uncertain',self.run_task()['state'])
        self.assertEqual('blocked',self.run_task()['state'])
        self.assertEqual(1,self.linkedin.calls)
    def test_missing_receipt_never_completes(self):
        self.linkedin.response={'status':'accepted'}
        self.assertEqual('receipt_missing',self.run_task()['reason'])
        self.assertFalse(self.apollo.completed)
    def test_prior_invitation_uncertain_before_send(self):
        self.linkedin.pending=True
        self.assertEqual('prior_invitation',self.run_task()['reason'])
        self.assertEqual(0,self.linkedin.calls)
    def test_text_change_and_removed(self):
        self.apollo.text='different'
        self.assertEqual('text_changed',self.run_task()['reason'])
        self.apollo.removed=True
        self.assertEqual('task_removed',self.run_task('second')['reason'])
        self.assertEqual(0,self.linkedin.calls)
    def test_reply_quota_pacing_and_unknown_block(self):
        for field, value, expected in [('human_reply',True,'human_reply'),('provider_remaining',0,'quota_exhausted'),
                                       ('lead_touches_today',1,'lead_daily_limit'),('last_account_action_at',990,'pacing_wait'),
                                       ('coverage','partial','coverage_unavailable')]:
            with self.subTest(field=field):
                original=self.safety.data[field];self.safety.data[field]=value
                self.assertEqual(expected,self.run_task('t-'+field)['reason'])
                self.safety.data[field]=original
        self.assertEqual(0,self.linkedin.calls)
    def test_invalid_nonfinite_pacing_blocks(self):
        for value in (float('nan'), float('inf'), float('-inf')):
            self.safety.data['last_account_action_at']=value
            self.assertEqual('pacing_unverified',self.run_task('t-'+str(value))['reason'])
        self.assertEqual(0,self.linkedin.calls)
    def test_human_reply_requires_persistent_pause(self):
        self.safety.data['human_reply']=True
        self.safety.pause_persisted=False
        self.assertEqual('human_reply_pause_unverified',self.run_task('one')['reason'])
        self.safety.pause_persisted=True
        self.assertEqual('human_reply',self.run_task('two')['reason'])
        self.assertEqual(2,self.safety.pause_calls)
        self.assertEqual(0,self.linkedin.calls)
    def test_reconciliation_reads_already_closed_before_mutation(self):
        self.apollo.fail_complete=True
        self.assertEqual('apollo_reconcile_pending',self.run_task()['state'])
        self.apollo.completed=True
        self.assertEqual('confirmed',self.coordinator.reconcile('co','task','linkedin_step_connect')['state'])
        self.assertEqual(1,self.linkedin.calls)
    def test_receipt_commit_failure_is_uncertain(self):
        self.store.fail_confirmation=True
        self.assertEqual('receipt_persistence_unverified',self.run_task()['reason'])
        self.assertFalse(self.apollo.completed)
        self.assertEqual('blocked',self.run_task()['state'])
        self.assertEqual(1,self.linkedin.calls)
    def test_lock_contenders_and_canary_consumed(self):
        self.assertTrue(self.store.lock.acquire(False))
        try:self.assertEqual('blocked',self.run_task()['state'])
        finally:self.store.lock.release()
        self.assertEqual('confirmed',self.run_task()['state'])
        self.apollo.completed=False
        self.assertEqual('canary_unavailable',self.run_task('second')['reason'])
        self.assertEqual(1,self.linkedin.calls)

if __name__ == '__main__': unittest.main()
