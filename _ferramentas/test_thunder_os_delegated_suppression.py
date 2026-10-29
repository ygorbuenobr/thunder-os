import unittest

from thunder_os.delegated_suppression import ContactScope, DelegatedSuppression, SuppressionUnavailable


class SuppressionTest(unittest.TestCase):
    def setUp(self):
        self.scope = ContactScope('123', 'urn:li:member:42', 'ygor@example.test', 'apollo')
        self.calls = []

    def reader(self, label, change=None):
        def read(target):
            self.calls.append((label, target))
            if isinstance(change, Exception):
                raise change
            response = {'target': target, 'complete': True, 'suppressed': False}
            if change:
                response.update(change)
            return response
        return read

    def guard(self, linkedin=None, email=None, hubspot=None):
        return DelegatedSuppression(linkedin=linkedin or self.reader('li'),
            email_readers={'apollo': email or self.reader('email')},
            hubspot=hubspot or self.reader('hubspot'), clock=lambda: 100)

    def test_fresh_calls_and_proof_scope_and_expiry(self):
        guard = self.guard()
        proof = guard.check(self.scope)
        guard.check(self.scope)
        self.assertEqual(len(self.calls), 6)
        self.assertTrue(proof.valid_for(self.scope, now=129))
        self.assertFalse(proof.valid_for(self.scope, now=131))
        self.assertFalse(proof.valid_for(ContactScope('999', self.scope.linkedin_profile_urn,
            self.scope.email, 'apollo'), now=101))

    def test_each_source_failure_ambiguity_and_suppression_fail_closed(self):
        for source in ('linkedin', 'email', 'hubspot'):
            for change in ({'complete': False}, {'suppressed': True}, {'suppressed': None},
                           {'target': 'another'}, RuntimeError('failed')):
                with self.subTest(source=source, change=change):
                    kwargs = {source: self.reader(source, change)}
                    with self.assertRaises(SuppressionUnavailable):
                        self.guard(**kwargs).check(self.scope)

    def test_missing_provider_is_not_clean(self):
        guard = DelegatedSuppression(linkedin=self.reader('li'), email_readers={},
                                      hubspot=self.reader('hubspot'))
        with self.assertRaises(SuppressionUnavailable):
            guard.check(self.scope)


if __name__ == '__main__':
    unittest.main()
