"""S27C3/C4: real Repository/HTTP/executor, synthetic provider, sockets forbidden."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import json
import sqlite3
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import test_thunder_os_new_journey_eligibility as fixture
from thunder_os.company_qualification import (CompanyQualification, CompanyQualificationHTTP,
    QUALIFICATION_FIELDS)
from thunder_os.hubspot_console import HubSpotConsole
from thunder_os.hubspot_executor import HubSpotHTTP, HubSpotExecutor
from thunder_os import hubspot_read_budget
from thunder_os.integration_hubspot import FIELDS, HubSpotContractError
from thunder_os.new_journey import NewJourney, NewJourneyError
from thunder_os.new_journey_qualification import QualificationError


class CompanyQualificationTests(unittest.TestCase):
    def setUp(self):
        # Share isolated admission across the fixture readers and writer. Advance
        # time deterministically; schema mutation tests deliberately use TTL=0.
        ticks = [0.0]
        budget = hubspot_read_budget.HubSpotReadBudget(monotonic=lambda: ticks[0],
            sleep=lambda seconds: ticks.__setitem__(0, ticks[0] + seconds), schema_ttl=0)
        p = patch.object(hubspot_read_budget, 'SHARED_READ_BUDGET', budget)
        p.start(); self.addCleanup(p.stop)
        fixture.CanonicalEligibilityTests.setUp(self)
        self.now = fixture.NOW
        self.clock = 1000.0
        self.journey = NewJourney(self.service, checker=self.checker, clock=lambda: self.now)
        self.row = self.journey.create({'campaign_id': 'BR-APOLLO-FIXTURE', 'name': 'Company fixture'}, 'ygor')
        self.row = self.journey.source(self.row['id'], {'mode': 'csv', 'csv_text':
            'email,linkedin_url\n' + fixture.EMAIL + ',' + fixture.PROFILE + '\n'}, 'ygor', self.row['version'])
        self.row = self.journey.check(self.row['id'], 'ygor', self.row['version'])
        self.source_key = self.row['rows'][0]['source_key']
        self.facts = {'decision': 'qualified', 'is_b2b': True, 'country': 'BR',
            'sector': 'industria_b2b', 'annual_revenue_brl': 25_000_000,
            'source_url': 'https://source.example.invalid/report', 'source_date': '2026-10-01',
            'rationale': 'Synthetic source reviewed by Ygor.', 'not_customer': True, 'not_excluded': True}
        # Existing reader fixture is readonly; add only the scoped PATCH contract.
        for prop in self.http.schemas['companies']['results']:
            prop['modificationMetadata'] = {'readOnlyValue': False}
        self.writes = []
        self.timeout, self.bad_receipt, self.bad_readback, self.partial = False, False, False, False
        self.after_write = None

        def request(method, path, body, headers, timeout):
            parsed = urlsplit(path)
            parts = parsed.path.split('/')
            if parsed.path in self.http.fail:
                return self.http(method, path, body, headers, timeout)
            if parsed.path == '/crm/v3/objects/companies/search':
                rows = list(self.http.companies.values())
                def fits(row):
                    return any(all(str(row['properties'].get(f['propertyName']) or '').lower()
                        == str(f['value']).lower() for f in group['filters']) for group in body['filterGroups'])
                rows = [row for row in rows if fits(row)]
                return 200, json.dumps({'results': [self.project(r, body['properties']) for r in rows],
                    'total': len(rows) + int(self.partial)}).encode()
            if method == 'PATCH':
                self.assertEqual(parsed.path, '/crm/v3/objects/companies/500')
                self.assertLessEqual(set(body['properties']), QUALIFICATION_FIELDS)
                self.assertIsNotNone(getattr(self.repo._local, 'connection', None))
                # Independent connection proves attempted intent is already durable.
                with closing(sqlite3.connect(self.repo.path)) as con:
                    attempts = con.execute("SELECT payload FROM thunder_state WHERE kind='hubspot_attempt'").fetchall()
                self.assertTrue(any(json.loads(a[0])['status'] == 'attempted' for a in attempts))
                self.writes.append((method, path, deepcopy(body)))
                self.http.companies['500']['properties'].update(body['properties'])
                if self.after_write:
                    self.after_write()
                if self.timeout:
                    raise TimeoutError('private provider response must never escape')
                if self.bad_readback:
                    self.http.companies['500']['properties']['thunder_icp_status'] = 'pending_review'
                return 200, json.dumps({'id': '999' if self.bad_receipt else '500'}).encode()
            if method == 'GET' and len(parts) == 6 and parts[1:5] == ['crm', 'v3', 'objects', 'companies']:
                row = self.http.companies.get(parts[5])
                if row is None:
                    return 404, b'{}'
                fields = parse_qs(parsed.query).get('properties', [''])[0].split(',')
                return 200, json.dumps(self.project(row, fields)).encode()
            return self.http(method, path, body, headers, timeout)

        self.transport = HubSpotHTTP(self.http.loader, request=request, creation_mode='business_ledger')
        self.console = HubSpotConsole(self.repo, self.transport, read_enabled=True,
            write_enabled=True, clock=lambda: self.clock)
        self.qualifier = CompanyQualification(self.journey, self.console)

    @staticmethod
    def project(row, fields):
        return {**deepcopy(row), 'properties': {k: row['properties'].get(k) for k in fields}}

    def preview(self, facts=None, actor='ygor', version=None, review_hash=None):
        return self.qualifier.preview(self.row['id'], self.source_key,
            self.facts if facts is None else facts, actor,
            self.row['version'] if version is None else version,
            self.row['review_hash'] if review_hash is None else review_hash)

    @staticmethod
    def body(preview):
        return {'preview_id': preview.get('preview_id', preview['id']), 'expected_version': preview['version'],
            'preview_hash': preview['preview_hash'], 'confirm': True}

    def execute(self, preview=None, actor='ygor'):
        return self.qualifier.execute(self.body(preview or self.preview()), actor)

    def test_company_clear_null_readback_verifies_without_second_write(self):
        self.after_write = lambda: self.http.companies['500']['properties'].update(thunder_icp_exclusion_reason=None)
        preview = self.preview()
        saved_plan = deepcopy(preview['plan'])
        result = self.execute(preview)
        self.assertEqual(result['result']['status'], 'verified_state')
        self.assertTrue(result['qualification_saved'])
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(result['plan'], saved_plan)
        self.assertIsNone(self.transport.read('companies', '500', saved_plan['properties'])['properties']['thunder_icp_exclusion_reason'])
        body = {k: v for k, v in self.body(result).items() if k != 'confirm'}
        again = self.qualifier.reconcile(body, 'ygor')
        self.assertEqual(again['result']['status'], 'verified_state')
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(again['plan'], saved_plan)

    def test_company_clear_null_requires_exact_field_schema_and_identity(self):
        port = CompanyQualificationHTTP(self.transport)
        value = {'id': '500', 'properties': {'thunder_icp_exclusion_reason': None}}
        valid_schema = {'properties': {'thunder_icp_exclusion_reason': {'type': 'enumeration', 'readOnlyValue': False}}}
        cases = [({'id': '500', 'properties': {}}, valid_schema, {'thunder_icp_exclusion_reason': ''}, None),
                 (value, {'properties': {}}, {'thunder_icp_exclusion_reason': ''}, None),
                 (value, {'properties': {'thunder_icp_exclusion_reason': {'type': 'string', 'readOnlyValue': False}}}, {'thunder_icp_exclusion_reason': ''}, None),
                 (value, {'properties': {'thunder_icp_exclusion_reason': {'type': 'enumeration', 'readOnlyValue': True}}}, {'thunder_icp_exclusion_reason': ''}, None),
                 (value, valid_schema, {'thunder_icp_exclusion_reason': 'blocked'}, None),
                 ({'id': '999', 'properties': value['properties']}, valid_schema, {'thunder_icp_exclusion_reason': ''}, None),
                 (value, valid_schema, {'thunder_icp_exclusion_reason': ''}, 'domain'),
                 ({'id': '500', 'properties': {'thunder_is_customer': None}}, valid_schema, {'thunder_is_customer': ''}, None)]
        for row, schema, properties, id_property in cases:
            with self.subTest(row=row, schema=schema, properties=properties, id_property=id_property), \
                    patch.object(HubSpotHTTP, 'read', return_value=deepcopy(row)), \
                    patch.object(port, 'metadata', return_value=schema):
                self.assertEqual(port.read('companies', '500', properties, id_property=id_property), row)
        with patch.object(HubSpotHTTP, 'read', return_value=value), patch.object(port, 'metadata', return_value=valid_schema):
            observed = port.read('companies', '500', {'thunder_icp_exclusion_reason': ''})
        self.assertEqual(observed['properties']['thunder_icp_exclusion_reason'], '')
        self.assertIsNone(value['properties']['thunder_icp_exclusion_reason'])
        self.assertEqual(self.writes, [])

    def test_company_review_one_patch_bound_identity(self):
        before_company = deepcopy(self.http.companies)
        before_contacts = deepcopy(self.http.contacts)
        before_draft = self.repo.get_state('journey_draft', self.row['id'])
        p = self.preview()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.http.companies, before_company)
        self.assertEqual(p['before']['id'], '500')
        self.assertEqual(p['company_identity']['contact_id'], '123')
        self.assertEqual(p['after']['properties']['thunder_is_customer'], 'false')
        self.assertEqual(p['after']['properties']['thunder_icp_exclusion_reason'], '')
        self.assertEqual(p['facts'], self.facts)
        self.assertEqual(p['qualification']['source_kind'], 'human_company_review')
        result = self.execute(p)
        self.assertEqual(result['result']['status'], 'verified_state')
        self.assertEqual(result['result']['record_id'], '500')
        self.assertEqual(result['result']['readback_ref'], 'hubspot:51412240:companies:500')
        self.assertFalse(result['result']['write_attribution_confirmed'])
        self.assertEqual(result['external_writes'], 1)
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.http.contacts, before_contacts)
        draft = self.repo.get_state('journey_draft', self.row['id'])
        self.assertEqual(draft['rows'], before_draft['rows'])
        self.assertEqual(draft['version'], before_draft['version'] + 1)
        self.assertEqual(draft['qualifications'][self.source_key], result['qualification'])
        self.assertEqual(draft['qualifications'][self.source_key]['company_id'], '500')
        self.assertTrue(draft['qualifications'][self.source_key]['not_customer'])
        self.assertTrue(draft['qualifications'][self.source_key]['not_excluded'])
        self.assertEqual(draft['checks'], [])
        self.assertEqual(draft['approvals'], {})
        self.assertTrue(result['qualification_saved'])
        self.assertEqual(self.repo.effects(), [])
        self.assertEqual(self.repo.outbox(), [])
        provenance = json.loads(result['after']['properties']['thunder_enrichment_source'])
        self.assertEqual(provenance['source_date'], self.facts['source_date'])
        self.assertEqual(provenance['source_url'], self.facts['source_url'])
        self.assertEqual(result['after']['properties']['thunder_last_enriched_at'], '2026-10-23')
        self.assertNotIn('thunder_is_customer', FIELDS['companies'])

    def test_company_review_rejects_stale_conflict_and_uncertain_retry(self):
        p = self.preview()
        self.timeout = True
        result = self.execute(p)
        self.assertEqual(result['result']['status'], 'pending_reconciliation')
        self.assertFalse(result['result']['retry_write'])
        self.assertEqual(self.repo.get_state('journey_draft', self.row['id'])['qualifications'], {})
        self.assertEqual(len(self.writes), 1)
        self.assertTrue(any(row['status'] != 'released' for row in self.repo.list_state('hubspot_identity')))
        with self.assertRaises(NewJourneyError):
            self.execute(p)
        with self.assertRaises(NewJourneyError):
            self.execute(result)
        # Read-only reconciliation is the existing executor API, never execute again.
        reconciled = HubSpotExecutor(self.repo, CompanyQualificationHTTP(self.transport)).reconcile(p['plan'])
        self.assertEqual(reconciled['status'], 'verified_state')
        self.assertTrue(reconciled['identity_lock_retained'])
        self.assertEqual(len(self.writes), 1)

    def test_preview_requires_literal_three_assertions_and_existing_review_validation(self):
        for changes in ({'not_customer': False}, {'not_excluded': 'true'}, {'not_customer': 1},
            {'opt_out': False}, {'decision': 'approved'}, {'source_url': 'http://localhost/x'},
            {'source_date': '2027-01-01'}, {'rationale': ''}, {'annual_revenue_brl': float('nan')},
            {'annual_revenue_brl': 10 ** 1000}, {'country': 'OTHER'}, {'is_b2b': False},
            {'annual_revenue_brl': 10_000_000}, {'sector': 'outro'}):
            with self.subTest(changes=changes), self.assertRaises((NewJourneyError, QualificationError)):
                self.preview({**self.facts, **changes})
        self.assertEqual(self.writes, [])
        self.assertEqual(self.repo.list_state('hubspot_preview'), [])

    def test_actor_version_hash_source_key_and_exact_execute_body(self):
        for actor in ('codex', 'hermes', 'Ygor', None):
            with self.subTest(actor=actor), self.assertRaises(NewJourneyError):
                self.preview(actor=actor)
        with self.assertRaises(NewJourneyError):
            self.preview(version=True)
        with self.assertRaises(NewJourneyError):
            self.preview(review_hash='wrong')
        with self.assertRaises(NewJourneyError):
            self.qualifier.preview(self.row['id'], 'missing', self.facts, 'ygor',
                self.row['version'], self.row['review_hash'])
        p = self.preview()
        for change in ({'confirm': False}, {'confirm': 1}, {'expected_version': True},
                       {'preview_hash': 'bad'}, {'extra': True}):
            with self.subTest(change=change), self.assertRaises(NewJourneyError):
                self.qualifier.execute({**self.body(p), **change}, 'ygor')
        with self.assertRaises(NewJourneyError):
            self.execute(p, actor='codex')
        self.assertEqual(self.writes, [])

    def test_missing_company_identity_is_retained(self):
        self.http.contacts['123']['associations']['companies']['results'][0]['id'] = '999'
        with self.assertRaisesRegex(NewJourneyError, 'identity_changed'):
            self.preview()
        self.assertEqual(self.writes, [])

    def test_target_values_changed_after_preview_cannot_write(self):
        p = self.preview()
        self.http.companies['500']['properties']['thunder_is_customer'] = 'true'
        result = self.execute(p)
        self.assertEqual(result['result']['status'], 'needs_human')
        self.assertFalse(result['retry_write'])
        self.assertEqual(self.writes, [])

    def test_single_company_review_covers_two_distinct_linked_contacts(self):
        self.http.contacts['123']['properties']['tipo_do_lead'] = ''
        self.row = self.journey.check(self.row['id'], 'ygor', self.row['version'])
        p = self.preview()
        result = self.execute(p)
        self.assertEqual(result['result']['status'], 'verified_state')
        second = deepcopy(self.http.contacts['123'])
        second['id'] = '124'
        profile = 'https://www.linkedin.com/in/second-synthetic'
        second['properties'].update(email='second@example.invalid', hs_additional_emails='',
            hs_linkedin_url=profile, linkedin_url=profile, tipo_do_lead='')
        self.http.contacts['124'] = second
        self.http.search = {'total':1, 'results':[{'id':'124'}]}
        row = {'source_key':'second-source', 'email':'second@example.invalid', 'linkedin_url':profile}
        checked = self.checker([row], 'BR-APOLLO-FIXTURE')[0]
        self.assertEqual(checked['qualification_context']['company_id'], '500')
        self.assertEqual(checked['qualification_context']['contact_id'], '124')
        self.assertEqual({c['key']:c['status'] for c in checked['checks']},
            {key:'pass' for key in ('icp','client','exclusions','open_deal','refusal','membership')})
        second['properties']['opt_out'] = 'true'
        checked = self.checker([row], 'BR-APOLLO-FIXTURE')[0]
        self.assertEqual(next(c for c in checked['checks'] if c['key']=='refusal')['status'], 'blocked')
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.http.contacts['123']['properties']['tipo_do_lead'], '')
        self.assertEqual(second['properties']['tipo_do_lead'], '')

    def test_company_reassociation_after_preview_cannot_write(self):
        p = self.preview()
        other = deepcopy(self.http.companies['500']); other['id'] = '600'
        other['properties'].update(name='Other linked company', domain='other.invalid')
        self.http.companies['600'] = other
        self.http.contacts['123']['associations']['companies']['results'][0]['id'] = '600'
        result = self.execute(p)
        self.assertEqual(result['result']['status'], 'needs_human')
        self.assertEqual(self.writes, [])

    def test_draft_change_invalidates_approval(self):
        p = self.preview()
        draft = self.repo.get_state('journey_draft', self.row['id'])
        self.repo.put_state('journey_draft', draft['id'], {**draft, 'name': 'changed'})
        with self.assertRaises(NewJourneyError):
            self.execute(p)
        self.assertEqual(self.writes, [])

    def test_sheet_change_invalidates_approval(self):
        p = self.preview()
        campaign_file = self.root / 'brain' / 'campaign.md'
        campaign_file.write_text(campaign_file.read_text() + '\nChanged source\n')
        with self.assertRaises(NewJourneyError):
            self.execute(p)
        self.assertEqual(self.writes, [])

    def test_pause_write_disabled_and_expiry_reject_without_attempt(self):
        p = self.preview()
        self.console.write_enabled = False
        with self.assertRaises(NewJourneyError): self.execute(p)
        self.console.write_enabled = True
        self.repo.put_state('control', 'global', {'paused': True})
        with self.assertRaises(NewJourneyError): self.execute(p)
        self.repo.put_state('control', 'global', {'paused': False})
        self.clock = p['expires_at']
        with self.assertRaises(NewJourneyError): self.execute(p)
        self.assertEqual(self.repo.list_state('hubspot_attempt'), [])
        self.assertEqual(self.writes, [])

    def test_dispatch_rechecks_pause_and_expiry_after_durable_intent(self):
        p = self.preview()
        original = CompanyQualificationHTTP.prepare
        calls = 0
        def expire(port, *args, **kwargs):
            nonlocal calls
            calls += 1
            result = original(port, *args, **kwargs)
            # First reprepare is executor preflight; next is the dispatch fence.
            if calls == 2:
                self.clock = p['expires_at']
            return result
        with patch.object(CompanyQualificationHTTP, 'prepare', expire):
            result = self.execute(p)
        self.assertEqual(result['result']['status'], 'pending_reconciliation')
        self.assertEqual(self.writes, [])

    def test_unknown_schema_readonly_property_enum_or_partial_dedupe_fail_closed(self):
        schemas = self.http.schemas['companies']['results']
        field = next(x for x in schemas if x['name'] == 'thunder_icp_exclusion_reason')
        field['type'] = 'string'
        with self.assertRaises(HubSpotContractError): self.preview()
        field['type'] = 'enumeration'
        field['modificationMetadata']['readOnlyValue'] = True
        with self.assertRaises(HubSpotContractError): self.preview()
        field['modificationMetadata']['readOnlyValue'] = False
        self.partial = True
        with self.assertRaises(HubSpotContractError): self.preview()
        self.assertEqual(self.writes, [])

    def test_no_duplicate_name_or_domain_can_select_another_company(self):
        other = deepcopy(self.http.companies['500']); other['id'] = '600'
        self.http.companies['600'] = other
        with self.assertRaisesRegex(HubSpotContractError, 'ambiguous_duplicate'):
            self.preview()

    def test_receipt_mismatch_cannot_claim_verified(self):
        p = self.preview(); self.bad_receipt = True
        result = self.execute(p)
        self.assertEqual(result['result']['status'], 'pending_reconciliation')
        self.assertEqual(len(self.writes), 1)

    def test_success_response_without_matching_readback_is_pending(self):
        p = self.preview(); self.bad_readback = True
        result = self.execute(p)
        self.assertEqual(result['result']['status'], 'pending_reconciliation')
        self.assertFalse(result['retry_write'])
        self.assertEqual(len(self.writes), 1)

    def test_preview_integrity_rejects_tampering(self):
        p = self.preview()
        saved = self.repo.get_state('hubspot_preview', p['id'])
        saved['facts']['source_date'] = '2026-09-01'
        changed = self.repo.put_state('hubspot_preview', p['id'], saved)
        with self.assertRaisesRegex(NewJourneyError, 'integrity_mismatch'):
            self.execute(changed)
        self.assertEqual(self.writes, [])

    def test_two_concurrent_clicks_produce_at_most_one_patch(self):
        p = self.preview()
        def click():
            try: return self.execute(p)
            except NewJourneyError: return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: click(), range(2)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(len(self.writes), 1)

    def test_disqualified_decision_and_explicit_enum_clear_stay_scoped(self):
        self.http.companies['500']['properties']['thunder_icp_exclusion_reason'] = 'fora_porte'
        p = self.preview({**self.facts, 'decision': 'disqualified'})
        result = self.execute(p)
        self.assertEqual(result['result']['status'], 'verified_state')
        self.assertEqual(self.writes[0][2]['properties']['thunder_icp_exclusion_reason'], '')
        self.assertEqual(self.writes[0][2]['properties']['thunder_icp_status'], 'disqualified')
        self.assertTrue(all(key not in self.writes[0][2]['properties'] for key in
            ('opt_out', 'hs_email_optout', 'thunder_suppressed', 'thunder_outreach_risk',
             'hubspot_owner_id', 'lifecyclestage', 'dealstage', 'campanha_ativa', 'executor')))

    def test_scoped_prepare_cannot_create_or_expand_global_allowlist(self):
        p = self.preview(); port = CompanyQualificationHTTP(self.transport)
        for kind, rid, props in [('contacts', '123', p['plan']['properties']),
            ('companies', None, p['plan']['properties']),
            ('companies', '500', {**p['plan']['properties'], 'thunder_suppressed': 'false'})]:
            with self.subTest(kind=kind, rid=rid), self.assertRaises(HubSpotContractError):
                port.prepare(kind, props, 'effect', rid)
        with self.assertRaisesRegex(HubSpotContractError, 'unsupported_property'):
            self.transport.prepare('companies', {k: v for k, v in p['plan']['properties'].items()
                if k != 'thunder_icp_exclusion_reason'}, 'effect', '500')
        self.assertEqual(self.writes, [])

    def test_reconcile_is_readonly_after_timeout_even_if_expired_or_paused(self):
        p = self.preview(); self.timeout = True
        result = self.execute(p)
        self.clock = p['expires_at'] + 1
        self.repo.put_state('control', 'global', {'paused': True})
        self.console.write_enabled = False
        body = {k: v for k, v in self.body(result).items() if k != 'confirm'}
        recovered = self.qualifier.reconcile(body, 'ygor')
        self.assertEqual(recovered['result']['status'], 'verified_state')
        self.assertTrue(recovered['result']['identity_lock_retained'])
        self.assertEqual(recovered['external_writes'], 1)
        self.assertFalse(recovered['qualification_saved'])
        self.assertEqual(len(self.writes), 1)
        with self.assertRaises(NewJourneyError): self.qualifier.reconcile(body, 'ygor')
        with self.assertRaises(NewJourneyError): self.qualifier.reconcile(body, 'codex')
        self.repo.put_state('control', 'global', {'paused': False})
        recovered = self.qualifier.reconcile({k: v for k, v in self.body(recovered).items()
                                             if k != 'confirm'}, 'ygor')
        self.assertTrue(recovered['qualification_saved'])
        self.assertEqual(self.repo.get_state('journey_draft', self.row['id'])['qualifications']
                         [self.source_key]['facts'], {k: v for k, v in self.facts.items()
                             if k not in {'not_customer', 'not_excluded'}})
        self.assertEqual(len(self.writes), 1)

    def test_reconcile_before_attempt_is_rejected(self):
        p = self.preview()
        body = {k: v for k, v in self.body(p).items() if k != 'confirm'}
        with self.assertRaisesRegex(NewJourneyError, 'attempt_required'):
            self.qualifier.reconcile(body, 'ygor')
        self.assertEqual(self.writes, [])

    def test_pause_during_dispatch_read_prevents_patch(self):
        p = self.preview()
        original = CompanyQualificationHTTP.prepare
        calls = 0
        def pause(port, *args, **kwargs):
            nonlocal calls
            calls += 1
            result = original(port, *args, **kwargs)
            if calls == 2:
                self.repo.put_state('control', 'global', {'paused': True})
            return result
        with patch.object(CompanyQualificationHTTP, 'prepare', pause):
            result = self.execute(p)
        self.assertEqual(result['result']['status'], 'pending_reconciliation')
        self.assertEqual(self.writes, [])

    def test_noop_reads_back_without_patch_and_duplicate_preview_never_rearms(self):
        p = self.preview()
        self.http.companies['500']['properties'].update(p['plan']['properties'])
        # A new draft revision is needed to review the new before-state.
        self.row = self.journey.check(self.row['id'], 'ygor', self.row['version'])
        p = self.preview()
        self.assertEqual(p['plan']['changes'], {})
        result = self.execute(p)
        self.assertEqual(result['result']['status'], 'verified_state')
        self.assertEqual(result['external_writes'], 0)
        with self.assertRaisesRegex(NewJourneyError, 'journey_version_conflict'):
            self.preview()
        self.assertEqual(self.writes, [])

    def test_wrong_portal_and_provider_error_never_save_review(self):
        self.http.portal['portalId'] = '999'
        with self.assertRaises(NewJourneyError): self.preview()
        self.http.portal['portalId'] = '51412240'
        self.http.fail['/crm/v3/objects/companies/500'] = 429
        with self.assertRaises(NewJourneyError): self.preview()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.repo.list_state('hubspot_preview'), [])

    def test_metadata_change_after_preview_is_retained(self):
        p = self.preview()
        field = next(p for p in self.http.schemas['companies']['results']
                     if p['name'] == 'thunder_icp_status')
        field['modificationMetadata']['readOnlyValue'] = True
        result = self.execute(p)
        self.assertEqual(result['result']['status'], 'needs_human')
        self.assertEqual(self.writes, [])

    def test_dispatch_guard_rejects_request_outside_exact_company_patch(self):
        p = self.preview()
        with patch('thunder_os.hubspot_executor.wire_request', return_value={
                'method': 'PATCH', 'path': '/crm/v3/objects/contacts/123',
                'body': {'properties': {'opt_out': 'false'}}}):
            result = self.execute(p)
        self.assertEqual(result['result']['status'], 'pending_reconciliation')
        self.assertEqual(self.writes, [])

    def test_verified_remote_readback_preserves_concurrent_draft_edit(self):
        p = self.preview()
        def change_draft():
            draft = self.repo.get_state('journey_draft', self.row['id'])
            self.repo.put_state('journey_draft', draft['id'], {**draft, 'name': 'Concurrent user edit'})
        self.after_write = change_draft
        result = self.execute(p)
        self.assertEqual(result['result']['status'], 'verified_state')
        self.assertFalse(result['qualification_saved'])
        self.assertEqual(result['qualification_error_code'], 'journey_version_conflict')
        draft = self.repo.get_state('journey_draft', self.row['id'])
        self.assertEqual(draft['name'], 'Concurrent user edit')
        self.assertEqual(draft['qualifications'], {})


if __name__ == '__main__':
    unittest.main()
