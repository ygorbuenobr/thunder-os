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
        self.f.http.contacts['123']['properties'].pop('opt_out')
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
        self.f.http.contacts['123']['properties'].pop('opt_out')
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

    def empty_crm_fields(self):
        self.f.http.contacts['123']['properties'].update(
            opt_out=None, hs_email_optout='', thunder_outreach_risk=None)
        self.f.http.companies['500']['properties']['thunder_suppressed'] = ''

    def test_empty_crm_still_blocks_linked_ledger_refusal_and_reply_on_both_channels(self):
        self.empty_crm_fields()
        self.assertEqual(self.f.check()['refusal']['status'], 'pass')
        for channel, identity in (('email', support.EMAIL), ('linkedin', support.PROFILE)):
            with self.subTest(channel=channel, kind='suppression'):
                self.write('INSERT INTO thunder_suppressions VALUES (?,?,?)', (channel, identity, 'opt_out'))
                result = self.f.check()['refusal']
                self.assertEqual(result['status'], 'blocked')
                self.assertEqual(result['observation']['status'], 'registered_refusal')
                self.assertEqual(result['observation']['crm_pending_fields'], [])
                self.write('DELETE FROM thunder_suppressions')
            with self.subTest(channel=channel, kind='reply'):
                self.write('INSERT INTO respostas VALUES (?,?,?)', (1, channel, identity))
                result = self.f.check()['refusal']
                self.assertEqual(result['status'], 'blocked')
                self.assertEqual(result['observation']['status'], 'registered_reply')
                self.write('DELETE FROM respostas')

    def test_empty_crm_never_clears_unavailable_or_partial_registered_reads(self):
        self.empty_crm_fields()
        self.assertEqual(self.f.check()['refusal']['status'], 'pass')
        self.write('DROP TABLE respostas')
        result = self.f.check()['refusal']
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['observation']['status'], 'unavailable')
        self.assertFalse(result['observation']['coverage']['complete'])
        self.assertFalse(result['coverage']['complete'])

    def test_empty_crm_cannot_override_linked_local_reply_or_paused_cadence(self):
        self.empty_crm_fields()
        self.assertEqual(self.f.check()['refusal']['status'], 'pass')
        self.f.repo.put_state('cadence', 'synthetic-paused', {'person_key': support.EMAIL, 'status': 'paused_reply'})
        self.assertEqual(self.f.check()['refusal']['reason'], 'canonical_cadence_paused_reply')
        self.f.repo.put_state('reply', 'synthetic-reply', {'person_key': support.EMAIL,
            'state': 'classified', 'classification': 'opt_out'})
        result = self.f.check()['refusal']
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['reason'], 'canonical_refusal_all_channels')

    def test_positive_crm_survives_unavailable_registered_read(self):
        self.f.http.contacts['123']['properties']['opt_out'] = 'true'
        self.write('DROP TABLE respostas')
        result = self.f.check()['refusal']
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['observation']['status'], 'unavailable')

    def test_positive_ledger_survives_later_partial_read(self):
        self.empty_crm_fields()
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
