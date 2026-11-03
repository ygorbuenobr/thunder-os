import json
import io
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from thunder_os.canary_readers import (ApolloCanaryReader, CanaryConnectSafelyHTTPS,
    CanaryContact, CanaryReadError, CanaryReaders, CanaryScope, CanarySEQDClient,
    ConnectSafelyCanaryReader, HubSpotCanaryReader, InstantlyCanaryReader, install_canary_readers, main)
from thunder_os.connectsafely_client import ConnectSafelyClient
from thunder_os.hubspot_executor import HubSpotHTTP
from thunder_os.seqd_client import MAILBOX


EMAIL = MAILBOX
CONTACT = '123456'
TEAM = 'synthetic-team'
WORKSPACE = '12345678-1234-1234-1234-123456789012'
ACCOUNT = '0123456789abcdef01234567'
OWNER = 'urn:li:fsd_profile:owner'
RECIPIENT = 'urn:li:fsd_profile:recipient'


def row(**kw):
    return {'hubspot_contact_id': CONTACT, 'email': EMAIL, 'channels': ['email'],
            'email_providers': ['apollo', 'instantly'], **kw}


def scope(*rows):
    return CanaryScope({'version': 1, 'contacts': list(rows) or [row()]}, owner_public_id='owner-public')


def wire(data, status=200):
    return status, json.dumps(data).encode()


class ScopeTests(unittest.TestCase):
    def test_exact_two_contacts_are_allowed_but_zero_and_three_are_rejected(self):
        second = row(hubspot_contact_id='654321', email='second@example.invalid', channels=['linkedin'],
            linkedin_public_id='second', linkedin_profile_urn=RECIPIENT, apollo_contact_id='apollo-second')
        configured = scope(row(), second)
        self.assertEqual(2, len(configured.by_id))
        self.assertEqual(('email',), configured.contact(CONTACT).channels)
        self.assertEqual(('linkedin',), configured.contact('654321').channels)
        third = row(hubspot_contact_id='987654', email='third@example.invalid', channels=['linkedin'],
            linkedin_public_id='third', linkedin_profile_urn='urn:li:fsd_profile:third', apollo_contact_id='apollo-third')
        for contacts in ([], [row(),second,third]):
            with self.subTest(count=len(contacts)), self.assertRaisesRegex(CanaryReadError,'canary_config_invalid'):
                CanaryScope({'version':1,'contacts':contacts}, owner_public_id='owner-public')

    def test_self_email_does_not_require_linkedin(self):
        target = scope().email(EMAIL)
        self.assertEqual('', target.linkedin_profile_urn)
        with self.assertRaisesRegex(CanaryReadError, 'outside_canary'):
            scope().email('someone@example.invalid')

    def test_linkedin_own_profile_and_arbitrary_email_are_rejected(self):
        bad = (row(email='third-party@example.invalid'),
            row(channels=['linkedin'], linkedin_public_id='owner-public',
                linkedin_profile_urn=OWNER, apollo_contact_id='apollo-contact'))
        for item in bad:
            with self.subTest(item=item), self.assertRaises(CanaryReadError):
                scope(item)

    def test_second_profile_requires_explicit_id_and_email(self):
        item = row(email='second@example.invalid', channels=['linkedin'], linkedin_public_id='second',
                   linkedin_profile_urn=RECIPIENT, apollo_contact_id='apollo-second')
        self.assertEqual(RECIPIENT, scope(item).contact(CONTACT).linkedin_profile_urn)


