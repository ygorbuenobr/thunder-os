import unittest

from thunder_os.apollo_linkedin_message import MessageAdapter
from thunder_os.apollo_linkedin_reply_coverage import ReplyCoverage

PLAN = {'account_id':'account', 'account_public_id':'owner', 'action':'dm',
        'recipient_public_id':'lead', 'recipient_profile_urn':'urn:li:fsd_profile:lead',
        'channel':'linkedin', 'messaging_channel':'linkedin_inbox', 'content':'Hello', 'conversation_urn':None}


class FakeClient:
    account_id, expected_public_id, self_profile_id = 'account', 'owner', 'urn:li:fsd_profile:owner'

    def __init__(self):
        self.status, self.calls = 'CONNECTED', []
        self.sent = {'status':'sent','receipt_ref':'connectsafely:message'}
        self.readback = {'status':'uncertain'}
        self.pages = [{'success':True, 'accountId':'account', 'conversationUrn':'urn:li:msg_conversation:thread',
                       'hasMore':False, 'participants':[{'profileId':'owner'}, {'profileId':'lead'}], 'messages':[]}]

    def _plan(self, plan):
        if plan != PLAN: raise ValueError('invalid')
    def verify_identity(self): pass
    def _profile_identity(self, value): return value.split(':')[-1] if value else None
    def _sender_identity(self, row): return self._profile_identity(row.get('senderProfileId'))
    def _seconds(self, n): return n / 1000 if n > 100_000_000_000 else n
    def relationship(self, plan):
        return {'account_id':'account','recipient_public_id':'lead',
                'recipient_profile_urn':PLAN['recipient_profile_urn'],'relationship_status':self.status}
    def send(self, plan, *, before_dispatch):
        if before_dispatch() is not True: raise ValueError('denied')
        self.calls.append('POST')
        return self.sent
    def reconcile(self, plan, attempted_at):
        self.calls.append(('GET', attempted_at))
        return self.readback
    def _call(self, method, path, query):
        self.calls.append((method, path, query))
        if path.startswith('/linkedin/conversations/exists/'):
            return {'success':True,'accountId':'account','profileUrn':PLAN['recipient_profile_urn'],
                    'hasConversation':True,'conversationUrn':'urn:li:msg_conversation:thread'}
        return self.pages.pop(0)


