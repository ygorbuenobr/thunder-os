"""Offline fixtures for the Mission A request and report contract."""
import unittest

from thunder_os.mission_report_contract import validate_request, validate_report


class MissionReportContractTests(unittest.TestCase):
    def setUp(self):
        self.request = {'type': 'campaign_report', 'campaign_id': 'BR-APOLLO-01',
                        'request_id': 'fixture-1', 'origin': 'panel'}
        self.report = {'request_id': 'fixture-1', 'campaign_id': 'BR-APOLLO-01',
                       'summary': 'Fixture report', 'sources': [
                           {'provider': 'apollo', 'observed_at': '2026-11-03T01:00:00Z', 'coverage': 'partial'},
                           {'provider': 'hubspot', 'observed_at': '2026-11-03T01:00:00Z', 'coverage': 'partial'}],
                       'limitations': ['Enrollment coverage is incomplete'],
                       'delegation': {'execution_id': 'fixture-exec', 'status': 'completed'},
                       'review': {'execution_id': 'fixture-review', 'status': 'completed'}}

    def test_accepts_explicit_request_and_partial_report(self):
        self.assertEqual(validate_request(self.request), self.request)
        self.assertEqual(validate_report(self.request, self.report), self.report)

    def test_rejects_unscoped_or_implicit_request(self):
        for field, value in [('campaign_id', 'other'), ('origin', 'scheduler'), ('request_id', '')]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_request({**self.request, field: value})

    def test_rejects_missing_sources_limitations_or_evidence(self):
        for change in ({'sources': self.report['sources'][:1]}, {'limitations': []},
                       {'delegation': {'execution_id': 'fixture-exec', 'status': 'pending'}},
                       {'review': {'execution_id': '', 'status': 'completed'}},
                       {'request_id': 'other'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_report(self.request, {**self.report, **change})

    def test_rejects_unrecognized_external_effects(self):
        with self.assertRaises(ValueError):
            validate_report(self.request, {**self.report, 'hubspot_write': True})


if __name__ == '__main__':
    unittest.main()