class HubSpotTests(unittest.TestCase):
    def make(self, *, search_id=CONTACT, optout=None, email=EMAIL):
        calls = []
        def request(method, path, body, headers, timeout):
            calls.append((method, path, body))
            if path == '/account-info/v3/details':
                return wire({'portalId': 51412240})
            if path.endswith('/contacts/search'):
                return wire({'total': 1, 'results': [{'id': search_id, 'properties': {'email': email}}]})
            return wire({'id': CONTACT, 'properties': {'email': EMAIL, 'hs_email_optout': optout}})
        return HubSpotCanaryReader(scope(), HubSpotHTTP(lambda:'synthetic-hubspot-token', request=request)), calls

    def test_exact_email_is_confirmed_before_each_read_and_null_is_no_record(self):
        reader, calls = self.make()
        for _ in range(2):
            self.assertEqual({'target': CONTACT, 'complete': True, 'suppressed': False,
                'coverage': 'exact_contact', 'no_record': True}, reader.optout(CONTACT))
        self.assertEqual(6, len(calls))
        self.assertEqual(['/account-info/v3/details', '/crm/v3/objects/contacts/search'],
                         [call[1] for call in calls[:2]])
        self.assertEqual(EMAIL, calls[1][2]['filterGroups'][0]['filters'][0]['value'])

    def test_id_mismatch_stops_before_record_read(self):
        reader, calls = self.make(search_id='999999')
        with self.assertRaisesRegex(CanaryReadError, 'email_id_mismatch'):
            reader.optout(CONTACT)
        self.assertEqual(2, len(calls))

    def test_optout_and_failure_are_not_clean(self):
        reader, _ = self.make(optout='true')
        self.assertTrue(reader.optout(CONTACT)['suppressed'])
        reader, _ = self.make(optout='unknown')
        with self.assertRaises(CanaryReadError):
            reader.optout(CONTACT)


class ApolloTests(unittest.TestCase):
    def make(self, rows=None, **changes):
        data = {'contacts': rows or [], 'partial_results_only': False,
                'pagination': {'page': 1, 'per_page': 10, 'total_entries': len(rows or [])}, **changes}
        calls = []
        def request(method, path, body, headers, timeout):
            calls.append((method, path, body))
            return wire({'user': {'team_id': TEAM}} if method == 'GET' else data)
        return ApolloCanaryReader(scope(), TEAM, lambda:'synthetic-apollo-token', request=request), calls

    def test_zero_saved_contacts_is_scoped_no_record_and_repeated_read_is_fresh(self):
        reader, calls = self.make()
        for _ in range(2):
            result = reader(EMAIL)
            self.assertTrue(result['complete'])
            self.assertTrue(result['no_record'])
            self.assertFalse(result['suppressed'])
        self.assertEqual(4, len(calls))
        self.assertEqual({'q_keywords': EMAIL, 'page': 1, 'per_page': 10}, calls[1][2])
        with self.assertRaises(CanaryReadError):
            reader('not-allowed@example.invalid')
        self.assertEqual(4, len(calls))

    def test_populated_membership_requires_reply_observation_not_just_active(self):
        contact = {'id': 'apollo-1', 'email': EMAIL, 'contact_campaign_statuses': [{'status': 'active'}]}
        reader, _ = self.make([contact])
        with self.assertRaisesRegex(CanaryReadError, 'reply_unavailable'):
            reader(EMAIL)
        contact['contact_campaign_statuses'][0]['replied'] = True
        reader, _ = self.make([contact])
        self.assertTrue(reader(EMAIL)['suppressed'])

    def test_row_level_reply_suppresses_even_without_membership_or_with_negative_membership(self):
        for memberships in ([], [{'status': 'active', 'replied': False}]):
            with self.subTest(memberships=memberships):
                reader, _ = self.make([{'id': 'apollo-1', 'email': EMAIL,
                    'contact_campaign_statuses': memberships, 'email_replied': True}])
                result = reader(EMAIL)
                self.assertTrue(result['suppressed'])
                self.assertFalse(result['no_record'])

    def test_row_level_reply_false_without_membership_is_clear(self):
        reader, _ = self.make([{'id': 'apollo-1', 'email': EMAIL,
            'contact_campaign_statuses': [], 'email_replied': False}])
        result = reader(EMAIL)
        self.assertFalse(result['suppressed'])
        self.assertFalse(result['no_record'])

    def test_never_sequenced_contact_is_clear_but_optout_and_bounce_still_suppress(self):
        # Owner decision 2026-11-03: no Apollo sequence means no Apollo email and no Apollo reply.
        reader, _ = self.make([{'id': 'apollo-1', 'email': EMAIL, 'contact_campaign_statuses': []}])
        result = reader(EMAIL)
        self.assertEqual((False, False, True), (result['suppressed'], result['no_record'], result['complete']))
        for extra in ({'email_unsubscribed': True}, {'email_status': 'bounced'}, {'email_status': 'unsubscribed'}):
            with self.subTest(extra=extra):
                reader, _ = self.make([{'id': 'apollo-1', 'email': EMAIL, 'contact_campaign_statuses': [], **extra}])
                self.assertTrue(reader(EMAIL)['suppressed'])
        reader, _ = self.make([{'id': 'apollo-1', 'email': EMAIL}])
        with self.assertRaisesRegex(CanaryReadError, 'reply_unavailable'):
            reader(EMAIL)

    def test_sequenced_contact_without_reply_observation_fails_closed(self):
        reader, _ = self.make([{'id': 'apollo-1', 'email': EMAIL,
            'contact_campaign_statuses': [{'status': 'paused'}]}])
        with self.assertRaisesRegex(CanaryReadError, 'reply_unavailable'):
            reader(EMAIL)

    def test_row_level_reply_and_membership_invalid_values_fail_closed(self):
        for value in (None, 'false', 0, []):
            for location in ('contact', 'membership'):
                with self.subTest(value=value, location=location):
                    contact = {'id': 'apollo-1', 'email': EMAIL,
                        'contact_campaign_statuses': []}
                    if location == 'contact':
                        contact['email_replied'] = value
                    else:
                        contact['contact_campaign_statuses'] = [{'replied': value}]
                    reader, _ = self.make([contact])
                    with self.assertRaisesRegex(CanaryReadError, 'reply_unavailable'):
                        reader(EMAIL)

    def test_partial_search_fails_and_null_optout_is_not_suppressed(self):
        reader, _ = self.make(partial_results_only=True)
        with self.assertRaises(CanaryReadError):
            reader(EMAIL)
        reader, _ = self.make([{'id':'apollo-1', 'email':EMAIL, 'contact_campaign_statuses':[],
                               'email_unsubscribed':None, 'email_replied':False}])
        self.assertFalse(reader(EMAIL)['suppressed'])

    def test_wrong_team_and_rate_limit_stop_without_retry(self):
        for response in (wire({'user':{'team_id':'other'}}), wire({}, 429)):
            request = Mock(return_value=response)
            reader = ApolloCanaryReader(scope(), TEAM, lambda:'synthetic-token', request=request)
            with self.assertRaises(CanaryReadError):
                reader(EMAIL)
            self.assertEqual(1, request.call_count)


