"""Offline trajectory slice; --trace emits observations, never a release verdict."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
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
