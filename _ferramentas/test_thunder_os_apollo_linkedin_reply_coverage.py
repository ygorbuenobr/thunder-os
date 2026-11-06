import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from thunder_os.apollo_linkedin_reply_coverage import ReplyCoverage, HubSpotReplySource, coverage_current

NOW = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)
TARGET = {'email':'lead@example.invalid', 'apollo_contact_id':'contact', 'sequence_id':'sequence',
          'linkedin_profile_urn':'urn:li:fsd_profile:lead', 'linkedin_url':'https://www.linkedin.com/in/lead/',
          'hubspot_contact_id':'123', 'hubspot_company_id':'456', 'hubspot_binding_hash':'a'*64}


class Apollo:
    def __init__(self):
        self.rows = [{'id':'contact', 'email_replied':False, 'email_unsubscribed':False, 'email_status':'verified', 'contact_campaign_statuses':[{'emailer_campaign_id':'sequence',
                      'status':'active','replied':False}]}]
    def contacts(self, email):
        if email != TARGET['email']: raise ValueError('mismatch')
        return deepcopy(self.rows)


class LinkedIn:
    def __init__(self): self.present = False; self.complete = True
    def __call__(self, urn):
        return {'target':urn, 'complete':self.complete, 'suppressed':self.present,
                'coverage':'exact_profile_conversation'}


class Eligibility:
    def lookup(self, email, *, contact_id, profile):
        return {'identity':{'verified':True,'contact_id':contact_id,'company_id':'456'},
                'coverage':{'identity':True,'company':True,'deals':True}, 'deals':[]}


class History:
    def __init__(self): self.events = []
    def read(self, row, context):
        return {'contact_id':'123', 'company_id':'456', 'observed_at':NOW.isoformat(),
                'sources':[{'provider':'hubspot','complete':True}], 'events':deepcopy(self.events)}


