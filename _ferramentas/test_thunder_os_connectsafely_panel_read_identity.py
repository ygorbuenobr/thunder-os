"""The facade narrows the normal API; it is not Python process isolation."""
import unittest

from thunder_os.connectsafely_panel_read_identity import PanelReadIdentity


class FakeDirect:
    account_id = 'a' * 24
    expected_public_id = 'owner'
    def __init__(self):
        self.sent = 0
    def account_status(self):
        return {'id': self.account_id, 'publicId': self.expected_public_id,
                'available': True, 'secret_extra': 'must be stripped'}
    def send(self, *args, **kwargs):
        self.sent += 1


class PanelReadIdentityTests(unittest.TestCase):
    def test_public_surface_exposes_only_read_and_sanitized_fields(self):
        direct = FakeDirect()
        reader = PanelReadIdentity(direct, account_id=direct.account_id, public_id='owner')
        self.assertEqual({'id': 'a' * 24, 'publicId': 'owner', 'available': True,
                          'write_authorized': False}, reader.account_status())
        self.assertEqual('a' * 24, reader.account_id)
        self.assertEqual('owner', reader.public_id)
        self.assertFalse(hasattr(reader, 'send'))
        self.assertFalse(hasattr(reader, 'request'))
        self.assertFalse(hasattr(reader, 'direct_client'))
        self.assertFalse(hasattr(reader, '__getattr__'))
        self.assertEqual(0, direct.sent)
        # Bound Python methods retain their owner: this test makes the accepted
        # risk explicit and prevents calling the facade a security boundary.
        self.assertIs(reader._read_status.__self__, direct)

    def test_binding_and_bad_status_fail_closed(self):
        direct = FakeDirect()
        with self.assertRaisesRegex(ValueError, 'account_identity_mismatch'):
            PanelReadIdentity(direct, account_id='b' * 24, public_id='owner')
        reader = PanelReadIdentity(direct, account_id=direct.account_id, public_id='owner')
        direct.expected_public_id = 'other'
        with self.assertRaisesRegex(ValueError, 'account_status_unavailable'):
            reader.account_status()
        self.assertEqual(0, direct.sent)
