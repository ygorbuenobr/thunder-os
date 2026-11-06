import unittest

from thunder_os.apollo_linkedin_invitation import InvitationAdapter
from thunder_os.connectsafely_client import ConnectSafelyClient

ACCOUNT = 'a' * 24
RECIPIENT = 'urn:li:fsd_profile:SYNTHETIC'
PLAN = {'account_id': ACCOUNT, 'account_public_id': 'synthetic-sender', 'action': 'invite',
        'recipient_public_id': 'synthetic-person', 'recipient_profile_urn': RECIPIENT,
        'channel': 'linkedin', 'messaging_channel': 'linkedin_inbox', 'content': 'Olá'}


class FakeClient:
    def __init__(self):
        self.posts = 0
        self.rows = []
        self.complete = True
        self.status = 'NOT_CONNECTED'
        self.offsets = []

    def _plan(self, plan):
        if plan != PLAN:
            raise ValueError('invalid plan')

    def verify_identity(self):
        pass

    def relationship(self, plan):
        return {'account_id': ACCOUNT, 'recipient_public_id': plan['recipient_public_id'],
                'recipient_profile_urn': RECIPIENT, 'relationship_status': self.status}

    def sent_invitations(self, *, max_pages, start_index):
        self.offsets.append(start_index)
        return {'account_id': ACCOUNT, 'complete': self.complete, 'invitations': self.rows}

    def send(self, plan, *, before_dispatch):
        self.posts += 1
        return {'status': 'sent'}


class InvitationTests(unittest.TestCase):
    def test_unverified_freshness_blocks_without_post(self):
        client = FakeClient()
        result = InvitationAdapter(client).send(PLAN, 1000, before_dispatch=lambda: True)
        self.assertEqual(result, {'status': 'blocked', 'reason': 'provider_freshness_unverified'})
        self.assertEqual(client.posts, 0)

    def test_old_cached_invitation_cannot_turn_into_a_new_receipt(self):
        client = FakeClient()
        adapter = InvitationAdapter(client)
        first = adapter.send(PLAN, 1000, before_dispatch=lambda: True)
        client.rows = [{'invitationId': '123', 'profile': {'publicIdentifier': 'synthetic-person'},
                        'memberUrn': RECIPIENT, 'sentAt': '2 days ago'}]
        client.status = 'INVITATION_SENT'
        later = adapter.send(PLAN, 1000, before_dispatch=lambda: True)
        self.assertEqual(first['status'], 'blocked')
        self.assertEqual(later['status'], 'blocked')
        self.assertNotIn('receipt_ref', later)
        self.assertEqual(client.posts, 0)

    def test_incomplete_or_conflicting_observation_blocks_before_post(self):
        client = FakeClient()
        client.complete = False
        result = InvitationAdapter(client).send(PLAN, 1000, before_dispatch=lambda: True)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(client.posts, 0)
        client.complete = True
        client.rows = [{'invitationId': '123', 'profile': {'publicIdentifier': 'synthetic-person'},
                        'memberUrn': 'urn:li:fsd_profile:OTHER'}]
        result = InvitationAdapter(client).send(PLAN, 1000, before_dispatch=lambda: True)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(client.posts, 0)

    def test_pagination_and_budget(self):
        client = FakeClient()
        def pages(*, max_pages, start_index):
            client.offsets.append(start_index)
            if start_index == 0:
                return {'account_id': ACCOUNT, 'complete': False, 'next_start_index': 20, 'invitations': []}
            return {'account_id': ACCOUNT, 'complete': True, 'invitations': []}
        client.sent_invitations = pages
        self.assertEqual(InvitationAdapter(client).send(PLAN, 1000, before_dispatch=lambda: True)['status'], 'blocked')
        self.assertEqual(client.offsets, [0, 20])
        ticks = iter([100, 100, 121])
        adapter = InvitationAdapter(client, clock=lambda: next(ticks), time_budget=20)
        self.assertEqual(adapter.send(PLAN, 1000, before_dispatch=lambda: True)['status'], 'blocked')
        self.assertEqual(client.posts, 0)

    def test_reservation_is_required(self):
        client = FakeClient()
        self.assertEqual(InvitationAdapter(client).send(PLAN, 0, before_dispatch=lambda: True)['reason'],
                         'reservation_unverified')
        self.assertEqual(client.posts, 0)

    def test_actual_client_partial_documented_fields_fail_closed(self):
        calls = []
        def request(method, path, query, body):
            calls.append((method, path))
            if path == '/linkedin/accounts':
                return 200, [{'id': ACCOUNT, 'publicId': 'synthetic-sender', 'enabled': True, 'status': 'AVAILABLE'}]
            if '/relationship/' in path:
                return 200, {'accountId': ACCOUNT, 'profileUrn': RECIPIENT, 'status': 'NOT_CONNECTED',
                             'connected': False, 'invitationSent': False, 'invitationReceived': False}
            if path == '/linkedin/invitations/sent':
                return 200, {'success': True, 'accountId': ACCOUNT,
                             'invitations': [{'invitationId': '123', 'profile': {'publicIdentifier': 'synthetic-person'}}],
                             'count': 1, 'startIndex': 0, 'nextStartIndex': 1, 'endOfList': True}
            raise AssertionError('unexpected fake request')
        client = ConnectSafelyClient(ACCOUNT, 'synthetic-sender', request=request, clock=lambda: 1800000000)
        result = InvitationAdapter(client).send(PLAN, 1800000000, before_dispatch=lambda: True)
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse(any(method == 'POST' for method, _ in calls))


