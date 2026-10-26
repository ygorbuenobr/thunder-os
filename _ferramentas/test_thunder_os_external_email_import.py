"""Isolated contract tests, synthetic contacts and CRM; no real credentials."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from thunder_os.db import Repository
from thunder_os.external_email_import import (APOLLO_TEAM, APOLLO_SEQUENCE, PORTAL_ID,
    ExternalEmailError, ExternalEmailImport, HubSpotExternalEmail, plan_contact, plan_email)


def contact(ident='synthetic-contact', primary='person@example.invalid'):
    return {'id': ident, 'email': primary, 'first_name': 'Synthetic', 'last_name': 'Contact',
            'contact_emails': [{'email': 'alias@example.invalid'}]}


def record(ident='100', primary='person@example.invalid', aliases='alias@example.invalid'):
    return {'id': ident, 'archived': False,
            'properties': {'email': primary, 'hs_additional_emails': aliases,
                           'firstname': 'Synthetic', 'lastname': 'Contact'}}


def snapshot(records=None):
    return {'portal_id': PORTAL_ID, 'complete': True, 'aliases_complete': True,
            'checked_emails': ['person@example.invalid', 'alias@example.invalid'],
            'records': records or []}


def event(ident='synthetic-email'):
    return {'source': {'provider': 'apollo', 'account_id': APOLLO_TEAM,
                      'external_campaign_id': APOLLO_SEQUENCE, 'campaign_id': 'BR-APOLLO-01',
                      'provider_id': ident}, 'event_type': 'email_sent',
            'lead_email': 'person@example.invalid', 'occurred_at': '2026-10-21T12:00:00Z',
            'subject': 'Synthetic subject', 'text': 'Synthetic body, never sent.'}


class FakeCRM:
    def __init__(self, repo):
        self.repo, self.writes, self.fault = repo, [], None
        self.contact = record()
        self.objects = {}
        self.dedupe = {'complete': True, 'records': [], 'unattributed_candidates': 0}

    def portal(self):
        return PORTAL_ID

    def contact_snapshot(self, identifiers):
        return snapshot([self.contact])

    def email_dedupe(self, plan):
        return deepcopy(self.dedupe)

    def read_object(self, kind, ident, properties):
        return deepcopy(self.contact if kind == 'contacts' else self.objects.get(ident))

    def write(self, plan):
        effect, _ = ExternalEmailImport._keys(plan)
        # A separate connection must see a COMMITTED attempt before remote I/O.
        observed = Repository(self.repo.path).get_state('hubspot_attempt', effect)
        assert observed['status'] == 'attempted'
        self.writes.append(deepcopy(plan))
        row = {'id': '500', 'archived': False, 'properties': deepcopy(plan['properties']),
               'associations': {'contacts': {'results': [{'id': plan.get('contact_id')}]}}}
        if self.fault != 'accepted_only':
            self.objects['500'] = row
        if self.fault == 'wrong_association':
            self.objects['500']['associations']['contacts']['results'] = [{'id': '101'}]
        if self.fault == 'timeout':
            raise TimeoutError('PRIVATE provider body/token must never appear')
        return {'id': '500'}


class ExternalEmailImportTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / '.scratch' / 'external-email'
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.repo = Repository(Path(self.temp.name) / 'isolated.db')
        self.crm = FakeCRM(self.repo)
        self.service = ExternalEmailImport(self.repo, self.crm, enabled=True, authorize=lambda plan: True)

    def test_defaults_off(self):
        repo, crm = Mock(), Mock()
        plan = plan_email(event(), '100')
        with self.assertRaisesRegex(ExternalEmailError, 'email_writers_disabled'):
            ExternalEmailImport(repo, crm).execute(plan)
        self.assertEqual(repo.mock_calls, [])
        self.assertEqual(crm.mock_calls, [])
        loader = Mock(side_effect=AssertionError('must not load'))
        with self.assertRaisesRegex(ExternalEmailError, 'email_writers_disabled'):
            HubSpotExternalEmail(loader).write(plan)
        loader.assert_not_called()

    def test_alias_dedupe_no_overwrite(self):
        original = contact()
        canonical = record(primary='other@example.invalid', aliases=' ALIAS@example.invalid ;')
        plan = plan_contact(original, snapshot([canonical]))
        self.assertEqual(plan['action'], 'existing')
        self.assertEqual(plan['record_id'], '100')
        self.assertEqual(plan['properties'], {})
        self.crm.contact = canonical
        result = self.service.execute(plan)
        self.assertEqual(result['status'], 'verified_state')
        self.assertEqual(self.crm.writes, [])
        self.assertEqual(original, contact())

    def test_incomplete_and_ambiguous_block(self):
        for changes in ({'complete': False}, {'aliases_complete': False}, {'checked_emails': []},
                        {'portal_id': '123'}):
            with self.subTest(changes=changes), self.assertRaises(ExternalEmailError):
                plan_contact(contact(), {**snapshot(), **changes})
        with self.assertRaisesRegex(ExternalEmailError, 'ambiguous_contact_identity'):
            plan_contact(contact(), snapshot([record(), record('101')]))
        missing = record()
        del missing['properties']['hs_additional_emails']
        with self.assertRaisesRegex(ExternalEmailError, 'canonical_aliases_missing'):
            plan_contact(contact(), snapshot([missing]))

    def test_contact_create_is_minimal_and_no_email_is_not_identity(self):
        plan = plan_contact(contact(), snapshot())
        self.assertEqual(plan['action'], 'create')
        self.assertEqual(set(plan['properties']), {'email', 'firstname', 'lastname'})
        self.assertEqual(plan_contact(contact(primary=None), None)['action'], 'no_email')

    def test_contact_creation_readback(self):
        plan = plan_contact(contact(), snapshot())
        self.crm.contact_snapshot = lambda identifiers: snapshot()
        self.crm.contact = record('500')
        result = self.service.execute(plan)
        self.assertEqual(result['status'], 'verified_state')
        self.assertFalse(result['email_logged'])
        self.assertEqual(len(self.crm.writes), 1)

    def test_factual_company_and_title_create_only_and_readback(self):
        source = {**contact(), 'company': 'Synthetic Manufacturing', 'title': 'Operations Director'}
        plan = plan_contact(source, snapshot())
        self.assertEqual(plan['properties'], {'email': 'person@example.invalid',
            'firstname': 'Synthetic', 'lastname': 'Contact',
            'company': 'Synthetic Manufacturing', 'jobtitle': 'Operations Director'})
        self.crm.contact_snapshot = lambda identifiers: snapshot()
        self.crm.contact = {**record('500'), 'properties': {**record('500')['properties'],
            'company': 'Synthetic Manufacturing', 'jobtitle': 'Operations Director'}}
        result = self.service.execute(plan)
        self.assertEqual(result['status'], 'verified_state')
        self.assertEqual(len(self.crm.writes), 1)
        self.assertNotIn('associations', plan)

    def test_company_and_title_never_patch_existing_contact(self):
        from thunder_os.external_email_import import _validate_plan
        from thunder_os.models import payload_hash
        source = {**contact(), 'company': 'Source Company', 'title': 'Source Title'}
        canonical = record()
        canonical['properties'].update(company='Preserve Company', jobtitle='Preserve Title')
        plan = plan_contact(source, snapshot([canonical]))
        self.assertEqual(plan['action'], 'existing')
        self.assertEqual(plan['properties'], {})
        before = deepcopy(canonical)
        self.crm.contact = canonical
        self.assertEqual(self.service.execute(plan)['status'], 'verified_state')
        self.assertEqual(self.crm.writes, [])
        self.assertEqual(self.crm.contact, before)
        for field in ('company', 'jobtitle'):
            changed = {**plan, 'properties': {field: 'Attempted overwrite'}}
            changed['plan_hash'] = payload_hash({k: v for k, v in changed.items() if k != 'plan_hash'})
            with self.subTest(field=field), self.assertRaisesRegex(ExternalEmailError, 'contact_overwrite_blocked'):
                _validate_plan(changed)

    def test_contact_company_and_title_validate_and_wire_readback_allowlist(self):
        from thunder_os.external_email_import import _validate_plan
        from thunder_os.models import payload_hash
        for key in ('company', 'title'):
            for invalid in ({'name': 'Not a factual string'}, 'x' * 201):
                with self.subTest(key=key, invalid=invalid), self.assertRaises(ExternalEmailError):
                    plan_contact({**contact(), key: invalid}, snapshot())
        for key in ('company', 'jobtitle'):
            plan = plan_contact(contact(), snapshot())
            plan['properties'][key] = {'invalid': 'value'}
            plan['plan_hash'] = payload_hash({k: v for k, v in plan.items() if k != 'plan_hash'})
            with self.subTest(key=key), self.assertRaisesRegex(ExternalEmailError, 'invalid_contact_display_fields'):
                _validate_plan(plan)
        calls = []
        def request(method, path, body, headers, timeout):
            calls.append((method, path))
            return 200, b'{"id":"100"}'
        port = HubSpotExternalEmail(lambda: 'synthetic-token', request=request)
        port.read_object('contacts', '100', ['email', 'company', 'jobtitle'])
        self.assertEqual(calls[0][0], 'GET')
        self.assertEqual(parse_qs(urlsplit(calls[0][1]).query)['properties'], ['company,email,jobtitle'])

    def test_provider_id_is_not_rfc_message_id(self):
        plan = plan_email(event('internal-apollo-id'), '100')
        self.assertNotIn('hs_email_message_id', plan['properties'])
        self.assertNotIn('hs_email_headers', plan['properties'])
        self.assertIn('internal-apollo-id', plan['properties']['hs_email_text'])
        self.assertEqual(plan['source']['provider_id'], 'internal-apollo-id')
        self.assertEqual(plan_email(event('internal-apollo-id'), '100')['plan_hash'], plan['plan_hash'])

    def test_email_readback_requires_association(self):
        self.crm.fault = 'wrong_association'
        plan = plan_email(event(), '100')
        self.assertEqual(self.service.execute(plan)['status'], 'pending_reconciliation')
        self.assertEqual(self.service.execute(plan)['status'], 'pending_reconciliation')
        self.assertEqual(len(self.crm.writes), 1)
        self.crm.objects['500']['associations']['contacts']['results'] = [{'id': '100'}]
        result = self.service.reconcile(plan)
        self.assertEqual(result['status'], 'verified_state')
        self.assertTrue(result['association_verified'])
        self.assertTrue(result['email_logged'])

    def test_attempt_persisted_timeout_never_retries(self):
        self.crm.fault = 'timeout'
        plan = plan_email(event(), '100')
        result = self.service.execute(plan)
        self.assertEqual(result['status'], 'pending_reconciliation')
        self.assertFalse(result['retry_write'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        restarted = ExternalEmailImport(Repository(self.repo.path), self.crm,
                                        enabled=True, authorize=lambda plan: True)
        for _ in range(3):
            self.assertEqual(restarted.execute(plan)['status'], 'pending_reconciliation')
        self.assertEqual(len(self.crm.writes), 1)
        self.assertEqual(restarted.reconcile(plan, recovered_id='500')['status'], 'verified_state')
        self.assertEqual(len(self.crm.writes), 1)

    def test_concurrent_duplicate(self):
        plan = plan_email(event(), '100')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.service.execute(plan), range(2)))
        self.assertEqual(len(self.crm.writes), 1)
        self.assertTrue(any(result['status'] == 'verified_state' for result in results))

    def test_changed_plan_same_provider_id_never_replays(self):
        plan = plan_email(event(), '100')
        self.service.execute(plan)
        changed = plan_email({**event(), 'text': 'changed content'}, '100')
        with self.assertRaisesRegex(ExternalEmailError, 'idempotency_payload_mismatch'):
            self.service.execute(changed)
        self.assertEqual(len(self.crm.writes), 1)

    def test_event_reclassification_cannot_create_second_email(self):
        original = {**event(), 'event_type': 'reply_received', 'human_reply': True}
        self.service.execute(plan_email(original, '100'))
        with self.assertRaisesRegex(ExternalEmailError, 'nonhuman_or_unverified_reply_held'):
            self.service.execute(plan_email({**original, 'event_type': 'auto_reply_received'}, '100'))
        self.assertEqual(len(self.crm.writes), 1)

    def test_unknown_and_automatic_replies_never_reach_crm(self):
        for kind, human in [('auto_reply_received', False), ('reply_received', False),
                            ('reply_received', None)]:
            with self.subTest(kind=kind, human=human):
                with self.assertRaisesRegex(ExternalEmailError, 'nonhuman_or_unverified_reply_held'):
                    self.service.execute(plan_email({**event(), 'event_type': kind,
                                                     'human_reply': human}, '100'))
        self.assertEqual(self.crm.writes, [])

    def test_human_reply_reaches_reader_only_after_verified_import(self):
        from thunder_os.cadence_readonly import project_activity
        plan = plan_email({**event(), 'event_type': 'reply_received', 'human_reply': True}, '100')
        self.assertTrue(self.service.execute(plan)['email_logged'])
        self.assertTrue(project_activity('emails', self.crm.objects['500'])['response_evidence'])

    def test_resealed_unknown_reply_plan_cannot_bypass_hold(self):
        from thunder_os.models import payload_hash
        plan = plan_email({**event(), 'event_type': 'reply_received', 'human_reply': True}, '100')
        plan['human_reply'] = None
        plan['plan_hash'] = payload_hash({key: value for key, value in plan.items() if key != 'plan_hash'})
        with self.assertRaisesRegex(ExternalEmailError, 'nonhuman_or_unverified_reply_held'):
            self.service.execute(plan)
        self.assertEqual(self.crm.writes, [])

    def test_outer_transaction_is_rejected_before_transport_or_state(self):
        plan = plan_email(event(), '100')
        observer = Repository(self.repo.path)
        effect, _ = ExternalEmailImport._keys(plan)
        self.crm.portal = Mock(side_effect=AssertionError('must not read provider'))
        with self.assertRaisesRegex(RuntimeError, 'force rollback'):
            with self.repo.transaction():
                with self.assertRaisesRegex(ExternalEmailError, 'outer_transaction_blocked'):
                    self.service.execute(plan)
                self.assertIsNone(observer.get_state('hubspot_attempt', effect))
                raise RuntimeError('force rollback')
        self.crm.portal.assert_not_called()
        self.assertEqual(self.crm.writes, [])
        self.assertIsNone(observer.get_state('hubspot_attempt', effect))
        self.crm.portal = lambda: PORTAL_ID
        self.assertEqual(self.service.execute(plan)['status'], 'verified_state')
        self.assertEqual(len(self.crm.writes), 1)

    def test_email_dedupe_unknown_blocks_and_existing_is_read_only(self):
        plan = plan_email(event(), '100')
        self.crm.dedupe['unattributed_candidates'] = 1
        self.assertEqual(self.service.execute(plan)['status'], 'needs_human')
        self.assertEqual(self.crm.writes, [])
        other = plan_email(event('another-source-id'), '100')
        self.crm.dedupe = {'complete': True, 'records': [{'id': '600'}], 'unattributed_candidates': 0}
        self.crm.objects['600'] = {'id': '600', 'archived': False, 'properties': other['properties'],
                                  'associations': {'contacts': {'results': [{'id': '100'}]}}}
        self.assertEqual(self.service.execute(other)['status'], 'verified_state')
        self.assertEqual(self.crm.writes, [])

    def test_accepted_response_is_not_persistence(self):
        self.crm.fault = 'accepted_only'
        result = self.service.execute(plan_email(event(), '100'))
        self.assertEqual(result['status'], 'pending_reconciliation')
        self.assertFalse(result['email_logged'])

    def test_authority_rechecked_after_reads(self):
        calls = []
        def authority(plan):
            calls.append(plan)
            return len(calls) == 1
        service = ExternalEmailImport(self.repo, self.crm, enabled=True, authorize=authority)
        self.assertEqual(service.execute(plan_email(event(), '100'))['status'], 'needs_human')
        self.assertEqual(self.crm.writes, [])

    def test_canonical_search_paginates_and_filters_aliases(self):
        calls = []
        def request(method, path, body, headers, timeout):
            calls.append((method, path, deepcopy(body)))
            if path == '/account-info/v3/details':
                result = {'portalId': int(PORTAL_ID)}
            elif path == '/crm/v3/properties/contacts':
                result = {'results': [{'name': name} for name in ('email', 'hs_additional_emails')]}
            else:
                self.assertEqual(method, 'POST')
                self.assertEqual(body['properties'], ['email', 'hs_additional_emails'])
                filt = body['filterGroups'][0]['filters'][0]
                if filt['operator'] == 'IN':
                    result = {'results': [], 'total': 0}
                elif 'after' not in body:
                    result = {'results': [record(primary='other@example.invalid')], 'total': 2,
                              'paging': {'next': {'after': '100'}}}
                else:
                    result = {'results': [record('101', 'unrelated@example.invalid', 'extra@example.invalid')],
                              'total': 2}
            return 200, json.dumps(result).encode()
        port = HubSpotExternalEmail(lambda: 'synthetic-token', request=request)
        result = port.contact_snapshot(['alias@example.invalid'])
        self.assertTrue(result['aliases_complete'])
        self.assertEqual([row['id'] for row in result['records']], ['100'])
        self.assertEqual(len(calls), 5)

    def test_incomplete_http_search_never_proves_absence(self):
        def request(method, path, body, headers, timeout):
            return 200, json.dumps({'total': 2, 'results': []}).encode()
        port = HubSpotExternalEmail(lambda: 'synthetic-token', request=request)
        with self.assertRaisesRegex(ExternalEmailError, 'incomplete_contact_dedupe'):
            port._search([{'propertyName': 'email', 'operator': 'IN', 'values': ['person@example.invalid']}])

    def test_wire_email_is_log_and_not_send(self):
        calls = []
        def request(method, path, body, headers, timeout):
            calls.append((method, path, body))
            return 201, b'{"id":"500"}'
        port = HubSpotExternalEmail(lambda: 'synthetic-token', request=request,
                                    enabled=True, authorize=lambda plan: True)
        port.write(plan_email(event(), '100'))
        self.assertEqual(calls[0][:2], ('POST', '/crm/v3/objects/emails'))
        self.assertEqual(calls[0][2]['associations'][0]['to']['id'], '100')
        self.assertEqual(calls[0][2]['associations'][0]['types'][0]['associationTypeId'], 198)

    def test_email_dedupe_canonical_list_finds_marker(self):
        plan = plan_email(event(), '100')
        def request(method, path, body, headers, timeout):
            self.assertEqual(method, 'GET')
            self.assertEqual(urlsplit(path).path, '/crm/v3/objects/emails')
            self.assertEqual(parse_qs(urlsplit(path).query)['associations'], ['contacts'])
            result = {'results': [{'id': '500', 'archived': False, 'properties': plan['properties']}]}
            return 200, json.dumps(result).encode()
        port = HubSpotExternalEmail(lambda: 'synthetic-token', request=request)
        self.assertEqual(port.email_dedupe(plan)['records'][0]['id'], '500')


if __name__ == '__main__':
    unittest.main()