class InstantlyTests(unittest.TestCase):
    def make(self, pages):
        calls = []
        def request(method, path, body, headers, timeout):
            calls.append((method, path, body))
            return wire({'id': WORKSPACE} if path.endswith('/current') else pages.pop(0))
        return InstantlyCanaryReader(scope(), WORKSPACE, lambda:'synthetic-token', request=request), calls

    def test_exact_lead_no_campaign_sweep_and_empty_page_is_no_record(self):
        reader, calls = self.make([{'items':[], 'next_starting_after':None}])
        self.assertTrue(reader(EMAIL)['no_record'])
        query = parse_qs(urlsplit(calls[1][1]).query)
        self.assertEqual([EMAIL], query['lead'])
        self.assertNotIn('campaign_id', query)
        self.assertTrue(all(method == 'GET' and body is None for method, path, body in calls))

    def test_pagination_finds_reply_on_second_page(self):
        message = {'organization_id': WORKSPACE, 'lead':EMAIL, 'ue_type':1, 'id':'message1'}
        reader, calls = self.make([{'items':[message], 'next_starting_after':'message1'},
            {'items':[{**message, 'id':'message2', 'ue_type':2}], 'next_starting_after':None}])
        self.assertTrue(reader(EMAIL)['suppressed'])
        self.assertIn('starting_after=message1', calls[2][1])

    def test_wrong_contact_or_truncated_pagination_fails(self):
        message = {'organization_id': WORKSPACE, 'lead':'different@example.invalid', 'ue_type':1, 'id':'message1'}
        reader, _ = self.make([{'items':[message]}])
        with self.assertRaises(CanaryReadError):
            reader(EMAIL)
        pages = [{'items':[{**message, 'lead':EMAIL, 'id':'message'+str(i)}],
                  'next_starting_after':'cursor'+str(i)} for i in range(3)]
        reader, _ = self.make(pages)
        with self.assertRaisesRegex(CanaryReadError, 'incomplete'):
            reader(EMAIL)


