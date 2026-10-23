"""Isolated repository and HubSpot read doubles; sockets and writes forbidden."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from thunder_os.db import Repository
from thunder_os.hubspot_executor import HubSpotHTTP
from thunder_os.new_journey_checks import KEYS, NewJourneyChecks, NewJourneyChecksError, _HubSpotRead
from thunder_os.service import ThunderService

NOW = datetime(2026, 10, 23, 15, tzinfo=timezone.utc)
EMAIL = 'person@company.invalid'
PROFILE = 'https://www.linkedin.com/in/synthetic-person'
CAMPAIGN = 'BR-FIXTURE'


def lead(**extra):
    return {'source_key': 'csv:1', 'rowstablekey': 'stable-fixture', 'email': EMAIL,
            'company': 'Synthetic industry', **extra}


def normalized(result):
    return [{k: c[k] for k in ('key', 'status', 'reason', 'source')} for c in result[0]['checks']]


class ReadPort:
    def __init__(self, *, props=None, deals=False, total=1, portal='51412240'):
        self.calls, self.props, self.deals, self.total, self.portal = [], props or {}, deals, total, portal

    def __call__(self, method, path, body, headers, timeout):
        self.calls.append((method, path, deepcopy(body), timeout))
        if path == '/account-info/v3/details':
            value = {'portalId': self.portal}
        elif method == 'POST' and path == '/crm/v3/objects/contacts/search':
            value = {'results': [{'id': '123'}] * self.total, 'total': self.total}
        elif path.startswith('/crm/v3/objects/contacts/123?'):
            value = {'id': '123', 'archived': False, 'properties': {'email': EMAIL, **self.props},
                     'associations': {'deals': {'results': [{'id': '456'}] if self.deals else []}}}
        elif path.startswith('/crm/v3/objects/deals/456?'):
            value = {'id': '456', 'archived': False, 'properties': {'hs_is_closed': 'false', 'dealname': 'Synthetic'}}
        else:
            raise AssertionError('unexpected request')
        return 200, json.dumps(value).encode()


class NewJourneyCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Repository(Path(self.tmp.name) / 'fixture.db')
        self.service = ThunderService(self.repo)
        self.now = NOW
        self.checker = NewJourneyChecks(self.service, clock=lambda: self.now)
        for target in ('socket.create_connection', 'socket.socket.connect', 'socket.getaddrinfo'):
            p = patch(target, side_effect=AssertionError('network forbidden'))
            self.addCleanup(p.stop)
            self.addCleanup(p.start().assert_not_called)

    def identity(self, person=EMAIL, contact='123', ident=None):
        self.repo.put_state('identity', ident or person, {'actor': 'ygor', 'status': 'canonical',
            'canonical_person': person, 'canonical_account': 'company.invalid',
            'records': [{'person_key': person, 'domain': 'company.invalid', 'hubspot_contact_id': contact,
                         'source': source} for source in ('hubspot', 'fixture')]})

    def check(self, row=None, checker=None):
        result = (checker or self.checker)([row or lead()], CAMPAIGN)[0]
        self.assertEqual([c['key'] for c in result['checks']], list(KEYS))
        return {c['key']: c for c in result['checks']}

    def port(self, **kwargs):
        transport = ReadPort(**kwargs)
        port = HubSpotHTTP(lambda: 'fixture-secret', request=transport, timeout=4)
        return NewJourneyChecks(self.service, hubspot=port, clock=lambda: self.now), transport, port

    def test_csv_claims_never_become_facts_and_keys_preserved(self):
        row = lead(client=False, open_deal=False, suppressed=False, verified=True, icp=True,
                   memberships={'apollo': False, 'instantly': False, 'chattie': False, 'laura': False},
                   checks=[{'key': k, 'status': 'pass'} for k in KEYS])
        before = deepcopy(row)
        result = self.checker([row], CAMPAIGN)[0]
        self.assertEqual(result['source_key'], 'csv:1')
        self.assertEqual(result['rowstablekey'], 'stable-fixture')
        self.assertTrue(all(c['status'] == 'unknown' for c in result['checks']))
        self.assertEqual(before, row)
        self.assertTrue(result['retained'])

    def test_canonical_email_refusal_blocks_linkedin_across_verified_identity(self):
        self.identity()
        self.identity(PROFILE)
        for classification in ('negative', 'opt_out', 'risk'):
            self.repo.put_state('reply', 'refusal', {'person_key': EMAIL, 'classification': classification,
                'state': 'classified', 'channel': 'email', 'content': 'Synthetic refusal'})
            check = self.check(lead(email='', linkedin_url=PROFILE))['refusal']
            self.assertEqual(check['status'], 'blocked')
            self.assertEqual(check['reason'], 'canonical_refusal_all_channels')

    def test_any_open_human_reply_blocks_new_cold_approach(self):
        self.repo.put_state('reply', 'human', {'person_key': EMAIL, 'human_reply': True,
            'identity_confirmed': True, 'state': 'needs_classification'})
        self.assertEqual(self.check()['refusal']['reason'], 'human_reply_requires_conversation_review')

    def test_unlinked_name_and_duplicate_contact_identity_stay_unknown(self):
        self.identity('other@company.invalid')
        self.repo.put_state('reply', 'other', {'person_key': 'other@company.invalid', 'name': 'Same name',
            'state': 'classified', 'classification': 'negative'})
        self.assertEqual(self.check(lead(name='Same name'))['refusal']['status'], 'unknown')
        self.identity()
        self.identity(EMAIL, '999', 'conflict')
        checks = self.check()
        self.assertTrue(all(c['status'] == 'unknown' for c in checks.values()))
        self.assertTrue(all(c['reason'] == 'identity_ambiguous' for c in checks.values()))

    def test_fixed_clients_exclusions_and_loa_requirements(self):
        for company, key in [('Kawasaki Brasil', 'client'), ('PTA Rental', 'client'), ('Coca-Cola FEMSA', 'exclusions'),
                             ('Empresa Bebidas', 'exclusions'), ('Yamaha', 'exclusions'),
                             ('Grupo Bimbo', 'client'), ('Cu\u00e9tara', 'client'), ('Gonac', 'client')]:
            with self.subTest(company=company):
                check = self.check(lead(company=company, loa_ok=True))[key]
                self.assertEqual(check['status'], 'blocked')
                self.assertTrue(check['source'].startswith('policy:'))

    def test_live_customer_open_deal_optout_and_membership_are_positive_blockers(self):
        checker, transport, original = self.port(props={'lifecyclestage': 'customer', 'hs_email_optout': 'true',
            'campanha_ativa': 'BR-OTHER', 'executor': 'apollo', 'tipo_do_lead': 'NAO E ICP'}, deals=True)
        checks = self.check(checker=checker)
        for key in ('client', 'refusal', 'membership', 'open_deal', 'icp'):
            self.assertEqual(checks[key]['status'], 'blocked')
        self.assertTrue(all(m in {'GET', 'POST'} and (m != 'POST' or p.endswith('/search'))
                            for m, p, _, _ in transport.calls))
        self.assertTrue(all(0 < timeout <= 4 for _, _, _, timeout in transport.calls))
        self.assertIsNone(original._token)
        self.assertNotIn('fixture-secret', json.dumps(checks))

    def test_empty_fields_and_empty_deal_snapshot_never_prove_absence(self):
        checker, _, _ = self.port()
        checks = self.check(checker=checker)
        self.assertTrue(all(c['status'] == 'unknown' for c in checks.values()))
        self.assertEqual(checks['membership']['reason'], 'hubspot_membership_property_missing')
        self.assertFalse(checks['membership']['coverage']['complete'])

    def test_current_hubspot_empty_membership_is_not_provider_coverage(self):
        checker, _, _ = self.port(props={'campanha_ativa': '', 'executor': ''})
        check = self.check(checker=checker)['membership']
        self.assertEqual(check['status'], 'unknown')
        self.assertEqual(check['reason'], 'apollo_instantly_membership_coverage_unavailable')

    def test_strict_transport_rejects_mutation_before_credentials_and_request(self):
        checker, transport, original = self.port()
        reader = _HubSpotRead(original)
        for method, path in [('PATCH', '/crm/v3/objects/contacts/123'),
                ('POST', '/crm/v3/objects/contacts'), ('GET', 'https://evil.invalid/account-info/v3/details')]:
            with self.assertRaises(NewJourneyChecksError):
                reader.request(method, path, {}, {}, 4)
        self.assertEqual(transport.calls, [])
        self.assertIsNone(reader.port._token)

    def test_portal_mismatch_and_duplicate_live_email_do_not_link(self):
        for kwargs in ({'portal': '999'}, {'total': 2}):
            checker, _, _ = self.port(**kwargs)
            self.assertTrue(all(c['status'] == 'unknown' for c in self.check(checker=checker).values()))

    def test_ten_contact_budget_and_deadline_keep_remainder_unknown(self):
        checker, transport, _ = self.port()
        rows = [lead(source_key='csv:' + str(i), email=f'person{i}@company.invalid') for i in range(12)]
        result = checker(rows, CAMPAIGN)
        self.assertEqual(len(result), 12)
        searches = [c for c in transport.calls if c[0] == 'POST']
        self.assertEqual(len(searches), 10)
        self.assertEqual(result[-1]['coverage']['hubspot_reason'], 'checks_hubspot_budget_exhausted')
        ticks = iter([0, 0])
        with patch('thunder_os.new_journey_checks.time.monotonic', side_effect=lambda: next(ticks, 16)):
            result = checker([lead()], CAMPAIGN)
        self.assertEqual(result[0]['coverage']['hubspot_reason'], 'checks_hubspot_budget_exhausted')

    def test_stale_freeform_research_never_proves_icp(self):
        self.identity()
        self.repo.put_state('research', 'company.invalid', {'status': 'validated', 'icp': True,
            'facts': [{'observed_at': '2025-01-01', 'statement': 'Fits every ICP'}]})
        check = self.check()['icp']
        self.assertEqual(check['status'], 'unknown')
        self.assertEqual(check['reason'], 'canonical_research_stale')

    def test_stable_normalized_checks_ignore_clock_and_unrelated_person_records(self):
        self.identity()
        first = normalized(self.checker([lead()], CAMPAIGN))
        self.now += timedelta(minutes=1)
        self.repo.put_state('reply', 'unrelated', {'person_key': 'other@company.invalid', 'state': 'classified',
            'classification': 'negative'})
        self.repo.put_state('cadence', 'unrelated', {'person_key': 'other@company.invalid', 'status': 'active'})
        self.assertEqual(first, normalized(self.checker([lead()], CAMPAIGN)))
        self.repo.put_state('reply', 'target', {'person_key': EMAIL, 'state': 'classified', 'classification': 'negative'})
        second = normalized(self.checker([lead()], CAMPAIGN))
        self.assertNotEqual(first, second)
        before = self.check()['refusal']['source']
        self.repo.put_state('reply', 'target', {'person_key': EMAIL, 'state': 'classified', 'classification': 'opt_out'})
        self.assertNotEqual(before, self.check()['refusal']['source'])

    def test_sheet_mtime_is_not_review_provenance_but_actual_hash_is(self):
        sheet = {'status': 'complete', 'fingerprint': 'declared-sheet', 'fields': [
            {'key': 'icp', 'status': 'ok', 'observed_sha256': 'a' * 64, 'content': 'Canonical ICP',
             'source_updated_at': '2026-10-23T10:00:00Z'}]}
        with patch.object(self.checker.service, 'campaign_sheet_view', side_effect=lambda _: deepcopy(sheet)):
            first = normalized(self.checker([lead()], CAMPAIGN))
            sheet['fields'][0]['source_updated_at'] = '2026-10-23T11:00:00Z'
            self.assertEqual(first, normalized(self.checker([lead()], CAMPAIGN)))
            sheet['fields'][0]['observed_sha256'] = 'b' * 64
            self.assertNotEqual(first, normalized(self.checker([lead()], CAMPAIGN)))

    def test_refusal_remains_primary_over_other_human_replies_and_paused_cadence(self):
        self.repo.put_state('reply', 'a-negative', {'person_key': EMAIL, 'state': 'classified', 'classification': 'negative'})
        self.repo.put_state('reply', 'z-human', {'person_key': EMAIL, 'state': 'classified', 'classification': 'positive'})
        self.repo.put_state('cadence', 'paused', {'person_key': EMAIL, 'status': 'paused_reply'})
        self.assertEqual(self.check()['refusal']['reason'], 'canonical_refusal_all_channels')

    def test_no_repository_writes_no_schema_changes_and_readonly_enforced(self):
        with self.repo._connect() as con:
            before = list(con.iterdump())
        self.check()
        with self.repo._connect() as con:
            self.assertEqual(before, list(con.iterdump()))
        with self.assertRaises(sqlite3.OperationalError):
            self.checker.repo.put_state('reply', 'forbidden', {})

    def test_bounds_and_errors_have_stable_namespace_without_input_content(self):
        for rows, campaign in (([lead()] * 201, CAMPAIGN), ([{'source_key': None}], CAMPAIGN),
                               ([lead()], '../private'), ([None], CAMPAIGN)):
            with self.assertRaises(NewJourneyChecksError) as exc:
                self.checker(rows, campaign)
            self.assertTrue(exc.exception.code.startswith('checks_'))
            self.assertNotIn('private', str(exc.exception))


if __name__ == '__main__':
    unittest.main()
