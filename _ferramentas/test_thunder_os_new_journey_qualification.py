"""Human ICP review is internal, identity-bound and never overrides blockers."""
from copy import deepcopy
import csv
import io
from datetime import timedelta
import unittest

import test_thunder_os_new_journey_eligibility as fixture
from thunder_os.new_journey import NewJourney, NewJourneyError
from thunder_os.new_journey_qualification import QualificationError, decision


class ReviewedQualificationTests(fixture.CanonicalEligibilityTests):
    def setUp(self):
        super().setUp()
        self.now = fixture.NOW
        self.journey = NewJourney(self.service, checker=self.checker, clock=lambda: self.now)
        self.row = self.journey.create({'campaign_id': 'BR-APOLLO-FIXTURE', 'name': 'Review fixture'}, 'ygor')
        self.row = self.journey.source(self.row['id'], {'mode': 'csv',
            'csv_text': 'email,linkedin_url\n' + fixture.EMAIL + ',' + fixture.PROFILE + '\n'}, 'ygor', self.row['version'])
        self.facts = {'decision': 'qualified', 'is_b2b': True, 'country': 'BR',
            'sector': 'industria_b2b', 'annual_revenue_brl': 20_000_000,
            'source_url': 'https://source.example.invalid/annual-report', 'source_date': '2026-10-01',
            'rationale': 'Synthetic reviewed annual report, not a claim about a real company.'}

    def refresh(self):
        self.row = self.journey.check(self.row['id'], 'ygor', self.row['version'])

    def qualify(self, **changes):
        self.row = self.journey.qualify(self.row['id'], self.row['rows'][0]['source_key'],
            {**self.facts, **changes}, 'ygor', self.row['version'], self.row['review_hash'])
        return {c['key']: c['status'] for c in self.row['rows'][0]['checks']}

    def test_review_fills_icp_without_crm_qualification_properties_or_writes(self):
        props = self.http.companies['500']['properties']
        for key in ('thunder_icp_status', 'thunder_icp_sector', 'thunder_annual_revenue_estimate',
                    'thunder_last_enriched_at', 'thunder_enrichment_source'):
            props.pop(key)
        before = deepcopy(self.http.companies)
        self.refresh()
        self.assertEqual(self.row['rows'][0]['checks'][0]['status'], 'unknown')
        self.assertEqual(self.qualify()['icp'], 'pass')
        self.assertEqual(self.http.companies, before)
        self.assertEqual(self.repo.effects(), [])
        self.assertEqual(self.repo.outbox(), [])
        self.assertEqual(self.row['qualifications'][self.row['rows'][0]['source_key']]['actor'], 'ygor')

    def test_human_fit_does_not_override_optout_client_open_deal_or_fixed_exclusion(self):
        self.http.contacts['123']['properties']['opt_out'] = 'true'
        self.http.companies['500']['properties']['thunder_is_customer'] = 'true'
        self.http.companies['500']['properties']['name'] = 'Coca-Cola FEMSA'
        self.http.deals['4']['properties']['hs_is_closed'] = 'false'
        self.refresh()
        checks = self.qualify()
        for key in ('refusal', 'client', 'open_deal', 'exclusions'):
            self.assertEqual(checks[key], 'blocked')

    def test_review_cannot_override_canonical_icp_disqualification(self):
        self.http.companies['500']['properties']['thunder_icp_status'] = 'disqualified'
        self.refresh()
        self.assertEqual(self.qualify()['icp'], 'blocked')

    def test_review_requires_bound_identity_source_date_and_exact_actor_version(self):
        self.refresh()
        for change in ({'source_url': 'http://localhost/x'}, {'source_date': '2027-01-01'},
                       {'annual_revenue_brl': float('nan')}, {'rationale': ''}):
            with self.subTest(change=change), self.assertRaises(QualificationError):
                self.qualify(**change)
        for actor in ('hermes', 'codex'):
            with self.assertRaises(NewJourneyError):
                self.journey.qualify(self.row['id'], self.row['rows'][0]['source_key'], self.facts,
                                     actor, self.row['version'], self.row['review_hash'])
        del self.http.contacts['123']['properties']['hs_additional_emails']
        with self.assertRaisesRegex(NewJourneyError, 'journey_checks_changed'):
            self.qualify()

    def test_review_invalidates_on_company_sheet_or_age_change(self):
        self.refresh(); self.qualify()
        record = next(iter(self.row['qualifications'].values()))
        context = self.row['checks'][0]['qualification_context']
        sheet_hash = self.row['sheet_hash']
        self.assertTrue(decision(record, context, sheet_hash, self.now)['fits'])
        self.assertIsNone(decision(record, {**context, 'binding_hash': 'changed'}, sheet_hash, self.now))
        self.assertIsNone(decision(record, context, 'changed', self.now))
        self.assertIsNone(decision(record, context, sheet_hash, self.now + timedelta(days=15)))

    def test_company_changed_after_presentation_rejects_stale_review_post(self):
        self.refresh()
        before = deepcopy(self.row)
        company = deepcopy(self.http.companies['500'])
        company['id'] = '600'
        company['properties'].update(name='Another company', domain='other.example.invalid')
        self.http.companies['600'] = company
        self.http.contacts['123']['associations']['companies']['results'][0]['id'] = '600'
        with self.assertRaisesRegex(NewJourneyError, 'journey_checks_changed'):
            self.qualify()
        self.assertEqual(self.journey.get(before['id'], 'ygor')['qualifications'], {})
        self.assertEqual(self.journey.get(before['id'], 'ygor')['version'], before['version'])

    def test_disqualified_or_outside_threshold_never_mints_pass(self):
        self.refresh()
        self.assertEqual(self.qualify(decision='disqualified')['icp'], 'blocked')
        self.assertEqual(self.qualify(annual_revenue_brl=10_000_000)['icp'], 'blocked')


