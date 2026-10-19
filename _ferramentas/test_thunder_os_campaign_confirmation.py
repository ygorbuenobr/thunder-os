"""Offline tests of persisted campaign authority; synthetic identities only."""
import copy
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from thunder_os.approvals import (ApprovalError, ApprovalService, execution_request,
    confirm_execution, validate_execution)
from thunder_os.db import Repository, RepositoryConflict
from thunder_os.models import payload_hash
from thunder_os.service import ThunderService
from thunder_os.worker import WorkerRuntime, EffectCoordinator, EffectBlocked
from thunder_os.providers.result import ProviderResult


def context(**changes):
    return dict(platform='thunder_os', campaign_id='synthetic-mexico', campaign_version='1',
                rules_version='1', rules_hash='a'*64, configuration_hash='b'*64,
                run_id='synthetic-run-1', providers={'email': 'fake-email'}, **changes)


class Ledger:
    def __init__(self):
        self.calls = []
        self.rows = {}
        self.lock = threading.Lock()
        self.on_reserve = lambda: None

    def preflight(self, *args, **kwargs):
        return {}

    def reserve(self, key, step_id, step=None):
        with self.lock:
            self.calls.append(key)
            if key in self.rows:
                return False, self.rows[key]
            self.rows[key] = {'id': step_id, 'status': 'reserved'}
        self.on_reserve()
        return True, self.rows[key]

    def finish(self, key, status, receipt=None, error=None):
        self.rows[key].update(status=status, receipt_ref=receipt, error_code=error)
        return self.rows[key]


class CampaignConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Repository(Path(self.tmp.name)/'workflow.db')
        self.canonical = context()
        self.ledger = Ledger()
        self.service = ThunderService(self.repo, self.ledger,
            campaign_context_loader=lambda campaign: copy.deepcopy(self.canonical))
        self.research = {'version': 1, 'facts': [], 'account': {}, 'knowledge_source': 'synthetic'}
        self.repo.put_state('research', 'synthetic.invalid', self.research)
        self.service.current_research = lambda _: self.research
        self.step = {'id': 's1', 'recipient': 'synthetic@example.invalid', 'channel': 'email',
                     'content': 'Offline only', 'subject': 'Synthetic', 'order': 1, 'interval_hours': 0,
                     'due_at': (datetime.now(timezone.utc)-timedelta(hours=1)).isoformat(),
                     'predecessor_id': None, 'status': 'draft'}
        self.cadence = self.repo.put_state('cadence', 'batch-1', {
            'account_id': 'synthetic.invalid', 'person_key': 'synthetic@example.invalid',
            'objective': 'offline', 'steps': [self.step], 'status': 'ready_for_review',
            'research_hash': payload_hash({'version': 1, 'facts': []}),
            'execution_context': copy.deepcopy(self.canonical)})
        self.adapter = Mock()
        self.adapter.capability = 'fake'
        self.adapter.send.return_value = ProviderResult('s1', 'confirmed', receipt_ref='offline:1')

    def approve(self, request='exact'):
        cadence = self.repo.get_state('cadence', 'batch-1')
        if request == 'exact': request = self.service.campaign_execution_review(cadence)
        return self.service.approve_cadence('batch-1', 'ygor', expected_version=cadence['version'],
            review_hash=self.service.cadence_review_hash(cadence), execution_confirmation=request)

    def queue(self):
        return self.service.dispatch_step('batch-1', 's1', 'ygor')

    def worker(self, worker_id='offline-worker', provider='fake-email'):
        worker = WorkerRuntime(self.repo, self.ledger, {'email': self.adapter}, worker_id=worker_id,
            health_path=Path(self.tmp.name)/(worker_id+'.json'), revision='offline',
            campaign_context_loader=lambda _: copy.deepcopy(self.canonical),
            adapter_provider_ids={'email': provider})
        worker.authorization_service.current_research = lambda _: self.research
        return worker

    def test_clear_context_without_confirmation_is_denied(self):
        with self.assertRaisesRegex(ApprovalError, 'campaign_confirmation_required'):
            self.approve(None)
        self.assertEqual(self.repo.effects(), [])
        self.assertIsNone(self.repo.get_state('approval', 'batch-1'))

    def test_generic_and_other_scope_approvals_are_denied(self):
        exact = self.service.campaign_execution_review(self.cadence)
        variants = [True, {'confirmed': True}, 'aprovado']
        for field, value in [('platform','instantly'), ('campaign_id','other'),
                              ('campaign_version','2'), ('rules_version','2'),
                              ('rules_hash','c'*64), ('configuration_hash','c'*64),
                              ('run_id','restart')]:
            bad = copy.deepcopy(exact); bad['context'][field] = value; variants.append(bad)
        for field,value in [('scope_hash','d'*64),('batch_id','other'),('batch_version',2)]:
            bad=copy.deepcopy(exact);bad[field]=value;variants.append(bad)
        for bad in variants:
            with self.subTest(bad=bad), self.assertRaises(ApprovalError): self.approve(bad)
        self.assertEqual(self.repo.effects(), [])

    def test_expired_confirmation_blocks_dispatch(self):
        approval = self.approve()
        record = copy.deepcopy(approval['execution_confirmation'])
        record['expires_at'] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        self.repo.put_state('approval','batch-1',{**approval,'execution_confirmation':record})
        with self.assertRaisesRegex(ApprovalError,'expired'): self.queue()
        self.assertEqual(self.repo.effects(), [])

    def test_rule_version_configuration_and_provider_changes_block_queued_effect(self):
        for field,value in [('campaign_version','2'),('rules_hash','c'*64),
                            ('configuration_hash','d'*64),('providers',{'email':'other'})]:
            with self.subTest(field=field):
                self.setUp()
                self.approve();self.queue();self.canonical[field]=value
                self.assertTrue(self.worker().process_one())
                self.adapter.send.assert_not_called()
                self.assertEqual(self.ledger.calls, [])

    def test_actual_adapter_provider_mismatch_blocks(self):
        self.approve();self.queue();self.worker(provider='instantly').process_one()
        self.adapter.send.assert_not_called();self.assertEqual(self.ledger.calls,[])

    def test_pause_resume_requires_new_confirmation(self):
        self.approve()
        self.service.set_global_pause(True,'ygor');self.service.set_global_pause(False,'ygor')
        with self.assertRaisesRegex(ApprovalError,'scope_changed'): self.queue()
        self.approve();self.queue();self.worker().process_one()
        self.adapter.send.assert_called_once()

    def test_scope_correct_confirmation_queues_and_mock_dispatches_once(self):
        self.approve();self.queue();self.queue()
        self.assertEqual(len(self.repo.effects()),1)
        self.adapter.send.assert_not_called() # Approval/queue have no provider side effect.
        worker=self.worker();self.assertTrue(worker.process_one());self.assertFalse(worker.process_one())
        self.adapter.send.assert_called_once()
        self.assertEqual(self.repo.effects()[0]['state'],'sent')

    def test_parallel_workers_do_not_duplicate_confirmed_effect(self):
        self.approve();self.queue()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda n:self.worker('offline-'+str(n)).process_one(),range(2)))
        self.adapter.send.assert_called_once()

    def test_parallel_unconfirmed_jobs_never_send(self):
        # Even forged legacy jobs with a valid payload hash are not confirmations.
        from thunder_os.approvals import _step_hash
        self.repo.enqueue_effect(_step_hash(self.service._approval_step(self.step)),
            {'cadence_id':'batch-1','step':self.step,'actor':'ygor',
             'approval_hash':_step_hash(self.service._approval_step(self.step))})
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda n:self.worker('unconfirmed-'+str(n)).process_one(),range(2)))
        self.adapter.send.assert_not_called();self.assertEqual(self.ledger.calls,[])

    def test_revalidate_after_ledger_reservation(self):
        self.approve();self.queue()
        self.ledger.on_reserve=lambda:self.canonical.update(rules_version='changed')
        self.worker().process_one();self.adapter.send.assert_not_called()
        self.assertEqual(len(self.ledger.calls),1)
        self.assertEqual(self.repo.effects()[0]['state'],'needs_reconciliation')

    def test_rules_changed_during_final_preflight_never_send(self):
        self.approve();self.queue()
        calls = []
        def preflight(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:self.canonical['rules_version'] = 'during-preflight'
            return {}
        self.ledger.preflight=preflight
        self.worker().process_one();self.adapter.send.assert_not_called()
        self.assertEqual(self.repo.effects()[0]['state'],'needs_reconciliation')

    def test_malformed_loader_context_is_terminal_without_send(self):
        self.approve();self.queue();self.canonical['platform']=[]
        self.assertTrue(self.worker().process_one())
        self.adapter.send.assert_not_called();self.assertEqual(self.ledger.calls,[])
        self.assertEqual(self.repo.effects()[0]['error_code'],'campaign_context_unavailable')

    def test_human_review_question_and_scope_are_explicit(self):
        view=self.service.campaign_review_view(self.cadence)
        self.assertIn('Thunder OS',view['execution_question'])
        self.assertIn('synthetic-mexico',view['execution_question'])
        self.assertEqual(view['execution_confirmation']['batch_id'],'batch-1')
        self.service.campaign_context_loader=None
        self.assertIsNone(self.service.campaign_review_view(self.cadence)['execution_confirmation'])

    def test_new_confirmation_does_not_reauthorize_old_queue(self):
        self.approve();self.queue()
        self.service.set_global_pause(True,'ygor');self.service.set_global_pause(False,'ygor')
        self.approve()
        self.worker().process_one();self.adapter.send.assert_not_called()

    def test_legacy_missing_context_and_missing_loader_are_safe(self):
        legacy={**self.cadence};legacy.pop('execution_context')
        self.repo.put_state('cadence','batch-1',legacy)
        with self.assertRaisesRegex(ApprovalError,'context_required'):self.approve()
        self.repo.put_state('cadence','batch-1',self.cadence)
        self.service.campaign_context_loader=None
        with self.assertRaisesRegex(ApprovalError,'loader_unconfigured'):self.approve()

    def test_payload_only_coordinator_cannot_bypass_campaign_gate(self):
        coordinator=EffectCoordinator(self.ledger,ApprovalService(lambda:False),{'email':self.adapter})
        with self.assertRaisesRegex(EffectBlocked,'integration_required'):
            coordinator.execute_due(self.step,None,datetime.now(timezone.utc))
        self.adapter.send.assert_not_called();self.assertEqual(self.ledger.calls,[])

    def test_added_recipient_is_not_covered_by_confirmation(self):
        self.approve()
        c=self.repo.get_state('cadence','batch-1')
        c['steps'].append({**self.step,'id':'s2','recipient':'other@example.invalid'})
        self.repo.put_state('cadence','batch-1',c)
        with self.assertRaises(ApprovalError):self.queue()
        self.assertEqual(self.repo.effects(),[])


if __name__ == '__main__':unittest.main()
