"""Bounded B import contract, real repository/executor, synthetic HTTP only.

Acceptance checklist owned by this slice (principal owns independent review):
* preview/missing email/strict provenance/one row: input tests below;
* exact actor, versions/hash/expiry, paused/write flags: authorization tests;
* complete alias dedupe, pinned IDs, no existing-property PATCH: identity tests;
* serial company/contact/association and exact-ID readback: execution tests;
* durable intent, no retry after timeout, read-only recovery: failure tests.
No test claims real tenant scopes, publication, delivery or ICP qualification.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

from test_campaign_sheet_support import make_sheet
from test_thunder_os_campaign_registry import config
from thunder_os.db import Repository
from thunder_os.hubspot_console import HubSpotConsole
from thunder_os.hubspot_executor import HubSpotHTTP
from thunder_os.integration_hubspot import PORTAL_ID
from thunder_os.new_journey import NewJourney, NewJourneyError
from thunder_os.new_journey_import import NewJourneyImport, JourneyImportHTTP
from thunder_os.service import ThunderService


class SyntheticHTTP:
    def __init__(self, repo):
        self.repo = repo
        self.rows = {'contacts': {}, 'companies': {}}
        self.links = {}
        self.calls, self.writes = [], []
        self.portal_id, self.next_id = PORTAL_ID, 100
        self.timeout_kind, self.after_write = None, None
        self.hide_link, self.wrong_read_id = False, False
        self.incomplete, self.alias_schema, self.labels = False, True, True

    def add(self, kind, ident, **properties):
        alias = 'hs_additional_emails' if kind == 'contacts' else 'hs_additional_domains'
        self.rows[kind][ident] = {'id': ident, 'archived': False,
                                 'properties': {alias: '', **properties}}
        return self.rows[kind][ident]

    def project(self, row, properties):
        return {**deepcopy(row), 'properties': {key: row['properties'].get(key) for key in properties}}

    def request(self, method, path, body, headers, timeout):
        self.calls.append((method, path, deepcopy(body)))
        if not 0 < timeout <= 4:
            raise AssertionError('unbounded request')
        parsed = urlsplit(path)
        parts = parsed.path.strip('/').split('/')
        query = parse_qs(parsed.query)
        fields = query.get('properties', [''])[0].split(',')
        if path == '/account-info/v3/details':
            return self.response({'portalId': self.portal_id})
        if path == '/crm/v4/associations/contacts/companies/labels':
            return self.response({'results': [{'typeId': 279, 'category': 'HUBSPOT_DEFINED', 'label': None}]
                                  if self.labels else []})
        if parts[:4] == ['crm', 'v4', 'objects', 'contacts'] and method == 'GET':
            ids = [] if self.hide_link else self.links.get(parts[4], [])
            return self.response({'results': [{'toObjectId': int(i), 'associationTypes': [
                {'category': 'HUBSPOT_DEFINED', 'typeId': 279, 'label': None}]} for i in ids]})
        if parts[:3] == ['crm', 'v3', 'properties']:
            kind = parts[3]
            props = ({'email', 'firstname', 'lastname', 'jobtitle', 'hs_additional_emails'} if kind == 'contacts'
                     else {'name', 'domain', 'website', 'hs_additional_domains'})
            if not self.alias_schema:
                props = {p for p in props if not p.startswith('hs_additional')}
            return self.response({'results': [{'name': p, 'type': 'string', 'archived': False,
                'modificationMetadata': {'readOnlyValue': p.startswith('hs_additional')},
                'hasUniqueValue': p == 'email'} for p in sorted(props)]})
        if parts[:3] != ['crm', 'v3', 'objects']:
            raise AssertionError('unexpected route')
        kind = parts[3]
        if method == 'POST' and parts[-1] == 'search':
            rows = list(self.rows[kind].values())
            if 'query' in body:
                rows = [r for r in rows if any(body['query'].lower() in str(v).lower()
                                               for v in r['properties'].values())]
            else:
                def fits(row, group):
                    for f in group['filters']:
                        value = row['properties'].get(f['propertyName']) or ''
                        target = f['value']
                        if f['operator'] == 'EQ' and value.lower() != target.lower():
                            return False
                        if f['operator'] == 'CONTAINS_TOKEN' and target.lower() not in value.lower().split(';'):
                            return False
                    return True
                rows = [r for r in rows if any(fits(r, g) for g in body['filterGroups'])]
            return self.response({'results': [self.project(r, body['properties']) for r in rows],
                                  'total': len(rows) + (1 if self.incomplete else 0)})
        if method == 'POST' and len(parts) == 4:
            # Intent must have committed BEFORE any externally affecting call.
            assert getattr(self.repo._local, 'connection', None) is not None  # write fence only
            # Independent connection must see committed intent, not the fence's transaction.
            with closing(sqlite3.connect(self.repo.path)) as connection:
                attempts = connection.execute("SELECT payload FROM thunder_state WHERE kind='hubspot_attempt'").fetchall()
                assert any(json.loads(r[0]).get('status') == 'attempted'
                    and json.loads(r[0])['plan']['properties'] == body['properties'] for r in attempts)
            ident = str(self.next_id)
            self.next_id += 1
            row = self.add(kind, ident, **body['properties'])
            self.writes.append((method, path, deepcopy(body)))
            if self.after_write:
                self.after_write(kind)
            if self.timeout_kind == kind:
                raise TimeoutError('sensitive provider response must not escape')
            return self.response(row)
        if method == 'PUT':
            assert parts[3] == 'contacts' and parts[5:7] == ['associations', 'companies'] and parts[-1] == '279'
            # Dispatch exclusion is separate from the already committed intent.
            assert getattr(self.repo._local, 'connection', None) is not None
            with closing(sqlite3.connect(self.repo.path)) as connection:
                attempts = connection.execute("SELECT payload FROM thunder_state WHERE kind='hubspot_attempt'").fetchall()
                assert any(json.loads(r[0]).get('status') == 'attempted'
                    and json.loads(r[0])['plan'].get('operation') == 'associate'
                    and json.loads(r[0])['plan']['contact_id'] == parts[4]
                    and json.loads(r[0])['plan']['company_id'] == parts[7] for r in attempts)
            self.writes.append((method, path, deepcopy(body)))
            self.links[parts[4]] = [parts[7]]
            if self.timeout_kind == 'association':
                raise TimeoutError('sensitive association response must not escape')
            return self.response({'id': parts[4]})
        if method == 'GET':
            identifier = unquote(parts[4])
            if query.get('idProperty') == ['email']:
                row = next((r for r in self.rows[kind].values() if identifier in
                    [r['properties'].get('email'), *str(r['properties'].get('hs_additional_emails') or '').split(';')]), None)
            else:
                row = self.rows[kind].get(identifier)
            if row is None:
                return 404, b'{}'
            projected = self.project(row, fields)
            if 'associations' in query:
                ids = [] if self.hide_link else self.links.get(identifier, [])
                projected['associations'] = {'companies': {'results': [{'id': i, 'type': 'contact_to_company'} for i in ids]}}
            if self.wrong_read_id and identifier.isdigit():
                projected['id'] = '999'
            return self.response(projected)
        raise AssertionError('unexpected HTTP mutation')

    @staticmethod
    def response(value):
        return 200, json.dumps(value).encode()


class NewJourneyImportTests(unittest.TestCase):
    _pace_original = staticmethod(JourneyImportHTTP._pace_search)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = Repository(self.root / 'state.db')
        store, sheet = make_sheet(self.root / 'brain')
        self.service = ThunderService(self.repo, knowledge_store=store)
        cfg = {**config(), 'campaign_id': 'BR-APOLLO-02', 'platform': 'apollo',
            'providers': {'email': 'apollo'}, 'briefing': sheet,
            'configuration': {'flow_version': '1', 'provider_accounts': {'email': 'sender@example.invalid'}}}
        self.service.campaign_registry.register(cfg, 'ygor', 0)
        self.journey = NewJourney(self.service, clock=lambda: datetime(2026, 10, 24, tzinfo=timezone.utc))
        self.row = self.journey.create({'campaign_id': cfg['campaign_id'], 'name': 'Synthetic import'}, 'ygor')
        self.row = self.journey.source(self.row['id'], {'mode': 'csv', 'csv_text':
            'name,first_name,last_name,company,email\nAna Example,Ana,Example,Example,ana@example.invalid\n'},
            'ygor', self.row['version'])
        self.http = SyntheticHTTP(self.repo)
        self.now = 1000.0
        self.console = HubSpotConsole(self.repo,
            HubSpotHTTP(lambda: 'synthetic-only', request=self.http.request, creation_mode='business_ledger'),
            read_enabled=True, write_enabled=True, clock=lambda: self.now)
        self.imports = NewJourneyImport(self.journey, self.console)
        pacing = patch.object(JourneyImportHTTP, '_pace_search')
        pacing.start()
        self.addCleanup(pacing.stop)
        self.data = {'source_key': 'csv:1',
            'contact': {'email': 'ana@example.invalid', 'firstname': 'Ana', 'lastname': 'Example', 'jobtitle': 'CMO'},
            'company': {'name': 'Example', 'domain': 'example.invalid', 'website': 'http://www.example.invalid'},
            'source_refs': {}}
        self.refs()
        network = patch('socket.socket.connect', side_effect=AssertionError('network forbidden'))
        network.start()
        self.addCleanup(network.stop)

    def refs(self):
        self.data['source_refs'] = {label + '.' + key: {'ref': 'synthetic:source:verified-1', 'verified': True}
                                  for label in ('contact', 'company') for key, value in self.data[label].items() if value}

    def preview(self):
        return self.imports.preview(self.row['id'], deepcopy(self.data), 'ygor',
                                    self.row['version'], self.row['review_hash'])

    @staticmethod
    def body(preview, confirm=False):
        body = {'preview_id': preview['id'], 'expected_version': preview['version'],
                'preview_hash': preview['preview_hash']}
        return {**body, 'confirm': True} if confirm else body

    def execute(self, preview=None):
        return self.imports.execute(self.body(preview or self.preview(), True), 'ygor')

    def existing(self, alias=False):
        self.http.add('companies', '11', name='Established name', domain='other.invalid' if alias else 'example.invalid',
                      hs_additional_domains='example.invalid' if alias else '', website='https://old.invalid',
                      lifecyclestage='customer', hubspot_owner_id='777')
        self.http.add('contacts', '22', email='primary@example.invalid' if alias else 'ana@example.invalid',
                      hs_additional_emails='ana@example.invalid' if alias else '', firstname='Existing', lastname='Name',
                      jobtitle='Old title', lifecyclestage='customer', hubspot_owner_id='888', campanha_ativa='unchanged')

    def test_preview_zero_writes_persists_existing_namespace_and_leaves_journey_exact(self):
        before = self.repo.get_state('journey_draft', self.row['id'])
        p = self.preview()
        self.assertEqual(p['state'], 'review_required')
        self.assertTrue(p['can_execute'])
        self.assertEqual(p['contact']['action'], 'propose_create')
        self.assertEqual(p['company']['action'], 'propose_create')
        self.assertEqual(p['association']['status'], 'planned')
        self.assertEqual(self.http.writes, [])
        self.assertEqual(p['external_writes'], 0)
        self.assertEqual(self.repo.list_state('hubspot_attempt'), [])
        self.assertEqual(self.repo.get_state('journey_draft', self.row['id']), before)
        self.assertEqual(self.preview()['id'], p['id'])

    def test_missing_email_retained_without_any_provider_lookup(self):
        self.data['contact'].pop('email')
        self.refs()
        p = self.preview()
        self.assertIn('contact_email_required', p['blockers'])
        self.assertEqual(p['state'], 'blocked')
        self.assertFalse(p['can_execute'])
        self.assertEqual(self.http.calls, [])
        with self.assertRaisesRegex(NewJourneyError, 'import_preview_not_current'):
            self.execute(p)
        self.assertEqual(self.http.writes, [])

    def test_missing_name_or_company_identity_blocks_without_guessing(self):
        self.data['contact'] = {'email': 'ana@example.invalid'}
        self.data['company'] = {}
        self.refs()
        p = self.preview()
        self.assertTrue({'contact_name_required', 'company_name_required', 'company_domain_required'} <= set(p['blockers']))
        self.assertEqual(self.http.calls, [])
        self.assertEqual(p['contact']['properties'], self.data['contact'])

    def test_unverified_source_blocks_and_hash_binds_each_field(self):
        self.data['source_refs']['contact.email']['verified'] = False
        p = self.preview()
        self.assertIn('source_unverified:contact.email', p['blockers'])
        self.assertEqual(self.http.calls, [])

    def test_wrong_email_not_accepted_even_with_verified_source(self):
        self.data['contact']['email'] = 'other@example.invalid'
        with self.assertRaisesRegex(NewJourneyError, 'import_email_not_in_journey_row'):
            self.preview()
        self.assertEqual(self.http.calls, [])

    def test_extra_properties_rejected_without_owner_stage_or_fake_icp(self):
        for label, key in (('contact', 'lifecyclestage'), ('contact', 'phone'), ('company', 'description'),
                           ('company', 'thunder_icp_status'), ('contact', 'hubspot_owner_id')):
            with self.subTest(key=key):
                original = deepcopy(self.data)
                self.data[label][key] = 'not-authorized'
                with self.assertRaisesRegex(NewJourneyError, 'import_properties_invalid'):
                    self.preview()
                self.data = original
        self.assertEqual(self.http.calls, [])

    def test_website_domain_must_match(self):
        self.data['company']['website'] = 'https://another.invalid'
        with self.assertRaisesRegex(NewJourneyError, 'import_website_identity_mismatch'):
            self.preview()

    def test_explicit_single_row_and_actor_required(self):
        self.data['source_key'] = ['csv:1', 'csv:2']
        with self.assertRaisesRegex(NewJourneyError, 'import_single_row_required'):
            self.preview()
        self.data['source_key'] = 'csv:1'
        with self.assertRaisesRegex(NewJourneyError, 'journey_human_authority_required'):
            self.imports.preview(self.row['id'], self.data, 'hermes', self.row['version'], self.row['review_hash'])

    def test_disabled_write_and_pause_preserved(self):
        p = self.preview()
        self.console.write_enabled = False
        self.assertFalse(self.preview()['can_execute'])
        with self.assertRaisesRegex(NewJourneyError, 'hubspot_writes_disabled_or_paused'):
            self.execute(p)
        self.console.write_enabled = True
        self.service.set_global_pause(True, 'ygor')
        with self.assertRaisesRegex(NewJourneyError, 'hubspot_writes_disabled_or_paused'):
            self.execute(p)
        self.assertEqual(self.http.writes, [])

    def test_dispatch_gate_still_applies(self):
        self.console.dispatch_authorized = lambda: False
        p = self.preview()
        self.assertFalse(p['can_execute'])
        with self.assertRaisesRegex(NewJourneyError, 'hubspot_writes_disabled_or_paused'):
            self.execute(p)

    def test_review_version_hash_confirmation_and_expiry_exact(self):
        p = self.preview()
        for key, value in (('expected_version', p['version'] + 1), ('preview_hash', '0' * 64), ('confirm', False)):
            with self.subTest(key=key), self.assertRaises(NewJourneyError):
                self.imports.execute({**self.body(p, True), key: value}, 'ygor')
        self.now += 301
        with self.assertRaisesRegex(NewJourneyError, 'import_preview_not_current'):
            self.execute(p)
        self.assertEqual(self.http.writes, [])

    def test_journey_version_changed_blocks(self):
        p = self.preview()
        self.repo.put_state('journey_draft', self.row['id'], self.repo.get_state('journey_draft', self.row['id']))
        with self.assertRaisesRegex(NewJourneyError, 'journey_version_conflict'):
            self.execute(p)
        self.assertEqual(self.http.writes, [])

    def test_preview_payload_tampering_detected(self):
        p = self.preview()
        stored = self.repo.get_state('hubspot_preview', p['id'])
        stored['contact']['properties']['email'] = 'other@example.invalid'
        changed = self.repo.put_state('hubspot_preview', p['id'], stored)
        with self.assertRaisesRegex(NewJourneyError, 'import_preview_integrity_mismatch'):
            self.execute(changed)
        self.assertEqual(self.http.writes, [])

    def test_existing_records_and_aliases_reused_without_property_updates(self):
        self.existing(alias=True)
        before = deepcopy(self.http.rows)
        p = self.preview()
        self.assertEqual((p['contact']['existing_id'], p['company']['existing_id']), ('22', '11'))
        result = self.execute(p)
        self.assertEqual(result['result']['status'], 'verified_state')
        self.assertEqual(self.http.rows, before)
        self.assertEqual([w[0] for w in self.http.writes], ['PUT'])
        self.assertFalse(any(c[0] == 'PATCH' for c in self.http.calls))

    def test_existing_correct_association_requires_no_external_write(self):
        self.existing()
        self.http.links['22'] = ['11']
        p = self.preview()
        self.assertEqual(p['association']['action'], 'reuse_existing')
        done = self.execute(p)
        self.assertEqual(done['result']['status'], 'verified_state')
        self.assertEqual(self.http.writes, [])
        self.assertEqual(done['external_writes'], 0)

    def test_name_only_collision_and_duplicate_aliases_block(self):
        self.http.add('contacts', '31', email='other@example.invalid', firstname='Ana', lastname='Example')
        p = self.preview()
        self.assertIn('import_name_identity_conflict', p['blockers'])
        self.assertFalse(p['can_execute'])
        self.http.add('contacts', '32', email='ana@example.invalid', firstname='Ana', lastname='Example')
        self.data['source_refs']['contact.email']['ref'] = 'synthetic:revision-2'
        p = self.preview()
        self.assertIn('import_identity_conflict', p['blockers'])
        self.assertEqual(self.http.writes, [])

    def test_existing_other_company_association_blocks_before_any_create(self):
        self.http.add('contacts', '22', email='ana@example.invalid', firstname='Ana', lastname='Example')
        self.http.links['22'] = ['555']
        p = self.preview()
        self.assertIn('import_company_association_conflict', p['blockers'])
        self.assertEqual(self.http.writes, [])

    def test_incomplete_search_alias_schema_and_wrong_portal_fail_closed(self):
        for setting, value in (('incomplete', True), ('alias_schema', False), ('portal_id', '999'), ('labels', False)):
            with self.subTest(setting=setting):
                original = getattr(self.http, setting)
                setattr(self.http, setting, value)
                self.data['source_refs']['contact.email']['ref'] = 'synthetic:' + setting
                p = self.preview()
                self.assertFalse(p['can_execute'])
                self.assertTrue(p['blockers'])
                setattr(self.http, setting, original)
        self.assertEqual(self.http.writes, [])

    def test_three_serial_explicit_effects_success_only_after_association_readback(self):
        p = self.preview()
        done = self.execute(p)
        self.assertEqual(done['result']['status'], 'verified_state')
        self.assertEqual([w[1] for w in self.http.writes], ['/crm/v3/objects/companies',
            '/crm/v3/objects/contacts', '/crm/v3/objects/contacts/101/associations/companies/100/279'])
        self.assertEqual(done['external_writes'], 3)
        self.assertEqual((done['result']['contact_id'], done['result']['company_id']), ('101', '100'))
        self.assertEqual(self.http.links, {'101': ['100']})
        self.assertEqual(len(self.repo.list_state('hubspot_attempt')), 3)
        self.assertIsNone(self.repo.get_state('agent_control', 'global'))
        self.assertEqual(self.repo.get_state('journey_draft', self.row['id'])['qualifications'], {})

    def test_association_http_acceptance_is_not_success_without_readback(self):
        self.http.hide_link = True
        done = self.execute()
        self.assertEqual(done['result']['status'], 'pending_reconciliation')
        self.assertEqual(done['external_writes'], 3)
        self.assertEqual(done['result']['operations']['association']['status'], 'pending_reconciliation')
        self.http.hide_link = False
        count = len(self.http.writes)
        readback = self.imports.reconcile(self.body(done), 'ygor')
        self.assertEqual(readback['result']['status'], 'verified_state')
        self.assertEqual(len(self.http.writes), count)

    def test_lost_company_response_stops_contact_and_never_retries(self):
        self.http.timeout_kind = 'companies'
        done = self.execute()
        self.assertEqual(done['result']['status'], 'pending_reconciliation')
        self.assertEqual(len(self.http.writes), 1)
        self.assertEqual(len(self.http.rows['contacts']), 0)
        self.assertEqual(done['external_writes'], 1)
        reconciled = self.imports.reconcile(self.body(done), 'ygor')
        self.assertEqual(reconciled['result']['status'], 'pending_reconciliation')
        self.assertTrue(reconciled['result']['operations']['company']['identity_lock_retained'])
        self.assertEqual(len(self.http.writes), 1)
        with self.assertRaisesRegex(NewJourneyError, 'import_preview_not_current'):
            self.execute(reconciled)

    def test_lost_contact_response_does_not_associate(self):
        self.http.timeout_kind = 'contacts'
        done = self.execute()
        self.assertEqual(done['result']['status'], 'pending_reconciliation')
        self.assertEqual(len(self.http.writes), 2)
        self.assertEqual(self.http.links, {})
        self.assertNotIn('sensitive', json.dumps(done))

    def test_lost_association_response_reconciles_readonly_even_paused_and_expired(self):
        self.http.timeout_kind = 'association'
        done = self.execute()
        self.assertEqual(done['result']['status'], 'pending_reconciliation')
        self.service.set_global_pause(True, 'ygor')
        self.console.write_enabled = False
        self.now += 301
        readback = self.imports.reconcile(self.body(done), 'ygor')
        self.assertEqual(readback['result']['status'], 'verified_state')
        self.assertFalse(readback['result']['write_attribution_confirmed'])
        self.assertEqual(len(self.http.writes), 3)

    def test_pause_after_first_effect_prevents_later_writes(self):
        self.http.after_write = lambda kind: self.service.set_global_pause(True, 'ygor')
        done = self.execute()
        self.assertEqual(done['result']['status'], 'pending_reconciliation')
        self.assertEqual(len(self.http.writes), 1)
        self.assertEqual(self.http.rows['contacts'], {})

    def test_new_remote_identity_after_preview_prevents_create(self):
        p = self.preview()
        self.http.add('contacts', '91', email='ana@example.invalid', firstname='Ana', lastname='Example')
        done = self.execute(p)
        self.assertEqual(done['result']['error_code'], 'import_target_changed')
        self.assertEqual(self.http.writes, [])

    def test_wrong_exact_id_readback_does_not_authorize_association(self):
        self.existing()
        p = self.preview()
        self.http.wrong_read_id = True
        done = self.execute(p)
        self.assertNotEqual(done['result']['status'], 'verified_state')
        self.assertEqual(self.http.writes, [])

    def test_concurrent_same_preview_has_only_one_owner(self):
        p = self.preview()
        def run():
            try:
                return self.execute(p)['result']['status']
            except NewJourneyError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: run(), range(2)))
        self.assertEqual(results.count('verified_state'), 1)
        self.assertEqual(len(self.http.writes), 3)

    def test_expiry_during_final_dedupe_blocks_before_any_create(self):
        p = self.preview()
        original = self.http.request
        def delayed(method, path, body, headers, timeout):
            result = original(method, path, body, headers, timeout)
            if path.endswith('/search') and getattr(self.repo._local, 'connection', None) is not None:
                self.now = p['expires_at'] + 1
            return result
        self.console.port._request = delayed
        self.execute(p)
        self.assertEqual(self.http.writes, [])

    def test_pause_committed_before_association_dispatch_blocks_put(self):
        self.existing()
        p = self.preview()
        other = Repository(self.repo.path)
        original = self.repo.transaction
        paused = []
        @contextmanager
        def before_dispatch():
            if not getattr(self.repo._local, 'connection', None) and not paused:
                attempts = self.repo.list_state('hubspot_attempt')
                if any(a.get('plan', {}).get('operation') == 'associate' for a in attempts):
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        pool.submit(other.put_state, 'control', 'global', {'paused': True}).result(timeout=2)
                    paused.append(True)
            with original() as con:
                yield con
        with patch.object(self.repo, 'transaction', before_dispatch):
            self.execute(p)
        self.assertTrue(paused)
        self.assertEqual(self.http.writes, [])
        self.assertTrue(other.get_state('control', 'global')['paused'])

    def test_association_readback_requires_exact_hubspot_defined_type(self):
        self.existing()
        self.http.links['22'] = ['11']
        original = self.http.request
        for types in (None, [], [{'category': 'USER_DEFINED', 'typeId': 999}],
                      [{'category': 'HUBSPOT_DEFINED', 'typeId': True}]):
            def invalid(method, path, body, headers, timeout):
                if path.startswith('/crm/v4/objects/contacts/'):
                    return self.http.response({'results': [{'toObjectId': 11, 'associationTypes': types}]})
                return original(method, path, body, headers, timeout)
            with self.subTest(types=types):
                self.console.port._request = invalid
                port = JourneyImportHTTP(self.console.port)
                with self.assertRaisesRegex(NewJourneyError, 'import_association_type_unverified'):
                    port.association_ids('22')
        self.console.port._request = original
        self.assertEqual(JourneyImportHTTP(self.console.port).association_ids('22'), ['11'])
        self.assertEqual(self.http.writes, [])

    def test_extension_rejects_unrelated_association_endpoint(self):
        port = JourneyImportHTTP(self.console.port)
        with self.assertRaisesRegex(NewJourneyError, 'import_endpoint_blocked'):
            port._association_call('DELETE', '/crm/v3/objects/contacts/22')
        self.assertEqual(self.http.calls, [])

    def test_pacing_serializes_search_requests_below_provider_limit(self):
        # Actual implementation, saved before the test-only pacing replacement.
        method = self._pace_original
        port = JourneyImportHTTP(self.console.port)
        old = JourneyImportHTTP._last_search_at
        self.addCleanup(setattr, JourneyImportHTTP, '_last_search_at', old)
        JourneyImportHTTP._last_search_at = 10.0
        with patch('thunder_os.new_journey_import.time.monotonic', side_effect=[10.1, 10.26]), \
                patch('thunder_os.new_journey_import.time.sleep') as sleep:
            method(port)
        self.assertAlmostEqual(sleep.call_args.args[0], 0.16)
        self.assertEqual(JourneyImportHTTP._last_search_at, 10.26)

    def test_expired_review_preview_refreshes_metadata_version_and_expiry(self):
        original = self.preview()
        old_calls = len(self.http.calls)
        self.now += 301
        fresh = self.preview()
        self.assertEqual(fresh['id'], original['id'])
        self.assertEqual(fresh['version'], original['version'] + 1)
        self.assertGreater(fresh['expires_at'], original['expires_at'])
        self.assertNotEqual(fresh['preview_hash'], original['preview_hash'])
        self.assertGreater(len(self.http.calls), old_calls)
        self.assertTrue(any('/properties/' in call[1] for call in self.http.calls[old_calls:]))
        self.assertTrue(fresh['can_execute'])
        self.assertEqual(self.http.writes, [])
        with self.assertRaisesRegex(NewJourneyError, 'import_preview_conflict'):
            self.execute(original)

    def test_expired_blocked_preview_can_observe_recovered_provider(self):
        self.http.labels = False
        original = self.preview()
        self.assertEqual(original['state'], 'blocked')
        self.now += 301
        self.http.labels = True
        fresh = self.preview()
        self.assertEqual(fresh['version'], original['version'] + 1)
        self.assertEqual(fresh['state'], 'review_required')
        self.assertTrue(fresh['can_execute'])

    def test_expired_executing_or_processed_preview_never_resets(self):
        original = self.preview()
        for state in ('executing', 'processed'):
            with self.subTest(state=state):
                stored = self.repo.get_state('hubspot_preview', original['id'])
                current = self.repo.put_state('hubspot_preview', original['id'],
                    {**stored, 'state': state, 'result': {'status': 'pending_reconciliation'}})
                self.now += 301
                old_calls = len(self.http.calls)
                fresh = self.preview()
                self.assertEqual(fresh['version'], current['version'])
                self.assertEqual(fresh['state'], state)
                self.assertEqual(fresh['result']['status'], 'pending_reconciliation')
                self.assertEqual(len(self.http.calls), old_calls)
                self.assertFalse(fresh['can_execute'])


if __name__ == '__main__':
    unittest.main()
