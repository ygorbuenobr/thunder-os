import unittest
from unittest.mock import Mock
from copy import deepcopy

from thunder_os.apollo_hunter_preview import plan_company, plan_snapshot, read_identity_candidates, readonly_hubspot_request, readonly_candidate_reader, readonly_association_evidence, readonly_deal_pipeline_evidence, readonly_company_deals, readonly_pipeline_catalog, validate_company_deals

def evidence():
    return {'coverage': {'contacts': True, 'companies': True, 'deals': True},
            'contact_matches': {'a@example.invalid': [], 'b@example.invalid': []},
            'company_matches': [], 'open_deals': []}

class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.company = {'domain': 'example.invalid', 'domain_classification': 'corporate_verified'}
        self.contacts = [{'id': '1', 'email': 'a@example.invalid'},
                         {'id': '2', 'email': 'b@example.invalid'}]

    def plan(self, ev=None, company=None, contacts=None):
        return plan_company(company or self.company, contacts or self.contacts, ev or evidence())

    def test_two_new_contacts_share_one_company_and_one_deal(self):
        result = self.plan()
        self.assertEqual(result['company_action'], 'create')
        self.assertEqual(result['deal_action'], 'create')
        self.assertEqual([r['action'] for r in result['contacts']], ['create', 'create'])
        self.assertEqual(result, self.plan())
        self.assertEqual(result['deal_properties']['pipeline'], '903417452')
        self.assertEqual(result['deal_properties']['dealstage'], '1366237352')

    def test_existing_hunter_deal_is_reused_without_stage_change(self):
        ev = evidence()
        ev['company_matches'] = [{'id': '10', 'domain': 'example.invalid'}]
        ev['contact_matches']['a@example.invalid'] = [{'id':'5', 'company_ids':['10'], 'company_associations_complete':True}]
        ev['open_deals'] = [{'id': '99', 'company_id': '10', 'pipeline': '903417452', 'is_closed': False}]
        result = self.plan(ev)
        self.assertEqual((result['company_action'], result['company_id']), ('reuse', '10'))
        self.assertEqual((result['deal_action'], result['deal_id']), ('reuse', '99'))
        self.assertIsNone(result['deal_properties'])
        self.assertEqual(result['contacts'][0]['action'], 'reuse')

    def test_missing_coverage_and_multiple_open_deals_are_held(self):
        ev = evidence(); ev['coverage']['deals'] = False
        self.assertEqual(self.plan(ev)['deal_action'], 'hold')
        ev = evidence()
        ev['company_matches'] = [{'id':'10','domain':'example.invalid'}]
        ev['open_deals'] = [{'id': i, 'company_id': '10', 'pipeline': '903417452', 'is_closed': False} for i in ('8','9')]
        self.assertEqual(self.plan(ev)['deal_action'], 'hold')

    def test_personal_conflicting_and_ambiguous_identities_are_held(self):
        self.assertEqual(self.plan(company={'domain':'gmail.com'},
            contacts=[{'id':'1','email':'x@gmail.com'}])['company_action'], 'hold')
        ev = evidence(); ev['contact_matches']['a@example.invalid'] = ['5','6']
        self.assertEqual(self.plan(ev)['contacts'][0]['action'], 'hold')
        ev = evidence(); ev['company_matches'] = [{'id':'10','domain':'wrong.invalid'}]
        self.assertEqual(self.plan(ev)['company_action'], 'hold')

    def test_all_contacts_held_cannot_plan_new_deal(self):
        ev = evidence()
        ev['contact_matches']['a@example.invalid'] = ['5', '6']
        ev['contact_matches']['b@example.invalid'] = ['7', '8']
        result = self.plan(ev)
        self.assertEqual(result['company_action'], 'hold')
        self.assertEqual(result['deal_action'], 'hold')

    def test_two_source_contacts_matching_same_crm_contact_are_held(self):
        ev = evidence()
        ev['company_matches'] = [{'id':'10', 'domain':'example.invalid'}]
        ev['contact_matches']['a@example.invalid'] = [{'id':'5', 'company_ids':['10'], 'company_associations_complete':True}]
        ev['contact_matches']['b@example.invalid'] = [{'id':'5', 'company_ids':['10'], 'company_associations_complete':True}]
        result = self.plan(ev)
        self.assertEqual(result['company_action'], 'hold')
        self.assertEqual(result['deal_action'], 'hold')
        self.assertTrue(all(c['action'] == 'hold' and c['contact_id'] is None for c in result['contacts']))

    def test_duplicate_email_and_duplicate_source_id_hold_every_action(self):
        for contacts in (
            [{'id':'1','email':'a@example.invalid'}, {'id':'2','email':' A@example.invalid '}],
            [{'id':'1','email':'a@example.invalid'}, {'id':'1','email':'b@example.invalid'}],
        ):
            result = self.plan(contacts=contacts)
            self.assertEqual((result['company_action'], result['deal_action']), ('hold', 'hold'))
            self.assertTrue(all(c['action'] == 'hold' and c['contact_id'] is None for c in result['contacts']))

    def test_unknown_or_personal_domain_cannot_create(self):
        for domain in ('outlook.com.br', 'unverified.example'):
            result = self.plan(company={'domain':domain},
                contacts=[{'id':'1','email':'a@'+domain}])
            self.assertEqual(result['company_action'], 'hold')

    def test_excluded_subdomain_is_held(self):
        result = self.plan(company={'domain':'sales.embalatec.com.br',
                                    'domain_classification':'corporate_verified'},
                           contacts=[{'id':'1','email':'a@sales.embalatec.com.br'}])
        self.assertEqual((result['company_action'], result['deal_action']), ('hold', 'hold'))

    def test_existing_contact_company_binding_must_be_complete_and_compatible(self):
        ev = evidence()
        ev['company_matches'] = [{'id':'10','domain':'example.invalid'}]
        for candidate in ({'id':'5'}, {'id':'5', 'company_ids':['11']}):
            ev['contact_matches']['a@example.invalid'] = [candidate]
            result = self.plan(ev, contacts=[self.contacts[0]])
            self.assertEqual(result['company_action'], 'hold')
            self.assertEqual(result['contacts'][0]['action'], 'hold')

    def test_malformed_contact_id_is_held_without_crash(self):
        result = self.plan(contacts=[{'id': ['untrusted'], 'email':'a@example.invalid'}])
        self.assertEqual(result['company_action'], 'hold')
        self.assertEqual(result['contacts'][0]['action'], 'hold')

    def test_excluded_company_is_held(self):
        result = self.plan(company={'domain':'embalatec.com'},
                           contacts=[{'id':'1','email':'a@embalatec.com'}])
        self.assertEqual(result['company_action'], 'hold')

