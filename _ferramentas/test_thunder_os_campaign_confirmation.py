"""Offline tests of persisted campaign authority; synthetic identities only."""
import copy
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

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
        self.config = {k:v for k,v in context().items() if k not in {'rules_hash','configuration_hash'}}
        self.config.update(rules='Synthetic rules only',configuration={
            'flow_version':'offline-v1','provider_accounts':{'email':'offline-mailbox'}})
        self.ledger = Ledger()
        self.service = ThunderService(self.repo, self.ledger)
        self.service.resolve_identity({'id': 'confirmed-person', 'records': [
            {'domain': 'synthetic.invalid', 'person_key': 'synthetic@example.invalid', 'source': 'fixture:crm'},
            {'domain': 'synthetic.invalid', 'person_key': 'synthetic@example.invalid', 'source': 'fixture:mailbox'},
        ]}, 'ygor')
        self.registry_row = self.service.configure_campaign(self.config,'ygor',expected_version=0)
        self.canonical = self.registry_row['context']
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
        self.adapter.provider_name='fake-email';self.adapter.account_id='offline-mailbox'
        self.adapter.send.return_value = ProviderResult('s1', 'confirmed', receipt_ref='offline:1')

    def change_config(self, **changes):
        self.config.update(changes)
        self.registry_row=self.service.configure_campaign(self.config,'ygor',expected_version=self.registry_row['version'])
        self.canonical=self.registry_row['context']

    def revoke(self):
        self.registry_row=self.service.revoke_campaign(self.config['campaign_id'],'ygor',expected_version=self.registry_row['version'])

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
            adapter_provider_ids={'email': provider}, adapter_account_ids={'email':'offline-mailbox'})
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
        for field,value in [('campaign_version','2'),('rules','Changed rules'),
                            ('configuration',{'flow_version':'v2','provider_accounts':{'email':'offline-mailbox'}}),('providers',{'email':'other'})]:
            with self.subTest(field=field):
                self.setUp()
                self.approve();self.queue();self.change_config(**{field:value})
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

    def assert_external_campaign_cannot_execute(self, platform, campaign_id):
        cfg = {**self.config, 'campaign_id': campaign_id, 'platform': platform}
        registered = self.service.configure_campaign(cfg, 'ygor', expected_version=0)
        canonical = self.service.campaign_registry.load(campaign_id)
        self.assertEqual(canonical, registered['context'])
        self.assertEqual(registered['authorized_by'], 'ygor')
        cadence = self.repo.put_state('cadence', 'batch-1', {
            **self.cadence, 'execution_context': canonical})
        now = datetime.now(timezone.utc)
        pack = ApprovalService(lambda: False).approve_pack(
            cadence['id'], cadence['version'], [self.service._approval_step(self.step)],
            'ygor', now + timedelta(hours=1), now)
        request = execution_request(canonical, cadence['id'], cadence['version'],
                                    dict(pack.step_hashes), 0)
        with self.assertRaisesRegex(ApprovalError, '^campaign_context_changed$'):
            self.approve(request)
        self.assertIsNone(self.repo.get_state('approval', 'batch-1'))

        # Persist an otherwise valid external approval fixture: denial must be
        # the OS platform boundary, not missing approval, stale hash or expiry.
        confirmation = confirm_execution(request, request, 'ygor', pack.expires_at, now)
        validate_execution(confirmation, request, now, platform=platform)
        approval = self.repo.put_state('approval', 'batch-1', {
            'actor': 'ygor', 'status': 'active', 'cadence_version': cadence['version'],
            'step_ids': list(pack.step_ids), 'step_hashes': dict(pack.step_hashes),
            'expires_at': pack.expires_at.isoformat(), 'execution_confirmation': confirmation})
        approved = self.repo.put_state('cadence', 'batch-1', {
            **cadence, 'status': 'approved', 'steps': [{**self.step, 'status': 'approved'}]})
        self.assertEqual(approved['version'], approval['cadence_version'] + 1)
        with self.assertRaisesRegex(ApprovalError, '^campaign_context_changed$'):
            self.queue()
        self.assertEqual(self.repo.effects(), [])

        # A job already in the outbox must be rejected by the actual worker too.
        effect_hash = dict(pack.step_hashes)['s1']
        self.repo.enqueue_effect(effect_hash, {
            'cadence_id': 'batch-1', 'step': approved['steps'][0], 'actor': 'ygor',
            'approval_hash': effect_hash, 'execution_confirmation_hash': payload_hash(confirmation)})
        self.assertTrue(self.worker().process_one())
        self.assertFalse(self.worker().process_one())
        self.assertEqual(self.repo.effects()[0]['state'], 'failed')
        self.assertEqual(self.repo.effects()[0]['error_code'], 'campaign_context_changed')
        self.adapter.send.assert_not_called()
        self.assertEqual(self.ledger.calls, [])

    def test_registry_plataforma_apollo(self):
        self.assert_external_campaign_cannot_execute('apollo', 'BR-APOLLO-01')

    def test_other_external_platforms_cannot_execute(self):
        for platform in ('hubspot_seq', 'codex_local', 'chattie', 'instantly'):
            with self.subTest(platform=platform):
                self.setUp()
                self.assert_external_campaign_cannot_execute(platform, 'synthetic-' + platform)

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
        self.ledger.on_reserve=lambda:self.change_config(rules_version='changed')
        self.worker().process_one();self.adapter.send.assert_not_called()
        self.assertEqual(len(self.ledger.calls),1)
        self.assertEqual(self.repo.effects()[0]['state'],'needs_reconciliation')

    def test_rules_changed_during_final_preflight_never_send(self):
        self.approve();self.queue()
        calls = []
        def preflight(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:self.change_config(rules_version='during-preflight')
            return {}
        self.ledger.preflight=preflight
        self.worker().process_one();self.adapter.send.assert_not_called()
        self.assertEqual(self.repo.effects()[0]['state'],'needs_reconciliation')

    def test_malformed_loader_context_is_terminal_without_send(self):
        self.approve();self.queue()
        row=self.repo.get_state('campaign_config',self.config['campaign_id'])
        row['context']['platform']=[]
        self.repo.put_state('campaign_config',self.config['campaign_id'],row)
        self.assertTrue(self.worker().process_one())
        self.adapter.send.assert_not_called();self.assertEqual(self.ledger.calls,[])
        self.assertEqual(self.repo.effects()[0]['error_code'],'campaign_context_unavailable')

    def test_human_review_question_and_scope_are_explicit(self):
        view=self.service.campaign_review_view(self.cadence)
        self.assertIn('Thunder OS',view['execution_question'])
        self.assertIn('synthetic-mexico',view['execution_question'])
        self.assertEqual(view['execution_confirmation']['batch_id'],'batch-1')
        self.revoke()
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
        self.revoke()
        with self.assertRaisesRegex(ApprovalError,'context_unavailable'):self.approve()

    def test_payload_only_coordinator_cannot_bypass_campaign_gate(self):
        coordinator=EffectCoordinator(self.ledger,ApprovalService(lambda:False),{'email':self.adapter})
        with self.assertRaisesRegex(EffectBlocked,'integration_required'):
            coordinator.execute_due(self.step,None,datetime.now(timezone.utc))
        self.adapter.send.assert_not_called();self.assertEqual(self.ledger.calls,[])

    def test_revocation_after_queue_blocks_before_reservation(self):
        self.approve();self.queue();self.revoke()
        self.worker().process_one()
        self.adapter.send.assert_not_called();self.assertEqual(self.ledger.calls,[])

    def test_expiration_between_queue_and_worker_blocks(self):
        approval=self.approve()
        expiry=datetime.now(timezone.utc)+timedelta(seconds=10)
        approval['execution_confirmation']['expires_at']=expiry.isoformat()
        self.repo.put_state('approval','batch-1',approval)
        self.queue()
        class Later(datetime):
            @classmethod
            def now(cls,tz=None):return expiry+timedelta(seconds=1)
        with patch('thunder_os.service.datetime',Later):self.worker().process_one()
        self.adapter.send.assert_not_called();self.assertEqual(self.ledger.calls,[])
        self.assertEqual(self.repo.effects()[0]['error_code'],'campaign_confirmation_expired')

    def test_configuration_and_revocation_are_serialized_with_dispatch(self):
        for operation in ('configure','revoke'):
            with self.subTest(operation=operation):
                self.setUp();self.approve();self.queue()
                sending=threading.Event();release=threading.Event();writer_started=threading.Event()
                initial=copy.deepcopy(self.canonical)
                def send(effect):
                    sending.set()
                    if not release.wait(3):raise RuntimeError('test synchronization timeout')
                    self.assertEqual(self.service.campaign_registry.load(self.config['campaign_id']),initial)
                    return ProviderResult('s1','confirmed',receipt_ref='offline:fenced')
                self.adapter.send.side_effect=send
                other=ThunderService(Repository(self.repo.path))
                def change():
                    writer_started.set()
                    if operation=='configure':
                        return other.configure_campaign({**self.config,'rules':'new rules'},'ygor',expected_version=1)
                    return other.revoke_campaign(self.config['campaign_id'],'ygor',expected_version=1)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    work=pool.submit(self.worker().process_one)
                    self.assertTrue(sending.wait(2))
                    write=pool.submit(change);self.assertTrue(writer_started.wait(2))
                    try:
                        with self.assertRaises(FutureTimeout):write.result(timeout=0.1)
                    finally:release.set()
                    self.assertTrue(work.result(timeout=3));changed=write.result(timeout=3)
                self.adapter.send.assert_called_once()
                self.assertEqual(self.repo.effects()[0]['state'],'sent')
                self.assertEqual(changed['version'],2)

    def test_expired_lease_cannot_be_reclaimed_during_fenced_send(self):
        self.approve();self.queue()
        def send(effect):
            # Simulates time crossing lease expiry while lock is held. A different
            # connection cannot claim until the transaction commits terminal state.
            with self.repo.transaction() as con:
                con.execute("UPDATE thunder_effect_jobs SET lease_until=?",
                    ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),))
            return ProviderResult('s1','confirmed',receipt_ref='offline:lease')
        self.adapter.send.side_effect=send
        self.worker().process_one()
        self.assertEqual(self.repo.effects()[0]['state'],'sent')
        self.assertFalse(self.worker('second').process_one())
        self.adapter.send.assert_called_once()

    def test_deferred_job_cannot_open_fence_without_owner(self):
        self.approve();self.queue()
        job=self.repo.claim_effect('test',datetime.now(timezone.utc),timedelta(seconds=30))
        self.repo.defer_effect(job['id'],job['lease_owner'],'test')
        for owner in (None,''):
            with self.subTest(owner=owner),self.assertRaisesRegex(RepositoryConflict,'owner required'):
                with self.repo.effect_dispatch_fence(job['id'],owner):self.fail('must not enter')
        self.adapter.send.assert_not_called()

    def test_wrong_claimant_cannot_open_dispatch_fence(self):
        self.approve();self.queue()
        job=self.repo.claim_effect('test',datetime.now(timezone.utc),timedelta(seconds=30))
        with self.assertRaisesRegex(RepositoryConflict,'lease lost'):
            with self.repo.effect_dispatch_fence(job['id'],'different-owner'):self.fail('must not enter')
        self.adapter.send.assert_not_called()

    def test_unknown_provider_exception_commits_uncertainty_without_retry(self):
        self.approve();self.queue();self.adapter.send.side_effect=TimeoutError('offline')
        self.worker().process_one()
        self.assertEqual(self.repo.effects()[0]['state'],'uncertain')
        self.assertFalse(self.worker('second').process_one())
        self.adapter.send.assert_called_once()

    def test_wrong_provider_account_blocks(self):
        self.approve();self.queue();worker=self.worker()
        worker.adapter_account_ids={'email':'other-account'}
        worker.process_one();self.adapter.send.assert_not_called()
        self.assertEqual(self.repo.effects()[0]['error_code'],'campaign_account_mismatch')

    def test_miswired_adapter_cannot_use_trusted_binding_labels(self):
        self.approve();self.queue();self.adapter.provider_name='wrong-provider'
        self.worker().process_one();self.adapter.send.assert_not_called()
        self.assertEqual(self.repo.effects()[0]['error_code'],'campaign_adapter_identity_unverified')

    def test_legacy_like_and_retry_routes_remain_explicitly_blocked(self):
        with self.assertRaisesRegex(ApprovalError,'integration_required'):
            self.service.dispatch_like({'id':'synthetic'},'ygor')
        self.approve();job=self.queue()
        # A legacy safe-retry label must not silently restart a commercial batch.
        with self.repo.transaction() as con:
            con.execute("UPDATE thunder_effect_jobs SET state='failed',error_code='provider_rejected' WHERE id=?",(job['id'],))
        with self.assertRaisesRegex(ApprovalError,'restart_confirmation_required'):
            self.service.retry_effect(job['id'],'ygor')
        self.adapter.send.assert_not_called()

    def test_meeting_mirror_is_blocked_before_external_call(self):
        hubspot = Mock()
        self.service.hubspot = hubspot
        with self.assertRaisesRegex(ApprovalError, 'campaign_confirmation_integration_required'):
            self.service.record_meeting({'id':'offline-meeting','account_id':'synthetic.invalid',
                'person_key':'synthetic@example.invalid','occurred_at':datetime.now(timezone.utc).isoformat(),
                'hubspot_effect':{}}, 'ygor')
        hubspot.mirror.assert_not_called()
        self.assertEqual(self.repo.get_state('meeting','offline-meeting')['status'],'mirror_pending')

    def test_blocked_legacy_application_does_not_starve_confirmed_core(self):
        from thunder_os.connectsafely_console import ConnectSafelyError
        app=Mock(); app.process_one.side_effect=ConnectSafelyError('campaign_confirmation_required')
        self.approve();self.queue();worker=self.worker();worker.connectsafely_app=app
        self.assertTrue(worker.process_one());self.adapter.send.assert_called_once()
        app.process_one.assert_called_once()
        health=__import__('json').loads(worker.health_path.read_text())
        self.assertIs(health['campaign_confirmation_required'], True)

    def test_added_recipient_is_not_covered_by_confirmation(self):
        self.approve()
        c=self.repo.get_state('cadence','batch-1')
        c['steps'].append({**self.step,'id':'s2','recipient':'other@example.invalid'})
        self.repo.put_state('cadence','batch-1',c)
        with self.assertRaises(ApprovalError):self.queue()
        self.assertEqual(self.repo.effects(),[])


if __name__ == '__main__':unittest.main()
