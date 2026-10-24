"""S27C6/C7: actual NewJourney contract, synthetic provider and blocked network."""
from copy import deepcopy
import unittest

from test_thunder_os_new_journey import NewJourneyTests, passing
from test_thunder_os_apollo_prompt_inputs import snapshot
from thunder_os.new_journey import NewJourneyError


class PromptModeTests(NewJourneyTests):
    def prompts(self):
        self.source(); self.check()
        self.data = snapshot()
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

    def test_three_cadence_modes_keep_exact_copy_contract(self):
        self.ready(); self.approve()
        self.assertEqual(self.row['approval_mode'], 'exact_copy_v1')
        self.assertIn('E1_content', self.export()['csv_text'])
        self.prompts()
        self.assertEqual(self.row['apollo_text_parity']['status'], 'verified')
        self.assertFalse(self.row['apollo_text_parity']['final_email_parity'])
        self.assertEqual({p['id'] for p in self.row['cadence_paths']}, {'apollo', 'instantly', 'thunder'})
        self.approve(self.row['steps'][0]['id'])
        result = self.export()
        self.assertIn('approval_kind', result['csv_text'])
        self.assertIn('subject_template', result['csv_text'])
        self.assertIn('body_template', result['csv_text'])
        self.assertFalse(result['final_email_generated'])
        self.assertFalse(result['enrollment_enabled'])
        self.assert_no_execution()

    def test_static_edit_cannot_keep_prompt_approval_mode(self):
        self.prompts(); self.approve(self.row['steps'][0]['id'])
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
        self.prompts(); self.approve(self.row['steps'][0]['id'])
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
