"""Offline migration acceptance: synthetic ports/ledgers, temporary SQLite only.

The fake readiness observation explicitly models verified deployment evidence;
these tests do not establish readiness or authorize any production activation.
"""
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from thunder_os.campaign_registry import CampaignRegistry
from thunder_os.connectsafely_application import ConnectSafelyApplication
from thunder_os.connectsafely_console import ConnectSafelyConsole
from thunder_os.connectsafely_social_console import ConnectSafelySocialConsole
from thunder_os.email_canary import EmailCanary, CanaryError
from thunder_os.db import Repository
from thunder_os.hubspot_console import HubSpotConsole
from thunder_os.integration_hubspot import PORTAL_ID, plan_record
from thunder_os.seqd_client import MAILBOX, MAILBOX_ID, WORKSPACE_ID
from thunder_os.seqd_console import SEQDConsole


class FixtureLedger:
    """Explicit injected legacy fixture contract, no canonical database access."""
    def __init__(self):
        self.calls = []

    def reserve(self, *args):
        self.calls.append('reserve')
        return True, {'id': 'synthetic-touch'}

    def validate(self, *args):
        self.calls.append('validate')

    def preflight(self, *args, **kwargs):
        self.calls.append('preflight')

    def finish(self, effect, status, receipt):
        self.calls.append('finish')
        return {'status': status, 'receipt_ref': receipt}


class FixturePort:
    def __init__(self, kind, observer):
        self.kind, self.observer = kind, observer
        self.calls = 0
        self.unknown = False
        self.hook = lambda: None
        self.observations = []
        self.props = None

    def verify_identity(self):
        return {'verified': True, 'account_id': 'synthetic-cs',
                'account_public_id': 'synthetic-owner'}

    def portal(self):
        return PORTAL_ID

    def prepare(self, kind, props, effect_id, record_id=None):
        self.props = dict(props)
        metadata = {'portal_id': PORTAL_ID, 'object_type': kind,
                    'properties': {key: {'readOnlyValue': False} for key in props}}
        dedupe = {'portal_id': PORTAL_ID, 'object_type': kind, 'complete': True,
                  'records': [], 'checked': {'name': props['firstname'].casefold(),
                  'identity': {'property': 'email', 'value': props['email']}}}
        return plan_record(kind, props, metadata=metadata, dedupe=dedupe,
                           effect_id=effect_id, record_id=record_id)

    def observe(self):
        self.calls += 1
        self.observations.append(self.observer.list_state(self.kind + '_attempt'))
        self.hook()
        if self.unknown:
            raise TimeoutError('synthetic lost response')

    def send(self, envelope, before_dispatch=None):
        if before_dispatch is not None:
            assert before_dispatch() is True
        self.observe()
        if self.kind == 'seqd':
            return {'mailbox': MAILBOX, 'status': 'sent', 'provider_id': 'synthetic-receipt'}
        return {'status': 'sent', 'receipt_ref': 'synthetic:receipt',
                'confirmation_kind': 'provider_receipt', 'write_attribution_confirmed': True}

    def write(self, request):
        self.observe()
        return {'id': '123'}

    def read(self, *args, **kwargs):
        if self.calls and not self.unknown:
            return {'id': '123', 'properties': self.props, 'archived': False}
        return None


class MigratedOperationsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = Repository(Path(temp.name) / 'synthetic.db')
        self.observer = Repository(self.repo.path)
        self.now = 1800000000
        self.registry = CampaignRegistry(self.repo)
        self.cfg = {'campaign_id': 'synthetic', 'campaign_version': '1', 'rules_version': '1',
                    'run_id': 'offline-run', 'platform': 'thunder_os',
                    'providers': {'linkedin': 'connectsafely', 'email': 'seqd', 'hubspot': 'hubspot'},
                    'rules': 'Synthetic fixtures only.', 'configuration': {'flow_version': '1',
                    'provider_accounts': {'linkedin': 'synthetic-cs',
                    'email': WORKSPACE_ID + ':' + MAILBOX_ID, 'hubspot': PORTAL_ID}}}
        self.registry.register(self.cfg, 'ygor', 0)
        # Synthetic readiness contract replaces only the external ledger probe.
        self.addCleanup(patch.stopall)
        patch('thunder_os.seqd_console.canonical_ledger_readiness', return_value=None).start()
        patch('thunder_os.email_canary.restricted_mode', return_value=False).start()

    def build(self, kind):
        port = FixturePort(kind, self.observer)
        ledger = FixtureLedger()
        clock = lambda: self.now
        if kind == 'connectsafely':
            console = ConnectSafelyConsole(self.repo, port, ledger, account_id='synthetic-cs',
                account_public_id='synthetic-owner', writes_enabled=True, clock=clock)
            plan = {'account_id': 'synthetic-cs', 'account_public_id': 'synthetic-owner',
                    'action': 'dm', 'recipient_public_id': 'synthetic-recipient',
                    'recipient_profile_urn': 'urn:li:member:synthetic', 'channel': 'linkedin',
                    'messaging_channel': 'linkedin_inbox', 'content': 'Offline fixture only.'}
        elif kind == 'seqd':
            console = SEQDConsole(self.repo, port, ledger=ledger, read_enabled=True,
                                  send_enabled=True, clock=clock)
            plan = {'to': 'synthetic@example.invalid', 'subject': 'Offline fixture', 'body': 'Synthetic.'}
        else:
            console = HubSpotConsole(self.repo, port, read_enabled=True, write_enabled=True, clock=clock)
            plan = {'idempotency_key': 'synthetic-contact', 'object_type': 'contacts',
                    'properties': {'email': 'synthetic@example.invalid', 'firstname': 'Synthetic'}}
        return console, port, ledger, plan

    def body(self, console, preview, kind):
        body = {'preview_id': preview['id'], 'expected_version': preview['version']}
        if kind == 'connectsafely':
            body['review_hash'] = preview['review_hash']
        elif kind == 'seqd':
            body.update(content_hash=preview['content_hash'], confirm_nonce='synthetic-nonce-123456')
        else:
            body.update(plan_hash=preview['plan']['plan_hash'], confirm=True)
        body['execution_confirmation'] = console.campaign_review(
            {'preview_id': preview['id'], 'campaign_id': 'synthetic'}, 'ygor')
        return body

    def test_valid_each_console_uses_durable_attempt_and_real_hubspot_executor(self):
        for kind in ('connectsafely', 'seqd', 'hubspot'):
            with self.subTest(kind=kind):
                console, port, ledger, plan = self.build(kind)
                preview = console.prepare(plan, 'ygor')
                self.assertEqual(port.calls, 0)
                self.assertEqual(ledger.calls, [])
                result = console.execute(self.body(console, preview, kind), 'ygor')
                self.assertEqual((result.get('result') or result)['status'],
                                 'verified_state' if kind == 'hubspot' else 'sent')
                self.assertEqual(port.calls, 1)
                self.assertEqual(len(port.observations[0]), 1)
                if kind == 'hubspot':
                    self.assertEqual(port.observations[0][0]['status'], 'attempted')
                elif kind == 'connectsafely':
                    self.assertEqual(port.observations[0][0]['phase'], 'dispatch_started')
                else:
                    self.assertTrue(port.observations[0][0]['canonical_owned'])

    def test_invalid_confirmation_revoke_expiry_and_pause_never_send(self):
        for kind in ('connectsafely', 'seqd', 'hubspot'):
            console, port, ledger, plan = self.build(kind)
            preview = console.prepare(plan, 'ygor')
            body = self.body(console, preview, kind)
            for mutation in ('missing', 'scope', 'expiry', 'pause', 'revoked'):
                with self.subTest(kind=kind, mutation=mutation):
                    altered = deepcopy(self.body(console, preview, kind))
                    if mutation == 'missing': del altered['execution_confirmation']
                    elif mutation == 'scope': altered['execution_confirmation']['scope_hash'] = '0' * 64
                    elif mutation == 'expiry': self.now += 301
                    elif mutation == 'pause': self.repo.put_state('control', 'global', {'paused': True})
                    else: self.registry.revoke('synthetic', 'ygor', self.repo.get_state('campaign_config', 'synthetic')['version'])
                    with self.assertRaises(ValueError): console.execute(altered, 'ygor')
                    self.assertEqual(port.calls, 0)
                    if mutation == 'expiry': self.now -= 301
                    if mutation == 'pause': self.repo.put_state('control', 'global', {'paused': False})
            self.registry.register(self.cfg, 'ygor', self.repo.get_state('campaign_config', 'synthetic')['version'])

    def test_external_registry_cannot_execute_console_or_enqueue(self):
        for platform in ('apollo', 'hubspot_seq', 'codex_local', 'chattie', 'instantly'):
            for kind in ('connectsafely', 'seqd', 'hubspot'):
                with self.subTest(platform=platform, kind=kind):
                    self.setUp()
                    console, port, ledger, plan = self.build(kind)
                    preview = console.prepare(plan, 'ygor')
                    body = self.body(console, preview, kind)
                    row = self.registry.register({**self.cfg, 'platform': platform}, 'ygor', 1)
                    body['execution_confirmation']['context'] = self.registry.load('synthetic')
                    self.assertEqual(body['execution_confirmation']['context'], row['context'])
                    with self.assertRaisesRegex(ValueError, '^operation_campaign_identity_mismatch$'):
                        console.execute(body, 'ygor')
                    if kind == 'connectsafely':
                        with self.assertRaisesRegex(ValueError, '^operation_campaign_identity_mismatch$'):
                            self.app(console).enqueue(body, 'ygor')
                        self.assertEqual(self.repo.list_state('connectsafely_job'), [])
                    self.assertEqual(port.calls, 0)
                    self.assertEqual(ledger.calls, [])
                    self.assertEqual(self.repo.list_state(kind + '_attempt'), [])
                    self.assertEqual(self.repo.list_state('operation_approval'), [])

    def test_unknown_results_are_not_replayed(self):
        for kind in ('connectsafely', 'seqd', 'hubspot'):
            with self.subTest(kind=kind):
                console, port, ledger, plan = self.build(kind)
                port.unknown = True
                preview = console.prepare(plan, 'ygor')
                body = self.body(console, preview, kind)
                result = console.execute(body, 'ygor')
                self.assertIn((result.get('result') or result)['status'], ('uncertain', 'pending_reconciliation'))
                with self.assertRaises(ValueError): console.execute(body, 'ygor')
                self.assertEqual(port.calls, 1)

    def test_seqd_dispatch_guard_is_one_shot(self):
        console, port, _, plan = self.build('seqd')
        preview = console.prepare(plan, 'ygor')
        calls = []
        def guard(envelope):
            calls.append(envelope)
            return len(calls) == 1
        result = console.execute(self.body(console, preview, 'seqd'), 'ygor', dispatch_guard=guard)
        self.assertEqual(result['status'], 'sent')
        self.assertEqual(len(calls), 1)
        self.assertEqual(port.calls, 1)

    def test_confirmed_context_does_not_bypass_identity_or_readiness(self):
        console, port, _, plan = self.build('connectsafely')
        preview = console.prepare(plan, 'ygor')
        body = self.body(console, preview, 'connectsafely')
        port.verify_identity = lambda: {'verified': True, 'account_id': 'other',
                                       'account_public_id': 'synthetic-owner'}
        with self.assertRaisesRegex(ValueError, 'account_identity_unverified'):
            console.execute(body, 'ygor')
        self.assertEqual(port.calls, 0)
        seqd, email, _, email_plan = self.build('seqd')
        preview = seqd.prepare(email_plan, 'ygor')
        with patch('thunder_os.seqd_console.canonical_ledger_readiness',
                   return_value='canonical_ledger_schema_unavailable'):
            with self.assertRaisesRegex(ValueError, 'canonical_ledger_schema_unavailable'):
                seqd.execute(self.body(seqd, preview, 'seqd'), 'ygor')
        self.assertEqual(email.calls, 0)

    def app(self, console):
        return ConnectSafelyApplication(self.repo, direct_console=console, execution_enabled=True,
            readiness=lambda: {'ready': True, 'fixture': 'synthetic verified readiness contract'},
            clock=lambda: self.now)

    def test_queue_valid_confirmation_runs_once(self):
        console, port, _, plan = self.build('connectsafely')
        app = self.app(console)
        preview = app.prepare(plan, 'ygor')
        job = app.enqueue(self.body(console, preview, 'connectsafely'), 'ygor')
        self.assertTrue(app.process_one(job_id=job['job_id']))
        stored = self.repo.get_state('connectsafely_job', job['job_id'])
        self.assertEqual(stored['state'], 'done', stored)
        self.assertFalse(app.process_one(job_id=job['job_id']))
        self.assertEqual(port.calls, 1)

    def test_provider_receipt_without_dispatch_callback_never_marks_sent(self):
        console, port, _, plan = self.build('connectsafely')
        app = self.app(console)
        def invalid_send(plan, before_dispatch=None):
            port.calls += 1
            return {'status': 'sent', 'receipt_ref': 'synthetic:invalid-receipt',
                    'confirmation_kind': 'provider_receipt', 'write_attribution_confirmed': True}
        port.send = invalid_send
        preview = app.prepare(plan, 'ygor')
        job = app.enqueue(self.body(console, preview, 'connectsafely'), 'ygor')
        self.assertTrue(app.process_one(job_id=job['job_id']))
        stored = self.repo.get_state('connectsafely_job', job['job_id'])
        self.assertNotEqual(stored['state'], 'done')
        self.assertNotEqual(stored['result']['status'], 'sent')
        self.assertFalse(stored.get('dispatch_started', False))
        attempt = self.repo.list_state('connectsafely_attempt')[0]
        self.assertNotEqual(attempt['status'], 'sent')
        self.assertFalse(app.process_one(job_id=job['job_id']))
        self.assertEqual(port.calls, 1)

    def test_legacy_queue_retained_for_reconciliation_without_send(self):
        console, port, _, plan = self.build('connectsafely')
        app = self.app(console)
        preview = app.prepare(plan, 'ygor')
        self.repo.append_state('connectsafely_job', 'legacy-job', {
            'preview_id': preview['id'], 'review': {}, 'actor': 'ygor',
            'state': 'queued', 'simulation': False})
        self.assertFalse(app.process_one(job_id='legacy-job'))
        self.assertEqual(self.repo.get_state('connectsafely_job', 'legacy-job')['state'], 'needs_reconciliation')
        self.assertEqual(port.calls, 0)

    def test_social_actions_use_inherited_confirmation_and_bound_capability(self):
        for index, action in enumerate(('comment', 'reply', 'react_post')):
            with self.subTest(action=action):
                port = FixturePort('connectsafely', self.observer)
                issued, bound = [], []
                capability = object()
                class Coordinator:
                    def issue(self, effect, **scope):
                        issued.append((effect, scope))
                        return capability
                def bind(value):
                    self.assertIs(value, capability)
                    bound.append(value)
                    return port
                port.with_social_capability = bind
                def resolver(plan):
                    proof = {'post_urn': plan['post_urn'], 'post_url': plan['post_url'],
                             'public_id': 'synthetic-author', 'profile_urn': 'urn:li:fsd_profile:synthetic'}
                    if plan['action'] == 'reply':
                        proof.update(parent_comment_urn=plan['parent_comment_urn'],
                            parent_public_id='synthetic-parent', parent_profile_urn='urn:li:fsd_profile:parent')
                    return proof
                console = ConnectSafelySocialConsole(self.repo, port, FixtureLedger(),
                    account_id='synthetic-cs', account_public_id='synthetic-owner',
                    writes_enabled=True, clock=lambda: self.now,
                    author_resolver=resolver, coordinator=Coordinator())
                urn = 'urn:li:activity:' + str(1000 + index)
                plan = {'account_id': 'synthetic-cs', 'account_public_id': 'synthetic-owner',
                        'action': action, 'channel': 'linkedin', 'post_urn': urn,
                        'post_url': 'https://www.linkedin.com/feed/update/' + urn + '/',
                        'parent_comment_urn': 'urn:li:fsd_comment:synthetic' if action == 'reply' else None,
                        'content': '' if action == 'react_post' else 'Synthetic offline comment.',
                        'reaction_type': 'LIKE' if action == 'react_post' else None}
                preview = console.prepare(plan, 'ygor')
                body = self.body(console, preview, 'connectsafely')
                invalid = deepcopy(body)
                invalid['execution_confirmation']['scope_hash'] = '0' * 64
                with self.assertRaises(ValueError): console.execute(invalid, 'ygor')
                self.assertEqual(issued, [])
                self.assertEqual(bound, [])
                self.assertEqual(port.calls, 0)
                result = console.execute(body, 'ygor')
                self.assertEqual(result['status'], 'sent', result)
                self.assertEqual(port.calls, 1)
                self.assertEqual(len(issued), 1)
                self.assertEqual(issued[0][1]['action'], action)
                self.assertEqual(issued[0][1]['post_urn'], urn)
                self.assertEqual(len(bound), 1)

    def test_real_email_canary_claim_dispatch_and_no_replay(self):
        self.exercise_real_email_canary(unknown=False)

    def test_canary_provider_timeout_preserves_dispatch_for_reconciliation(self):
        self.exercise_real_email_canary(unknown=True)

    def exercise_real_email_canary(self, *, unknown):
        console, port, _, plan = self.build('seqd')
        preview = console.prepare(plan, 'ygor')
        body = self.body(console, preview, 'seqd')
        policy = {'version': 1, 'approval_ref': 'synthetic-offline-approval',
                  'mailbox_id': MAILBOX_ID, 'recipient': plan['to'],
                  'preview_id': preview['id'], 'expected_version': preview['version'],
                  'content_hash': preview['content_hash'], 'issued_at': self.now,
                  'expires_at': self.now + 240}
        policy_path = Path(self.repo.path).parent / 'synthetic-policy.json'
        raw = json.dumps(policy).encode()
        policy_path.write_bytes(raw)
        environment = {'THUNDER_OS_EFFECT_MODE': 'canary', 'THUNDER_EXTERNAL_EFFECTS_ENABLED': 'true',
                       'THUNDER_OS_CANARY_POLICY_FILE': str(policy_path),
                       'THUNDER_OS_CANARY_POLICY_SHA256': hashlib.sha256(raw).hexdigest()}
        observations = []
        port.unknown = unknown
        with patch.dict(os.environ, environment), patch('thunder_os.email_canary.restricted_mode', return_value=True):
            guard = EmailCanary(self.repo, clock=lambda: self.now).claim(body, 'ygor')
            self.assertEqual(self.observer.get_state('email_canary_claim', guard.key)['phase'], 'claimed')
            port.hook = lambda: observations.append(self.observer.get_state('email_canary_claim', guard.key))
            result = console.execute(body, 'ygor', dispatch_guard=guard.before_dispatch,
                                     dispatch_expires_at=policy['expires_at'])
            self.assertEqual(result['status'], 'uncertain' if unknown else 'sent', result)
            self.assertEqual(port.calls, 1)
            self.assertEqual(self.observer.get_state('email_canary_claim', guard.key)['phase'], 'dispatch_started')
            self.assertTrue(guard.reconcile_scope({'preview_id': preview['id']}, 'ygor'))
            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0]['token'], guard.token)
            with self.assertRaisesRegex(CanaryError, 'canary_already_claimed'):
                EmailCanary(self.repo, clock=lambda: self.now).claim(body, 'ygor')
            with self.assertRaises(ValueError):
                console.execute(body, 'ygor', dispatch_guard=guard.before_dispatch,
                                dispatch_expires_at=policy['expires_at'])
            self.assertEqual(port.calls, 1)

    def test_invite_provider_timeout_preserves_job_and_allows_readonly_reconciliation(self):
        console, port, ledger, plan = self.build('connectsafely')
        app = self.app(console)
        port.unknown = True
        preview = app.prepare({**plan, 'action': 'invite'}, 'ygor')
        job = app.enqueue(self.body(console, preview, 'connectsafely'), 'ygor')
        self.assertTrue(app.process_one(job_id=job['job_id']))
        stored = self.observer.get_state('connectsafely_job', job['job_id'])
        self.assertTrue(stored['dispatch_started'])
        self.assertEqual(stored['state'], 'needs_reconciliation')
        attempt = self.observer.list_state('connectsafely_attempt')[0]
        self.assertEqual(attempt['phase'], 'dispatch_started')
        self.assertEqual(attempt['status'], 'uncertain')
        self.assertEqual(port.calls, 1)
        self.assertFalse(app.process_one(job_id=job['job_id']))
        self.assertEqual(port.calls, 1)
        reads, reconciliations = [], []
        def reconcile(plan, attempted_at):
            reads.append((plan, attempted_at))
            return {'status': 'sent', 'receipt_ref': 'synthetic:observed-invite',
                    'confirmation_kind': 'observed_outbound'}
        port.reconcile = reconcile
        ledger.reconcile_invitation = lambda *args: reconciliations.append(args)
        result = app.reconcile({'preview_id': preview['id']}, 'ygor')
        self.assertEqual(result['status'], 'sent', result)
        self.assertEqual(len(reads), 1)
        self.assertEqual(len(reconciliations), 1)
        self.assertEqual(self.repo.get_state('connectsafely_job', job['job_id'])['state'], 'done')
        self.assertEqual(len(self.repo.list_state('connectsafely_reconciliation')), 1)
        self.assertFalse(app.process_one(job_id=job['job_id']))
        self.assertEqual(port.calls, 1)

    def test_queued_confirmation_expiry_and_revocation_require_reconciliation(self):
        console, port, _, plan = self.build('connectsafely')
        app = self.app(console)
        for reason in ('expired', 'revoked'):
            with self.subTest(reason=reason):
                preview = app.prepare({**plan, 'content': 'Synthetic ' + reason}, 'ygor')
                job = app.enqueue(self.body(console, preview, 'connectsafely'), 'ygor')
                if reason == 'expired': self.now += 301
                else: self.registry.revoke('synthetic', 'ygor', 1)
                self.assertFalse(app.process_one(job_id=job['job_id']))
                self.assertEqual(self.repo.get_state('connectsafely_job', job['job_id'])['state'],
                                 'needs_reconciliation')
                self.assertEqual(port.calls, 0)

    def test_registry_writer_waits_until_provider_dispatch_finishes(self):
        console, port, _, plan = self.build('hubspot')
        preview = console.prepare(plan, 'ygor')
        body = self.body(console, preview, 'hubspot')
        started, finished = threading.Event(), threading.Event()
        failures = []
        def writer():
            started.set()
            try: CampaignRegistry(self.observer).revoke('synthetic', 'ygor', 1)
            except Exception as exc: failures.append(exc)
            finally: finished.set()
        thread = threading.Thread(target=writer)
        def hook():
            thread.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(finished.wait(.1))
        port.hook = hook
        result = console.execute(body, 'ygor')
        thread.join(3)
        self.assertTrue(finished.is_set())
        self.assertEqual(failures, [])
        self.assertEqual(result['result']['status'], 'verified_state')
        self.assertFalse(self.repo.get_state('campaign_config', 'synthetic')['active'])


if __name__ == '__main__':
    unittest.main()