class SnapshotTests(unittest.TestCase):
    def test_incomplete_apollo_source_holds_observed_without_inventing_total(self):
        from thunder_os.external_email_import import APOLLO_TEAM, APOLLO_SEQUENCE
        snapshot = {'campaign_id':'BR-APOLLO-01', 'account_id':APOLLO_TEAM, 'external_campaign_id':APOLLO_SEQUENCE, 'status':'error',
                    'coverage': {'complete':False}, 'leads':[{'id':'a'}, {'id':'b'}]}
        result = plan_snapshot(snapshot, {})
        self.assertEqual(result['status'], 'held')
        self.assertIsNone(result['total_leads'])
        self.assertEqual(result['observed_leads'], 2)
        self.assertEqual(result['held_leads'], 2)
        self.assertEqual(result['reason'], 'apollo_incomplete')

    def test_incomplete_snapshot_exposes_labeled_retained_observation_time(self):
        from thunder_os.external_email_import import APOLLO_TEAM, APOLLO_SEQUENCE
        snap={'campaign_id':'BR-APOLLO-01','account_id':APOLLO_TEAM,'external_campaign_id':APOLLO_SEQUENCE,
              'status':'error','observed_at':'2026-10-27T15:48:27.503695+00:00',
              'coverage':{'complete':False,'retained_observed_at':'2026-10-27T15:48:27.503695+00:00'},
              'leads':[{'id':'a'}]}
        result=plan_snapshot(snap,{})
        self.assertEqual(result['retained_observed_at'],'2026-10-27T15:48:27.503Z')
        self.assertEqual(result['provenance_status'],'unverified')
        self.assertIsNone(result['total_leads'])
        self.assertEqual(result['held_leads'],1)

    def test_partial_snapshot_lists_observations_without_authorizing_actions(self):
        from thunder_os.external_email_import import APOLLO_TEAM, APOLLO_SEQUENCE
        snap = {'campaign_id':'BR-APOLLO-01', 'account_id':APOLLO_TEAM,
                'external_campaign_id':APOLLO_SEQUENCE, 'status':'partial', 'stale':True,
                'coverage':{'complete':False, 'contacts_complete':True, 'messages_complete':True,
                    'atomic_snapshot':False, 'contacts_observed_at':'2026-10-26T12:00:00Z',
                    'messages_observed_at':'2026-10-26T12:40:00Z',
                    'reasons':['contacts_scan_spans_refreshes']},
                'leads':[{'id':'a', 'name':'Pessoa A', 'company':'Empresa A',
                    'email':'a@example.invalid', 'membership':{'emailer_campaign_id':APOLLO_SEQUENCE},
                    'membership_observed_at':'2026-10-26T12:05:00Z'},
                    {'id':'b', 'name':'Pessoa B'}]}
        result = plan_snapshot(snap, {})
        self.assertIsNone(result['total_leads'])
        self.assertEqual(result['observed_leads'], 2)
        self.assertEqual(result['status'], 'held')
        self.assertTrue(result['enumeration_finished'])
        self.assertFalse(result['atomic_snapshot'])
        self.assertEqual(result['contacts_observed_at'], '2026-10-26T12:00:00.000Z')
        self.assertEqual(result['messages_observed_at'], '2026-10-26T12:40:00.000Z')
        self.assertEqual([row['action'] for row in result['rows']], ['hold','hold'])
        self.assertEqual(result['rows'][0]['membership_status'], 'observed')
        self.assertEqual(result['rows'][0]['membership_observed_at'], '2026-10-26T12:05:00.000Z')
        self.assertEqual(result['rows'][1]['membership_status'], 'unverified')
        self.assertTrue(all(row['crm_binding'] == 'unverified' for row in result['rows']))

    def test_partial_flags_preserve_unknown_instead_of_false(self):
        from thunder_os.external_email_import import APOLLO_TEAM, APOLLO_SEQUENCE
        base = {'campaign_id':'BR-APOLLO-01', 'account_id':APOLLO_TEAM,
                'external_campaign_id':APOLLO_SEQUENCE, 'status':'partial', 'leads':[]}
        for value in (None, 'false', 0, {}, []):
            with self.subTest(value=value):
                snap = {**base, 'stale':value, 'coverage':{'complete':False,
                    'atomic_snapshot':value, 'contacts_complete':value, 'messages_complete':value}}
                result = plan_snapshot(snap, {})
                for key in ('stale', 'atomic_snapshot', 'enumeration_finished', 'messages_enumerated'):
                    self.assertIsNone(result[key])
        result = plan_snapshot({**base, 'coverage':{'complete':False}}, {})
        for key in ('stale', 'atomic_snapshot', 'enumeration_finished', 'messages_enumerated'):
            self.assertIsNone(result[key])

    def test_complete_snapshot_produces_one_held_row_per_id(self):
        from thunder_os.external_email_import import APOLLO_TEAM, APOLLO_SEQUENCE
        snap={'campaign_id':'BR-APOLLO-01','account_id':APOLLO_TEAM,'external_campaign_id':APOLLO_SEQUENCE,
              'observed_at':'2026-11-01T12:00:00Z','status':'ok',
              'coverage':{'complete':True,'contacts_complete':True,'messages_complete':True},
              'leads':[{'id':'a'},{'id':'b'}]}
        result=plan_snapshot(snap,{})
        self.assertEqual(result['total_leads'],2)
        self.assertEqual(result['held_leads'],2)
        self.assertEqual([row['source_id'] for row in result['rows']],['a','b'])
        self.assertTrue(all(row['action']=='hold' for row in result['rows']))
        self.assertEqual(result['snapshot_observed_at'],'2026-11-01T12:00:00Z')
        self.assertEqual(result['provenance_status'],'unverified')

    def test_incomplete_snapshot_wrong_provider_never_exposes_counts(self):
        snap={'campaign_id':'BR-APOLLO-01','account_id':'wrong','external_campaign_id':'wrong',
              'status':'error','coverage':{'complete':False},'leads':[{'id':'a'}]}
        result=plan_snapshot(snap,{})
        self.assertIsNone(result['observed_leads'])
        self.assertIsNone(result['held_leads'])

    def test_complete_snapshot_with_wrong_provider_scope_is_held(self):
        snap={'campaign_id':'BR-APOLLO-01','account_id':'wrong','external_campaign_id':'wrong',
              'observed_at':'2026-11-01T12:00:00Z','status':'ok',
              'coverage':{'complete':True,'contacts_complete':True,'messages_complete':True},
              'leads':[{'id':'a'}]}
        result=plan_snapshot(snap,{})
        self.assertIsNone(result['total_leads'])
        self.assertEqual(result['reason'],'apollo_scope_unverified')
        self.assertIsNone(result['observed_leads'])
        self.assertIsNone(result['held_leads'])

    def test_wrong_campaign_cannot_be_used_as_apollo_authority(self):
        snapshot = {'campaign_id':'MX27-CDMX', 'status':'ok',
                    'coverage': {'complete':True}, 'leads':[{'id':'a'}]}
        result = plan_snapshot(snapshot, {})
        self.assertEqual(result['status'], 'held')
        self.assertEqual(result['reason'], 'source_scope_mismatch')
        self.assertIsNone(result['observed_leads'])
        self.assertIsNone(result['held_leads'])