class MessageAdapterTests(unittest.TestCase):
    def setUp(self): self.client = FakeClient(); self.adapter = MessageAdapter(self.client)

    def test_message_receipt_and_no_retry(self):
        result = self.adapter.send(PLAN, 100, before_dispatch=lambda: True)
        self.assertEqual(result['receipt_ref'], 'connectsafely:message')
        self.assertEqual(self.client.calls, ['POST'])

    def test_readback_or_uncertainty_never_repeats_post(self):
        self.client.sent = {'status':'uncertain'}
        self.client.readback = {'status':'sent', 'receipt_ref':'connectsafely:readback'}
        result = self.adapter.send(PLAN, 100, before_dispatch=lambda: True)
        self.assertEqual(result['receipt_ref'], 'connectsafely:readback')
        self.assertGreater(self.client.calls[1][1], 100)
        self.client.calls = []; self.client.readback = {'status':'uncertain'}
        self.assertEqual(self.adapter.send(PLAN, 100, before_dispatch=lambda: True)['status'], 'uncertain')
        self.assertEqual(self.client.calls.count('POST'), 1)

    def test_no_connection_or_pause_prevents_post(self):
        self.client.status = 'NOT_CONNECTED'
        self.assertEqual(self.adapter.send(PLAN, 100, before_dispatch=lambda:True)['status'], 'blocked')
        self.client.status = 'CONNECTED'
        self.assertEqual(self.adapter.send(PLAN, 100, before_dispatch=lambda:False)['status'], 'blocked')
        self.assertNotIn('POST', self.client.calls)

    def test_reply_since_last_touch_and_unknown_coverage(self):
        def row(stamp):
            return {'messageUrn':'urn:li:messagingMessage:1','senderProfileId':'lead',
                    'isSentByOwner':False,'sentAt':stamp}
        read = self.adapter.reply_source(PLAN, 100)
        self.client.pages[0]['messages'] = [row(101)]
        self.assertTrue(read(PLAN['recipient_profile_urn'])['suppressed'])
        self.client.pages = [FakeClient().pages[0]]; self.client.pages[0]['messages'] = [row(99)]
        self.assertFalse(read(PLAN['recipient_profile_urn'])['suppressed'])
        self.client.pages = [FakeClient().pages[0]]; self.client.pages[0]['messages'] = [row(None)]
        self.assertFalse(read(PLAN['recipient_profile_urn'])['complete'])

    def test_reply_source_reads_invite_plan_with_empty_note_without_sending(self):
        plan = {**PLAN, 'action': 'invite', 'content': ''}
        self.client._plan = lambda candidate: None if candidate == plan else (_ for _ in ()).throw(ValueError('invalid'))
        self.client.pages[0]['messages'] = [{'messageUrn': 'urn:li:messagingMessage:sample',
            'senderProfileId': 'lead', 'isSentByOwner': False, 'sentAt': 101}]
        result = self.adapter.reply_source(plan, 100)(plan['recipient_profile_urn'])
        self.assertTrue(result['suppressed'])
        self.assertEqual(result['reply']['recipient_profile_urn'], plan['recipient_profile_urn'])
        self.assertTrue(all(call[0] == 'GET' for call in self.client.calls))

    def test_invite_reply_source_refuses_different_profile(self):
        plan = {**PLAN, 'action': 'invite', 'content': ''}
        self.client._plan = lambda candidate: None if candidate == plan else (_ for _ in ()).throw(ValueError('invalid'))
        result = self.adapter.reply_source(plan, 100)('urn:li:fsd_profile:other')
        self.assertFalse(result['complete'])
        self.assertFalse(result['suppressed'])
        self.assertEqual(self.client.calls, [])

    def test_inbound_evidence_preserves_provider_identity_content_and_time(self):
        self.client.pages[0]['messages'] = [{'messageUrn': 'urn:li:messagingMessage:sample',
            'senderProfileId': 'lead', 'isSentByOwner': False, 'sentAt': 101,
            'text': 'Synthetic reply'}]
        result = self.adapter.reply_source(PLAN, 100)(PLAN['recipient_profile_urn'])
        self.assertTrue(result['suppressed'])
        self.assertTrue(result['positive_evidence_verified'])
        self.assertFalse(result['history_complete'])
        self.assertEqual(result['reply'], {'provider': 'connectsafely', 'account_id': 'account',
            'recipient_profile_urn': PLAN['recipient_profile_urn'], 'recipient_public_id': 'lead',
            'conversation_urn': 'urn:li:msg_conversation:thread',
            'provider_message_id': 'urn:li:messagingMessage:sample',
            'received_at': '1970-01-01T00:01:41+00:00', 'content': 'Synthetic reply'})

    def test_inbound_without_text_still_blocks_but_does_not_fabricate_content(self):
        self.client.pages[0]['messages'] = [{'messageUrn': 'urn:li:messagingMessage:sample',
            'senderProfileId': 'lead', 'isSentByOwner': False, 'sentAt': 101}]
        result = self.adapter.reply_source(PLAN, 100)(PLAN['recipient_profile_urn'])
        self.assertTrue(result['suppressed'])
        self.assertIsNone(result['reply']['content'])

    def test_inbound_with_unverified_message_identifier_still_blocks(self):
        self.client.pages[0]['messages'] = [{'messageUrn': 'invalid-id',
            'senderProfileId': 'lead', 'isSentByOwner': False, 'sentAt': 101, 'text': 'Synthetic'}]
        result = self.adapter.reply_source(PLAN, 100)(PLAN['recipient_profile_urn'])
        self.assertTrue(result['suppressed'])
        self.assertIsNone(result['reply']['provider_message_id'])

    def test_partial_history_and_wrong_participant_are_unknown(self):
        read = self.adapter.reply_source(PLAN, 100)
        self.client.pages[0]['participants'] = [{'profileId':'owner'}, {'profileId':'other'}]
        self.assertFalse(read(PLAN['recipient_profile_urn'])['complete'])
        self.client.pages = [FakeClient().pages[0]]; self.client.pages[0]['hasMore'] = True
        self.assertFalse(read(PLAN['recipient_profile_urn'])['complete'])

    def test_real_provider_shape_empty_participants_and_tail_page(self):
        # Shape observed on 06/11/2026: participants=[] and an empty tail page.
        def row(urn, sender, stamp):
            return {'messageUrn': urn, 'senderProfileId': sender, 'isSentByOwner': sender == 'owner', 'sentAt': stamp}
        first = {'success': True, 'accountId': 'account', 'conversationUrn': 'urn:li:msg_conversation:thread',
                 'hasMore': True, 'nextCursor': '50000', 'fetchingMore': False, 'participants': [],
                 'messages': [row('m2', 'owner', 90), row('m1', 'owner', 50)]}
        tail = {'success': True, 'conversationUrn': 'urn:li:msg_conversation:thread', 'hasMore': True,
                'nextCursor': None, 'fetchingMore': False, 'participants': [], 'messages': []}
        self.client.pages = [first, tail]
        result = self.adapter.reply_source(PLAN, 100)(PLAN['recipient_profile_urn'])
        self.assertTrue(result['complete'])
        self.assertFalse(result['suppressed'])
        # A lead message after the last touch on the first page is still a reply.
        self.client.pages = [dict(first, messages=[row('m3', 'lead', 120), row('m1', 'owner', 50)]), dict(tail)]
        self.assertTrue(self.adapter.reply_source(PLAN, 100)(PLAN['recipient_profile_urn'])['suppressed'])
        # The tail without a prior cursor is not proof of completeness.
        self.client.pages = [dict(tail, conversationUrn='urn:li:msg_conversation:thread', accountId='account')]
        self.assertFalse(self.adapter.reply_source(PLAN, 100)(PLAN['recipient_profile_urn'])['complete'])

    def test_reply_source_composes_with_apollo_coverage(self):
        class Apollo:
            def contacts(self, email):
                return [{'id':'contact', 'email_replied':False, 'email_unsubscribed':False,
                         'email_status':'verified','contact_campaign_statuses':[
                             {'emailer_campaign_id':'sequence','status':'active','replied':False}]}]
        self.client.pages[0]['messages'] = [{'messageUrn':'id','senderProfileId':'lead',
                                             'isSentByOwner':False,'sentAt':101}]
        from datetime import datetime, timezone
        coverage = ReplyCoverage(Apollo(), self.adapter.reply_source(PLAN, 100),
                                 clock=lambda:datetime(2026, 1, 1, tzinfo=timezone.utc))
        target = {'email':'lead@example.invalid', 'apollo_contact_id':'contact','sequence_id':'sequence',
                  'linkedin_profile_urn':PLAN['recipient_profile_urn']}
        self.assertEqual(coverage.observe(target)['status'], 'reply')


