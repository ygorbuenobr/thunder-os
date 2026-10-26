"""Read registered blockers even when CRM negatives are unknown; no network."""
from contextlib import closing
import sqlite3
import unittest
from unittest.mock import patch
import test_thunder_os_new_journey_eligibility as support
from thunder_os.touch_ledger import TouchLedger


class RefusalObservationTests(unittest.TestCase):
    def setUp(self):
        self.f = support.CanonicalEligibilityTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def write(self, sql, values=()):
        with closing(sqlite3.connect(self.f.ledger_file)) as connection, connection:
            connection.execute(sql, values)

    def test_null_crm_fields_still_observe_registered_suppression(self):
        self.f.http.contacts['123']['properties']['opt_out'] = None
        self.write('INSERT INTO thunder_suppressions VALUES (?,?,?)', ('linkedin', support.PROFILE, 'opt_out'))
        result = self.f.check()['refusal']
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['observation']['status'], 'registered_refusal')

    def test_registered_reply_requires_review_without_claiming_refusal(self):
        self.f.http.contacts['123']['properties']['opt_out'] = None
        self.write('INSERT INTO respostas VALUES (?,?,?)', (1, 'email', support.EMAIL))
        result = self.f.check()['refusal']
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['observation']['status'], 'registered_reply')
        self.assertEqual(result['observation']['reason'], 'registered_reply_requires_review')

    def test_none_registered_never_bypasses_crm_pass_guard(self):
        self.f.http.contacts['123']['properties']['opt_out'] = None
        result = self.f.check()['refusal']
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['observation']['status'], 'none_registered')
        self.assertIn('person_opt_out', result['observation']['crm_pending_fields'])
        self.assertEqual(result['reason'], 'canonical_registered_crm_state_incomplete')
        self.assertTrue(result['source'].startswith('registered_refusal_observation:'))
        self.assertNotIn('coverage_unavailable', result['source'])
        self.assertTrue(result['observation']['coverage']['complete'])
        self.assertFalse(result['observation']['coverage']['global_inbox_complete'])

    def test_aggregate_source_binds_exact_registered_observation(self):
        self.f.http.contacts['123']['properties']['opt_out'] = None
        port = self.f.checker.refusals
        baseline = port.lookup([support.EMAIL], [support.PROFILE])
        self.assertEqual(baseline['status'], 'clear')
        observations = []
        for suffix in ('a', 'b', 'a'):
            evidence = {**baseline, 'source_ref': 'toques:registered:' + suffix * 64}
            with patch.object(port, 'lookup', return_value=evidence):
                result = self.f.check()['refusal']
            self.assertEqual(result['status'], 'unknown')
            self.assertEqual(result['reason'], 'canonical_registered_crm_state_incomplete')
            self.assertEqual(result['observation']['source_ref'], evidence['source_ref'])
            self.assertFalse(result['observation']['coverage']['global_inbox_complete'])
            observations.append(result['source'])
        self.assertNotEqual(observations[0], observations[1])
        self.assertEqual(observations[0], observations[2])

    def test_positive_crm_survives_unavailable_registered_read(self):
        self.f.http.contacts['123']['properties']['opt_out'] = 'true'
        self.write('DROP TABLE respostas')
        result = self.f.check()['refusal']
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['observation']['status'], 'unavailable')

    def test_positive_ledger_survives_later_partial_read(self):
        self.write('INSERT INTO thunder_suppressions VALUES (?,?,?)', ('email', support.EMAIL, 'opt_out'))
        original, calls = TouchLedger.preflight, []
        def fail_after_first(ledger, *args, **kwargs):
            calls.append(True)
            if len(calls) > 1:
                raise sqlite3.OperationalError('synthetic read interrupted')
            return original(ledger, *args, **kwargs)
        with patch.object(TouchLedger, 'preflight', fail_after_first):
            result = self.f.check()['refusal']
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['observation']['status'], 'registered_refusal')
        self.assertFalse(result['observation']['coverage']['complete'])

    def test_suppression_has_precedence_over_another_channel_reply(self):
        self.write('INSERT INTO thunder_suppressions VALUES (?,?,?)', ('email', support.EMAIL, 'opt_out'))
        self.write('INSERT INTO respostas VALUES (?,?,?)', (1, 'linkedin', support.PROFILE))
        result = self.f.check()['refusal']
        self.assertEqual(result['observation']['status'], 'registered_refusal')