class LinkedInTests(unittest.TestCase):
    def make(self, *, inbound=False, has_more=False, company='Synthetic Company'):
        item = row(email='second@example.invalid', channels=['linkedin'], linkedin_public_id='second',
            linkedin_profile_urn=RECIPIENT, apollo_contact_id='apollo-second')
        targets = scope(item)
        calls = []
        def request(method, path, query, body):
            calls.append((method, path, query, body))
            if path == '/linkedin/profile':
                return 200, {'success':True, 'accountId':ACCOUNT, 'cached':False, 'profile':{
                    'publicIdentifier':'second', 'entityUrn':RECIPIENT, 'firstName':'Synthetic',
                    'lastName':'Person', 'companyName':company}}
            if '/exists/' in path:
                return 200, {'success':True, 'accountId':ACCOUNT, 'profileUrn':RECIPIENT,
                             'hasConversation':True, 'conversationUrn':'urn:li:msg_conversation:synthetic'}
            return 200, {'success':True, 'accountId':ACCOUNT, 'conversationUrn':'urn:li:msg_conversation:synthetic',
                'participants':[{'profileId':OWNER}, {'profileId':RECIPIENT}],
                'hasMore':has_more, 'messages':[{'messageUrn':'urn:li:message:1',
                'isSentByOwner':not inbound, 'senderProfileId':RECIPIENT if inbound else OWNER}],
                'nextCursor':'100' if has_more else None}
        client = ConnectSafelyClient(ACCOUNT, 'owner-public', self_profile_id=OWNER, request=request)
        client.verify_identity = Mock()
        return ConnectSafelyCanaryReader(targets, client), targets.contact(CONTACT), calls

    def test_profile_is_explicitly_fresh_and_scoped_to_second_profile(self):
        reader, contact, calls = self.make()
        result = reader.profile(contact, {'account_id':ACCOUNT, 'recipient_public_id':'second',
                                         'recipient_profile_urn':RECIPIENT})
        self.assertEqual('Synthetic Company', result['company'])
        self.assertEqual('true', calls[0][2]['forceRefresh'])

    def observed_profile_reader(self, *, cached='absent', experiences=None):
        reader, contact, calls = self.make()
        # Sanitized shape observed from the current provider: no cache flag,
        # currentCompany=null, and explicit Present text instead of isCurrent.
        data = {'success':True, 'accountId':ACCOUNT, 'profileId':'second', 'profile': {
            'firstName':'Synthetic', 'lastName':'Person', 'publicIdentifier':'second',
            'entityUrn':RECIPIENT, 'currentCompany':None, 'experience':experiences if experiences is not None else [
                {'companyName':'Previous Company','duration':'Aug 2023 - Jan 2025 · 1 yr 6 mos'},
                {'companyName':'Current Company','duration':'Jan 2025 - Present · 1 yr 10 mos'}]}}
        if cached != 'absent':
            data['cached'] = cached
        reader.client.request = lambda method,path,query,body: (200,data)
        return reader, contact

    def test_profile_real_shape_uses_explicit_present_not_first_position(self):
        reader, contact = self.observed_profile_reader()
        result = reader.profile(contact, {'account_id':ACCOUNT,'recipient_public_id':'second',
                                          'recipient_profile_urn':RECIPIENT})
        self.assertEqual('Current Company', result['company'])
        self.assertEqual({'request':'forceRefresh=true','provider_cache':'unreported'}, result['freshness'])
        self.assertNotIn('cached', result)

    def test_profile_reported_cache_and_unknown_cache_flag_are_rejected(self):
        for cached in (True, None, 'false', 0):
            reader, contact = self.observed_profile_reader(cached=cached)
            with self.subTest(cached=cached), self.assertRaisesRegex(CanaryReadError,'freshness_unavailable'):
                reader.profile(contact, {'account_id':ACCOUNT,'recipient_public_id':'second',
                                         'recipient_profile_urn':RECIPIENT})

    def test_profile_no_explicit_current_or_multiple_current_positions_are_rejected(self):
        cases = ([{'companyName':'Past Company','duration':'Jan 2024 - Jan 2025'}],
            [{'companyName':'Unknown Company','duration':'Current project delivered in 2025'}],
            [{'companyName':'Company One','duration':'Jan 2025 - Present'},
             {'companyName':'Company Two','duration':'Feb 2025 - Current · 1 yr'}])
        for positions in cases:
            reader, contact = self.observed_profile_reader(experiences=positions)
            with self.subTest(positions=positions), self.assertRaisesRegex(CanaryReadError,'current_company_unavailable'):
                reader.profile(contact, {'account_id':ACCOUNT,'recipient_public_id':'second',
                                         'recipient_profile_urn':RECIPIENT})

    def test_outbound_only_conversation_clears_and_inbound_suppresses(self):
        for inbound in (True, False):
            reader, _, calls = self.make(inbound=inbound)
            result = reader(RECIPIENT)
            self.assertEqual(inbound, result['suppressed'])
            self.assertEqual('true', calls[-1][2]['sync'])

    def test_conversation_can_repeat_pinned_alias_without_creating_third_principal(self):
        reader, _, _ = self.make()
        original = reader.client.request
        def response(method,path,query,body):
            status, data = original(method,path,query,body)
            if path.endswith('/messages'):
                data['participants'] = [{'profileId':'owner-public'}, {'profileId':'second'},
                                        {'profileId':RECIPIENT.removeprefix('urn:li:fsd_profile:')}]
            return status, data
        reader.client.request = response
        self.assertFalse(reader(RECIPIENT)['suppressed'])

    def test_conversation_rejects_third_principal_and_unknown_alias(self):
        for third in ('urn:li:fsd_profile:third-person', 'unknown-alias', None):
            reader, _, _ = self.make()
            original = reader.client.request
            def response(method,path,query,body):
                status, data = original(method,path,query,body)
                if path.endswith('/messages'):
                    data['participants'].append({'profileId':third})
                return status, data
            reader.client.request = response
            with self.subTest(third=third), self.assertRaisesRegex(CanaryReadError,'conversation_mismatch'):
                reader(RECIPIENT)

    def test_unconfigured_target_makes_zero_calls_and_incomplete_conversation_fails(self):
        reader, _, calls = self.make()
        with self.assertRaises(CanaryReadError):
            reader(OWNER)
        self.assertEqual([], calls)
        reader, _, calls = self.make(has_more=True)
        with self.assertRaises(CanaryReadError):
            reader(RECIPIENT)

    def test_alternate_slug_cannot_target_owners_profile_urn(self):
        reader, _, calls = self.make()
        reader.client.self_profile_id = RECIPIENT
        with self.assertRaisesRegex(CanaryReadError, 'must_not_target_owner'):
            reader(RECIPIENT)
        self.assertEqual([], calls)

    def test_readonly_transport_rejects_write_before_credential(self):
        credential = Mock(side_effect=AssertionError('credential must not be loaded'))
        port = CanaryConnectSafelyHTTPS(ACCOUNT, credential)
        with self.assertRaises(CanaryReadError):
            port('POST', '/linkedin/conversations/send', {}, {})
        credential.assert_not_called()

    def test_real_reader_contract_sends_company_divergence_to_comparison(self):
        from thunder_os.delegated_identity import compare_identity
        linkedin, contact, _ = self.make(company='Changed Company')
        hubspot = Mock()
        hubspot.read.return_value = {'firstname':'Synthetic','lastname':'Person',
            'company':'Synthetic Company','linkedin_url':'https://www.linkedin.com/in/second'}
        def request(method,path,body,headers,timeout):
            if method == 'GET':
                return wire({'team_id':TEAM})
            return wire({'contacts':[{'id':'apollo-second','email':contact.email,
                'linkedin_url':'https://www.linkedin.com/in/second'}],
                'pagination':{'page':1,'per_page':10,'total_entries':1}})
        apollo = ApolloCanaryReader(linkedin.scope, TEAM, lambda:'synthetic-token', request=request)
        readers = CanaryReaders(linkedin.scope, hubspot, linkedin, {'apollo':apollo})
        plan = {'account_id':ACCOUNT,'recipient_public_id':'second','recipient_profile_urn':RECIPIENT}
        observation = readers.identity(CONTACT,plan)
        comparison = compare_identity(CONTACT,observation['contact'],observation['profile'],plan)
        self.assertFalse(comparison['matches'])
        self.assertEqual(['company'],comparison['mismatches'])


