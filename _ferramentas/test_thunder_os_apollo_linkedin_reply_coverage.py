import unittest
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from thunder_os.apollo_linkedin_reply_coverage import (ReplyCoverage, HubSpotReplySource,
    ApolloContactReplyReader, coverage_current)

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

    def test_linkedin_reply_details_require_matching_account_and_recipient(self):
        urn = TARGET['linkedin_profile_urn']
        reply = {'provider': 'connectsafely', 'account_id': 'account',
                 'recipient_profile_urn': urn, 'recipient_public_id': 'lead',
                 'conversation_urn': 'urn:li:msg_conversation:thread',
                 'provider_message_id': 'urn:li:messagingMessage:sample',
                 'received_at': NOW.isoformat(), 'content': 'Synthetic reply'}
        def source(profile):
            return {'target':profile, 'complete':True, 'suppressed':True,
                    'coverage':'exact_profile_conversation', 'reply':reply}
        reader = ReplyCoverage(self.apollo, source, clock=lambda:NOW)
        bound = {**TARGET, 'account_id':'account', 'recipient_public_id':'lead'}
        result = reader.observe(bound)
        self.assertEqual(result['status'], 'reply')
        self.assertEqual(result['sources']['linkedin']['reply'], reply)
        reply['provider_message_id'] = 'urn:li:messagingMessage:(a,b)'
        self.assertEqual(reader.observe(bound)['sources']['linkedin']['reply']['provider_message_id'], reply['provider_message_id'])
        for target in (TARGET, {**bound, 'account_id':'other'}, {**bound, 'recipient_public_id':'other'}):
            result = reader.observe(target)
            self.assertEqual(result['status'], 'reply')
            self.assertNotIn('reply', result['sources']['linkedin'])

    def test_linkedin_reply_missing_text_and_invalid_details_still_stops(self):
        urn = TARGET['linkedin_profile_urn']
        evidence = {'target':urn, 'complete':True, 'suppressed':True,
                    'coverage':'exact_profile_conversation', 'reply':{
                        'provider':'connectsafely', 'account_id':'account',
                        'recipient_profile_urn':urn, 'recipient_public_id':'lead',
                        'conversation_urn':'urn:li:msg_conversation:thread',
                        'provider_message_id':'not-a-provider-urn',
                        'received_at':'not-a-time', 'content':None}}
        reader = ReplyCoverage(self.apollo, lambda _:evidence, clock=lambda:NOW)
        target = {**TARGET, 'account_id':'account', 'recipient_public_id':'lead'}
        result = reader.observe(target)
        self.assertEqual(result['status'], 'reply')
        detail = result['sources']['linkedin']['reply']
        self.assertIsNone(detail['content'])
        self.assertIsNone(detail['provider_message_id'])
        self.assertIsNone(detail['received_at'])
        evidence['reply']['recipient_profile_urn'] = 'urn:li:fsd_profile:other'
        result = reader.observe(target)
        self.assertEqual(result['status'], 'reply')
        self.assertNotIn('reply', result['sources']['linkedin'])

    def test_positive_reply_preserves_partial_history_flags_without_claiming_full_history(self):
        evidence = {'target':TARGET['linkedin_profile_urn'], 'complete':True, 'suppressed':True,
                    'coverage':'exact_profile_conversation', 'positive_evidence_verified':True,
                    'history_complete':False}
        reader = ReplyCoverage(self.apollo, lambda _:evidence, clock=lambda:NOW)
        result = reader.observe(TARGET)
        self.assertEqual(result['status'], 'reply')
        self.assertIs(result['sources']['linkedin']['positive_evidence_verified'], True)
        self.assertIs(result['sources']['linkedin']['history_complete'], False)
        evidence['history_complete'] = True
        evidence['positive_evidence_verified'] = 'true'
        result = reader.observe(TARGET)
        self.assertEqual(result['status'], 'reply')
        self.assertNotIn('history_complete', result['sources']['linkedin'])
        self.assertNotIn('positive_evidence_verified', result['sources']['linkedin'])
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

    def test_no_email_uses_contact_id_and_hubspot_is_not_configured(self):
        target = {**TARGET, 'email': None}
        calls = []
        class ApolloById:
            def contact(_, ident):
                calls.append(ident)
                return {'id': ident, 'contact_campaign_statuses': [
                    {'emailer_campaign_id':'sequence', 'status':'active', 'replied':False}]}
        result = ReplyCoverage(ApolloById(), self.linkedin, hubspot_configured=True,
                               clock=lambda:NOW).observe(target)
        self.assertEqual(calls, ['contact'])
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['sources']['hubspot']['status'], 'not_configured')
        self.assertTrue(coverage_current(result, NOW, hubspot_configured=True))
        self.assertFalse(coverage_current(result, NOW, hubspot_configured=False))

    def test_no_email_reply_and_missing_apollo_coverage_block(self):
        target = {**TARGET, 'email': None}
        class ApolloById:
            def __init__(self, row): self.row = row
            def contact(self, ident): return self.row
        row = {'id':'contact', 'contact_campaign_statuses': [
            {'emailer_campaign_id':'sequence', 'status':'active', 'replied': True}]}
        read = ReplyCoverage(ApolloById(row), self.linkedin, clock=lambda:NOW)
        self.assertEqual(read.observe(target)['status'], 'reply')
        del row['contact_campaign_statuses'][0]['replied']
        self.assertEqual(read.observe(target)['status'], 'unknown')
        row['id'] = 'wrong'
        self.assertEqual(read.observe(target)['status'], 'unknown')

    def test_no_email_does_not_hide_actual_email_coverage_requirement(self):
        class ApolloById:
            def contact(self, ident):
                return {'id': ident, 'email': 'other@example.invalid', 'contact_campaign_statuses': [
                    {'emailer_campaign_id':'sequence', 'status':'active', 'replied':False}]}
        result = ReplyCoverage(ApolloById(), self.linkedin, clock=lambda:NOW).observe({**TARGET, 'email': None})
        self.assertEqual(result['status'], 'unknown')

    def test_apollo_contact_reader_pins_team_actor_and_endpoint(self):
        calls = []
        def wire(method, path, body, headers, timeout):
            calls.append((method, path))
            if path.endswith('api_profile'):
                value = {'user': {'id':'actor', 'team_id':'team'}}
            else:
                value = {'contact': {'id':'contact', 'contact_campaign_statuses': []}}
            return 200, json.dumps(value).encode()
        reader = ApolloContactReplyReader(lambda:'synthetic-key', team_id='team', api_actor_id='actor', request=wire)
        self.assertEqual(reader.contact('contact')['id'], 'contact')
        self.assertEqual(calls, [('GET','/api/v1/users/api_profile'), ('GET','/api/v1/contacts/contact')])
        with self.assertRaises(ValueError): reader.contact('../bad')
        wrong = ApolloContactReplyReader(lambda:'synthetic-key', team_id='other', api_actor_id='actor', request=wire)
        with self.assertRaises(ValueError): wrong.contact('contact')

