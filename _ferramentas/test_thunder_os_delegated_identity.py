import unittest

from thunder_os.delegated_identity import compare_identity, IdentityObservationError
from thunder_os.hermes_linkedin_cadence import HermesLinkedInCadence, CadenceBlocked


class IdentityTest(unittest.TestCase):
    def setUp(self):
        self.contact = {'id': '42', 'linkedin_url': 'https://www.linkedin.com/in/example',
                        'name': 'Jane Doe', 'company': 'Example Inc'}
        self.profile = {'account_id': 'account', 'profile_urn': 'urn:li:fsd_profile:abc',
                        'linkedin_url': 'https://www.linkedin.com/in/example',
                        'name': 'Jane Doe', 'company': 'Example Inc'}
        self.plan = {'account_id': 'account', 'recipient_public_id': 'example',
                     'recipient_profile_urn': 'urn:li:fsd_profile:abc'}

    def compare(self):
        return compare_identity('42', self.contact, self.profile, self.plan)

    def test_matches_normalized_names(self):
        self.profile['name'] = '  JANE   DOE '
        self.assertTrue(self.compare()['matches'])

    def test_divergence_changes_evidence_hash(self):
        original = self.compare()['observation_hash']
        self.profile['company'] = 'Another Company'
        result = self.compare()
        self.assertEqual(result['mismatches'], ['company'])
        self.assertNotEqual(result['observation_hash'], original)

    def test_missing_profile_company_is_not_a_match(self):
        self.profile.pop('company')
        with self.assertRaises(IdentityObservationError):
            self.compare()

    def test_provider_recipient_changed_is_not_overrideable(self):
        self.profile['profile_urn'] = 'urn:li:fsd_profile:other'
        with self.assertRaises(IdentityObservationError):
            self.compare()

    def test_divergence_requires_exact_human_confirmation_and_recheck(self):
        self.profile['company'] = 'Different Company'
        result = self.compare()
        binding = {'preview_id': 'preview', 'review_hash': 'review',
                   'identity_hash': result['observation_hash']}
        class Repository:
            confirmation = None
            def get_state(self, kind, key):
                return self.confirmation
        runner = object.__new__(HermesLinkedInCadence)
        runner.repository = Repository()
        with self.assertRaisesRegex(CadenceBlocked, 'identity_human_confirmation_required'):
            runner._check_identity_binding(binding, result)
        runner.repository.confirmation = {'actor': 'ygor', 'preview_id': 'preview',
            'review_hash': 'review', 'observation_hash': result['observation_hash']}
        runner._check_identity_binding(binding, result)
        self.profile['name'] = 'Jane Other'
        with self.assertRaisesRegex(CadenceBlocked, 'identity_observation_changed'):
            runner._check_identity_binding(binding, self.compare())


if __name__ == '__main__':
    unittest.main()