class ReviewedQualificationHTTPTests(fixture.CanonicalEligibilityHTTPTests):
    def test_confirmed_apollo_absence_never_exports_imported_foreign_identifiers(self):
        wizard = self.server.thunder_new_journey
        row = wizard.create({'campaign_id': 'BR-APOLLO-FIXTURE', 'name': 'Canonical export binding'}, 'ygor')
        row = wizard.source(row['id'], {'mode': 'csv', 'csv_text':
            'email,apollo_contact_id,apollo_person_id\n' + fixture.EMAIL + ',foreign-contact,foreign-person\n'},
            'ygor', row['version'])
        row = wizard.check(row['id'], 'ygor', row['version'])
        self.assertEqual(row['counts']['eligible'], 1)
        row = wizard.revise_sequence(row['id'], [{'id':'E1', 'channel':'email',
            'subject':'Fixture subject', 'content':'Original exact text\nSecond paragraph',
            'order':1, 'interval_hours':0}], 'ygor', row['version'],
            apollo_sequence_id='sequence-fixture', apollo_sequence_query='Fixture sequence')
        row = wizard.approve_step(row['id'], 'E1', 'ygor', row['version'], row['review_hash'])
        output = wizard.export(row['id'], 'ygor', row['version'], row['review_hash'])
        exported = next(csv.DictReader(io.StringIO(output['csv_text'].lstrip('\ufeff'))))
        self.assertEqual(exported['apollo_contact_id'], '')
        self.assertEqual(exported['apollo_person_id'], '')
        self.assertEqual(exported['hubspot_id'], '123')
        snapshot = self.repo.get_state('journey_draft', row['id'])['export_snapshot']
        self.assertIsNone(snapshot['recipient_bindings'][row['rows'][0]['source_key']]['contact_id'])
        self.assertEqual(self.repo.effects(), [])
        self.assertEqual(self.repo.outbox(), [])

    def test_real_checker_review_route_persists_only_internal_state(self):
        props = self.http.companies['500']['properties']
        for key in ('thunder_icp_status', 'thunder_icp_sector', 'thunder_annual_revenue_estimate',
                    'thunder_last_enriched_at', 'thunder_enrichment_source'):
            props.pop(key)
        before = deepcopy(self.http.companies)
        status, draft = self.request('create', method='POST', nonce=True,
            body={'campaign_id': 'BR-APOLLO-FIXTURE', 'name': 'HTTP reviewed ICP'})
        self.assertEqual(status, 200)
        ident = draft['id']
        status, draft = self.request(ident + '/source', method='POST', nonce=True,
            body={'expected_version': draft['version'], 'mode': 'csv',
                  'csv_text': 'email\n' + fixture.EMAIL + '\n'})
        self.assertEqual(status, 200)
        status, draft = self.request(ident + '/check', method='POST', nonce=True,
            body={'expected_version': draft['version']})
        self.assertEqual(status, 200)
        body = {'expected_version': draft['version'], 'review_hash': draft['review_hash'],
            'source_key': draft['rows'][0]['source_key'], 'facts': {
                'decision': 'qualified', 'is_b2b': True, 'country': 'BR',
                'sector': 'industria_b2b', 'annual_revenue_brl': 20_000_000,
                'source_url': 'https://source.example.invalid/report', 'source_date': '2026-10-01',
                'rationale': 'Synthetic source reviewed by the authenticated user.'}}
        self.assertEqual(self.request(ident + '/qualify', method='POST', body=body)[0], 401)
        self.assertEqual(self.request(ident + '/qualify', method='POST', nonce=True,
                                     actor='codex', body=body)[0], 403)
        status, saved = self.request(ident + '/qualify', method='POST', nonce=True, body=body)
        self.assertEqual(status, 200)
        self.assertEqual(next(c for c in saved['rows'][0]['checks'] if c['key'] == 'icp')['status'], 'pass')
        self.assertEqual(next(iter(saved['qualifications'].values()))['actor'], 'ygor')
        self.assertEqual(self.http.companies, before)
        self.assertEqual(self.repo.effects(), [])
        self.assertEqual(self.repo.outbox(), [])
        self.assertEqual(self.request(ident + '/qualify', method='POST', nonce=True, body=body)[0], 409)


if __name__ == '__main__':
    unittest.main()
