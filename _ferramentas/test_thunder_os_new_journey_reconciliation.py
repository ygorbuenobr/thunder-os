"""Export-cohort reads with fake ports, persisted JSON state and no network."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

from thunder_os.new_journey_reconciliation import observe_batch


NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)


def draft(count=30):
    rows = [{'source_key': f'csv:{i}', 'email': f'person{i}@example.invalid',
             'apollo_contact_id': f'contact-{i}'} for i in range(count)]
    return {'rows': rows, 'export_snapshot': {'scope_hash': 'scope-1',
            'source_keys': [r['source_key'] for r in rows],
            'sequence_id': 'sequence-2', 'account_id': 'team-2'}, 'reconciliation': None}


def envelope(address):
    index = address.removeprefix('person').split('@')[0]
    return {'status': 'ok', 'credit_cost': 0, 'complete': True,
            'source': {'account_id': 'team-2', 'identity_source': 'injected'},
            'items': [{'email': address.upper(), 'id': f'contact-{index}',
                       'contact_campaign_statuses_complete': True,
                       'contact_campaign_statuses': [
                           {'emailer_campaign_id': 'sequence-2', 'status': 'active'}]}]}


def bind_recipients(row):
    row['export_snapshot']['recipient_bindings'] = {
        lead['source_key']: {'email': lead['email'], 'contact_id': lead['apollo_contact_id'],
                             'account_id': 'team-2', 'lookup_complete': True}
        for lead in row['rows']}
    return row


class FakeProvider:
    def __init__(self, reply=None):
        self.calls, self.reply = [], reply

    def __call__(self, method, **kwargs):
        self.calls.append((method, kwargs))
        return self.reply(kwargs['query']) if self.reply else envelope(kwargs['query'])


class BatchReconciliationTests(unittest.TestCase):
    def setUp(self):
        network = patch('socket.socket.connect', side_effect=AssertionError('network forbidden'))
        network.start()
        self.addCleanup(network.stop)

    def test_thirty_exported_leads_finish_in_three_rounds_and_refresh(self):
        row, provider = draft(), FakeProvider()
        for round_index in range(3):
            result = observe_batch(row, provider, NOW + timedelta(minutes=round_index))
            self.assertEqual(result['request_count'], 10)
            self.assertEqual(len(result['rows']), 30)
            self.assertEqual(result['pagination']['processed_count'], (round_index + 1) * 10)
            self.assertEqual(result['pagination']['complete'], round_index == 2)
            self.assertEqual(result['pagination']['next_cursor'], ((round_index + 1) * 10) % 30)
            self.assertEqual(sum(r['status'] == 'enrolled' for r in result['rows']), (round_index + 1) * 10)
            row['reconciliation'] = json.loads(json.dumps(result))
        self.assertEqual([call[1]['query'] for call in provider.calls], [r['email'] for r in row['rows']])
        self.assertTrue(all(method == 'contacts_search' and kw['page'] == 1 and kw['per_page'] == 100
                            for method, kw in provider.calls))
        self.assertEqual(result['rows'][-1]['observed_at'], (NOW + timedelta(minutes=2)).isoformat())
        self.assertFalse(result['current'])
        self.assertEqual((result['external_writes'], result['credits']), (0, 0))
        refreshed = observe_batch(row, provider, NOW + timedelta(minutes=3))
        self.assertEqual(refreshed['pagination']['next_cursor'], 10)
        self.assertEqual(refreshed['rows'][0]['observed_at'], (NOW + timedelta(minutes=3)).isoformat())
        self.assertEqual(refreshed['rows'][-1]['observed_at'], result['rows'][-1]['observed_at'])

    def test_larger_cohort_wraps_without_repeating_first_ten(self):
        row, provider = draft(37), FakeProvider()
        for _ in range(4):
            row['reconciliation'] = observe_batch(row, provider, NOW)
        self.assertTrue(row['reconciliation']['pagination']['complete'])
        self.assertTrue(all(r['status'] == 'enrolled' for r in row['reconciliation']['rows']))
        self.assertEqual(len({call[1]['query'] for call in provider.calls[:37]}), 37)
        self.assertEqual(row['reconciliation']['pagination']['next_cursor'], 3)

    def test_only_exported_keys_are_queried_even_if_current_eligibility_changes(self):
        row, provider = draft(4), FakeProvider()
        row['export_snapshot']['source_keys'] = ['csv:1', 'csv:3']
        row['rows'][1]['eligible'] = False  # Export is authority, not current checks.
        row['rows'][0]['eligible'] = True
        row['sequence'] = {'apollo_sequence_id': 'other', 'account_id': 'other'}
        result = observe_batch(row, provider, NOW)
        self.assertEqual([kw['query'] for _, kw in provider.calls],
                         ['person1@example.invalid', 'person3@example.invalid'])
        self.assertEqual([r['source_key'] for r in result['rows']], ['csv:1', 'csv:3'])
        self.assertTrue(all(r['status'] == 'enrolled' for r in result['rows']))

    def test_missing_or_invalid_snapshot_never_queries(self):
        invalid = [None, {}, {'source_keys': ['csv:0']},
                   {**draft(1)['export_snapshot'], 'source_keys': 'csv:0'},
                   {**draft(1)['export_snapshot'], 'source_keys': [None]},
                   {**draft(1)['export_snapshot'], 'account_id': ''},
                   {**draft(1)['export_snapshot'], 'sequence_id': 'bad id'}]
        for snapshot in invalid:
            with self.subTest(snapshot=snapshot):
                row, provider = draft(1), FakeProvider()
                row['export_snapshot'] = snapshot
                result = observe_batch(row, provider, NOW)
                self.assertEqual(result['status'], 'unknown')
                self.assertEqual(result['rows'], [])
                self.assertFalse(result['pagination']['complete'])
                self.assertEqual(result['request_count'], 0)
                self.assertEqual(provider.calls, [])

    def test_each_snapshot_identity_change_resets_observations_and_cursor(self):
        for field, value in [('scope_hash', 'scope-2'), ('sequence_id', 'sequence-3'),
                             ('account_id', 'team-3'), ('source_keys', ['csv:29', 'csv:0'])]:
            with self.subTest(field=field):
                row, provider = draft(), FakeProvider()
                row['reconciliation'] = observe_batch(row, provider, NOW)
                row['export_snapshot'][field] = value
                result = observe_batch(row, provider, NOW + timedelta(minutes=1))
                expected_start = 'person29@example.invalid' if field == 'source_keys' else 'person0@example.invalid'
                self.assertEqual(provider.calls[10][1]['query'], expected_start)
                self.assertTrue(result['snapshot_changed'])
                if field != 'source_keys':
                    self.assertEqual(result['pagination']['processed_count'], 10)
                    self.assertNotIn('observed_at', result['rows'][10])
                if field in ('sequence_id', 'account_id'):
                    self.assertTrue(all(r['status'] == 'unknown' for r in result['rows']))

    def test_duplicate_keys_and_rows_missing_rows_and_invalid_cursor(self):
        row, provider = draft(3), FakeProvider()
        row['export_snapshot']['source_keys'] = ['csv:0', 'csv:0', 'csv:1', 'missing', 'csv:2']
        row['rows'].append(deepcopy(row['rows'][1]))
        result = observe_batch(row, provider, NOW)
        self.assertEqual([r['source_key'] for r in result['rows']], ['csv:0', 'csv:1', 'missing', 'csv:2'])
        self.assertEqual(result['rows'][1]['reason'], 'exported_source_ambiguous')
        self.assertEqual(result['rows'][2]['reason'], 'exported_source_missing')
        self.assertEqual(result['request_count'], 2)
        self.assertTrue(result['pagination']['complete'])
        for cursor in (-1, 999, True, '10', None):
            with self.subTest(cursor=cursor):
                row, provider = draft(11), FakeProvider()
                prior = observe_batch(row, provider, NOW)
                prior['pagination']['next_cursor'] = cursor
                row['reconciliation'] = prior
                result = observe_batch(row, provider, NOW)
                self.assertEqual(provider.calls[10][1]['query'], 'person0@example.invalid')
                self.assertEqual(result['pagination']['next_cursor'], 10)

    def test_exact_identity_and_complete_active_membership_required(self):
        def change(path, value):
            result = envelope('person0@example.invalid')
            target = result
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            return result

        cases = [
            (('source', 'account_id'), 'other'),
            (('source', 'identity_source'), 'unverified'),
            (('items', 0, 'email'), 'person01@example.invalid'),
            (('items', 0, 'id'), 'wrong'),
            (('items', 0, 'contact_campaign_statuses_complete'), False),
            (('complete',), False), (('status',), 'partial'),
            (('credit_cost',), 1),
            (('items', 0, 'contact_campaign_statuses'), []),
            (('items', 0, 'contact_campaign_statuses'), None),
            (('items', 0, 'contact_campaign_statuses', 0, 'emailer_campaign_id'), 'other'),
        ]
        for path, value in cases:
            with self.subTest(path=path, value=value):
                result = observe_batch(bind_recipients(draft(1)), FakeProvider(lambda _: change(path, value)), NOW)
                self.assertEqual(result['rows'][0]['status'], 'unknown')
        for signals, expected in [({'state': 'active'}, 'enrolled'),
                                  ({'status': 'in_progress'}, 'enrolled'),
                                  ({'state': 'active', 'status': 'in_progress'}, 'enrolled'),
                                  ({}, 'unknown'), ({'state': None}, 'unknown'),
                                  ({'status': 'Active'}, 'unknown'),
                                  ({'status': 'paused'}, 'unknown'),
                                  ({'state': 'active', 'status': 'finished'}, 'unknown')]:
            with self.subTest(signals=signals):
                reply = envelope('person0@example.invalid')
                reply['items'][0]['contact_campaign_statuses'] = [{'emailer_campaign_id': 'sequence-2', **signals}]
                result = observe_batch(draft(1), FakeProvider(lambda _: reply), NOW)
                self.assertEqual(result['rows'][0]['status'], expected)
        for duplicate in ('contact', 'membership'):
            reply = envelope('person0@example.invalid')
            items = reply['items'] if duplicate == 'contact' else reply['items'][0]['contact_campaign_statuses']
            items.append(deepcopy(items[0]))
            self.assertEqual(observe_batch(draft(1), FakeProvider(lambda _: reply), NOW)['rows'][0]['status'], 'unknown')

    def test_same_email_contacts_cannot_be_disambiguated_by_csv_or_bound_id(self):
        for mode in ('legacy', 'bound_existing', 'bound_absent'):
            with self.subTest(mode=mode):
                row = draft(1)
                if mode != 'legacy':
                    bind_recipients(row)
                    if mode == 'bound_absent':
                        row['export_snapshot']['recipient_bindings']['csv:0']['contact_id'] = None
                reply = envelope(row['rows'][0]['email'])
                duplicate = deepcopy(reply['items'][0])
                duplicate['id'] = 'second-contact-same-email'
                reply['items'].append(duplicate)
                result = observe_batch(row, FakeProvider(lambda _: reply), NOW)
                self.assertEqual(result['rows'][0]['status'], 'unknown')
                self.assertEqual(result['rows'][0]['reason'], 'membership_unverified')

    def test_arbitrary_csv_contact_id_is_ignored(self):
        for bound in (False, True):
            with self.subTest(bound=bound):
                row = bind_recipients(draft(1)) if bound else draft(1)
                row['rows'][0]['apollo_contact_id'] = 'arbitrary-untrusted-csv-id'
                result = observe_batch(row, FakeProvider(), NOW)
                self.assertEqual(result['rows'][0]['status'], 'enrolled')
                self.assertEqual(result['rows'][0]['contact_id'], 'contact-0')

    def test_bound_existing_id_mismatch_stays_unknown_even_if_csv_id_matches(self):
        row = bind_recipients(draft(1))
        row['export_snapshot']['recipient_bindings']['csv:0']['contact_id'] = 'export-bound-other-id'
        result = observe_batch(row, FakeProvider(), NOW)
        self.assertEqual(result['rows'][0]['status'], 'unknown')

    def test_absent_contact_at_export_can_be_observed_as_new_unique_id(self):
        row = bind_recipients(draft(1))
        row['export_snapshot']['recipient_bindings']['csv:0']['contact_id'] = None
        reply = envelope(row['rows'][0]['email'])
        reply['items'][0]['id'] = 'created-after-manual-enrollment'
        result = observe_batch(row, FakeProvider(lambda _: reply), NOW)
        self.assertEqual(result['rows'][0]['status'], 'enrolled')
        self.assertEqual(result['rows'][0]['contact_id'], 'created-after-manual-enrollment')

    def test_invalid_or_mismatched_binding_is_unknown_without_reads(self):
        for field, value in [('email', 'other@example.invalid'), ('account_id', 'other-team'),
                             ('lookup_complete', False), ('lookup_complete', 1),
                             ('contact_id', ''), ('contact_id', ['invalid'])]:
            with self.subTest(field=field, value=value):
                row, provider = bind_recipients(draft(1)), FakeProvider()
                row['export_snapshot']['recipient_bindings']['csv:0'][field] = value
                result = observe_batch(row, provider, NOW)
                self.assertEqual(result['rows'][0]['status'], 'unknown')
                self.assertEqual(result['request_count'], 0)
                self.assertEqual(provider.calls, [])
        for bindings in (None, [], {}, {'csv:0': None}, {'csv:0': {'email': 'person0@example.invalid',
                           'account_id': 'team-2', 'lookup_complete': True}}):
            with self.subTest(bindings=bindings):
                row, provider = draft(1), FakeProvider()
                row['export_snapshot']['recipient_bindings'] = bindings
                result = observe_batch(row, provider, NOW)
                self.assertEqual(result['rows'][0]['status'], 'unknown')
                self.assertEqual(provider.calls, [])

    def test_binding_addition_change_or_removal_resets_cursor_and_cached_observations(self):
        for change in ('add', 'id', 'absence', 'remove'):
            with self.subTest(change=change):
                row = draft() if change == 'add' else bind_recipients(draft())
                provider = FakeProvider()
                row['reconciliation'] = json.loads(json.dumps(observe_batch(row, provider, NOW)))
                if change == 'add':
                    bind_recipients(row)
                elif change == 'remove':
                    row['export_snapshot'].pop('recipient_bindings')
                else:
                    row['export_snapshot']['recipient_bindings']['csv:0']['contact_id'] = (
                        None if change == 'absence' else 'changed-bound-id')
                result = observe_batch(row, provider, NOW + timedelta(minutes=1))
                self.assertTrue(result['snapshot_changed'])
                self.assertEqual(provider.calls[10][1]['query'], 'person0@example.invalid')
                self.assertEqual(result['pagination']['next_cursor'], 10)
                self.assertEqual(result['pagination']['processed_count'], 10)
                self.assertNotIn('observed_at', result['rows'][10])
                if 'recipient_bindings' in row['export_snapshot']:
                    expected = deepcopy(row['export_snapshot']['recipient_bindings'])
                    self.assertEqual(result['export_snapshot']['recipient_bindings'], expected)
                    result['export_snapshot']['recipient_bindings']['csv:0']['contact_id'] = 'tampered'
                    self.assertEqual(row['export_snapshot']['recipient_bindings'], expected)

    def test_missing_or_invalid_provider_contact_id_cannot_prove_enrollment(self):
        for ident in (None, '', [], 'invalid id'):
            with self.subTest(ident=ident):
                row, reply = draft(1), envelope('person0@example.invalid')
                reply['items'][0]['id'] = ident
                result = observe_batch(row, FakeProvider(lambda _: reply), NOW)
                self.assertEqual(result['rows'][0]['status'], 'unknown')

    def test_cached_positive_from_previous_identity_policy_is_invalidated(self):
        row = draft()
        prior = observe_batch(row, FakeProvider(), NOW)
        prior.pop('identity_policy')  # State persisted by the vulnerable implementation.
        row['reconciliation'] = prior
        def reply(address):
            response = envelope(address)
            if address.startswith('person0@'):
                duplicate = deepcopy(response['items'][0])
                duplicate['id'] = 'other-contact-same-email'
                response['items'].append(duplicate)
            return response
        provider = FakeProvider(reply)
        result = observe_batch(row, provider, NOW + timedelta(minutes=1))
        self.assertEqual(provider.calls[0][1]['query'], 'person0@example.invalid')
        self.assertEqual(result['rows'][0]['status'], 'unknown')
        self.assertNotIn('observed_at', result['rows'][10])
        self.assertEqual(result['pagination']['next_cursor'], 10)

    def test_invalid_binding_does_not_block_independent_recipient(self):
        row, provider = bind_recipients(draft(2)), FakeProvider()
        row['export_snapshot']['recipient_bindings']['csv:0']['account_id'] = 'wrong-team'
        result = observe_batch(row, provider, NOW)
        self.assertEqual([r['status'] for r in result['rows']], ['unknown', 'enrolled'])
        self.assertEqual([kw['query'] for _, kw in provider.calls], ['person1@example.invalid'])
        self.assertTrue(result['pagination']['complete'])

    def test_provider_failures_continue_without_leaking_errors(self):
        def reply(address):
            if address.startswith('person0@'):
                raise TimeoutError('secret-token-do-not-expose')
            if address.startswith('person1@'):
                return {'status': 'error', 'reason': 'rate_limited'}
            if address.startswith('person2@'):
                return None
            if address.startswith('person3@'):
                return {**envelope(address), 'source': []}
            return envelope(address)
        provider = FakeProvider(reply)
        result = observe_batch(draft(5), provider, NOW)
        self.assertEqual(result['request_count'], 5)
        self.assertEqual([r['status'] for r in result['rows']], ['unknown'] * 4 + ['enrolled'])
        self.assertEqual(result['rows'][1]['reason'], 'journey_apollo_rate_limited')
        self.assertNotIn('secret-token', json.dumps(result))

    def test_wrapped_provider_error_codes_and_cancellation(self):
        from thunder_os.new_journey import NewJourneyError

        for code, expected in [('journey_apollo_timeout', 'journey_apollo_timeout'),
                               ('secret-private-error', 'journey_apollo_unavailable')]:
            def fail(address):
                if address.startswith('person0@'):
                    raise NewJourneyError(code, 503)
                return envelope(address)
            result = observe_batch(draft(2), FakeProvider(fail), NOW)
            self.assertEqual(result['rows'][0]['reason'], expected)
            self.assertEqual(result['rows'][1]['status'], 'enrolled')
        def cancel(_address):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            observe_batch(draft(1), FakeProvider(cancel), NOW)

    def test_invalid_email_is_unknown_without_using_request_budget(self):
        row, provider = draft(12), FakeProvider()
        row['rows'][0]['email'] = 'invalid-address'
        result = observe_batch(row, provider, NOW)
        self.assertEqual(result['rows'][0]['reason'], 'exported_email_invalid')
        self.assertEqual(result['request_count'], 10)
        self.assertEqual(result['pagination']['next_cursor'], 11)
        self.assertEqual(result['pagination']['processed_count'], 11)
        self.assertEqual(provider.calls[0][1]['query'], 'person1@example.invalid')
        self.assertNotIn('observed_at', result['rows'][-1])

    def test_failed_refresh_replaces_enrollment_and_does_not_invalidate_other_keys(self):
        row, provider = draft(12), FakeProvider()
        for _ in range(2):
            row['reconciliation'] = observe_batch(row, provider, NOW)
        self.assertTrue(all(r['status'] == 'enrolled' for r in row['reconciliation']['rows']))
        def fail(_address):
            raise TimeoutError()
        result = observe_batch(row, FakeProvider(fail), NOW + timedelta(minutes=1))
        self.assertEqual([r['status'] for r in result['rows']], ['unknown'] * 6 + ['enrolled'] * 2 + ['unknown'] * 4)
        self.assertTrue(result['pagination']['complete'])

    def test_roundtrip_state_is_detached_and_progress_survives_failures(self):
        row, provider = draft(30), FakeProvider(lambda _: (_ for _ in ()).throw(RuntimeError()))
        original = deepcopy(row)
        first = observe_batch(row, provider, NOW)
        self.assertEqual(row, original)
        row['reconciliation'] = json.loads(json.dumps(first))
        second = observe_batch(row, provider, NOW)
        self.assertEqual(second['pagination']['next_cursor'], 20)
        self.assertEqual(second['pagination']['processed_count'], 20)
        self.assertEqual(provider.calls[10][1]['query'], 'person10@example.invalid')
        second['rows'][0]['status'] = 'tampered'
        second['export_snapshot']['source_keys'].clear()
        self.assertEqual(first['rows'][0]['status'], 'unknown')
        self.assertEqual(len(row['export_snapshot']['source_keys']), 30)

    def test_empty_export_and_unavailable_provider_are_bounded(self):
        empty, provider = draft(0), FakeProvider()
        result = observe_batch(empty, provider, NOW)
        self.assertEqual(provider.calls, [])
        self.assertTrue(result['pagination']['complete'])
        result = observe_batch(draft(15), None, NOW)
        self.assertEqual(result['request_count'], 0)
        self.assertEqual(result['status'], 'configuration_pending')
        self.assertTrue(all(r['status'] == 'unknown' for r in result['rows']))


if __name__ == '__main__':
    unittest.main()