class ReadAdapterTests(unittest.TestCase):
    def test_complete_candidate_reads_and_no_mutation(self):
        port = Mock()
        port.candidates.side_effect = [[], []]
        result = read_identity_candidates(port.candidates, "example.invalid", ["a@example.invalid"])
        self.assertFalse(result["coverage_complete"])
        self.assertEqual(port.candidates.call_count, 2)
        port.associate.assert_not_called()
        port.write.assert_not_called()

    def test_untrusted_lists_never_prove_complete_identity(self):
        reader = Mock(return_value=[])
        result = read_identity_candidates(reader, "example.invalid", ["a@example.invalid"])
        self.assertFalse(result["coverage_complete"])

    def test_partial_failure_never_proves_absence(self):
        port = Mock()
        port.candidates.side_effect = [[], RuntimeError("truncated")]
        result = read_identity_candidates(port.candidates, "example.invalid", ["a@example.invalid"])
        self.assertFalse(result["coverage_complete"])
        self.assertIsNone(result["contacts"])
        self.assertIsNone(result["companies"])

    def test_malformed_or_foreign_domain_never_reads(self):
        read = Mock()
        result = read_identity_candidates(read, "example.invalid", ["a@other.invalid"])
        self.assertFalse(result["coverage_complete"])
        read.assert_not_called()

