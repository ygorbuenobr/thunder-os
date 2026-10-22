"""Offline trajectory slice; --trace emits observations, never a release verdict."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import MappingProxyType
import unittest
from unittest.mock import patch

from test_thunder_os_research import base_artifact
from thunder_os.agents import AgentJob, AgentRunner
from thunder_os.approvals import ApprovalError, _step_hash
from thunder_os.db import Repository, RepositoryConflict
from thunder_os.models import ContractError, WorkStatus, as_payload, payload_hash
from thunder_os.providers.email import EmailAdapter
from thunder_os.reply_control import ReplyControl
from thunder_os.service import ThunderService
from thunder_os.touch_ledger import TouchBlocked, TouchLedger
from thunder_os import touch_ledger
from thunder_os.worker import WorkerRuntime


TRAJECTORIES = []
CASE_RUNS = []
FIXTURE_FILES = {'cases.jsonl': 30, 'cases-person-suppression.jsonl': 6}
CATALOG = {'version': 'eval-1', 'status': 'validado',
           'capabilities': ['cap-1'], 'proofs': ['proof-1']}


def eval_case(ident):
    """Bind one fixture to one executed scenario; cleanup also captures failures."""
    def decorate(method):
        @wraps(method)
        def execute(self):
            self.case_fixture = self.fixture(ident)
            self.observations = {}
            self.addCleanup(self.capture_case, ident)
            method(self)
        execute.eval_case_id = ident
        return execute
    return decorate


class FakeMailbox:
    """Only the provider is a double; possible effects survive a fake timeout."""
    def __init__(self):
        self.mode = 'confirmed'
        self.commands, self.messages, self.reads = [], {}, []
        self.readback_results = []

    def __call__(self, command):
        self.commands.append(deepcopy(command))
        ident = 'message-' + str(len(self.commands))
        if self.mode == 'rejected':
            return {'ok': False, 'http_status': 503, 'code': 'provider_rejected'}
        if self.mode != 'missing_receipt':
            self.messages[ident] = deepcopy(command)
        if self.mode == 'timeout_after_effect':
            raise TimeoutError('synthetic timeout after possible effect')
        return {'ok': True, 'http_status': 200,
                **({'provider_id': ident} if self.mode != 'missing_receipt' else {})}

    def readback(self, receipt):
        self.reads.append(receipt)
        ident = receipt.removeprefix('email:eval-mail:')
        message = deepcopy(self.messages.get(ident))
        self.readback_results.append({'receipt': receipt, 'message': message})
        return message


class EmailReadbackTests(unittest.TestCase):
    def setUp(self):
        self.mailbox = FakeMailbox()
        self.message = {'target': 'person@example.invalid', 'subject': 'Synthetic subject',
                        'content': '  Exact synthetic content\n', 'effect_id': 'E1'}
        self.mailbox.messages['message-1'] = deepcopy(self.message)
        self.adapter = EmailAdapter('eval-mail', self.mailbox, account_id='eval-mailbox')
        self.receipt = 'email:eval-mail:message-1'

    def test_readback_uses_explicit_reader_and_preserves_exact_mapping(self):
        with patch.object(self.mailbox, 'readback', return_value=MappingProxyType(self.message)) as reader:
            observed = self.adapter.readback(self.receipt)
            reader.assert_called_once_with(self.receipt)
        self.assertEqual(observed, self.message)
        observed['content'] = 'Changed local copy'
        self.assertEqual(self.message['content'], '  Exact synthetic content\n')
        self.assertEqual(self.mailbox.commands, [])

    def test_readback_rejects_wrong_receipt_before_transport(self):
        for receipt in (None, 1, '', 'linkedin:eval-mail:message-1',
                        'email:other-provider:message-1', 'email:eval-mail:',
                        'email:eval-mail:   ', ' email:eval-mail:message-1',
                        'email:eval-mail:message-1 '):
            with self.subTest(receipt=receipt), self.assertRaises(ValueError):
                self.adapter.readback(receipt)
        self.assertEqual(self.mailbox.reads, [])
        self.assertEqual(self.mailbox.commands, [])

    def test_readback_requires_configured_provider(self):
        adapter = EmailAdapter(None, self.mailbox, account_id='eval-mailbox')
        with self.assertRaises(ValueError):
            adapter.readback(self.receipt)
        self.assertEqual(self.mailbox.reads, [])
        self.assertEqual(self.mailbox.commands, [])

    def test_readback_without_callable_reader_never_falls_back_to_send(self):
        sends = []

        def send_only(command):
            sends.append(command)
            return {'ok': True, 'provider_id': 'must-not-send'}

        for transport in (None, send_only):
            with self.subTest(transport=transport), self.assertRaises(ValueError):
                EmailAdapter('eval-mail', transport).readback(self.receipt)
        send_only.readback = 'not callable'
        with self.assertRaises(ValueError):
            EmailAdapter('eval-mail', send_only).readback(self.receipt)
        self.assertEqual(sends, [])

    def test_readback_rejects_missing_or_malformed_provider_message(self):
        malformed = [None, [], {}, {**self.message, 'target': 'not-an-email'}]
        for field in self.message:
            malformed.append({key: value for key, value in self.message.items() if key != field})
            malformed.extend({**self.message, field: value} for value in (None, 3, '', '  '))
        for message in malformed:
            with self.subTest(message=message):
                with patch.object(self.mailbox, 'readback', return_value=message) as reader:
                    with self.assertRaises(ValueError):
                        self.adapter.readback(self.receipt)
                    reader.assert_called_once_with(self.receipt)
        self.assertEqual(self.mailbox.commands, [])

    def test_readback_timeout_and_provider_error_never_send_or_retry(self):
        for error in (TimeoutError('synthetic timeout'), RuntimeError('synthetic provider error')):
            with self.subTest(error=type(error).__name__):
                with patch.object(self.mailbox, 'readback', side_effect=error) as reader:
                    with self.assertRaisesRegex(ValueError, '^email readback failed$'):
                        self.adapter.readback(self.receipt)
                    reader.assert_called_once_with(self.receipt)
        self.assertEqual(self.mailbox.commands, [])


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
        self.mailbox = FakeMailbox()
        self.adapter = EmailAdapter('eval-mail', self.mailbox, account_id='eval-mailbox')
        self.service = ThunderService(self.repo, self.ledger, approved_catalog=CATALOG,
                                      effect_readers={'email': self.adapter})
        config = {'platform': 'thunder_os', 'campaign_id': 'eval-mexico',
                  'campaign_version': '1', 'rules_version': '1', 'run_id': 'eval-run',
                  'rules': 'Synthetic offline campaign; no live authority',
                  'providers': {'email': 'eval-mail'},
                  'configuration': {'flow_version': 'eval-1',
                                    'provider_accounts': {'email': 'eval-mailbox'}}}
        self.context = self.service.configure_campaign(config, 'ygor', expected_version=0)['context']
        self.research = self.service.submit_research({'dossier': base_artifact()}, 'ygor')
        self.service.resolve_identity({'id': 'person', 'records': [
            {'domain': self.research['account']['domain'], 'person_key': 'person@example.invalid',
             'source': 'fixture'} for _ in range(2)]}, 'ygor')
        self.case_fixture = {'id': 'readback-regression', 'entrada': 'Synthetic result regression'}
        self.observations = {}
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

    def remove_default_identity(self):
        with self.repo.transaction() as connection:
            connection.execute('DELETE FROM thunder_state WHERE kind=? AND entity_id=?',
                               ('identity', 'person'))

    def capture(self, case, *, error=None):
        effects = Repository(self.root / 'workflow.db').effects()
        readbacks = []
        for effect in effects:
            receipt = effect['receipt_ref']
            reads = [row for row in self.mailbox.readback_results if row['receipt'] == receipt]
            readbacks.append({'receipt': receipt, 'requested': bool(reads),
                              'message': deepcopy(reads[-1]['message']) if reads else None,
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

    def capture_case(self, ident):
        result = self._outcome.result
        failures = [str(detail) for test, detail in result.failures + result.errors
                    if test.id().startswith(self.id())]
        trace = self.capture(ident)
        trace.update(test=self.id(), fixture=deepcopy(self.case_fixture),
                     observations=deepcopy(self.observations),
                     outcome='FAIL' if failures else 'PASS', failures=failures,
                     provider_readbacks=deepcopy(self.mailbox.readback_results),
                     identities=self.repo.list_state('identity'),
                     work=[as_payload(item) for item in self.repo.list_work()],
                     operator_alerts=self.repo.list_state('operator_alert'))
        CASE_RUNS.append(trace)

    def assert_no_effect(self):
        self.assertEqual(self.mailbox.commands, [])
        self.assertEqual(self.repo.effects(), [])
        with self.database() as (connection, _):
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM toques').fetchone()[0], 0)

    def send_one(self):
        self.approve()
        job = self.queue()
        self.assertEqual(self.mailbox.commands, [])
        self.assertTrue(self.worker.process_one())
        self.assertFalse(self.worker.process_one())
        return self.repo.effect(job['id'])

    def propose_effect_result(self, effect, *, channel='email', readback=None,
                              evidence=None, status='sent'):
        """Exercise the existing result writer, with an effect-linked work context."""
        self._report_count = getattr(self, '_report_count', 0) + 1
        item = self.service.request_work({
            'objective': 'Conferir resultado operacional',
            'context': {'effect_id': effect['id'], 'cadence_id': 'batch',
                        'channel': 'email', 'case_id': self.case_fixture['id'],
                        'request': self.case_fixture['entrada']}},
            'ygor', self.case_fixture['id'] + '-report-' + str(self._report_count))
        item = self.service.claim(item.id, item.version, 'ygor')
        report = {'effect_id': effect['id'], 'cadence_id': 'batch', 'channel': channel,
                  'status': status, 'receipt_ref': effect['receipt_ref'], 'readback': readback,
                  'evidence': evidence if evidence is not None else [{
                      'source': 'fake_provider', 'effect_id': effect['id'],
                      'channel': channel, 'receipt_ref': effect['receipt_ref'],
                      'readback': readback}]}
        self.observations['proposed_result'] = deepcopy(report)
        try:
            self.service.record_job_result(item.id, item.version, report, 'ygor')
        except ContractError as exc:
            self.observations['result_rejection'] = str(exc)
        stored = Repository(self.root / 'workflow.db').get_work(item.id)
        self.observations['result_readback'] = as_payload(stored)
        self.observations['result_events'] = self.repo.events(item.id)
        return stored

    def assert_unverified_result_blocked(self, stored):
        self.assertNotEqual(stored.status, WorkStatus.SUCCEEDED,
                            'effect-linked work was promoted without matching receipt/readback')
        self.assertFalse(any(event['type'] == 'work_succeeded'
                             for event in self.repo.events(stored.id)))

    def identity_records(self, records):
        identity = self.service.resolve_identity({
            'id': self.case_fixture['id'], 'records': records}, 'ygor')
        self.observations['identity'] = identity
        return identity

    def attempt_identity_effect(self):
        try:
            approval = self.approve()
            self.observations['approval_status'] = approval['status']
            self.queue()
            self.worker.process_one()
        except ContractError as exc:
            self.observations['identity_effect_rejection'] = str(exc)
        self.assertEqual(self.mailbox.commands, [],
                         'unresolved business identity reached the provider')
        self.assertEqual(self.repo.list_state('contact'), [])

    def test_fixture_case_mapping_is_exactly_thirty_plus_six(self):
        methods = [getattr(type(self), name) for name in dir(type(self)) if name.startswith('test_')]
        mapped = [method.eval_case_id for method in methods if hasattr(method, 'eval_case_id')]
        expected = {f'os-eval-{number:03}' for number in range(1, 37)}
        self.assertEqual(len(mapped), 36)
        self.assertEqual(set(mapped), expected)
        for ident in mapped:
            self.assertEqual(self.fixture(ident)['estado_inicial']['case_id'], ident)

    @eval_case('os-eval-001')
    def test_case_001_readonly_research_preserves_account_without_effect(self):
        domain = self.research['account']['domain']
        identity = self.identity_records([
            {'domain': domain, 'person_key': 'person@example.invalid', 'source': source}
            for source in ('company_site', 'hubspot')])
        context = self.service.canonical_context(domain)
        item = self.service.request_work({'objective': self.case_fixture['entrada'],
            'context': {'account_id': domain, 'identity_id': identity['id'],
                        'job_type': 'research', 'read_only': True}}, 'ygor', 'case-001')
        self.assertEqual(identity['status'], 'canonical')
        self.assertEqual(context['account']['domain'], domain)
        self.assertEqual(item.status, WorkStatus.REQUESTED)
        self.assertEqual(self.repo.get_work(item.id).context['account_id'], domain)
        self.assertEqual(self.repo.outbox()[0]['kind'], 'agent_job')
        self.assertFalse(self.worker.process_one())
        self.assert_no_effect()

    @eval_case('os-eval-002')
    def test_case_002_unconfirmed_business_identity_blocks_effect(self):
        domain = self.research['account']['domain']
        identity = self.identity_records([
            {'domain': domain, 'source': 'company_site'},
            {'person_key': 'person@example.invalid', 'source': 'unconfirmed_contact'}])
        with self.subTest(contract='identity_resolution'):
            self.assertEqual(identity['status'], 'identity_resolution')
            self.assertIsNone(identity['canonical_person'])
        with self.subTest(contract='no_effect'):
            self.attempt_identity_effect()

    @eval_case('os-eval-003')
    def test_case_003_permitted_next_action_is_prepared_without_dispatch(self):
        domain = self.research['account']['domain']
        context = self.service.canonical_context(domain)
        step = self.service._approval_step(self.repo.get_state('cadence', 'batch')['steps'][0])
        cadence = self.service.create_cadence({
            'id': 'next-action', 'account_id': domain, 'person_key': step['recipient'],
            'objective': self.case_fixture['entrada'], 'execution_context': self.context,
            'steps': [step]}, 'ygor')
        self.assertTrue(context['canonical'])
        self.assertEqual(context['account_id'], domain)
        self.assertIn(step['channel'], self.context['providers'])
        self.assertEqual(cadence['status'], 'ready_for_review')
        self.assertEqual(cadence['objective'], self.case_fixture['entrada'])
        self.assertEqual(self.repo.get_state('cadence', 'next-action')['steps'][0]['status'], 'draft')
        with self.assertRaisesRegex(ContractError, 'active approval required'):
            self.queue('next-action')
        self.assert_no_effect()

    @eval_case('os-eval-004')
    def test_case_004_specific_approval_is_required_before_reservation(self):
        cadence = self.repo.get_state('cadence', 'batch')
        request = self.service.campaign_execution_review(cadence)
        self.observations['approval_request'] = request
        self.assertEqual(request['batch_id'], cadence['id'])
        self.assertEqual(request['scope_hash'], payload_hash({
            'E1': _step_hash(self.service._approval_step(cadence['steps'][0]))}))
        self.assertIsNone(self.repo.get_state('approval', 'batch'))
        with self.assertRaisesRegex(ContractError, 'active approval required'):
            self.queue()
        self.assert_no_effect()

    @eval_case('os-eval-005')
    def test_case_005_exact_live_approval_receipt_and_explicit_fake_readback(self):
        approval = self.approve()
        self.assertGreater(datetime.fromisoformat(approval['expires_at']), datetime.now(timezone.utc))
        job = self.queue()
        self.assertEqual(self.mailbox.commands, [])
        self.assertTrue(self.worker.process_one())
        effect = Repository(self.root / 'workflow.db').effect(job['id'])
        self.assertEqual(effect['payload']['approval_hash'], approval['step_hashes']['E1'])
        self.assertEqual(effect['state'], 'sent')
        self.assertEqual(effect['receipt_ref'], 'email:eval-mail:message-1')
        self.assertEqual(self.mailbox.reads, [])
        message = self.adapter.readback(effect['receipt_ref'])
        self.assertEqual(message, self.mailbox.commands[0])
        self.assertEqual(message['target'], effect['payload']['step']['recipient'])
        self.assertEqual(message['content'], effect['payload']['step']['content'])
        result = self.propose_effect_result(effect, readback=message)
        self.assertEqual(result.status, WorkStatus.SUCCEEDED)
        self.assertEqual(result.result['receipt_ref'], effect['receipt_ref'])
        self.assertEqual(result.result['verification'], {
            'source': 'configured_provider_readback', 'effect_id': effect['id'],
            'receipt_ref': effect['receipt_ref'], 'channel': 'email', 'provider': 'eval-mail',
            'account_id': 'eval-mailbox', 'scope': 'message_in_sender_account_not_delivery'})
        self.assertEqual(self.mailbox.reads, [effect['receipt_ref']] * 2)
        self.assertFalse(self.worker.process_one())
        self.assertEqual(len(self.mailbox.commands), 1)
        self.observations['verification_scope'] = 'submitted fake readback plus trusted consumer reread'

    @eval_case('os-eval-006')
    def test_case_006_prior_same_person_channel_target_prevents_second_touch(self):
        step = self.repo.get_state('cadence', 'batch')['steps'][0]
        self.assertTrue(self.ledger.reserve('historical', 'E1', step)[0])
        self.ledger.finish('historical', 'sent', 'email:eval-mail:historical')
        context = self.service.canonical_context(self.research['account']['domain'])
        self.observations['context'] = context
        approval = self.approve()
        self.assertEqual(approval['status'], 'needs_reconciliation')
        self.assertEqual(approval['duplicate_evidence'][0]['duplicate']['status'], 'sent')
        with self.assertRaisesRegex(ContractError, 'active approval required'):
            self.queue()
        self.assertFalse(self.worker.process_one())
        self.assertEqual(self.repo.effects(), [])
        self.assertEqual(self.mailbox.commands, [])
        with self.database() as (connection, _):
            self.assertEqual(connection.execute('SELECT status,provider_id FROM toques').fetchall(),
                             [('feito', 'email:eval-mail:historical')])

    @eval_case('os-eval-007')
    def test_case_007_global_person_suppression_blocks_before_reservation(self):
        person = 'person@example.invalid'
        self.ledger.suppress('email', person, self.case_fixture['entrada'], identities=[
            ('linkedin', 'https://www.linkedin.com/in/eval-person')])
        self.approve()
        context = self.service.canonical_context(self.research['account']['domain'])
        self.observations['context'] = context
        with self.assertRaisesRegex(TouchBlocked, '^suppressed$'):
            self.queue()
        with self.database() as (connection, _):
            self.assertEqual(connection.execute('SELECT canal FROM thunder_suppressions ORDER BY canal').fetchall(),
                             [('email',), ('linkedin',)])
        self.assertFalse(self.worker.process_one())
        self.assert_no_effect()

    @eval_case('os-eval-008')
    def test_case_008_result_cannot_succeed_before_readback_then_accepts_matching_evidence(self):
        effect = self.send_one()
        self.assertEqual(effect['receipt_ref'], 'email:eval-mail:message-1')
        result = self.propose_effect_result(effect)
        self.assertEqual(self.mailbox.reads, [])
        with self.subTest(phase='before_readback'):
            self.assert_unverified_result_blocked(result)
        message = self.adapter.readback(effect['receipt_ref'])
        self.assertEqual(message, self.mailbox.commands[0])
        verified = self.propose_effect_result(effect, readback=message)
        self.assertEqual(verified.status, WorkStatus.SUCCEEDED)
        self.assertEqual(self.mailbox.reads, [effect['receipt_ref']] * 2)
        self.assertEqual(len(self.mailbox.commands), 1)

    @eval_case('os-eval-009')
    def test_case_009_completed_worker_without_readback_keeps_result_unverified(self):
        effect = self.send_one()
        self.assertEqual(effect['state'], 'sent')
        self.assertIsNotNone(effect['receipt_ref'])
        result = self.propose_effect_result(effect)
        self.assertEqual(self.mailbox.reads, [])
        self.assert_unverified_result_blocked(result)

    @eval_case('os-eval-010')
    def test_case_010_unknown_provider_timeout_reconciles_without_new_attempt(self):
        self.mailbox.mode = 'timeout_after_effect'
        effect = self.send_one()
        self.assertEqual(effect['state'], 'uncertain')
        self.assertEqual(effect['error_code'], 'provider_timeout')
        self.assertIsNone(effect['receipt_ref'])
        self.queue()
        self.assertFalse(self.worker.process_one())
        with self.assertRaisesRegex(RepositoryConflict, 'not safely retryable'):
            self.service.retry_effect(effect['id'], 'ygor')
        self.assertEqual(len(self.mailbox.commands), 1)
        self.assertEqual(len(self.mailbox.messages), 1)
        self.assertEqual(self.service.canonical_context(self.research['account']['domain'])['blocks'],
                         ['provider_timeout'])

    @eval_case('os-eval-011')
    def test_case_011_known_failure_retry_preserves_required_restart_confirmation(self):
        self.mailbox.mode = 'rejected'
        effect = self.send_one()
        self.assertEqual((effect['state'], effect['error_code']), ('failed', 'provider_rejected'))
        self.assertEqual(self.mailbox.messages, {})
        with self.assertRaisesRegex(ApprovalError, '^campaign_restart_confirmation_required$'):
            self.service.retry_effect(effect['id'], 'ygor')
        self.assertFalse(self.worker.process_one())
        self.assertEqual(len(self.mailbox.commands), 1)
        prior_approval = self.repo.get_state('approval', 'batch')
        prior_cadence = self.repo.get_state('cadence', 'batch')
        review = self.service.retry_execution_review(effect['id'])
        self.assertEqual(review['batch_id'], 'batch')
        self.assertEqual(review['batch_version'], prior_cadence['version'])
        self.assertEqual(review['scope_hash'], payload_hash({
            'E1': _step_hash(self.service._approval_step(effect['payload']['step']))}))
        self.assertEqual(self.repo.effect(effect['id']), effect)
        self.assertEqual(self.repo.get_state('approval', 'batch'), prior_approval)
        self.assertEqual(self.repo.get_state('cadence', 'batch'), prior_cadence)
        with self.assertRaises(ContractError):
            self.service.retry_effect(effect['id'], 'ygor',
                execution_confirmation=prior_approval['execution_confirmation']['request'])
        self.assertEqual(self.repo.effect(effect['id']), effect)
        self.service.retry_effect(effect['id'], 'ygor', execution_confirmation=review)
        retry = Repository(self.root / 'workflow.db').effect(effect['id'])
        self.observations['retry_readback'] = retry
        self.assertEqual(retry['state'], 'pending')
        self.assertEqual(retry['effect_key'], effect['effect_key'])
        self.assertEqual(retry['payload']['step'], effect['payload']['step'])
        fresh_approval = self.repo.get_state('approval', 'batch')
        self.assertEqual(fresh_approval['step_ids'], ['E1'])
        self.assertNotEqual(retry['payload']['execution_confirmation_hash'],
                            effect['payload']['execution_confirmation_hash'])
        self.assertEqual(retry['payload']['execution_confirmation_hash'],
                         payload_hash(fresh_approval['execution_confirmation']))
        self.mailbox.mode = 'confirmed'
        self.assertTrue(self.worker.process_one())
        self.assertFalse(self.worker.process_one())
        self.assertEqual(self.repo.effect(effect['id'])['state'], 'sent')
        self.assertEqual(len(self.repo.effects()), 1)
        self.assertEqual(len(self.mailbox.commands), 2)
        self.assertEqual(self.mailbox.commands[0], self.mailbox.commands[1])
        self.assertEqual(len(self.mailbox.messages), 1)
        self.assertEqual(self.adapter.readback(self.repo.effect(effect['id'])['receipt_ref']),
                         self.mailbox.commands[1])
        with self.assertRaises(RepositoryConflict):
            self.service.retry_effect(effect['id'], 'ygor', execution_confirmation=review)
        self.assertFalse(self.worker.process_one())
        self.assertEqual(len(self.mailbox.commands), 2)

    def retry_candidate(self):
        self.mailbox.mode = 'rejected'
        effect = self.send_one()
        return effect, self.service.retry_execution_review(effect['id'])

    def assert_retry_blocked_without_new_attempt(self, effect, review, actor='ygor'):
        before = self.repo.effect(effect['id'])
        with self.assertRaises((ContractError, RepositoryConflict, TouchBlocked)):
            self.service.retry_effect(effect['id'], actor, execution_confirmation=review)
        self.assertEqual(self.repo.effect(effect['id']), before)
        self.assertFalse(self.worker.process_one())
        self.assertEqual(len(self.mailbox.commands), 1)
        self.assertEqual(self.mailbox.messages, {})

    def test_retry_rejects_confirmation_for_wrong_batch_version_scope_or_control(self):
        effect, review = self.retry_candidate()
        for field, wrong in (('batch_id', 'other-batch'),
                             ('batch_version', review['batch_version'] + 1),
                             ('scope_hash', '0' * 64),
                             ('control_version', review['control_version'] + 1)):
            with self.subTest(field=field):
                self.assert_retry_blocked_without_new_attempt(effect, {**review, field: wrong})

    def test_retry_rejects_confirmation_after_cadence_version_changes(self):
        effect, review = self.retry_candidate()
        cadence = self.repo.get_state('cadence', 'batch')
        self.repo.put_state('cadence', 'batch', cadence)
        self.assert_retry_blocked_without_new_attempt(effect, review)

    def test_retry_confirmation_does_not_authorize_hermes(self):
        effect, review = self.retry_candidate()
        self.assert_retry_blocked_without_new_attempt(effect, review, actor='hermes')

    def test_retry_global_pause_blocks_before_new_attempt(self):
        effect, review = self.retry_candidate()
        self.service.set_global_pause(True, 'ygor')
        self.assert_retry_blocked_without_new_attempt(effect, review)

    def test_retry_reply_paused_cadence_blocks_before_new_attempt(self):
        effect, review = self.retry_candidate()
        cadence = self.repo.get_state('cadence', 'batch')
        cadence['status'] = 'paused_reply'
        self.repo.put_state('cadence', 'batch', cadence)
        self.assert_retry_blocked_without_new_attempt(effect, review)

    def test_retry_suppression_blocks_before_new_attempt(self):
        effect, review = self.retry_candidate()
        self.ledger.suppress('email', 'person@example.invalid', 'Synthetic opt-out before retry')
        self.assert_retry_blocked_without_new_attempt(effect, review)

    def test_retry_missing_canonical_identity_blocks_before_new_attempt(self):
        effect, review = self.retry_candidate()
        self.remove_default_identity()
        self.assert_retry_blocked_without_new_attempt(effect, review)

    @eval_case('os-eval-012')
    def test_case_012_out_of_scope_publication_hands_off_without_external_effect(self):
        item = self.service.request_work({'objective': 'Publicar campanha fora do escopo operacional',
            'context': {'request': self.case_fixture['entrada'], 'channel': 'unsupported'}},
            'hermes', 'case-012')
        self.assertEqual(item.status, WorkStatus.NEEDS_HUMAN)
        self.assertIn('Ygor', item.next_action)
        self.assertEqual(self.repo.outbox(), [])
        self.assertFalse(self.worker.process_one())
        self.assert_no_effect()

    @eval_case('os-eval-013')
    def test_case_013_draft_only_copy_is_persisted_without_dispatch(self):
        copy = self.service.request_copy({'account_id': self.research['account']['domain'],
            'capability_ids': ['cap-1'], 'proof_ids': ['proof-1'],
            'final_copy': self.case_fixture['entrada']}, 'ygor')
        self.assertEqual(copy['status'], 'ready_for_review')
        self.assertEqual(self.repo.get_state('copy', copy['id'])['final_copy'], self.case_fixture['entrada'])
        self.assertIsNone(self.repo.get_state('approval', 'batch'))
        self.assertFalse(self.worker.process_one())
        self.assert_no_effect()

    @eval_case('os-eval-014')
    def test_case_014_similar_unconfirmed_companies_require_disambiguation(self):
        identity = self.identity_records([
            {'name': 'Acme Industrial', 'domain': self.research['account']['domain'],
             'person_key': 'person@example.invalid', 'source': 'company_site'},
            {'name': 'Acme Industria', 'domain': 'acme-other.invalid',
             'person_key': 'person@example.invalid', 'source': 'hubspot'}])
        self.assertEqual(identity['status'], 'identity_resolution')
        self.assertIsNone(identity['canonical_account'])
        self.attempt_identity_effect()

    @eval_case('os-eval-015')
    def test_case_015_result_channel_must_match_executed_email(self):
        effect = self.send_one()
        message = self.adapter.readback(effect['receipt_ref'])
        self.assertEqual(effect['payload']['step']['channel'], 'email')
        self.assertEqual(message, self.mailbox.commands[0])
        result = self.propose_effect_result(effect, channel='linkedin', readback=message)
        self.assert_unverified_result_blocked(result)
        self.assertEqual(self.mailbox.reads, [effect['receipt_ref']])

    @eval_case('os-eval-016')
    def test_case_016_other_target_approval_is_rejected_before_queue(self):
        approval = self.approve()
        cadence = self.repo.get_state('cadence', 'batch')
        approved_step = self.service._approval_step(cadence['steps'][0])
        self.assertEqual(approval['status'], 'active')
        self.assertEqual(approved_step['recipient'], 'person@example.invalid')
        self.assertEqual(approval['step_hashes']['E1'], _step_hash(approved_step))
        self.service.validate_campaign_execution(cadence, approval)
        cadence['steps'][0]['recipient'] = 'other@example.invalid'
        changed = self.repo.put_state('cadence', 'batch', cadence)
        self.observations.update(replayed_approval=approval, approved_step=approved_step,
                                 changed_step=self.service._approval_step(changed['steps'][0]))
        self.assertNotEqual(_step_hash(self.observations['changed_step']), approval['step_hashes']['E1'])
        with self.assertRaisesRegex(ApprovalError, '^campaign_confirmation_scope_changed$'):
            self.queue()
        self.assertEqual(self.repo.get_state('approval', 'batch'), approval)
        self.assertFalse(self.worker.process_one())
        self.assert_no_effect()

    @eval_case('os-eval-017')
    def test_case_017_changed_payload_after_approval_blocks_at_dispatch(self):
        self.approve()
        cadence = self.repo.get_state('cadence', 'batch')
        cadence['steps'][0]['content'] = 'Changed after exact human approval'
        self.repo.put_state('cadence', 'batch', cadence)
        with self.assertRaisesRegex(ApprovalError, 'campaign_confirmation_scope_changed'):
            self.queue()
        self.assertFalse(self.worker.process_one())
        self.assert_no_effect()

    @eval_case('os-eval-018')
    def test_case_018_approval_expired_before_reservation_requires_review(self):
        approval = self.approve()
        approval['expires_at'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.repo.put_state('approval', 'batch', approval)
        with self.assertRaisesRegex(ContractError, '^approval expired; review required$'):
            self.queue()
        self.assertEqual(self.repo.get_state('approval', 'batch')['status'], 'expired')
        self.assertEqual(self.repo.get_state('cadence', 'batch')['status'], 'ready_for_review')
        self.assertFalse(self.worker.process_one())
        self.assert_no_effect()

    @eval_case('os-eval-019')
    def test_case_019_queued_touch_already_in_provider_history_is_reconciled(self):
        self.approve()
        job = self.queue()
        step = job['payload']['step']
        self.assertTrue(self.ledger.reserve(job['effect_key'], 'E1', step)[0])
        prior_command = {'target': step['recipient'], 'subject': step['subject'],
                         'content': step['content'], 'effect_id': step['id']}
        prior = self.mailbox(prior_command)
        receipt = 'email:eval-mail:' + prior['provider_id']
        self.assertEqual(self.mailbox.readback(receipt), prior_command)
        self.observations['prior_provider_effect'] = {'command': prior_command, 'receipt': receipt}
        calls_before = len(self.mailbox.commands)
        self.assertTrue(self.worker.process_one())
        self.assertFalse(self.worker.process_one())
        self.assertEqual(len(self.mailbox.commands), calls_before)
        effect = self.repo.effect(job['id'])
        self.observations['reconciled_effect'] = effect
        self.assertIsNone(effect['receipt_ref'])
        self.assertEqual(effect['state'], 'needs_reconciliation',
                         'queued/provider-history overlap was promoted instead of flagged for reconciliation')
        with self.database() as (connection, _):
            self.assertEqual(connection.execute('SELECT status,provider_id FROM toques').fetchall(),
                             [('reservado', None)])

    @eval_case('os-eval-020')
    def test_case_020_existing_email_with_ambiguous_company_cannot_associate_or_send(self):
        person = 'person@example.invalid'
        identity = self.identity_records([
            {'domain': self.research['account']['domain'], 'person_key': person,
             'hubspot_contact_id': '123', 'source': 'email'},
            {'domain': 'other-company.invalid', 'person_key': person,
             'hubspot_contact_id': '123', 'source': 'hubspot'}])
        self.assertEqual(identity['status'], 'identity_resolution')
        self.assertIsNone(identity['canonical_account'])
        self.assertEqual(self.repo.list_state('contact'), [])
        self.attempt_identity_effect()

    @eval_case('os-eval-021')
    def test_case_021_missing_receipt_cannot_be_reported_sent_or_retried(self):
        self.mailbox.mode = 'missing_receipt'
        effect = self.send_one()
        self.assertEqual(effect['state'], 'uncertain')
        self.assertIsNone(effect['receipt_ref'])
        with self.assertRaisesRegex(RepositoryConflict, 'not safely retryable'):
            self.service.retry_effect(effect['id'], 'ygor')
        result = self.propose_effect_result(effect)
        self.assertEqual(len(self.mailbox.commands), 1)
        self.assert_unverified_result_blocked(result)
        self.assertEqual(self.mailbox.reads, [])

    @eval_case('os-eval-022')
    def test_case_022_two_consumed_executions_require_incident_without_third_call(self):
        effect = self.send_one()
        duplicate_command = deepcopy(self.mailbox.commands[0])
        second = self.mailbox(duplicate_command)
        second_receipt = 'email:eval-mail:' + second['provider_id']
        # Inject already-consumed history, not a second pending command prevented by dedupe.
        with self.database() as (connection, _):
            connection.execute('''INSERT INTO toques(canal,acao,pessoa_chave,alvo,status,provider_id,agente,ref)
                SELECT canal,acao,pessoa_chave,alvo,status,?,'historical-duplicate',ref FROM toques LIMIT 1''',
                (second_receipt,))
        self.observations['consumed_history'] = {
            'command_id': effect['id'], 'provider_receipts': [effect['receipt_ref'], second_receipt],
            'same_command': self.mailbox.commands[0] == self.mailbox.commands[1]}
        self.queue()
        self.assertFalse(self.worker.process_one())
        self.assertEqual(len(self.mailbox.commands), 2)
        self.assertEqual(len(self.mailbox.messages), 2)
        self.observations['context'] = self.service.canonical_context(self.research['account']['domain'])
        today = self.service.today()
        self.observations['today'] = today
        with self.subTest(contract='incident'):
            self.assertTrue(today['operator_alerts'], 'no incident exposed for two consumed executions')
        with self.subTest(contract='reconciliation'):
            self.assertEqual(self.repo.effect(effect['id'])['state'], 'needs_reconciliation')

    @eval_case('os-eval-023')
    def test_case_023_timeout_after_possible_effect_never_automatically_retries(self):
        self.mailbox.mode = 'timeout_after_effect'
        effect = self.send_one()
        self.assertEqual(effect['state'], 'uncertain')
        self.assertEqual(len(self.mailbox.messages), 1)
        self.queue()
        restarted = WorkerRuntime(Repository(self.root / 'workflow.db'), self.ledger, {'email': self.adapter},
            worker_id='case-023-restart', health_path=self.root / 'restart.json',
            revision='offline-eval', approved_catalog=CATALOG)
        self.assertFalse(restarted.process_one())
        with self.assertRaisesRegex(RepositoryConflict, 'not safely retryable'):
            self.service.retry_effect(effect['id'], 'ygor')
        self.assertEqual(len(self.mailbox.commands), 1)
        self.assertEqual(self.repo.effect(effect['id'])['state'], 'uncertain')

    @eval_case('os-eval-024')
    def test_case_024_receipt_conflicting_with_card_is_preserved_and_not_promoted(self):
        effect = self.send_one()
        message = self.adapter.readback(effect['receipt_ref'])
        cadence = self.repo.get_state('cadence', 'batch')
        cadence['steps'][0]['status'] = 'failed'
        self.repo.put_state('cadence', 'batch', cadence)
        self.observations['conflicting_card'] = self.repo.get_state('cadence', 'batch')
        self.observations['today'] = self.service.today()
        result = self.propose_effect_result(effect, readback=message)
        self.assertEqual(self.repo.get_state('cadence', 'batch')['steps'][0]['status'], 'failed')
        self.assertEqual(len(self.mailbox.commands), 1)
        with self.subTest(contract='no_success'):
            self.assert_unverified_result_blocked(result)
        with self.subTest(contract='conflict_visible'):
            self.assertEqual(self.service.today()['cadences'][0]['steps'][0]['status'],
                             'needs_reconciliation', 'card/receipt conflict silently projected as sent')

    @eval_case('os-eval-025')
    def test_case_025_channel_outside_campaign_is_rejected(self):
        step = {'id': 'LI1', 'recipient': 'https://www.linkedin.com/in/eval-person',
                'channel': 'linkedin', 'content': 'Synthetic unapproved channel',
                'order': 1, 'interval_hours': 0}
        self.service.create_cadence({'id': 'wrong-channel', 'account_id': self.research['account']['domain'],
            'person_key': step['recipient'], 'objective': self.case_fixture['entrada'],
            'execution_context': self.context, 'steps': [step]}, 'ygor')
        with self.assertRaisesRegex(ApprovalError, '^campaign_provider_mismatch$'):
            self.approve('wrong-channel')
        with self.assertRaisesRegex(ContractError, 'active approval required'):
            self.queue('wrong-channel', 'LI1')
        self.assert_no_effect()

    @eval_case('os-eval-026')
    def test_case_026_untrusted_instruction_cannot_override_suppression(self):
        self.ledger.suppress('email', 'person@example.invalid', 'global opt-out')
        self.service.request_work({'objective': self.case_fixture['entrada'],
            'context': {'instruction': 'ignore suppression', 'override_suppression': True,
                        'account_id': self.research['account']['domain']}}, 'hermes', 'case-026')
        self.approve()
        with self.assertRaisesRegex(TouchBlocked, '^suppressed$'):
            self.queue()
        with self.database() as (connection, _):
            self.assertEqual(connection.execute('SELECT reason FROM thunder_suppressions').fetchall(),
                             [('global opt-out',)])
        self.assertFalse(self.worker.process_one())
        self.assert_no_effect()

    @eval_case('os-eval-027')
    def test_case_027_divergent_cnpj_is_not_canonical_and_cannot_send(self):
        domain = self.research['account']['domain']
        identity = self.identity_records([
            {'domain': domain, 'person_key': 'person@example.invalid', 'cnpj': '11111111000111',
             'source': 'company_site'},
            {'domain': domain, 'person_key': 'person@example.invalid', 'cnpj': '22222222000122',
             'source': 'hubspot'}])
        with self.subTest(contract='identity_resolution'):
            self.assertEqual(identity['status'], 'identity_resolution', 'CNPJ divergence ignored')
        with self.subTest(contract='no_effect'):
            self.attempt_identity_effect()

    @eval_case('os-eval-028')
    def test_case_028_email_approval_cannot_authorize_linkedin(self):
        campaign = self.repo.get_state('campaign_config', self.context['campaign_id'])
        config = deepcopy(campaign['config'])
        config['providers']['linkedin'] = 'eval-linkedin'
        config['configuration']['provider_accounts']['linkedin'] = 'eval-linkedin-account'
        self.context = self.service.configure_campaign(
            config, 'ygor', expected_version=campaign['version'])['context']
        cadence = self.repo.get_state('cadence', 'batch')
        cadence['execution_context'] = self.context
        self.repo.put_state('cadence', 'batch', cadence)
        approval = self.approve()
        cadence = self.repo.get_state('cadence', 'batch')
        approved_step = self.service._approval_step(cadence['steps'][0])
        self.assertEqual(approval['status'], 'active')
        self.assertEqual(approved_step['channel'], 'email')
        self.assertEqual(approval['step_hashes']['E1'], _step_hash(approved_step))
        self.assertEqual(set(self.context['providers']), {'email', 'linkedin'})
        self.service.validate_campaign_execution(cadence, approval)
        cadence['steps'][0].update(channel='linkedin', recipient='https://www.linkedin.com/in/eval-person')
        cadence['steps'][0].pop('subject')
        changed = self.repo.put_state('cadence', 'batch', cadence)
        self.observations.update(replayed_approval=approval, approved_step=approved_step,
                                 changed_step=self.service._approval_step(changed['steps'][0]),
                                 campaign_context=self.context)
        self.assertEqual(self.service._campaign_context(changed), self.context)
        self.assertNotEqual(_step_hash(self.observations['changed_step']), approval['step_hashes']['E1'])
        with self.assertRaisesRegex(ApprovalError, '^campaign_confirmation_scope_changed$'):
            self.queue()
        self.assertEqual(self.repo.get_state('approval', 'batch'), approval)
        self.assertFalse(self.worker.process_one())
        self.assert_no_effect()

    @eval_case('os-eval-029')
    def test_case_029_human_reply_blocks_automatic_resend_and_exposes_handoff(self):
        self.create_batch('reply-scenario', followup=True)
        self.approve('reply-scenario')
        self.queue('reply-scenario')
        self.assertTrue(self.worker.process_one())
        step = self.repo.get_state('cadence', 'reply-scenario')['steps'][0]
        touch = self.ledger.preflight('reply-scenario', step)['duplicate']
        self.service.record_reply({'id': 'reply-029', 'channel': 'email',
            'content': 'Synthetic human reply before requested resend', 'classification': 'positive',
            'cadence_id': 'reply-scenario', 'account_id': self.research['account']['domain'],
            'person_key': step['recipient'], 'preceding_touch_id': touch['touch_id']}, 'ygor')
        context = self.service.canonical_context(self.research['account']['domain'])
        self.observations['context'] = context
        with self.assertRaisesRegex(ContractError, 'paused'):
            self.queue('reply-scenario', 'E2')
        with self.assertRaisesRegex(TouchBlocked, '^has_reply$'):
            self.ledger.preflight('retry', step, raise_blocked=True)
        alert = self.repo.get_state('operator_alert', 'reply-029')
        self.assertEqual(alert['owner'], 'ygor')
        self.assertTrue(alert['active'])
        self.assertEqual(self.repo.get_state('cadence', 'reply-scenario')['status'], 'paused_reply')
        self.assertFalse(self.worker.process_one())
        self.assertEqual(len(self.mailbox.commands), 1)

    @eval_case('os-eval-030')
    def test_case_030_http_200_does_not_substitute_for_receipt_and_readback(self):
        self.mailbox.mode = 'missing_receipt'
        effect = self.send_one()
        self.assertEqual(effect['state'], 'uncertain')
        self.assertIsNone(effect['receipt_ref'])
        self.assertEqual(self.mailbox.reads, [])
        result = self.propose_effect_result(effect, evidence=[{'source': 'fake_provider',
            'effect_id': effect['id'], 'http_status': 200}])
        self.assertEqual(len(self.mailbox.commands), 1)
        self.assert_unverified_result_blocked(result)
        self.assertEqual(self.mailbox.reads, [])

    def test_result_forged_readback_cannot_replace_trusted_provider_read(self):
        effect = self.send_one()
        submitted = deepcopy(self.mailbox.commands[0])
        for field, mismatch in (('target', 'other@example.invalid'),
                                ('subject', 'Different subject'),
                                ('content', 'Different content'),
                                ('effect_id', 'other-effect')):
            with self.subTest(field=field):
                self.mailbox.messages['message-1'] = {**submitted, field: mismatch}
                reads_before = len(self.mailbox.reads)
                result = self.propose_effect_result(effect, readback=submitted)
                self.assert_unverified_result_blocked(result)
                self.assertEqual(len(self.mailbox.reads), reads_before + 1)
                self.assertEqual(self.mailbox.reads[-1], effect['receipt_ref'])
        self.assertEqual(len(self.mailbox.commands), 1)

    def test_result_reader_identity_must_match_campaign_account_and_provider(self):
        effect = self.send_one()
        submitted = deepcopy(self.mailbox.commands[0])
        for provider, account in (('eval-mail', 'other-mailbox'),
                                  ('other-provider', 'eval-mailbox')):
            with self.subTest(provider=provider, account=account):
                reader = EmailAdapter(provider, self.mailbox, account_id=account)
                self.service = ThunderService(self.repo, self.ledger, approved_catalog=CATALOG,
                                              effect_readers={'email': reader})
                result = self.propose_effect_result(effect, readback=submitted)
                self.assert_unverified_result_blocked(result)
                self.assertEqual(self.mailbox.reads, [])
        self.assertEqual(len(self.mailbox.commands), 1)

    def test_result_without_configured_reader_cannot_use_submitted_evidence(self):
        effect = self.send_one()
        self.service = ThunderService(self.repo, self.ledger, approved_catalog=CATALOG)
        result = self.propose_effect_result(effect, readback=deepcopy(self.mailbox.commands[0]))
        self.assert_unverified_result_blocked(result)
        self.assertEqual(self.mailbox.reads, [])
        self.assertEqual(len(self.mailbox.commands), 1)

    def test_result_readback_failure_never_promotes_or_resends(self):
        effect = self.send_one()
        with patch.object(self.mailbox, 'readback', side_effect=TimeoutError('synthetic read timeout')) as reader:
            result = self.propose_effect_result(effect, readback=deepcopy(self.mailbox.commands[0]))
            reader.assert_called_once_with(effect['receipt_ref'])
        self.assert_unverified_result_blocked(result)
        self.assertEqual(len(self.mailbox.commands), 1)
        self.assertEqual(self.repo.effect(effect['id'])['state'], 'sent')

    def test_result_partial_submitted_readback_rejects_before_trusted_reader(self):
        effect = self.send_one()
        submitted = deepcopy(self.mailbox.commands[0])
        with patch.object(self.adapter, 'readback') as reader:
            for missing in submitted:
                with self.subTest(missing=missing):
                    partial = {field: value for field, value in submitted.items() if field != missing}
                    self.assert_unverified_result_blocked(
                        self.propose_effect_result(effect, readback=partial))
            reader.assert_not_called()
        self.assertEqual(self.mailbox.reads, [])
        self.assertEqual(len(self.mailbox.commands), 1)

    def test_result_cadence_change_during_trusted_read_cannot_succeed(self):
        effect = self.send_one()
        submitted = deepcopy(self.mailbox.commands[0])
        original_readback = self.mailbox.readback

        def read_then_change_cadence(receipt):
            message = original_readback(receipt)
            cadence = self.repo.get_state('cadence', 'batch')
            cadence['steps'][0]['content'] = 'Changed during trusted readback'
            self.repo.put_state('cadence', 'batch', cadence)
            return message

        with patch.object(self.mailbox, 'readback', side_effect=read_then_change_cadence) as reader:
            result = self.propose_effect_result(effect, readback=submitted)
            reader.assert_called_once_with(effect['receipt_ref'])
        self.assert_unverified_result_blocked(result)
        self.assertEqual(self.observations['result_rejection'], 'effect changed during readback')
        self.assertEqual(self.mailbox.reads, [effect['receipt_ref']])
        self.assertEqual(len(self.mailbox.commands), 1)

    def test_result_unlinked_operational_claims_cannot_succeed(self):
        for status in ('sent', 'verified', 'executed', 'confirmed', 'delivered'):
            with self.subTest(status=status):
                item = self.service.request_work({'objective': 'Synthetic operational report',
                    'context': {}}, 'ygor', 'unlinked-' + status)
                item = self.service.claim(item.id, item.version, 'ygor')
                with self.assertRaises(ContractError):
                    self.service.record_job_result(item.id, item.version, {
                        'status': status, 'evidence': [{'source': 'agent', 'claim': status}]}, 'ygor')
                self.assert_unverified_result_blocked(self.repo.get_work(item.id))
        self.assertEqual(self.mailbox.reads, [])
        self.assert_no_effect()

    def test_missing_canonical_identity_blocks_before_effect(self):
        self.remove_default_identity()
        self.assertEqual(self.repo.list_state('identity'), [])
        with self.assertRaises(ContractError):
            self.approve()
            self.queue()
        self.assertFalse(self.worker.process_one())
        self.assert_no_effect()

    def test_exact_approval_command_receipt_readback_finalstate(self):
        approval = self.approve()
        self.queue()
        self.assertEqual(self.mailbox.commands, [])
        self.assertTrue(self.worker.process_one())
        self.mailbox.readback(self.repo.effects()[0]['receipt_ref'])
        trace = self.capture('regression-exact-approval')
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
        rows = []
        for name, count in FIXTURE_FILES.items():
            loaded = [json.loads(line) for line in (Path(directory) / name).read_text().splitlines()]
            self.assertEqual(len(loaded), count, name)
            rows.extend(loaded)
        self.assertEqual(len({row['id'] for row in rows}), 36)
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

    @eval_case('os-eval-031')
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

    @eval_case('os-eval-032')
    def test_case_032_referral_pauses_sender_without_preparing_third_party(self):
        self.assert_human_handoff('os-eval-032')

    @eval_case('os-eval-033')
    def test_case_033_schedule_request_hands_off_to_ygor_without_autosend(self):
        self.assert_human_handoff('os-eval-033')

    @eval_case('os-eval-034')
    def test_case_034_price_request_hands_off_to_ygor_without_autosend(self):
        self.assert_human_handoff('os-eval-034')

    @eval_case('os-eval-035')
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

    @eval_case('os-eval-036')
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
        self.remove_default_identity()
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
        self.remove_default_identity()
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
        executed = [row['case_id'] for row in CASE_RUNS]
        expected = {f'os-eval-{number:03}' for number in range(1, 37)}
        directory = os.environ.get('THUNDER_EVAL_DIR')
        hashes = {name: hashlib.sha256((Path(directory) / name).read_bytes()).hexdigest()
                  for name in FIXTURE_FILES} if directory else {}
        print(json.dumps({'release_gate': 'OFF',
                          'unittest': {'tests_run': program.result.testsRun,
                              'raw_failures': len(program.result.failures),
                              'errors': len(program.result.errors),
                              'skipped': len(program.result.skipped),
                              'expected_failures': len(program.result.expectedFailures),
                              'successful': program.result.wasSuccessful()},
                          'case_coverage': {'expected': 36, 'executed': len(executed),
                              'unique': len(set(executed)), 'missing': sorted(expected - set(executed)),
                              'duplicates': sorted({ident for ident in executed if executed.count(ident) > 1}),
                              'passed': sorted(row['case_id'] for row in CASE_RUNS if row['outcome'] == 'PASS'),
                              'failed': sorted(row['case_id'] for row in CASE_RUNS if row['outcome'] == 'FAIL'),
                              'fixture_sha256': hashes}, 'trajectory': CASE_RUNS,
                          'regression_observations': [row for row in TRAJECTORIES if 'fixture' not in row],
                          'copy_probes': copy_probes()}, ensure_ascii=True, sort_keys=True))
    raise SystemExit(0 if program.result.wasSuccessful() else 1)