class RealClientFakeHTTPTests(unittest.TestCase):
    def setUp(self):
        from test_thunder_os_connectsafely_client import ClientTests, ACCOUNT, OWNER, RECIPIENT, PLAN, NOW
        self.fixture = ClientTests(); self.fixture.setUp()
        self.plan, self.now = PLAN, NOW
        request = self.fixture.request
        def http(method, path, query, body):
            if '/relationship/' in path:
                self.fixture.calls.append((method, path, query, body))
                return 200, {'accountId':ACCOUNT, 'profileUrn':RECIPIENT, 'status':'CONNECTED',
                             'connected':True, 'invitationSent':False, 'invitationReceived':False}
            return request(method, path, query, body)
        self.fixture.client.request = http
        self.adapter = MessageAdapter(self.fixture.client)

    def test_real_client_fake_http_receipt(self):
        result = self.adapter.send(self.plan, self.now-1, before_dispatch=lambda:True)
        self.assertEqual(result['status'], 'sent')
        self.assertEqual(sum(call[0] == 'POST' for call in self.fixture.calls), 1)

    def test_real_client_fake_http_ambiguous_response_readback(self):
        self.fixture.post = (500, {})
        result = self.adapter.send(self.plan, self.now-1, before_dispatch=lambda:True)
        self.assertEqual(result['status'], 'sent')
        self.assertEqual(result['confirmation_kind'], 'observed_outbound')
        self.assertEqual(sum(call[0] == 'POST' for call in self.fixture.calls), 1)

    def test_real_client_fake_http_readback_wrong_text_remains_uncertain(self):
        self.fixture.post = (500, {})
        self.fixture.message['text'] = 'not the approved text'
        self.assertEqual(self.adapter.send(self.plan, self.now-1, before_dispatch=lambda:True)['status'], 'uncertain')
        self.assertEqual(sum(call[0] == 'POST' for call in self.fixture.calls), 1)

    def test_invite_reply_source_accepts_real_client_plan_without_post(self):
        from thunder_os.connectsafely_client import ConnectSafelyClient
        account = 'a' * 24
        profile = 'urn:li:fsd_profile:lead'
        plan = {'account_id': account, 'account_public_id': 'sender', 'action': 'invite',
                'recipient_public_id': 'lead', 'recipient_profile_urn': profile,
                'channel': 'linkedin', 'messaging_channel': 'linkedin_inbox',
                'content': '', 'conversation_urn': None}
        calls = []
        def request(method, path, query, body):
            calls.append((method, path))
            if path == '/linkedin/accounts':
                return 200, [{'id': account, 'publicId': 'sender', 'status': 'AVAILABLE', 'enabled': True}]
            if '/exists/' in path:
                return 200, {'success': True, 'accountId': account, 'profileUrn': profile,
                             'hasConversation': False}
            raise AssertionError('unexpected request')
        client = ConnectSafelyClient(account, 'sender', request=request,
                                    self_profile_id='urn:li:fsd_profile:owner')
        result = MessageAdapter(client).reply_source(plan, 100)(profile)
        self.assertTrue(result['complete'])
        self.assertTrue(result['no_record'])
        self.assertFalse(result['suppressed'])
        self.assertTrue(all(method == 'GET' for method, _ in calls))

    def test_reply_source_crosses_real_https_transport_without_network(self):
        import json
        from urllib.parse import urlsplit, parse_qs
        from thunder_os.connectsafely_client import ConnectSafelyClient
        from thunder_os.connectsafely_transport import ConnectSafelyHTTPS
        account='a'*24
        plan={'account_id':account, 'account_public_id':'sender', 'action':'dm',
              'recipient_public_id':'lead', 'recipient_profile_urn':'urn:li:fsd_profile:lead',
              'channel':'linkedin', 'messaging_channel':'linkedin_inbox', 'content':'Synthetic', 'conversation_urn':None}
        calls=[]
        class FakeHTTPS:
            status=200
            def __init__(self, *args, **kwargs): pass
            def request(self, method, path, body=None, headers=None):
                self.path=path; calls.append((method,path))
            def getresponse(self): return self
            def read(self, size):
                path=urlsplit(self.path).path
                if path=='/linkedin/accounts':
                    payload=[{'id':account,'publicId':'sender','status':'AVAILABLE','enabled':True}]
                elif '/exists/' in path:
                    payload={'success':True,'accountId':account,'profileUrn':plan['recipient_profile_urn'],
                             'hasConversation':True,'conversationUrn':'urn:li:msg_conversation:thread'}
                else:
                    payload={'success':True,'accountId':account,'conversationUrn':'urn:li:msg_conversation:thread',
                             'hasMore':False,'participants':[{'profileId':'owner'},{'profileId':'lead'}],
                             'messages':[{'messageUrn':'urn:li:messagingMessage:sample','senderProfileId':'lead',
                                          'isSentByOwner':False,'sentAt':101}]}
                return json.dumps(payload).encode()
            def close(self): pass
        transport=ConnectSafelyHTTPS(account,lambda:'synthetic-token-only',connection_factory=FakeHTTPS)
        client=ConnectSafelyClient(account,'sender',self_profile_id='urn:li:fsd_profile:owner',request=transport)
        result=MessageAdapter(client).reply_source(plan,100)(plan['recipient_profile_urn'])
        self.assertTrue(result['complete']); self.assertTrue(result['suppressed'])
        message_requests=[path for method,path in calls if '/messages?' in path]
        self.assertEqual(len(message_requests),1)
        self.assertEqual(parse_qs(urlsplit(message_requests[0]).query)['sync'],['true'])
        self.assertTrue(all(method=='GET' for method,_ in calls))


if __name__ == '__main__': unittest.main()