class ReadOnlyTransportTests(unittest.TestCase):
    def test_search_and_get_allowed_without_write(self):
        underlying = Mock(return_value=(200, b'{}'))
        request = readonly_hubspot_request(underlying)
        self.assertEqual(request('POST', '/crm/v3/objects/contacts/search', {}, {}, 1), (200, b'{}'))
        self.assertEqual(request('GET', '/crm/v3/objects/companies/1', None, {}, 1), (200, b'{}'))
        with self.assertRaises(ValueError):
            request('POST', '/crm/v3/objects/contacts', {}, {}, 1)
        with self.assertRaises(ValueError):
            request('PUT', '/crm/v3/objects/contacts/1', {}, {}, 1)
        self.assertEqual(underlying.call_count, 2)

    def test_unknown_post_rejected(self):
        underlying = Mock()
        request = readonly_hubspot_request(underlying)
        for path in ('/crm/v3/objects/deals/search', '/crm/v3/objects/contacts/search?bad=1'):
            with self.assertRaises(ValueError):
                request('POST', path, {}, {}, 1)
        underlying.assert_not_called()

class ConcreteReaderTests(unittest.TestCase):
    def test_email_get_exact_query_only(self):
        underlying = Mock(return_value=(200, b'{}'))
        request = readonly_hubspot_request(underlying)
        request('GET', '/crm/v3/objects/contacts/a%40example.invalid?properties=email&idProperty=email', None, {}, 1)
        for path in ('/crm/v3/objects/contacts/a%40example.invalid?properties=email&idProperty=other',
                     '/crm/v3/objects/contacts/a%40example.invalid?properties=email&idProperty=email&bad=x'):
            with self.assertRaises(ValueError):
                request('GET', path, None, {}, 1)
        self.assertEqual(underlying.call_count, 1)

    def test_reader_uses_guard_to_block_write(self):
        from unittest.mock import patch
        from thunder_os.hubspot_executor import HubSpotHTTP
        original_request = Mock(return_value=(200, b'{}'))
        original = HubSpotHTTP(lambda: 'fixture', request=original_request)
        with patch('thunder_os.new_journey_import.JourneyImportHTTP.candidates', autospec=True) as candidates:
            def probe(instance, kind, properties):
                instance._request('PUT', '/crm/v3/objects/contacts/1', {}, {}, 1)
            candidates.side_effect = probe
            reader = readonly_candidate_reader(original)
            with self.assertRaises(ValueError):
                reader('contacts', {'email':'a@example.invalid'})
        original_request.assert_not_called()

