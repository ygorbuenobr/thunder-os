"""Critério 1 meetings: synthetic HubSpot transport only, no auth files or network."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from thunder_os.apollo_meetings import ApolloMeetingsReader, KIND, CAMPAIGN, first_sends, metric
from thunder_os.cadence_readonly import HubSpotReadonlyAdapter, OWNER_ID, PORTAL_ID
from thunder_os.db import Repository
from thunder_os.hubspot_read_budget import HubSpotReadBudget

NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
SENT = '2026-10-21T14:00:00Z'


def lead(address, *sent, ident=None):
    return {'id': ident or address, 'email': address, 'events': [
        {'event_type': 'email_sent', 'receipt': True, 'occurred_at': when} for when in sent]}


def meeting(ident, created, outcome='SCHEDULED'):
    return {'id': ident, 'properties': {'hs_createdate': created, 'hs_timestamp': created,
                                        'hs_meeting_title': 'Conversa ' + ident, 'hs_meeting_outcome': outcome}}


class CRM:
    def __init__(self):
        self.contacts = {'a@example.invalid': ['101'], 'b@example.invalid': ['102'],
                         'c@example.invalid': ['103']}
        self.links = {'101': ['9001'], '102': ['9002', '9003'], '103': ['9004']}
        self.meetings = {'9001': meeting('9001', '2026-10-25T10:00:00Z'),
                         '9002': meeting('9002', '2026-10-01T10:00:00Z'),
                         '9003': meeting('9003', '2026-10-28T10:00:00Z', 'CANCELED'),
                         '9004': meeting('9004', '2026-10-30T10:00:00Z', 'COMPLETED')}
        self.calls = []
        self.fail_meetings = False

    def __call__(self, method, path, body, headers, timeout):
        self.calls.append((method, path, deepcopy(body)))
        if method != 'GET' and not path.endswith(('/search', '/batch/read')):
            raise AssertionError('write attempted')
        if path == '/account-info/v3/details':
            result = {'portalId': PORTAL_ID}
        elif path.startswith('/crm/v3/owners/'):
            result = {'id': OWNER_ID, 'archived': False}
        elif path == '/crm/v3/objects/contacts/search':
            wanted = body['filterGroups'][0]['filters'][0]['values']
            rows = [{'id': ident, 'properties': {'email': address}}
                    for address in wanted for ident in self.contacts.get(address, [])]
            result = {'results': rows, 'total': len(rows)}
        elif path == '/crm/v4/associations/contacts/meetings/batch/read':
            # Real HubSpot shape: contacts without meetings come back as per-input errors.
            linked = [item['id'] for item in body['inputs'] if self.links.get(item['id'])]
            empty = [item['id'] for item in body['inputs'] if not self.links.get(item['id'])]
            result = {'status': 'COMPLETE', 'results': [{'from': {'id': ident},
                'to': [{'toObjectId': m} for m in self.links[ident]]} for ident in linked],
                'numErrors': len(empty), 'errors': [{'status': 'error', 'category': 'OBJECT_NOT_FOUND',
                    'subCategory': 'crm.associations.NO_ASSOCIATIONS_FOUND',
                    'message': 'No meeting_event is associated with contact ' + ident + '.',
                    'context': {'fromObjectId': [ident]}} for ident in empty]}
        elif path == '/crm/v3/objects/meetings/batch/read':
            if self.fail_meetings:
                return 500, b'{}'
            result = {'status': 'COMPLETE', 'results': [self.meetings[i['id']] for i in body['inputs']]}
        else:
            raise AssertionError('unexpected endpoint ' + path)
        return 200, json.dumps(result).encode()


class MeetingsTests(unittest.TestCase):
    def setUp(self):
        network = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        network.start()
        self.addCleanup(network.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Repository(Path(tmp.name) / 'painel.db')
        self.crm = CRM()
        tick = [0.0]
        adapter = HubSpotReadonlyAdapter(lambda: 'synthetic-token', request=self.crm,
            read_budget=HubSpotReadBudget(monotonic=lambda: tick[0], sleep=lambda s: tick.__setitem__(0, tick[0] + s)))
        self.now = NOW
        self.reader = ApolloMeetingsReader(self.repo, adapter, clock=lambda: self.now)
        self.apollo = {'leads': [lead('a@example.invalid', SENT), lead('b@example.invalid', SENT),
                                 lead('c@example.invalid', SENT), lead('d@example.invalid', SENT),
                                 lead('e@example.invalid')]}

    def test_rule_counts_meetings_created_after_first_send_and_skips_canceled(self):
        state = self.reader.refresh(self.apollo)
        self.assertEqual(state['value'], 2)
        self.assertEqual(sorted(r['meeting_id'] for r in state['rows']), ['9001', '9004'])
        self.assertTrue(state['complete'])
        self.assertEqual(state['leads_considered'], 4)
        self.assertEqual(state['leads_without_crm_contact'], 1)
        self.assertEqual(state['reasons'], ['meetings_contacts_without_crm_match'])
        self.assertTrue(all(r['source_url'].endswith(r['meeting_id']) for r in state['rows']))
        self.assertEqual(self.repo.get_state(KIND, CAMPAIGN)['value'], 2)
        self.assertTrue(all(m == 'GET' or p.endswith(('/search', '/batch/read')) for m, p, _ in self.crm.calls))

    def test_earliest_confirmed_send_anchors_the_rule(self):
        self.apollo['leads'][1] = lead('b@example.invalid', SENT, '2026-09-20T10:00:00Z')
        self.crm.meetings['9003'] = meeting('9003', '2026-09-10T10:00:00Z')
        rows = {r['meeting_id']: r for r in self.reader.refresh(self.apollo)['rows']}
        self.assertIn('9002', rows)
        self.assertNotIn('9003', rows)
        self.assertEqual(rows['9002']['first_apollo_send_at'], '2026-09-20T10:00:00+00:00')

    def test_ambiguous_crm_contact_is_listed_not_counted(self):
        self.crm.contacts['a@example.invalid'] = ['101', '201']
        state = self.reader.refresh(self.apollo)
        self.assertEqual(state['value'], 1)
        self.assertEqual(state['leads_with_ambiguous_crm_contact'], 1)
        self.assertIn('meetings_contacts_ambiguous_in_crm', state['reasons'])

    def test_crm_failure_keeps_previous_value_and_marks_partial(self):
        first = self.reader.refresh(self.apollo)
        self.now += timedelta(hours=1)
        self.crm.fail_meetings = True
        state = self.reader.refresh(self.apollo)
        self.assertEqual(state['value'], first['value'])
        self.assertEqual(state['observed_at'], first['observed_at'])
        self.assertFalse(state['complete'])
        self.assertIn('meetings_crm_read_partial', state['reasons'])

    def test_interval_throttles_crm_reads(self):
        self.reader.refresh(self.apollo)
        calls = len(self.crm.calls)
        self.now += timedelta(minutes=10)
        self.reader.refresh(self.apollo)
        self.assertEqual(len(self.crm.calls), calls)

    def test_no_confirmed_send_writes_nothing(self):
        self.assertEqual(self.reader.refresh({'leads': [lead('e@example.invalid')]}), {})
        self.assertIsNone(self.repo.get_state(KIND, CAMPAIGN))
        self.assertEqual(self.crm.calls, [])

    def test_unreceipted_send_is_not_a_first_send(self):
        unreceipted = {'id': 'x', 'email': 'x@example.invalid', 'events': [
            {'event_type': 'email_sent', 'receipt': False, 'occurred_at': SENT}]}
        self.assertEqual(first_sends([unreceipted]), {})

    def test_partial_contact_search_keeps_previous_and_never_claims_no_match(self):
        first = self.reader.refresh(self.apollo)
        self.now += timedelta(hours=1)
        original = self.crm.__call__
        def broken(method, path, body, headers, timeout):
            if path == '/crm/v3/objects/contacts/search':
                return 200, json.dumps({'results': [], 'paging': {'next': {'after': 'not-a-number'}}}).encode()
            return original(method, path, body, headers, timeout)
        self.reader.adapter.request = broken
        state = self.reader.refresh(self.apollo)
        self.assertEqual(state['error_code'], 'contact_search_partial')
        self.assertEqual(state['value'], first['value'])
        self.assertEqual(state['leads_without_crm_contact'], first['leads_without_crm_contact'])
        self.assertEqual(state['observed_at'], first['observed_at'])
        self.assertFalse(state['complete'])

    def test_association_with_further_page_is_partial(self):
        original = self.crm.__call__
        def paged(method, path, body, headers, timeout):
            status, raw = original(method, path, body, headers, timeout)
            if path == '/crm/v4/associations/contacts/meetings/batch/read':
                data = json.loads(raw)
                data['results'][0]['paging'] = {'next': {'after': 'c1'}}
                raw = json.dumps(data).encode()
            return status, raw
        self.reader.adapter.request = paged
        state = self.reader.refresh(self.apollo)
        self.assertFalse(state['complete'])
        self.assertIn('contact_meetings:page_limit', state['coverage']['limitations'])

    def test_missing_meeting_record_is_partial(self):
        original = self.crm.__call__
        def missing(method, path, body, headers, timeout):
            if path == '/crm/v3/objects/meetings/batch/read':
                body = {**body, 'inputs': [i for i in body['inputs'] if i['id'] != '9004']}
            return original(method, path, body, headers, timeout)
        self.reader.adapter.request = missing
        state = self.reader.refresh(self.apollo)
        self.assertFalse(state['complete'])
        self.assertIn('meetings_batch_records_missing', state['coverage']['limitations'])
        self.assertIn('meetings_crm_read_partial', state['reasons'])

    def test_unparseable_creation_date_is_listed_and_not_complete(self):
        self.crm.meetings['9001'] = meeting('9001', 'not-a-date')
        state = self.reader.refresh(self.apollo)
        self.assertNotIn('9001', [r['meeting_id'] for r in state['rows']])
        self.assertIn('meetings_created_at_unparseable', state['reasons'])
        self.assertFalse(state['complete'])

    def test_offset_timestamps_compare_as_instants_and_boundary_counts(self):
        self.crm.meetings['9001'] = meeting('9001', '2026-10-21T11:00:00-03:00')  # == SENT (14:00Z)
        self.crm.meetings['9004'] = meeting('9004', '2026-10-21T10:59:00-03:00')  # one minute before
        rows = [r['meeting_id'] for r in self.reader.refresh(self.apollo)['rows']]
        self.assertIn('9001', rows)
        self.assertNotIn('9004', rows)

    def test_stale_or_foreign_snapshot_is_never_complete(self):
        state = self.reader.refresh(self.apollo)
        self.assertTrue(metric(state, NOW)['complete'])
        late = metric(state, NOW + timedelta(hours=2))
        self.assertFalse(late['complete'])
        self.assertIn('meetings_stale', late['reasons'])
        self.assertIsNone(metric({**state, 'campaign_id': 'OTHER'}, NOW)['value'])

    def test_contacts_without_meetings_are_a_complete_zero(self):
        self.crm.links = {}
        state = self.reader.refresh(self.apollo)
        self.assertEqual(state['value'], 0)
        self.assertTrue(state['complete'])
        self.assertEqual(state['coverage']['sources']['contact_meetings']['records'], 3)

    def test_other_association_error_stays_partial(self):
        original = self.crm.__call__
        def failing(method, path, body, headers, timeout):
            status, raw = original(method, path, body, headers, timeout)
            if path == '/crm/v4/associations/contacts/meetings/batch/read':
                data = json.loads(raw)
                data['errors'] = [{'status': 'error', 'category': 'RATE_LIMITS', 'subCategory': 'other',
                                   'context': {'fromObjectId': ['103']}}]
                data['results'] = [r for r in data['results'] if r['from']['id'] != '103']
                raw = json.dumps(data).encode()
            return status, raw
        self.reader.adapter.request = failing
        state = self.reader.refresh(self.apollo)
        self.assertFalse(state['complete'])
        self.assertEqual(state['error_code'], 'meeting_associations_partial')

    def test_metric_block_without_observation_is_unverified(self):
        block = metric(None)
        self.assertIsNone(block['value'])
        self.assertFalse(block['complete'])
        self.assertEqual(metric(self.reader.refresh(self.apollo))['value'], 2)


if __name__ == '__main__':
    unittest.main()
