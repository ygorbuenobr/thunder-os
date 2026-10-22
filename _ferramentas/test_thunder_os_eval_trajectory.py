"""Offline trajectory slice; --trace emits observations, never a release verdict."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_thunder_os_research import base_artifact
from thunder_os.agents import AgentJob, AgentRunner
from thunder_os.approvals import ApprovalError, _step_hash
from thunder_os.db import Repository, RepositoryConflict
from thunder_os.models import ContractError, payload_hash
from thunder_os.providers.email import EmailAdapter
from thunder_os.reply_control import ReplyControl
from thunder_os.service import ThunderService
from thunder_os.touch_ledger import TouchBlocked, TouchLedger
from thunder_os import touch_ledger
from thunder_os.worker import WorkerRuntime


TRAJECTORIES = []
CATALOG = {'version': 'eval-1', 'status': 'validado',
           'capabilities': ['cap-1'], 'proofs': ['proof-1']}


class FakeMailbox:
    """Only the provider is a double; possible effects survive a fake timeout."""
    def __init__(self):
        self.mode = 'confirmed'
        self.commands, self.messages, self.reads = [], {}, []

    def __call__(self, command):
        self.commands.append(deepcopy(command))
        ident = 'message-' + str(len(self.commands))
        if self.mode != 'missing_receipt':
            self.messages[ident] = deepcopy(command)
        if self.mode == 'timeout_after_effect':
            raise TimeoutError('synthetic timeout after possible effect')
        return {'ok': True, 'http_status': 200,
                **({'provider_id': ident} if self.mode != 'missing_receipt' else {})}

    def readback(self, receipt):
        self.reads.append(receipt)
        ident = receipt.removeprefix('email:eval-mail:')
        return deepcopy(self.messages.get(ident))


class EvalTrajectoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='thunder-eval-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ('socket.socket', 'socket.create_connection'):
            guard = patch(name, side_effect=AssertionError('eval network forbidden'))
            guard.start()
            self.addCleanup(guard.stop)
        guard = patch.object(touch_ledger.toques, '_banco',
                             side_effect=AssertionError('configured database forbidden'))
        guard.start()
        self.addCleanup(guard.stop)
        self.repo = Repository(self.root / 'workflow.db')
        self.ledger_path = self.root / 'touches.db'
        with self.database() as (connection, _):
            connection.executescript('''
                CREATE TABLE toques (
                    id INTEGER PRIMARY KEY, canal TEXT, acao TEXT, pessoa_chave TEXT,
                    alvo TEXT, status TEXT, provider_id TEXT, detalhe TEXT, agente TEXT,
                    ref TEXT, atualizado_em TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE respostas (
                    id INTEGER PRIMARY KEY, canal TEXT, pessoa_chave TEXT, pessoa_nome TEXT,
                    conta TEXT, toque_id INTEGER, classificacao TEXT, resumo TEXT, ref TEXT,
                    UNIQUE(canal,pessoa_chave,ref));
                CREATE TABLE thunder_suppressions (
                    canal TEXT, pessoa_chave TEXT, reason TEXT, PRIMARY KEY(canal,pessoa_chave));
                CREATE TABLE thunder_effect_touches (effect_key TEXT PRIMARY KEY,touch_id INTEGER);
            ''')
        self.ledger = TouchLedger(db_context=self.database)
        self.service = ThunderService(self.repo, self.ledger, approved_catalog=CATALOG)
        config = {'platform': 'thunder_os', 'campaign_id': 'eval-mexico',
                  'campaign_version': '1', 'rules_version': '1', 'run_id': 'eval-run',
                  'rules': 'Synthetic offline campaign; no live authority',
                  'providers': {'email': 'eval-mail'},
                  'configuration': {'flow_version': 'eval-1',
                                    'provider_accounts': {'email': 'eval-mailbox'}}}
        self.context = self.service.configure_campaign(config, 'ygor', expected_version=0)['context']
        self.research = self.service.submit_research({'dossier': base_artifact()}, 'ygor')
        self.mailbox = FakeMailbox()
        self.adapter = EmailAdapter('eval-mail', self.mailbox, account_id='eval-mailbox')
        self.worker = WorkerRuntime(self.repo, self.ledger, {'email': self.adapter},
                                    worker_id='eval-worker', health_path=self.root / 'health.json',
                                    revision='offline-eval', approved_catalog=CATALOG)
        self.create_batch()

    @contextmanager
    def database(self):
        connection = sqlite3.connect(self.ledger_path, timeout=1)
        try:
            with connection:
                yield connection, '?'
        finally:
            connection.close()

    def create_batch(self, ident='batch', followup=False):
        steps = [{'id': 'E1', 'recipient': 'person@example.invalid', 'channel': 'email',
                  'content': 'Synthetic approved text', 'subject': 'Synthetic E1',
                  'order': 1, 'interval_hours': 0}]
        if followup:
            steps.append({**steps[0], 'id': 'E2', 'subject': 'Synthetic E2',
                          'content': 'Synthetic followup', 'order': 2})
        return self.service.create_cadence({
            'id': ident, 'account_id': self.research['account']['domain'],
            'person_key': steps[0]['recipient'], 'objective': 'offline evaluation',
            'execution_context': self.context, 'steps': steps}, 'ygor')

    def approve(self, ident='batch', request='exact'):
        cadence = self.repo.get_state('cadence', ident)
        if request == 'exact':
            request = self.service.campaign_execution_review(cadence)
        return self.service.approve_cadence(
            ident, 'ygor', expected_version=cadence['version'],
            review_hash=self.service.cadence_review_hash(cadence), execution_confirmation=request)

    def queue(self, ident='batch', step='E1'):
        return self.service.dispatch_step(ident, step, 'ygor')

    def capture(self, case, *, error=None):
        effects = Repository(self.root / 'workflow.db').effects()
        readbacks = []
        for effect in effects:
            receipt = effect['receipt_ref']
            readbacks.append({'receipt': receipt, 'requested': bool(receipt),
                              'message': self.mailbox.readback(receipt) if receipt else None,
                              'authority': 'fake_provider_observer_only'})
        with self.database() as (connection, _):
            touches = [dict(zip(('id', 'status', 'receipt'), row)) for row in
                       connection.execute('SELECT id,status,provider_id FROM toques ORDER BY id')]
        trace = {'case_id': case, 'command': [row['payload'] for row in effects],
                 'provider_commands': deepcopy(self.mailbox.commands),
                 'receipt': [row['receipt_ref'] for row in effects], 'readback': readbacks,
                 'finalstate': [{'state': row['state'], 'error_code': row['error_code']}
                                for row in effects], 'touches': touches, 'error': error,
                 'cadence_state': {row['id']: {'status': row['status'],
                     'steps': [{'id': step['id'], 'status': step['status']} for step in row['steps']]}
                     for row in self.repo.list_state('cadence')}}
        TRAJECTORIES.append(trace)
        return trace

    def test_exact_approval_command_receipt_readback_finalstate(self):
        approval = self.approve()
        self.queue()
        self.assertEqual(self.mailbox.commands, [])
        self.assertTrue(self.worker.process_one())
        trace = self.capture('os-eval-005/008')
        command = trace['command'][0]
        self.assertEqual(command['approval_hash'], approval['step_hashes']['E1'])
        self.assertEqual(command['step']['recipient'], 'person@example.invalid')
        self.assertEqual(command['step']['content'], 'Synthetic approved text')
        self.assertEqual(trace['receipt'], ['email:eval-mail:message-1'])
        self.assertEqual(trace['readback'][0]['message'], trace['provider_commands'][0])
        self.assertEqual(trace['finalstate'], [{'state': 'sent', 'error_code': None}])
        self.assertEqual(trace['touches'][0]['status'], 'feito')
        # Worker has no provider readback call: this is an observer, not promotion.
        self.assertEqual(self.mailbox.reads, trace['receipt'])

    def test_missing_or_generic_approval_never_queues(self):
        for request in (None, True, {'confirmed': True}):
            with self.subTest(request=request), self.assertRaises(ApprovalError):
                self.approve(request=request)
        with self.assertRaisesRegex(ContractError, 'active approval required'):
            self.queue()
        trace = self.capture('os-eval-004', error='approval_rejected')
        self.assertEqual(trace['command'], [])
        self.assertEqual(trace['provider_commands'], [])
        self.assertIsNone(self.repo.get_state('approval', 'batch'))

    def test_approval_other_target_payload_channel_campaign_is_denied(self):
        cadence = self.repo.get_state('cadence', 'batch')
        exact = self.service.campaign_execution_review(cadence)
        for change in ('target', 'payload', 'channel', 'campaign'):
            bad = deepcopy(exact)
            if change == 'campaign':
                bad['context']['campaign_id'] = 'other-campaign'
            else:
                changed = deepcopy(cadence['steps'][0])
                field = {'target': 'recipient', 'payload': 'content', 'channel': 'channel'}[change]
                changed[field] = {'target': 'other@example.invalid', 'payload': 'Changed text',
                                  'channel': 'linkedin'}[change]
                if change == 'channel':
                    changed.pop('subject')
                    changed['recipient'] = 'https://www.linkedin.com/in/eval-synthetic'
                bad['scope_hash'] = payload_hash({'E1': _step_hash(self.service._approval_step(changed))})
            with self.subTest(change=change), self.assertRaises(ApprovalError):
                self.approve(request=bad)
        trace = self.capture('os-eval-016/017/028', error='confirmation_mismatch')
        self.assertEqual(trace['command'], [])
        self.assertEqual(trace['provider_commands'], [])

    def test_payload_change_after_queue_is_rejected_by_worker(self):
        self.approve()
        self.queue()
        cadence = self.repo.get_state('cadence', 'batch')
        cadence['steps'][0]['content'] = 'Changed after approval'
        self.repo.put_state('cadence', 'batch', cadence)
        self.assertTrue(self.worker.process_one())
        trace = self.capture('os-eval-017', error='scope_changed_after_queue')
        self.assertEqual(trace['provider_commands'], [])
        self.assertEqual(trace['finalstate'][0]['state'], 'failed')
        self.assertEqual(trace['finalstate'][0]['error_code'], 'campaign_confirmation_scope_changed')
        self.assertEqual(trace['touches'], [])

    def test_uncertain_after_possible_effect_never_repeats(self):
        self.mailbox.mode = 'timeout_after_effect'
        self.approve()
        self.queue()
        self.assertTrue(self.worker.process_one())
        self.queue()
        self.assertFalse(self.worker.process_one())
        restarted = WorkerRuntime(self.repo, self.ledger, {'email': self.adapter},
                                  worker_id='restarted', health_path=self.root / 'restart.json',
                                  revision='offline-eval', approved_catalog=CATALOG)
        self.assertFalse(restarted.process_one())
        effect = self.repo.effects()[0]
        with self.assertRaisesRegex(RepositoryConflict, 'not safely retryable'):
            self.repo.retry_effect(effect['id'])
        trace = self.capture('os-eval-010/021/023')
        self.assertEqual(len(trace['provider_commands']), 1)
        self.assertEqual(len(self.mailbox.messages), 1)  # Possible remote effect retained by fake.
        self.assertEqual(trace['receipt'], [None])
        self.assertFalse(trace['readback'][0]['requested'])
        self.assertEqual(trace['finalstate'], [{'state': 'uncertain', 'error_code': 'provider_timeout'}])
        self.assertEqual(trace['touches'][0]['status'], 'incerto')

    def test_http_200_without_receipt_is_uncertain(self):
        self.mailbox.mode = 'missing_receipt'
        self.approve()
        self.queue()
        self.worker.process_one()
        trace = self.capture('os-eval-030')
        self.assertEqual(trace['receipt'], [None])
        self.assertEqual(trace['finalstate'][0]['state'], 'uncertain')
        self.assertIsNone(trace['readback'][0]['message'])
        self.assertFalse(self.worker.process_one())

    def test_duplicate_command_and_second_batch_cannot_send_twice(self):
        self.approve()
        self.queue()
        self.queue()
        self.assertEqual(len(self.repo.effects()), 1)
        self.assertTrue(self.worker.process_one())
        self.queue()
        self.assertFalse(self.worker.process_one())
        self.create_batch('duplicate-batch')
        approval = self.approve('duplicate-batch')
        self.assertEqual(approval['status'], 'needs_reconciliation')
        with self.assertRaisesRegex(ContractError, 'active approval required'):
            self.queue('duplicate-batch')
        trace = self.capture('os-eval-006/022')
        self.assertEqual(len(trace['provider_commands']), 1)
        self.assertEqual(len(trace['touches']), 1)
        self.assertEqual(trace['finalstate'][0]['state'], 'sent')

    def test_reply_after_queue_blocks_remaining_send(self):
        self.create_batch('reply-batch', followup=True)
        self.approve('reply-batch')
        self.queue('reply-batch')
        self.assertTrue(self.worker.process_one())
        self.queue('reply-batch', 'E2')
        touch = self.ledger.preflight('unused', {'channel': 'email',
            'recipient': 'person@example.invalid', 'subject': 'Synthetic E1'})['duplicate']
        reply = self.service.record_reply({
            'id': 'reply-1', 'channel': 'email', 'content': 'Synthetic human reply',
            'cadence_id': 'reply-batch', 'account_id': self.research['account']['domain'],
            'person_key': 'person@example.invalid', 'preceding_touch_id': touch['touch_id'],
            'classification': 'positive'}, 'ygor')
        self.assertEqual(reply['state'], 'classified')
        self.assertTrue(self.worker.process_one())
        with self.assertRaisesRegex(ContractError, 'paused'):
            self.queue('reply-batch', 'E2')
        with self.assertRaisesRegex(TouchBlocked, 'has_reply'):
            self.ledger.preflight('unused', {'channel': 'email', 'recipient': 'person@example.invalid',
                                           'subject': 'Synthetic E2'}, raise_blocked=True)
        trace = self.capture('os-eval-029')
        self.assertEqual(len(trace['provider_commands']), 1)
        self.assertEqual(trace['finalstate'][1], {'state': 'failed', 'error_code': 'cadence_not_approved'})
        cadence = self.repo.get_state('cadence', 'reply-batch')
        self.assertEqual(cadence['status'], 'paused_reply')
        self.assertEqual(cadence['steps'][1]['status'], 'skipped_reply')
        self.assertEqual(trace['finalstate'][0]['state'], 'sent')

    def test_e2_without_confirmed_e1_cannot_dispatch(self):
        self.create_batch('followup-batch', followup=True)
        self.approve('followup-batch')
        with self.assertRaisesRegex(ContractError, 'predecessor confirmation time is missing'):
            self.queue('followup-batch', 'E2')
        trace = self.capture('mexico-e2-predecessor-only')
        self.assertEqual(trace['command'], [])
        self.assertEqual(trace['provider_commands'], [])

    def test_copy_declared_unapproved_proof_and_missing_reference_are_rejected(self):
        for approved, evidence in (([], [{'id': 'proof-1'}]), (['proof-1'], [])):
            job = AgentJob('proof-check', 'copy', {
                'validated_research': {'proof_ids': ['proof-1']}, 'approved_proofs': approved})
            output = {'copy': 'Synthetic text', 'angle_id': 'angle', 'evidence': evidence}
            result = AgentRunner(lambda *_: json.dumps(output), max_attempts=1).run(job)
            self.assertEqual(result.status, 'needs_human')
            self.assertEqual(result.error_code, 'invalid_agent_output')

    def test_dependency_is_the_existing_pinned_toques_source(self):
        manifest = json.loads((Path(__file__).resolve().parents[1] / 'runtime/dependencies.json').read_text())
        actual = hashlib.sha256(Path(touch_ledger.toques.__file__).read_bytes()).hexdigest()
        self.assertEqual(actual, manifest['dependencies']['toques.py']['sha256'])

    def fixture(self, ident):
        directory = os.environ.get('THUNDER_EVAL_DIR')
        self.assertTrue(directory, 'THUNDER_EVAL_DIR must name the canonical offline eval directory')
        rows = [json.loads(line) for line in (Path(directory) / 'cases-person-suppression.jsonl').read_text().splitlines()]
        self.assertEqual(len(rows), 6)
        return next(row for row in rows if row['id'] == ident)

    def canonical_reply(self, classification='negative', content='Nao me contate novamente.', fixture=None):
        initial = fixture['estado_inicial'] if fixture else {}
        person = initial.get('email', 'person@example.invalid')
        account = self.research['account']['domain']
        self.service.resolve_identity({'id': 'person', 'records': [
            {'domain': account, 'person_key': person, 'hubspot_contact_id': '123', 'source': 'email'},
            {'domain': account, 'person_key': person, 'hubspot_contact_id': '123', 'source': 'hubspot'},
        ]}, 'ygor')
        linkedin = {'id': 'LI0', 'recipient': initial.get('linkedin', 'https://www.linkedin.com/in/eval-person'),
                    'channel': 'linkedin', 'content': 'Convite sintetico pendente',
                    'order': 1, 'interval_hours': 0}
        self.service.resolve_identity({'id': 'person-linkedin', 'records': [
            {'domain': account, 'person_key': linkedin['recipient'], 'hubspot_contact_id': '123', 'source': 'linkedin'},
            {'domain': account, 'person_key': linkedin['recipient'], 'hubspot_contact_id': '123', 'source': 'hubspot'},
        ]}, 'ygor')
        self.service.create_cadence({'id': 'linkedin-pending', 'account_id': account,
            'person_key': person, 'objective': 'offline evaluation',
            'execution_context': self.context, 'steps': [linkedin]}, 'ygor')
        if initial.get('pending_invite'):
            with self.database() as (connection, _):
                connection.execute("INSERT INTO toques(canal,acao,pessoa_chave,alvo,status) VALUES (?,?,?,?,?)",
                    ('linkedin', 'convite', self.ledger.validate_identity(linkedin), 'perfil', 'reservado'))
        self.approve()
        self.queue()
        self.assertTrue(self.worker.process_one())
        touch = self.ledger.preflight('unused', self.repo.get_state('cadence', 'batch')['steps'][0])['duplicate']
        reply = self.service.record_reply({'id': 'canonical-reply', 'channel': 'email',
            'content': content, 'classification': classification, 'cadence_id': 'batch',
            'account_id': account, 'person_key': person,
            'preceding_touch_id': touch['touch_id']}, 'ygor')
        return reply, linkedin

    def test_email_refusal_suppresses_canonical_linkedin_pending(self):
        fixture = self.fixture('os-eval-031')
        reply, linkedin = self.canonical_reply(fixture['estado_inicial']['classification'], fixture['entrada'], fixture)
        self.assertEqual(reply['state'], 'classified')
        with self.assertRaisesRegex(TouchBlocked, '^suppressed$'):
            self.ledger.reserve('pending-linkedin', 'LI0', linkedin)
        self.assertEqual(self.ledger.preflight('unused', linkedin)['blocked'], 'suppressed')
        self.assertEqual(self.repo.get_state('cadence', 'linkedin-pending')['status'], 'paused_reply')
        alert = self.repo.get_state('operator_alert', 'canonical-reply')
        self.assertEqual(alert['owner'], 'ygor')
        self.assertIn('Pausa externa nao confirmada', alert['message'])
        self.assertEqual(len(self.mailbox.commands), 1)
        self.assertNotEqual(linkedin['recipient'], fixture['estado_inicial']['email'])
        with self.database() as (connection, _):
            self.assertEqual(connection.execute("SELECT status FROM toques WHERE acao='convite'").fetchall(), [('reservado',)])
        trace = self.capture(fixture['id'])
        self.assertEqual(len(trace['provider_commands']), 1)  # Only E1 preceding the reply.

    def assert_human_handoff(self, ident):
        fixture = self.fixture(ident)
        reply, _ = self.canonical_reply(fixture['estado_inicial']['classification'], fixture['entrada'], fixture)
        self.assertEqual(reply['content'], fixture['entrada'])
        for cadence in self.repo.list_state('cadence'):
            self.assertEqual(cadence['status'], 'paused_reply')
        alert = self.repo.get_state('operator_alert', 'canonical-reply')
        self.assertEqual(alert['owner'], 'ygor')
        self.assertTrue(alert['active'])
        self.assertIn('Ygor', alert['next_action'])
        self.assertIn('Pausa externa nao confirmada', alert['message'])
        self.assertFalse(self.worker.process_one())
        with self.assertRaisesRegex(ContractError, 'paused'):
            self.queue()
        self.assertEqual(len(self.repo.effects()), 1)
        self.assertEqual(len(self.mailbox.commands), 1)  # Zero after inbound.
        self.assertEqual(self.repo.list_state('copy'), [])
        self.assertEqual(self.repo.list_state('contact'), [])
        self.assertEqual(self.repo.list_state('meeting'), [])
        self.assertEqual({c['person_key'] for c in self.repo.list_state('cadence')}, {'person@example.invalid'})
        self.capture(fixture['id'])

    def test_case_032_referral_pauses_sender_without_preparing_third_party(self):
        self.assert_human_handoff('os-eval-032')

    def test_case_033_schedule_request_hands_off_to_ygor_without_autosend(self):
        self.assert_human_handoff('os-eval-033')

    def test_case_034_price_request_hands_off_to_ygor_without_autosend(self):
        self.assert_human_handoff('os-eval-034')

    def test_case_035_edited_approved_text_requires_new_approval(self):
        fixture = self.fixture('os-eval-035')
        self.approve()
        self.queue()
        cadence = self.repo.get_state('cadence', 'batch')
        cadence['steps'][0]['content'] = fixture['estado_inicial']['edited_content']
        self.repo.put_state('cadence', 'batch', cadence)
        self.assertTrue(self.worker.process_one())
        self.assertEqual(self.repo.effects()[0]['error_code'], 'campaign_confirmation_scope_changed')
        self.assertEqual(self.mailbox.commands, [])
        with self.assertRaisesRegex(ApprovalError, 'campaign_confirmation_scope_changed'):
            self.service.campaign_execution_review(self.repo.get_state('cadence', 'batch'),
                approval=self.repo.get_state('approval', 'batch'))
        self.assertEqual(self.capture(fixture['id'])['touches'], [])

    def test_case_036_timeout_after_start_never_repeats(self):
        fixture = self.fixture('os-eval-036')
        self.mailbox.mode = fixture['estado_inicial']['provider_mode']
        self.approve()
        self.queue()
        self.assertTrue(self.worker.process_one())
        self.queue()
        self.assertFalse(self.worker.process_one())
        restarted = WorkerRuntime(self.repo, self.ledger, {'email': self.adapter},
            worker_id='restarted', health_path=self.root / 'restart.json',
            revision='offline-eval', approved_catalog=CATALOG)
        self.assertFalse(restarted.process_one())
        with self.assertRaisesRegex(RepositoryConflict, 'not safely retryable'):
            self.repo.retry_effect(self.repo.effects()[0]['id'])
        trace = self.capture(fixture['id'])
        self.assertEqual(len(trace['provider_commands']), 1)
        self.assertEqual(len(self.mailbox.messages), 1)
        self.assertEqual(trace['receipt'], [None])
        self.assertEqual(trace['finalstate'], [{'state': 'uncertain', 'error_code': 'provider_timeout'}])
        self.assertEqual(trace['touches'][0]['status'], 'incerto')

    def test_opt_out_suppresses_canonical_linkedin(self):
        _, linkedin = self.canonical_reply('opt_out')
        with self.assertRaisesRegex(TouchBlocked, '^suppressed$'):
            self.ledger.reserve('pending-linkedin', 'LI0', linkedin)

    def test_risk_suppresses_canonical_linkedin(self):
        _, linkedin = self.canonical_reply('risk')
        with self.assertRaisesRegex(TouchBlocked, '^suppressed$'):
            self.ledger.reserve('pending-linkedin', 'LI0', linkedin)

    def test_reclassification_suppresses_canonical_linkedin(self):
        _, linkedin = self.canonical_reply('positive')
        self.service.classify_reply('canonical-reply', 'negative', 1.0, 'Ygor confirmou recusa', 'ygor')
        with self.assertRaisesRegex(TouchBlocked, '^suppressed$'):
            self.ledger.reserve('pending-linkedin', 'LI0', linkedin)

    def test_mendel_reply_control_to_service_negative_suppresses_confirmed_person(self):
        self.canonical_reply('positive')
        result = ReplyControl(self.repo, self.service.campaign_registry).ingest({
            'id': 'manual-synthetic', 'channel': 'email', 'content': 'Synthetic refusal',
            'received_at': '2026-10-22T12:00:00+00:00', 'hubspot_contact_id': '123',
            'campaign_id': 'eval-mexico', 'human_reply': True, 'identity_verified': True,
            'source': {'provider': 'manual', 'account_id': 'ygor',
                       'provider_message_id': 'manual-synthetic'}}, 'ygor')
        reply = self.service.classify_reply(result['reply']['id'], 'negative', 1.0,
                                             'Synthetic refusal', 'ygor')
        self.assertEqual(reply['classification'], 'negative')
        self.assertEqual(reply['state'], 'classified')
        for step in ({'channel': 'email', 'recipient': 'person@example.invalid', 'subject': 'Other'},
                     {'channel': 'linkedin', 'recipient': 'https://www.linkedin.com/in/eval-person'}):
            self.assertEqual(self.ledger.preflight('unused', step)['blocked'], 'suppressed')
            with self.assertRaisesRegex(TouchBlocked, '^suppressed$'):
                self.ledger.reserve('mendel-blocked', 'step', step)
        alert = self.repo.get_state('operator_alert', result['reply']['id'])
        self.assertEqual(alert['owner'], 'ygor')
        self.assertIn('Pausa externa nao confirmada', alert['message'])
        self.assertEqual(len(self.mailbox.commands), 1)

    def test_mendel_unapproved_cadence_cannot_bind_unrelated_recipient(self):
        unrelated = {'id': 'LI0', 'channel': 'linkedin',
                     'recipient': 'https://www.linkedin.com/in/synthetic-unrelated',
                     'content': 'Unapproved unrelated draft', 'order': 1, 'interval_hours': 0}
        self.service.create_cadence({'id': 'mistagged-draft',
            'account_id': self.research['account']['domain'], 'person_key': 'person@example.invalid',
            'objective': 'offline evaluation', 'execution_context': self.context,
            'steps': [unrelated]}, 'ygor')
        self.assertEqual(self.repo.get_state('cadence', 'mistagged-draft')['status'], 'ready_for_review')
        self.canonical_reply()
        self.assertIsNone(self.ledger.preflight('unrelated', unrelated)['blocked'])
        self.assertTrue(self.ledger.reserve('unrelated', 'LI0', unrelated)[0])

    def test_mendel_reply_control_channel_table_uses_confirmed_destinations(self):
        for channel in ('email', 'linkedin', 'phone', 'chattie'):
            with self.subTest(channel=channel):
                scenario = EvalTrajectoryTests()
                try:
                    scenario.setUp()
                    scenario.canonical_reply('positive')
                    result = ReplyControl(scenario.repo, scenario.service.campaign_registry).ingest({
                        'id': 'manual-channel', 'channel': channel, 'content': 'Synthetic refusal',
                        'received_at': '2026-10-22T12:00:00+00:00', 'hubspot_contact_id': '123',
                        'campaign_id': 'eval-mexico', 'human_reply': True, 'identity_verified': True,
                        'source': {'provider': 'manual', 'account_id': 'ygor',
                                   'provider_message_id': 'manual-channel'}}, 'ygor')
                    self.assertEqual(result['reply']['cadence_id'], 'batch')
                    self.assertEqual(scenario.repo.get_state('cadence', 'batch')['steps'][0]['channel'], 'email')
                    reply = scenario.service.classify_reply(result['reply']['id'], 'negative', 1.0,
                                                             'Synthetic refusal', 'ygor')
                    self.assertEqual(reply['state'], 'classified')
                    self.assertEqual(reply['classification'], 'negative')
                    for step in ({'channel': 'email', 'recipient': 'person@example.invalid', 'subject': 'Other'},
                                 {'channel': 'linkedin', 'recipient': 'https://www.linkedin.com/in/eval-person'}):
                        self.assertEqual(scenario.ledger.preflight('unused', step)['blocked'], 'suppressed')
                        with self.assertRaisesRegex(TouchBlocked, '^suppressed$'):
                            scenario.ledger.reserve('channel-blocked', 'step', step)
                    with scenario.database() as (connection, _):
                        self.assertEqual(connection.execute('SELECT canal FROM thunder_suppressions ORDER BY canal').fetchall(),
                                         [('email',), ('linkedin',)])
                    self.assertEqual(len(scenario.mailbox.commands), 1)
                    alert = scenario.repo.get_state('operator_alert', result['reply']['id'])
                    self.assertIn('Pausa externa nao confirmada', alert['message'])
                finally:
                    scenario.doCleanups()

    def test_mendel_confirmed_alias_outside_steps_is_suppressed(self):
        self.canonical_reply('positive')
        alias = 'alias@example.invalid'
        self.service.resolve_identity({'id': 'confirmed-alias', 'records': [
            {'domain': self.research['account']['domain'], 'person_key': alias,
             'hubspot_contact_id': '123', 'source': source} for source in ('email', 'hubspot')]}, 'ygor')
        self.service.classify_reply('canonical-reply', 'negative', 1.0, 'Synthetic refusal', 'ygor')
        step = {'channel': 'email', 'recipient': alias, 'subject': 'Outside cadence'}
        self.assertEqual(self.ledger.preflight('alias', step)['blocked'], 'suppressed')
        with self.assertRaisesRegex(TouchBlocked, '^suppressed$'):
            self.ledger.reserve('alias', 'outside', step)

    def test_negative_without_identity_registry_preserves_known_channel_stop(self):
        unrelated = {'id': 'LI0', 'channel': 'linkedin',
                     'recipient': 'https://www.linkedin.com/in/synthetic-unrelated',
                     'content': 'Unapproved unrelated draft', 'order': 1, 'interval_hours': 0}
        self.service.create_cadence({'id': 'unrelated-draft',
            'account_id': self.research['account']['domain'], 'person_key': 'person@example.invalid',
            'objective': 'offline evaluation', 'execution_context': self.context,
            'steps': [unrelated]}, 'ygor')
        self.approve()
        self.queue()
        self.assertTrue(self.worker.process_one())
        step = self.repo.get_state('cadence', 'batch')['steps'][0]
        touch = self.ledger.preflight('unused', step)['duplicate']
        self.assertEqual(self.repo.list_state('identity'), [])
        reply = self.service.record_reply({'id': 'legacy-negative', 'channel': 'email',
            'content': 'Nao me contate novamente.', 'classification': 'negative',
            'cadence_id': 'batch', 'account_id': self.research['account']['domain'],
            'person_key': 'person@example.invalid', 'preceding_touch_id': touch['touch_id']}, 'ygor')
        self.assertEqual(reply['classification'], 'negative')
        self.assertEqual(self.repo.get_state('cadence', 'batch')['status'], 'paused_reply')
        self.assertEqual(self.ledger.preflight('legacy', step)['blocked'], 'suppressed')
        with self.assertRaisesRegex(TouchBlocked, '^suppressed$'):
            self.ledger.reserve('legacy', 'E1', step)
        self.assertIsNone(self.ledger.preflight('unrelated', unrelated)['blocked'])
        self.assertTrue(self.ledger.reserve('unrelated', 'LI0', unrelated)[0])

    def test_reclassification_without_identity_registry_uses_confirmed_touch(self):
        self.approve()
        self.queue()
        self.assertTrue(self.worker.process_one())
        step = self.repo.get_state('cadence', 'batch')['steps'][0]
        touch = self.ledger.preflight('unused', step)['duplicate']
        self.service.record_reply({'id': 'legacy-positive', 'channel': 'email',
            'content': 'Resposta sintetica', 'classification': 'positive', 'cadence_id': 'batch',
            'account_id': self.research['account']['domain'], 'person_key': 'person@example.invalid',
            'preceding_touch_id': touch['touch_id']}, 'ygor')
        self.assertEqual(self.repo.list_state('identity'), [])
        self.assertEqual(self.ledger.preflight('legacy', step)['blocked'], 'has_reply')
        reply = self.service.classify_reply('legacy-positive', 'negative', 1.0, 'Recusa confirmada', 'ygor')
        self.assertEqual(reply['classification'], 'negative')
        self.assertEqual(self.ledger.preflight('legacy', step)['blocked'], 'suppressed')
        with self.assertRaisesRegex(TouchBlocked, '^suppressed$'):
            self.ledger.reserve('legacy', 'E1', step)

    def test_mendel_conflicting_alias_contact_ids_fail_before_suppression(self):
        self.canonical_reply('positive')
        for contact in ('123', '999'):
            self.service.resolve_identity({'id': 'alias-' + contact, 'records': [
                {'domain': self.research['account']['domain'], 'person_key': 'alias@example.invalid',
                 'hubspot_contact_id': contact, 'source': source} for source in ('email', 'hubspot')]}, 'ygor')
        with self.assertRaisesRegex(ContractError, '^reply identity conflict$'):
            self.service.classify_reply('canonical-reply', 'negative', 1.0, 'Refusal', 'ygor')
        with self.database() as (connection, _):
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM thunder_suppressions').fetchone()[0], 0)
        self.assertEqual(self.repo.get_state('reply', 'canonical-reply')['classification'], 'positive')

    def test_mendel_conflicting_record_contact_ids_fail_before_suppression(self):
        self.canonical_reply('positive')
        self.service.resolve_identity({'id': 'conflicting-records', 'records': [
            {'domain': self.research['account']['domain'], 'person_key': 'alias@example.invalid',
             'hubspot_contact_id': contact} for contact in ('123', '999')]}, 'ygor')
        with self.assertRaisesRegex(ContractError, '^reply identity conflict$'):
            self.service.classify_reply('canonical-reply', 'negative', 1.0, 'Refusal', 'ygor')
        with self.database() as (connection, _):
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM thunder_suppressions').fetchone()[0], 0)

    def test_mendel_reply_control_invalid_context_never_suppresses(self):
        self.canonical_reply('positive')
        result = ReplyControl(self.repo, self.service.campaign_registry).ingest({
            'id': 'manual-invalid', 'channel': 'email', 'content': 'Synthetic refusal',
            'received_at': '2026-10-22T12:00:00+00:00', 'hubspot_contact_id': '123',
            'campaign_id': 'eval-mexico', 'human_reply': True, 'identity_verified': True,
            'source': {'provider': 'manual', 'account_id': 'ygor',
                       'provider_message_id': 'manual-invalid'}}, 'ygor')
        for change, error in (({'account_slug': 'other.invalid'}, 'reply identity mismatch'),
                              ({'human_reply': False}, 'reply identity unresolved'),
                              ({'identity_confirmed': False}, 'reply identity unresolved'),
                              ({'hubspot_contact_id': '999'}, 'reply identity conflict'),
                              ({'paused_cadence_ids': []}, 'reply identity unresolved')):
            with self.subTest(change=change):
                self.repo.put_state('reply', result['reply']['id'], {**result['reply'], **change})
                with self.assertRaisesRegex(ContractError, '^' + error + '$'):
                    self.service.classify_reply(result['reply']['id'], 'negative', 1.0, 'Refusal', 'ygor')
                with self.database() as (connection, _):
                    self.assertEqual(connection.execute('SELECT COUNT(*) FROM thunder_suppressions').fetchone()[0], 0)

    def test_mendel_reclassification_rejects_changed_unconfirmed_cadence_destination(self):
        self.canonical_reply('positive')
        cadence = self.repo.get_state('cadence', 'batch')
        cadence['steps'][0]['recipient'] = 'unrelated@example.invalid'
        self.repo.put_state('cadence', 'batch', cadence)
        with self.assertRaisesRegex(ContractError, '^reply destination unconfirmed$'):
            self.service.classify_reply('canonical-reply', 'negative', 1.0, 'Refusal', 'ygor')
        with self.database() as (connection, _):
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM thunder_suppressions').fetchone()[0], 0)

    def test_unresolved_identity_does_not_suppress_unrelated_linkedin(self):
        self.canonical_reply()
        other = {'channel': 'linkedin', 'recipient': 'https://www.linkedin.com/in/eval-other'}
        self.assertIsNone(self.ledger.preflight('other', other)['blocked'])
        self.assertTrue(self.ledger.reserve('other', 'LI0', other)[0])

    def test_completed_cadence_history_is_preserved_on_canonical_reply(self):
        historical = self.repo.get_state('cadence', 'batch')
        self.repo.put_state('cadence', 'historical', {**historical, 'id': 'historical', 'status': 'completed'})
        self.canonical_reply()
        self.assertEqual(self.repo.get_state('cadence', 'historical')['status'], 'completed')

    def test_workflow_failure_rolls_back_projection_and_keeps_ledger_suppressed(self):
        original = self.repo.put_state
        def fail_reply(kind, *args, **kwargs):
            if kind == 'reply':
                raise RuntimeError('synthetic workflow failure')
            return original(kind, *args, **kwargs)
        with patch.object(self.repo, 'put_state', side_effect=fail_reply):
            with self.assertRaisesRegex(RuntimeError, 'synthetic workflow failure'):
                self.canonical_reply()
        self.assertEqual(self.repo.list_state('reply'), [])
        self.assertEqual(self.repo.list_state('operator_alert'), [])
        self.assertTrue(all(c['status'] != 'paused_reply' for c in self.repo.list_state('cadence')))
        linkedin = {'channel': 'linkedin', 'recipient': 'https://www.linkedin.com/in/eval-person'}
        self.assertEqual(self.ledger.preflight('unused', linkedin)['blocked'], 'suppressed')

    def test_fail_on_socket_guard_is_active(self):
        import socket
        for call in (lambda: socket.socket(), lambda: socket.create_connection(('example.invalid', 443))):
            with self.assertRaisesRegex(AssertionError, 'eval network forbidden'):
                call()


def copy_probes():
    """Diagnostic observations only; absence of enforcement is not a passing gate."""
    examples = {
        'mexico-e2-without-real-e1': ('Seguimos con nuestra propuesta.', {'touch': 'E2', 'e1': None}),
        'mexico-generic-e1': ('Ayudamos a empresas a crecer. Podemos conversar?', {'touch': 'E1'}),
        'mexico-missing-approved-proof': ('Podemos aumentar sus ventas.', {'touch': 'E1'}),
        'mexico-wrong-cta': ('Puede reunirse manana a las 9?', {'touch': 'E1'}),
        'mexico-vi-que': ('Vi que su empresa esta creciendo.', {'touch': 'E1'}),
        'mexico-e2-repeats-intro': ('Soy de una agencia y ayudamos a empresas.',
                                    {'touch': 'E2', 'e1': 'Soy de una agencia y ayudamos a empresas.'}),
    }
    observations = []
    for ident, (text, context) in examples.items():
        job = AgentJob(ident, 'copy', {'validated_research': {'proof_ids': []},
                                     'approved_proofs': [], **context})
        output = {'copy': text, 'angle_id': 'synthetic', 'evidence': []}
        result = AgentRunner(lambda *_: json.dumps(output), max_attempts=1).run(job)
        observations.append({'case_id': ident, 'observed_status': result.status,
                             'error_code': result.error_code, 'diagnostic_only': True,
                             'copy_quality_enforcement': 'unverified'})
    return observations


if __name__ == '__main__':
    emit = '--trace' in sys.argv
    if emit:
        sys.argv.remove('--trace')
    program = unittest.main(exit=False)
    if emit:
        print(json.dumps({'release_gate': 'OFF', 'trajectory': TRAJECTORIES,
                          'copy_probes': copy_probes()}, ensure_ascii=True, sort_keys=True))
    raise SystemExit(0 if program.result.wasSuccessful() else 1)