class ActualClientInvitationTests(unittest.TestCase):
    def setUp(self):
        self.posts = 0
        self.prior = []
        self.post_result = (200, {'success': True, 'profileUrn': RECIPIENT})
        self.after_result = None
        self.relationship_state = 'NOT_CONNECTED'
        self.client = ConnectSafelyClient(ACCOUNT, 'synthetic-sender', request=self.request,
                                          clock=lambda: 1800000000)
        self.adapter = InvitationAdapter(self.client)

    def request(self, method, path, query, body):
        if path == '/linkedin/accounts':
            return 200, [{'id': ACCOUNT, 'publicId': 'synthetic-sender',
                          'enabled': True, 'status': 'AVAILABLE'}]
        if '/relationship/' in path:
            state = self.relationship_state
            return 200, {'accountId': ACCOUNT, 'profileUrn': RECIPIENT, 'status': state,
                         'connected': state == 'CONNECTED', 'invitationSent': state == 'INVITATION_SENT',
                         'invitationReceived': state == 'INVITATION_RECEIVED'}
        if path == '/linkedin/invitations/sent':
            if self.posts and self.after_result is not None:
                return self.after_result
            rows = self.prior if not self.posts else [self.invitation()]
            return 200, {'success': True, 'accountId': ACCOUNT, 'invitations': rows,
                         'count': len(rows), 'startIndex': 0, 'nextStartIndex': len(rows), 'endOfList': True}
        if path.endswith('/quota'):
            return 200, {'accountId': ACCOUNT, 'quotas': [{'feature': 'CONNECT', 'window': 'weekly',
                'used': 0, 'limit': 200, 'remaining': 200, 'limitReached': False, 'resetAt': '2040-01-01T00:00:00Z'}]}
        if '/conversations/exists/' in path:
            return 200, {'success': True, 'accountId': ACCOUNT, 'profileUrn': RECIPIENT,
                         'hasConversation': False}
        if path == '/linkedin/connect' and method == 'POST':
            self.posts += 1
            if isinstance(self.post_result, Exception):
                raise self.post_result
            return self.post_result
        raise AssertionError('unexpected fake request')

    @staticmethod
    def invitation():
        return {'id': '123', 'invitationId': '123', 'invitationUrn': 'urn:li:invitation:123',
                'memberUrn': RECIPIENT, 'invitationType': 'SENT', 'sentAt': '1 minute ago',
                'profile': {'publicIdentifier': 'synthetic-person',
                            'profileUrl': 'https://www.linkedin.com/in/synthetic-person'}}

    def test_provider_confirmation_and_complete_directory_create_receipt(self):
        result = self.adapter.send(PLAN, 1800000000, before_dispatch=lambda: True)
        self.assertEqual(result['status'], 'sent')
        self.assertEqual(result['provider_id'], '123')
        self.assertFalse(result['accepted'])
        self.assertEqual(self.posts, 1)

    def test_missing_sync_confirmation_never_uses_matching_list_as_proof(self):
        for response in [TimeoutError(), (200, {'success': True, 'profileUrn': 'urn:li:fsd_profile:OTHER'}),
                         (200, {'success': False, 'profileUrn': RECIPIENT})]:
            with self.subTest(response=response):
                self.posts = 0
                self.post_result = response
                result = self.adapter.send(PLAN, 1800000000, before_dispatch=lambda: True)
                self.assertEqual(result['status'], 'uncertain')
                self.assertNotIn('receipt_ref', result)
                self.assertEqual(self.posts, 1)

    def test_previous_invitation_or_connection_never_dispatches(self):
        self.prior = [self.invitation()]
        self.assertEqual(self.adapter.send(PLAN, 1800000000, before_dispatch=lambda: True)['reason'], 'prior_invitation')
        self.assertEqual(self.posts, 0)
        self.prior = []
        self.relationship_state = 'CONNECTED'
        self.assertFalse(self.adapter.relationship(PLAN))
        self.assertEqual(self.adapter.send(PLAN, 1800000000, before_dispatch=lambda: True)['reason'], 'relationship_not_eligible')
        self.assertEqual(self.posts, 0)

    def test_502_or_missing_invitation_id_after_post_is_uncertain(self):
        for response in [(502, {}), (200, {'success': True, 'accountId': ACCOUNT, 'invitations': [],
                          'count': 0, 'startIndex': 0, 'nextStartIndex': 0, 'endOfList': True})]:
            with self.subTest(response=response):
                self.posts = 0
                self.after_result = response
                result = self.adapter.send(PLAN, 1800000000, before_dispatch=lambda: True)
                self.assertEqual(result['status'], 'uncertain')
                self.assertNotIn('receipt_ref', result)
                self.assertEqual(self.posts, 1)

    def test_provider_502_before_post_is_not_an_empty_directory(self):
        request = self.client.request
        def unavailable(method, path, query, body):
            if path == '/linkedin/invitations/sent':
                return 502, {'code': 'INVITATIONS_UNAVAILABLE'}
            return request(method, path, query, body)
        self.client.request = unavailable
        result = self.adapter.send(PLAN, 1800000000, before_dispatch=lambda: True)
        self.assertEqual(result, {'status': 'blocked', 'reason': 'invitation_history_unverified'})
        self.assertEqual(self.posts, 0)

    def test_gate_rechecked_immediately_after_provider_reads(self):
        calls = []
        def gate():
            calls.append(1)
            return len(calls) == 1
        self.assertEqual(self.adapter.send(PLAN, 1800000000, before_dispatch=gate)['status'], 'blocked')
        self.assertEqual(self.posts, 0)

    def test_original_plan_mutation_does_not_change_dispatch_target(self):
        plan = dict(PLAN)
        def gate():
            plan['recipient_profile_urn'] = 'urn:li:fsd_profile:OTHER'
            return True
        result = self.adapter.send(plan, 1800000000, before_dispatch=gate)
        self.assertEqual(result['status'], 'sent')
        self.assertEqual(self.posts, 1)


if __name__ == '__main__':
    unittest.main()
