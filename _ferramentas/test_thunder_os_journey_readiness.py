"""Read-only Apollo pilot diagnostics; synthetic provider, no external effects."""
from copy import deepcopy
import unittest
from test_thunder_os_new_journey_prompt_mode import PromptModeTests
from thunder_os.new_journey import NewJourney


class PilotReadinessTests(PromptModeTests):
    def test_auto_email_export_stays_preparatory_not_operational(self):
        self.prompts()
        before = deepcopy(self.row['approvals'])
        readiness = self.row['operational_readiness']
        self.assertFalse(readiness['execution_ready'])
        self.assertFalse(readiness['pilot']['current'])
        self.assertEqual(readiness['pilot']['e1_observed_type'], 'auto_email')
        self.assertFalse(readiness['pilot']['e1_manual_observed'])
        self.assertEqual(readiness['pilot']['max_initial_leads'], 10)
        self.assertEqual(self.row['approvals'], before)
        self.approve(self.row['steps'][0]['id'])
        exported = self.export()
        self.assertEqual(exported['rows'], 1)
        self.assertFalse(exported['operational_readiness']['execution_ready'])
        self.assertFalse(exported['enrollment_enabled'])
        self.assert_no_execution()

    def test_manual_snapshot_never_attests_operational_proofs(self):
        self.prompts()
        row = self.repo.get_state('journey_draft', self.row['id'])
        row['sequence']['template_snapshot']['items'][0]['flags']['raw_step_type'] = 'manual_email'
        readiness = NewJourney._operational_readiness(row)
        self.assertTrue(readiness['pilot']['e1_manual_observed'])
        self.assertFalse(readiness['pilot']['current'])
        self.assertFalse(readiness['execution_ready'])
        self.assertEqual(set(readiness['proofs']), {'provider_receipt', 'hubspot_email_activity',
            'human_reply_in_journey', 'external_stop_readback'})
        self.assertEqual(set(readiness['proofs'].values()), {'unverified_by_this_journey'})

    def test_missing_malformed_or_partial_snapshot_is_unknown(self):
        cases = [{}, {'sequence': None}, {'sequence': {'template_snapshot': {'status':'partial','complete':False,
            'items':[{'order':1,'channel':'email','flags':{'raw_step_type':'manual_email'}}]}}}]
        for items in (None, [], [None], [{'order':True,'channel':'email','flags':{'raw_step_type':'manual_email'}}],
                      [{'order':1,'channel':'email','flags':[]}],
                      [{'order':1,'channel':'email','flags':{'raw_step_type':[]}}]):
            cases.append({'sequence':{'template_snapshot':{'status':'ok','complete':True,'items':items}}})
        for row in cases:
            with self.subTest(row=row):
                r = NewJourney._operational_readiness(row)
                self.assertIsNone(r['pilot']['e1_observed_type'])
                self.assertFalse(r['pilot']['e1_manual_observed'])
                self.assertFalse(r['execution_ready'])


if __name__ == '__main__': unittest.main()