class CompositionTests(unittest.TestCase):
    def make(self, *, clean=True):
        hubspot = Mock()
        hubspot.optout.return_value = {'target':CONTACT, 'complete':True, 'suppressed':False}
        readers = {name:Mock(return_value={'target':EMAIL, 'complete':True, 'suppressed':False,
            'no_record':True}) for name in ('apollo', 'instantly')}
        linkedin = Mock(side_effect=AssertionError('email cannot read own LinkedIn'))
        if not clean:
            readers['apollo'].return_value['suppressed'] = True
        return CanaryReaders(scope(), hubspot, linkedin, readers), hubspot, linkedin, readers

    def test_email_reads_fresh_sources_without_linkedin_or_writes(self):
        bundle, hub, linkedin, providers = self.make()
        for _ in range(2):
            proof = bundle.check_email(EMAIL)
            self.assertEqual({'hubspot','apollo','instantly'}, set(proof['sources']))
        self.assertEqual(2, hub.optout.call_count)
        self.assertEqual(2, providers['apollo'].call_count)
        linkedin.assert_not_called()

    def test_any_read_failure_or_reply_stops_before_seqd_send(self):
        bundle, _, _, providers = self.make(clean=False)
        transport = Mock()
        client = CanarySEQDClient(bundle, transport)
        with self.assertRaises(CanaryReadError):
            client.send({'to':EMAIL})
        transport.send.assert_not_called()
        providers['apollo'].side_effect = TimeoutError('synthetic timeout')
        with self.assertRaises(TimeoutError):
            client.send({'to':EMAIL})
        transport.send.assert_not_called()

    def test_seqd_send_is_explicit_and_bound_to_self_then_suppression(self):
        bundle, _, _, _ = self.make()
        transport = Mock()
        client = CanarySEQDClient(bundle, transport)
        transport.send.assert_not_called()
        envelope = {'to':EMAIL, 'preview_id':'synthetic-exact-preview', 'idempotency_key':'synthetic-effect'}
        client.send(envelope)
        transport.send.assert_called_once_with(envelope)
        with self.assertRaises(CanaryReadError):
            client.send({'to':'third-party@example.invalid'})
        self.assertEqual(1, transport.send.call_count)

    def test_seqd_read_and_reconcile_cannot_escape_observed_canary_namespace(self):
        bundle, _, _, _ = self.make()
        transport = Mock()
        client = CanarySEQDClient(bundle, transport)
        with self.assertRaises(CanaryReadError):
            client.read('unobserved-message')
        with self.assertRaises(CanaryReadError):
            client.reconcile('unrelated-effect')
        transport.read.assert_not_called()
        transport.reconcile.assert_not_called()
        transport.contact_search.return_value = {'messages':[{'id':'observed-message', 'thread_id':'thread'}]}
        client.contact_search(EMAIL)
        client.read('observed-message')
        transport.read.assert_called_once_with('observed-message')

    def test_wiring_is_disabled_without_explicit_config(self):
        app = SimpleNamespace()
        with patch('socket.create_connection', side_effect=AssertionError('network forbidden')):
            self.assertIsNone(install_canary_readers(app, None, {'live':{}}))
        self.assertFalse(hasattr(app, 'canary_readers'))

    def test_live_hook_wires_real_readers_without_loading_credentials_or_calling_network(self):
        cfg = {'live': {'api_credential_file':'/fixture/connectsafely.token', 'canary_readers': {
            'version':1, 'contacts':[row()], 'hubspot': {'credential_file':'/fixture/hubspot_access'},
            'apollo':{'team_id':TEAM, 'credential_file':'/fixture/apollo.token'},
            'instantly':{'workspace_id':WORKSPACE, 'credential_file':'/fixture/instantly.token'}}}}
        client = ConnectSafelyClient(ACCOUNT, 'owner-public', self_profile_id=OWNER)
        app = SimpleNamespace(provider=SimpleNamespace(client=client, expected_public_id='owner-public'))
        with patch('socket.create_connection', side_effect=AssertionError('network forbidden')), \
             patch('pathlib.Path.read_text', side_effect=AssertionError('credential must stay lazy')):
            readers = install_canary_readers(app, SimpleNamespace(), cfg)
        self.assertIs(readers, app.canary_readers)
        self.assertIs(readers.suppression, app.delegated_suppression)
        self.assertTrue(callable(app.delegated_contact_scope))
        self.assertIsInstance(readers.email_readers['apollo'], ApolloCanaryReader)
        self.assertIsInstance(readers.email_readers['instantly'], InstantlyCanaryReader)

    def test_seqd_restart_readback_is_bound_to_persisted_self_preview(self):
        bundle, _, _, _ = self.make()
        bundle.repository = Mock()
        def state(kind, identifier):
            return ({'preview_id':'own-preview'} if kind == 'seqd_attempt' else
                    {'effect_key':'own-effect', 'plan':{'to':EMAIL}})
        bundle.repository.get_state.side_effect = state
        transport = Mock()
        client = CanarySEQDClient(bundle, transport)
        client.reconcile('own-effect')
        transport.reconcile.assert_called_once_with('own-effect')
        with self.assertRaises(CanaryReadError):
            client.reconcile('different-effect')

    def test_seqd_mime_context_preserves_partial_coverage_and_is_never_a_receipt(self):
        bundle, _, _, _ = self.make()
        transport = Mock()
        client = CanarySEQDClient(bundle, transport)
        transport.contact_search.return_value = {'messages':[{'id':'observed-message', 'thread_id':'thread'}]}
        client.contact_search(EMAIL)
        transport.conversation.return_value = {'thread': {'context_only':True, 'receipt_eligible':False,
            'messages':[{'id':'observed-message', 'complete':False, 'context_only':True}],
            'complete':False, 'truncated':False, 'coverage':{'reasons':['attachment_body_not_fetched']}}}
        result = client.read_context('observed-message')
        self.assertFalse(result['complete'])
        self.assertFalse(result['receipt_eligible'])
        self.assertEqual(['attachment_body_not_fetched'], result['coverage']['reasons'])
        transport.send.assert_not_called()
        transport.read.assert_not_called()

    def test_readonly_cli_reports_contact_source_coverage_without_loading_effects(self):
        readers, _, linkedin, _ = self.make()
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)/'canary.json'
            config.write_text('{}')
            with patch('thunder_os.canary_readers.build_canary_readers', return_value=readers), \
                 patch('socket.create_connection', side_effect=AssertionError('network forbidden')), redirect_stdout(output):
                result = main(['--config', str(config), '--owner-public-id', 'owner-public', '--email', EMAIL])
        self.assertEqual(0, result)
        report = json.loads(output.getvalue())
        self.assertEqual({'hubspot','apollo','instantly'}, set(report['sources']))
        self.assertTrue(report['read_only'])
        self.assertFalse(report['send_enabled'])
        self.assertEqual(0, report['writes'])
        linkedin.assert_not_called()

    def test_cli_stops_after_failed_source_without_retry_or_false_clear(self):
        readers, hubspot, _, providers = self.make()
        hubspot.optout.side_effect = CanaryReadError('canary_provider_rate_limited')
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)/'canary.json'; config.write_text('{}')
            with patch('thunder_os.canary_readers.build_canary_readers', return_value=readers), redirect_stdout(output):
                result = main(['--config',str(config),'--owner-public-id','owner-public','--email',EMAIL])
        self.assertEqual(2, result)
        self.assertEqual('blocked', json.loads(output.getvalue())['status'])
        self.assertEqual(1, hubspot.optout.call_count)
        providers['apollo'].assert_not_called()
        providers['instantly'].assert_not_called()


if __name__ == '__main__':
    unittest.main()
