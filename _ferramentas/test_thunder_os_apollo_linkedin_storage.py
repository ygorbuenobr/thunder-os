import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from thunder_os.db import Repository, RepositoryConflict
from thunder_os.service import ThunderService


def contend(path, queue, gate):
    patcher = patch("thunder_os.db.OFFICIAL_PANEL_DB", Path(path)); patcher.start()
    service = ThunderService(Repository(path))
    gate.wait()
    try:
        service.reserve_apollo_linkedin('company', 'task', 'linkedin_step_connect', 'account')
        queue.put('reserved')
    except RepositoryConflict:
        queue.put('conflict')


def contend_account(path, queue):
    patcher = patch("thunder_os.db.OFFICIAL_PANEL_DB", Path(path)); patcher.start()
    service = ThunderService(Repository(path))
    try:
        with service.apollo_linkedin_account_lock('account'):
            queue.put('acquired')
    except RepositoryConflict:
        queue.put('conflict')


def contend_t4(path, queue, gate):
    patcher = patch('thunder_os.db.OFFICIAL_PANEL_DB', Path(path)); patcher.start()
    repo = Repository(path)
    gate.wait()
    try:
        repo.reserve_apollo_linkedin('company', 't4', 'linkedin_step_connect', 'account',
            text_hash='b' * 64, campaign_version='v1', recipient='lead')
        queue.put('reserved')
    except RepositoryConflict:
        queue.put('conflict')


def contend_completion(path, queue, gate):
    patcher = patch('thunder_os.db.OFFICIAL_PANEL_DB', Path(path)); patcher.start()
    repo = Repository(path)
    gate.wait()
    try:
        repo.claim_apollo_linkedin_completion('company', 't4', 'linkedin_step_connect')
        queue.put('claimed')
    except RepositoryConflict:
        queue.put('conflict')


def contend_canary(path, queue, gate, canary_id):
    patcher = patch('thunder_os.db.OFFICIAL_PANEL_DB', Path(path)); patcher.start()
    repo = Repository(path)
    gate.wait()
    try:
        repo.reserve_apollo_linkedin_with_canary(canary_id, 'company', 'canary-task',
            'linkedin_step_connect', 'account', 'lead', 'c' * 64, 'v1')
        queue.put('reserved')
    except RepositoryConflict:
        queue.put('conflict')


def contend_message_canary(path, queue, gate, canary_id):
    patcher = patch('thunder_os.db.OFFICIAL_PANEL_DB', Path(path)); patcher.start()
    repo = Repository(path)
    gate.wait()
    try:
        repo.reserve_apollo_linkedin_with_canary(canary_id, 'company', 'message-task',
            'linkedin_step_message', 'account', 'lead', 'c' * 64, 'v1',
            member_urn='urn:li:fsd_profile:member', approval_id='approval1')
        queue.put('reserved')
    except RepositoryConflict:
        queue.put('conflict')


class ApolloLinkedinStorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'painel.db')
        Path(self.path).touch()
        self.patcher = patch("thunder_os.db.OFFICIAL_PANEL_DB", Path(self.path))
        self.patcher.start(); self.addCleanup(self.patcher.stop)
        self.service = ThunderService(Repository(self.path))

    def test_reservation_is_unique_and_uncertain_never_reopened(self):
        self.service.reserve_apollo_linkedin('company', 'task', 'linkedin_step_connect', 'account')
        self.service.finish_apollo_linkedin('company', 'task', 'linkedin_step_connect', 'uncertain', 'timeout')
        with self.assertRaises(RepositoryConflict):
            self.service.reserve_apollo_linkedin('company', 'task', 'linkedin_step_connect', 'other')
        with self.assertRaises(RepositoryConflict):
            self.service.finish_apollo_linkedin('company', 'task', 'linkedin_step_connect', 'confirmed')
        self.assertEqual(self.service.list_apollo_linkedin_reservations()[0]['state'], 'uncertain')
        self.service.reserve_apollo_linkedin('company', 'task', 'linkedin_step_message', 'account')
        self.service.reserve_apollo_linkedin('other', 'task', 'linkedin_step_connect', 'account')

    def test_canary_consumed_with_reservation_and_never_reused(self):
        repo = self.service.repository
        auth = repo.issue_apollo_linkedin_canary('company', 'canary-task', 'account', 'lead', 'c' * 64, 'ygor')
        row = repo.reserve_apollo_linkedin_with_canary(auth['id'], 'company', 'canary-task',
            'linkedin_step_connect', 'account', 'lead', 'c' * 64, 'v1')
        self.assertEqual((row['state'], row['recipient']), ('reserved', 'lead'))
        self.assertEqual(repo.get_state('apollo_linkedin_canary', auth['id'])['state'], 'consumed')
        with self.assertRaises(RepositoryConflict):
            repo.reserve_apollo_linkedin_with_canary(auth['id'], 'company', 'canary-task',
                'linkedin_step_connect', 'account', 'lead', 'c' * 64, 'v1')

    def test_canary_mismatch_or_duplicate_does_not_consume(self):
        repo = self.service.repository
        auth = repo.issue_apollo_linkedin_canary('company', 'canary-task', 'account', 'lead', 'c' * 64, 'ygor')
        for company, account, recipient, digest in [('other','account','lead','c'*64),
                ('company','other','lead','c'*64), ('company','account','other','c'*64),
                ('company','account','lead','d'*64)]:
            with self.subTest(company=company,account=account,recipient=recipient,digest=digest), self.assertRaises(RepositoryConflict):
                repo.reserve_apollo_linkedin_with_canary(auth['id'], company, 'canary-task',
                    'linkedin_step_connect', account, recipient, digest, 'v1')
        repo.reserve_apollo_linkedin('company', 'canary-task', 'linkedin_step_connect', 'account')
        with self.assertRaises(RepositoryConflict):
            repo.reserve_apollo_linkedin_with_canary(auth['id'], 'company', 'canary-task',
                'linkedin_step_connect', 'account', 'lead', 'c' * 64, 'v1')
        self.assertEqual(repo.get_state('apollo_linkedin_canary', auth['id'])['state'], 'issued')

    def test_expired_canary_fails_without_reserving(self):
        import json
        repo = self.service.repository
        auth = repo.issue_apollo_linkedin_canary('company', 'canary-task', 'account', 'lead', 'c' * 64, 'ygor')
        with repo.transaction() as con:
            row = con.execute('SELECT payload FROM thunder_state WHERE kind=? AND entity_id=?',
                              ('apollo_linkedin_canary', auth['id'])).fetchone()
            payload = json.loads(row['payload'])
            payload.update(issued_at='2020-01-01T00:00:00+00:00', expires_at='2020-01-01T00:30:00+00:00')
            con.execute('UPDATE thunder_state SET payload=? WHERE kind=? AND entity_id=?',
                        (json.dumps(payload), 'apollo_linkedin_canary', auth['id']))
        with self.assertRaises(RepositoryConflict):
            repo.reserve_apollo_linkedin_with_canary(auth['id'], 'company', 'canary-task',
                'linkedin_step_connect', 'account', 'lead', 'c' * 64, 'v1')
        self.assertEqual(repo.list_apollo_linkedin_reservations(), [])

    def test_canary_never_authorizes_protected_fixture(self):
        repo = self.service.repository
        with self.assertRaises(ValueError):
            repo.issue_apollo_linkedin_canary('company', '6aea584a2b8c4b0020f93935',
                                              'account', 'lead', 'c'*64, 'ygor')
        with self.assertRaises(ValueError):
            repo.issue_apollo_linkedin_canary('company', '6aea584a2b8c4b0020f93935',
                'account', 'lead', 'c'*64, 'ygor', action_type='linkedin_step_message',
                member_urn='urn:li:fsd_profile:member', campaign_version='v1', approval_id='approval1')
        self.assertEqual(repo.list_apollo_linkedin_reservations(), [])

    def test_message_canary_binds_every_identifier_and_consumes_atomically(self):
        repo = self.service.repository
        auth = repo.issue_apollo_linkedin_canary('company', 'message-task', 'account', 'lead',
            'c' * 64, 'ygor', action_type='linkedin_step_message',
            member_urn='urn:li:fsd_profile:member', campaign_version='v1', approval_id='approval1')
        args = ['company', 'message-task', 'linkedin_step_message', 'account', 'lead', 'c' * 64, 'v1']
        for index, replacement in ((0, 'other'), (1, 'other'), (2, 'linkedin_step_connect'),
                                   (3, 'other'), (4, 'other'), (5, 'd' * 64), (6, 'v2')):
            altered = args.copy(); altered[index] = replacement
            with self.subTest(index=index), self.assertRaises(RepositoryConflict):
                repo.reserve_apollo_linkedin_with_canary(auth['id'], *altered,
                    member_urn='urn:li:fsd_profile:member', approval_id='approval1')
        for urn, approval in [('urn:li:fsd_profile:other', 'approval1'),
                              ('urn:li:fsd_profile:member', 'approval2')]:
            with self.subTest(urn=urn, approval=approval), self.assertRaises(RepositoryConflict):
                repo.reserve_apollo_linkedin_with_canary(auth['id'], *args,
                    member_urn=urn, approval_id=approval)
        self.assertEqual(repo.get_state('apollo_linkedin_canary', auth['id'])['state'], 'issued')
        self.assertEqual(repo.list_apollo_linkedin_reservations(), [])
        row = repo.reserve_apollo_linkedin_with_canary(auth['id'], *args,
            member_urn='urn:li:fsd_profile:member', approval_id='approval1')
        self.assertEqual(row['state'], 'reserved')
        self.assertEqual(repo.get_state('apollo_linkedin_canary', auth['id'])['state'], 'consumed')

    def test_message_canary_requires_complete_binding_and_rejects_legacy_invitation(self):
        repo = self.service.repository
        for kwargs in ({}, {'member_urn': 'urn:li:fsd_profile:member', 'campaign_version': 'v1'},
                       {'member_urn': 'bad', 'campaign_version': 'v1', 'approval_id': 'approval1'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                repo.issue_apollo_linkedin_canary('company', 'message-task', 'account', 'lead',
                    'c' * 64, 'ygor', action_type='linkedin_step_message', **kwargs)
        legacy = repo.issue_apollo_linkedin_canary('company', 'message-task', 'account', 'lead', 'c'*64, 'ygor')
        with self.assertRaises(RepositoryConflict):
            repo.reserve_apollo_linkedin_with_canary(legacy['id'], 'company', 'message-task',
                'linkedin_step_message', 'account', 'lead', 'c'*64, 'v1',
                member_urn='urn:li:fsd_profile:member', approval_id='approval1')
        self.assertEqual(repo.get_state('apollo_linkedin_canary', legacy['id'])['state'], 'issued')

    def test_message_canary_two_processes_only_one_reserves(self):
        repo = self.service.repository
        auth = repo.issue_apollo_linkedin_canary('company', 'message-task', 'account', 'lead', 'c'*64,
            'ygor', action_type='linkedin_step_message', member_urn='urn:li:fsd_profile:member',
            campaign_version='v1', approval_id='approval1')
        ctx = multiprocessing.get_context('spawn')
        queue, gate = ctx.Queue(), ctx.Event()
        processes = [ctx.Process(target=contend_message_canary, args=(self.path, queue, gate, auth['id']))
                     for _ in range(2)]
        for process in processes: process.start()
        gate.set()
        results = [queue.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(15)
            self.assertEqual(process.exitcode, 0)
        self.assertCountEqual(results, ['reserved', 'conflict'])
        self.assertEqual(repo.get_state('apollo_linkedin_canary', auth['id'])['state'], 'consumed')

    def test_uncertain_message_never_retried_even_after_release(self):
        repo = self.service.repository
        kwargs = dict(action_type='linkedin_step_message', member_urn='urn:li:fsd_profile:member',
                      campaign_version='v1', approval_id='approval1')
        auth = repo.issue_apollo_linkedin_canary('company', 'message-task', 'account', 'lead', 'c'*64,
                                                'ygor', **kwargs)
        args = ('company', 'message-task', 'linkedin_step_message', 'account', 'lead', 'c'*64, 'v1')
        repo.reserve_apollo_linkedin_with_canary(auth['id'], *args,
            member_urn=kwargs['member_urn'], approval_id=kwargs['approval_id'])
        repo.finish_apollo_linkedin('company', 'message-task', 'linkedin_step_message', 'uncertain', 'timeout')
        repo.resolve_apollo_linkedin_uncertain('company', 'message-task', 'linkedin_step_message',
                                               'release', 'operator reviewed', 'ygor')
        second = repo.issue_apollo_linkedin_canary('company', 'message-task', 'account', 'lead', 'c'*64,
                                                  'ygor', **kwargs)
        with self.assertRaises(RepositoryConflict):
            repo.reserve_apollo_linkedin_with_canary(second['id'], *args,
                member_urn=kwargs['member_urn'], approval_id=kwargs['approval_id'])
        self.assertEqual(repo.get_state('apollo_linkedin_canary', second['id'])['state'], 'issued')

    def test_two_processes_cannot_consume_same_canary(self):
        repo = self.service.repository
        auth = repo.issue_apollo_linkedin_canary('company', 'canary-task', 'account', 'lead', 'c' * 64, 'ygor')
        ctx = multiprocessing.get_context('spawn')
        queue, gate = ctx.Queue(), ctx.Event()
        processes = [ctx.Process(target=contend_canary, args=(self.path, queue, gate, auth['id'])) for _ in range(2)]
        for process in processes: process.start()
        gate.set()
        results = [queue.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(15)
            self.assertEqual(process.exitcode, 0)
        self.assertCountEqual(results, ['reserved', 'conflict'])
        self.assertEqual(repo.get_state('apollo_linkedin_canary', auth['id'])['state'], 'consumed')

    def test_uncertain_decision_audited_and_never_sends_or_releases_reservation(self):
        repo = self.service.repository
        repo.reserve_apollo_linkedin('company', 't4', 'linkedin_step_connect', 'account')
        repo.finish_apollo_linkedin('company', 't4', 'linkedin_step_connect', 'uncertain', 'timeout')
        with self.assertRaises(ValueError):
            repo.resolve_apollo_linkedin_uncertain('company', 't4', 'linkedin_step_connect', 'close', ' ', 'ygor')
        with patch.object(repo, 'append_event', side_effect=RuntimeError('disk')):
            with self.assertRaises(RuntimeError):
                repo.resolve_apollo_linkedin_uncertain('company', 't4', 'linkedin_step_connect', 'release', 'checked', 'ygor')
        result = repo.resolve_apollo_linkedin_uncertain('company', 't4', 'linkedin_step_connect', 'release', 'checked', 'ygor')
        self.assertEqual(result, {'state': 'uncertain', 'decision': 'release', 'execution_enabled': False})
        self.assertEqual(repo.get_apollo_linkedin_reservation('company', 't4', 'linkedin_step_connect')['state'], 'uncertain')
        with repo._connect() as con:
            events = con.execute("SELECT type,payload FROM thunder_events WHERE type='apollo_linkedin_uncertain_resolved'").fetchall()
        self.assertEqual(len(events), 1)
        self.assertIn('checked', events[0]['payload'])
        with self.assertRaises(RepositoryConflict):
            repo.resolve_apollo_linkedin_uncertain('company', 't4', 'linkedin_step_connect', 'close', 'checked', 'ygor')

    def test_release_needs_fresh_canary_and_reopens_same_reservation_once(self):
        repo = self.service.repository
        repo.reserve_apollo_linkedin('company', 'canary-task', 'linkedin_step_connect', 'account',
                                    text_hash='c'*64, campaign_version='v1', recipient='lead')
        repo.finish_apollo_linkedin('company', 'canary-task', 'linkedin_step_connect', 'uncertain', 'timeout')
        old_auth = repo.issue_apollo_linkedin_canary('company', 'canary-task', 'account', 'lead', 'c'*64, 'ygor')
        result = repo.resolve_apollo_linkedin_uncertain('company', 'canary-task', 'linkedin_step_connect',
                                                         'release', 'Provider checked', 'ygor')
        self.assertFalse(result['execution_enabled'])
        self.assertEqual(repo.get_apollo_linkedin_reservation('company', 'canary-task',
                                                               'linkedin_step_connect')['state'], 'uncertain')
        with self.assertRaises(RepositoryConflict):
            repo.reserve_apollo_linkedin_with_canary(old_auth['id'], 'company', 'canary-task',
                'linkedin_step_connect', 'account', 'lead', 'c'*64, 'v1')
        self.assertEqual(repo.get_state('apollo_linkedin_canary', old_auth['id'])['state'], 'issued')
        auth = repo.issue_apollo_linkedin_canary('company', 'canary-task', 'account', 'lead', 'c'*64, 'ygor')
        with self.assertRaises(RepositoryConflict):
            repo.reserve_apollo_linkedin_with_canary(auth['id'], 'company', 'canary-task',
                'linkedin_step_connect', 'account', 'lead', 'c'*64, 'v2')
        self.assertEqual(repo.get_state('apollo_linkedin_canary', auth['id'])['state'], 'issued')
        result = repo.reserve_apollo_linkedin_with_canary(auth['id'], 'company', 'canary-task',
            'linkedin_step_connect', 'account', 'lead', 'c'*64, 'v1')
        self.assertEqual(result['state'], 'reserved')
        self.assertEqual(len(repo.list_apollo_linkedin_reservations()), 1)
        self.assertEqual(repo.get_state('apollo_linkedin_canary', auth['id'])['state'], 'consumed')
        with self.assertRaises(RepositoryConflict):
            repo.reserve_apollo_linkedin_with_canary(auth['id'], 'company', 'canary-task',
                'linkedin_step_connect', 'account', 'lead', 'c'*64, 'v1')
        # A second uncertain outcome cannot reuse the prior release.
        repo.finish_apollo_linkedin('company', 'canary-task', 'linkedin_step_connect', 'uncertain', 'timeout')
        fresh = repo.issue_apollo_linkedin_canary('company', 'canary-task', 'account', 'lead', 'c'*64, 'ygor')
        with self.assertRaises(RepositoryConflict):
            repo.reserve_apollo_linkedin_with_canary(fresh['id'], 'company', 'canary-task',
                'linkedin_step_connect', 'account', 'lead', 'c'*64, 'v1')
        self.assertEqual(repo.get_state('apollo_linkedin_canary', fresh['id'])['state'], 'issued')
        # A fresh human decision applies only to the new uncertain outcome.
        repo.resolve_apollo_linkedin_uncertain('company', 'canary-task', 'linkedin_step_connect',
                                                'close', 'Confirmed no retry', 'ygor')
        with self.assertRaises(RepositoryConflict):
            repo.reserve_apollo_linkedin_with_canary(fresh['id'], 'company', 'canary-task',
                'linkedin_step_connect', 'account', 'lead', 'c'*64, 'v1')

    def test_two_processes_compete_for_one_uncertain_release(self):
        repo = self.service.repository
        repo.reserve_apollo_linkedin('company', 'canary-task', 'linkedin_step_connect', 'account',
                                    text_hash='c'*64, campaign_version='v1', recipient='lead')
        repo.finish_apollo_linkedin('company', 'canary-task', 'linkedin_step_connect', 'uncertain', 'timeout')
        repo.resolve_apollo_linkedin_uncertain('company', 'canary-task', 'linkedin_step_connect',
                                                'release', 'Checked once', 'ygor')
        auth = repo.issue_apollo_linkedin_canary('company', 'canary-task', 'account', 'lead', 'c'*64, 'ygor')
        ctx = multiprocessing.get_context('spawn')
        queue, gate = ctx.Queue(), ctx.Event()
        processes = [ctx.Process(target=contend_canary, args=(self.path, queue, gate, auth['id'])) for _ in range(2)]
        for process in processes: process.start()
        gate.set()
        results = [queue.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(15)
            self.assertEqual(process.exitcode, 0)
        self.assertCountEqual(results, ['reserved', 'conflict'])
        self.assertEqual(repo.get_state('apollo_linkedin_canary', auth['id'])['state'], 'consumed')

    def test_t4_evidence_is_persisted_and_reconciliation_is_cas(self):
        repo = self.service.repository
        digest = 'a' * 64
        row = repo.reserve_apollo_linkedin('company', 't4', 'linkedin_step_connect', 'account',
            text_hash=digest, campaign_version='v1', recipient='lead-profile')
        self.assertEqual((row['text_hash'], row['campaign_version'], row['recipient']),
                         (digest, 'v1', 'lead-profile'))
        with self.assertRaises(ValueError):
            repo.finish_apollo_linkedin('company', 't4', 'linkedin_step_connect', 'confirmed')
        receipt = {'provider_id': 'invite-123'}
        repo.finish_apollo_linkedin('company', 't4', 'linkedin_step_connect', 'confirmed',
                                    receipt=receipt, apollo_status='pending')
        row = repo.get_apollo_linkedin_reservation('company', 't4', 'linkedin_step_connect')
        self.assertEqual(row['receipt'], receipt)
        self.assertEqual(row['apollo_status'], 'pending')
        with self.assertRaises(RepositoryConflict):
            repo.reconcile_apollo_linkedin('company', 't4', 'linkedin_step_connect')
        repo.claim_apollo_linkedin_completion('company', 't4', 'linkedin_step_connect')
        self.assertEqual(repo.get_apollo_linkedin_reservation('company', 't4', 'linkedin_step_connect')['apollo_status'], 'attempted')
        with self.assertRaises(RepositoryConflict):
            repo.claim_apollo_linkedin_completion('company', 't4', 'linkedin_step_connect')
        repo.reconcile_apollo_linkedin('company', 't4', 'linkedin_step_connect')
        self.assertEqual(repo.get_apollo_linkedin_reservation('company', 't4', 'linkedin_step_connect')['apollo_status'], 'confirmed')
        with self.assertRaises(RepositoryConflict):
            repo.reconcile_apollo_linkedin('company', 't4', 'linkedin_step_connect')

    def test_t4_partial_bindings_fail_closed(self):
        repo = self.service.repository
        with self.assertRaises(ValueError):
            repo.reserve_apollo_linkedin('company', 't4', 'linkedin_step_connect', 'account', text_hash='a' * 64)
        self.assertEqual(repo.list_apollo_linkedin_reservations(), [])

    def test_t4_confirm_rejects_nonreceipt_or_wrong_recipient(self):
        repo = self.service.repository
        repo.reserve_apollo_linkedin('company', 't4', 'linkedin_step_connect', 'account',
            text_hash='a' * 64, campaign_version=1, recipient='lead-profile')
        for invalid in ({}, {'publicIdentifier': 'wrong'}, {'provider_id': ''}, {'status': 'accepted'}):
            with self.subTest(receipt=invalid), self.assertRaises(ValueError):
                repo.finish_apollo_linkedin('company', 't4', 'linkedin_step_connect', 'confirmed',
                                             receipt=invalid, apollo_status='pending')
        self.assertEqual(repo.get_apollo_linkedin_reservation('company', 't4', 'linkedin_step_connect')['state'], 'reserved')

    def test_additive_migration_preserves_old_reservation(self):
        import sqlite3
        old = Path(self.tmp.name) / 'old.db'
        with sqlite3.connect(old) as con:
            con.execute("CREATE TABLE apollo_linkedin_reservations (company TEXT NOT NULL, task_id TEXT NOT NULL, "
                "action_type TEXT NOT NULL, account_id TEXT NOT NULL, state TEXT NOT NULL "
                "CHECK(state IN ('reserved','confirmed','rejected','uncertain')), reason TEXT, "
                "created_at TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(company,task_id,action_type))")
            con.execute("INSERT INTO apollo_linkedin_reservations VALUES ('c','t','linkedin_step_connect','a','uncertain','timeout','x','x')")
        with patch('thunder_os.db.OFFICIAL_PANEL_DB', old):
            repo = Repository(old)
            repo.migrate()
            row = repo.get_apollo_linkedin_reservation('c', 't', 'linkedin_step_connect')
            self.assertEqual((row['state'], row['reason'], row['receipt'], row['apollo_status']),
                             ('uncertain', 'timeout', None, None))

    def test_independent_processes_cannot_reserve_same_identity(self):
        ctx = multiprocessing.get_context('spawn')
        queue, gate = ctx.Queue(), ctx.Event()
        processes = [ctx.Process(target=contend, args=(self.path, queue, gate)) for _ in range(2)]
        for process in processes:
            process.start()
        gate.set()
        results = [queue.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(15)
            self.assertEqual(process.exitcode, 0)
        self.assertCountEqual(results, ['reserved', 'conflict'])

    def test_t4_independent_processes_share_one_reservation(self):
        ctx = multiprocessing.get_context('spawn')
        queue, gate = ctx.Queue(), ctx.Event()
        processes = [ctx.Process(target=contend_t4, args=(self.path, queue, gate)) for _ in range(2)]
        for process in processes:
            process.start()
        gate.set()
        results = [queue.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(15)
            self.assertEqual(process.exitcode, 0)
        self.assertCountEqual(results, ['reserved', 'conflict'])
        row = self.service.repository.get_apollo_linkedin_reservation('company', 't4', 'linkedin_step_connect')
        self.assertEqual((row['text_hash'], row['campaign_version'], row['recipient']), ('b' * 64, 'v1', 'lead'))

    def test_t4_two_processes_cannot_claim_same_apollo_completion(self):
        repo = self.service.repository
        repo.reserve_apollo_linkedin('company', 't4', 'linkedin_step_connect', 'account',
            text_hash='b' * 64, campaign_version='v1', recipient='lead')
        repo.finish_apollo_linkedin('company', 't4', 'linkedin_step_connect', 'confirmed',
                                    receipt={'provider_id': 'provider-1'}, apollo_status='pending')
        ctx = multiprocessing.get_context('spawn')
        queue, gate = ctx.Queue(), ctx.Event()
        processes = [ctx.Process(target=contend_completion, args=(self.path, queue, gate)) for _ in range(2)]
        for process in processes:
            process.start()
        gate.set()
        results = [queue.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(15)
            self.assertEqual(process.exitcode, 0)
        self.assertCountEqual(results, ['claimed', 'conflict'])
        self.assertEqual(repo.get_apollo_linkedin_reservation('company', 't4', 'linkedin_step_connect')['apollo_status'], 'attempted')

    def test_account_lock_is_shared_by_independent_repositories(self):
        other = ThunderService(Repository(self.path))
        with self.service.apollo_linkedin_account_lock('account'):
            with self.assertRaises(RepositoryConflict):
                with other.apollo_linkedin_account_lock('account'):
                    self.fail('account lock was not exclusive')
            with other.apollo_linkedin_account_lock('different'):
                pass
        with other.apollo_linkedin_account_lock('account'):
            pass

    def test_account_lock_is_shared_across_processes(self):
        ctx = multiprocessing.get_context('spawn')
        queue = ctx.Queue()
        with self.service.apollo_linkedin_account_lock('account'):
            process = ctx.Process(target=contend_account, args=(self.path, queue))
            process.start()
            self.assertEqual(queue.get(timeout=15), 'conflict')
            process.join(15)
            self.assertEqual(process.exitcode, 0)

    def test_expired_lease_is_recovered_only_after_owner_exits(self):
        import sqlite3
        ctx = multiprocessing.get_context('spawn')
        queue = ctx.Queue()
        with self.service.apollo_linkedin_account_lock('account'):
            with sqlite3.connect(self.path) as con:
                con.execute("UPDATE apollo_linkedin_account_locks SET lease_until=? WHERE account_id=?",
                            ('2000-01-01T00:00:00+00:00', 'account'))
            process = ctx.Process(target=contend_account, args=(self.path, queue))
            process.start()
            self.assertEqual(queue.get(timeout=15), 'conflict')
            process.join(15)
            self.assertEqual(process.exitcode, 0)
        with sqlite3.connect(self.path) as con:
            con.execute('INSERT INTO apollo_linkedin_account_locks (account_id, owner, created_at, lease_until) VALUES (?,?,?,?)',
                        ('account', 'dead-owner', '2000-01-01T00:00:00+00:00', '2000-01-01T00:01:00+00:00'))
        with self.service.apollo_linkedin_account_lock('account'):
            self.assertEqual(len(self.service.list_apollo_linkedin_account_locks()), 1)

    def test_cleanup_failure_does_not_mask_original_exception(self):
        import sqlite3
        with self.assertLogs('thunder_os.db', level='ERROR'):
            with self.assertRaisesRegex(ValueError, 'original error'):
                with self.service.apollo_linkedin_account_lock('account'):
                    with sqlite3.connect(self.path) as con:
                        con.execute('DELETE FROM apollo_linkedin_account_locks WHERE account_id=?', ('account',))
                    raise ValueError('original error')

    def test_unknown_or_malformed_lease_fails_closed(self):
        import sqlite3
        for value in (None, '', '0', '2000-01-01T00:00:00'):
            with self.subTest(value=value):
                with sqlite3.connect(self.path) as con:
                    con.execute('DELETE FROM apollo_linkedin_account_locks')
                    con.execute('INSERT INTO apollo_linkedin_account_locks (account_id, owner, created_at, lease_until) VALUES (?,?,?,?)',
                                ('account', 'unknown-owner', '2000-01-01T00:00:00+00:00', value))
                with self.assertRaises(RepositoryConflict):
                    with self.service.apollo_linkedin_account_lock('account'):
                        self.fail('unverifiable lease was recovered')

    def test_configured_store_requires_existing_absolute_database(self):
        import os
        missing = Path(self.tmp.name) / 'not-created.db'
        with patch.dict(os.environ, {'THUNDER_APOLLO_LI_OFFICIAL_DB': str(missing)}):
            with self.assertRaisesRegex(RepositoryConflict, 'unavailable'):
                Repository(missing)
            self.assertFalse(missing.exists())
            missing.touch()
            configured = Repository(missing)
            self.assertTrue(configured.apollo_linkedin_store_ready())
            with self.assertRaises(RepositoryConflict):
                self.service.apollo_linkedin_store_ready()
        with patch.dict(os.environ, {'THUNDER_APOLLO_LI_OFFICIAL_DB': 'relative.db'}):
            with self.assertRaisesRegex(RepositoryConflict, 'configuration invalid'):
                self.service.apollo_linkedin_store_ready()
            # Invalid T3 configuration disables only T3; unrelated repositories still open.
            unrelated = Repository(Path(self.tmp.name) / 'unrelated.db')
            with self.assertRaisesRegex(RepositoryConflict, 'configuration invalid'):
                unrelated.apollo_linkedin_store_ready()

    def test_wrong_existing_database_refuses(self):
        other = Path(self.tmp.name) / 'other.db'
        other.touch()
        other_repo = Repository(other)
        import sqlite3
        with sqlite3.connect(other) as con:
            self.assertIsNone(con.execute("SELECT name FROM sqlite_master WHERE name='apollo_linkedin_reservations'").fetchone())
        self.service.repository.path = str(other)
        with self.assertRaises(RepositoryConflict):
            self.service.apollo_linkedin_store_ready()

    def test_missing_or_unmigrated_store_refuses(self):
        self.service.repository.path = str(Path(self.tmp.name) / 'missing.db')
        with self.assertRaises(RepositoryConflict):
            self.service.apollo_linkedin_store_ready()
        self.assertFalse(Path(self.service.repository.path).exists())
        self.service.repository.path = ':memory:'
        with self.assertRaises(RepositoryConflict):
            self.service.apollo_linkedin_store_ready()


if __name__ == '__main__':
    unittest.main()
