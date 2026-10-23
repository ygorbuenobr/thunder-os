"""Real checker + scoped HTTP ports + real readonly ledger; providers simulated."""
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from test_thunder_os_campaign_registry import config
from test_thunder_os_new_journey_hubspot import FakeHTTP
import test_thunder_os_new_journey_http as http_fixture
from thunder_os.apollo_prospect_readonly import ApolloProspectReadonly
from thunder_os.campaign_sheet import FIELDS
from thunder_os.db import Repository
from thunder_os.external_email_import import INSTANTLY_WORKSPACE
from thunder_os.hubspot_executor import HubSpotHTTP
from thunder_os.knowledge import KnowledgeStore
from thunder_os.new_journey import NewJourney
from thunder_os.new_journey_checks import KEYS, NewJourneyChecks
from thunder_os.new_journey_membership import CrossCampaignReadOnly, InstantlyMembershipReadOnly
from thunder_os.new_journey_refusal import RegisteredRefusalRead
from thunder_os.service import ThunderService


EMAIL = 'ana@example.invalid'
PROFILE = 'https://www.linkedin.com/in/synthetic-person'
NOW = datetime(2026, 10, 23, 15, tzinfo=timezone.utc)
ICP = ('- Empresas B2B brasileiras, especialmente industria e distribuicao.\n'
       '- Faturamento acima de R$ 10 milhoes/ano; potencial de consultoria a partir de R$ 15 mil.\n'
       '- Qualificacao no HubSpot (tipo_do_lead): MQL, LEAD, SPAM, CONCORRENTE, NAO E ICP.\n'
       '- Sinal nao e intencao: vaga, post ou noticia pode ter outra explicacao. Registrar a hipotese alternativa.')


def assemble(owner, repo, service, root, expected_version):
    owner.http = FakeHTTP()
    owner.http.contacts['123']['properties'].update(email=EMAIL, hs_additional_emails='')
    raw = '\n'.join('## ' + key + '\n' + (ICP if key == 'icp' else 'Synthetic ' + label) + '\n'
                    for key, label in FIELDS.items()).encode()
    (root / 'campaign.md').write_bytes(raw)
    sheet = {'version': '1', 'fields': {key: {'path': 'campaign.md',
        'sha256': hashlib.sha256(raw).hexdigest(), 'section': key} for key in FIELDS}}
    cfg = {**config(), 'campaign_id': 'BR-APOLLO-FIXTURE', 'platform': 'apollo',
        'providers': {'email': 'apollo'}, 'briefing': sheet,
        'configuration': {'flow_version': '1', 'provider_accounts': {'email': 'sender@example.invalid'}}}
    service.campaign_registry.register(cfg, 'ygor', expected_version)
    owner.ledger_file = root.parent / 'registered-touch-fixture.db'
    with closing(sqlite3.connect(owner.ledger_file)) as con, con:
        con.executescript('''
            CREATE TABLE thunder_suppressions (canal TEXT,pessoa_chave TEXT,reason TEXT);
            CREATE TABLE respostas (id INTEGER,canal TEXT,pessoa_chave TEXT);
            CREATE TABLE toques (id INTEGER,canal TEXT,acao TEXT,pessoa_chave TEXT,
                                 alvo TEXT,status TEXT,provider_id TEXT);
        ''')

    @contextmanager
    def database():
        with closing(sqlite3.connect(owner.ledger_file.as_uri() + '?mode=ro', uri=True)) as con:
            yield con, '?'

    owner.apollo_occupied, owner.instantly_occupied, owner.provider_partial = False, False, False
    owner.provider_calls = []

    def apollo_request(method, path, body, headers, timeout):
        owner.provider_calls.append((method, path, deepcopy(body)))
        if method == 'GET':
            value = {'id': 'user-fixture', 'team_id': 'team-fixture'}
        else:
            rows = [] if not owner.apollo_occupied else [{'id': 'contact-fixture', 'email': body['q_keywords'],
                'team_id': 'team-fixture', 'contact_campaign_statuses': [
                    {'emailer_campaign_id': 'another-sequence', 'status': 'active'}]}]
            value = {'contacts': rows, 'pagination': {'page': 1, 'per_page': 100,
                'total_entries': len(rows), 'total_pages': 1 if rows else 0}}
        return 200, json.dumps(value).encode()

    def instantly_request(method, path, body, headers, timeout):
        owner.provider_calls.append((method, path, deepcopy(body)))
        if method == 'GET':
            value = {'id': INSTANTLY_WORKSPACE}
        else:
            value = {'items': [] if not owner.instantly_occupied else [{
                'id': '00000000-0000-4000-8000-000000000001', 'email': body['contacts'][0],
                'organization': INSTANTLY_WORKSPACE,
                'campaign': '00000000-0000-4000-8000-000000000002', 'status': 1}]}
            if owner.provider_partial:
                value['next_starting_after'] = '00000000-0000-4000-8000-000000000001'
        return 200, json.dumps(value).encode()

    apollo = ApolloProspectReadonly(lambda: 'synthetic-key', request=apollo_request, enabled=True)
    membership = CrossCampaignReadOnly(apollo, InstantlyMembershipReadOnly(
        lambda: 'synthetic-key', request=instantly_request), apollo_account_id='team-fixture')
    owner.checker = NewJourneyChecks(service, HubSpotHTTP(owner.http.loader, request=owner.http),
        clock=lambda: NOW, membership=membership, refusals=RegisteredRefusalRead(database))
    return owner.checker


class CanonicalEligibilityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        brain = self.root / 'brain'
        brain.mkdir()
        self.repo = Repository(self.root / 'state.db')
        self.service = ThunderService(self.repo, knowledge_store=KnowledgeStore(brain))
        assemble(self, self.repo, self.service, brain, 0)
        for target in ('socket.socket.connect', 'socket.getaddrinfo'):
            p = patch(target, side_effect=AssertionError('external network forbidden'))
            self.addCleanup(p.stop)
            self.addCleanup(p.start().assert_not_called)

    def check(self, **extra):
        row = {'source_key': 'csv:1', 'email': EMAIL, 'company': 'Untrusted CSV name',
               'linkedin_url': PROFILE, **extra}
        result = self.checker([row], 'BR-APOLLO-FIXTURE')[0]
        return {entry['key']: entry for entry in result['checks']}

    def test_real_checker_proves_all_six_from_canonical_reads_not_csv_claims(self):
        checks = self.check(icp=False, verified=False, client=True)
        self.assertEqual(list(checks), list(KEYS))
        self.assertEqual({key: value['status'] for key, value in checks.items()}, {
            'icp': 'pass', 'exclusions': 'pass', 'client': 'pass',
            'open_deal': 'pass', 'refusal': 'pass', 'membership': 'pass'})
        self.assertEqual(checks['membership']['coverage']['scope'],
                         'hubspot_apollo_instantly_all_verified_email_aliases')
        self.assertIn('not_global_inbox', checks['refusal']['coverage']['scope'])
        self.assertTrue(all(value['source'] for value in checks.values()))
        self.assertTrue(all(method == 'GET' or path.endswith(('/search', '/list'))
                            for method, path, _ in self.provider_calls))
        self.assertEqual(self.repo.effects(), [])
        self.assertEqual(self.repo.outbox(), [])

    def test_missing_revenue_or_schema_or_stale_qualification_cannot_pass_icp(self):
        props = self.http.companies['500']['properties']
        original = deepcopy(props)
        for key, value in (('thunder_annual_revenue_estimate', None),
                           ('thunder_annual_revenue_estimate', 'NaN'),
                           ('thunder_annual_revenue_estimate', '10000000'),
                           ('thunder_last_enriched_at', '2025-01-01'),
                           ('thunder_icp_status', 'pending_review'), ('hs_country_code', 'MX')):
            with self.subTest(key=key, value=value):
                props.clear(); props.update(original); props[key] = value
                self.assertNotEqual(self.check()['icp']['status'], 'pass')
        props.clear(); props.update(original)
        prop = next(p for p in self.http.schemas['companies']['results'] if p['name'] == 'thunder_annual_revenue_estimate')
        prop['description'] = 'Revenue, unspecified currency'
        self.assertNotEqual(self.check()['icp']['status'], 'pass')

    def test_fourth_company_deal_and_partial_association_cannot_be_cleared(self):
        self.http.deals['4']['properties']['hs_is_closed'] = 'false'
        self.assertEqual(self.check()['open_deal']['status'], 'blocked')
        self.http.deals['4']['properties']['hs_is_closed'] = 'true'
        self.http.companies['500']['associations']['deals']['paging'] = {'next': {'after': 'more'}}
        self.assertEqual(self.check()['open_deal']['status'], 'unknown')

    def test_protected_real_company_is_not_hidden_by_csv_name(self):
        for name, key in (('Kawasaki', 'client'), ('Coca-Cola FEMSA', 'exclusions'), ('Bimbo', 'client')):
            self.http.companies['500']['properties']['name'] = name
            self.assertEqual(self.check(company='Innocent CSV name')[key]['status'], 'blocked')

    def test_registered_refusal_and_optout_block_after_previous_clear(self):
        first = self.check()['refusal']
        with closing(sqlite3.connect(self.ledger_file)) as con, con:
            con.execute('INSERT INTO thunder_suppressions VALUES (?,?,?)', ('linkedin', PROFILE, 'negative'))
        second = self.check()['refusal']
        self.assertEqual(second['status'], 'blocked')
        self.assertNotEqual(first['source'], second['source'])
        with closing(sqlite3.connect(self.ledger_file)) as con, con:
            con.execute('DELETE FROM thunder_suppressions')
        self.http.contacts['123']['properties']['opt_out'] = 'true'
        self.assertEqual(self.check()['refusal']['status'], 'blocked')

    def test_missing_registered_optout_or_linked_profile_is_unknown(self):
        props = self.http.contacts['123']['properties']
        props['opt_out'] = None
        self.assertEqual(self.check()['refusal']['status'], 'unknown')
        props['opt_out'] = 'false'
        props['hs_linkedin_url'] = props['linkedin_url'] = None
        self.assertEqual(self.check(linkedin_url='')['refusal']['status'], 'unknown')

    def test_missing_alias_property_or_schema_retains_refusal_and_membership(self):
        del self.http.contacts['123']['properties']['hs_additional_emails']
        for key in ('refusal', 'membership'):
            self.assertEqual(self.check()[key]['status'], 'unknown')
        self.http.contacts['123']['properties']['hs_additional_emails'] = ''
        self.http.schemas['contacts']['results'] = [p for p in self.http.schemas['contacts']['results']
                                                   if p['name'] != 'hs_additional_emails']
        self.assertEqual(self.check()['membership']['status'], 'unknown')

    def test_missing_company_name_or_invalid_schema_retains_protected_checks(self):
        del self.http.companies['500']['properties']['name']
        for key in ('client', 'exclusions'):
            self.assertEqual(self.check()[key]['status'], 'unknown')
        self.http.companies['500']['properties']['name'] = 'Synthetic B2B'
        next(p for p in self.http.schemas['companies']['results'] if p['name'] == 'name')['type'] = 'number'
        self.assertEqual(self.check()['exclusions']['status'], 'unknown')

    def test_membership_empty_fields_need_current_string_schema(self):
        for field in ('campanha_ativa', 'executor'):
            prop = next(p for p in self.http.schemas['contacts']['results'] if p['name'] == field)
            prop['type'] = 'number'
            self.assertEqual(self.check()['membership']['status'], 'unknown')
            prop['type'] = 'string'
        self.http.schemas['contacts']['results'] = [p for p in self.http.schemas['contacts']['results']
                                                   if p['name'] not in ('campanha_ativa', 'executor')]
        self.assertEqual(self.check()['membership']['status'], 'unknown')

    def test_apollo_instantly_or_alias_membership_blocks_and_partial_is_unknown(self):
        self.apollo_occupied = True
        self.assertEqual(self.check()['membership']['status'], 'blocked')
        self.apollo_occupied = False
        self.instantly_occupied = True
        self.assertEqual(self.check()['membership']['status'], 'blocked')
        self.instantly_occupied = False
        self.provider_partial = True
        self.assertEqual(self.check()['membership']['status'], 'unknown')

    def test_sources_stay_stable_until_qualification_changes(self):
        first, second = self.check(), self.check()
        self.assertEqual([c['source'] for c in first.values()], [c['source'] for c in second.values()])
        self.http.companies['500']['properties']['thunder_annual_revenue_estimate'] = '30000000'
        self.assertNotEqual(first['icp']['source'], self.check()['icp']['source'])

    def test_unsupported_extra_icp_clause_and_missing_customer_proof_are_retained(self):
        sheet = self.service.campaign_sheet_view('BR-APOLLO-FIXTURE')
        next(field for field in sheet['fields'] if field['key'] == 'icp')['content'] += '\nSomente empresas com 500 funcionarios.'
        with patch.object(self.checker.service, 'campaign_sheet_view', return_value=sheet):
            self.assertEqual(self.check()['icp']['status'], 'unknown')
        del self.http.companies['500']['properties']['thunder_is_customer']
        self.assertEqual(self.check()['client']['status'], 'unknown')

    def test_sheet_mtime_only_does_not_invalidate_canonical_proof(self):
        sheet = self.service.campaign_sheet_view('BR-APOLLO-FIXTURE')
        with patch.object(self.checker.service, 'campaign_sheet_view', return_value=sheet):
            first = self.check()['icp']['source']
            for field in sheet['fields']:
                field['source_updated_at'] = '2026-10-23T19:00:00Z'
            self.assertEqual(first, self.check()['icp']['source'])

    def test_two_contact_ids_claiming_same_linkedin_retain_earlier_pass_too(self):
        other = deepcopy(self.http.contacts['123'])
        other['id'], other['properties']['email'] = '124', 'other@example.invalid'
        self.http.contacts['124'] = other
        original = self.http

        def request(method, path, body, headers, timeout):
            if path.endswith('/contacts/search'):
                address = body['filterGroups'][0]['filters'][0]['value']
                original.search = {'total': 1, 'results': [{'id': '123' if address == EMAIL else '124'}]}
            return original(method, path, body, headers, timeout)

        self.checker.hubspot = HubSpotHTTP(self.http.loader, request=request)
        results = self.checker([
            {'source_key': 'csv:1', 'email': EMAIL, 'linkedin_url': PROFILE},
            {'source_key': 'csv:2', 'email': 'other@example.invalid', 'linkedin_url': PROFILE}], 'BR-APOLLO-FIXTURE')
        self.assertEqual(len(results), 2)
        self.assertTrue(all(row['retained'] for row in results))
        self.assertFalse(any(check['status'] == 'pass' for row in results for check in row['checks']))


