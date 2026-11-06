"""Offline contract tests; not a deployment or OS isolation test."""
import unittest
from unittest.mock import Mock, call
from datetime import datetime, timedelta, timezone

from thunder_os.connectsafely_read_broker_contract import AccountReadBrokerContract, ReadBrokerError


class AccountReadBrokerContractTests(unittest.TestCase):
    def setUp(self):
        self.company = 'company-a'
        self.account = 'a' * 24
        self.public = 'sender-123'
        self.provider = Mock()
        self.clock = lambda: datetime(2026, 11, 4, 9, 30, tzinfo=timezone(timedelta(hours=-3)))
        self.broker = AccountReadBrokerContract(self.company, self.account, self.public, self.provider, clock=self.clock)
        self.request = {'version': 1, 'operation': 'account_status', 'company_id': self.company,
                        'account_id': self.account, 'request_id': 'request-123'}
        self.status_path = '/linkedin/account/' + self.account + '/status'
        self.accounts = [{'id': self.account, 'publicId': self.public, 'apiKey': 'secret'},
                         {'id': 'b' * 24, 'publicId': 'other'}]
        self.account_status = {'id': self.account, 'publicId': self.public,
                               'status': 'AVAILABLE', 'enabled': True, 'hasTokens': True,
                               'token': 'secret', 'extra': {'secret': 1}}

    def test_valid_envelope_runs_only_two_fixed_gets_and_sanitizes_output(self):
        self.provider.side_effect = [(200, self.accounts), (200, self.account_status)]
        result = self.broker.dispatch(self.request)
        self.assertEqual(self.provider.call_args_list,
                         [call('/linkedin/accounts', {'q': self.account}), call(self.status_path, {})])
        self.assertEqual(result, {'version': 1, 'operation': 'account_status', 'request_id': 'request-123',
                                  'company_id': self.company, 'account_id': self.account,
                                  'public_id': self.public, 'status': 'AVAILABLE', 'available': True,
                                  'source': 'connectsafely', 'observed_at': '2026-11-04T12:30:00+00:00'})
        self.assertNotIn('credential_ref', result)
        self.assertNotIn('secret', str(result))
        self.assertNotIn('hasTokens', result)

    def test_invalid_envelopes_never_reach_provider(self):
        variants = [
            {**self.request, 'operation': 'send_message'},
            {**self.request, 'operation': 'connect'},
            {**self.request, 'method': 'POST'},
            {**self.request, 'path': '/linkedin/connect'},
            {**self.request, 'body': {'text': 'test'}},
            {**self.request, 'version': True},
            {**self.request, 'version': 2},
            {**self.request, 'request_id': '../other'},
            {k: v for k, v in self.request.items() if k != 'request_id'},
            None,
        ]
        for envelope in variants:
            with self.subTest(envelope=envelope), self.assertRaisesRegex(ReadBrokerError, '^invalid_request$'):
                self.broker.dispatch(envelope)
        self.provider.assert_not_called()

    def test_wrong_company_or_account_never_reaches_provider(self):
        for patch in [{'company_id': 'company-b'}, {'account_id': 'b' * 24}]:
            with self.subTest(patch=patch), self.assertRaisesRegex(ReadBrokerError, '^scope_mismatch$'):
                self.broker.dispatch({**self.request, **patch})
        self.provider.assert_not_called()

    def test_unverified_or_ambiguous_identity_stops_before_second_get(self):
        for payload in [[], [{'id': self.account, 'publicId': 'wrong'}],
                        [{'id': self.account, 'publicId': self.public}] * 2,
                        [{'id': self.account, 'publicId': self.public}, {'id': 'b' * 24, 'publicId': self.public}]]:
            self.provider.reset_mock()
            self.provider.return_value = (200, payload)
            with self.subTest(payload=payload), self.assertRaisesRegex(ReadBrokerError, '^identity_mismatch$'):
                self.broker.dispatch(self.request)
            self.provider.assert_called_once()

    def test_second_get_errors_and_identity_mismatch_fail_closed(self):
        for second in [
            (429, {'error': 'secret'}),
            (200, {'id': 'b' * 24, 'publicId': self.public, 'status': 'AVAILABLE', 'enabled': True, 'hasTokens': True}),
            (200, {'id': self.account, 'publicId': self.public, 'status': 'AVAILABLE', 'enabled': 'true', 'hasTokens': True}),
            (200, {'id': self.account, 'publicId': self.public, 'status': 'INVALID', 'enabled': True, 'hasTokens': True}),
            RuntimeError('Bearer secret'),
        ]:
            self.provider.reset_mock()
            self.provider.side_effect = [(200, self.accounts), second]
            with self.subTest(second=second), self.assertRaises(ReadBrokerError) as caught:
                self.broker.dispatch(self.request)
            self.assertNotIn('secret', str(caught.exception))
            self.assertEqual(self.provider.call_count, 2)

    def test_unavailable_account_is_not_reported_available(self):
        for change in ({'status': 'IN_USE'}, {'enabled': False}, {'hasTokens': False}):
            with self.subTest(change=change):
                self.provider.side_effect = [(200, self.accounts), (200, {**self.account_status, **change})]
                self.assertFalse(self.broker.dispatch(self.request)['available'])

    def test_invalid_clock_fails_closed_without_leaking_message(self):
        self.provider.side_effect = [(200, self.accounts), (200, self.account_status)]
        broker = AccountReadBrokerContract(self.company, self.account, self.public, self.provider,
                                           clock=lambda: datetime(2026, 11, 4))
        with self.assertRaisesRegex(ReadBrokerError, '^observation_time_invalid$'):
            broker.dispatch(self.request)

    def test_invalid_configuration_cannot_construct(self):
        for company, account, public in [('', self.account, self.public), (self.company, 'nothex', self.public),
                                         (self.company, self.account, 'bad/name')]:
            with self.subTest(company=company, account=account, public=public), self.assertRaisesRegex(ReadBrokerError, '^invalid_configuration$'):
                AccountReadBrokerContract(company, account, public, self.provider)


if __name__ == '__main__':
    unittest.main()
