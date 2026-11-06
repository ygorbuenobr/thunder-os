import unittest
from datetime import datetime, timezone

from thunder_os.apollo_linkedin_live_executor import _RuntimeCoverage, _approved_hubspot_required
from thunder_os.apollo_linkedin_reply_coverage import coverage_current


class Apollo:
    def contact(self, ident):
        return {'id': ident, 'email_replied': False, 'email_unsubscribed': False,
                'email_status': 'verified', 'contact_campaign_statuses': [
                    {'emailer_campaign_id': 'sequence', 'status': 'active', 'replied': False}]}


class Message:
    def reply_source(self, plan, last_touch):
        return lambda urn: {'target': urn, 'complete': True, 'suppressed': False,
                            'coverage': 'exact_profile_conversation'}


class Safety:
    def last_touch_at(self, plan):
        return 1


class HubSpotRuntimeTest(unittest.TestCase):
    def target(self, email='lead@example.test'):
        return {'email': email, 'apollo_contact_id': 'contact', 'sequence_id': 'sequence',
                'linkedin_profile_urn': 'urn:li:fsd_profile:synthetic'}

    def coverage(self, configured):
        value = _RuntimeCoverage(Apollo(), Message(), Safety(), hubspot_configured=configured)
        value.bind_plan({'recipient_public_id': 'synthetic'})
        return value

    def test_approved_crm_without_verified_binding_is_unknown_and_blocks(self):
        value = self.coverage(True).observe(self.target())
        self.assertEqual(value['status'], 'unknown')
        self.assertEqual(value['sources']['hubspot']['reason'], 'hubspot_unavailable')
        self.assertFalse(coverage_current(value, datetime.now(timezone.utc), hubspot_configured=True))

    def test_absent_crm_does_not_add_gate(self):
        value = self.coverage(False).observe(self.target())
        self.assertEqual(value['status'], 'ok')
        self.assertNotIn('hubspot', value['required_sources'])

    def test_no_email_marks_crm_not_configured(self):
        value = self.coverage(True).observe(self.target(None))
        self.assertEqual(value['sources']['hubspot']['status'], 'not_configured')

    def test_only_approved_context_configures_crm(self):
        self.assertTrue(_approved_hubspot_required({'context': {'providers': {'hubspot': 'hubspot'}}}))
        self.assertFalse(_approved_hubspot_required({'context': {'providers': {'linkedin': 'connectsafely'}}}))
        with self.assertRaises(ValueError):
            _approved_hubspot_required({'context': {'providers': {'hubspot': 'other'}}})
        with self.assertRaises(ValueError):
            _approved_hubspot_required({})


if __name__ == '__main__':
    unittest.main()
