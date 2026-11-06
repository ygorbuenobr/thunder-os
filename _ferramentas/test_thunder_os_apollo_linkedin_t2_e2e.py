"""Offline invitation journey with real coordinator, client and durable receipt."""
from unittest.mock import patch
import json
import unittest

import test_thunder_os_apollo_linkedin_t4d_e2e as base


class InvitationScenario(base.Scenario):
    def __init__(self):
        super().__init__()
        self.mode = 'success'
        self.invitation_reads = 0
        self.sent = False
        self.pause = lambda: None

    def task(self):
        value = super().task()
        value['type'] = 'linkedin_step_connect'
        return value

    def linkedin(self, method, path, query=None, body=None):
        if path.endswith('/accounts') or '/exists/' in path:
            return super().linkedin(method, path, query, body)
        self.linkedin_calls.append((method, path, body))
        if '/relationship/' in path:
            return 200, {'accountId': base.ACCOUNT, 'profileUrn': base.RECIPIENT,
                         'status': 'NOT_CONNECTED', 'connected': False,
                         'invitationSent': False, 'invitationReceived': False}
        if path.endswith('/quota'):
            return 200, {'accountId': base.ACCOUNT, 'quotas': [
                {'feature': 'CONNECT', 'window': 'weekly', 'used': 0, 'limit': 200,
                 'remaining': 200, 'limitReached': False, 'resetAt': '2099-01-01T00:00:00Z'}]}
        if path.endswith('/invitations/sent'):
            self.invitation_reads += 1
            if self.mode == 'pause' and self.invitation_reads == 2:
                self.pause()
            if self.sent and self.mode == '502':
                return 502, {'success': False, 'error': 'INVITATIONS_UNAVAILABLE'}
            if self.sent and self.mode == 'partial':
                return 200, {'success': True, 'accountId': base.ACCOUNT, 'invitations': [],
                             'count': 0, 'startIndex': 0, 'nextStartIndex': 0, 'endOfList': False}
            rows = []
            if self.sent or self.mode == 'preexisting':
                rows = [{'id': '123', 'invitationId': '123',
                         'invitationUrn': 'urn:li:invitation:123', 'memberUrn': base.RECIPIENT,
                         'invitationType': 'SENT', 'sentAt': '1 minute ago',
                         'profile': {'publicIdentifier': 'fixture-person',
                                     'profileUrl': 'https://www.linkedin.com/in/fixture-person'}}]
            return 200, {'success': True, 'accountId': base.ACCOUNT, 'invitations': rows,
                         'count': len(rows), 'startIndex': 0, 'nextStartIndex': len(rows),
                         'endOfList': True}
        if method == 'POST' and path.endswith('/connect'):
            self.sent = True
            if self.mode == 'timeout':
                raise TimeoutError('synthetic timeout')
            return 200, {'success': True, 'profileUrn': base.RECIPIENT}
        raise AssertionError('unexpected fake request: ' + path)


class InvitationEndToEnd(unittest.TestCase):
    def setUp(self):
        original_issue = base.issue.issue
        def issue_with_invite_target(*args, **kwargs):
            original_resolver = kwargs['resolver']
            def resolver(request):
                result = original_resolver(request)
                result['target']['action'] = request['action']
                return result
            kwargs['resolver'] = resolver
            return original_issue(*args, **kwargs)
        with patch.object(base, 'Scenario', InvitationScenario), \
             patch.object(base.issue, 'issue', side_effect=issue_with_invite_target):
            base.T4dEndToEnd.setUp(self)
        def receipt_before_completion():
            row = self.repo.get_apollo_linkedin_reservation('company', 'fixture-task', 'linkedin_step_connect')
            self.assertEqual(row['state'], 'confirmed')
            self.assertEqual(row['receipt']['provider_id'], 'connectsafely:invitation-id:123')
        self.fake.on_complete = receipt_before_completion
        self.pause_observed = False
        self.fake.pause = lambda: setattr(self, 'pause_observed', True)

    run_executor = base.T4dEndToEnd.run_executor
    posts = base.T4dEndToEnd.posts

    def test_confirmed_receipt_is_durable_before_apollo_completion_and_no_retry(self):
        code, result = self.run_executor()
        self.assertEqual((code, result['state']), (0, 'confirmed'))
        row = self.repo.get_apollo_linkedin_reservation('company', 'fixture-task', 'linkedin_step_connect')
        self.assertEqual(row['apollo_status'], 'confirmed')
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(sum(call[0] == 'POST' for call in self.fake.apollo_calls), 1)
        self.run_executor()
        self.assertEqual(len(self.posts()), 1)

    def test_unverified_after_dispatch_never_retries_or_completes(self):
        for mode in ('timeout', '502', 'partial'):
            with self.subTest(mode=mode):
                # Each scenario owns a distinct database and authorization.
                case = type(self)(methodName='runTest')
                case.setUp()
                try:
                    case.fake.mode = mode
                    _, result = case.run_executor()
                    self.assertEqual(result['state'], 'uncertain')
                    self.assertEqual(len(case.posts()), 1)
                    self.assertEqual(sum(call[0] == 'POST' for call in case.fake.apollo_calls), 0)
                    case.run_executor()
                    self.assertEqual(len(case.posts()), 1)
                finally:
                    case.doCleanups()

    def test_preexisting_invitation_does_not_send(self):
        self.fake.mode = 'preexisting'
        _, result = self.run_executor()
        self.assertNotEqual(result['state'], 'confirmed')
        self.assertEqual(self.posts(), [])
        self.assertEqual(sum(call[0] == 'POST' for call in self.fake.apollo_calls), 0)

    def test_pause_during_gate_prevents_post(self):
        self.fake.mode = 'pause'
        original = base.Repository.get_state
        def controlled_read(repo, namespace, key):
            if namespace == 'control' and key == 'global' and self.pause_observed:
                return {'paused': True, 'actor': 'ygor'}
            return original(repo, namespace, key)
        with patch.object(base.Repository, 'get_state', controlled_read):
            _, result = self.run_executor()
        self.assertTrue(self.pause_observed)
        self.assertNotEqual(result['state'], 'confirmed')
        self.assertEqual(self.posts(), [])
        self.assertEqual(sum(call[0] == 'POST' for call in self.fake.apollo_calls), 0)


if __name__ == '__main__':
    unittest.main()