class CanonicalEligibilityHTTPTests(http_fixture.NewJourneyBoundHTTPTests):
    def setUp(self):
        super().setUp()
        checker = assemble(self, self.repo, self.service, self.service.knowledge_store.root, 1)
        existing = self.server.thunder_new_journey
        self.server.thunder_new_journey = NewJourney(self.service, apollo=existing.apollo,
                                                    checker=checker, clock=lambda: NOW)

    def test_incomplete_alias_name_or_membership_schema_cannot_approve_or_export(self):
        contacts, companies, schemas = deepcopy(self.http.contacts), deepcopy(self.http.companies), deepcopy(self.http.schemas)
        for gap in ('aliases', 'company_name', 'membership_schema'):
            with self.subTest(gap=gap):
                self.http.contacts, self.http.companies, self.http.schemas = deepcopy(contacts), deepcopy(companies), deepcopy(schemas)
                if gap == 'aliases':
                    del self.http.contacts['123']['properties']['hs_additional_emails']
                elif gap == 'company_name':
                    del self.http.companies['500']['properties']['name']
                else:
                    self.http.schemas['contacts']['results'] = [p for p in self.http.schemas['contacts']['results']
                                                               if p['name'] not in ('campanha_ativa', 'executor')]
                status, draft = self.request('create', method='POST', nonce=True,
                    body={'campaign_id': 'BR-APOLLO-FIXTURE', 'name': 'Isolated missing-data draft'})
                self.assertEqual(status, 200)
                ident = draft['id']

                def post(action, **body):
                    return self.request(ident + '/' + action, method='POST', nonce=True,
                                        body={'expected_version': draft['version'], **body})

                status, draft = post('source', mode='csv', csv_text='name,company,email\nAna,Fixture,ana@example.invalid\n')
                self.assertEqual(status, 200)
                status, draft = post('check')
                self.assertEqual(status, 200)
                self.assertEqual(draft['counts']['eligible'], 0)
                status, draft = post('sequence', steps=[{'id': 'E1', 'channel': 'email',
                    'subject': 'Fixture', 'content': 'Exact fixture text', 'order': 1, 'interval_hours': 0}],
                    apollo_sequence_id='sequence-fixture', apollo_sequence_query='Fixture sequence')
                self.assertEqual(status, 200)
                self.assertFalse(draft['steps'][0]['can_approve'])
                self.assertEqual(post('approve-step', step_id='E1', review_hash=draft['review_hash'])[0], 409)
                self.assertEqual(post('export', review_hash=draft['review_hash'])[0], 409)
                self.assertEqual(self.repo.effects(), [])
                self.assertEqual(self.repo.outbox(), [])


if __name__ == '__main__':
    unittest.main()
