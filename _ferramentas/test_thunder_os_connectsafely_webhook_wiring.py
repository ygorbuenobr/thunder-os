"""Offline webhook-to-reply correlation; synthetic identities only."""
from types import SimpleNamespace
from unittest.mock import Mock, patch
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, local
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import hashlib
import hmac
import json
import threading
import unittest

import test_thunder_os_reply_control as fixtures
from thunder_os.apollo_linkedin_runtime_safety import RuntimeSafety
from thunder_os.connectsafely_webhook import ConnectSafelyWebhook, ConnectSafelyStoredEventReconciler


class WebhookWiringTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ApolloLinkedInReplyIdentityTests('test_webhook_binding_stops_t4_and_records_unclassified_reply_once')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.repo = self.fixture.repo
        self.service = SimpleNamespace(repository=self.repo, campaign_registry=self.fixture.control.registry,
                                       record_reply=Mock())
        self.webhook = ConnectSafelyWebhook(self.repo, secret=b's' * 32, account_id='a' * 24)

    def test_signed_event_with_one_binding_stops_and_creates_one_reply(self):
        event_id = self.fixture.event_id
        result = self.webhook.process(event_id, self.service)
        self.assertEqual(result['status'], 'processed')
        self.service.record_reply.assert_not_called()
        row = self.repo.get_state('connectsafely_event', event_id)
        self.assertEqual(row['reply_id'], result['reply_id'])
        reply = self.repo.get_state('reply', result['reply_id'])
        self.assertEqual(reply['identity_basis'], 'verified_connectsafely_event')
        self.assertIsNone(reply['human_reply'])
        key = RuntimeSafety._key({'company_id': 'company'}, {'recipient_public_id': 'person'})
        self.assertTrue(self.repo.get_state('apollo_linkedin_reply_stop', key)['active'])
        self.assertEqual(self.webhook.process(event_id, self.service)['status'], 'processed')
        self.assertEqual(len(self.repo.list_state('reply')), 1)

    def _convert_fixture_to_invitation(self):
        # Keep the original immutable evidence while changing the action on
        # every persisted source, as produced by the invitation executor.
        with self.repo.transaction() as con:
            con.execute("UPDATE thunder_state SET payload=json_set(payload,'$.action_type','linkedin_step_connect') "
                        "WHERE kind='apollo_linkedin_identity_binding' AND entity_id=?", (self.fixture.binding_id,))
            con.execute("UPDATE apollo_linkedin_reservations SET action_type='linkedin_step_connect' "
                        "WHERE company='company' AND task_id='task1' AND action_type='linkedin_step_message'")
            con.execute("UPDATE thunder_events SET payload=json_set(payload,'$.action_type','linkedin_step_connect') "
                        "WHERE entity_id='task1' AND type='apollo_linkedin_campaign_reservation'")

    def test_reply_after_confirmed_invitation_stops_campaign_once(self):
        self._convert_fixture_to_invitation()
        result = self.webhook.process(self.fixture.event_id, self.service)
        self.assertEqual(result['status'], 'processed')
        self.service.record_reply.assert_not_called()
        reply = self.repo.get_state('reply', result['reply_id'])
        self.assertEqual(reply['identity_basis'], 'verified_connectsafely_event')
        self.assertEqual(reply['campaign_id'], 'campaign')
        key = RuntimeSafety._key({'company_id': 'company'}, {'recipient_public_id': 'person'})
        self.assertTrue(self.repo.get_state('apollo_linkedin_reply_stop', key)['active'])
        self.assertEqual(self.webhook.process(self.fixture.event_id, self.service)['status'], 'processed')
        self.assertEqual(len(self.repo.list_state('reply')), 1)

    def test_invitation_with_mismatched_reservation_evidence_remains_in_triage(self):
        self._convert_fixture_to_invitation()
        with self.repo.transaction() as con:
            con.execute("UPDATE thunder_events SET payload=json_set(payload,'$.action_type','linkedin_step_message') "
                        "WHERE entity_id='task1' AND type='apollo_linkedin_campaign_reservation'")
        result = self.webhook.process(self.fixture.event_id, self.service)
        self.assertEqual(result['status'], 'processed')
        self.service.record_reply.assert_called_once()
        self.assertEqual(self.repo.list_state('apollo_linkedin_reply_stop'), [])

    def test_unconfirmed_invitation_and_cross_tenant_binding_are_not_auto_correlated(self):
        self._convert_fixture_to_invitation()
        with self.repo.transaction() as con:
            con.execute("UPDATE apollo_linkedin_reservations SET state='uncertain' "
                        "WHERE company='company' AND task_id='task1' AND action_type='linkedin_step_connect'")
        event = self.repo.get_state('connectsafely_event', self.fixture.event_id)['event']
        self.assertEqual(self.webhook._candidates(event), [])
        with self.repo.transaction() as con:
            con.execute("UPDATE apollo_linkedin_reservations SET state='confirmed' "
                        "WHERE company='company' AND task_id='task1' AND action_type='linkedin_step_connect'")
        original = self.repo.get_state('apollo_linkedin_identity_binding', self.fixture.binding_id)
        self.repo.append_state('apollo_linkedin_identity_binding', 'other-binding',
                               {**{k:v for k,v in original.items() if k not in ('id','version','updated_at')},
                                'company':'other-company'})
        self.webhook.process(self.fixture.event_id, self.service)
        self.service.record_reply.assert_called_once()
        self.assertEqual(self.repo.list_state('apollo_linkedin_reply_stop'), [])

    def _add_invitation_for_same_person(self):
        from thunder_os.apollo_linkedin_identity_binding import KIND, binding_key
        from test_thunder_os_apollo_linkedin_campaign_reservation import task
        invitation = {**self.fixture.binding, 'task_id':'invite-task', 'action_type':'linkedin_step_connect'}
        binding_id = binding_key('company', 'campaign', 'invite-task', 'a'*24, invitation['approval_id'])
        self.repo.append_state(KIND, binding_id, invitation)
        self.repo.reserve_apollo_linkedin_for_campaign('campaign', invitation['approval_id'], 'v1',
            {**task(), 'id':'invite-task', 'type':'linkedin_step_connect'}, observed_team_id='team',
            expected_text_hash=hashlib.sha256(b'Ola').hexdigest(), binding_id=binding_id)
        self.repo.finish_apollo_linkedin('company', 'invite-task', 'linkedin_step_connect', 'confirmed',
            receipt={'provider_id':'synthetic-invitation'}, apollo_status='pending')
        return binding_id

    def test_invitation_and_message_for_same_lead_correlate_once(self):
        self._add_invitation_for_same_person()
        result = self.webhook.process(self.fixture.event_id, self.service)
        self.service.record_reply.assert_not_called()
        self.assertEqual(self.repo.get_state('reply', result['reply_id'])['identity_basis'], 'verified_connectsafely_event')
        self.assertEqual(len(self.repo.list_state('apollo_linkedin_reply_stop')), 1)

    def test_reply_before_later_touch_uses_earlier_valid_evidence(self):
        self._add_invitation_for_same_person()
        with self.repo.transaction() as con:
            con.execute("UPDATE apollo_linkedin_reservations SET created_at='2035-01-01T00:00:00+00:00' "
                        "WHERE company='company' AND task_id='invite-task'")
        result = self.webhook.process(self.fixture.event_id, self.service)
        self.service.record_reply.assert_not_called()
        self.assertEqual(self.repo.get_state('reply', result['reply_id'])['identity_binding_id'], self.fixture.binding_id)

    def test_confirmed_touches_in_different_campaigns_stay_in_triage(self):
        invitation_id = self._add_invitation_for_same_person()
        with self.repo.transaction() as con:
            con.execute("UPDATE thunder_state SET payload=json_set(payload,'$.campaign_id','other-campaign') "
                        "WHERE kind='apollo_linkedin_identity_binding' AND entity_id=?", (invitation_id,))
        self.webhook.process(self.fixture.event_id, self.service)
        self.service.record_reply.assert_called_once()
        self.assertEqual(self.repo.list_state('apollo_linkedin_reply_stop'), [])

    def test_no_binding_keeps_existing_unresolved_triage(self):
        event_id = self.fixture.event_id
        item = self.repo.get_state('connectsafely_event', event_id)
        event = dict(item['event'], sender_urn='urn:li:fsd_profile:other')
        from thunder_os.models import payload_hash
        self.repo.put_state('connectsafely_event', event_id, {**item, 'event': event, 'fingerprint': payload_hash(event)})
        result = self.webhook.process(event_id, self.service)
        self.assertEqual(result['status'], 'processed')
        self.service.record_reply.assert_called_once()
        self.assertEqual(self.repo.list_state('apollo_linkedin_reply_stop'), [])
        pending = self.repo.get_state('connectsafely_correlation_pending', event_id)
        self.assertEqual(pending['state'], 'pending')
        self.assertEqual(pending['account_id'], 'a' * 24)

    def test_concurrent_processing_keeps_one_reply_stop_and_alert(self):
        event_id = self.fixture.event_id
        barrier = Barrier(2, timeout=5)
        thread = local()
        candidates = self.webhook._candidates
        def synchronize(event,**kwargs):
            rows = candidates(event,**kwargs)
            if not getattr(thread, 'seen', False):
                thread.seen = True
                barrier.wait()
            return rows
        with patch.object(self.webhook, '_candidates', side_effect=synchronize):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: self.webhook.process(event_id, self.service), range(2)))
        self.assertEqual([r['status'] for r in results], ['processed', 'processed'])
        self.assertEqual(len(self.repo.list_state('reply')), 1)
        self.assertEqual(len(self.repo.list_state('apollo_linkedin_reply_stop')), 1)
        self.assertEqual(len(self.repo.list_state('operator_alert')), 1)
        self.assertEqual(self.repo.get_state('connectsafely_event', event_id)['state'], 'processed')
        self.service.record_reply.assert_not_called()

    def test_two_eligible_bindings_stay_in_triage(self):
        original = self.repo.get_state('apollo_linkedin_identity_binding', self.fixture.binding_id)
        fake_id = 'another-synthetic-binding'
        self.repo.append_state('apollo_linkedin_identity_binding', fake_id,
                               {k:v for k,v in original.items() if k not in ('id','version','updated_at')})
        self.repo.append_event('task1', 'apollo_linkedin_campaign_reservation', {'binding_id': fake_id})
        result = self.webhook.process(self.fixture.event_id, self.service)
        self.assertEqual(result['status'], 'processed')
        self.service.record_reply.assert_called_once()
        self.assertEqual(self.repo.list_state('apollo_linkedin_reply_stop'), [])

    def test_http_signed_event_reaches_bound_reply_and_stop(self):
        import painel_servidor as panel
        item = self.repo.get_state('connectsafely_event', self.fixture.event_id)
        event = item['event']
        raw = json.dumps({'object': 'event', 'event': 'message.received', 'data': {
            'provider':'LINKEDIN','account_id':event['account_id'],'message_urn':event['message_urn'],
            'conversation_id':event['conversation_id'],'sender':{'id':event['sender_urn']},
            'timestamp':event['sent_at'],'body':event['content']}}).encode()
        signature = 'sha256=' + hmac.new(b's' * 32, raw, hashlib.sha256).hexdigest()
        server = ThreadingHTTPServer(('127.0.0.1', 0), panel.Handler)
        server.banco = SimpleNamespace(); server.thunder_service = self.service
        server.thunder_connectsafely_webhook = ConnectSafelyWebhook(
            self.repo, secret=b's' * 32, account_id='a' * 24, clock=lambda:1800000000)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            conn = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            try:
                conn.request('POST', '/api/os/v1/integrations/connectsafely/webhook', raw,
                             {'Content-Type':'application/json','X-Webhook-Signature':signature,
                              'X-Webhook-Timestamp':'1800000000'})
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())['status'], 'duplicate')
            finally:
                conn.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(2)
        self.assertEqual(self.repo.get_state('connectsafely_event', self.fixture.event_id)['state'], 'processed')
        self.assertEqual(len(self.repo.list_state('reply')), 1)
        self.assertEqual(len(self.repo.list_state('apollo_linkedin_reply_stop')), 1)
        self.service.record_reply.assert_not_called()

    def test_reply_before_receipt_remains_in_inbox_then_is_promoted_once(self):
        from thunder_os.service import ThunderService
        event_id = self.fixture.event_id
        with self.repo.transaction() as con:
            con.execute("UPDATE apollo_linkedin_reservations SET state='reserved',receipt=NULL,apollo_status=NULL "
                        "WHERE company='company' AND task_id='task1' AND action_type='linkedin_step_message'")
        service = ThunderService(self.repo)
        self.assertEqual(self.webhook.process(event_id, service)['status'], 'processed')
        reply_id = 'connectsafely:' + event_id
        original = self.repo.get_state('reply', reply_id)
        self.assertEqual(original['state'], 'identity_resolution')
        self.assertEqual(self.repo.get_state('connectsafely_correlation_pending', event_id)['state'], 'pending')
        self.assertEqual(self.webhook.reconcile_pending(service)['items'][0]['status'], 'triage')
        self.repo.finish_apollo_linkedin('company', 'task1', 'linkedin_step_message', 'confirmed',
            receipt={'provider_id':'synthetic-receipt'}, apollo_status='pending')
        reconciler = ConnectSafelyStoredEventReconciler(self.repo, account_id='a'*24)
        self.assertFalse(hasattr(reconciler, '_secret'))
        self.assertFalse(hasattr(reconciler, 'accept'))
        result = reconciler.reconcile_pending(service)
        self.assertEqual(result['items'][0]['status'], 'correlated')
        self.assertEqual(len(self.repo.list_state('reply')), 1)
        self.assertTrue(self.repo.get_state('reply', reply_id)['identity_confirmed'])
        self.assertEqual(self.repo.get_state('connectsafely_correlation_pending', event_id)['state'], 'resolved')
        self.assertEqual(len(self.repo.list_state('apollo_linkedin_reply_stop')), 1)
        self.assertEqual(self.webhook.reconcile_pending(service)['items'], [])

    def test_concurrent_reconciliation_promotes_existing_reply_once(self):
        from thunder_os.service import ThunderService
        event_id = self.fixture.event_id
        with self.repo.transaction() as con:
            con.execute("UPDATE apollo_linkedin_reservations SET state='reserved',receipt=NULL,apollo_status=NULL "
                        "WHERE company='company' AND task_id='task1' AND action_type='linkedin_step_message'")
        service = ThunderService(self.repo)
        self.webhook.process(event_id, service)
        self.repo.finish_apollo_linkedin('company', 'task1', 'linkedin_step_message', 'confirmed',
            receipt={'provider_id':'synthetic-receipt'}, apollo_status='pending')
        barrier = Barrier(2, timeout=5)
        thread = local()
        candidates = self.webhook._candidates
        def synchronize(event,**kwargs):
            rows = candidates(event,**kwargs)
            if not getattr(thread, 'seen', False):
                thread.seen = True
                barrier.wait()
            return rows
        with patch.object(self.webhook, '_candidates', side_effect=synchronize):
            with ThreadPoolExecutor(max_workers=2) as pool:
                result = list(pool.map(lambda _: self.webhook.reconcile_pending(service), range(2)))
        self.assertTrue(all(item['items'][0]['status'] == 'correlated' for item in result))
        self.assertEqual(len(self.repo.list_state('reply')), 1)
        self.assertEqual(len(self.repo.list_state('apollo_linkedin_reply_stop')), 1)
        self.assertEqual(len(self.repo.list_state('operator_alert')), 1)
        self.assertEqual(self.repo.get_state('connectsafely_correlation_pending', event_id)['state'], 'resolved')

    def test_signed_reply_older_than_first_touch_stays_in_triage(self):
        from thunder_os.service import ThunderService
        data = {'object':'event','event':'message.received','data':{
            'provider':'LINKEDIN','account_id':'a'*24,
            'message_urn':'urn:li:messagingMessage:older',
            'conversation_id':'urn:li:messagingThread:synthetic',
            'sender':{'id':'urn:li:fsd_profile:synthetic'},
            'timestamp':'2020-01-01T00:00:00+00:00','body':'Synthetic historic message'}}
        raw = json.dumps(data).encode()
        signature = 'sha256=' + hmac.new(b's'*32, raw, hashlib.sha256).hexdigest()
        receiver = ConnectSafelyWebhook(self.repo, secret=b's'*32, account_id='a'*24, clock=lambda:1800000000)
        accepted = receiver.accept(raw, {'X-Webhook-Signature':signature,'X-Webhook-Timestamp':'1800000000'})
        service = ThunderService(self.repo)
        self.assertEqual(receiver.process(accepted['event_id'], service)['status'], 'processed')
        result = receiver.reconcile_pending(service)
        self.assertEqual(result['items'][0]['status'], 'triage')
        reply = self.repo.get_state('reply','connectsafely:'+accepted['event_id'])
        self.assertEqual(reply['state'], 'identity_resolution')
        self.assertEqual(self.repo.list_state('apollo_linkedin_reply_stop'), [])

    def test_pending_scan_is_bounded_cursor_based_and_account_scoped(self):
        for key, account in [('1'*64, 'a'*24), ('2'*64, 'a'*24), ('3'*64, 'b'*24)]:
            self.repo.append_state('connectsafely_correlation_pending', key,
                                   {'account_id':account, 'event_id':key, 'state':'pending'})
        # Selection must paginate inside SQLite, not load all tenant records.
        with patch.object(self.repo, 'list_state', side_effect=AssertionError('unbounded scan')):
            first = self.webhook.reconcile_pending(self.service, limit=1)
        self.assertEqual(first['status'], 'partial')
        self.assertEqual(first['items'], [{'event_id':'1'*64,'status':'unverified'}])
        self.assertEqual(first['next_cursor'], '1'*64)
        second = self.webhook.reconcile_pending(self.service, after=first['next_cursor'], limit=1)
        self.assertEqual(second['status'], 'complete')
        self.assertEqual(second['items'], [{'event_id':'2'*64,'status':'unverified'}])
        self.assertIsNone(second['next_cursor'])
        with self.assertRaises(ValueError):
            self.webhook.reconcile_pending(self.service, after='not-a-cursor')

    def test_oversized_identity_candidates_fail_closed_without_false_triage(self):
        original = self.repo.get_state('apollo_linkedin_identity_binding', self.fixture.binding_id)
        body = {k:v for k,v in original.items() if k not in ('id','version','updated_at')}
        for i in range(100):
            self.repo.append_state('apollo_linkedin_identity_binding', 'synthetic-'+str(i), body)
        with self.assertRaisesRegex(ValueError, 'binding_budget_exceeded'):
            self.webhook.process(self.fixture.event_id, self.service)
        self.assertEqual(self.repo.get_state('connectsafely_event', self.fixture.event_id)['state'], 'received')
        self.assertEqual(self.repo.list_state('reply'), [])

    def test_budget_exhaustion_keeps_event_pending_and_allows_later_events(self):
        from thunder_os.connectsafely_webhook import WebhookError
        for key in ('1'*64, '2'*64, '3'*64):
            self.repo.append_state('connectsafely_correlation_pending', key,
                                   {'account_id':'a'*24, 'event_id':key, 'state':'pending'})
            self.repo.append_state('connectsafely_event', key, {'state':'processed',
                'event':{'account_id':'a'*24, 'sender_urn':'urn:li:fsd_profile:synthetic'}})
        with patch.object(self.webhook, '_candidates', side_effect=[[], WebhookError('binding_budget_exceeded')]):
            result = self.webhook.reconcile_pending(self.service, limit=5)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['reason'], 'binding_budget_exceeded')
        self.assertEqual(result['next_cursor'], '2'*64)
        self.assertEqual(result['items'], [{'event_id':'1'*64,'status':'triage'},
                         {'event_id':'2'*64,'status':'unverified','reason':'binding_budget_exceeded'}])
        self.assertEqual(self.repo.get_state('connectsafely_correlation_pending','2'*64)['state'], 'pending')
        with patch.object(self.webhook, '_candidates', return_value=[]):
            resumed = self.webhook.reconcile_pending(self.service, after=result['next_cursor'], limit=5)
            self.assertEqual(resumed['items'], [{'event_id':'3'*64,'status':'triage'}])
            restarted = self.webhook.reconcile_pending(self.service, limit=5)
        self.assertEqual([row['event_id'] for row in restarted['items']], ['1'*64,'2'*64,'3'*64])


if __name__ == '__main__':
    unittest.main()