if __name__ == '__main__':
    unittest.main()


class ApolloActivityFallbackTest(unittest.TestCase):
    """Real contacts/show omits reply booleans (observed 06/11/2026)."""
    class Feed:
        def __init__(self, events=None, fail=False):
            self.events, self.fail = events or [], fail
            self.row = {'id': 'contact', 'email': 'lead@example.invalid', 'email_replied': None,
                        'email_unsubscribed': None, 'email_status': 'verified',
                        'contact_campaign_statuses': [{'emailer_campaign_id': 'sequence', 'status': 'active',
                                                       'replied': None, 'inactive_reason': None}]}
        def contact(self, ident): return deepcopy(self.row)
        def activity(self, ident):
            if self.fail: raise ValueError('activity_incomplete')
            return deepcopy(self.events)

    def observe(self, apollo):
        return ReplyCoverage(apollo, LinkedIn(), clock=lambda: NOW).observe(TARGET)['sources']['apollo']

    def test_activity_without_reply_is_ok_for_active_sequence(self):
        result = self.observe(self.Feed([{'type': 'enrolled', 'sequence_id': 'sequence'}]))
        self.assertEqual(result['status'], 'ok')

    def test_replied_event_in_any_sequence_is_reply(self):
        result = self.observe(self.Feed([{'type': 'replied', 'sequence_id': 'other'}]))
        self.assertEqual(result['status'], 'reply')

    def test_feed_failure_or_inactive_membership_stays_unknown(self):
        self.assertEqual(self.observe(self.Feed(fail=True))['status'], 'unknown')
        feed = self.Feed([])
        feed.row['contact_campaign_statuses'][0]['status'] = 'paused'
        self.assertEqual(self.observe(feed)['status'], 'unknown')

    def test_full_page_is_incomplete(self):
        calls = []
        def request(method, path, body, headers, timeout):
            calls.append((method, path))
            return 200, json.dumps({'contact_id': 'contact', 'events': [{'type': 'enrolled'}] * 100}).encode()
        reader = ApolloContactReplyReader(lambda: 'synthetic-key', team_id='team', api_actor_id='actor', request=request)
        with self.assertRaises(ValueError):
            reader.activity('contact')
        self.assertEqual(calls, [('POST', '/api/v1/emailer_campaigns/activity_feed')])
