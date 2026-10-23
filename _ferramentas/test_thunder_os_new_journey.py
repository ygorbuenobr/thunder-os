"""Phase 2 contract: temporary real Repository/Service, fake reads, no network."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_campaign_sheet_support import make_sheet
from test_thunder_os_campaign_registry import config
from thunder_os.db import Repository
from thunder_os.new_journey import CHECK_KEYS, NewJourney, NewJourneyError
from thunder_os.service import ThunderService


class Apollo:
    def __init__(self):
        self.calls = []
        self.items = []
        self.sequence = {'id': 'sequence-2', 'name': 'Synthetic sequence',
                         'user_id': 'owner-1', 'active': True, 'archived': False}
        self.source = {'provider': 'apollo', 'account_id': 'team-2',
                       'identity_source': 'injected', 'endpoint': 'synthetic'}
        self.error = None

    def response(self, items):
        if self.error:
            return {'status': 'error', 'reason': self.error, 'items': [], 'credit_cost': 0}
        return {'status': 'ok', 'items': deepcopy(items), 'credit_cost': 0,
                'source': deepcopy(self.source), 'complete': True}

    def sequences_search(self, **kwargs):
        self.calls.append(('sequences', kwargs))
        return self.response([self.sequence] if self.sequence else [])

    def people_search(self, **kwargs):
        self.calls.append(('people', kwargs))
        return self.response(self.items)

    def contacts_search(self, **kwargs):
        self.calls.append(('contacts', kwargs))
        return self.response(self.items)


def passing(rows, campaign_id):
    return [{'source_key': row['source_key'], 'checks': [
        {'key': key, 'status': 'pass', 'reason': 'Synthetic verified absence/fit',
         'source': 'synthetic-record:revision-1'} for key in CHECK_KEYS]} for row in rows]


class NewJourneyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = Repository(self.root / 'state.db')
        store, sheet = make_sheet(self.root / 'brain')
        self.service = ThunderService(self.repo, knowledge_store=store)
        self.cfg = {**config(), 'campaign_id': 'BR-APOLLO-02', 'platform': 'apollo',
            'providers': {'email': 'apollo'}, 'briefing': sheet,
            'configuration': {'flow_version': '1', 'provider_accounts': {'email': 'sender@example.invalid'}}}
        self.service.campaign_registry.register(self.cfg, 'ygor', 0)
        self.now = datetime(2026, 10, 23, tzinfo=timezone.utc)
        self.apollo = Apollo()
        self.journey = NewJourney(self.service, self.apollo, passing, lambda: self.now)
        self.row = self.journey.create({'campaign_id': self.cfg['campaign_id'], 'name': 'Synthetic'}, 'ygor')
        self.network = patch('socket.socket.connect', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def source(self, text='name,company,email,linkedin_url,apollo_contact_id,hubspot_id\nAna,Example,ana@example.invalid,https://www.linkedin.com/in/ana,contact-1,hubspot-1\n'):
        self.row = self.journey.source(self.row['id'], {'mode': 'csv', 'csv_text': text},
                                       'ygor', self.row['version'])
        return self.row

    def check(self):
        self.row = self.journey.check(self.row['id'], 'ygor', self.row['version'])
        return self.row

    def steps(self, linkedin=False, select=True):
        steps = [{'id': 'E1', 'channel': 'email', 'subject': 'Exact subject',
                  'content': 'Exact original text\nSecond line', 'order': 1, 'interval_hours': 0}]
        if linkedin:
            steps.append({'id': 'L1', 'channel': 'linkedin', 'subject': '',
                          'content': 'Exact LinkedIn text', 'order': 2, 'interval_hours': 72})
        args = {'apollo_sequence_id': 'sequence-2', 'apollo_sequence_query': 'Synthetic'} if select else {}
        self.row = self.journey.revise_sequence(self.row['id'], steps, 'ygor', self.row['version'], **args)
        return self.row

    def ready(self, linkedin=False):
        self.source(); self.check(); self.steps(linkedin)
        return self.row

    def approve(self, step='E1'):
        self.row = self.journey.approve_step(self.row['id'], step, 'ygor',
                                            self.row['version'], self.row['review_hash'])
        return self.row

    def export(self):
        return self.journey.export(self.row['id'], 'ygor', self.row['version'], self.row['review_hash'])

    def assert_no_execution(self):
        for kind in ('cadence', 'approval', 'operation_approval'):
            self.assertEqual(self.repo.list_state(kind), [])
        self.assertEqual(self.repo.effects(), [])
        with self.repo.transaction() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM thunder_outbox').fetchone()[0], 0)

    def test_main_flow_exact_human_approval_csv_and_repeat_export_no_dispatch(self):
        self.ready(True)
        self.assertTrue(all(s['can_approve'] for s in self.row['steps']))
        self.approve(); self.approve('L1')
        output = self.export()
        self.assertEqual(output, self.export())
        rows = list(csv.DictReader(io.StringIO(output['csv_text'].removeprefix('\ufeff'))))
        self.assertEqual(len(rows), 1)
        self.assertEqual({k: rows[0][k] for k in ('name', 'company', 'email', 'apollo_contact_id', 'hubspot_id')},
            {'name': 'Ana', 'company': 'Example', 'email': 'ana@example.invalid',
             'apollo_contact_id': 'contact-1', 'hubspot_id': 'hubspot-1'})
        self.assertEqual(rows[0]['E1_content'], 'Exact original text\nSecond line')
        self.assertEqual(rows[0]['E1_interval_hours'], '0')
        self.assertEqual(rows[0]['L1_content'], 'Exact LinkedIn text')
        self.assertEqual(rows[0]['apollo_sequence_id'], 'sequence-2')
        self.assertFalse(output['enrollment_enabled'])
        self.assertEqual((output['credits'], output['external_writes']), (0, 0))
        pack = self.repo.get_state('journey_draft', self.row['id'])['approvals']['E1']['pack']
        self.assertEqual(pack['actor'], 'ygor')
        self.assertEqual(len(pack['step_hashes']), 1)
        self.assert_no_execution()

    def test_source_check_get_catalog_never_approve_and_catalog_has_bounded_summaries(self):
        self.ready()
        before = self.repo.get_state('journey_draft', self.row['id'])
        self.assertEqual(self.journey.get(self.row['id'], 'ygor')['approvals'], {})
        catalog = self.journey.catalog()
        self.assertEqual(catalog['campaigns'][0]['sheet']['status'], 'complete')
        self.assertEqual(set(catalog['drafts'][0]), {'id', 'name', 'campaign_id', 'version', 'updated_at'})
        self.assertNotIn('rows', catalog['drafts'][0])
        self.assertEqual(self.repo.get_state('journey_draft', self.row['id']), before)
        self.assert_no_execution()

    def test_all_steps_must_be_approved_and_missing_linkedin_destination_blocks(self):
        self.ready(True); self.approve()
        with self.assertRaisesRegex(NewJourneyError, 'approval_scope_changed'):
            self.export()
        self.source('email\nana@example.invalid\n'); self.check(); self.steps(True)
        self.assertFalse(self.row['steps'][1]['can_approve'])
        with self.assertRaisesRegex(NewJourneyError, 'recipient_required'):
            self.approve('L1')

    def test_missing_checker_or_each_missing_gate_is_unknown(self):
        self.source()
        self.journey.checker = None
        self.check()
        self.assertTrue(all(c['status'] == 'unknown' for c in self.row['checks'][0]['checks']))
        self.steps()
        self.assertFalse(self.row['steps'][0]['can_approve'])
        with self.assertRaisesRegex(NewJourneyError, 'checks_changed'):
            self.approve()
        for key in CHECK_KEYS:
            def incomplete(rows, campaign_id, missing=key):
                data = passing(rows, campaign_id)
                data[0]['checks'] = [c for c in data[0]['checks'] if c['key'] != missing]
                return data
            self.journey.checker = incomplete
            self.check()
            self.assertEqual(self.row['counts']['eligible'], 0)
            self.assertEqual(next(c for c in self.row['checks'][0]['checks'] if c['key'] == key)['status'], 'unknown')

    def test_freshness_rechecks_on_approve_and_export_without_trusting_previous_pass(self):
        self.ready()
        def changed(rows, campaign_id):
            result = passing(rows, campaign_id)
            result[0]['checks'][0].update(status='blocked', reason='ICP changed')
            return result
        self.journey.checker = changed
        with self.assertRaisesRegex(NewJourneyError, 'checks_changed'):
            self.approve()
        self.journey.checker = passing
        self.approve()
        self.journey.checker = changed
        with self.assertRaisesRegex(NewJourneyError, 'checks_changed'):
            self.export()
        self.assert_no_execution()

    def test_checker_failure_and_duplicate_evidence_never_clear(self):
        self.source()
        for checker in (lambda *_: (_ for _ in ()).throw(TimeoutError()),
                        lambda rows, campaign: passing(rows, campaign) * 2,
                        lambda *_: {'status': 'ok'}):
            self.journey.checker = checker
            self.check()
            self.assertEqual(self.row['counts']['eligible'], 0)

    def test_csv_bom_semicolon_quoted_newline_formula_escape_and_custom_variables(self):
        self.source('\ufeffName;Company;Email;var_cta\n"=FORMULA\nline";"+Company";ana@example.invalid;"@CTA"\n')
        self.check(); self.steps(); self.approve()
        csv_row = next(csv.DictReader(io.StringIO(self.export()['csv_text'].removeprefix('\ufeff'))))
        self.assertEqual(csv_row['name'], "'=FORMULA\nline")
        self.assertEqual(csv_row['company'], "'+Company")
        self.assertEqual(csv_row['var_cta'], "'@CTA")

    def test_duplicate_email_linkedin_block_but_same_name_never_dedupes(self):
        self.source('name,email,linkedin_url\nSame,a@example.invalid,https://linkedin.com/in/a\nSame,b@example.invalid,https://www.linkedin.com/in/a/\n')
        self.check()
        self.assertEqual(self.row['counts']['total'], 2)
        self.assertEqual(self.row['counts']['eligible'], 0)
        self.assertIn('duplicate_linkedin_url', self.row['rows'][0]['blockers'])
        self.source('name,email\nSame,A@example.invalid\nSame,a@example.invalid\n')
        self.assertIn('duplicate_email', self.row['rows'][1]['blockers'])

    def test_csv_recipient_never_rewritten_and_formula_addresses_are_retained(self):
        self.source('email\n+unsafe@example.invalid\n-unsafe@example.invalid\n=unsafe@example.invalid\nsafe+tag@example.invalid\n')
        self.check(); self.steps(); self.approve()
        self.assertEqual(self.row['counts']['eligible'], 1)
        for lead in self.row['rows'][:3]:
            self.assertIn('email_csv_unsafe', lead['blockers'])
        exported = list(csv.DictReader(io.StringIO(self.export()['csv_text'].removeprefix('\ufeff'))))
        self.assertEqual([lead['email'] for lead in exported], ['safe+tag@example.invalid'])

    def test_apollo_people_requires_explicit_geography_and_business_filter_before_call(self):
        for filters, code in (({}, 'geography_required'), ({'person_titles': ['CEO']}, 'geography_required'),
                              ({'person_locations': ['Brazil']}, 'business_filter_required'),
                              ({'person_locations': []}, 'geography_required')):
            with self.subTest(filters=filters), self.assertRaisesRegex(NewJourneyError, code):
                self.journey.source(self.row['id'], {'mode': 'apollo_people', 'filters': filters, 'page': 1},
                                    'ygor', self.row['version'])
        self.assertEqual(self.apollo.calls, [])

    def test_stable_checker_coverage_is_fingerprinted_but_consultation_time_is_not(self):
        self.ready()
        def timestamp_only(rows, campaign):
            result = passing(rows, campaign)
            for c in result[0]['checks']:
                c['checked_at'] = self.now.isoformat()
            return result
        self.journey.checker = timestamp_only
        self.approve()
        self.now += timedelta(minutes=1)
        self.assertEqual(self.export()['rows'], 1)
        def coverage_changed(rows, campaign):
            result = timestamp_only(rows, campaign)
            result[0]['checks'][0]['coverage'] = {'revision': 2}
            return result
        self.journey.checker = coverage_changed
        with self.assertRaisesRegex(NewJourneyError, 'checks_changed'):
            self.export()

    def test_csv_limits_and_malformed_required_email_are_explicit(self):
        for text in ('name\nAna\n', 'email,email\na@example.invalid,b@example.invalid\n',
                     'email\n"unfinished', 'email\na@example.invalid,extra\n',
                     'email\n' + 'a@example.invalid\n' * 201, 'email\n' + 'x' * (100 * 1024)):
            with self.subTest(text=text[:20]), self.assertRaises(NewJourneyError):
                self.source(text)
        for address in ('a*@example.invalid', 'bad', 'email_not_unlocked@domain.com', '=x@example.invalid'):
            self.source('email\n' + address + '\n')
            if address.startswith('='):
                self.assertIn('email_csv_unsafe', self.row['rows'][0]['blockers'])
            else:
                self.assertIn('email_invalid', self.row['rows'][0]['blockers'])

    def test_apollo_people_masked_email_keeps_distinct_person_ids_never_exports(self):
        self.apollo.items = [{'id': 'person-1', 'first_name': 'Ana', 'has_email': True,
                             'last_name_obfuscated': 'X***', 'email': 'a@example.invalid'},
                            {'id': 'person-2', 'first_name': 'Ana', 'has_email': True}]
        self.row = self.journey.source(self.row['id'], {'mode': 'apollo_people',
            'filters': {'person_locations': ['Brazil'], 'person_titles': ['CEO']}, 'page': 1},
                                       'ygor', self.row['version'])
        self.check()
        self.assertEqual(len({r['source_key'] for r in self.row['rows']}), 2)
        self.assertTrue(all(not r['email'] and not r['last_name'] and not r['apollo_contact_id'] for r in self.row['rows']))
        self.assertEqual(self.row['counts']['eligible'], 0)

    def test_empty_provider_results_and_safe_error_codes(self):
        self.row = self.journey.source(self.row['id'], {'mode': 'apollo_contacts', 'query': 'none', 'page': 1},
                                       'ygor', self.row['version'])
        self.assertEqual(self.row['counts']['total'], 0)
        for reason, expected in (('permission_denied', 'journey_apollo_permission_denied'),
                                  ('token=private', 'journey_apollo_unavailable')):
            self.apollo.error = reason
            with self.assertRaises(NewJourneyError) as raised:
                self.journey.sequences(self.row['id'], '', 'ygor', self.row['version'])
            self.assertEqual(raised.exception.code, expected)
            self.assertEqual(raised.exception.status, 503)

    def test_source_change_invalidates_checks_approval_and_old_review(self):
        self.ready(); self.approve()
        previous = deepcopy(self.row)
        self.source('email\nother@example.invalid\n')
        self.assertEqual(self.row['checks'], [])
        self.assertEqual(self.row['approvals'], {})
        with self.assertRaisesRegex(NewJourneyError, 'version_conflict'):
            self.journey.export(previous['id'], 'ygor', previous['version'], previous['review_hash'])
        self.check(); self.steps()
        with self.assertRaisesRegex(NewJourneyError, 'review_conflict'):
            self.journey.approve_step(self.row['id'], 'E1', 'ygor', self.row['version'], previous['review_hash'])

    def test_edit_exact_text_and_sequence_selection_invalidates_scope(self):
        self.ready(); self.approve()
        steps = [{k: s[k] for k in ('id', 'channel', 'subject', 'content', 'order', 'interval_hours')} for s in self.row['steps']]
        steps[0]['content'] = 'Changed exact text'
        self.row = self.journey.revise_sequence(self.row['id'], steps, 'ygor', self.row['version'])
        self.assertEqual(self.row['approvals'], {})
        with self.assertRaisesRegex(NewJourneyError, 'approval_scope_changed'):
            self.export()

    def test_expiry_pause_and_agent_pause_boundaries(self):
        self.ready(); self.approve()
        self.repo.put_state('agent_control', 'global', {'paused': True})
        self.assertEqual(self.export()['rows'], 1)
        self.service.set_global_pause(True, 'ygor')
        with self.assertRaisesRegex(NewJourneyError, 'global_paused'):
            self.export()
        self.assertFalse(self.journey.get(self.row['id'], 'ygor')['steps'][0]['can_approve'])
        self.service.set_global_pause(False, 'ygor')
        self.now += timedelta(hours=24)
        with self.assertRaisesRegex(NewJourneyError, 'approval_invalid'):
            self.export()

    def test_sheet_bytes_and_registry_context_changes_block(self):
        self.ready(); self.approve()
        (self.root / 'brain' / 'campaign.md').write_text('changed source')
        with self.assertRaisesRegex(NewJourneyError, 'sheet_incomplete'):
            self.export()
        self.assertFalse(self.journey.get(self.row['id'], 'ygor')['steps'][0]['can_approve'])
        make_sheet(self.root / 'brain')
        self.service.campaign_registry.register(self.cfg, 'ygor', 1)
        with self.assertRaisesRegex(NewJourneyError, 'review_conflict'):
            self.approve()

    def test_no_sequence_no_export_and_paired_selection_owner_and_unarchived_required(self):
        self.source(); self.check(); self.steps(select=False)
        self.assertFalse(self.row['steps'][0]['can_approve'])
        with self.assertRaisesRegex(NewJourneyError, 'sequence_required'):
            self.approve()
        for mutation in ({'user_id': None}, {'archived': None}, {'archived': True}):
            original = dict(self.apollo.sequence)
            self.apollo.sequence.update(mutation)
            with self.assertRaises(NewJourneyError): self.steps()
            self.apollo.sequence = original
        self.apollo.source['account_id'] = None
        with self.assertRaisesRegex(NewJourneyError, 'owner_unverified'):
            self.steps()

    def test_inactive_or_unknown_sequence_can_prepare_approve_and_export_without_activation(self):
        self.source(); self.check()
        for active, status in ((False, 'inactive'), (None, 'unknown')):
            with self.subTest(active=active):
                if active is None:
                    self.apollo.sequence.pop('active', None)
                else:
                    self.apollo.sequence['active'] = active
                provider_before = deepcopy(self.apollo.sequence)
                self.steps()
                self.assertEqual(self.row['sequence']['activity_status'], status)
                self.assertTrue(self.row['steps'][0]['can_approve'])
                self.approve()
                result = self.export()
                self.assertEqual(result['rows'], 1)
                self.assertEqual(result['external_writes'], 0)
                self.assertFalse(result['enrollment_enabled'])
                self.assertEqual(self.apollo.sequence, provider_before)
                self.assertTrue(all(operation == 'sequences' for operation, _ in self.apollo.calls))
                self.assert_no_execution()

    def test_profile_precedes_sequence_search_and_verified_profile_proof_is_accepted(self):
        self.source(); self.check()
        def profile():
            self.apollo.calls.append(('profile', {}))
            self.apollo.source['identity_source'] = 'verified_profile'
            return self.apollo.response([{'id': 'owner-1', 'team_id': 'team-2'}])
        self.apollo.profile = profile
        self.steps()
        self.assertEqual([operation for operation, _ in self.apollo.calls], ['profile', 'sequences'])
        self.assertEqual(self.row['sequence']['source']['identity_source'], 'verified_profile')
        self.approve()
        self.assertEqual(self.export()['rows'], 1)
        self.assert_no_execution()

    def test_profile_failure_blocks_search_without_mutating_draft(self):
        self.source(); self.check()
        before = self.repo.get_state('journey_draft', self.row['id'])
        def profile():
            self.apollo.calls.append(('profile', {}))
            return {'status': 'error', 'reason': 'permission_denied', 'items': [], 'credit_cost': 0}
        self.apollo.profile = profile
        with self.assertRaisesRegex(NewJourneyError, '^journey_apollo_permission_denied$'):
            self.steps()
        self.assertEqual(self.apollo.calls, [('profile', {})])
        self.assertEqual(self.repo.get_state('journey_draft', self.row['id']), before)

    def test_partial_profile_cannot_reuse_previous_identity_as_authority(self):
        self.source(); self.check()
        self.apollo.source['identity_source'] = 'verified_profile'
        self.apollo.profile = lambda: {'status': 'partial', 'items': [], 'credit_cost': 0, 'complete': False}
        before = self.repo.get_state('journey_draft', self.row['id'])
        with self.assertRaisesRegex(NewJourneyError, '^journey_apollo_configuration_pending$'):
            self.steps()
        self.assertEqual(self.apollo.calls, [])
        self.assertEqual(self.repo.get_state('journey_draft', self.row['id']), before)

    def test_actual_adapter_profile_supplies_identity_when_sequence_response_has_no_team(self):
        from thunder_os.apollo_prospect_readonly import ApolloProspectReadonly
        self.source(); self.check()
        requests = []
        sequence = deepcopy(self.apollo.sequence)
        def request(method, path, body, headers, timeout):
            requests.append((method, path.split('?')[0]))
            if path == '/api/v1/users/api_profile':
                self.assertEqual(method, 'GET')
                self.assertIsNone(body)
                data = {'id': 'owner-1', 'team_id': 'team-2'}
            else:
                self.assertEqual(method, 'POST')
                self.assertTrue(path.startswith('/api/v1/emailer_campaigns/search?'))
                data = {'emailer_campaigns': [sequence], 'pagination': {
                    'page': 1, 'per_page': 100, 'total_entries': 1, 'total_pages': 1}}
            return 200, json.dumps(data).encode()
        self.journey.apollo = ApolloProspectReadonly(lambda: 'synthetic-free-key',
                                                     request=request, enabled=True)
        self.steps(); self.approve()
        self.assertEqual(self.export()['rows'], 1)
        self.assertEqual(self.row['sequence']['source']['identity_source'], 'verified_profile')
        self.assertEqual(self.row['sequence']['account_id'], 'team-2')
        self.assertEqual(requests, [('GET', '/api/v1/users/api_profile'),
            ('POST', '/api/v1/emailer_campaigns/search')] * 3)
        self.assert_no_execution()

    def test_formula_leading_copy_rejected_before_state_change(self):
        self.ready(); self.approve()
        before = self.repo.get_state('journey_draft', self.row['id'])
        fields = ('id', 'channel', 'subject', 'content', 'order', 'interval_hours')
        for field in ('subject', 'content'):
            for prefix in ('-', '+', '=', '@', '  ='):
                with self.subTest(field=field, prefix=prefix):
                    steps = [{key: step[key] for key in fields} for step in self.row['steps']]
                    steps[0][field] = prefix + 'Exact text'
                    with self.assertRaisesRegex(NewJourneyError, '^journey_csv_copy_unsafe$'):
                        self.journey.revise_sequence(self.row['id'], steps, 'ygor', self.row['version'])
                    self.assertEqual(self.repo.get_state('journey_draft', self.row['id']), before)

    def test_every_exported_subject_and_content_exactly_matches_approved_copy(self):
        self.ready(True)
        fields = ('id', 'channel', 'subject', 'content', 'order', 'interval_hours')
        steps = [{key: step[key] for key in fields} for step in self.row['steps']]
        steps[0].update(subject='Original, "quoted" subject', content='Original, "quoted" body\nExact next line')
        steps[1]['content'] = 'Original LinkedIn text\nExact next line'
        self.row = self.journey.revise_sequence(self.row['id'], steps, 'ygor', self.row['version'])
        self.approve(); self.approve('L1')
        output = self.export()
        for exported in csv.DictReader(io.StringIO(output['csv_text'].removeprefix('\ufeff'))):
            for step in steps:
                for field in ('subject', 'content'):
                    self.assertEqual(exported[step['id'] + '_' + field], step[field])
        self.assert_no_execution()

    def test_unresolved_template_variables_rejected_without_state_change(self):
        self.ready()
        before = self.repo.get_state('journey_draft', self.row['id'])
        for field in ('subject', 'content'):
            steps = [{k: s[k] for k in ('id', 'channel', 'subject', 'content', 'order', 'interval_hours')} for s in self.row['steps']]
            steps[0][field] = 'Hello {{first_name}}'
            with self.assertRaisesRegex(NewJourneyError, 'journey_unresolved_variables'):
                self.journey.revise_sequence(self.row['id'], steps, 'ygor', self.row['version'])
        self.assertEqual(self.repo.get_state('journey_draft', self.row['id']), before)

    def test_concurrent_writers_only_one_wins(self):
        version = self.row['version']
        def write(_):
            try:
                return self.journey.source(self.row['id'], {'mode': 'csv', 'csv_text': 'email\na@example.invalid\n'}, 'ygor', version)['version']
            except NewJourneyError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(write, range(2)))
        self.assertCountEqual(outcomes, [version + 1, 'journey_version_conflict'])

    def test_checker_toctou_concurrent_state_change_blocks_approval(self):
        self.ready()
        def racing(rows, campaign_id):
            self.repo.put_state('journey_draft', self.row['id'], self.repo.get_state('journey_draft', self.row['id']))
            return passing(rows, campaign_id)
        self.journey.checker = racing
        with self.assertRaisesRegex(NewJourneyError, 'version_conflict'):
            self.approve()
        self.assertEqual(self.repo.get_state('journey_draft', self.row['id'])['approvals'], {})

    def test_human_authority_and_strict_expected_version(self):
        for actor in ('hermes', 'claude', '', None):
            with self.assertRaisesRegex(NewJourneyError, 'human_authority'):
                self.journey.get(self.row['id'], actor)
        for version in (None, True, '1', -1):
            with self.assertRaisesRegex(NewJourneyError, 'version_invalid'):
                self.journey.check(self.row['id'], 'ygor', version)
        with self.assertRaises(NewJourneyError) as raised:
            self.journey.get('missing', 'ygor')
        self.assertEqual(raised.exception.status, 404)

    def test_reconciliation_configuration_pending_no_calls_and_get_retains_observation(self):
        self.source()
        calls = len(self.apollo.calls)
        self.row = self.journey.reconcile(self.row['id'], 'ygor', self.row['version'])
        self.assertEqual(len(self.apollo.calls), calls)
        reconciliation = self.row['reconciliation']
        self.assertEqual(reconciliation['status'], 'configuration_pending')
        self.assertFalse(reconciliation['current'])
        self.assertIn('observed_at', reconciliation)
        self.assertEqual(self.journey.get(self.row['id'], 'ygor')['reconciliation'], reconciliation)

    def test_reconciliation_exact_sequence_and_missing_status_semantics(self):
        self.ready()
        for statuses, expected in (([{'emailer_campaign_id': 'other', 'status': 'active'}], 'unknown'),
                                   ([{'emailer_campaign_id': 'sequence-2'}], 'unknown'),
                                   ([{'emailer_campaign_id': 'sequence-2', 'status': 'paused'}], 'unknown'),
                                   ([{'emailer_campaign_id': 'sequence-2', 'status': 'active'}], 'enrolled')):
            self.apollo.items = [{'id': 'contact-1', 'email': 'ana@example.invalid',
                'contact_campaign_statuses': statuses, 'contact_campaign_statuses_complete': True}]
            self.row = self.journey.reconcile(self.row['id'], 'ygor', self.row['version'])
            self.assertEqual(self.row['reconciliation']['rows'][0]['status'], expected)
        self.assert_no_execution()

    def test_reconciliation_request_budget_and_no_provider_or_account_proof(self):
        self.source('email\n' + ''.join(f'person{i}@example.invalid\n' for i in range(12)))
        self.check(); self.steps()
        self.row = self.journey.reconcile(self.row['id'], 'ygor', self.row['version'])
        self.assertEqual(self.row['reconciliation']['request_count'], 10)
        self.assertEqual(self.row['reconciliation']['rows'][10]['reason'], 'request_budget_exhausted')
        self.journey.apollo = None
        self.row = self.journey.reconcile(self.row['id'], 'ygor', self.row['version'])
        self.assertEqual(self.row['reconciliation']['request_count'], 0)
        self.assertTrue(all(r['status'] == 'unknown' for r in self.row['reconciliation']['rows']))

    def test_reconciliation_partial_statuses_or_wrong_team_cannot_prove_enrollment(self):
        self.ready()
        self.apollo.source['identity_source'] = 'provider_response_team'
        self.apollo.items = [{'id': 'contact-1', 'email': 'ana@example.invalid',
            'contact_campaign_statuses': [{'emailer_campaign_id': 'sequence-2', 'status': 'active'}],
            'contact_campaign_statuses_complete': False}]
        self.row = self.journey.reconcile(self.row['id'], 'ygor', self.row['version'])
        self.assertEqual(self.row['reconciliation']['rows'][0]['status'], 'unknown')
        self.apollo.items[0]['contact_campaign_statuses_complete'] = True
        self.apollo.source['account_id'] = 'other-team'
        self.row = self.journey.reconcile(self.row['id'], 'ygor', self.row['version'])
        self.assertEqual(self.row['reconciliation']['rows'][0]['reason'], 'journey_apollo_account_mismatch')

    def test_sequence_catalog_get_and_provider_sequence_change_cannot_mint_approval(self):
        self.ready(); self.approve()
        before = self.repo.get_state('journey_draft', self.row['id'])
        self.journey.sequences(self.row['id'], '', 'ygor', self.row['version'])
        self.assertEqual(self.repo.get_state('journey_draft', self.row['id']), before)
        self.apollo.sequence['user_id'] = 'new-owner'
        with self.assertRaisesRegex(NewJourneyError, 'sequence_changed'):
            self.export()

    def test_draft_catalog_caps_summaries_at_twenty(self):
        for index in range(21):
            self.journey.create({'campaign_id': self.cfg['campaign_id'], 'name': 'Synthetic ' + str(index)}, 'ygor')
        self.assertEqual(len(self.journey.catalog()['drafts']), 20)


if __name__ == '__main__':
    unittest.main()
