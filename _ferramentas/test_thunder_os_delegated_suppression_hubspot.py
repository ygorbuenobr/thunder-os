import unittest

from thunder_os.delegated_suppression import SuppressionUnavailable
from thunder_os.delegated_suppression_hubspot import HubSpotOptOutReader
from thunder_os.new_journey_hubspot import PORTAL_ID


class Port:
    def _call(self, method, path):
        assert (method, path) == ('GET', '/account-info/v3/details')
        return {'portalId': PORTAL_ID}


class Read:
    def __init__(self, value):
        self.value = value
        self.port = Port()
        self._schema_complete = {'contacts': True}
        self._schema_valid = {'contacts': {'hs_email_optout': True}}

    def _schema(self, kind):
        assert kind == 'contacts'

    def _record(self, kind, ident):
        props = {} if self.value == 'missing' else {'hs_email_optout': self.value}
        return {'id': ident, 'properties': props}, {}


class HubSpotOptOutTest(unittest.TestCase):
    def test_values_and_fresh_instance(self):
        for value, blocked in [(True, True), ('true', True), (False, False), ('false', False), ('', False), (None, False)]:
            created = []
            def factory(_):
                instance = Read(value); created.append(instance); return instance
            reader = HubSpotOptOutReader(object(), reader_factory=factory)
            self.assertEqual(reader('123')['suppressed'], blocked)
            reader('123')
            self.assertEqual(len(created), 2)
            self.assertIsNot(created[0], created[1])

    def test_missing_and_invalid_fail_closed(self):
        for value in ('missing', 'unexpected'):
            with self.assertRaises(SuppressionUnavailable):
                HubSpotOptOutReader(object(), reader_factory=lambda _: Read(value))('123')


if __name__ == '__main__':
    unittest.main()
