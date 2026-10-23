"""Real read-only adapters over synthetic transports; sockets are forbidden."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

from thunder_os.apollo_prospect_readonly import ApolloProspectReadonly
from thunder_os.external_email_import import INSTANTLY_WORKSPACE
from thunder_os.new_journey_membership import (
    CrossCampaignReadOnly, InstantlyMembershipReadOnly, _Budget, _https_request)


EMAIL = 'person@example.invalid'
CAMPAIGN = '00000000-0000-4000-8000-000000000002'
LEAD = '00000000-0000-4000-8000-000000000001'


def contact(statuses=(), **kwargs):
    return {'id': 'contact-1', 'email': EMAIL, 'team_id': 'team-1',
            'contact_campaign_statuses': list(statuses), **kwargs}


def apage(rows, page=1, total=None):
    total = len(rows) if total is None else total
    return {'contacts': rows, 'pagination': {'page': page, 'per_page': 100,
            'total_entries': total, 'total_pages': (total + 99) // 100}}


def lead(**kwargs):
    return {'id': LEAD, 'email': EMAIL, 'organization': INSTANTLY_WORKSPACE,
            'campaign': CAMPAIGN, 'status': 1, **kwargs}


class MembershipTests(unittest.TestCase):
    def setUp(self):
        self.network = patch('socket.socket', side_effect=AssertionError('no network'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def port(self, *, apollo_pages=None, instantly_pages=None, profile=None, workspace=None):
        self.apollo_calls, self.instantly_calls = [], []
        apollo_pages = deepcopy(apollo_pages if apollo_pages is not None else [apage([])])
        instantly_pages = deepcopy(instantly_pages if instantly_pages is not None else [{'items': []}])

        def apollo_request(method, path, body, headers, timeout):
            self.apollo_calls.append((method, path, deepcopy(body), timeout))
            data = (profile or {'id': 'user-1', 'team_id': 'team-1'}) if method == 'GET' else apollo_pages.pop(0)
            return 200, json.dumps(data).encode()

        def instantly_request(method, path, body, headers, timeout):
            self.instantly_calls.append((method, path, deepcopy(body), timeout))
            data = (workspace or {'id': INSTANTLY_WORKSPACE}) if method == 'GET' else instantly_pages.pop(0)
            return 200, json.dumps(data).encode()

        apollo = ApolloProspectReadonly(lambda: 'synthetic-apollo-key', enabled=True,
            request=apollo_request, timeout=20)
        instantly = InstantlyMembershipReadOnly(lambda: 'synthetic-instantly-key', request=instantly_request)
        return CrossCampaignReadOnly(apollo, instantly, apollo_account_id='team-1')

    def test_full_empty_searches_clear_with_fixed_exact_read_requests(self):
        result = self.port().lookup(' Person@example.invalid ')
        self.assertEqual(result['status'], 'clear')
        self.assertTrue(result['coverage']['complete'])
        self.assertEqual(result['coverage']['providers'], ['apollo', 'instantly'])
        self.assertEqual([(c[0], c[1]) for c in self.apollo_calls], [
            ('GET', '/api/v1/users/api_profile'), ('POST', '/api/v1/contacts/search')])
        self.assertEqual(self.apollo_calls[1][2], {'q_keywords': EMAIL, 'page': 1, 'per_page': 100})
        self.assertEqual(self.instantly_calls[1][:3], ('POST', '/api/v2/leads/list',
            {'contacts': [EMAIL], 'limit': 100, 'distinct_contacts': False}))
        self.assertTrue(all(0 < c[3] <= 8 for c in self.apollo_calls + self.instantly_calls))

    def test_apollo_active_or_paused_in_any_campaign_blocks(self):
        for status in ('active', 'paused'):
            result = self.port(apollo_pages=[apage([contact([{
                'emailer_campaign_id': 'unregistered-other-sequence', 'status': status}])])]).lookup(EMAIL)
            self.assertEqual(result['status'], 'occupied')
            self.assertEqual(result['reason'], 'apollo_membership_present')

    def test_apollo_finished_requires_actual_finished_date_failed_and_expired_unknown(self):
        for status, fields, expected in (
            ('finished', {'finished_at': '2026-10-20T12:00:00Z'}, 'clear'),
            ('finished', {}, 'unknown'), ('finished', {'finished_at': None}, 'unknown'),
            ('expired', {}, 'unknown'), ('failed', {}, 'unknown')):
            result = self.port(apollo_pages=[apage([contact([{
                'emailer_campaign_id': 'other', 'status': status, **fields}])])]).lookup(EMAIL)
            self.assertEqual(result['status'], expected)

    def test_apollo_missing_or_null_membership_never_clears(self):
        for row in ({'id': 'c1', 'email': EMAIL}, contact()):
            row.pop('contact_campaign_statuses', None)
            result = self.port(apollo_pages=[apage([row])]).lookup(EMAIL)
            self.assertEqual(result['status'], 'unknown')
        row = contact()
        row['contact_campaign_statuses'] = None
        self.assertEqual(self.port(apollo_pages=[apage([row])]).lookup(EMAIL)['status'], 'unknown')

    def test_contradictory_literal_membership_state_cannot_clear(self):
        row = contact([{'emailer_campaign_id': 'other', 'status': 'finished',
                        'state': 'active', 'finished_at': '2026-10-20T12:00:00Z'}])
        self.assertEqual(self.port(apollo_pages=[apage([row])]).lookup(EMAIL)['status'], 'unknown')

    def test_instantly_any_campaign_including_completed_blocks(self):
        for status in (1, 2, 3, -1, -2, -3):
            result = self.port(instantly_pages=[{'items': [lead(status=status)]}]).lookup(EMAIL)
            self.assertEqual(result['status'], 'occupied')
            self.assertEqual(result['reason'], 'instantly_membership_present')

    def test_missing_unknown_status_or_missing_campaign_is_unknown(self):
        for changes in ({'status': None}, {'status': 99}, {'status': True},
                        {'campaign': None}, {'campaign': ''}):
            self.assertEqual(self.port(instantly_pages=[{'items': [lead(**changes)]}])
                             .lookup(EMAIL)['status'], 'unknown')
        row = lead()
        del row['status']
        self.assertEqual(self.port(instantly_pages=[{'items': [row]}]).lookup(EMAIL)['status'], 'unknown')

    def test_mixed_unknown_does_not_hide_proven_blocker_but_cannot_clear(self):
        result = self.port(apollo_pages=[apage([{'id': 'c1', 'email': EMAIL}])],
            instantly_pages=[{'items': [lead()]}]).lookup(EMAIL)
        self.assertEqual(result['status'], 'occupied')
        self.assertFalse(result['coverage']['complete'])
        self.assertEqual(CrossCampaignReadOnly().lookup(EMAIL)['status'], 'unknown')

    def test_identity_mismatch_profile_search_workspace_or_row_never_clears(self):
        ports = [self.port(profile={'id': 'user-1', 'team_id': 'other-team'}),
                 self.port(apollo_pages=[apage([contact(team_id='other-team')])]),
                 self.port(workspace={'id': CAMPAIGN}),
                 self.port(instantly_pages=[{'items': [lead(organization=CAMPAIGN)]}])]
        for port in ports:
            self.assertEqual(port.lookup(EMAIL)['status'], 'unknown')

    def test_apollo_two_pages_aggregate_full_coverage_and_third_page_is_unknown(self):
        rows = [contact(id='c' + str(i), email='other%d@example.invalid' % i) for i in range(100)]
        result = self.port(apollo_pages=[apage(rows, total=101),
            apage([contact()], page=2, total=101)]).lookup(EMAIL)
        self.assertEqual(result['status'], 'clear')
        result = self.port(apollo_pages=[apage(rows, total=201),
            apage(rows, page=2, total=201)]).lookup(EMAIL)
        self.assertEqual(result['status'], 'unknown')
        self.assertLessEqual(len(self.apollo_calls), 3)

    def test_pagination_partial_null_items_repeated_cursor_or_wrong_email_unknown(self):
        for pages in ([{'items': None}], [{'items': [] , 'next_starting_after': LEAD}],
                      [{'items': [lead(email='other@example.invalid')]}],
                      [{'items': [], 'next_starting_after': 1}]):
            self.assertEqual(self.port(instantly_pages=pages).lookup(EMAIL)['status'], 'unknown')
        pages = [{'items': [lead(campaign=None)], 'next_starting_after': LEAD}] * 2
        self.assertEqual(self.port(instantly_pages=pages).lookup(EMAIL)['status'], 'unknown')

    def test_provider_errors_redacted_and_no_retry(self):
        apollo = ApolloProspectReadonly(lambda: 'secret', enabled=True,
            request=Mock(side_effect=RuntimeError('secret exception contents')))
        instantly = InstantlyMembershipReadOnly(lambda: 'secret',
            request=Mock(return_value=(429, b'secret error body')))
        result = CrossCampaignReadOnly(apollo, instantly).lookup(EMAIL)
        self.assertEqual(result['status'], 'unknown')
        self.assertNotIn('secret', json.dumps(result))
        self.assertEqual(apollo._request.call_count, 1)
        self.assertEqual(instantly.request.call_count, 1)

    def test_source_ref_stable_for_same_records_changes_with_target_status(self):
        first, second = self.port().lookup(EMAIL), self.port().lookup(EMAIL)
        self.assertEqual(first['source_ref'], second['source_ref'])
        active = self.port(instantly_pages=[{'items': [lead()]}]).lookup(EMAIL)
        paused = self.port(instantly_pages=[{'items': [lead(status=2)]}]).lookup(EMAIL)
        self.assertNotEqual(active['source_ref'], paused['source_ref'])

    def test_mixed_apollo_statuses_preserve_positive_blocker_and_order_stability(self):
        rows = [{'emailer_campaign_id': 'other-a', 'status': 'active'},
                {'emailer_campaign_id': 'other-b', 'status': 'unexpected'}]
        result = self.port(apollo_pages=[apage([contact(rows)])]).lookup(EMAIL)
        self.assertEqual(result['status'], 'occupied')
        self.assertFalse(result['coverage']['complete'])
        reversed_result = self.port(apollo_pages=[apage([contact(list(reversed(rows)))])]).lookup(EMAIL)
        self.assertEqual(result['source_ref'], reversed_result['source_ref'])

    def test_apollo_original_cache_timeout_and_key_are_not_mutated(self):
        port = self.port()
        original = port.apollo
        original._profile_identity = ('sentinel',)
        self.assertEqual(port.lookup(EMAIL)['status'], 'clear')
        self.assertEqual(original._profile_identity, ('sentinel',))
        self.assertEqual(original.timeout, 20)

    def test_deadline_between_provider_calls_cannot_clear_or_start_later_request(self):
        now = [0.0]
        port = self.port()
        port.clock = lambda: now[0]
        original = port.apollo._request

        def request(*args):
            result = original(*args)
            now[0] += 8
            return result

        port.apollo._request = request
        result = port.lookup(EMAIL)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(len(self.apollo_calls), 2)
        self.assertEqual(self.apollo_calls[0][3], 8)
        self.assertEqual(self.apollo_calls[1][3], 7)
        self.assertEqual(self.instantly_calls, [])

    def test_instantly_successful_cursor_path_keeps_all_campaigns(self):
        pages = [{'items': [lead(campaign=None)], 'next_starting_after': LEAD},
                 {'items': [lead(id=CAMPAIGN, status=2)]}]
        result = self.port(instantly_pages=pages).lookup(EMAIL)
        self.assertEqual(result['status'], 'occupied')
        self.assertFalse(result['coverage']['complete'])
        self.assertEqual(self.instantly_calls[2][2]['starting_after'], LEAD)
        self.assertNotIn('campaign', self.instantly_calls[2][2])

    def test_invalid_email_cannot_make_any_request(self):
        port = self.port()
        for address in (None, '', 'https://evil.invalid', 'a*masked@example.invalid',
                        'person@example.invalid\nInjected: value'):
            self.assertEqual(port.lookup(address)['status'], 'unknown')
        self.assertEqual(self.apollo_calls + self.instantly_calls, [])

    def test_shared_deadline_no_requests_and_row_cap(self):
        port = self.port()
        self.assertEqual(port.lookup(EMAIL, deadline=0)['status'], 'unknown')
        self.assertEqual(self.apollo_calls + self.instantly_calls, [])
        with self.assertRaisesRegex(ValueError, 'membership_row_limit'):
            port.lookup_many([EMAIL] * 201, deadline=0)
        self.assertEqual(len(port.lookup_many([EMAIL] * 200, deadline=0)), 200)
        self.assertEqual(self.apollo_calls + self.instantly_calls, [])

    def test_request_budget_stops_at_ten_without_background_or_retry(self):
        budget = _Budget(lambda: 0)
        request = Mock(return_value=(200, b'{}'))
        for _ in range(10):
            budget.call(request, 'GET', '/fixed', None, {}, 8)
        with self.assertRaisesRegex(ValueError, 'membership_budget_exhausted'):
            budget.call(request, 'GET', '/fixed', None, {}, 8)
        self.assertEqual(request.call_count, 10)

    def test_key_rotation_after_apollo_profile_cannot_reuse_identity(self):
        port = self.port()
        keys = iter(['key-one', 'key-two'])
        port.apollo._loader = lambda: next(keys)
        self.assertEqual(port.lookup(EMAIL)['status'], 'unknown')

    def test_wire_guard_blocks_every_non_list_post_and_arbitrary_filter(self):
        port = InstantlyMembershipReadOnly(lambda: 'secret', request=Mock())
        for method, path, body in (('PATCH', '/api/v2/workspaces/current', None),
            ('POST', '/api/v2/leads', {}), ('POST', 'https://evil.invalid/api/v2/leads/list', {}),
            ('GET', '/api/v2/leads/list', None),
            ('POST', '/api/v2/leads/list', {'contacts': [EMAIL], 'campaign': CAMPAIGN})):
            with self.assertRaises(ValueError):
                port._read(method, path, body, 'secret', None)
        port.request.assert_not_called()

    def test_default_post_transport_really_serializes_body_and_fixed_host(self):
        response = Mock(status=200)
        response.read.return_value = b'{"items":[]}'
        connection = Mock()
        connection.getresponse.return_value = response
        body = {'contacts': [EMAIL], 'limit': 100, 'distinct_contacts': False}
        with patch('thunder_os.new_journey_membership.http.client.HTTPSConnection', return_value=connection) as factory:
            result = _https_request('POST', '/api/v2/leads/list', body, {}, 8)
        self.assertEqual(factory.call_args.args[:2], ('api.instantly.ai', 443))
        self.assertEqual(json.loads(connection.request.call_args.kwargs['body']), body)
        connection.close.assert_called_once()
        self.assertEqual(result, (200, b'{"items":[]}'))


if __name__ == '__main__':
    unittest.main()
