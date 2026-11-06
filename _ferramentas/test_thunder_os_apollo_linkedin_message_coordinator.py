import hashlib
import os
import time
import unittest
from contextlib import nullcontext
from datetime import datetime, timezone
from unittest.mock import patch

from thunder_os.apollo_linkedin_live_executor import MessageCoordinator, hermes_unit_inactive
from thunder_os.apollo_linkedin_reply_coverage import ReplyCoverage


class Repo:
    def __init__(self):
        self.row = None
        self.control = {'paused': False}
        self.auth = {'task_id': 'task', 'state': 'issued', 'action_type': 'linkedin_step_message',
            'approval_id': 'approval', 'campaign_version': 'v1', 'company': 'company',
            'account_id': 'account', 'recipient': 'lead', 'member_urn': 'urn:li:fsd_profile:abc',
            'text_hash': hashlib.sha256(b'hello').hexdigest()}
    def get_state(self, kind, _id):
        return {'control': self.control, 'approval': {'campaign_id': 'campaign'},
                'apollo_linkedin_canary': self.auth}.get(kind)
    def apollo_linkedin_account_lock(self, account): return nullcontext()
    def transaction(self): return nullcontext()
    def reserve_apollo_linkedin_with_canary(self, *args, **kwargs):
        if self.row: raise ValueError('duplicate')
        self.row = {'state': 'reserved'}
    def finish_apollo_linkedin(self, *args, **kwargs):
        if not self.row or self.row['state'] != 'reserved': raise ValueError('bad transition')
        self.row.update(state=args[3], receipt=kwargs.get('receipt'))


class Authority:
    def require(self, *args):
        return {'binding': {'company_id': 'company', 'account_id': 'account', 'public_id': 'sender',
                            'sequence_id': 'sequence', 'owner_ids': ['owner']}}


class Apollo:
    def get(self, task_id):
        return {'id': task_id, 'type': 'linkedin_step_message', 'status': 'scheduled',
            'emailer_campaign_id': 'sequence', 'user_id': 'owner', 'contact_id': 'contact',
            'contact': {'id': 'contact', 'email': 'lead@example.test', 'linkedin_url': 'https://www.linkedin.com/in/lead'},
            'linkedin_emailer_template': {'body_text': 'hello'}}


class Message:
    def __init__(self, repo):
        self.repo, self.connected, self.result, self.sent, self.pause = repo, True, {'status': 'sent', 'receipt_ref': 'message-id'}, 0, False
    def relationship(self, plan): return self.connected
    def send(self, plan, reserved_at, *, before_dispatch):
        if self.pause: self.repo.control['paused'] = True
        if before_dispatch() is not True: return {'status': 'blocked', 'reason': 'dispatch_blocked'}
        self.sent += 1
        return self.result


class Coverage:
    hubspot_configured = False
    def __init__(self): self.status = 'ok'
    def observe(self, target):
        stamp = datetime.now(timezone.utc).isoformat()
        return {'status': self.status, 'required_sources': ['apollo', 'linkedin'], 'sources': {
            name: {'status': self.status, 'observed_at': stamp} for name in ('apollo', 'linkedin')}}


class Safety:
    def __init__(self): self.paused = False
    def other_writers_inactive(self): return True
    def ready(self, binding, plan): return True
    def pause_after_reply(self, binding, plan):
        self.paused = True
        return {'persisted': True}


