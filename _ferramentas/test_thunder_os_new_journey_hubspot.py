"""Reader proofs using a real scoped HubSpotHTTP and synthetic HTTP only."""
from copy import deepcopy
import json
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from thunder_os.hubspot_executor import HubSpotHTTP
from thunder_os.new_journey_hubspot import (BOOLS, COMPANY_PROPERTIES, CONTACT_PROPERTIES,
    DEAL_PROPERTIES, ENUMS, HubSpotEligibilityError, HubSpotEligibilityRead)


EMAIL = 'person@mail.invalid'
PROFILE = 'https://www.linkedin.com/in/synthetic-person'
TOKEN = 'synthetic-test-secret'


def schema(names):
    result = []
    for name in sorted(names):
        typ, options, description = 'string', [], ''
        if name in BOOLS:
            typ = 'bool'
        elif name in ENUMS:
            typ, options = 'enumeration', sorted(ENUMS[name])
        elif name == 'lifecyclestage':
            typ, options = 'enumeration', ['lead', 'customer', 'subscriber', 'opportunity']
        elif name == 'thunder_annual_revenue_estimate':
            typ, description = 'number', 'Estimativa de faturamento anual em R$.'
        elif name == 'thunder_last_enriched_at':
            typ = 'date'
        result.append({'name': name, 'type': typ, 'archived': False, 'description': description,
                       'options': [{'value': value, 'hidden': False} for value in options]})
    return {'results': result}


def record(ident, properties, associations=None):
    result = {'id': ident, 'archived': False, 'properties': properties}
    if associations is not None:
        result['associations'] = associations
    return result


def links(*ids):
    return {'results': [{'id': ident} for ident in ids]}


class FakeHTTP:
    def __init__(self):
        self.calls, self.loads = [], 0
        self.portal = {'portalId': '51412240'}
        self.schemas = {'contacts': schema(CONTACT_PROPERTIES), 'companies': schema(COMPANY_PROPERTIES),
                        'deals': schema(DEAL_PROPERTIES)}
        self.contacts = {'123': record('123', {'email': EMAIL, 'hs_additional_emails': 'alias@mail.invalid',
            'lifecyclestage': 'lead', 'opt_out': 'false', 'hs_email_optout': 'false',
            'hs_linkedin_url': PROFILE, 'linkedin_url': PROFILE, 'tipo_do_lead': 'MQL',
            'thunder_outreach_risk': 'low', 'campanha_ativa': '', 'executor': ''},
            {'companies': links('500'), 'deals': links('1', '2')})}
        self.companies = {'500': record('500', {'name': 'Synthetic B2B', 'domain': 'registered.invalid',
            'country': 'Brazil', 'hs_country_code': 'BR', 'lifecyclestage': 'lead',
            'thunder_icp_status': 'qualified', 'thunder_icp_sector': 'industria_b2b',
            'thunder_icp_exclusion_reason': '', 'thunder_annual_revenue_estimate': '20000000',
            'thunder_last_enriched_at': '2026-10-23', 'thunder_enrichment_source': 'Synthetic canonical CRM',
            'thunder_is_customer': 'false', 'thunder_suppressed': 'false'}, {'deals': links('2', '3', '4')})}
        self.deals = {str(i): record(str(i), {'hs_is_closed': 'true', 'dealname': 'Synthetic'})
                      for i in range(1, 5)}
        self.search = {'total': 1, 'results': [{'id': '123'}]}
        self.fail = {}

    def loader(self):
        self.loads += 1
        return TOKEN

    def __call__(self, method, path, body, headers, timeout):
        self.calls.append((method, path, deepcopy(body), timeout))
        base = urlsplit(path).path
        if base in self.fail:
            failure = self.fail[base]
            if isinstance(failure, Exception):
                raise failure
            return failure, b'{"error":"private provider payload"}'
        if base == '/account-info/v3/details':
            data = self.portal
        elif base.startswith('/crm/v3/properties/'):
            data = self.schemas[base.rsplit('/', 1)[1]]
        elif base == '/crm/v3/objects/contacts/search':
            data = self.search
        else:
            parts = base.split('/')
            values = getattr(self, parts[-2])
            if parts[-1] not in values:
                return 404, b'{}'
            data = values[parts[-1]]
        return 200, json.dumps(data).encode()


