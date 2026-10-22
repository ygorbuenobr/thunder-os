"""Prepared LinkedIn scheduling only; no executor, provider or live quotas."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from thunder_os.campaign_registry import CampaignRegistry
from thunder_os.db import Repository
from thunder_os.linkedin_preparation import LinkedInPreparation, next_slot, validate_policy
from test_thunder_os_campaign_registry import config


def policy():
    return {'timezone':'America/Mexico_City','send_hour':9,'weekly_invite_limit':90,
            'holidays':['2026-11-16']}


def window_policy():
    return {'timezone':'lead','send_window':{'start_hour':7,'end_hour':21},
            'weekly_invite_limit':None,'holiday_countries':['BR','MX'],'holidays':[]}


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

    def test_provider_refusal_holds_legacy_policy(self):
        result=self.service.prepare('PILOT',self.now,{**self.quota,'http_status':429})
        self.assertEqual(result['status'],'provider_blocked')
        self.assertIn('provider_blocked',result['hold_reasons'])
        self.assertFalse(result['executor_enabled'])

class LinkedInWindowTests(unittest.TestCase):
    setUp=LinkedInPreparationTests.setUp

    def register_window(self, campaign_id='PILOT'):
        row=deepcopy(self.cfg)
        row['campaign_id']=campaign_id
        row['configuration']['linkedin_policy']=window_policy()
        self.registry.register(row,'ygor',1 if campaign_id=='PILOT' else 0)
        return row

    def test_new_policy_validation_and_registry_roundtrip(self):
        original=window_policy()
        original['holiday_countries']=['MX','BR']
        original['holidays']=['2026-11-16','2026-11-02']
        before=deepcopy(original)
        normalized=validate_policy(original)
        self.assertEqual(original,before)
        self.assertEqual(normalized['holiday_countries'],['BR','MX'])
        self.assertEqual(normalized['holidays'],['2026-11-02','2026-11-16'])
        self.assertIsNone(normalized['weekly_invite_limit'])
        row=self.register_window()
        self.assertEqual(self.registry.configuration('PILOT')['linkedin_policy'],window_policy())
        normalized['send_window']['start_hour']=8
        self.assertEqual(original['send_window']['start_hour'],7)
        self.assertEqual(row['configuration']['linkedin_policy'],window_policy())

    def test_invalid_new_policy_is_not_registered(self):
        invalid=[{'send_window':{'start_hour':21,'end_hour':7}},
                 {'send_window':{'start_hour':7,'end_hour':7}},
                 {'send_window':{'start_hour':True,'end_hour':21}},
                 {'send_window':{'start_hour':7,'end_hour':25}},
                 {'send_window':{'start_hour':7,'end_hour':21,'extra':0}},
                 {'send_window':None},{'send_hour':9},
                 {'holiday_countries':[]},{'holiday_countries':['BR','BR']},
                 {'holiday_countries':['br','MX']},{'holiday_countries':['US']},
                 {'holiday_countries':'BR'},{'holiday_countries':[{}]},
                 {'weekly_invite_limit':True},{'weekly_invite_limit':0},
                 {'weekly_invite_limit':200.0},{'weekly_invite_limit':'200'},
                 {'holidays':None},{'holidays':['2026-02-30']},
                 {'holidays':['2026-11-16','2026-11-16']},
                 {'timezone':'Not/A_Zone'},{'timezone':None}]
        for changes in invalid:
            row=deepcopy(self.cfg)
            row['configuration']['linkedin_policy']={**window_policy(),**changes}
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                self.registry.register(row,'ygor',1)
        self.assertEqual(self.registry.configuration('PILOT')['linkedin_policy'],policy())

    def test_window_boundaries_preserve_anchor_instant(self):
        cases=[('2026-10-21T06:59:59-06:00','2026-10-21T13:00:00+00:00'),
               ('2026-10-21T07:00:00-06:00','2026-10-21T13:00:00+00:00'),
               ('2026-10-21T12:34:56.123456-06:00','2026-10-21T18:34:56.123456+00:00'),
               ('2026-10-21T20:59:59.999999-06:00','2026-10-22T02:59:59.999999+00:00'),
               ('2026-10-21T21:00:00-06:00','2026-10-22T13:00:00+00:00')]
        for anchor,expected in cases:
            with self.subTest(anchor=anchor):
                due=next_slot(datetime.fromisoformat(anchor),window_policy(),lead_timezone='America/Mexico_City')
                self.assertEqual(due.isoformat(),expected)
        with self.assertRaisesRegex(ValueError,'linkedin_anchor_invalid'):
            next_slot(datetime(2026,10,21,12),window_policy(),lead_timezone='America/Mexico_City')

    def test_explicit_dates_and_weekends_are_skipped(self):
        cases=[('America/Mexico_City','2026-11-13T21:00:00-06:00','2026-11-16',
                '2026-11-17T13:00:00+00:00'),
               ('America/Sao_Paulo','2026-10-30T21:00:00-03:00','2026-11-02',
                '2026-11-03T10:00:00+00:00')]
        for zone,anchor,excluded,expected in cases:
            with self.subTest(zone=zone):
                value={**window_policy(),'holidays':[excluded]}
                self.assertEqual(next_slot(datetime.fromisoformat(anchor),value,zone).isoformat(),expected)
        # These are supplied fixture exclusions, not a generated holiday calendar.

    def test_unknown_lead_context_and_calendar_hold(self):
        self.register_window()
        for zone in (None,'','lead','es-MX','Not/A_Zone',{},True):
            with self.subTest(zone=zone):
                result=self.service.prepare('PILOT',self.now,lead_timezone=zone)
                self.assertEqual(result['status'],'lead_timezone_unverified')
                self.assertIn('lead_country_unverified',result['hold_reasons'])
                self.assertIn('calendar_unverified',result['hold_reasons'])
                self.assertNotIn('due_at',result)
        with self.assertRaisesRegex(ValueError,'linkedin_lead_timezone_required'):
            next_slot(self.now,window_policy())
        with self.assertRaisesRegex(ValueError,'linkedin_lead_timezone_invalid'):
            next_slot(self.now,window_policy(),lead_timezone='es-MX')
        for country in (None,'MX','BR','US','mx',{}):
            with self.subTest(country=country):
                result=self.service.prepare('PILOT',self.now,lead_timezone='America/Mexico_City',lead_country=country)
                self.assertIn('calendar_unverified',result['hold_reasons'])
                self.assertFalse(result['holiday_calendar_verified'])
                self.assertEqual(result['timezone'],'America/Mexico_City')
                self.assertEqual(result['weekly_invite_limit'],None)
                if country in ('MX','BR'):
                    self.assertEqual(result['status'],'calendar_unverified')
                else:
                    self.assertIn('lead_country_unverified' if country is None or not isinstance(country,str)
                                  else 'holiday_country_unsupported',result['hold_reasons'])

    def test_window_dst_gap_and_fold(self):
        zone=ZoneInfo('Africa/Cairo')
        standard=window_policy()
        anchor=datetime.fromisoformat('2026-04-23T20:30:00+00:00')
        due=next_slot(anchor,standard,'Africa/Cairo')
        self.assertEqual(due.astimezone(zone).isoformat(),'2026-04-24T07:00:00+03:00')
        gap={**standard,'send_window':{'start_hour':0,'end_hour':2}}
        due=next_slot(datetime.fromisoformat('2026-04-23T22:00:00+00:00'),gap,'Africa/Cairo')
        self.assertEqual(due.astimezone(zone).isoformat(),'2026-04-24T01:00:00+03:00')
        folded={**standard,'send_window':{'start_hour':23,'end_hour':24}}
        for fold in (0,1):
            anchor=datetime(2026,10,29,23,30,15,123,tzinfo=zone,fold=fold)
            with self.subTest(fold=fold):
                due=next_slot(anchor,folded,'Africa/Cairo')
                self.assertEqual(due.timestamp(),anchor.timestamp())
                self.assertEqual(due.astimezone(zone).fold,fold)
                self.assertEqual(due.astimezone(zone).microsecond,123)

    def test_imaginary_anchor_is_checked_as_a_real_instant(self):
        zone=ZoneInfo('Africa/Cairo')
        anchor=datetime(2026,4,24,0,30,tzinfo=zone)
        value={**window_policy(),'send_window':{'start_hour':0,'end_hour':1}}
        due=next_slot(anchor,value,'Africa/Cairo')
        self.assertEqual(due.astimezone(zone).isoformat(),'2026-04-27T00:00:00+03:00')
        self.assertGreaterEqual(due.timestamp(),anchor.timestamp())

    def test_no_internal_ceiling_or_prefixed_campaign_dependency(self):
        self.register_window('ordinary-campaign')
        self.register_window('hypothesis200')
        for campaign in ('ordinary-campaign','hypothesis200'):
            for quota in (None,{**self.quota,'used':200},{**self.quota,'used':20000}):
                with self.subTest(campaign=campaign,quota=quota):
                    result=self.service.prepare(campaign,self.now,quota,
                        lead_timezone='America/Mexico_City',lead_country='MX')
                    self.assertEqual(result['status'],'calendar_unverified')
                    self.assertEqual(result['hold_reasons'],['calendar_unverified'])
                    self.assertIsNone(result['weekly_invite_limit'])
                    self.assertFalse(result['internal_limit_applied'])
                    self.assertFalse(result['quota_required'])
                    self.assertFalse(result['quota_live_verified'])

    def test_provider_block_remains_a_hold_without_internal_ceiling(self):
        self.register_window()
        for refusal in ({'http_status':429},{'state':'blocked'},
                        {'error_code':'provider_rate_limited'},
                        {'provider_limit_reached':True},
                        {'http_status':401},{'http_status':403}):
            with self.subTest(refusal=refusal):
                result=self.service.prepare('PILOT',self.now,{**self.quota,**refusal},
                    lead_timezone='America/Mexico_City',lead_country='MX')
                self.assertEqual(result['status'],'provider_blocked')
                self.assertIn('provider_blocked',result['hold_reasons'])
                self.assertIn('calendar_unverified',result['hold_reasons'])
                self.assertFalse(result['executor_enabled'])
        without_context=self.service.prepare('PILOT',self.now,{'http_status':429})
        self.assertEqual(without_context['status'],'provider_blocked')

    def test_preparation_is_read_only_and_executor_remains_off(self):
        self.register_window()
        before=self.repo.get_state('campaign_config','PILOT')
        for _ in range(2):
            result=self.service.prepare('PILOT',self.now,
                lead_timezone='America/Mexico_City',lead_country='MX')
            for flag in ('executor_enabled','reserved','sent','quota_live_verified'):
                self.assertFalse(result[flag])
            self.assertTrue(result['simulation'])
            with self.assertRaisesRegex(ValueError,'linkedin_executor_disabled'):
                self.service.execute({'executor_enabled':True,'weekly_invite_limit':None})
        self.assertEqual(self.repo.get_state('campaign_config','PILOT'),before)
        self.assertEqual(self.repo.effects(),[])
        self.assertEqual(self.repo.outbox(),[])
        self.assertFalse(self.service.snapshot()['executor_enabled'])
        self.assertFalse(self.service.snapshot()['canary_sent'])

    def test_new_policy_changes_invalidate_configuration_hash(self):
        row=self.register_window()
        old=self.registry.load('PILOT')['configuration_hash']
        changes=[{'send_window':{'start_hour':8,'end_hour':21}},
                 {'timezone':'America/Mexico_City'},
                 {'weekly_invite_limit':90},
                 {'holiday_countries':['MX']},
                 {'holidays':['2026-11-16']}]
        for version,change in enumerate(changes,2):
            row['configuration']['linkedin_policy']={**window_policy(),**change}
            self.registry.register(row,'ygor',version)
            current=self.registry.load('PILOT')['configuration_hash']
            self.assertNotEqual(old,current)
            old=current


if __name__=='__main__':unittest.main()