class Completion:
    def __init__(self): self.result = {'state': 'confirmed'}
    def complete_verified(self, *args, **kwargs): return self.result


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repo(); self.message = Message(self.repo); self.coverage = Coverage()
        self.safety = Safety(); self.completion = Completion()
        self.subject = MessageCoordinator(repository=self.repo, authority=Authority(), apollo=Apollo(),
            message=self.message, coverage=self.coverage, safety=self.safety, completion=self.completion)
    def run_one(self):
        with patch.dict(os.environ, {'THUNDER_APOLLO_LI_EXECUTION_ENABLED': 'true'}):
            return self.subject.run('task', 'auth', dry_run=False)
    def test_confirmed_receipt_and_completion(self):
        self.assertEqual(self.run_one()['state'], 'confirmed')
        self.assertEqual(self.repo.row['receipt'], {'provider_id': 'message-id'})
        self.assertEqual(self.message.sent, 1)
    def test_missing_receipt_is_uncertain_and_never_retried(self):
        self.message.result = {'status': 'uncertain'}
        self.assertEqual(self.run_one()['state'], 'uncertain')
        self.assertEqual(self.run_one()['state'], 'blocked')
        self.assertEqual(self.message.sent, 1)
    def test_not_connected_blocks_before_reservation(self):
        self.message.connected = False
        self.assertEqual(self.run_one()['reason'], 'not_connected_or_unknown')
        self.assertIsNone(self.repo.row)
    def test_reply_blocks_and_pauses(self):
        self.coverage.status = 'reply'
        self.assertEqual(self.run_one()['reason'], 'human_reply')
        self.assertTrue(self.safety.paused)
        self.assertEqual(self.message.sent, 0)
    def test_unpersisted_reply_stop_is_visible(self):
        self.coverage.status = 'reply'
        self.safety.pause_after_reply = lambda *_: None
        self.assertEqual(self.run_one()['reason'], 'human_reply_pause_unverified')
        self.assertEqual(self.message.sent, 0)
    def test_pause_inside_lock_blocks_send(self):
        self.message.pause = True
        self.assertEqual(self.run_one()['state'], 'rejected')
        self.assertEqual(self.message.sent, 0)
    def test_apollo_failure_stays_pending(self):
        self.completion.result = {'state': 'apollo_reconcile_pending'}
        self.assertEqual(self.run_one()['state'], 'apollo_reconcile_pending')
        self.assertEqual(self.repo.row['state'], 'confirmed')
    def test_actual_reply_coverage_contract_is_composed(self):
        class Contacts:
            def contacts(self, email):
                return [{'id': 'contact', 'email_replied': False, 'email_unsubscribed': False,
                    'email_status': 'verified', 'contact_campaign_statuses': [
                        {'emailer_campaign_id': 'sequence', 'status': 'active', 'replied': False}]}]
        def linkedin(profile):
            return {'target': profile, 'complete': True, 'suppressed': False,
                    'coverage': 'exact_profile_conversation'}
        self.subject.coverage = ReplyCoverage(Contacts(), linkedin)
        self.assertEqual(self.run_one()['state'], 'confirmed')

    def test_coordinator_with_real_message_adapter_and_http_client(self):
        from thunder_os.apollo_linkedin_message import MessageAdapter
        from test_thunder_os_connectsafely_client import ClientTests, ACCOUNT, RECIPIENT, NOW
        fixture = ClientTests(); fixture.setUp()
        request = fixture.request
        def fake_http(method, path, query, body):
            if '/relationship/' in path:
                fixture.calls.append((method, path, query, body))
                return 200, {'accountId': ACCOUNT, 'profileUrn': RECIPIENT, 'status': 'CONNECTED',
                             'connected': True, 'invitationSent': False, 'invitationReceived': False}
            return request(method, path, query, body)
        fixture.client.request = fake_http
        self.repo.auth.update(account_id=ACCOUNT, recipient='fixture-person', member_urn=RECIPIENT,
                              text_hash=hashlib.sha256(b'Synthetic message').hexdigest())
        class Approved:
            def require(self, *args):
                return {'binding': {'company_id': 'company', 'account_id': ACCOUNT,
                    'public_id': 'ygorbuenobr', 'sequence_id': 'sequence', 'owner_ids': ['owner']}}
        class Task:
            def get(self, task_id):
                task = Apollo().get(task_id)
                task['contact']['linkedin_url'] = 'https://www.linkedin.com/in/fixture-person'
                task['linkedin_emailer_template']['body_text'] = 'Synthetic message'
                return task
        self.subject.authority = Approved()
        self.subject.apollo = Task()
        self.subject.message = MessageAdapter(fixture.client)
        self.subject.clock = lambda: NOW - 1
        self.assertEqual(self.run_one()['state'], 'confirmed')
        self.assertEqual(self.repo.row['receipt']['provider_id'], 'connectsafely:urn:li:messagingMessage:ONE')
        self.assertEqual(sum(call[0] == 'POST' for call in fixture.calls), 1)

    @patch('thunder_os.apollo_linkedin_live_executor.subprocess.run')
    def test_systemd_check_fails_closed_when_unavailable_or_active(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = 'LoadState=loaded\nActiveState=inactive\nSubState=dead\n'
        self.assertTrue(hermes_unit_inactive())
        self.assertEqual(run.call_args.args[0][2], 'thunder-os-hermes-linkedin.service')
        run.return_value.stdout = 'LoadState=loaded\nActiveState=active\nSubState=running\n'
        self.assertFalse(hermes_unit_inactive())
        run.return_value.returncode = 1
        self.assertFalse(hermes_unit_inactive())
        run.side_effect = OSError('unavailable')
        self.assertFalse(hermes_unit_inactive())


if __name__ == '__main__': unittest.main()