class CandidatePaginationTests(unittest.TestCase):
    def test_alias_pagination_and_no_empty_name_search(self):
        from unittest.mock import patch
        from thunder_os.hubspot_executor import HubSpotHTTP
        from thunder_os.new_journey_import import JourneyImportHTTP
        port = JourneyImportHTTP(HubSpotHTTP(lambda: 'fixture', request=Mock()))
        port.metadata = Mock(return_value={'properties': {'hs_additional_emails': {}}})
        port._search = Mock(side_effect=AssertionError('empty name search'))
        port.read = Mock(return_value=None)
        rows = [
            {'id':'1', 'archived':False, 'properties':{'email':'other@example.invalid','hs_additional_emails':'a@example.invalid'}},
            {'id':'2', 'archived':False, 'properties':{'email':'noise@example.invalid','hs_additional_emails':''}},
        ]
        port._call = Mock(side_effect=[{'results':[rows[0]], 'total':2, 'paging':{'next':{'after':'2'}}},
                                       {'results':[rows[1]], 'total':2}])
        with patch.object(JourneyImportHTTP, '_pace_search', return_value=None):
            result = port.candidates('contacts', {'email':'a@example.invalid'})
        self.assertEqual([row['id'] for row in result], ['1'])
        port._search.assert_not_called()
        self.assertEqual(port._call.call_count, 2)
        self.assertNotIn('after', port._call.call_args_list[0].args[2])
        self.assertEqual(port._call.call_args_list[1].args[2]['after'], '2')

    def test_repeated_cursor_and_inconsistent_total_are_held(self):
        from thunder_os.hubspot_executor import HubSpotHTTP
        from thunder_os.new_journey_import import JourneyImportHTTP
        from thunder_os.new_journey import NewJourneyError
        row = {'id':'1', 'archived':False, 'properties':{'email':'a@example.invalid','hs_additional_emails':''}}
        for pages in (
            [{'results':[row], 'total':2, 'paging':{'next':{'after':'2'}}},
             {'results':[], 'total':2, 'paging':{'next':{'after':'2'}}}],
            [{'results':[row], 'total':2}],
        ):
            port = JourneyImportHTTP(HubSpotHTTP(lambda: 'fixture', request=Mock()))
            port.metadata = Mock(return_value={'properties': {'hs_additional_emails': {}}})
            port._search = Mock(side_effect=AssertionError('empty name search'))
            port._call = Mock(side_effect=pages)
            with self.assertRaises(NewJourneyError):
                port.candidates('contacts', {'email':'a@example.invalid'})

    def test_secondary_company_domain_is_matched_exactly(self):
        from thunder_os.hubspot_executor import HubSpotHTTP
        from thunder_os.new_journey_import import JourneyImportHTTP
        port = JourneyImportHTTP(HubSpotHTTP(lambda: 'fixture', request=Mock()))
        port.metadata = Mock(return_value={'properties': {'hs_additional_domains': {}}})
        port._search = Mock(side_effect=AssertionError('empty name search'))
        row={'id':'3','archived':False,'properties':{'domain':'primary.invalid','hs_additional_domains':'example.invalid'}}
        noise={'id':'4','archived':False,'properties':{'domain':'unrelated.invalid','hs_additional_domains':'example.invalid.evil'}}
        port._call = Mock(return_value={'results':[row,noise],'total':2})
        result=port.candidates('companies',{'domain':'example.invalid'})
        self.assertEqual([item['id'] for item in result],['3'])
        self.assertEqual(port._call.call_args.args[2]['filterGroups'][1]['filters'][0]['propertyName'],'hs_additional_domains')
        self.assertEqual(port._call.call_args.args[2]['filterGroups'][1]['filters'][0]['operator'],'CONTAINS_TOKEN')
        self.assertEqual(port._call.call_args.args[2]['filterGroups'][1]['filters'][0]['value'],'example.invalid')

