"""Offline candidate tests: legacy consoles cannot execute without integration.

All provider, repository and ledger boundaries are mocks; no production activation.
"""
import unittest
from unittest.mock import Mock

from thunder_os.connectsafely_application import ConnectSafelyApplication
from thunder_os.connectsafely_console import ConnectSafelyConsole, ConnectSafelyError
from thunder_os.connectsafely_social_console import ConnectSafelySocialConsole
from thunder_os.hubspot_console import HubSpotConsole
from thunder_os.integration_hubspot import HubSpotContractError
from thunder_os.seqd_console import SEQDConsole, SEQDError


class LegacyCampaignGateTests(unittest.TestCase):
    consoles = (
        (ConnectSafelyConsole, ConnectSafelyError),
        (ConnectSafelySocialConsole, ConnectSafelyError),
        (SEQDConsole, SEQDError),
        (HubSpotConsole, HubSpotContractError),
    )

    def instance(self, cls):
        console = cls.__new__(cls)
        console.read_enabled = console.write_enabled = console.writes_enabled = True
        for name in ('repo', 'port', 'client', 'ledger', 'approvals', 'coordinator'):
            setattr(console, name, Mock(name=name))
        return console

    def assert_untouched(self, console):
        for name in ('repo', 'port', 'client', 'ledger', 'approvals', 'coordinator'):
            self.assertEqual(getattr(console, name).mock_calls, [], name)

    def test_legacy_generic_and_invented_confirmations_never_reach_boundaries(self):
        bodies = (
            {'preview_id': 'offline', 'expected_version': 1, 'review_hash': 'legacy'},
            {'preview_id': 'offline', 'expected_version': 1, 'content_hash': 'legacy',
             'confirm_nonce': 'offline-nonce-123456'},
            {'preview_id': 'offline', 'expected_version': 1, 'plan_hash': 'legacy', 'confirm': True},
            {'confirm': True, 'approved': True},
            {'confirmation': {'platform': 'thunder-os', 'campaign': 'invented',
                              'version': 1, 'rules_hash': 'invented', 'batch': ['offline']}},
        )
        for cls, error in self.consoles:
            for body in bodies:
                with self.subTest(console=cls.__name__, body=body):
                    console = self.instance(cls)
                    with self.assertRaisesRegex(error, '^campaign_confirmation_integration_required$'):
                        console.execute(body, actor='ygor')
                    self.assert_untouched(console)

    def test_authentication_is_still_required(self):
        for cls, error in self.consoles:
            with self.subTest(console=cls.__name__):
                console = self.instance(cls)
                with self.assertRaisesRegex(error, '^human_authority_required$'):
                    console.execute({}, actor='hermes')
                self.assert_untouched(console)

    def test_application_queue_and_worker_block_before_any_dependency(self):
        repo = Mock()
        console = Mock(); console.repo = repo
        readiness = Mock()
        app = ConnectSafelyApplication(repo, direct_console=console,
            readiness=readiness, execution_enabled=True)
        for call in (lambda: app.enqueue({}, 'ygor'),
                     lambda: app.process_one(), lambda: app.process_one(job_id='existing-job')):
            with self.assertRaisesRegex(ConnectSafelyError, '^campaign_confirmation_integration_required$'):
                call()
        repo.assert_not_called()
        self.assertEqual(repo.mock_calls, [])
        self.assertEqual(console.mock_calls, [])
        readiness.assert_not_called()
        with self.assertRaisesRegex(ConnectSafelyError, '^human_authority_required$'):
            app.enqueue({}, 'hermes')

    def test_social_inherits_the_same_execution_gate(self):
        self.assertIs(ConnectSafelySocialConsole.execute, ConnectSafelyConsole.execute)


if __name__ == '__main__':
    unittest.main()
