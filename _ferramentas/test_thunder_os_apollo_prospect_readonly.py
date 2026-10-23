"""Synthetic free Apollo contract proofs. All sockets are forbidden."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from thunder_os.apollo_prospect_readonly import ApolloProspectReadonly
from thunder_os.external_email_import import ExternalEmailError


def person(ident='person-1'):
    return {'id': ident, 'first_name': 'Synthetic', 'last_name_obfuscated': 'Te***t',
            'title': 'Director', 'has_email': True, 'email': 'masked***@example.invalid',
            'phone_numbers': ['PRIVATE'], 'organization': {'name': 'Example', 'api_key': 'PRIVATE'}}


def paged(rows, *, key='contacts', page=1, per_page=25, total=None):
    total = len(rows) if total is None else total
    return {key: rows, 'pagination': {'page': page, 'per_page': per_page,
            'total_entries': total, 'total_pages': (total + per_page - 1) // per_page}}


class ApolloProspectReadonlyTests(unittest.TestCase):
    def setUp(self):
        network = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        network.start()
        self.addCleanup(network.stop)

    def client(self, data=None, *, status=200, **kwargs):
        self.calls = []

        def request(method, path, body, headers, timeout):
            self.calls.append((method, path, deepcopy(body), dict(headers), timeout))
            return status, json.dumps(data).encode()

        return ApolloProspectReadonly(lambda: 'synthetic-secret', enabled=True, request=request, **kwargs)

    def test_profile_is_one_exact_free_get_and_projects_only_user_and_team(self):
        client = self.client({'id': 'user-1', 'team_id': 'team-1', 'email': 'PRIVATE@example.invalid',
                             'first_name': 'PRIVATE', 'api_key': 'synthetic-secret',
                             'num_credits_remaining': 123})
        result = client.profile()
        self.assertEqual(self.calls[0][:3], ('GET', '/api/v1/users/api_profile', None))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result['items'], [{'id': 'user-1', 'team_id': 'team-1'}])
        self.assertEqual(result['source']['required_scope'], 'read_user_profile')
        self.assertEqual(result['source']['account_id'], 'team-1')
        self.assertEqual(result['source']['authenticated_user_id'], 'user-1')
        self.assertEqual(result['source']['identity_source'], 'verified_profile')
        self.assertIs(result['source']['identity_verified'], True)
        self.assertEqual(result['source']['identity_cache_age_seconds'], 0)
        self.assertEqual(result['source']['identity_ttl_seconds'], 300)
        self.assertEqual(result['credit_cost'], 0)
        self.assertTrue(result['complete'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('synthetic-secret', json.dumps(result))

    def test_profile_wire_rejects_every_query_body_and_other_method(self):
        client = self.client({})
        for method in ('POST', 'PUT', 'PATCH', 'DELETE', 'HEAD'):
            with self.assertRaisesRegex(ExternalEmailError, '^endpoint_blocked$'):
                client._call(method, '/api/v1/users/api_profile', None, {})
        for path, body in (('/api/v1/users/api_profile?include_credit_usage=true', None),
                           ('/api/v1/users/api_profile?include_credit_usage=false', None),
                           ('/api/v1/users/api_profile?', None),
                           ('/api/v1/users/api_profile', {}),
                           ('/api/v1/users/api_profile?route=people/match', None)):
            with self.assertRaises(ExternalEmailError):
                client._call('GET', path, body, {})
        self.assertEqual(self.calls, [])

    def test_invalid_or_nested_profile_never_verifies_or_populates_cache(self):
        for data in ({}, {'id': 'user-1'}, {'team_id': 'team-1'},
                     {'id': 'bad/id', 'team_id': 'team-1'},
                     {'id': 'user-1', 'team_id': ['team-1']},
                     {'id': 'user-1', 'team_id': 'synthetic-secret'},
                     {'user': {'id': 'user-1', 'team_id': 'team-1'}}):
            client = self.client(data)
            result = client.profile()
            self.assertEqual(result['reason'], 'invalid_profile')
            self.assertFalse(result['source']['identity_verified'])
            self.assertIsNone(result['source']['account_id'])
            self.assertIsNone(client._profile_identity)

    def test_explicit_profile_proof_propagates_only_to_same_instance_and_current_key(self):
        responses = [(200, json.dumps(data).encode()) for data in (
            {'id': 'user-1', 'team_id': 'team-1'},
            paged([], key='emailer_campaigns'), paged([]), paged([]), paged([]))]
        request = Mock(side_effect=responses)
        key = ['key-a']
        client = ApolloProspectReadonly(lambda: key[0], enabled=True, request=request)
        client.profile()
        sequence = client.sequences_search('')
        self.assertEqual(sequence['source']['identity_source'], 'verified_profile')
        self.assertTrue(sequence['source']['identity_verified'])
        self.assertEqual(sequence['source']['account_id'], 'team-1')
        self.assertEqual(sequence['source']['authenticated_user_id'], 'user-1')
        self.assertTrue(client.contacts_search('')['source']['identity_verified'])
        self.assertEqual(request.call_count, 3)
        key[0] = 'key-b'
        result = client.contacts_search('')
        self.assertFalse(result['source']['identity_verified'])
        self.assertIsNone(result['source']['account_id'])
        key[0] = 'key-a'
        self.assertFalse(client.contacts_search('')['source']['identity_verified'])
        self.assertEqual([call.args[0] for call in request.call_args_list], ['GET', 'POST', 'POST', 'POST', 'POST'])
        other = ApolloProspectReadonly(lambda: 'key-a', enabled=True,
            request=Mock(return_value=(200, json.dumps(paged([])).encode())))
        self.assertFalse(other.contacts_search('')['source']['identity_verified'])

    def test_profile_identity_ttl_uses_monotonic_age_and_requires_explicit_refresh(self):
        for now in (299, 300, 301, -1):
            request = Mock(side_effect=[(200, b'{"id":"user-1","team_id":"team-1"}'),
                                        (200, json.dumps(paged([])).encode())])
            client = ApolloProspectReadonly(lambda: 'key-a', enabled=True, request=request)
            with patch('thunder_os.apollo_prospect_readonly.time.monotonic', return_value=0):
                client.profile()
            with patch('thunder_os.apollo_prospect_readonly.time.monotonic', return_value=now):
                result = client.contacts_search('')
            self.assertIs(result['source']['identity_verified'], now == 299)
            self.assertEqual(request.call_count, 2)
            self.assertNotIn('verified_at', result['source'])

    def test_failed_profile_refresh_clears_previous_proof(self):
        for status, raw in ((403, b'PRIVATE'), (200, b'{"id":"user-1"}'),
                            (302, b'PRIVATE'), (200, b'{')):
            request = Mock(side_effect=[(200, b'{"id":"user-1","team_id":"team-1"}'),
                                       (status, raw), (200, json.dumps(paged([])).encode())])
            client = ApolloProspectReadonly(lambda: 'key-a', enabled=True, request=request)
            self.assertTrue(client.profile()['source']['identity_verified'])
            self.assertFalse(client.profile()['source']['identity_verified'])
            self.assertFalse(client.contacts_search('')['source']['identity_verified'])
            self.assertEqual(request.call_count, 3)

    def test_profile_proves_actual_team_without_assuming_expected_namespace(self):
        client = self.client({'id': 'user-other', 'team_id': 'other-team'},
                             identity_reader=lambda: {'team_id': 'expected-team'})
        result = client.profile()
        self.assertEqual(result['source']['account_id'], 'other-team')
        self.assertEqual(result['source']['injected_account_id'], 'expected-team')
        self.assertTrue(result['source']['identity_verified'])
        self.assertNotIn('namespace_verified', result['source'])

    def test_conflicting_team_on_operation_cannot_reuse_profile_proof(self):
        data = paged([{'id': 'c1', 'team_id': 'team-other'}])
        request = Mock(side_effect=[(200, b'{"id":"user-1","team_id":"team-1"}'),
                                   (200, json.dumps(data).encode()),
                                   (200, json.dumps(paged([])).encode())])
        client = ApolloProspectReadonly(lambda: 'key-a', enabled=True, request=request)
        client.profile()
        result = client.contacts_search('')
        self.assertFalse(result['source']['identity_verified'])
        self.assertIsNone(result['source']['account_id'])
        self.assertEqual(result['source']['identity_reason'], 'profile_team_mismatch')
        self.assertFalse(result['complete'])
        self.assertNotIn('team_id', result['source'])
        self.assertFalse(client.contacts_search('')['source']['identity_verified'])

    def test_profile_and_search_serialize_auth_and_share_only_completed_proof(self):
        entered, release, search_started = threading.Event(), threading.Event(), threading.Event()
        methods = []
        loader = Mock(return_value='key-a')

        def request(method, path, body, headers, timeout):
            methods.append(method)
            if method == 'GET':
                entered.set()
                self.assertTrue(release.wait(2))
                return 200, b'{"id":"user-1","team_id":"team-1"}'
            return 200, json.dumps(paged([])).encode()

        client = ApolloProspectReadonly(loader, enabled=True, request=request)

        def search():
            search_started.set()
            return client.contacts_search('')

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(client.profile)
            try:
                self.assertTrue(entered.wait(1))
                second = pool.submit(search)
                self.assertTrue(search_started.wait(1))
                self.assertEqual(loader.call_count, 1)
                self.assertEqual(methods, ['GET'])
            finally:
                release.set()
            self.assertTrue(first.result(timeout=2)['source']['identity_verified'])
            self.assertTrue(second.result(timeout=2)['source']['identity_verified'])
        self.assertEqual(methods, ['GET', 'POST'])

    def test_people_wire_uses_only_free_api_search_and_explicit_filters(self):
        filters = {'person_titles': ['Sales Director', 'CEO'], 'person_locations': ['Brazil'],
                   'organization_locations': ['Sao Paulo'],
                   'organization_num_employees_ranges': ['50,100', '101,500'], 'q_keywords': 'food'}
        original = deepcopy(filters)
        client = self.client({'people': [person()], 'total_entries': 1})
        result = client.people_search(filters)
        method, path, body, headers, timeout = self.calls[0]
        self.assertEqual(method, 'POST')
        self.assertEqual(urlsplit(path).path, '/api/v1/mixed_people/api_search')
        self.assertIsNone(body)
        self.assertEqual(parse_qs(urlsplit(path).query), {
            'person_titles[]': ['Sales Director', 'CEO'], 'person_locations[]': ['Brazil'],
            'organization_locations[]': ['Sao Paulo'],
            'organization_num_employees_ranges[]': ['50,100', '101,500'],
            'q_keywords': ['food'], 'page': ['1'], 'per_page': ['25']})
        self.assertEqual(filters, original)
        self.assertEqual(headers['x-api-key'], 'synthetic-secret')
        self.assertEqual(timeout, 15)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['credit_cost'], 0)
        self.assertEqual(result['items'][0]['last_name_obfuscated'], 'Te***t')
        self.assertTrue(result['items'][0]['has_email'])
        self.assertNotIn('email', result['items'][0])
        self.assertNotIn('phone_numbers', result['items'][0])
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_contact_search_reads_existing_contacts_without_registering(self):
        client = self.client(paged([{'id': 'contact-1', 'email': 'real@example.invalid',
                                    'email_unsubscribed': True, 'api_key': 'PRIVATE'}]))
        result = client.contacts_search('Synthetic')
        method, path, body, _, _ = self.calls[0]
        self.assertEqual((method, path), ('POST', '/api/v1/contacts/search'))
        self.assertEqual(body, {'q_keywords': 'Synthetic', 'page': 1, 'per_page': 25})
        self.assertEqual(result['items'], [{'id': 'contact-1', 'email': 'real@example.invalid',
                                           'email_unsubscribed': True}])
        self.assertTrue(result['complete'])

    def test_contact_masked_or_unlocked_placeholder_is_never_an_email(self):
        for address in ('te***@example.invalid', 'email_not_unlocked@domain.com', None, 'bad'):
            client = self.client(paged([{'id': 'contact-1', 'email': address}]))
            self.assertNotIn('email', client.contacts_search('')['items'][0])

    def test_contact_campaign_statuses_preserve_actual_states_and_known_fields(self):
        statuses = [{'emailer_campaign_id': 'sequence-' + state, 'status': state,
                     'id': 'membership-' + state, 'added_by_user_id': 'user-1',
                     'added_at': '2026-10-23T12:00:00Z', 'finished_at': None,
                     'manually_set_unpause': False, 'current_step_position': 2,
                     'failure_reason': None, 'api_key': 'PRIVATE'}
                    for state in ('active', 'paused', 'failed', 'finished')]
        statuses[1]['state'] = 'paused'
        original = deepcopy(statuses)
        client = self.client(paged([{'id': 'contact-1', 'contact_campaign_statuses': statuses}]))
        result = client.contacts_search('')
        projected = result['items'][0]['contact_campaign_statuses']
        self.assertEqual([row['status'] for row in projected], ['active', 'paused', 'failed', 'finished'])
        self.assertEqual(projected[1]['state'], 'paused')
        self.assertEqual(projected[0]['added_at'], '2026-10-23T12:00:00Z')
        self.assertEqual(projected[0]['added_by_user_id'], 'user-1')
        self.assertEqual(projected[0]['current_step_position'], 2)
        self.assertIsNone(projected[0]['finished_at'])
        self.assertIs(projected[0]['manually_set_unpause'], False)
        self.assertTrue(result['items'][0]['contact_campaign_statuses_complete'])
        self.assertTrue(result['complete'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertEqual(statuses, original)

    def test_malformed_memberships_are_unknown_partial_and_never_fabricate_active(self):
        bad_lists = (None, {}, [{'status': 'active'}],
                     [{'emailer_campaign_id': '../sequence', 'status': 'active'}],
                     [{'emailer_campaign_id': 'sequence-1'}],
                     [{'emailer_campaign_id': 'sequence-1', 'status': {'value': 'active'}}],
                     [{'emailer_campaign_id': 'sequence-1', 'status': 'active', 'state': []}],
                     [{'emailer_campaign_id': 'sequence-1', 'status': 'active', 'added_at': 'yesterday'}],
                     [{'emailer_campaign_id': 'sequence-1', 'status': 'active', 'current_step_position': True}],
                     [{'emailer_campaign_id': 'sequence-1', 'status': 'active', 'added_by_user_id': 'bad/id'}],
                     [{'emailer_campaign_id': 'sequence-1', 'status': 'active'}] * 51)
        for statuses in bad_lists:
            with self.subTest(statuses=statuses):
                client = self.client(paged([{'id': 'contact-1', 'contact_campaign_statuses': statuses}]))
                result = client.contacts_search('')
                self.assertEqual(result['items'][0]['contact_campaign_statuses'], [])
                self.assertFalse(result['items'][0]['contact_campaign_statuses_complete'])
                self.assertFalse(result['complete'])
                self.assertEqual(result['reason'], 'partial_response')

    def test_duplicate_sequence_memberships_do_not_hide_conflicts(self):
        client = self.client(paged([{'id': 'contact-1', 'contact_campaign_statuses': [
            {'emailer_campaign_id': 'sequence-1', 'status': 'active'},
            {'emailer_campaign_id': 'sequence-1', 'status': 'paused'},
            {'emailer_campaign_id': 'sequence-2', 'status': 'finished'}]}]))
        result = client.contacts_search('')
        self.assertEqual(result['items'][0]['contact_campaign_statuses'], [
            {'emailer_campaign_id': 'sequence-2', 'status': 'finished'}])
        self.assertEqual(result['reason'], 'partial_response')
        self.assertFalse(result['complete'])

    def test_memberships_are_contacts_only_and_conflicting_literal_state_is_preserved(self):
        status = {'emailer_campaign_id': 'sequence-1', 'status': 'paused', 'state': 'active'}
        client = self.client(paged([{'id': 'contact-1', 'contact_campaign_statuses': [status]}]))
        result = client.contacts_search('')
        self.assertEqual(result['items'][0]['contact_campaign_statuses'], [status])
        row = person()
        row['contact_campaign_statuses'] = [status]
        client = self.client({'people': [row], 'total_entries': 1})
        self.assertNotIn('contact_campaign_statuses', client.people_search({})['items'][0])
        client = self.client(paged([{'id': 'sequence-1', 'contact_campaign_statuses': [status]}],
                                   key='emailer_campaigns'))
        self.assertNotIn('contact_campaign_statuses', client.sequences_search('')['items'][0])

    def test_missing_or_new_membership_state_is_not_inferred_as_active(self):
        client = self.client(paged([{'id': 'contact-1'}]))
        result = client.contacts_search('')
        self.assertNotIn('contact_campaign_statuses', result['items'][0])
        client = self.client(paged([{'id': 'contact-1', 'contact_campaign_statuses': [
            {'emailer_campaign_id': 'sequence-1', 'status': 'provider_new_state'}]}]))
        result = client.contacts_search('')
        self.assertEqual(result['items'][0]['contact_campaign_statuses'][0]['status'], 'provider_new_state')
        self.assertEqual(result['reason'], 'partial_response')
        self.assertFalse(result['complete'])

    def test_contact_linkedin_identity_accepts_only_https_profile_urls(self):
        for url in ('https://www.linkedin.com/in/synthetic-person/', 'https://linkedin.com/in/synthetic-person'):
            client = self.client(paged([{'id': 'contact-1', 'linkedin_url': url}]))
            self.assertEqual(client.contacts_search('')['items'][0]['linkedin_url'], url)
        for url in ('http://linkedin.com/in/person', 'https://linkedin.com.evil.invalid/in/person',
                    'https://user@linkedin.com/in/person', 'https://linkedin.com:443/in/person',
                    'https://linkedin.com/in/person?token=PRIVATE', 'https://linkedin.com/company/example',
                    'https://linkedin.com/in/../person', 'https://linkedin.com/in/%2fperson', 'javascript:alert(1)'):
            client = self.client(paged([{'id': 'contact-1', 'linkedin_url': url}]))
            self.assertNotIn('linkedin_url', client.contacts_search('')['items'][0])

    def test_only_observed_unique_team_sets_account_identity(self):
        client = self.client(paged([{'id': 'contact-1', 'team_id': 'observed-team'}]),
                             identity_reader=lambda: {'team_id': 'observed-team'})
        result = client.contacts_search('')
        self.assertEqual(result['items'][0]['team_id'], 'observed-team')
        self.assertEqual(result['source']['account_id'], 'observed-team')
        self.assertEqual(result['source']['identity_source'], 'provider_contact_team')
        self.assertFalse(result['source']['identity_verified'])
        data = paged([{'id': 'contact-1'}])
        data['team_id'] = 'observed-team'
        client = self.client(data)
        result = client.contacts_search('')
        self.assertEqual(result['source']['team_id'], 'observed-team')
        self.assertEqual(result['source']['account_id'], 'observed-team')
        self.assertEqual(result['source']['identity_source'], 'provider_response_team')
        self.assertFalse(result['source']['identity_verified'])

    def test_missing_mixed_invalid_or_mismatched_teams_remain_explicitly_unverified(self):
        for rows, reason in (([{'id': 'c1'}], 'provider_team_unavailable'),
                             ([{'id': 'c1', 'team_id': 'team-a'}, {'id': 'c2', 'team_id': 'team-b'}],
                              'provider_team_mixed'),
                             ([{'id': 'c1', 'team_id': 'bad/team'}], 'provider_team_invalid')):
            client = self.client(paged(rows), identity_reader=lambda: {'team_id': 'injected-team'})
            result = client.contacts_search('')
            self.assertIsNone(result['source']['account_id'])
            self.assertFalse(result['source']['identity_verified'])
            self.assertEqual(result['source']['identity_reason'], reason)
        client = self.client(paged([{'id': 'contact-1', 'team_id': 'actual-team'}]),
                             identity_reader=lambda: {'team_id': 'injected-team'})
        result = client.contacts_search('')
        self.assertEqual(result['source']['account_id'], 'actual-team')
        self.assertEqual(result['source']['injected_account_id'], 'injected-team')
        self.assertEqual(result['source']['identity_reason'], 'injected_team_mismatch')
        self.assertFalse(result['source']['identity_verified'])
        data = paged([{'id': 'contact-1', 'team_id': 'team-a'}])
        data['team_id'] = 'team-b'
        client = self.client(data)
        result = client.contacts_search('')
        self.assertIsNone(result['source']['account_id'])
        self.assertEqual(result['source']['identity_reason'], 'provider_team_mixed')
        self.assertFalse(result['complete'])

    def test_sequence_search_is_query_only_with_sharing_metadata(self):
        client = self.client(paged([{'id': 'sequence-1', 'name': 'Synthetic', 'active': False,
            'sharing_permission': {'visibility': 'restricted', 'access_type': None,
                                   'sharing_accesses': [{'token': 'PRIVATE'}]}}], key='emailer_campaigns'))
        result = client.sequences_search('Synthetic & safe')
        method, path, body, _, _ = self.calls[0]
        self.assertEqual((method, urlsplit(path).path), ('POST', '/api/v1/emailer_campaigns/search'))
        self.assertEqual(parse_qs(urlsplit(path).query),
                         {'q_name': ['Synthetic & safe'], 'page': ['1'], 'per_page': ['25']})
        self.assertIsNone(body)
        self.assertEqual(result['items'][0]['sharing_permission'],
                         {'visibility': 'restricted', 'access_type': None})
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_credit_usage_preserves_types_cycle_and_negative_consumed_without_summing(self):
        client = self.client({'credit_usage_stats': {
            'lead_credit': {'limit': 100, 'consumed': -10, 'left_over': 110},
            'dialer': {'limit': 50, 'consumed': 10, 'left_over': 40}},
            'current_credit_cycle': {'start_date': None, 'end_date': None}})
        result = client.credit_usage()
        self.assertEqual(self.calls[0][:3], ('POST', '/api/v1/usage_stats/credit_usage_stats', {}))
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['items'][0]['consumed'], -10)
        self.assertEqual(result['items'][1]['credit_type'], 'dialer')
        self.assertEqual(result['source']['current_credit_cycle'], {'start_date': None, 'end_date': None})
        self.assertEqual(result['credit_cost'], 0)
        self.assertTrue(result['complete'])

    def test_default_disabled_and_missing_loader_do_not_read_auth_or_transport(self):
        loader, identity, request = Mock(), Mock(), Mock()
        for client, reason in ((ApolloProspectReadonly(loader, identity_reader=identity, request=request),
                                'disabled'),
                               (ApolloProspectReadonly(request=request, enabled=True), 'configuration_pending')):
            for result in (client.people_search({}), client.contacts_search(''),
                           client.sequences_search(''), client.credit_usage()):
                self.assertEqual(result['reason'], reason)
                self.assertFalse(result['complete'])
                self.assertEqual(result['source']['request_count'], 0)
        for port in (loader, identity, request):
            port.assert_not_called()

    def test_unavailable_or_invalid_key_never_reaches_transport(self):
        for token in (None, '', 'bad key', 'bad\r\nheader', 'bad\x00key', 'x' * 4097, 123):
            request = Mock()
            result = ApolloProspectReadonly(lambda: token, enabled=True, request=request).people_search({})
            self.assertEqual(result['reason'], 'credential_unavailable')
            request.assert_not_called()
        request = Mock()
        result = ApolloProspectReadonly(Mock(side_effect=RuntimeError('PRIVATE')), enabled=True,
                                       request=request).credit_usage()
        self.assertEqual(result['reason'], 'credential_unavailable')
        request.assert_not_called()

    def test_identity_is_injected_context_not_assumed_campaign_or_verified_account(self):
        client = self.client({'people': [], 'total_entries': 0},
                             identity_reader=lambda: {'team_id': 'synthetic-team', 'token': 'PRIVATE'})
        result = client.people_search({})
        self.assertIsNone(result['source']['account_id'])
        self.assertEqual(result['source']['injected_account_id'], 'synthetic-team')
        self.assertEqual(result['source']['identity_source'], 'injected')
        self.assertFalse(result['source']['identity_verified'])
        self.assertEqual(result['source']['environment'], 'unverified')
        self.assertNotIn('PRIVATE', json.dumps(result))
        client = self.client({'people': [], 'total_entries': 0})
        self.assertIsNone(client.people_search({})['source']['account_id'])

    def test_identity_failure_is_closed_before_request(self):
        for identity in (None, {}, {'team_id': 'synthetic-secret'}, {'team_id': ['bad']}):
            client = self.client({}, identity_reader=lambda: identity)
            self.assertEqual(client.credit_usage()['reason'], 'identity_unavailable')
            self.assertEqual(self.calls, [])

    def test_page_and_row_window_limits_are_strict(self):
        for page, per_page in ((0, 25), (501, 1), (True, 25), ('1', 25), (1, True),
                               (1, 0), (1, 101), (1, '25'), (41, 25)):
            client = self.client({})
            result = client.people_search({}, page, per_page)
            self.assertEqual(result['reason'], 'invalid_pagination')
            self.assertEqual(self.calls, [])
        client = self.client({'people': [person(str(i)) for i in range(25)], 'total_entries': 50},
                             max_rows=10)
        result = client.people_search({})
        self.assertEqual(len(result['items']), 10)
        self.assertEqual(result['reason'], 'row_limit')
        self.assertIsNone(result['source']['next_page'])
        self.assertFalse(result['complete'])

    def test_constructor_limits_reject_bool_nonfinite_and_unbounded_values(self):
        for timeout in (True, 0, -1, 31, float('nan'), float('inf'), '15'):
            with self.assertRaisesRegex(ExternalEmailError, '^invalid_timeout$'):
                ApolloProspectReadonly(timeout=timeout)
        for max_rows in (True, 0, -1, 1001, '100'):
            with self.assertRaisesRegex(ExternalEmailError, '^invalid_max_rows$'):
                ApolloProspectReadonly(max_rows=max_rows)

    def test_unknown_and_paid_filter_fields_rejected_without_auth(self):
        for field in ('path', 'route', 'endpoint', 'url', 'api_key', 'reveal_personal_emails',
                      'reveal_phone_number', 'webhook_url', 'organization_industry_tag_ids',
                      'revenue_range', 'page', 'per_page', 'q_name'):
            loader, request = Mock(), Mock()
            result = ApolloProspectReadonly(loader, enabled=True, request=request).people_search({field: 'x'})
            self.assertEqual(result['reason'], 'invalid_filters')
            loader.assert_not_called()
            request.assert_not_called()

    def test_filters_validate_shape_size_and_literal_headcount_ranges(self):
        bad = (None, [], {'person_titles': 'CEO'}, {'person_titles': []},
               {'person_titles': ['CEO'] * 51}, {'person_locations': [None]},
               {'person_locations': ['\n']}, {'q_keywords': ''}, {'q_keywords': {}},
               {'q_keywords': 'x' * 501}, {'organization_num_employees_ranges': ['100-500']},
               {'organization_num_employees_ranges': ['500,100']},
               {'organization_num_employees_ranges': ['100, 500']})
        for filters in bad:
            client = self.client({})
            self.assertEqual(client.people_search(filters)['reason'], 'invalid_filters')
            self.assertEqual(self.calls, [])

    def test_queries_are_strings_never_arbitrary_field_mappings(self):
        for query in ({'path': '/people/match'}, ['text'], None, True, 'x' * 501, 'bad\x00', '\ud800'):
            client = self.client({})
            self.assertEqual(client.contacts_search(query)['reason'], 'invalid_query')
            self.assertEqual(client.sequences_search(query)['reason'], 'invalid_query')
            self.assertEqual(self.calls, [])

    def test_free_wire_boundary_blocks_every_other_method_and_endpoint(self):
        client = self.client({})
        routes = ('people/match', 'people/bulk_match', 'mixed_people/search',
                  'mixed_companies/search', 'organizations/enrich', 'organizations/bulk_enrich',
                  'contacts', 'contacts/bulk_create', 'emailer_campaigns',
                  'emailer_campaigns/seq/add_contact_ids', 'emailer_campaigns/seq/activate',
                  'emailer_messages/send', 'emailer_messages/search', 'users/register')
        for route in routes:
            with self.assertRaisesRegex(ExternalEmailError, '^endpoint_blocked$'):
                client._call('POST', '/api/v1/' + route, {}, {})
        for method in ('GET', 'PUT', 'PATCH', 'DELETE', 'HEAD'):
            with self.assertRaisesRegex(ExternalEmailError, '^endpoint_blocked$'):
                client._call(method, '/api/v1/contacts/search', {}, {})
        for path in ('https://other.invalid/api/v1/contacts/search',
                     '//other.invalid/api/v1/contacts/search', '/api/v1/contacts/search#fragment',
                     '/api/v1/contacts/search/../contacts', '/api/v1/contacts/search/'):
            with self.assertRaisesRegex(ExternalEmailError, '^endpoint_blocked$'):
                client._call('POST', path, {}, {})
        self.assertEqual(self.calls, [])

    def test_invalid_page_values_are_not_reflected_as_unbounded_output(self):
        client = self.client({})
        result = client.people_search({}, page=10**500, per_page=10**500)
        self.assertEqual(result['reason'], 'invalid_pagination')
        self.assertIsNone(result['page'])
        self.assertIsNone(result['source']['per_page'])
        self.assertLess(len(json.dumps(result)), 1000)
        self.assertEqual(self.calls, [])

    def test_free_paths_cannot_carry_write_or_paid_parameters(self):
        client = self.client({})
        bad = (('/api/v1/mixed_people/api_search?page=1&per_page=25&reveal_phone_number=true', None),
               ('/api/v1/mixed_people/api_search?page=1&per_page=25', {'route': 'people/match'}),
               ('/api/v1/contacts/search', {'q_keywords': '', 'page': 1, 'per_page': 25, 'api_key': 'x'}),
               ('/api/v1/emailer_campaigns/search?q_name=x&page=1&per_page=25&activate=true', None),
               ('/api/v1/usage_stats/credit_usage_stats?url=x', {}),
               ('/api/v1/usage_stats/credit_usage_stats', {'path': 'people/match'}))
        for path, body in bad:
            with self.assertRaises(ExternalEmailError):
                client._call('POST', path, body, {})
        self.assertEqual(self.calls, [])

    def test_http_failure_and_redirects_are_redacted_single_attempts(self):
        for status, reason in ((301, 'redirect_blocked'), (302, 'redirect_blocked'),
                               (307, 'redirect_blocked'), (308, 'redirect_blocked'),
                               (401, 'credential_rejected'), (403, 'permission_denied'),
                               (429, 'rate_limited'), (500, 'provider_http_error')):
            client = self.client({'token': 'synthetic-secret', 'message': 'PRIVATE'}, status=status)
            result = client.people_search({})
            self.assertEqual(result['reason'], reason)
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(result['source']['request_count'], 1)
            self.assertFalse(result['complete'])
            self.assertNotIn('PRIVATE', json.dumps(result))
            self.assertNotIn('synthetic-secret', json.dumps(result))

    def test_timeout_and_transport_failures_do_not_retry_or_expose_errors(self):
        for error, reason in ((TimeoutError('PRIVATE synthetic-secret'), 'timeout'),
                              (RuntimeError('PRIVATE synthetic-secret'), 'provider_unavailable'),
                              (ExternalEmailError('PRIVATE'), 'provider_unavailable')):
            request = Mock(side_effect=error)
            result = ApolloProspectReadonly(lambda: 'synthetic-secret', enabled=True,
                                           request=request).credit_usage()
            self.assertEqual(result['reason'], reason)
            self.assertEqual(request.call_count, 1)
            self.assertNotIn('PRIVATE', json.dumps(result))
        request = Mock(return_value=(200, b'{"people": [], "total_entries": 0}'))
        with patch('thunder_os.apollo_prospect_readonly.time.monotonic', side_effect=[0, 16]):
            result = ApolloProspectReadonly(lambda: 'fake', enabled=True, request=request).people_search({})
        self.assertEqual(result['reason'], 'timeout')

    def test_response_byte_shape_and_json_limits(self):
        for raw in (b'x' * 1_000_001, 'not bytes', b'[]', b'null', b'{', b'\xff',
                    b'{"error_details": {"message": "PRIVATE"}}', b'{}',
                    json.dumps({'people': [person()] * 26, 'total_entries': 26}).encode()):
            request = Mock(return_value=(200, raw))
            result = ApolloProspectReadonly(lambda: 'fake', enabled=True, request=request).people_search({})
            self.assertEqual(result['reason'], 'invalid_response')
            self.assertFalse(result['complete'])
            self.assertNotIn('PRIVATE', json.dumps(result))

    def test_shared_https_transport_bounds_read_does_not_redirect_and_closes(self):
        for status in (200, 302):
            response = Mock(status=status)
            response.read.return_value = b'{"people": [], "total_entries": 0}'
            connection = Mock()
            connection.getresponse.return_value = response
            with patch('thunder_os.apollo_readonly.http.client.HTTPSConnection', return_value=connection) as https:
                result = ApolloProspectReadonly(lambda: 'fake', enabled=True).people_search({})
            self.assertEqual(https.call_args.args, ('api.apollo.io', 443))
            self.assertEqual(https.call_args.kwargs['timeout'], 15)
            response.read.assert_called_once_with(1_000_001)
            connection.request.assert_called_once()
            connection.close.assert_called_once()
            self.assertEqual(result['reason'], None if status == 200 else 'redirect_blocked')

    def test_more_pages_and_terminal_slice_never_claim_whole_search_complete(self):
        client = self.client(paged([person('c1')], per_page=1, total=2))
        result = client.contacts_search('', per_page=1)
        self.assertEqual(result['reason'], 'more_pages')
        self.assertEqual(result['source']['next_page'], 2)
        self.assertFalse(result['complete'])
        client = self.client(paged([person('c2')], page=2, per_page=1, total=2))
        result = client.contacts_search('', page=2, per_page=1)
        self.assertEqual(result['reason'], 'page_slice')
        self.assertFalse(result['source']['has_more'])
        self.assertFalse(result['complete'])
        self.assertEqual(len(self.calls), 1)

    def test_duplicate_malformed_or_missing_rows_are_explicit_partial(self):
        for rows, total in (([person(), person()], 2), ([person(), {}], 2),
                            ([person()], 2), ([person(), 'bad'], 2)):
            client = self.client({'people': rows, 'total_entries': total})
            result = client.people_search({})
            self.assertEqual(result['reason'], 'partial_response')
            self.assertFalse(result['complete'])
            self.assertEqual(len(result['items']), 1)
            self.assertEqual(result['total'], total)

    def test_missing_or_inconsistent_pagination_is_not_success(self):
        for data in ({'contacts': [person()]},
                     paged([person()], page=2),
                     {'contacts': [person()], 'pagination': {'page': 1, 'per_page': 25,
                                                            'total_entries': True}},
                     {'people': [person()], 'total_entries': -1}):
            client = self.client(data)
            result = client.people_search({}) if 'people' in data else client.contacts_search('')
            self.assertEqual(result['reason'], 'pagination_unverified')
            self.assertFalse(result['complete'])
            self.assertEqual(len(result['items']), 1)

    def test_empty_search_is_complete_but_unknown_total_is_not_inferred(self):
        client = self.client({'people': [], 'total_entries': 0})
        self.assertTrue(client.people_search({})['complete'])
        client = self.client({'people': []})
        result = client.people_search({})
        self.assertIsNone(result['total'])
        self.assertFalse(result['complete'])

    def test_success_payload_cannot_echo_the_key_in_allowlisted_fields(self):
        row = person()
        row.update(first_name='echo synthetic-secret', title='synthetic-secret', api_key='synthetic-secret')
        row['organization']['name'] = 'synthetic-secret'
        client = self.client({'people': [row], 'total_entries': 1})
        result = client.people_search({})
        self.assertNotIn('synthetic-secret', json.dumps(result))
        self.assertNotIn('first_name', result['items'][0])

    def test_credit_usage_missing_and_unrecognized_data_are_explicit_partial(self):
        client = self.client({'credit_usage_stats': {
            'lead_credit': {'limit': 100, 'consumed': 1, 'left_over': 99},
            'unknown': {'token': 'PRIVATE'}},
            'current_credit_cycle': {'start_date': None, 'end_date': None}})
        result = client.credit_usage()
        self.assertEqual(result['reason'], 'partial_response')
        self.assertEqual(len(result['items']), 1)
        self.assertFalse(result['complete'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        client = self.client({'credit_usage_stats': {}})
        self.assertEqual(client.credit_usage()['reason'], 'invalid_response')

    def test_every_result_is_plain_json_with_stable_envelope_and_scope_metadata(self):
        for operation, data, scope in (
                ('people_search', {'people': [], 'total_entries': 0}, 'mixed_people_api_search'),
                ('contacts_search', paged([]), 'contacts_search'),
                ('sequences_search', paged([], key='emailer_campaigns'), 'emailer_campaigns_search')):
            client = self.client(data)
            result = getattr(client, operation)({} if operation == 'people_search' else '')
            self.assertEqual(set(result), {'status', 'items', 'total', 'page', 'complete',
                                          'reason', 'credit_cost', 'source'})
            self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)
            self.assertEqual(result['source']['required_scope'], scope)
            self.assertEqual(result['source']['api_version'], 'v1')
            self.assertEqual(result['source']['request_count'], 1)


if __name__ == '__main__':
    unittest.main()
