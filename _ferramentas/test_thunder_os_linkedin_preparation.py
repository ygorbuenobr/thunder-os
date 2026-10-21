"""Prepared LinkedIn scheduling only; no executor, provider or live quotas."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from thunder_os.campaign_registry import CampaignRegistry
from thunder_os.db import Repository
from thunder_os.linkedin_preparation import LinkedInPreparation, next_slot
from test_thunder_os_campaign_registry import config


def policy():
    return {'timezone':'America/Mexico_City','send_hour':9,'weekly_invite_limit':90,
            'holidays':['2026-11-16']}


class LinkedInPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo=Repository(Path(self.temp.name)/'isolated.db')
        self.registry=CampaignRegistry(self.repo)
        self.cfg={**config(),'campaign_id':'PILOT','providers':{'linkedin':'connectsafely'},
            'configuration':{'flow_version':'1','provider_accounts':{'linkedin':'synthetic-account'},
                             'linkedin_policy':policy()}}
        self.registry.register(self.cfg,'ygor',0)
        self.service=LinkedInPreparation(self.registry)
        self.now=datetime.fromisoformat('2026-10-21T14:00:00+00:00')
        self.quota={'account_id':'synthetic-account','scope':'all_tools','complete':True,
            'week_start':'2026-10-19','used':89,'observed_at':self.now.isoformat()}

    def test_teto_semanal_da_campanha(self):
        self.assertEqual(self.service.prepare('PILOT',self.now,self.quota)['status'],'ready_for_review')
        result=self.service.prepare('PILOT',self.now,{**self.quota,'used':90})
        self.assertEqual(result['status'],'pending_quota')
        self.assertFalse(result['executor_enabled'])
        self.assertEqual(self.repo.effects(),[])
        self.assertEqual(self.repo.outbox(),[])

    def test_fuso_mexico(self):
        self.assertEqual(next_slot(self.now,policy()).isoformat(),'2026-10-21T15:00:00+00:00')
        holiday=datetime.fromisoformat('2026-11-16T00:00:00+00:00')
        self.assertEqual(next_slot(holiday,policy()).isoformat(),'2026-11-17T15:00:00+00:00')

    def test_next_week_slot_cannot_use_this_weeks_quota(self):
        now=datetime.fromisoformat('2026-11-13T16:00:00+00:00')
        quota={**self.quota,'observed_at':now.isoformat(),'week_start':'2026-11-09'}
        result=self.service.prepare('PILOT',now,quota)
        self.assertEqual(result['due_at'],'2026-11-17T15:00:00+00:00')
        self.assertEqual(result['status'],'quota_unverified')
        self.assertEqual(self.service.prepare('PILOT',now,{**quota,'week_start':'2026-11-16'})['status'],'quota_unverified')

    def test_dst_gap_is_skipped_and_fold_never_precedes_anchor(self):
        zone=ZoneInfo('Africa/Cairo')
        midnight={**policy(),'timezone':'Africa/Cairo','send_hour':0}
        anchor=datetime.fromisoformat('2026-04-23T22:00:00+00:00')
        due=next_slot(anchor,midnight)
        self.assertEqual(due.astimezone(zone).isoformat(),'2026-04-27T00:00:00+03:00')
        self.assertGreaterEqual(due,anchor)
        fold_anchor=datetime(2026,10,29,23,tzinfo=zone,fold=1)
        due=next_slot(fold_anchor,{**midnight,'send_hour':23})
        self.assertEqual(due.isoformat(),'2026-10-29T21:00:00+00:00')
        self.assertGreaterEqual(due.timestamp(),fold_anchor.timestamp())

    def test_mudanca_de_registro_invalida_lote(self):
        old=self.service.prepare('PILOT',self.now,self.quota)['execution_context']
        updated=deepcopy(self.cfg)
        updated['configuration']['linkedin_policy']['weekly_invite_limit']=80
        self.registry.register(updated,'ygor',1)
        new=self.service.prepare('PILOT',self.now,self.quota)['execution_context']
        self.assertNotEqual(old['configuration_hash'],new['configuration_hash'])
        self.assertNotEqual(old,new)

    def test_unknown_partial_stale_or_wrong_account_quota_is_held(self):
        for changes in ({'complete':False},{'scope':'os_only'},{'used':None},{'used':True},
                        {'account_id':'other'},{'week_start':'2026-10-12'},
                        {'observed_at':'2026-10-21T13:00:00+00:00'}):
            with self.subTest(changes=changes):
                result=self.service.prepare('PILOT',self.now,{**self.quota,**changes})
                self.assertEqual(result['status'],'quota_unverified')
        self.assertEqual(self.service.prepare('PILOT',self.now,None)['status'],'quota_unverified')

    def test_no_policy_and_external_campaign_cannot_prepare_execution(self):
        old=config();old['campaign_id']='OLD'
        self.registry.register(old,'ygor',0)
        self.assertEqual(self.service.prepare('OLD',self.now,self.quota)['status'],'policy_missing')
        external=deepcopy(self.cfg);external['campaign_id']='EXTERNAL';external['platform']='codex_local'
        self.registry.register(external,'ygor',0)
        self.assertEqual(self.service.prepare('EXTERNAL',self.now,self.quota)['status'],'external_platform')
        with self.assertRaisesRegex(ValueError,'linkedin_executor_disabled'):
            self.service.execute(None)

    def test_invalid_policy_is_not_registered(self):
        for changes in ({'timezone':'Not/A_Zone'},{'timezone':'X'*10000},{'send_hour':24},{'weekly_invite_limit':True},
                        {'holidays':['2026-02-30']},{'holidays':['2026-11-16']*2}):
            row=deepcopy(self.cfg);row['configuration']['linkedin_policy'].update(changes)
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                self.registry.register(row,'ygor',1)
        self.assertEqual(self.registry.configuration('PILOT')['linkedin_policy'],policy())

if __name__=='__main__':unittest.main()
