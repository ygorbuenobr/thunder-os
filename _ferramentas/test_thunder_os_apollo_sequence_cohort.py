"""Sequence cohort discovery is bounded, scoped and never dispatches."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from thunder_os.apollo_sequence_cohort import ApolloSequenceCohort
from thunder_os.db import Repository
from thunder_os.service import ThunderService
from test_thunder_os_apollo_linkedin_authority import config


class SequenceCohortTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Repository(Path(self.tmp.name) / 'temporary.sqlite')
        service = ThunderService(self.repo)
        service.configure_campaign(config(), 'ygor', expected_version=0)
        self.repo.put_state('control', 'global', {'paused': False, 'actor': 'ygor'})
        review = service.apollo_campaign_approval_review('campaign')
        self.approval = service.approve_apollo_campaign_version('campaign', 'ygor',
            execution_confirmation=review['execution_confirmation'])['id']
        self.calls = []
        self.contacts = [self.row('one', 'seq'), self.row('two', 'other'), self.row('three', 'seq')]
        self.reader = ApolloSequenceCohort(self.repo, lambda: 'synthetic-key', api_actor_id='actor', request=self.http)

    @staticmethod
    def row(ident, sequence):
        return {'id': ident, 'contact_campaign_statuses': [
            {'emailer_campaign_id': sequence, 'status': 'active'}]}

    def http(self, method, path, body, headers, timeout):
        self.calls.append((method, path, body))
        if path == '/api/v1/users/api_profile':
            value = {'user': {'id': 'actor', 'team_id': 'team'}}
        else:
            start = (body['page'] - 1) * body['per_page']
            value = {'contacts': self.contacts[start:start + body['per_page']], 'pagination': {
                'page': body['page'], 'per_page': body['per_page'], 'total_entries': len(self.contacts)}}
        return 200, json.dumps(value).encode()

    def scan(self, **kwargs):
        return self.reader.scan('campaign', self.approval, 'v1', per_page=2, **kwargs)

    def test_complete_membership_discovery_does_not_need_linkedin_task_or_email(self):
        result = self.scan(max_pages=2)
        self.assertEqual(result['coverage'], 'partial')
        self.assertEqual(result['reason'], 'cohort_snapshot_unverified')
        self.assertTrue(result['all_pages_observed'])
        self.assertEqual([m['contact_id'] for m in result['members']], ['one', 'three'])
        self.assertEqual([call[:2] for call in self.calls], [
            ('GET', '/api/v1/users/api_profile'), ('POST', '/api/v1/contacts/search'),
            ('POST', '/api/v1/contacts/search')])

    def test_page_budget_is_partial_not_empty(self):
        result = self.scan(max_pages=1)
        self.assertEqual((result['coverage'], result['next_page']), ('partial', 2))
        self.assertEqual([m['contact_id'] for m in result['members']], ['one'])

    def test_multiple_sequences_keep_each_real_membership_and_ignore_outside(self):
        self.contacts = [self.row('one', 'seq'), self.row('two', 'other'), self.row('three', 'outside')]
        self.contacts[0]['contact_campaign_statuses'].append({'emailer_campaign_id': 'other', 'status': 'paused'})
        approved = {'binding': {'sequence_ids': ['seq', 'other'], 'team_id': 'team'}, 'scope_hash': 'a' * 64}
        with patch.object(self.reader, '_approved_binding', return_value=approved):
            result = self.scan(max_pages=2)
        self.assertEqual([(m['contact_id'], m['sequence_id'], m['membership_status']) for m in result['members']],
                         [('one', 'seq', 'active'), ('one', 'other', 'paused'), ('two', 'other', 'active')])

    def test_invalid_or_ambiguous_sequence_binding_fails_before_provider_read(self):
        for binding in ({'sequence_ids': [], 'team_id': 'team'},
                        {'sequence_ids': ['seq', 'seq'], 'team_id': 'team'},
                        {'sequence_ids': ['seq'], 'sequence_id': 'seq', 'team_id': 'team'}):
            with self.subTest(binding=binding), patch.object(self.reader, '_approved_binding', return_value={'binding': binding}):
                self.calls.clear()
                result = self.scan(max_pages=2)
                self.assertEqual(result['coverage'], 'unknown')
                self.assertEqual(self.calls, [])

    def test_resume_observes_next_page_without_claiming_previous_coverage(self):
        first = self.scan(max_pages=1)
        second = self.scan(max_pages=1, start_page=first['next_page'])
        self.assertEqual([m['contact_id'] for m in second['members']], ['three'])
        self.assertEqual((second['coverage'], second['reason'], second['next_page']),
                         ('partial', 'cohort_prefix_unverified', None))
        self.assertFalse(second['history_complete'])
        self.assertEqual(second['consistency'], 'provider_snapshot_unverified')

    def test_missing_membership_is_unknown_not_complete(self):
        self.contacts[1].pop('contact_campaign_statuses')
        result = self.scan(max_pages=2)
        self.assertNotEqual(result['coverage'], 'complete')
        self.assertEqual(result['members'][0]['contact_id'], 'one')

    def test_duplicate_contact_or_ambiguous_membership_is_not_complete(self):
        for mode in ('duplicate', 'membership'):
            with self.subTest(mode=mode):
                self.contacts = [self.row('one', 'seq'), self.row('two', 'seq'), self.row('three', 'seq')]
                if mode == 'duplicate': self.contacts[1]['id'] = 'one'
                else: self.contacts[1]['contact_campaign_statuses'].append({'emailer_campaign_id': 'seq', 'status': 'active'})
                self.assertNotEqual(self.scan(max_pages=2)['coverage'], 'complete')

    def test_missing_approval_produces_no_provider_calls(self):
        result = self.reader.scan('campaign', 'missing', 'v1')
        self.assertEqual(result['coverage'], 'unknown'); self.assertEqual(self.calls, [])

    def test_monitoring_continues_when_globally_paused_or_approval_revoked(self):
        self.repo.put_state('control', 'global', {'paused': True, 'actor': 'ygor'})
        approval = self.repo.get_state('approval', self.approval)
        approval['status'] = 'revoked'
        self.repo.put_state('approval', self.approval, approval)
        result = self.scan(max_pages=2)
        self.assertEqual(result['reason'], 'cohort_snapshot_unverified')
        self.assertTrue(result['all_pages_observed'])

    def test_invalid_team_and_mid_scan_revocation_fail_closed(self):
        def wrong_team(method, path, body, headers, timeout):
            if method == 'GET': return 200, b'{"user":{"id":"actor","team_id":"other"}}'
            self.fail('search must not run')
        self.reader.request = wrong_team
        self.assertEqual(self.scan(max_pages=2)['coverage'], 'unknown')
        self.reader.request = self.http
        original = self.reader._approved_binding
        n = [0]
        def changed(*args):
            n[0] += 1
            if n[0] > 2: raise ValueError('snapshot_changed')
            return original(*args)
        with patch.object(self.reader, '_approved_binding', changed):
            result = self.scan(max_pages=2)
        self.assertEqual(result['coverage'], 'partial')
        self.assertEqual([m['contact_id'] for m in result['members']], ['one'])

    def test_empty_is_complete_only_on_valid_zero_total(self):
        self.contacts = []
        result = self.scan(max_pages=1)
        self.assertEqual((result['coverage'], result['members'], result['source_total']), ('complete', [], 0))

    def test_provider_failure_after_first_page_retains_partial_observation(self):
        original = self.http
        def failing(method, path, body, headers, timeout):
            if method == 'POST' and body['page'] == 2:
                return 503, b'{}'
            return original(method, path, body, headers, timeout)
        self.reader.request = failing
        result = self.scan(max_pages=2)
        self.assertEqual(result['coverage'], 'partial')
        self.assertEqual([m['contact_id'] for m in result['members']], ['one'])

    def test_changed_total_never_proves_complete(self):
        original = self.http
        def changing(method, path, body, headers, timeout):
            status, raw = original(method, path, body, headers, timeout)
            if method == 'POST' and body['page'] == 2:
                data = json.loads(raw); data['pagination']['total_entries'] += 1
                return status, json.dumps(data).encode()
            return status, raw
        self.reader.request = changing
        self.assertEqual(self.scan(max_pages=2)['coverage'], 'partial')

if __name__ == '__main__':
    unittest.main()
