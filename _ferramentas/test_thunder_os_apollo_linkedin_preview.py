import json
import hashlib
import unittest

from thunder_os.apollo_linkedin_preview import ApolloLinkedInPreview, _https_request


class PreviewTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.task = {'id': 'task1', 'type': 'linkedin_step_connect', 'status': 'scheduled',
                     'user_id': 'owner1', 'contact': {'id': 'contact1', 'name': 'Test',
                     'linkedin_url': 'https://www.linkedin.com/in/recipient/'},
                     'emailer_campaign_id': 'sequence1', 'contact_id': 'contact1',
                     'rendered_content': ''}

    def request(self, method, path, body, headers, timeout):
        self.calls.append((method, path, body))
        data = {'user': {'id': 'owner1', 'team_id': 'team1'}} if path.endswith('api_profile') else {'task': self.task}
        return 200, json.dumps(data).encode()

    def preview(self, *, account_status=None, **kwargs):
        return ApolloLinkedInPreview(lambda: 'test-token', team_id='team1', sequence_id='sequence1',
            task_id='task1', account_id='account1', account_public_id='sender', api_actor_id='owner1', owner_id='owner1',
            account_status_reader=account_status or (lambda: {'id': 'account1', 'publicId': 'sender',
                'available': True}), request=self.request, **kwargs)

    def test_read_is_only_get_exact_scoped_routes(self):
        result = self.preview().read()
        self.assertEqual(self.calls, [('GET', '/api/v1/users/api_profile', None),
                                      ('GET', '/api/v1/tasks/task1', None)])
        self.assertFalse(result['execution_enabled'])
        self.assertTrue(result['dry_run'])
        self.assertEqual(result['task']['sequence_id'], 'sequence1')

    def test_missing_owner_and_text_are_not_inferred(self):
        del self.task['user_id']
        del self.task['rendered_content']
        result = self.preview().read()
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('owner_unverified', result['blockers'])
        self.assertIn('text_missing', result['blockers'])
        self.assertIsNone(result['text']['value'])
        self.assertIsNone(result['call_preview'])

    def test_self_invitation_is_blocked(self):
        self.task['contact']['linkedin_url'] = 'https://www.linkedin.com/in/Sender/'
        result = self.preview().read()
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('self_invitation', result['blockers'])
        self.assertIsNone(result['call_preview'])

    def test_sequence_account_and_task_mismatches_fail_closed(self):
        self.task['emailer_campaign_id'] = 'different'
        result = self.preview().read()
        self.assertEqual(result['status'], 'unavailable')
        self.assertIsNone(result['call_preview'])
        self.task['emailer_campaign_id'] = 'sequence1'
        result = self.preview(account_status=lambda: {'id':'another','publicId':'sender','available':True}).read()
        self.assertIn('account_unverified', result['blockers'])

    def test_closed_task_does_not_generate_call(self):
        self.task['status'] = 'completed'
        result = self.preview().read()
        self.assertIn('task_not_open', result['blockers'])
        self.assertIsNone(result['call_preview'])

    def test_malformed_and_transport_failure_hide_private_data(self):
        def bad(*args):
            raise RuntimeError('secret-provider-error')
        preview = self.preview()
        preview._request = bad
        result = preview.read()
        self.assertEqual(result['status'], 'unavailable')
        self.assertNotIn('secret-provider-error', repr(result))

    def test_invalid_task_id_never_calls_provider(self):
        with self.assertRaises(ValueError):
            ApolloLinkedInPreview(lambda:'x', team_id='team1', sequence_id='sequence1',
                task_id='../complete', account_id='account1', account_public_id='sender',
                api_actor_id='owner1', owner_id='owner1', account_status_reader=lambda:{}, request=self.request)
        self.assertEqual(self.calls, [])

    def test_transport_refuses_effect_routes_before_network(self):
        for method, path in [('POST', '/api/v1/tasks/task1/complete'),
                             ('POST', '/api/v1/tasks/search'),
                             ('GET', '/api/v1/tasks/task1/complete')]:
            with self.subTest(method=method, path=path), self.assertRaises(ValueError):
                _https_request(method, path, None, {}, 1)

    def test_unverified_rendered_text_never_claimed_as_exact(self):
        result = self.preview().read()
        self.assertEqual(result['text'], {'value': None, 'verified': False, 'hash': None})
        self.assertIn('text_missing', result['blockers'])

    def test_exact_rendered_text_hash_and_non_executable_call(self):
        self.task['linkedin_emailer_template'] = {'body_text': ' Olá, Ygor! '}
        result = self.preview().read()
        self.assertEqual(result['text'], {'value': ' Olá, Ygor! ', 'verified': True,
            'hash': hashlib.sha256(' Olá, Ygor! '.encode('utf-8')).hexdigest()})
        self.assertEqual(result['call_preview']['body']['customMessage'], ' Olá, Ygor! ')
        self.assertFalse(result['call_preview']['executable'])

    def test_text_validation_and_action_limits(self):
        for value, reason in [(None, 'text_missing'), (123, 'text_missing'),
                              (' \n ', 'text_empty'), ('Hi {{first_name}}', 'text_unresolved_variables'),
                              ('Hi }}', 'text_unresolved_variables'), ('x' * 301, 'text_too_long')]:
            with self.subTest(value=value):
                self.task['linkedin_emailer_template'] = {'body_text': value}
                result = self.preview().read()
                self.assertIn(reason, result['blockers'])
                self.assertIsNone(result['text']['hash'])
                self.assertFalse(result['call_preview']['executable'])
        self.task['linkedin_emailer_template'] = {'body_text': 'abcd'}
        self.assertIn('text_too_long', self.preview(invite_text_limit=3).read()['blockers'])
        self.task['type'] = 'linkedin_step_message'
        self.assertEqual(self.preview(message_text_limit=4).read()['text']['value'], 'abcd')
        self.assertIn('text_too_long', self.preview(message_text_limit=3).read()['blockers'])

    def test_removed_task_http_status_is_explicit(self):
        for status in (404, 422):
            with self.subTest(status=status):
                preview = self.preview()
                preview._request = lambda method, path, body, headers, timeout: (
                    (200, json.dumps({'user': {'id': 'owner1', 'team_id': 'team1'}}).encode())
                    if path.endswith('api_profile') else (status, b'{}'))
                result = preview.read()
                self.assertEqual(result['blockers'], ['task_removed'])
                self.assertIsNone(result['call_preview'])

    def test_rest_task_binds_campaign_contact_and_profile_user(self):
        self.task['emailer_campaign_id'] = 'sequence1'
        self.task['contact_id'] = 'contact1'
        def request(method, path, body, headers, timeout):
            payload = {'user': {'id': 'owner1', 'team_id': 'team1'}} if path.endswith('api_profile') else {'task': self.task}
            return 200, json.dumps(payload).encode()
        preview = self.preview()
        preview._request = request
        result = preview.read()
        self.assertEqual(result['task']['sequence_id'], 'sequence1')
        self.assertIsNone(result['task']['step_position'])
        self.assertEqual(result['lead']['id'], 'contact1')

    def test_rest_contact_and_profile_identity_mismatch_fail_closed(self):
        self.task.update(emailer_campaign_id='sequence1', contact_id='different')
        preview = self.preview()
        def request(method, path, body, headers, timeout):
            payload = {'user': {'id': 'owner1', 'team_id': 'team1'}} if path.endswith('api_profile') else {'task': self.task}
            return 200, json.dumps(payload).encode()
        preview._request = request
        self.assertEqual(preview.read()['status'], 'unavailable')
        self.task['contact_id'] = 'contact1'
        def wrong_profile(method, path, body, headers, timeout):
            payload = {'user': {'id': 'other', 'team_id': 'team1'}} if path.endswith('api_profile') else {'task': self.task}
            return 200, json.dumps(payload).encode()
        preview._request = wrong_profile
        self.assertEqual(preview.read()['status'], 'unavailable')

    def test_connect_preview_is_incomplete_and_never_executes(self):
        result = self.preview().read()
        call = result['call_preview']
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(call['method'], 'POST')
        self.assertEqual(call['path'], '/linkedin/connect')
        self.assertEqual(call['body'], {'accountId': 'account1', 'profileId': 'recipient',
                                        'profileUrn': None, 'customMessage': None})
        self.assertTrue(call['incomplete'])
        self.assertFalse(call['executable'])
        self.assertTrue(all(call[0] == 'GET' for call in self.calls))

    def test_message_preview_does_not_invent_text_or_urn(self):
        self.task['type'] = 'linkedin_step_message'
        result = self.preview().read()
        call = result['call_preview']
        self.assertEqual(call['path'], '/linkedin/conversations/send')
        self.assertEqual(call['body'], {'accountId':'account1','recipientProfileId':'recipient',
            'recipientProfileUrn':None,'messagingChannel':'linkedin_inbox','message':None})
        self.assertTrue(call['incomplete'])
        self.assertFalse(call['executable'])

    def test_self_invitation_has_no_call_preview(self):
        self.task['contact']['linkedin_url'] = 'https://www.linkedin.com/in/sender/'
        result = self.preview().read()
        self.assertIn('self_invitation', result['blockers'])
        self.assertIsNone(result['call_preview'])

    def test_apollo_http_profile_is_canonical_https_and_self_invite_blocked(self):
        for host in ('linkedin.com', 'www.linkedin.com'):
            with self.subTest(host=host):
                self.task['contact']['linkedin_url'] = f'http://{host}/in/Sender/'
                result = self.preview().read()
                self.assertEqual(result['lead']['linkedin_url'], 'https://www.linkedin.com/in/sender/')
                self.assertIn('self_invitation', result['blockers'])
                self.assertIsNone(result['call_preview'])

    def test_apollo_http_invalid_host_port_or_extra_parts_blocked(self):
        for url in ('http://linkedin.com.evil.test/in/sender/',
                    'http://linkedin.com:80/in/sender/',
                    'http://user@linkedin.com/in/sender/',
                    'http://linkedin.com/in/sender/?x=1',
                    'http://linkedin.com/in/sender/#fragment'):
            with self.subTest(url=url):
                self.task['contact']['linkedin_url'] = url
                result = self.preview().read()
                self.assertIsNone(result['lead']['linkedin_url'])
                self.assertIn('profile_unverified', result['blockers'])
                self.assertIsNone(result['call_preview'])

    def test_percent_encoded_self_slug_is_rejected_without_preview(self):
        self.task['contact']['linkedin_url'] = 'http://linkedin.com/in/%73ender/'
        result = self.preview().read()
        self.assertEqual(result['status'], 'blocked')
        self.assertIsNone(result['lead']['linkedin_url'])
        self.assertIn('profile_unverified', result['blockers'])
        self.assertIsNone(result['call_preview'])

    def test_api_actor_can_differ_from_task_owner(self):
        preview = ApolloLinkedInPreview(lambda:'test-token', team_id='team1', sequence_id='sequence1',
            task_id='task1', account_id='account1', account_public_id='sender',
            api_actor_id='actor1', owner_id='owner1', account_status_reader=lambda:{},
            request=lambda method, path, body, headers, timeout: (200, json.dumps(
                {'user': {'id':'actor1','team_id':'team1'}} if path.endswith('api_profile')
                else {'task':self.task}).encode()))
        result = preview.read()
        self.assertEqual(result['status'], 'blocked')
        self.assertNotIn('owner_unverified', result['blockers'])

    def test_wrong_task_owner_blocks_with_separate_actor(self):
        self.task['user_id'] = 'other'
        preview = ApolloLinkedInPreview(lambda:'test-token', team_id='team1', sequence_id='sequence1',
            task_id='task1', account_id='account1', account_public_id='sender',
            api_actor_id='actor1', owner_id='owner1', account_status_reader=lambda:{},
            request=lambda method, path, body, headers, timeout: (200, json.dumps(
                {'user': {'id':'actor1','team_id':'team1'}} if path.endswith('api_profile')
                else {'task':self.task}).encode()))
        result = preview.read()
        self.assertIn('owner_unverified', result['blockers'])
        self.assertIsNone(result['call_preview'])

    def test_wrong_api_actor_is_rejected_before_task_read(self):
        calls = []
        def request(method, path, body, headers, timeout):
            calls.append(path)
            return 200, json.dumps({'user': {'id':'other','team_id':'team1'}}).encode()
        preview = ApolloLinkedInPreview(lambda:'test-token', team_id='team1', sequence_id='sequence1',
            task_id='task1', account_id='account1', account_public_id='sender', api_actor_id='actor1',
            owner_id='owner1', account_status_reader=lambda:{}, request=request)
        result = preview.read()
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(calls, ['/api/v1/users/api_profile'])


    def test_wrong_team_is_rejected_before_task_read(self):
        calls = []
        def request(method, path, body, headers, timeout):
            calls.append(path)
            return 200, json.dumps({'user': {'id': 'actor1', 'team_id': 'other'}}).encode()
        preview = ApolloLinkedInPreview(lambda:'test-token', team_id='team1', sequence_id='sequence1',
            task_id='task1', account_id='account1', account_public_id='sender', api_actor_id='actor1',
            owner_id='owner1', account_status_reader=lambda:{}, request=request)
        self.assertEqual(preview.read()['status'], 'unavailable')
        self.assertEqual(calls, ['/api/v1/users/api_profile'])


if __name__ == '__main__':
    unittest.main()