class ConcreteEndToEndReadTests(unittest.TestCase):
    def test_guarded_reader_finds_alias_without_writing(self):
        from unittest.mock import patch
        from thunder_os.hubspot_executor import HubSpotHTTP
        from thunder_os.new_journey_import import JourneyImportHTTP
        from test_thunder_os_new_journey_import import SyntheticHTTP
        fake=SyntheticHTTP(None)
        fake.add('contacts','11',email='primary@example.invalid',hs_additional_emails='a@example.invalid')
        def request(method,path,body,headers,timeout):
            if method == 'GET' and 'idProperty=email' in path:
                fake.calls.append((method,path,body)); return 404,b'{}'
            return fake.request(method,path,body,headers,timeout)
        original=HubSpotHTTP(lambda:'fixture',request=request)
        reader=readonly_candidate_reader(original)
        with patch.object(JourneyImportHTTP,'_pace_search',return_value=None):
            result=reader('contacts',{'email':'a@example.invalid'})
        self.assertEqual([row['id'] for row in result],['11'])
        self.assertEqual(fake.writes,[])
        self.assertTrue(any(method=='POST' and path.endswith('/contacts/search') for method,path,_ in fake.calls))
        self.assertTrue(any(method=='GET' and 'idProperty=email' in path for method,path,_ in fake.calls))
        self.assertTrue(all(method in ('GET','POST') and (method!='POST' or path.endswith('/search'))
                            for method,path,_ in fake.calls))

class AssociationEvidenceTests(unittest.TestCase):
    def test_observes_existing_contact_and_never_mutates(self):
        from thunder_os.hubspot_executor import HubSpotHTTP
        from test_thunder_os_new_journey_hubspot import FakeHTTP, EMAIL
        fake=FakeHTTP()
        original=HubSpotHTTP(fake.loader,request=fake)
        evidence=readonly_association_evidence(original,EMAIL)
        self.assertEqual(evidence['status'],'observed')
        self.assertTrue(evidence['coverage']['identity'])
        self.assertTrue(evidence['coverage']['company'])
        self.assertTrue(evidence['coverage']['deals'])
        self.assertTrue(all(method=='GET' or method=='POST' and path=='/crm/v3/objects/contacts/search' for method,path,_,_ in fake.calls))
        self.assertFalse(evidence['hunter_pipeline_verified'])
        self.assertEqual(evidence['evidence']['identity']['contact_id'],'123')
        self.assertEqual(evidence['evidence']['identity']['company_id'],'500')
        self.assertEqual(sorted(x['id'] for x in evidence['evidence']['deals']),['1','2','3','4'])

    def test_truncated_contact_associations_are_held(self):
        from thunder_os.hubspot_executor import HubSpotHTTP
        from test_thunder_os_new_journey_hubspot import FakeHTTP, EMAIL
        for group in ('companies','deals'):
            fake=FakeHTTP()
            fake.contacts['123']['associations'][group]={'results':[], 'paging':{'next':{'after':'2'}}}
            evidence=readonly_association_evidence(HubSpotHTTP(fake.loader,request=fake),EMAIL)
            self.assertEqual(evidence['status'],'held')
            self.assertFalse(evidence['hunter_pipeline_verified'])

    def test_fallback_error_holds_associations(self):
        from thunder_os.hubspot_executor import HubSpotHTTP
        from test_thunder_os_new_journey_hubspot import FakeHTTP, EMAIL
        fake=FakeHTTP()
        fake.contacts['123']['associations'].pop('companies')
        fake.fail['/crm/v4/objects/contacts/123/associations/companies']=503
        evidence=readonly_association_evidence(HubSpotHTTP(fake.loader,request=fake),EMAIL)
        self.assertEqual(evidence['status'],'held')
        self.assertFalse(evidence['coverage']['company'])
        self.assertFalse(evidence['hunter_pipeline_verified'])

    def test_truncated_association_is_held(self):
        from thunder_os.hubspot_executor import HubSpotHTTP
        from test_thunder_os_new_journey_hubspot import FakeHTTP, EMAIL
        fake=FakeHTTP()
        fake.companies['500']['associations']['deals']={'results':[{'id':'2','type':'company_to_deal'}], 'paging':{'next':{'after':'3'}}}
        evidence=readonly_association_evidence(HubSpotHTTP(fake.loader,request=fake),EMAIL)
        self.assertEqual(evidence['status'],'held')
        self.assertFalse(evidence['coverage']['deals'])
        self.assertFalse(evidence['hunter_pipeline_verified'])

