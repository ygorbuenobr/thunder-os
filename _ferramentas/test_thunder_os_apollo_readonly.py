"""Apollo synthetic HTTP contract tests; read ports only."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from thunder_os.apollo_readonly import ApolloReadOnly, contract_status, dry_run
from thunder_os.external_email_import import APOLLO_TEAM, APOLLO_SEQUENCE, ExternalEmailError


def contacts(count=1):
    return [{'id': 'contact-' + str(i), 'email': 'person' + str(i) + '@example.invalid',
             'contact_emails': [], 'contact_campaign_statuses': [{'emailer_campaign_id': APOLLO_SEQUENCE}]}
            for i in range(count)]


class ApolloReadOnlyTests(unittest.TestCase):
    def client(self, count=12, *, missing=None):
        calls = []
        def request(method, path, body, headers, timeout):
            calls.append((method, path, deepcopy(body)))
            if method == 'GET':
                query = parse_qs(urlsplit(path).query)
                self.assertEqual(urlsplit(path).path, '/api/v1/emailer_messages/search')
                self.assertEqual(query['emailer_campaign_ids[]'], [APOLLO_SEQUENCE])
                self.assertEqual(query['per_page'], ['100'])
                page = int(query['page'][0])
                result = {'pagination': {'page': str(page), 'per_page': '100',
                    'total_entries': count, 'total_pages': (count + 99) // 100},
                    'emailer_messages': [{'id': 'message-' + str(i), 'contact_id': 'contact-' + str(i),
                        'emailer_campaign_id': APOLLO_SEQUENCE, 'status': 'sent'}
                        for i in range((page-1)*100, min(page*100, count))]}
            else:
                self.assertEqual(path, '/api/v1/emailer_messages/get_content')
                self.assertEqual(body['body_format'], 'plain')
                self.assertLessEqual(len(body['ids']), 10)
                result = {'emailer_messages': [{'id': ident, 'status': 'completed', 'body_format': 'plain',
                    'sent_at': '2026-10-21T12:00:00Z', 'body': 'Synthetic text.', 'subject': 'Synthetic',
                    'recipients': [{'type': 'to', 'email': 'person' + ident.split('-')[1] + '@example.invalid'}]}
                    for ident in body['ids'] if ident != missing]}
            return 200, json.dumps(result).encode()
        return ApolloReadOnly(lambda: 'synthetic-key', identity_reader=lambda: {'team_id': APOLLO_TEAM},
                              request=request, enabled=True), calls

    def test_defaults_off(self):
        loader, identity, request = Mock(), Mock(), Mock()
        result = ApolloReadOnly(loader, identity_reader=identity, request=request).job([])
        self.assertEqual(result['status'], 'disabled')
        for port in (loader, identity, request):
            port.assert_not_called()
        self.assertFalse(contract_status()['writer_enabled'])

    def test_configuration_pending_does_not_discover_credentials(self):
        request = Mock()
        result = ApolloReadOnly(request=request, enabled=True).job([])
        self.assertEqual(result['status'], 'configuration_pending')
        request.assert_not_called()

    def test_job_wire_contract(self):
        client, calls = self.client()
        result = client.job(contacts(12))
        self.assertEqual(result['metadata_count'], 12)
        self.assertEqual(len(result['events']), 12)
        self.assertTrue(result['source_complete'])
        self.assertEqual(len(calls), 3)
        self.assertEqual([len(call[2]['ids']) for call in calls[1:]], [10, 2])
        self.assertEqual(result['writes'], 0)
        self.assertFalse(result['email_logged'])
        self.assertNotIn('sender', result['events'][0])

    def test_partial_content_is_explicit(self):
        client, _ = self.client(count=49, missing='message-48')
        result = client.job(contacts(49))
        self.assertEqual(len(result['events']), 48)
        self.assertFalse(result['source_complete'])
        self.assertEqual(result['missing_content_ids'], ['message-48'])
        self.assertEqual(result['metadata_count'], 49)
        self.assertEqual(result['email_dedupe'], 'unknown')

    def test_search_pagination(self):
        client, calls = self.client(count=101)
        result = client.job(contacts(101))
        self.assertEqual(len(result['events']), 101)
        self.assertEqual(len([call for call in calls if call[0] == 'GET']), 2)

    def test_bad_identity_and_pagination_block(self):
        request = Mock()
        client = ApolloReadOnly(lambda: 'fake-key', identity_reader=lambda: {'team_id': 'wrong'},
                                enabled=True, request=request)
        with self.assertRaisesRegex(ExternalEmailError, 'apollo_team_mismatch'):
            client.job(contacts())
        request.assert_not_called()
        for page, per_page in ((0, 10), (501, 10), (True, 10), (1, 101)):
            with self.subTest(page=page, per_page=per_page), self.assertRaises(ExternalEmailError):
                client.search(page=page, per_page=per_page)

    def test_content_limits_and_cross_campaign_fail_closed(self):
        client, _ = self.client()
        for ids in ([], ['x'] * 11, ['x', 'x']):
            with self.assertRaises(ExternalEmailError):
                client.content(ids)
        wrong = contacts()
        wrong[0]['contact_campaign_statuses'] = [{'emailer_campaign_id': 'other-campaign'}]
        with self.assertRaisesRegex(ExternalEmailError, 'contact_campaign_mismatch'):
            client.job(wrong)

    def test_network_failure_is_single_read_attempt_and_redacted(self):
        request = Mock(side_effect=TimeoutError('PRIVATE content/token'))
        client = ApolloReadOnly(lambda: 'fake-key', enabled=True, request=request)
        with self.assertRaisesRegex(ExternalEmailError, '^apollo_read_unavailable$'):
            client.search()
        self.assertEqual(request.call_count, 1)
        for status in (301, 401, 403, 429, 503):
            failed = Mock(return_value=(status, b'PRIVATE response'))
            client = ApolloReadOnly(lambda: 'fake-key', enabled=True, request=failed)
            with self.assertRaisesRegex(ExternalEmailError, '^apollo_http_error$'):
                client.search()
            self.assertEqual(failed.call_count, 1)

    def test_dry_run_has_no_io_and_optional_input(self):
        with patch('socket.socket', side_effect=AssertionError('network forbidden')):
            self.assertEqual(dry_run(), dry_run())
            result = dry_run(contacts()[0])
            self.assertTrue(result['structurally_valid'])
            self.assertTrue(result['needs_provider_readback'])
            self.assertFalse(result['provider_verified'])
            self.assertFalse(result['persisted'])
            self.assertFalse(dry_run({})['structurally_valid'])


if __name__ == '__main__':
    unittest.main()
