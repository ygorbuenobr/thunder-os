"""Synthetic T4 trajectories; no provider transport or production state."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from thunder_os.campaign_registry import CampaignRegistry
from thunder_os.db import Repository
from thunder_os.reply_control import ReplyControl, ReplyControlError, external_pause_plan
from thunder_os.worker import WorkerRuntime
from test_thunder_os_campaign_registry import config


def event(provider='connectsafely'):
    return {'id':'message-1','channel':'linkedin','content':'Synthetic human reply',
            'received_at':'2026-10-21T12:00:00+00:00','hubspot_contact_id':'123',
            'campaign_id':'PILOT','human_reply':True,'identity_verified':True,
            'source':{'provider':provider,'account_id':'synthetic-account',
                      'provider_message_id':'message-1'}}


class ReplyControlTests(unittest.TestCase):
    def setUp(self):
        scratch=Path(__file__).resolve().parents[1]/'.scratch'/'task4-replies'
        scratch.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.repo=Repository(Path(self.temp.name)/'isolated.db')
        self.registry=CampaignRegistry(self.repo)
        self.registry.register({**config(),'campaign_id':'PILOT','platform':'apollo',
            'providers':{'email':'instantly','linkedin':'connectsafely'},
            'configuration':{'flow_version':'1','provider_accounts':{
                'email':'synthetic-account','linkedin':'synthetic-account'}}},'ygor',0)
        self.cadence={'hubspot_contact_id':'123','person_key':'hubspot:123',
            'account_id':'synthetic.invalid','status':'approved','steps':[
                {'id':'sent','status':'sent'},{'id':'future','status':'approved'},
                {'id':'uncertain','status':'uncertain'}]}
        self.repo.put_state('cadence','cadence-1',deepcopy(self.cadence))
        self.repo.put_state('approval','cadence-1',{'status':'active'})
        self.control=ReplyControl(self.repo,self.registry)
        guard=patch('http.client.HTTPSConnection',side_effect=AssertionError('network forbidden'))
        guard.start();self.addCleanup(guard.stop)

    def test_resposta_linkedin_pausa_apollo(self):
        result=self.control.ingest(event(),'verified_source')
        self.assertEqual(result['status'],'paused_internal')
        self.assertFalse(result['external_pause_enabled'])
        self.assertEqual(self.repo.get_state('cadence','cadence-1')['status'],'paused_reply')
        self.assertEqual(result['alerts'][0]['reason'],'external_pause_off')
        self.assertEqual(external_pause_plan('apollo')['operation'],'remove_or_stop_contact_ids')
        self.assertFalse(external_pause_plan('apollo')['paused'])

    def test_resposta_instantly_pausa_linkedin(self):
        self.control.ingest({**event('instantly'),'channel':'email'},'verified_source')
        steps=self.repo.get_state('cadence','cadence-1')['steps']
        self.assertEqual([s['status'] for s in steps],['sent','skipped_reply','uncertain'])
        worker=object.__new__(WorkerRuntime)
        worker.repository=self.repo
        worker.authorization_service=SimpleNamespace(global_paused=lambda:False)
        self.assertEqual(worker._final_campaign_gate({'cadence_id':'cadence-1'},{}),'approval_not_active')

    def test_marcar_respondeu_manual(self):
        manual={**event('manual'),'channel':'phone','id':'manual-request-1',
                'source':{'provider':'manual','account_id':'ygor','provider_message_id':'request-1'}}
        first=self.control.ingest(manual,'ygor')
        second=self.control.ingest({**manual,'received_at':'2026-10-22T12:00:00+00:00'},'ygor')
        self.assertFalse(first['duplicate']);self.assertTrue(second['duplicate'])
        self.assertEqual(len(self.repo.list_state('reply')),1)
        self.assertEqual(len(self.repo.list_state('operator_alert')),1)
        with self.assertRaisesRegex(ReplyControlError,'reply_event_conflict'):
            self.control.ingest({**manual,'content':'Changed payload'},'ygor')

    def test_falha_pausa_externa_alerta(self):
        control=ReplyControl(self.repo,self.registry,external_adapters={'apollo':{'status':'failed'}})
        result=control.ingest(event(),'verified_source')
        alert=result['alerts'][0]
        self.assertEqual(alert['priority'],'high')
        self.assertEqual(alert['reason'],'external_pause_failed')
        self.assertTrue(alert['active'])
        self.assertEqual(alert['content'],event()['content'])
        self.assertFalse(result['external_pause_enabled'])
        self.assertEqual(self.repo.outbox(),[])

    def test_unknown_auto_identity_and_account_do_not_pause(self):
        for change in ({'human_reply':False},{'human_reply':None},{'identity_verified':False},
                       {'hubspot_contact_id':None},{'source':{**event()['source'],'account_id':'other'}}):
            with self.subTest(change=change):
                value={**event(),**change}
                value['source']={**value['source'],'provider_message_id':str(len(self.repo.list_state('reply')))}
                self.assertEqual(self.control.ingest(value,'verified_source')['status'],'triage')
                self.assertEqual(self.repo.get_state('cadence','cadence-1')['status'],'approved')

    def test_atomic_failure_rolls_back_pause_reply_and_alert(self):
        original=self.repo.append_state
        def fail_alert(kind,*args,**kwargs):
            if kind=='operator_alert':raise RuntimeError('synthetic disk failure')
            return original(kind,*args,**kwargs)
        with patch.object(self.repo,'append_state',side_effect=fail_alert):
            with self.assertRaisesRegex(RuntimeError,'synthetic disk failure'):
                self.control.ingest(event(),'verified_source')
        self.assertEqual(self.repo.get_state('cadence','cadence-1')['status'],'approved')
        self.assertEqual(self.repo.list_state('reply'),[])
        self.assertEqual(self.repo.list_state('operator_alert'),[])

    def test_outer_transaction_is_rejected_before_any_state_mutation(self):
        with self.repo.transaction():
            with self.assertRaisesRegex(ReplyControlError,'reply_outer_transaction_blocked'):
                self.control.ingest(event(),'verified_source')
        self.assertEqual(self.repo.get_state('cadence','cadence-1')['status'],'approved')
        self.assertEqual(self.repo.list_state('reply'),[])
        self.assertEqual(self.repo.list_state('operator_alert'),[])

    def test_concurrent_replay_has_one_reply_and_alert(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.control.ingest(event(),'verified_source'),range(2)))
        self.assertEqual(sum(r['duplicate'] for r in results),1)
        self.assertEqual(len(self.repo.list_state('reply')),1)
        self.assertEqual(len(self.repo.list_state('operator_alert')),1)

    def test_scope_does_not_pause_another_contact_or_change_crm(self):
        self.repo.put_state('cadence','another',{**self.cadence,'hubspot_contact_id':'124','person_key':'hubspot:124'})
        self.control.ingest(event(),'verified_source')
        self.assertEqual(self.repo.get_state('cadence','another')['status'],'approved')
        self.assertEqual(self.repo.effects(),[])
        self.assertEqual(self.repo.outbox(),[])

    def test_terminal_cadence_is_preserved_and_explicit_id_needs_no_name(self):
        self.repo.put_state('cadence','historical',{**self.cadence,'status':'completed'})
        missing={k:v for k,v in self.cadence.items() if k!='person_key'}
        self.repo.put_state('cadence','without-person-key',missing)
        self.control.ingest(event(),'verified_source')
        self.assertEqual(self.repo.get_state('cadence','historical')['status'],'completed')
        self.assertEqual(self.repo.get_state('cadence','without-person-key')['status'],'paused_reply')

    def test_activation_and_untrusted_actor_rejected(self):
        with self.assertRaisesRegex(ReplyControlError,'external_pause_activation_not_authorized'):
            ReplyControl(self.repo,self.registry,external_pause_enabled=True)
        with self.assertRaises(ReplyControlError):self.control.ingest(event(),'hermes')
        with self.assertRaises(ReplyControlError):self.control.ingest(event(),'ygor')
        self.assertEqual(self.repo.list_state('reply'),[])

    def test_invalid_channel_types_are_rejected_without_write(self):
        for channel in ([],{},None,True):
            with self.subTest(channel=channel),self.assertRaisesRegex(ReplyControlError,'invalid_reply_channel'):
                self.control.ingest({**event(),'channel':channel},'verified_source')
        self.assertEqual(self.repo.list_state('reply'),[])

    def test_async_acceptance_is_not_provider_pause(self):
        control=ReplyControl(self.repo,self.registry,external_adapters={'apollo':{'http_status':202}})
        result=control.ingest(event(),'verified_source')
        self.assertEqual(result['alerts'][0]['reason'],'external_pause_readback_required')
        self.assertFalse(result['external_pause_enabled'])
        for provider in ('instantly','hubspot_seq'):
            self.assertEqual(external_pause_plan(provider)['reason'],'pause_capability_unverified')

    def test_snapshot_does_not_write(self):
        with (patch.object(self.repo,'put_state',side_effect=AssertionError('write')),
              patch.object(self.repo,'append_state',side_effect=AssertionError('write'))):
            state=self.control.snapshot()
        self.assertFalse(state['automatic_reply_reader_connected'])
        self.assertFalse(state['external_pause_enabled'])


if __name__=='__main__':unittest.main()