class CoverageTest(unittest.TestCase):
    def setUp(self):
        self.apollo, self.linkedin = Apollo(), LinkedIn()
        self.history = History()
        self.hubspot = HubSpotReplySource(Eligibility, self.history, clock=lambda:NOW)
    def observe(self, **kw):
        return ReplyCoverage(self.apollo, self.linkedin, clock=lambda:NOW, **kw).observe(TARGET)
    def test_complete_required_sources_and_freshness(self):
        result = self.observe(hubspot=self.hubspot, hubspot_configured=True)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(set(result['sources']), {'apollo','linkedin','hubspot'})
        self.assertTrue(coverage_current(result, NOW + timedelta(minutes=15), hubspot_configured=True))
        self.assertFalse(coverage_current(result, NOW + timedelta(minutes=15, seconds=1), hubspot_configured=True))
        self.assertFalse(coverage_current(result, NOW - timedelta(seconds=1), hubspot_configured=True))
        self.assertFalse(coverage_current(result, NOW, hubspot_configured=False))
    def test_unknown_and_reply_from_each_source(self):
        self.linkedin.present = True
        self.assertEqual(self.observe()['status'], 'reply')
        self.linkedin.present = False; self.linkedin.complete = False
        self.assertEqual(self.observe()['status'], 'unknown')
        self.linkedin.complete = True
        self.apollo.rows[0]['contact_campaign_statuses'][0]['replied'] = True
        self.assertEqual(self.observe()['status'], 'reply')
        self.apollo.rows[0]['contact_campaign_statuses'][0]['replied'] = False
        self.history.events = [{'provider':'hubspot','direction':'incoming','classification':'human'}]
        self.assertEqual(self.observe(hubspot=self.hubspot, hubspot_configured=True)['status'], 'reply')
        self.history.events[0].pop('classification')
        self.assertEqual(self.observe(hubspot=self.hubspot, hubspot_configured=True)['status'], 'unknown')
    def test_apollo_paused_finished_missing_or_mismatch_never_proves_no_reply(self):
        row = self.apollo.rows[0]['contact_campaign_statuses'][0]
        for status in ('paused','finished','failed'):
            row['status'] = status
            self.assertEqual(self.observe()['status'], 'unknown')
        row['status'] = 'active'; del row['replied']
        self.assertEqual(self.observe()['status'], 'unknown')
        row['replied'] = False; row['emailer_campaign_id'] = 'other'
        self.assertEqual(self.observe()['status'], 'unknown')
        row['emailer_campaign_id'] = 'sequence'; self.apollo.rows[0]['id'] = 'other'
        self.assertEqual(self.observe()['status'], 'unknown')
    def test_hubspot_optional_and_configured_failure_blocks(self):
        result = self.observe()
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(coverage_current(result, NOW, hubspot_configured=False))
        self.assertFalse(coverage_current(result, NOW, hubspot_configured=True))
        self.assertEqual(self.observe(hubspot_configured=True)['status'], 'unknown')
        self.history.events = [{'provider':'hubspot','direction':'unknown'}]
        self.assertEqual(self.observe(hubspot=self.hubspot,hubspot_configured=True)['status'], 'unknown')
    def test_hubspot_stale_history_and_open_deal_fail_closed(self):
        stale = HubSpotReplySource(Eligibility, self.history, clock=lambda:NOW + timedelta(minutes=16))
        self.assertEqual(self.observe(hubspot=stale,hubspot_configured=True)['status'], 'unknown')
        class OpenDeal(Eligibility):
            def lookup(self, *args, **kwargs):
                result = super().lookup(*args, **kwargs)
                result['deals'] = [{'properties': {'hs_is_closed': False}}]
                return result
        open_source = HubSpotReplySource(OpenDeal,self.history,clock=lambda:NOW)
        self.assertEqual(self.observe(hubspot=open_source,hubspot_configured=True)['status'], 'unknown')
    def test_no_reply_cannot_hide_reply_in_other_source(self):
        self.linkedin.present = True
        self.apollo.rows = []
        result = self.observe()
        self.assertEqual(result['status'], 'reply')
        self.assertFalse(coverage_current(result,NOW,hubspot_configured=False))
    def test_stale_positive_reply_still_requires_stop(self):
        self.linkedin.present = True
        times = iter([NOW, NOW, NOW, NOW, NOW + timedelta(minutes=16), NOW + timedelta(minutes=16)])
        result = ReplyCoverage(self.apollo,self.linkedin,clock=lambda:next(times)).observe(TARGET)
        self.assertEqual(result['status'], 'reply')
        self.assertEqual(result['sources']['linkedin']['reason'], 'reply_observed_stale_requires_stop')
        self.assertFalse(coverage_current(result,NOW + timedelta(minutes=16),hubspot_configured=False))
    def test_apollo_optout_or_bounce_is_not_clear(self):
        for field, value in [('email_unsubscribed', True), ('email_status', 'bounced')]:
            old = self.apollo.rows[0][field]
            self.apollo.rows[0][field] = value
            self.assertEqual(self.observe()['status'], 'unknown')
            self.apollo.rows[0][field] = old
    def test_apollo_other_campaign_reply_or_incomplete_coverage(self):
        memberships = self.apollo.rows[0]['contact_campaign_statuses']
        memberships.append({'emailer_campaign_id':'other', 'replied':True})
        self.assertEqual(self.observe()['status'], 'reply')
        memberships[-1]['replied'] = False
        self.assertEqual(self.observe()['status'], 'ok')
        for reason in ('unsubscribed', 'bounced'):
            memberships[-1]['inactive_reason'] = reason
            self.assertEqual(self.observe()['status'], 'unknown')
        del memberships[-1]['inactive_reason']
        del memberships[-1]['replied']
        self.assertEqual(self.observe()['status'], 'unknown')
        memberships.pop()
        for field in ('email_unsubscribed', 'email_replied', 'email_status'):
            value = self.apollo.rows[0].pop(field)
            self.assertEqual(self.observe()['status'], 'unknown')
            self.apollo.rows[0][field] = value
    def test_expiry_during_collection_and_bad_shape(self):
        times = iter([NOW, NOW, NOW, NOW, NOW + timedelta(minutes=16), NOW + timedelta(minutes=16)])
        result = ReplyCoverage(self.apollo,self.linkedin,clock=lambda:next(times)).observe(TARGET)
        self.assertEqual(result['status'], 'unknown')
        self.assertFalse(coverage_current({'status':'ok','sources':{'fake':{'status':'ok','observed_at':NOW.isoformat()}},
                                           'required_sources':['fake']}, NOW, hubspot_configured=False))
    def test_missing_target_fails_closed_without_provider_calls(self):
        class Boom:
            def __getattr__(self, name): raise AssertionError('unexpected')
            def __call__(self, *args): raise AssertionError('unexpected')
        result = ReplyCoverage(Boom(),Boom(),clock=lambda:NOW).observe({'email':'x'})
        self.assertEqual(result['status'], 'unknown')

if __name__ == '__main__':
    unittest.main()
