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

    def test_same_contact_in_different_accounts_stays_in_triage(self):
        self.repo.put_state('cadence','other-profile', {**self.cadence, 'account_id':'other.invalid'})
        result=self.control.ingest(event(),'verified_source')
        self.assertEqual(result['status'],'triage')
        self.assertFalse(result['reply']['identity_confirmed'])
        self.assertEqual(self.repo.get_state('cadence','cadence-1')['status'],'approved')
        self.assertEqual(self.repo.get_state('cadence','other-profile')['status'],'approved')

    def test_reply_without_matching_cadence_stays_in_triage(self):
        from thunder_os.service import ContractError, ThunderService
        result=self.control.ingest({**event(), 'hubspot_contact_id':'999'}, 'verified_source')
        self.assertEqual(result['status'],'triage')
        self.assertFalse(result['reply']['identity_confirmed'])
        self.assertEqual(result['reply']['state'],'identity_resolution')
        self.assertEqual(result['alerts'][0]['reason'],'identity_unresolved')
        self.assertEqual(self.repo.get_state('cadence','cadence-1')['status'],'approved')
        with self.assertRaisesRegex(ContractError,'reply identity unresolved'):
            ThunderService(self.repo).classify_reply(result['reply']['id'],'positive',1.0,'Synthetic','ygor')

    def test_resposta_instantly_pausa_linkedin(self):
        self.control.ingest({**event('instantly'),'channel':'email'},'verified_source')
        steps=self.repo.get_state('cadence','cadence-1')['steps']
        self.assertEqual([s['status'] for s in steps],['sent','skipped_reply','uncertain'])
        worker=object.__new__(WorkerRuntime)
        worker.repository=self.repo
        worker.authorization_service=SimpleNamespace(global_paused=lambda:False)
        self.assertEqual(worker._final_campaign_gate({'cadence_id':'cadence-1'},{}),'approval_not_active')

    def test_triage_replay_cannot_resolve_without_verified_correlation(self):
        from thunder_os.new_journey_checks import NewJourneyChecks
        from thunder_os.service import ContractError, ThunderService
        self.repo.put_state('identity', 'person@company.invalid', {
            'actor': 'ygor', 'status': 'canonical', 'canonical_person': 'person@company.invalid',
            'canonical_account': 'company.invalid', 'records': [
                {'person_key': 'person@company.invalid', 'hubspot_contact_id': '123',
                 'domain': 'company.invalid', 'source': source} for source in ('hubspot', 'fixture')]})
        checker = NewJourneyChecks(ThunderService(self.repo))
        row = {'source_key': 'fixture', 'email': 'person@company.invalid', 'company': 'Synthetic'}
        def refusal():
            return next(x for x in checker([row], 'BR-FIXTURE')[0]['checks'] if x['key'] == 'refusal')
        unresolved = event('apollo')  # Apollo cannot establish a LinkedIn sender.
        first = self.control.ingest(unresolved, 'verified_source')
        self.assertEqual(first['status'], 'triage')
        self.assertFalse(first['reply']['identity_confirmed'])
        self.assertEqual(self.repo.get_state('cadence', 'cadence-1')['status'], 'approved')
        self.assertNotEqual(refusal()['status'], 'blocked')
        self.assertTrue(self.control.ingest(unresolved, 'verified_source')['duplicate'])
        with self.assertRaisesRegex(ReplyControlError, 'reply_event_conflict'):
            self.control.ingest({**unresolved, 'content': 'Changed payload'}, 'verified_source')
        with self.assertRaisesRegex(ContractError, 'reply identity evidence unavailable'):
            ThunderService(self.repo).resolve_reply_identity(first['reply']['id'], {
                'account_id': 'synthetic.invalid', 'person_key': 'hubspot:123',
                'cadence_id': 'cadence-1', 'preceding_touch_id': 'sent'}, 'ygor')
        self.assertEqual(self.repo.get_state('cadence', 'cadence-1')['steps'], self.cadence['steps'])
        self.assertEqual(self.repo.get_state('reply', first['reply']['id'])['state'], 'identity_resolution')
        self.assertTrue(self.control.ingest(unresolved, 'verified_source')['duplicate'])
        with self.assertRaisesRegex(ContractError, 'reply identity unresolved'):
            ThunderService(self.repo).classify_reply(first['reply']['id'], 'negative', 1.0, 'Synthetic', 'ygor')

    def test_legacy_unresolved_reply_also_stays_unchanged(self):
        from thunder_os.service import ContractError, ThunderService
        service = ThunderService(self.repo)
        service.record_reply({'id': 'legacy', 'channel': 'email', 'content': 'Synthetic'}, 'ygor')
        with self.assertRaisesRegex(ContractError, 'reply identity evidence unavailable'):
            service.resolve_reply_identity('legacy', {'account_id': 'synthetic.invalid',
                'person_key': 'hubspot:123', 'cadence_id': 'cadence-1', 'preceding_touch_id': 'sent'}, 'ygor')
        self.assertEqual(self.repo.get_state('cadence', 'cadence-1')['steps'], self.cadence['steps'])

    def test_legacy_direct_reply_rejects_untrusted_actor_without_ledger(self):
        from thunder_os.service import ContractError, ThunderService
        self.repo.put_state('cadence', 'cadence-1', {**self.cadence,
            'steps': [{'id': 'future', 'status': 'approved', 'channel': 'email',
                       'recipient': 'person@synthetic.invalid'}]})
        service = ThunderService(self.repo)
        with self.assertRaisesRegex(ContractError, 'reply trusted human required'):
            service.record_reply({'id': 'synthetic-direct', 'channel': 'email',
                'content': 'Unproven source', 'cadence_id': 'cadence-1',
                'account_id': 'synthetic.invalid', 'person_key': 'hubspot:123',
                'preceding_touch_id': 'no-such-touch', 'classification': 'positive'}, 'untrusted-fixture')
        self.assertEqual(self.repo.get_state('cadence', 'cadence-1')['status'], 'approved')
        self.assertIsNone(self.repo.get_state('reply', 'synthetic-direct'))

    def test_inbox_actors_still_create_unresolved_replies(self):
        from thunder_os.service import ThunderService
        service = ThunderService(self.repo)
        for actor in ('gmail-inbox', 'connectsafely-inbox'):
            reply = service.record_reply({'id': actor, 'channel': 'email',
                                          'content': 'Synthetic'}, actor)
            self.assertEqual(reply['state'], 'identity_resolution')

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

    def test_malformed_pause_diagnostic_preserves_internal_stop(self):
        for index, status in enumerate(([], {}, True, 202)):
            with self.subTest(status=status):
                self.repo.put_state('cadence', 'cadence-1', deepcopy(self.cadence))
                control = ReplyControl(self.repo, self.registry,
                    external_adapters={'apollo': {'status': status}})
                value = event()
                value['source']['provider_message_id'] = 'malformed-' + str(index)
                result = control.ingest(value, 'verified_source')
                self.assertEqual(result['status'], 'paused_internal')
                self.assertEqual(self.repo.get_state('cadence', 'cadence-1')['status'], 'paused_reply')
                self.assertEqual(result['alerts'][0]['reason'], 'external_pause_failed')
                self.assertEqual(len(self.repo.list_state('reply')), index + 1)
                self.assertEqual(len(self.repo.list_state('operator_alert')), index + 1)
                self.assertFalse(result['external_pause_enabled'])
                self.assertEqual(self.repo.effects(), [])
                self.assertEqual(self.repo.outbox(), [])

    def test_unknown_auto_identity_and_account_do_not_pause(self):
        for change in ({'human_reply':False},{'human_reply':None},{'identity_verified':False},
                       {'hubspot_contact_id':None},{'source':{**event()['source'],'account_id':'other'}}):
            with self.subTest(change=change):
                value={**event(),**change}
                value['source']={**value['source'],'provider_message_id':str(len(self.repo.list_state('reply')))}
                self.assertEqual(self.control.ingest(value,'verified_source')['status'],'triage')
                self.assertEqual(self.repo.get_state('cadence','cadence-1')['status'],'approved')

    def test_provider_account_must_match_reply_channel(self):
        wrong_channel = {**event('instantly'), 'channel': 'linkedin'}
        result = self.control.ingest(wrong_channel, 'verified_source')
        self.assertEqual(result['status'], 'triage')
        self.assertEqual(self.repo.get_state('cadence', 'cadence-1')['status'], 'approved')
        self.assertEqual(result['alerts'][0]['reason'], 'identity_unresolved')

    def test_chattie_preserves_existing_registry_correlation(self):
        self.registry.register({**config(), 'campaign_id':'CHATTIE', 'platform':'chattie',
            'providers':{'linkedin':'chattie'}, 'configuration':{'flow_version':'1',
            'provider_accounts':{'linkedin':'synthetic-account'}}}, 'ygor', 0)
        value = {**event('chattie'), 'channel':'linkedin', 'campaign_id':'CHATTIE'}
        self.assertEqual(self.control.ingest(value, 'verified_source')['status'], 'paused_internal')

    def test_unproven_instantly_channels_do_not_correlate(self):
        for channel in ('whatsapp', 'chattie', 'phone'):
            with self.subTest(channel=channel):
                value = {**event('instantly'), 'channel': channel,
                         'source': {**event('instantly')['source'], 'provider_message_id':channel}}
                result = self.control.ingest(value, 'verified_source')
                self.assertEqual(result['status'], 'triage')
                self.assertEqual(result['alerts'][0]['reason'], 'identity_unresolved')
                self.assertEqual(self.repo.get_state('cadence','cadence-1')['status'], 'approved')

    def test_hubspot_non_email_and_wrong_account_do_not_correlate(self):
        self.registry.register({**config(), 'campaign_id':'CRM', 'platform':'apollo',
            'providers':{'email':'apollo', 'hubspot':'hubspot'}, 'configuration':{'flow_version':'1',
            'provider_accounts':{'email':'synthetic-account','hubspot':'crm-account'}}}, 'ygor', 0)
        for channel, account in (('linkedin', 'crm-account'), ('email', 'wrong-account')):
            with self.subTest(channel=channel, account=account):
                value = {**event('hubspot'), 'channel':channel, 'campaign_id':'CRM',
                         'source':{'provider':'hubspot','account_id':account,'provider_message_id':channel}}
                self.assertEqual(self.control.ingest(value, 'verified_source')['status'], 'triage')
                self.assertEqual(self.repo.get_state('cadence','cadence-1')['status'], 'approved')

    def test_evidenced_provider_channel_pairs_and_account_matching(self):
        for index, (channel, provider) in enumerate((('email','apollo'), ('email','instantly'),
                ('email','gmail'), ('email','hubspot'), ('linkedin','connectsafely'), ('linkedin','chattie'))):
            campaign = 'PAIR' + str(index)
            self.registry.register({**config(), 'campaign_id':campaign, 'platform':'apollo',
                'providers':{channel:provider}, 'configuration':{'flow_version':'1',
                'provider_accounts':{channel:'synthetic-account'}}}, 'ygor', 0)
            value = {**event(provider), 'channel':channel, 'campaign_id':campaign}
            with self.subTest(channel=channel, provider=provider):
                self.assertTrue(self.control._identity(value))
                self.assertFalse(self.control._identity({**value, 'source':{**value['source'], 'account_id':'wrong'}}))

    def test_hubspot_source_preserves_existing_registry_correlation(self):
        self.registry.register({**config(), 'campaign_id':'CRM', 'platform':'apollo',
            'providers':{'email':'apollo', 'hubspot':'hubspot'}, 'configuration':{'flow_version':'1',
            'provider_accounts':{'email':'synthetic-account','hubspot':'crm-account'}}}, 'ygor', 0)
        value = {**event('hubspot'), 'channel':'email', 'campaign_id':'CRM',
                 'source':{'provider':'hubspot','account_id':'crm-account','provider_message_id':'crm-1'}}
        self.assertEqual(self.control.ingest(value, 'verified_source')['status'], 'paused_internal')

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