class HubSpotEligibilityReadTests(unittest.TestCase):
    def setUp(self):
        self.http = FakeHTTP()
        self.original = HubSpotHTTP(self.http.loader, request=self.http, timeout=20)
        self.reader = HubSpotEligibilityRead(self.original)
        for name in ('socket.create_connection', 'socket.socket.connect', 'socket.getaddrinfo'):
            mocked = patch(name, side_effect=AssertionError('network forbidden'))
            self.addCleanup(mocked.stop)
            self.addCleanup(mocked.start().assert_not_called)

    def lookup(self, **kwargs):
        return self.reader.lookup(EMAIL, **kwargs)

    def test_exact_lookup_returns_company_schema_and_all_union_deals_without_policy(self):
        result = self.lookup(profile=PROFILE)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['identity'], {'verified': True, 'emails': ['alias@mail.invalid', EMAIL],
            'linkedin_urls': [PROFILE], 'contact_id': '123', 'company_id': '500', 'domain': 'registered.invalid'})
        self.assertEqual([row['id'] for row in result['deals']], ['1', '2', '3', '4'])
        self.assertTrue(result['coverage']['deals'])
        self.assertTrue(result['property_coverage']['companies']['thunder_annual_revenue_estimate'])
        self.assertNotIn('checks', result)
        self.assertNotIn('approved', result)
        self.assertIsNone(self.original._token)
        self.assertEqual(self.http.loads, 1)
        self.assertTrue(all(0 < timeout <= 4 for _, _, _, timeout in self.http.calls))
        self.assertTrue(all(method == 'GET' or method == 'POST' and path.endswith('/contacts/search')
                            for method, path, _, _ in self.http.calls))

    def test_frozen_public_contract_has_boolean_coverage_and_full_schema_metadata(self):
        self.http.schemas['companies']['results'].append({'name': 'additional_existing_field',
            'type': 'enumeration', 'options': [{'value': 'existing_option'}], 'description': 'Existing metadata'})
        result = HubSpotEligibilityRead(self.original, deadline=None, max_requests=50).lookup(EMAIL)
        self.assertEqual(set(result['coverage']), {'identity', 'company', 'deals'})
        self.assertTrue(all(type(value) is bool for value in result['coverage'].values()))
        self.assertEqual(result['schema']['companies']['additional_existing_field']['options'], ['existing_option'])
        self.assertEqual(result['schema']['companies']['additional_existing_field']['type'], 'enumeration')
        self.assertEqual(result['association_coverage']['expected_ids'], ['1', '2', '3', '4'])
        self.assertTrue(result['schema_coverage']['contacts'])

    def test_fourth_open_deal_remains_positive_evidence(self):
        self.http.deals['4']['properties']['hs_is_closed'] = 'false'
        result = self.lookup()
        self.assertEqual(result['deals'][3]['properties']['hs_is_closed'], 'false')
        self.assertTrue(result['coverage']['deals'])

    def test_partial_associations_preserve_known_open_deal_and_mark_incomplete(self):
        self.http.deals['4']['properties']['hs_is_closed'] = 'false'
        self.http.companies['500']['associations']['deals']['paging'] = {'next': {'after': 'page-2'}}
        result = self.lookup()
        self.assertFalse(result['coverage']['deals'])
        self.assertEqual(result['deals'][-1]['properties']['hs_is_closed'], 'false')
        self.assertEqual(result['status'], 'partial')

    def test_missing_association_is_not_an_explicit_empty_list(self):
        for source, key in ((self.http.contacts['123'], 'deals'), (self.http.companies['500'], 'deals')):
            before = deepcopy(source['associations'])
            source['associations'].pop(key)
            result = HubSpotEligibilityRead(self.original).lookup(EMAIL)
            self.assertFalse(result['coverage']['deals'])
            source['associations'] = before
        self.http.contacts['123']['associations']['deals'] = links()
        self.http.companies['500']['associations']['deals'] = links()
        result = HubSpotEligibilityRead(self.original).lookup(EMAIL)
        self.assertEqual(result['deals'], [])
        self.assertTrue(result['coverage']['deals'])

    def test_unresolved_or_invalid_deal_does_not_discard_previous_evidence(self):
        for change in ('missing', 'wrong_id', 'archived', 'missing_flag'):
            with self.subTest(change=change):
                http = FakeHTTP()
                http.deals['1']['properties']['hs_is_closed'] = 'false'
                if change == 'missing': del http.deals['4']
                elif change == 'wrong_id': http.deals['4']['id'] = '999'
                elif change == 'archived': http.deals['4']['archived'] = True
                else: http.deals['4']['properties'].pop('hs_is_closed')
                result = HubSpotEligibilityRead(HubSpotHTTP(http.loader, request=http)).lookup(EMAIL)
                self.assertFalse(result['coverage']['deals'])
                self.assertEqual(result['deals'][0]['properties']['hs_is_closed'], 'false')

    def test_duplicate_invalid_and_truncated_associations_are_never_complete(self):
        for group in (links('4', '4'), {'results': [{'id': '../4'}]},
                      {'results': [{'id': '4'}], 'total': 2},
                      {'results': [], 'paging': 'invalid'}):
            self.http.companies['500']['associations']['deals'] = group
            result = HubSpotEligibilityRead(self.original).lookup(EMAIL)
            self.assertFalse(result['coverage']['deals'])

    def test_no_company_is_derived_from_email_and_ambiguous_companies_are_unknown(self):
        for group in (links(), links('500', '501'), {'results': [{'id': '500'}], 'paging': {'next': {'after': 1}}}):
            self.http.contacts['123']['associations']['companies'] = group
            result = HubSpotEligibilityRead(self.original).lookup(EMAIL)
            self.assertIsNone(result['identity']['domain'])
            self.assertIsNone(result['company'])
            self.assertFalse(result['coverage']['company'])
            self.assertFalse(result['coverage']['deals'])

    def test_company_read_failure_keeps_contact_and_known_contact_deals(self):
        self.http.fail['/crm/v3/objects/companies/500'] = 403
        self.http.deals['1']['properties']['hs_is_closed'] = 'false'
        result = self.lookup()
        self.assertIsNotNone(result['contact'])
        self.assertEqual([row['id'] for row in result['deals']], ['1', '2'])
        self.assertFalse(result['coverage']['deals'])

    def test_missing_schemas_and_company_still_read_known_contact_deal_blocker(self):
        for kind in self.http.schemas:
            self.http.fail['/crm/v3/properties/' + kind] = 403
        self.http.contacts['123']['associations'].pop('companies')
        self.http.contacts['123']['properties'].update(lifecyclestage='customer', hs_email_optout='true')
        self.http.deals['1']['properties']['hs_is_closed'] = 'false'
        result = self.lookup(contact_id='123')
        self.assertEqual(result['contact']['properties']['lifecyclestage'], 'customer')
        self.assertEqual(result['contact']['properties']['hs_email_optout'], 'true')
        self.assertEqual([deal['id'] for deal in result['deals']], ['1', '2'])
        self.assertEqual(result['deals'][0]['properties']['hs_is_closed'], 'false')
        self.assertFalse(result['identity']['verified'])
        self.assertEqual(result['coverage'], {'identity': False, 'company': False, 'deals': False})
        self.assertEqual(result['status'], 'unknown')

    def test_ambiguous_company_keeps_observed_contact_deals_without_negative_coverage(self):
        self.http.contacts['123']['associations']['companies'] = links('500', '501')
        self.http.deals['2']['properties']['hs_is_closed'] = 'false'
        result = self.lookup()
        self.assertEqual(result['reason'], 'checks_hubspot_company_ambiguous')
        self.assertEqual([deal['id'] for deal in result['deals']], ['1', '2'])
        self.assertEqual(result['deals'][1]['properties']['hs_is_closed'], 'false')
        self.assertFalse(result['coverage']['deals'])

    def test_partial_lookup_cache_keeps_internal_coverage_unprojected_and_detached(self):
        self.http.contacts['123']['associations'].pop('companies')
        first = self.lookup()
        count = len(self.http.calls)
        second = self.lookup()
        self.assertEqual(first, second)
        self.assertEqual(len(self.http.calls), count)
        self.assertTrue(all(type(value) is bool for value in second['coverage'].values()))
        internal = next(iter(self.reader._cache.values()))
        self.assertIsInstance(internal['coverage']['deals'], dict)
        self.assertIsInstance(internal['coverage']['identity'], dict)
        first['coverage']['deals'] = True
        first['deals'].clear()
        self.assertEqual(self.lookup(), second)

    def test_customer_optouts_and_disqualification_preserved_as_raw_evidence(self):
        self.http.contacts['123']['properties'].update(opt_out='true', hs_email_optout='true',
            lifecyclestage='customer', thunder_outreach_risk='blocked')
        self.http.companies['500']['properties'].update(thunder_is_customer='true',
            thunder_icp_status='disqualified', thunder_suppressed='true')
        result = self.lookup()
        self.assertEqual(result['contact']['properties']['opt_out'], 'true')
        self.assertEqual(result['company']['properties']['thunder_is_customer'], 'true')
        self.assertEqual(result['company']['properties']['thunder_icp_status'], 'disqualified')
        self.assertEqual(result['company']['properties']['thunder_suppressed'], 'true')
        self.assertEqual(result['contact']['properties']['thunder_outreach_risk'], 'blocked')
        self.assertTrue(result['property_coverage']['companies']['thunder_suppressed'])
        self.assertTrue(result['property_coverage']['contacts']['thunder_outreach_risk'])

    def test_blank_missing_or_wrong_bool_never_becomes_false(self):
        for value in ('', None, 'FALSE', '0', 0, 'no'):
            self.http.companies['500']['properties']['thunder_is_customer'] = value
            self.http.contacts['123']['properties']['opt_out'] = value
            result = HubSpotEligibilityRead(self.original).lookup(EMAIL)
            self.assertFalse(result['property_coverage']['companies']['thunder_is_customer'])
            self.assertFalse(result['property_coverage']['contacts']['opt_out'])
            self.assertEqual(result['company']['properties']['thunder_is_customer'], value)

    def test_explicit_false_and_real_lifecycle_options_have_property_coverage(self):
        result = self.lookup()
        self.assertTrue(result['property_coverage']['companies']['thunder_is_customer'])
        self.assertTrue(result['property_coverage']['companies']['lifecyclestage'])
        self.http.companies['500']['properties']['lifecyclestage'] = 'invented_stage'
        result = HubSpotEligibilityRead(self.original).lookup(EMAIL)
        self.assertFalse(result['property_coverage']['companies']['lifecyclestage'])

    def test_schema_missing_property_or_type_change_never_proves_the_value(self):
        for change in ('missing', 'wrong_type', 'wrong_options', 'wrong_currency'):
            with self.subTest(change=change):
                http = FakeHTTP()
                rows = http.schemas['companies']['results']
                if change == 'missing': rows[:] = [row for row in rows if row['name'] != 'thunder_is_customer']
                elif change == 'wrong_type': next(r for r in rows if r['name'] == 'thunder_is_customer')['type'] = 'string'
                elif change == 'wrong_options': next(r for r in rows if r['name'] == 'thunder_icp_status')['options'] = [{'value': 'qualified'}]
                else: next(r for r in rows if r['name'] == 'thunder_annual_revenue_estimate')['description'] = 'Annual revenue USD'
                result = HubSpotEligibilityRead(HubSpotHTTP(http.loader, request=http)).lookup(EMAIL)
                field = ('thunder_icp_status' if change == 'wrong_options' else
                         'thunder_annual_revenue_estimate' if change == 'wrong_currency' else 'thunder_is_customer')
                self.assertFalse(result['property_coverage']['companies'][field])

    def test_schema_failure_is_public_unknown_without_exception_or_secret(self):
        self.http.fail['/crm/v3/properties/contacts'] = RuntimeError(TOKEN + ' private detail')
        result = self.lookup()
        self.assertFalse(result['identity']['verified'])
        self.assertFalse(result['schema_coverage']['contacts'])
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertNotIn('private detail', json.dumps(result))

    def test_alias_and_https_profile_normalization_need_exact_record_identity(self):
        result = self.reader.lookup(' ALIAS@mail.invalid ', profile='https://LinkedIn.com/in/SYNTHETIC-PERSON/')
        self.assertTrue(result['identity']['verified'])
        self.assertEqual(result['identity']['contact_id'], '123')
        self.assertEqual(result['identity']['linkedin_urls'], [PROFILE])

    def test_conflicting_profile_email_or_contact_id_remains_unknown(self):
        for field, value in (('linkedin_url', 'https://www.linkedin.com/in/different'), ('email', 'other@mail.invalid')):
            http = FakeHTTP(); http.contacts['123']['properties'][field] = value
            result = HubSpotEligibilityRead(HubSpotHTTP(http.loader, request=http)).lookup(EMAIL, contact_id='123', profile=PROFILE)
            self.assertFalse(result['identity']['verified'])
            self.assertIsNone(result['company'])
        self.http.contacts['123']['id'] = '456'
        self.assertFalse(self.lookup(contact_id='123')['identity']['verified'])

    def test_empty_contact_search_is_not_mislabeled_as_duplicate_identity(self):
        self.http.search = {'results': [], 'total': 0}
        result = self.lookup()
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['reason'], 'checks_hubspot_contact_not_found')
        self.assertFalse(result['coverage']['identity'])
        self.assertIsNone(result['contact'])

    def test_ambiguous_or_partial_contact_search_never_selects_first_result(self):
        for search in ({'results': [{'id': '123'}, {'id': '456'}], 'total': 2},
                       {'results': [{'id': '123'}], 'total': 2},
                       {'results': [{'id': '123'}], 'total': 1, 'paging': {'next': {'after': '2'}}}):
            self.http.search = search
            result = HubSpotEligibilityRead(self.original).lookup(EMAIL)
            self.assertFalse(result['identity']['verified'])
            self.assertIsNone(result['contact'])

    def test_alias_collision_between_records_is_unknown_including_cached_read(self):
        self.http.contacts['456'] = deepcopy(self.http.contacts['123'])
        self.http.contacts['456']['id'] = '456'
        first = self.lookup(contact_id='123')
        self.assertTrue(first['identity']['verified'])
        self.assertFalse(self.lookup(contact_id='456')['identity']['verified'])
        self.assertFalse(self.lookup(contact_id='123')['identity']['verified'])

    @staticmethod
    def native_alias_schema(http):
        row = next(r for r in http.schemas['contacts']['results']
                   if r['name'] == 'hs_additional_emails')
        row.update(type='enumeration', fieldType='select', hubspotDefined=True,
                   calculated=False, externalOptions=False, options=[],
                   modificationMetadata={'readOnlyDefinition': True})
        return row

    def test_native_additional_email_schema_accepts_present_empty_and_valid_aliases(self):
        for aliases in (None, '', 'alias@mail.invalid', 'one@mail.invalid;two@mail.invalid'):
            with self.subTest(aliases=aliases):
                http = FakeHTTP()
                self.native_alias_schema(http)
                http.contacts['123']['properties']['hs_additional_emails'] = aliases
                result = HubSpotEligibilityRead(HubSpotHTTP(http.loader, request=http)).lookup(EMAIL)
                self.assertTrue(result['property_coverage']['contacts']['hs_additional_emails'])
                self.assertTrue(result['identity']['verified'])
                self.assertEqual(result['status'], 'ok')
                expected = sorted([EMAIL] + (aliases.split(';') if aliases else []))
                self.assertEqual(result['identity']['emails'], expected)

    def test_native_alias_missing_invalid_and_conflicting_evidence_stays_unknown(self):
        for change in ('missing_value', 'missing_schema', 'not_native', 'wrong_field',
                       'mutable_definition', 'calculated', 'external_options', 'conflict'):
            with self.subTest(change=change):
                http = FakeHTTP()
                schema_row = self.native_alias_schema(http)
                http.contacts['123']['properties']['hs_additional_emails'] = None
                if change == 'missing_value':
                    del http.contacts['123']['properties']['hs_additional_emails']
                elif change == 'missing_schema':
                    http.schemas['contacts']['results'].remove(schema_row)
                elif change == 'not_native': schema_row['hubspotDefined'] = False
                elif change == 'wrong_field': schema_row['fieldType'] = 'text'
                elif change == 'mutable_definition': schema_row['modificationMetadata']['readOnlyDefinition'] = False
                elif change == 'calculated': schema_row['calculated'] = True
                elif change == 'external_options': schema_row['externalOptions'] = True
                else:
                    http.contacts['456'] = deepcopy(http.contacts['123'])
                    http.contacts['456']['id'] = '456'
                reader = HubSpotEligibilityRead(HubSpotHTTP(http.loader, request=http))
                if change == 'conflict':
                    self.assertTrue(reader.lookup(EMAIL, contact_id='123')['identity']['verified'])
                    self.assertFalse(reader.lookup(EMAIL, contact_id='456')['identity']['verified'])
                    result = reader.lookup(EMAIL, contact_id='123')
                else:
                    result = reader.lookup(EMAIL, contact_id='123')
                self.assertFalse(result['identity']['verified'])

    def test_native_additional_email_values_do_not_bypass_validation(self):
        for aliases in (True, False, 3, [], {}, 'not-an-email',
                        'alias@mail.invalid;', 'alias@mail.invalid;bad',
                        ';'.join(['alias@mail.invalid'] * 51)):
            with self.subTest(aliases=aliases):
                http = FakeHTTP()
                self.native_alias_schema(http)
                http.contacts['123']['properties']['hs_additional_emails'] = aliases
                result = HubSpotEligibilityRead(HubSpotHTTP(http.loader, request=http)).lookup(EMAIL)
                self.assertFalse(result['identity']['verified'])
                self.assertIsNone(result['company'])

    def test_foreign_portal_never_reads_customer_records(self):
        self.http.portal = {'portalId': '999'}
        result = self.lookup()
        self.assertEqual(result['reason'], 'checks_hubspot_portal_mismatch')
        self.assertEqual(len(self.http.calls), 1)
        self.assertIsNone(result['contact'])

    def test_repeated_lookup_is_detached_cached_and_stable_without_more_search(self):
        first = self.lookup()
        count = len(self.http.calls)
        second = self.lookup()
        self.assertEqual(first, second)
        self.assertEqual(len(self.http.calls), count)
        first['company']['properties']['domain'] = 'tampered.invalid'
        self.assertEqual(self.lookup()['company']['properties']['domain'], 'registered.invalid')
        self.assertEqual(sum(method == 'POST' for method, _, _, _ in self.http.calls), 1)

    def test_request_and_ten_contact_budget_enforced_without_hidden_retry(self):
        short = HubSpotEligibilityRead(self.original, max_requests=7)
        result = short.lookup(EMAIL)
        self.assertFalse(result['coverage']['deals'])
        self.assertEqual(len(self.http.calls), 7)
        for i in range(12):
            self.reader.lookup(f'person{i}@mail.invalid')
        self.assertEqual(self.reader.count, 10)
        self.assertEqual(self.reader.lookup('last@mail.invalid')['reason'], 'checks_hubspot_budget_exhausted')

    def test_deadline_and_timeouts_are_bounded(self):
        reader = HubSpotEligibilityRead(self.original, deadline=time.monotonic() - 1)
        self.assertEqual(reader.lookup(EMAIL)['reason'], 'checks_hubspot_budget_exhausted')
        self.assertEqual(self.http.calls, [])
        with patch('thunder_os.new_journey_hubspot.time.monotonic', return_value=self.reader.deadline + 1):
            self.assertEqual(self.lookup()['reason'], 'checks_hubspot_budget_exhausted')

    def test_nonread_requests_rejected_before_auth_or_transport(self):
        for method, path, body in (
            ('PATCH', '/crm/v3/objects/contacts/123', {}),
            ('POST', '/crm/v3/objects/contacts', {}),
            ('POST', '/crm/v3/objects/companies/search', {}),
            ('DELETE', '/crm/v3/objects/deals/1', None),
            ('GET', 'https://evil.invalid/account-info/v3/details', None),
            ('GET', '/crm/v3/objects/contacts/123?properties=email&associations=deals&properties=email', None),
            ('GET', '/crm/v3/properties/contacts?extra=1', None),
        ):
            with self.subTest(path=path), self.assertRaises(HubSpotEligibilityError):
                self.reader.port._call(method, path, body)
        self.assertEqual(self.http.calls, [])
        self.assertEqual(self.http.loads, 0)

    def test_exact_email_search_filter_is_the_only_allowed_post(self):
        body = {'filterGroups': [{'filters': [{'propertyName': 'email', 'operator': 'EQ', 'value': EMAIL}]}],
                'properties': ['email'], 'limit': 2}
        for change in ('extra', 'field', 'operator', 'null', 'limit'):
            value = deepcopy(body)
            if change == 'extra': value['after'] = 'page'
            elif change == 'field': value['filterGroups'][0]['filters'][0]['propertyName'] = 'company'
            elif change == 'operator': value['filterGroups'][0]['filters'][0]['operator'] = 'CONTAINS_TOKEN'
            elif change == 'null': value['filterGroups'][0]['filters'][0]['value'] = None
            else: value['limit'] = True
            with self.assertRaises(HubSpotEligibilityError):
                self.reader.port._call('POST', '/crm/v3/objects/contacts/search', value)
        self.assertEqual(self.http.loads, 0)

    def test_invalid_inputs_never_request_credentials(self):
        for address, ident, profile in ((EMAIL, '../123', None), ('invalid', None, None),
            (EMAIL, None, 'http://linkedin.com/in/abc'), (EMAIL, None, 'https://www.linkedin.com/in/a?tracking=1'),
            ('', None, PROFILE), (EMAIL, True, None)):
            result = self.reader.lookup(address, ident, profile)
            self.assertEqual(result['reason'], 'checks_hubspot_input_invalid')
        self.assertEqual(self.http.calls, [])
        self.assertEqual(self.http.loads, 0)

    def test_schema_and_record_secret_echo_is_omitted(self):
        self.http.companies['500']['properties']['thunder_enrichment_source'] = TOKEN
        result = self.lookup()
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertFalse(result['property_coverage']['companies']['thunder_enrichment_source'])

    def test_truncated_or_duplicate_schema_cannot_prove_properties(self):
        self.http.schemas['companies']['paging'] = {'next': {'after': 'more'}}
        result = self.lookup()
        self.assertFalse(result['schema_coverage']['companies'])
        self.assertEqual(result['company']['properties']['thunder_is_customer'], 'false')
        self.assertFalse(result['property_coverage']['companies']['thunder_is_customer'])
        self.http.schemas['companies'].pop('paging')
        duplicated = next(r for r in self.http.schemas['companies']['results'] if r['name'] == 'thunder_is_customer')
        self.http.schemas['companies']['results'].append(deepcopy(duplicated))
        result = HubSpotEligibilityRead(self.original).lookup(EMAIL)
        self.assertFalse(result['property_coverage']['companies']['thunder_is_customer'])

    def test_provider_failure_preserves_known_deals_without_error_payload(self):
        self.http.deals['1']['properties']['hs_is_closed'] = 'false'
        self.http.fail['/crm/v3/objects/deals/4'] = RuntimeError(TOKEN + ' provider secret')
        result = self.lookup()
        self.assertFalse(result['coverage']['deals'])
        self.assertEqual(result['deals'][0]['properties']['hs_is_closed'], 'false')
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertNotIn('provider secret', json.dumps(result))

    def test_nonfinite_estimate_and_missing_suppression_do_not_have_property_coverage(self):
        self.http.companies['500']['properties'].pop('thunder_suppressed')
        self.http.companies['500']['properties']['thunder_annual_revenue_estimate'] = 'NaN'
        result = self.lookup()
        flags = result['property_coverage']['companies']
        self.assertFalse(flags['thunder_suppressed'])
        self.assertFalse(flags['thunder_annual_revenue_estimate'])

    def test_malformed_next_page_never_counts_as_complete(self):
        for value in ('', None, False, 0):
            self.http.companies['500']['associations']['deals']['paging'] = {'next': value}
            result = HubSpotEligibilityRead(self.original).lookup(EMAIL)
            self.assertFalse(result['coverage']['deals'])

    def test_original_port_and_existing_token_are_preserved(self):
        self.original._token = 'untouched-original-value'
        self.lookup()
        self.assertEqual(self.original._token, 'untouched-original-value')
        self.assertIs(self.original._request, self.http)


if __name__ == '__main__':
    unittest.main()
