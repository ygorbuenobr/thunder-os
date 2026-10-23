"""Offline operation approval tests; SQLite only, no provider transports."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import unittest

from thunder_os.approvals import ApprovalError
from thunder_os.campaign_registry import CampaignRegistry
from thunder_os.db import Repository
from thunder_os.operation_campaign import OperationCampaign
from test_campaign_sheet_support import make_sheet


class OperationCampaignTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Repository(Path(self.tmp.name)/'workflow.db')
        self.now = 1900000000.0
        self.registry = CampaignRegistry(self.repo)
        self.config = {'campaign_id':'synthetic', 'campaign_version':'1', 'rules_version':'1',
            'run_id':'run1', 'platform':'thunder_os', 'providers':{'email':'seqd'},
            'rules':'Synthetic only', 'configuration':{'flow_version':'1', 'provider_accounts':{'email':'mailbox1'}}}
        self.sheet_store, self.config['briefing'] = make_sheet(Path(self.tmp.name)/'brain')
        self.registry.register(self.config, 'ygor', 0)
        self.preview = self.repo.append_state('seqd_preview','p1', {
            'plan':{'to':'synthetic@example.invalid','body':'offline','subject':'test'},
            'content_hash':'a'*64,'state':'review_required','expires_at':self.now+300})
        self.gate = OperationCampaign(self.repo,'seqd','seqd','mailbox1','email',clock=lambda:self.now,
                                      sheet_store=self.sheet_store)

    def body(self):
        return {'preview_id':'p1','expected_version':1,
                'execution_confirmation':self.gate.review('p1','synthetic')}

    def approve(self):
        return self.gate.approve(self.body(),'ygor')

    def attempt(self):
        return self.repo.append_state('seqd_attempt','effect1',{'status':'uncertain','preview_id':'p1'})

    def test_exact_approval_persisted_and_does_not_modify_preview(self):
        record=self.approve()
        self.assertEqual(self.gate.validate(record),record['execution_confirmation']['request'])
        self.assertEqual(self.repo.get_state('seqd_preview','p1'),self.preview)
        self.assertEqual(record,self.gate.approve(self.body(),'ygor'))
        self.assertEqual(len(self.repo.list_state('operation_approval')),1)

    def test_missing_generic_wrong_human_and_stale_rejected(self):
        exact=self.body()
        for body in ({}, {**exact,'execution_confirmation':True},
                     {**exact,'execution_confirmation':{'confirmed':True}},
                     {**exact,'expected_version':2}, {**exact,'expected_version':True}):
            with self.subTest(body=body),self.assertRaises(ApprovalError):self.gate.approve(body,'ygor')
        with self.assertRaises(ApprovalError):self.gate.approve(exact,'hermes')
        self.assertEqual(self.repo.list_state('operation_approval'),[])

    def test_context_or_scope_change_is_not_a_confirmation(self):
        for path,value in [('batch_id','other'),('scope_hash','b'*64),('control_version',10)]:
            body=self.body();body['execution_confirmation'][path]=value
            with self.subTest(path=path),self.assertRaises(ApprovalError):self.gate.approve(body,'ygor')

    def test_revocation_configuration_and_pause_invalidate(self):
        record=self.approve()
        self.registry.revoke('synthetic','ygor',1)
        with self.assertRaises(ApprovalError):self.gate.validate(record)
        self.registry.register(self.config,'ygor',2)
        with self.assertRaises(ApprovalError):self.gate.validate(record)
        new_record=self.approve()
        self.repo.put_state('control','global',{'paused':True})
        with self.assertRaises(ApprovalError):self.gate.validate(new_record)
        self.repo.put_state('control','global',{'paused':False})
        with self.assertRaises(ApprovalError):self.gate.validate(new_record)

    def test_expiry_and_expiry_extension_are_denied(self):
        record=self.approve();self.now+=301
        with self.assertRaises(ApprovalError):self.gate.validate(record)
        self.now-=301
        self.repo.put_state('seqd_preview','p1',{**self.preview,'state':'attempted','expires_at':self.now+600})
        with self.assertRaises(ApprovalError):self.gate.validate(record)

    def test_bound_provider_account_platform(self):
        for provider,account,channel in [('other','mailbox1','email'),('seqd','other','email'),('seqd','mailbox1','linkedin')]:
            gate=OperationCampaign(self.repo,'seqd',provider,account,channel,clock=lambda:self.now)
            with self.subTest(provider=provider,account=account),self.assertRaises(ApprovalError):gate.review('p1','synthetic')
        self.registry.register({**self.config,'platform':'instantly'},'ygor',1)
        with self.assertRaises(ApprovalError):self.gate.review('p1','synthetic')

    def test_lifecycle_permitted_but_plan_and_unrelated_revision_denied(self):
        record=self.approve()
        self.repo.put_state('seqd_preview','p1',{**self.preview,'state':'attempted'})
        self.gate.validate(record)
        current=self.repo.get_state('seqd_preview','p1')
        self.repo.put_state('seqd_preview','p1',{**current,'state':'attempted'})
        with self.assertRaises(ApprovalError):self.gate.validate(record)

    def test_plan_hash_changed_and_forged_record_denied(self):
        record=self.approve();forged=deepcopy(record);forged['provider']='other'
        with self.assertRaises(ApprovalError):self.gate.validate(forged)
        self.repo.put_state('seqd_preview','p1',{**self.preview,'state':'attempted','content_hash':'changed'})
        with self.assertRaises(ApprovalError):self.gate.validate(record)

    def test_guard_requires_committed_attempt_and_refuses_outer_transaction(self):
        record=self.approve()
        with self.assertRaisesRegex(ApprovalError,'durable_attempt'):
            with self.gate.execution_guard(record,'seqd_attempt','effect1'):self.fail('entered')
        with self.repo.transaction():
            self.attempt()
            with self.assertRaisesRegex(ApprovalError,'committed_attempt'):
                with self.gate.execution_guard(record,'seqd_attempt','effect1'):self.fail('entered')
        other=Repository(self.repo.path)
        self.assertIsNotNone(other.get_state('seqd_attempt','effect1'))
        with self.gate.execution_guard(record,'seqd_attempt','effect1'):
            self.assertTrue(self.repo.dispatch_fence_active)
        self.assertFalse(self.repo.dispatch_fence_active)

    def test_attempt_for_another_preview_cannot_supply_durability(self):
        record=self.approve()
        self.repo.append_state('seqd_attempt','other',{'status':'uncertain','preview_id':'another'})
        with self.assertRaisesRegex(ApprovalError,'attempt_scope_changed'):
            with self.gate.execution_guard(record,'seqd_attempt','other'):self.fail('entered')

    def test_reentrant_registry_mutation_denied_during_guard(self):
        record=self.approve();self.attempt()
        with self.gate.execution_guard(record,'seqd_attempt','effect1'):
            for operation in (lambda:self.registry.register(self.config,'ygor',1),lambda:self.registry.revoke('synthetic','ygor',1)):
                with self.assertRaisesRegex(ApprovalError,'dispatch_in_progress'):operation()
            self.gate.validate(record)

    def test_concurrent_registry_writer_waits_until_guard_exits(self):
        record=self.approve();self.attempt()
        other=CampaignRegistry(Repository(self.repo.path));entered=threading.Event()
        def revoke():
            entered.set()
            return other.revoke('synthetic','ygor',1)
        with ThreadPoolExecutor(1) as pool:
            with self.gate.execution_guard(record,'seqd_attempt','effect1'):
                future=pool.submit(revoke)
                self.assertTrue(entered.wait(1))
                with self.assertRaises(TimeoutError):future.result(timeout=.1)
                self.gate.validate(record)
            self.assertFalse(future.result(timeout=2)['active'])
        with self.assertRaises(ApprovalError):self.gate.validate(record)

    def test_exception_keeps_preexisting_durable_attempt(self):
        record=self.approve();self.attempt()
        with self.assertRaisesRegex(RuntimeError,'mock-failure'):
            with self.gate.execution_guard(record,'seqd_attempt','effect1'):raise RuntimeError('mock-failure')
        self.assertIsNotNone(Repository(self.repo.path).get_state('seqd_attempt','effect1'))
        self.assertFalse(self.repo.dispatch_fence_active)

    def test_hubspot_and_connectsafely_preview_hash_shapes(self):
        for kind,channel,provider,account in [('hubspot','hubspot','hubspot','portal1'),('connectsafely','linkedin','connectsafely','account1')]:
            with self.subTest(kind=kind):
                cfg={**self.config,'campaign_id':kind,'providers':{channel:provider},
                     'configuration':{'flow_version':'1','provider_accounts':{channel:account}}}
                self.registry.register(cfg,'ygor',0)
                preview={'plan':{'plan_hash':'c'*64} if kind=='hubspot' else {'action':'dm'},
                         'expires_at':self.now+300,'state':'review_required'}
                if kind=='connectsafely':preview['review_hash']='c'*64
                self.repo.append_state(kind+'_preview','p1',preview)
                gate=OperationCampaign(self.repo,kind,provider,account,channel,clock=lambda:self.now,
                                       sheet_store=self.sheet_store)
                request=gate.review('p1',kind)
                record=gate.approve({'preview_id':'p1','expected_version':1,'execution_confirmation':request},'ygor')
                self.assertEqual(gate.validate(record),request)


if __name__=='__main__':unittest.main()