class DealPipelineEvidenceTests(unittest.TestCase):
    def test_reads_pipeline_and_stage_for_observed_deals(self):
        from thunder_os.hubspot_executor import HubSpotHTTP
        from test_thunder_os_new_journey_hubspot import FakeHTTP, EMAIL
        fake=FakeHTTP()
        for deal in fake.deals.values():
            deal['properties'].update(pipeline='903417452',dealstage='1366237352',hs_is_closed='false')
        port=HubSpotHTTP(fake.loader,request=fake)
        association=readonly_association_evidence(port,EMAIL)
        result=readonly_deal_pipeline_evidence(port,association)
        self.assertEqual(result['status'],'observed')
        self.assertEqual(len(result['deals']),4)
        self.assertTrue(all(d['pipeline']=='903417452' for d in result['deals']))
        self.assertFalse(result['company_binding_complete'])

    def test_missing_pipeline_holds_and_does_not_infer(self):
        from thunder_os.hubspot_executor import HubSpotHTTP
        from test_thunder_os_new_journey_hubspot import FakeHTTP, EMAIL
        fake=FakeHTTP();port=HubSpotHTTP(fake.loader,request=fake)
        association=readonly_association_evidence(port,EMAIL)
        result=readonly_deal_pipeline_evidence(port,association)
        self.assertEqual(result['status'],'held')
        self.assertFalse(result['company_binding_complete'])

    def test_incomplete_associations_do_not_read_deals(self):
        from thunder_os.hubspot_executor import HubSpotHTTP
        fake=Mock()
        port=HubSpotHTTP(lambda:'fixture',request=fake)
        result=readonly_deal_pipeline_evidence(port,{'status':'held'})
        self.assertEqual(result['status'],'held')
        fake.assert_not_called()

class CompanyDealReadTests(unittest.TestCase):
    def test_company_association_source_and_pipeline(self):
        from thunder_os.hubspot_executor import HubSpotHTTP
        from test_thunder_os_new_journey_hubspot import FakeHTTP
        fake=FakeHTTP()
        fake.associations['/crm/v4/objects/companies/500/associations/deals']={'results':[{'toObjectId':2,'associationTypes':[{'category':'HUBSPOT_DEFINED','typeId':342}]}]}
        fake.deals['2']['properties'].update(pipeline='903417452',dealstage='1366237352',hs_is_closed='false')
        fake.companies['500']['properties']['hs_additional_domains']=''
        result=readonly_company_deals(HubSpotHTTP(fake.loader,request=fake),'500','registered.invalid')
        self.assertEqual(result['status'],'observed')
        self.assertEqual(result['company_id'],'500')
        self.assertEqual(result['deals'],[{'id':'2','pipeline':'903417452','stage':'1366237352','is_closed':False}])
        self.assertTrue(all(m=='GET' for m,_,_,_ in fake.calls))

    def test_wrong_company_domain_is_held(self):
        from thunder_os.hubspot_executor import HubSpotHTTP
        from test_thunder_os_new_journey_hubspot import FakeHTTP
        fake=FakeHTTP()
        fake.companies['500']['properties']['hs_additional_domains']=''
        fake.associations['/crm/v4/objects/companies/500/associations/deals']={'results':[]}
        result=readonly_company_deals(HubSpotHTTP(fake.loader,request=fake),'500','other.invalid')
        self.assertEqual(result['status'],'held')

    def test_company_domain_alias_and_wrong_id(self):
        from thunder_os.hubspot_executor import HubSpotHTTP
        from test_thunder_os_new_journey_hubspot import FakeHTTP
        fake=FakeHTTP()
        fake.companies['500']['properties']['hs_additional_domains']='alias.invalid'
        fake.associations['/crm/v4/objects/companies/500/associations/deals']={'results':[]}
        port=HubSpotHTTP(fake.loader,request=fake)
        self.assertEqual(readonly_company_deals(port,'500','alias.invalid')['status'],'observed')
        fake.companies['500']['id']='501'
        port=HubSpotHTTP(fake.loader,request=fake)
        self.assertEqual(readonly_company_deals(port,'500','alias.invalid')['status'],'held')

    def test_company_associations_paged_are_held(self):
        from thunder_os.hubspot_executor import HubSpotHTTP
        from test_thunder_os_new_journey_hubspot import FakeHTTP
        fake=FakeHTTP()
        fake.associations['/crm/v4/objects/companies/500/associations/deals']={'results':[], 'paging':{'next':{'after':'2'}}}
        fake.companies['500']['properties']['hs_additional_domains']=''
        result=readonly_company_deals(HubSpotHTTP(fake.loader,request=fake),'500','registered.invalid')
        self.assertEqual(result['status'],'held')
        self.assertIsNone(result['deals'])

