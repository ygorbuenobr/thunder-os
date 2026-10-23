"""Bounded one-step authorization checks, using isolated existing journey fixtures.

Proofs: exact subset/no effects, prior authority preserved on refusal, unchanged
whole-pack contract, stale/edited/context/expiry/suppression denial, fake receipt,
atomic discard, immutable confirmed steps, and external executor isolation.
Independent review belongs to the main agent; no provider/network is authorized.
"""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import threading
import unittest
from unittest.mock import patch

import test_thunder_os_eval_trajectory as trajectory
from thunder_os.approvals import ApprovalError, _step_hash
from thunder_os.db import RepositoryConflict
from thunder_os.knowledge import KnowledgeStore
from thunder_os.models import ContractError, payload_hash


class StepApprovalTests(unittest.TestCase):
    def setUp(self):
        self.fixture = trajectory.EvalTrajectoryTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service, self.repo = self.fixture.service, self.fixture.repo
        self.cadence = self.fixture.create_batch('two-steps', followup=True)
        _, self.cadence = self.source()

    def review(self, step='E1'):
        return self.service.review_step('two-steps', step, 'ygor')

    def approve(self, step='E1', review=None, **overrides):
        review = review or self.review(step)
        args = {key: review[key] for key in ('expected_version', 'review_hash', 'execution_confirmation')}
        args.update({'step_ids': [step], **overrides})
        return self.service.approve_cadence('two-steps', 'ygor', **args)

    def current(self):
        return self.repo.get_state('cadence', 'two-steps')

    def source(self, text='status: validado\nSynthetic canonical source\n'):
        store = KnowledgeStore(self.fixture.root/'context-brain')
        store.root.mkdir(exist_ok=True)
        (store.root/'thunder').mkdir(exist_ok=True)
        (store.root/'thunder/catalog.json').write_text(json.dumps(trajectory.CATALOG))
        path = store.root/'source.md'
        path.write_text(text)
        self.service.knowledge_store = store
        self.fixture.worker.authorization_service.knowledge_store = store
        cadence = self.repo.put_state('cadence', 'two-steps', {**self.current(),
            'context_sources': [{'path': 'source.md', 'sha256': store.read('source.md').content_hash}]})
        return path, cadence

    def test_exact_subset_review_approval_and_no_effects(self):
        before = self.repo.list_state('cadence')
        with self.repo.transaction() as connection:
            outbox_before = connection.execute('SELECT COUNT(*) FROM thunder_outbox').fetchone()[0]
        review = self.review()
        self.assertEqual(set(review), {'cadence_id', 'step_id', 'expected_version', 'review_hash',
            'execution_confirmation', 'content', 'subject', 'context_hash', 'can_approve', 'blockers'})
        self.assertTrue(review['can_approve'])
        self.assertEqual(review['content'], self.cadence['steps'][0]['content'])
        expected = {'E1': _step_hash(self.service._approval_step(self.cadence['steps'][0]))}
        self.assertEqual(review['execution_confirmation']['scope_hash'], payload_hash(expected))
        self.assertEqual(self.repo.list_state('cadence'), before)
        self.assertIsNone(self.repo.get_state('approval', 'two-steps'))
        approval = self.approve(review=review)
        self.assertEqual(approval['step_hashes'], expected)
        self.assertEqual(approval['step_ids'], ['E1'])
        self.assertEqual(approval['status'], 'active')
        self.assertEqual([s['status'] for s in self.current()['steps']], ['approved', 'draft'])
        with self.assertRaisesRegex(ContractError, 'step is not approved'):
            self.service.dispatch_step('two-steps', 'E2', 'ygor')
        self.fixture.assert_no_effect()
        with self.repo.transaction() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM thunder_outbox').fetchone()[0], outbox_before)

    def test_concurrent_clicks_commit_exactly_one_authority(self):
        reviews = [self.review('E1'), self.review('E2')]
        barrier = threading.Barrier(2)
        def click(review):
            barrier.wait(timeout=3)
            try:
                return self.approve(review['step_id'], review=review)
            except RepositoryConflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(click, reviews))
        accepted = [result for result in results if result is not None]
        self.assertEqual(len(accepted), 1)
        approval = self.repo.get_state('approval', 'two-steps')
        self.assertEqual(approval, accepted[0])
        self.assertEqual(len(approval['step_ids']), 1)
        self.assertEqual(sum(s['status'] == 'approved' for s in self.current()['steps']), 1)
        self.fixture.assert_no_effect()

    def test_approval_rollback_keeps_both_records_unchanged(self):
        original = self.repo._put_state_in
        def fail_cadence(con, kind, *args, **kwargs):
            if kind == 'cadence':
                raise RuntimeError('synthetic approval rollback')
            return original(con, kind, *args, **kwargs)
        with patch.object(self.repo, '_put_state_in', side_effect=fail_cadence):
            with self.assertRaisesRegex(RuntimeError, 'synthetic approval rollback'):
                self.approve()
        self.assertIsNone(self.repo.get_state('approval', 'two-steps'))
        self.assertEqual(self.current(), self.cadence)
        self.fixture.assert_no_effect()

    def test_unavailable_suppression_reader_fails_closed(self):
        self.service.ledger = None
        review = self.review()
        self.assertFalse(review['can_approve'])
        self.assertIn('touch_ledger_unavailable', review['blockers'])
        exact = self.service.campaign_execution_review(self.cadence, step_ids=['E1'])
        with self.assertRaisesRegex(ApprovalError, 'touch_ledger_unavailable'):
            self.approve(execution_confirmation=exact)
        self.assertIsNone(self.repo.get_state('approval', 'two-steps'))

    def test_second_approval_fails_closed_and_preserves_first_authority(self):
        first = self.approve()
        cadence = self.current()
        review = self.review('E2')
        self.assertFalse(review['can_approve'])
        self.assertEqual(review['blockers'], ['prior_approval_review_required'])
        exact = self.service.campaign_execution_review(cadence, step_ids=['E2'])
        with self.assertRaisesRegex(ApprovalError, '^prior_approval_review_required$'):
            self.approve('E2', execution_confirmation=exact)
        self.assertEqual(self.repo.get_state('approval', 'two-steps'), first)
        self.assertEqual(self.current(), cadence)
        self.service.dispatch_step('two-steps', 'E1', 'ygor')
        self.assertEqual(len(self.repo.effects()), 1)

    def test_whole_pack_default_is_backward_compatible(self):
        legacy = {key: value for key, value in self.current().items() if key != 'context_sources'}
        self.cadence = self.repo.put_state('cadence', 'two-steps', legacy)
        request = self.service.campaign_execution_review(self.cadence)
        approval = self.service.approve_cadence('two-steps', 'ygor', expected_version=self.cadence['version'],
            review_hash=self.service.cadence_review_hash(self.cadence), execution_confirmation=request)
        self.assertEqual(approval['step_ids'], ['E1', 'E2'])
        self.service.validate_campaign_execution(self.current(), approval)
        self.assertEqual([s['status'] for s in self.current()['steps']], ['approved', 'approved'])

    def test_missing_or_empty_context_sources_deny_without_state_change(self):
        for sources in (None, []):
            with self.subTest(sources=sources):
                payload = {key: value for key, value in self.current().items() if key != 'context_sources'}
                if sources is not None:
                    payload['context_sources'] = sources
                cadence = self.repo.put_state('cadence', 'two-steps', payload)
                request = self.service.campaign_execution_review(cadence, step_ids=['E1'])
                with self.repo.transaction() as connection:
                    before = connection.execute('SELECT * FROM thunder_state ORDER BY kind,entity_id').fetchall()
                    outbox = connection.execute('SELECT * FROM thunder_outbox ORDER BY id').fetchall()
                review = self.review()
                self.assertFalse(review['can_approve'])
                self.assertEqual(review['blockers'], ['context_sources_required'])
                self.assertIsNone(review['execution_confirmation'])
                with self.assertRaisesRegex(ApprovalError, '^context_sources_required$'):
                    self.approve(review=review, execution_confirmation=request)
                self.assertIsNone(self.repo.get_state('approval', 'two-steps'))
                self.assertEqual(self.current(), cadence)
                with self.repo.transaction() as connection:
                    self.assertEqual(connection.execute('SELECT * FROM thunder_state ORDER BY kind,entity_id').fetchall(), before)
                    self.assertEqual(connection.execute('SELECT * FROM thunder_outbox ORDER BY id').fetchall(), outbox)
                self.fixture.assert_no_effect()

    def test_broad_or_fabricated_confirmation_cannot_approve_one(self):
        broad = self.service.campaign_execution_review(self.cadence)
        for envelope in (None, True, {'confirmed': True}, broad):
            with self.subTest(envelope=envelope), self.assertRaisesRegex(ApprovalError, 'campaign_confirmation_required'):
                self.approve(execution_confirmation=envelope)
        self.assertIsNone(self.repo.get_state('approval', 'two-steps'))
        self.fixture.assert_no_effect()

    def test_invalid_subset_never_writes_authority(self):
        for ids in ([], ['E1', 'E1'], ['missing'], 'E1', [None], [True], {'E1': True}):
            with self.subTest(ids=ids), self.assertRaises(ApprovalError):
                self.approve(step_ids=ids)
        self.assertIsNone(self.repo.get_state('approval', 'two-steps'))

    def test_stale_review_and_edit_invalidate_old_authority(self):
        review = self.review()
        self.approve(review=review)
        current = self.current()
        steps = deepcopy(current['steps'])
        steps[0]['content'] = 'Changed scoped text'
        revised = self.service.revise_cadence('two-steps', steps, current['version'], 'ygor')
        self.assertEqual(revised['steps'][1], current['steps'][1])
        with self.assertRaisesRegex(RepositoryConflict, 'stale cadence review'):
            self.approve(review=review)
        with self.assertRaisesRegex(ContractError, 'active approval required'):
            self.service.dispatch_step('two-steps', 'E1', 'ygor')
        self.assertEqual(self.repo.get_state('approval', 'two-steps')['invalidated_step_ids'], ['E1'])
        self.fixture.assert_no_effect()

    def test_edit_and_discard_require_human_and_keep_recipient(self):
        steps = deepcopy(self.cadence['steps'])
        steps[0]['recipient'] = 'another@example.invalid'
        with self.assertRaisesRegex(ContractError, 'preserve recipients'):
            self.service.revise_cadence('two-steps', steps, self.cadence['version'], 'ygor')
        for operation in (lambda: self.service.review_step('two-steps', 'E1', 'hermes'),
                          lambda: self.service.revise_cadence('two-steps', steps, self.cadence['version'], 'hermes'),
                          lambda: self.service.discard_step('two-steps', 'E1', 'hermes', expected_version=1,
                              review_hash=self.service.cadence_review_hash(self.cadence), reason='scope')):
            with self.assertRaises(ContractError):
                operation()
        self.assertEqual(self.current(), self.cadence)

    def test_changed_context_blocks_approval_dispatch_and_worker(self):
        path, cadence = self.source()
        review = self.review()
        self.assertTrue(review['can_approve'])
        path.write_text('status: validado\nChanged after preview\n')
        with self.assertRaisesRegex(ApprovalError, 'context_source_changed'):
            self.approve(review=review)
        self.assertIsNone(self.repo.get_state('approval', 'two-steps'))
        path.write_text('status: validado\nSynthetic canonical source\n')
        self.approve()
        self.service.dispatch_step('two-steps', 'E1', 'ygor')
        path.write_text('status: validado\nChanged before worker\n')
        with self.assertRaisesRegex(ApprovalError, 'context_source_changed'):
            self.service.dispatch_step('two-steps', 'E1', 'ygor')
        self.assertTrue(self.fixture.worker.process_one())
        self.assertEqual(self.fixture.mailbox.commands, [])
        self.assertEqual(self.repo.effects()[0]['error_code'], 'context_source_changed')
        self.assertNotEqual(self.service.cadence_review_hash(cadence),
            self.service.cadence_review_hash({**cadence, 'context_sources': []}))

    def test_context_refs_require_validated_brain_relative_regular_files(self):
        path, cadence = self.source('Unvalidated source\n')
        self.assertIn('context_source_not_validated', self.review()['blockers'])
        path.write_text('status: validado\nSynthetic canonical source\n')
        store = self.service.knowledge_store
        (store.root/'alias').symlink_to(store.root, target_is_directory=True)
        for relative in ('../source.md', str(path), 'alias/source.md', './source.md'):
            self.repo.put_state('cadence', 'two-steps', {**cadence,
                'context_sources': [{'path': relative, 'sha256': store.read('source.md').content_hash}]})
            self.assertFalse(self.review()['can_approve'], relative)
        self.assertIsNone(self.repo.get_state('approval', 'two-steps'))

    def test_source_reference_removal_cannot_retain_authority(self):
        self.source()
        approval = self.approve()
        changed = {**self.current(), 'context_sources': []}
        with self.assertRaisesRegex(ApprovalError, 'context_source_changed'):
            self.service.validate_campaign_execution(changed, approval)

    def test_expiry_and_suppression_block_execution(self):
        approval = self.approve()
        self.repo.put_state('approval', 'two-steps', {**approval,
            'expires_at': (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()})
        with self.assertRaisesRegex(ContractError, 'expired'):
            self.service.dispatch_step('two-steps', 'E1', 'ygor')
        self.fixture.assert_no_effect()
        self.fixture.ledger.suppress('email', 'person@example.invalid', 'opt_out')
        self.assertIn('suppressed', self.review()['blockers'])
        with self.assertRaisesRegex(ApprovalError, '^suppressed$'):
            self.approve(execution_confirmation=self.service.campaign_execution_review(self.current(), step_ids=['E1']))
        self.fixture.assert_no_effect()

    def test_research_identity_pause_sheet_and_external_campaign_gates(self):
        with patch.object(self.service, 'current_research', side_effect=ContractError('research not current')):
            self.assertIn('research not current', self.review()['blockers'])
        with patch.object(self.service, 'identity_execution_error', return_value='identity_resolution_required'):
            self.assertIn('identity_resolution_required', self.review()['blockers'])
        self.service.set_global_pause(True, 'ygor')
        self.assertIn('global pause active', self.review()['blockers'])
        self.service.set_global_pause(False, 'ygor')
        with patch.object(self.service, 'require_campaign_sheet', side_effect=ApprovalError('campaign_sheet_incomplete')):
            self.assertIn('campaign_sheet_incomplete', self.review()['blockers'])
        for platform in ('apollo', 'instantly', 'hubspot_seq', 'chattie', 'codex_local'):
            config = deepcopy(self.repo.get_state('campaign_config', self.fixture.context['campaign_id'])['config'])
            config.update(campaign_id='external-'+platform, platform=platform)
            context = self.service.configure_campaign(config, 'ygor', expected_version=0)['context']
            self.repo.put_state('cadence', 'two-steps', {**self.current(), 'execution_context': context})
            self.assertIn('external_campaign_executor_not_supported', self.review()['blockers'])
            with self.assertRaisesRegex(ApprovalError, 'campaign_context_changed'):
                self.approve()
        self.fixture.assert_no_effect()

    def test_exact_subset_fake_provider_receipt_and_sent_immutable(self):
        self.approve()
        self.service.dispatch_step('two-steps', 'E1', 'ygor')
        self.assertEqual(self.fixture.mailbox.commands, [])
        self.assertTrue(self.fixture.worker.process_one())
        effect = self.repo.effects()[0]
        self.assertEqual(effect['state'], 'sent')
        self.assertTrue(effect['receipt_ref'])
        readback = self.fixture.adapter.readback(effect['receipt_ref'])
        self.assertEqual(readback['content'], self.cadence['steps'][0]['content'])
        self.assertEqual(len(self.fixture.mailbox.commands), 1)
        current = self.current()
        steps = deepcopy(current['steps'])
        steps[0]['content'] = 'Cannot edit sent'
        with self.assertRaisesRegex(ContractError, 'confirmed steps are immutable'):
            self.service.revise_cadence('two-steps', steps, current['version'], 'ygor')
        with self.assertRaises(ApprovalError):
            self.service.discard_step('two-steps', 'E1', 'ygor', expected_version=current['version'],
                review_hash=self.service.cadence_review_hash(current), reason='Cannot discard sent')
        second = self.approve('E2')
        self.assertEqual(second['step_ids'], ['E2'])

    def test_discard_is_atomic_invalidates_authority_and_never_activates_rest(self):
        self.approve()
        current = self.current()
        args = dict(expected_version=current['version'], review_hash=self.service.cadence_review_hash(current), reason='No longer relevant')
        with patch.object(self.repo, '_put_state_in', wraps=self.repo._put_state_in) as write:
            original = write._mock_wraps
            def fail_cadence(con, kind, *args, **kwargs):
                if kind == 'cadence':
                    raise RuntimeError('synthetic rollback')
                return original(con, kind, *args, **kwargs)
            write.side_effect = fail_cadence
            with self.assertRaisesRegex(RuntimeError, 'synthetic rollback'):
                self.service.discard_step('two-steps', 'E1', 'ygor', **args)
        self.assertEqual(self.repo.get_state('approval', 'two-steps')['status'], 'active')
        self.assertEqual(self.current(), current)
        discarded = self.service.discard_step('two-steps', 'E1', 'ygor', **args)
        self.assertTrue(discarded['steps'][0]['discarded'])
        self.assertEqual(discarded['status'], 'ready_for_review')
        self.assertEqual(discarded['steps'][1]['status'], 'draft')
        self.assertEqual(self.repo.get_state('approval', 'two-steps')['status'], 'invalidated')
        self.assertFalse(self.review()['can_approve'])
        self.assertTrue(self.review('E2')['can_approve'])
        with self.assertRaises(ContractError):
            self.service.dispatch_step('two-steps', 'E2', 'ygor')
        self.fixture.assert_no_effect()


if __name__ == '__main__':
    unittest.main()
