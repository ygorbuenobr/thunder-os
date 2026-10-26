"""S27C6/C7: actual NewJourney contract, synthetic provider and blocked network."""
from copy import deepcopy
import unittest
from datetime import timedelta

from test_thunder_os_new_journey import NewJourneyTests, passing
from test_thunder_os_apollo_prompt_inputs import snapshot
from thunder_os.new_journey import NewJourneyError


class PromptModeTests(NewJourneyTests):
    def prompts(self, count=1):
        self.source(); self.check()
        self.data = snapshot()
        for order in range(2, count+1):
            item = deepcopy(self.data['items'][0])
            item.update(id='step-'+str(order), order=order, interval_hours=48*(order-1))
            item['variants'][0].update(touch_id='touch-'+str(order), template_id='template-'+str(order))
            self.data['items'].append(item)
        self.data['snapshot']['steps'] = deepcopy(self.data['items'])
        scope = self.data['source']
        self.apollo.sequence.update(id=scope['sequence_id'], user_id=scope['owner_id'])
        self.apollo.source['account_id'] = scope['account_id']
        self.journey.checker = lambda rows, campaign_id: passing(rows, campaign_id, scope['account_id'])
        self.check()
        class Reader:
            def sequence_detail(inner, *args, **kwargs):
                return deepcopy(self.data)
        self.journey.template_reader = Reader()
        self.row = self.journey.select_apollo_prompts(self.row['id'], scope['sequence_id'], 'Synthetic',
                                                     'ygor', self.row['version'])

    def confirm(self):
        self.row = self.journey.confirm_export(self.row['id'], 'ygor',
            self.row['version'], self.row['review_hash'])
        return self.row

    def test_single_confirmation_binds_all_apollo_steps_without_per_prompt_approval(self):
        self.prompts(count=3)
        self.assertEqual(len(self.row['steps']), 3)
        self.assertEqual(self.row['apollo_export_confirmation']['status'], 'pending')
        self.assertTrue(self.row['apollo_export_confirmation']['can_confirm'])
        self.assertTrue(all(not s['can_approve'] for s in self.row['steps']))
        with self.assertRaisesRegex(NewJourneyError, 'export_confirmation_required'):
            self.export()
        with self.assertRaisesRegex(NewJourneyError, 'single_confirmation_required'):
            self.approve(self.row['steps'][0]['id'])
        self.confirm()
        self.assertEqual(set(self.row['approvals']), {'apollo_export'})
        self.assertEqual(self.row['apollo_export_confirmation']['status'], 'approved')
        self.assertTrue(self.row['can_export'])
        self.assertFalse(self.row['approvals']['apollo_export']['dispatch_authorized'])
        self.assertEqual(self.export()['rows'], 1)
        self.assert_no_execution()

    def test_single_confirmation_rejects_expired_changed_scope_and_old_step_approvals(self):
        self.prompts(); self.confirm()
        saved = self.repo.get_state('journey_draft', self.row['id'])
        self.now += timedelta(hours=24)
        with self.assertRaisesRegex(NewJourneyError, 'approval_invalid'):
            self.export()
        self.now -= timedelta(hours=24)
        for mutation in ('scope', 'legacy'):
            altered = deepcopy(saved)
            if mutation == 'scope':
                altered['approvals']['apollo_export']['scope_hash'] = '0'*64
            else:
                altered['approvals'] = {self.row['steps'][0]['id']: altered['approvals']['apollo_export']}
            self.repo.put_state('journey_draft', self.row['id'], altered)
            self.row = self.journey.get(self.row['id'], 'ygor')
            self.assertFalse(self.row['can_export'])
            with self.assertRaises(NewJourneyError): self.export()
        self.assert_no_execution()

    def test_confirmation_binds_recipient_and_export_only_purpose(self):
        self.prompts(); self.confirm()
        saved = self.repo.get_state('journey_draft', self.row['id'])
        for field, value in (('approved_by', 'codex'), ('dispatch_authorized', True),
                             ('purpose', 'enroll'), ('input_hash', '0'*64)):
            altered = deepcopy(saved)
            altered['approvals']['apollo_export'][field] = value
            self.repo.put_state('journey_draft', self.row['id'], altered)
            self.row = self.journey.get(self.row['id'], 'ygor')
            self.assertFalse(self.row['can_export'])
            with self.assertRaisesRegex(NewJourneyError, 'approval_invalid'): self.export()
        altered = deepcopy(saved)
        altered['rows'][0]['email'] = 'different@example.invalid'
        self.repo.put_state('journey_draft', self.row['id'], altered)
        self.row = self.journey.get(self.row['id'], 'ygor')
        self.assertFalse(self.row['can_export'])
        with self.assertRaises(NewJourneyError): self.export()
        self.assert_no_execution()

    def test_single_confirmation_rejects_actor_stale_review_and_exact_copy_mode(self):
        self.prompts()
        with self.assertRaises(NewJourneyError):
            self.journey.confirm_export(self.row['id'], 'codex', self.row['version'], self.row['review_hash'])
        with self.assertRaisesRegex(NewJourneyError, 'review_conflict'):
            self.journey.confirm_export(self.row['id'], 'ygor', self.row['version'], '0'*64)
        self.assert_no_execution()

    def test_single_confirmation_does_not_replace_exact_copy_approval(self):
        self.ready()
        with self.assertRaisesRegex(NewJourneyError, 'apollo_mode_required'):
            self.confirm()
        self.assert_no_execution()

    def test_three_cadence_modes_keep_exact_copy_contract(self):
        self.ready(); self.approve()
        self.assertEqual(self.row['approval_mode'], 'exact_copy_v1')
        self.assertIn('E1_content', self.export()['csv_text'])
        self.prompts()
        self.assertEqual(self.row['apollo_text_parity']['status'], 'verified')
        self.assertFalse(self.row['apollo_text_parity']['final_email_parity'])
        self.assertEqual({p['id'] for p in self.row['cadence_paths']}, {'apollo', 'instantly', 'thunder'})
        self.confirm()
        result = self.export()
        self.assertIn('approval_kind', result['csv_text'])
        self.assertIn('subject_template', result['csv_text'])
        self.assertIn('body_template', result['csv_text'])
        self.assertFalse(result['final_email_generated'])
        self.assertFalse(result['enrollment_enabled'])
        self.assert_no_execution()

    def test_static_edit_cannot_keep_prompt_approval_mode(self):
        self.prompts(); self.confirm()
        steps = [{'id':'E1','channel':'email','subject':'Other subject','content':'Other text',
                  'order':1,'interval_hours':0}]
        self.row = self.journey.revise_sequence(self.row['id'], steps, 'ygor', self.row['version'])
        self.assertEqual(self.row['approval_mode'], 'exact_copy_v1')
        self.assertEqual(self.row['approvals'], {})
        self.assertFalse(self.row['can_export'])
        self.assertNotEqual(self.row['apollo_text_parity']['status'], 'verified')
        with self.assertRaisesRegex(NewJourneyError, 'text_unverified'):
            self.approve()
        self.assert_no_execution()

    def test_prompt_export_requires_all_complete_checks(self):
        self.prompts(); self.confirm()
        before = self.repo.get_state('journey_draft', self.row['id'])
        preview = self.journey.export_preview(self.row['id'], 'ygor', self.row['version'], self.row['review_hash'])
        self.assertTrue(preview['preview_only'])
        self.assertEqual(before, self.repo.get_state('journey_draft', self.row['id']))
        base = self.journey.checker
        for key in ('open_deal', 'membership'):
            def partial(rows, campaign_id):
                data = base(rows, campaign_id)
                next(c for c in data[0]['checks'] if c['key'] == key)['status'] = 'unknown'
                return data
            self.journey.checker = partial
            with self.assertRaisesRegex(NewJourneyError, 'checks_changed'):
                self.export()
        self.journey.checker = base
        self.data['items'][0]['variants'][0]['prompt_template']['instructions'] = 'Changed'
        self.data['snapshot']['steps'] = deepcopy(self.data['items'])
        with self.assertRaisesRegex(NewJourneyError, 'sequence_changed'):
            self.export()
        self.assert_no_execution()


if __name__ == '__main__': unittest.main()