class PipelineCatalogTests(unittest.TestCase):
    def test_hunter_stage_observed_from_complete_catalog(self):
        import json
        from thunder_os.hubspot_executor import HubSpotHTTP
        def request(method,path,body,headers,timeout):
            if path=='/account-info/v3/details': return 200,b'{"portalId":51412240}'
            self.assertEqual((method,path),('GET','/crm/v3/pipelines/deals'))
            return 200,json.dumps({'results':[{'id':'903417452','archived':False,'stages':[{'id':'1366237352','archived':False}]}]}).encode()
        result=readonly_pipeline_catalog(HubSpotHTTP(lambda:'fixture',request=request))
        self.assertEqual(result['status'],'observed')
        self.assertEqual(result['stages'],['1366237352'])

    def test_creation_stage_missing_or_archived_is_held(self):
        import json
        from thunder_os.hubspot_executor import HubSpotHTTP
        for stages in ([{'id':'999','archived':False}], [{'id':'1366237352','archived':True}]):
            def request(method,path,body,headers,timeout):
                if path=='/account-info/v3/details': return 200,b'{"portalId":51412240}'
                return 200,json.dumps({'results':[{'id':'903417452','archived':False,'stages':stages}]}).encode()
            self.assertEqual(readonly_pipeline_catalog(HubSpotHTTP(lambda:'fixture',request=request))['status'],'held')

    def test_incomplete_catalog_is_held(self):
        import json
        from thunder_os.hubspot_executor import HubSpotHTTP
        def request(method,path,body,headers,timeout):
            if path=='/account-info/v3/details': return 200,b'{"portalId":51412240}'
            return 200,json.dumps({'results':[], 'paging':{'next':{'after':'x'}}}).encode()
        self.assertEqual(readonly_pipeline_catalog(HubSpotHTTP(lambda:'fixture',request=request))['status'],'held')

class CompanyStageValidationTests(unittest.TestCase):
    def test_unknown_stage_is_held(self):
        catalog={'status':'observed','pipeline_id':'903417452','stages':['1366237352']}
        company={'status':'observed','company_id':'500','deals':[{'id':'2','pipeline':'903417452','stage':'bogus','is_closed':False}]}
        self.assertEqual(validate_company_deals(company,catalog)['status'],'held')

    def test_valid_stage_is_observation_not_unique_company(self):
        catalog={'status':'observed','pipeline_id':'903417452','stages':['1366237352']}
        company={'status':'observed','company_id':'500','deals':[{'id':'2','pipeline':'903417452','stage':'1366237352','is_closed':False}]}
        result=validate_company_deals(company,catalog)
        self.assertEqual(result['status'],'observed')
        self.assertFalse(result['company_uniqueness_verified'])

if __name__ == '__main__':
    unittest.main()
