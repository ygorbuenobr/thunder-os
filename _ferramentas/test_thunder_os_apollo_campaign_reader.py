"""S46 C1/C2: synthetic HTTP/SEQD fixtures only, no auth files or network.

Named assertions cover endpoint allowlist, complete/partial contacts, stable IDs,
source changes, cache age, budgets, scheduled/sent/content proof, reply signals,
exact Gmail originals/third parties, and repository-only snapshots. Integration
with real Apollo/Gmail and the final independent verifier belong to main.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from thunder_os.apollo_campaign_reader import (ApolloCampaignReadOnly, ApolloCampaignReader,
    APOLLO_SEQUENCE, APOLLO_TEAM, OWNER, CAMPAIGN, KIND, KEY_FILE, SEQUENCE_PATH, enabled, _validate_mcp, _mcp_https_request, _MCP_INITIALIZE)
from thunder_os.external_email_import import ExternalEmailError
from thunder_os.seqd_client import MAILBOX, SEQDClient

NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
SENT = '2026-10-26T10:00:00Z'
LEAD = 'lead@example.invalid'


def contact(ident='contact-1', scoped=True):
    return {'id': ident, 'team_id': APOLLO_TEAM, 'email': LEAD,
        'first_name': 'Synthetic', 'last_name': 'Person', 'organization': {'name': 'Fixture'},
        'contact_campaign_statuses': [{'emailer_campaign_id': APOLLO_SEQUENCE, 'status': 'active'}] if scoped else []}


def metadata(ident='message-1', **changes):
    return {'id': ident, 'team_id': APOLLO_TEAM, 'user_id': OWNER,
        'emailer_campaign_id': APOLLO_SEQUENCE, 'contact_id': 'contact-1',
        'to_email': LEAD, 'from_email': MAILBOX, 'status': 'completed',
        'completed_at': SENT, 'provider_message_id': 'abc1', 'provider_thread_id': 'abc123',
        'replied': False, 'reply_class': None, **changes}


def content(ident='message-1', **changes):
    return {'id': ident, 'body': 'Literal original\n<script>not executable</script>',
        'body_format': 'plain', 'subject': 'Original subject', 'status': 'completed',
        'sent_at': SENT, 'recipients': [{'type': 'to', 'email': LEAD}], **changes}


def gmail_message(ident='abc1', *, outgoing=True, sender=None, **changes):
    return {'id': ident, 'thread_id': 'abc123', 'from': sender or (MAILBOX if outgoing else LEAD),
        'to': LEAD if outgoing else MAILBOX, 'subject': 'Original subject',
        'internal_date': '1793008800000' if outgoing else '1793012400000',
        'labelIds': ['SENT'] if outgoing else ['INBOX'],
        'bodies': {'text': content()['body'] if outgoing else 'Literal incoming body'},
        'complete': True, 'coverage': {'reasons': []}, **changes}


def thread(messages, **changes):
    return {'status': 'ok', 'mailbox': MAILBOX, 'thread': {'id': 'abc123',
        'context_only': True, 'receipt_eligible': False, 'messages': messages,
        'complete': True, 'truncated': False, 'coverage': {'reasons': []}, **changes}}


class Wire:
    def __init__(self, contacts=None, messages=None, contents=None):
        self.contacts = [contact()] if contacts is None else contacts
        self.messages = [metadata()] if messages is None else messages
        self.contents = [content()] if contents is None else contents
        self.profile = {'team_id': APOLLO_TEAM, 'id': OWNER}
        self.sequences = [{'id': APOLLO_SEQUENCE, 'user_id': OWNER, 'archived': False}]
        self.calls = []
        self.override = None

    def __call__(self, method, path, body, headers, timeout):
        self.calls.append((method, path, deepcopy(body), timeout))
        if self.override:
            value = self.override(method, path, body)
            if value is not None:
                return value
        if path.endswith('users/api_profile'):
            data = self.profile
        elif path == SEQUENCE_PATH:
            data = {'emailer_campaigns': self.sequences}
        elif path.endswith('contacts/search'):
            page = body['page']
            data = {'contacts': self.contacts[(page-1)*100:page*100],
                'pagination': {'page': page, 'per_page': 100, 'total_entries': len(self.contacts)}}
        elif '/emailer_messages/search?' in path:
            ids = parse_qs(urlsplit(path).query).get('ids[]')
            if not ids:
                raise AssertionError('Unstable REST enumeration is forbidden')
            data = {'emailer_messages': [row for row in self.messages if row['id'] in ids],
                    'emailer_steps': [], 'num_fetch_result': None}
        elif path.endswith('get_content'):
            data = {'emailer_messages': [r for r in self.contents if r['id'] in body['ids']]}
        else:
            raise AssertionError('Unexpected endpoint')
        return 200, json.dumps(data).encode()


class FakeMCPTransport:
    """Official tool shape deliberately omits native sender/thread fields."""
    def __init__(self, wire, *, sse=False):
        self.wire, self.sse = wire, sse
        self.calls, self.override = [], None

    def __call__(self, method, path, body, headers, timeout):
        self.calls.append((method, path, deepcopy(body), dict(headers), timeout))
        if self.override:
            response = self.override(method, path, body, headers, timeout)
            if response is not None:
                return response
        if body['method'] == 'notifications/initialized':
            return 202, b'', {'Mcp-Session-Id': 'synthetic-session'}
        if body['method'] == 'initialize':
            result = {'protocolVersion': '2025-11-25', 'capabilities': {'tools': {}}}
        else:
            page = int(body['params']['arguments']['page'])
            source = self.wire.messages
            if body['params']['arguments'].get('emailer_message_stats') == ['sent']:
                source = [row for row in source if row.get('status') == 'completed']
            rows = source[(page-1)*100:page*100]
            result = {'structuredContent': {'pagination': {'page': str(page), 'per_page': '100',
                'total_entries': len(source), 'total_pages': (len(source)+99)//100},
                'emailer_messages': [{k: v for k, v in row.items() if k in {
                    'id', 'contact_id', 'emailer_campaign_id', 'status', 'replied'}} for row in rows]}}
        raw = json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': result}).encode()
        if self.sse:
            raw = b': heartbeat\n\nevent: message\ndata: '+raw+b'\n\n'
        return 200, raw, {'Content-Type': 'text/event-stream' if self.sse else 'application/json',
                          'Mcp-Session-Id': 'synthetic-session'}


class ReaderTests(unittest.TestCase):
    def setUp(self):
        network = patch('socket.socket', side_effect=AssertionError('external network forbidden'))
        network.start()
        self.addCleanup(network.stop)

    def make(self, wire=None, **options):
        self.wire = wire or Wire()
        self.mcp = options.pop('mcp_request', None) or FakeMCPTransport(self.wire)
        return ApolloCampaignReadOnly(lambda: 'synthetic-key', request=self.wire,
            clock=lambda: NOW, mcp_request=self.mcp, **options)

    def test_native_envelope_completed_original_and_zero_provider_writes(self):
        data = self.make().collect()
        self.assertTrue(data['coverage']['complete'])
        event = data['leads'][0]['events'][0]
        self.assertEqual(event['text'], content()['body'])
        self.assertTrue(event['content_complete'])
        self.assertEqual(event['provider_message_id'], 'abc1')
        self.assertEqual(event['ref_sent'], 'message-1')
        self.assertEqual(data['metrics']['sent_messages'], 1)
        self.assertEqual([(m, urlsplit(p).path) for m, p, _, _ in self.wire.calls], [
            ('GET', '/api/v1/users/api_profile'), ('GET', '/api/v1/emailer_campaigns/search'),
            ('POST', '/api/v1/contacts/search'),
            ('GET', '/api/v1/emailer_messages/search'), ('POST', '/api/v1/emailer_messages/get_content')])
        self.assertEqual(self.wire.calls[1][1:3], (SEQUENCE_PATH, None))
        self.assertEqual(self.wire.calls[2][2], {'page': 1, 'per_page': 100,
            'sort_by_field': 'contact_created_at', 'sort_ascending': True})

    def test_wrong_profile_team_and_invalid_actor_rejected_before_sequence_or_contacts(self):
        for field in ('team_id', 'id'):
            with self.subTest(field=field):
                wire = Wire()
                wire.profile[field] = 'other' if field == 'team_id' else 'bad/id'
                data = self.make(wire).collect()
                self.assertFalse(data['coverage']['complete'])
                self.assertEqual(len(wire.calls), 1)
                self.assertEqual(data['leads'], [])

    def test_team_key_actor_can_differ_from_independently_verified_sequence_owner(self):
        wire = Wire()
        wire.profile['id'] = '6a18b5e2bba6e300203fd7e4'
        data = self.make(wire).collect()
        self.assertTrue(data['coverage']['complete'])
        self.assertEqual(data['coverage']['authenticated_user_id'], wire.profile['id'])
        self.assertTrue(data['coverage']['sequence_owner_verified'])
        self.assertEqual(data['owner_id'], OWNER)
        self.assertNotEqual(wire.profile['id'], OWNER)
        self.assertEqual(data['metrics']['sent_messages'], 1)

    def test_sequence_owner_missing_duplicate_archived_or_conflicting_team_blocks_before_contacts(self):
        valid = {'id': APOLLO_SEQUENCE, 'user_id': OWNER, 'archived': False}
        variants = [[], [{**valid, 'id': 'other-sequence'}], [valid, valid],
            [{**valid, 'user_id': 'other-owner'}], [{**valid, 'archived': True}],
            [{**valid, 'archived': None}], [{**valid, 'team_id': 'other-team'}],
            [{'id': APOLLO_SEQUENCE, 'user_id': OWNER}], None, [None]]
        for sequences in variants:
            with self.subTest(sequences=sequences):
                wire = Wire()
                wire.profile['id'] = 'another-team-actor'
                wire.sequences = sequences
                data = self.make(wire).collect()
                self.assertFalse(data['coverage']['complete'])
                self.assertEqual(len(wire.calls), 2)
                self.assertEqual(wire.calls[-1][1], SEQUENCE_PATH)
                self.assertEqual(data['leads'], [])

    def test_cached_cohort_cannot_bypass_fresh_sequence_owner_proof(self):
        reader = self.make()
        previous = reader.collect()
        self.wire.calls.clear()
        self.wire.sequences[0]['user_id'] = 'other-owner'
        data = reader.collect(previous)
        self.assertEqual(data['coverage']['reasons'], ['apollo_owner_mismatch'])
        self.assertEqual(len(self.wire.calls), 2)
        self.assertEqual(data['leads'], [])

    def test_full_contact_scan_scopes_literal_members_only(self):
        rows = [contact('c'+str(i), scoped=False) for i in range(100)] + [contact()]
        data = self.make(Wire(contacts=rows)).collect()
        self.assertTrue(data['coverage']['complete'])
        self.assertEqual(data['coverage']['contacts_pages'], 2)
        self.assertEqual(len(data['leads']), 1)
        self.assertEqual(len(data['cohort_cache']['members']), 1)
        self.assertNotIn('c0', json.dumps(data['cohort_cache']))

    def test_162_page_scan_225_seconds_has_fresh_message_timestamp_and_285_members(self):
        rows = [contact('c'+str(i), scoped=i < 285) for i in range(16150)]
        wire = Wire(contacts=rows, messages=[])
        elapsed = [0.0]
        def simulate(method, path, body):
            if path.endswith('contacts/search'):
                elapsed[0] += 225.34 / 162
            return None
        wire.override = simulate
        clock = lambda: NOW + timedelta(seconds=elapsed[0])
        reader = ApolloCampaignReadOnly(lambda: 'fake', request=wire, clock=clock,
            monotonic=lambda: elapsed[0], mcp_request=FakeMCPTransport(wire))
        data = reader.collect()
        self.assertTrue(data['coverage']['complete'])
        self.assertEqual(data['coverage']['contacts_pages'], 162)
        self.assertEqual(data['coverage']['contacts_source_total'], 16150)
        self.assertEqual(len(data['leads']), 285)
        self.assertEqual(data['observed_at'], clock().isoformat())
        self.assertGreater(elapsed[0], 225)
        repo = Mock()
        repo.get_state.return_value = {**data, 'status': 'ok'}
        snapshot = ApolloCampaignReader(repo, reader, enable_check=lambda: True, clock=clock).snapshot()
        self.assertFalse(snapshot['stale'])

    def test_duplicate_contacts_fail_without_cache_authority(self):
        data = self.make(Wire(contacts=[contact(), contact()])).collect()
        self.assertIn('duplicate_or_invalid_contact', data['coverage']['reasons'])
        self.assertNotIn('cohort_cache', data)
        self.assertFalse(data['metrics']['complete'])

    def test_missing_memberships_is_unknown_not_empty(self):
        row = contact()
        del row['contact_campaign_statuses']
        data = self.make(Wire(contacts=[row], messages=[])).collect()
        self.assertIn('contact_membership_unknown', data['coverage']['reasons'])
        self.assertFalse(data['coverage']['contacts_complete'])

    def test_source_changed_total_fails(self):
        wire = Wire(contacts=[contact('c'+str(i), False) for i in range(101)], messages=[])
        wire.override = lambda m, p, b: (200, json.dumps({'contacts': [contact()],
            'pagination': {'page': 2, 'per_page': 100, 'total_entries': 102}}).encode()) if b and b.get('page') == 2 else None
        data = self.make(wire).collect()
        self.assertIn('apollo_contacts_source_changed', data['coverage']['reasons'])
        self.assertNotIn('cohort_cache', data)

    def test_page_limit_exposes_partial(self):
        data = self.make(Wire(contacts=[contact('c'+str(i)) for i in range(101)]), max_pages=1).collect()
        self.assertEqual(len(data['leads']), 100)
        self.assertIn('apollo_contacts_page_limit', data['coverage']['reasons'])
        self.assertFalse(data['coverage']['complete'])

    def test_title_is_literal_nonmutating_and_preserved_in_scoped_cache(self):
        row = contact()
        row['title'] = 'Diretor de Operações / Industrial'
        original = deepcopy(row)
        reader = self.make(Wire(contacts=[row]))
        first = reader.collect()
        self.assertEqual(first['leads'][0]['title'], row['title'])
        self.assertEqual(first['cohort_cache']['members'][0]['title'], row['title'])
        self.assertEqual(row, original)
        self.wire.calls.clear()
        cached = reader.collect(previous=first)
        self.assertTrue(cached['coverage']['contacts_cached'])
        self.assertEqual(cached['leads'][0]['title'], row['title'])
        self.assertFalse(any(p.endswith('contacts/search') for _, p, _, _ in self.wire.calls))
        self.assertIsNone(self.make().collect()['leads'][0]['title'])

    def test_cohort_cache_one_hour_then_rescan_and_scope_check(self):
        reader = self.make()
        previous = reader.collect()
        previous['cohort_cache']['observed_at'] = (NOW-timedelta(minutes=59)).isoformat()
        self.wire.calls.clear()
        data = reader.collect(previous)
        self.assertTrue(data['coverage']['contacts_cached'])
        self.assertFalse(any(p.endswith('contacts/search') for _, p, _, _ in self.wire.calls))
        self.assertEqual(data['coverage']['contacts_observed_at'], previous['cohort_cache']['observed_at'])
        for change in ({'observed_at': (NOW-timedelta(hours=1)).isoformat()}, {'account_id': 'other'}, {'complete': False}):
            old = deepcopy(previous)
            old['cohort_cache'].update(change)
            self.wire.calls.clear()
            reader.collect(old)
            self.assertTrue(any(p.endswith('contacts/search') for _, p, _, _ in self.wire.calls))

    def test_duplicate_or_wrong_sequence_cached_members_rejected(self):
        reader = self.make()
        previous = reader.collect()
        previous['cohort_cache']['members'] *= 2
        self.wire.calls.clear()
        reader.collect(previous)
        self.assertTrue(any(p.endswith('contacts/search') for _, p, _, _ in self.wire.calls))

    def test_empty_fully_read_source_proves_scoped_zero(self):
        data = self.make(Wire(contacts=[], messages=[])).collect()
        self.assertTrue(data['coverage']['complete'])
        self.assertEqual(data['metrics']['sent_messages'], 0)

    def test_mcp_pagination_totals_native_exact_ids_and_batches_ten(self):
        messages = [metadata('m'+str(i)) for i in range(101)]
        contents = [content('m'+str(i)) for i in range(101)]
        data = self.make(Wire(messages=messages, contents=contents)).collect()
        self.assertTrue(data['coverage']['complete'])
        self.assertEqual(data['coverage']['messages_pages'], 2)
        self.assertEqual(data['metrics']['sent_messages'], 101)
        batches = [b['ids'] for _, p, b, _ in self.wire.calls if p.endswith('get_content')]
        self.assertEqual([len(ids) for ids in batches], [10]*10+[1])

    def test_461_messages_five_explicit_pages_hydrate_exactly_once_in_47_native_batches(self):
        wire = Wire(messages=[metadata('m'+str(i)) for i in range(461)],
                    contents=[content('m'+str(i)) for i in range(461)])
        data = self.make(wire).collect()
        self.assertTrue(data['coverage']['complete'])
        self.assertEqual(data['coverage']['messages_pages'], 5)
        self.assertEqual(data['coverage']['messages_total_entries'], 461)
        self.assertEqual(data['metrics']['sent_messages'], 461)
        native = [parse_qs(urlsplit(p).query)['ids[]'] for _, p, _, _ in wire.calls
                  if '/emailer_messages/search?' in p]
        self.assertEqual(len(native), 47)
        self.assertTrue(all(1 <= len(ids) <= 10 for ids in native))
        ids = [ident for batch in native for ident in batch]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), {'m'+str(i) for i in range(461)})

    def test_sent_partition_193_then_all_partial_preserves_originals_replies_and_independent_coverage(self):
        scheduled = ([metadata('q'+str(i), status='scheduled') for i in range(263)]
                     + [metadata('f'+str(i), status='failed') for i in range(5)])
        sent = [metadata('s'+str(i), replied=i == 0) for i in range(193)]
        wire = Wire(messages=scheduled+sent, contents=[content('s'+str(i)) for i in range(193)])
        mcp = FakeMCPTransport(wire)
        gmail = Mock(spec=['conversation'])
        gmail.conversation.return_value = thread([gmail_message(), gmail_message('abc2', outgoing=False)])
        def unstable_all(method, path, body, headers, timeout):
            if body.get('method') != 'tools/call': return None
            args = body['params']['arguments']
            if 'emailer_message_stats' not in args and args['page'] == '1':
                gmail.conversation.assert_called_once_with('abc123')
            if 'emailer_message_stats' not in args and args['page'] == '2':
                payload = {'pagination': {'page': '2', 'per_page': '100', 'total_entries': 461, 'total_pages': 5},
                    'emailer_messages': [{k: row[k] for k in ('id', 'contact_id', 'emailer_campaign_id', 'status')}
                                        for row in scheduled[:100]]}
                return 200, json.dumps({'jsonrpc': '2.0', 'id': body['id'],
                    'result': {'structuredContent': payload}}).encode(), {'Content-Type': 'application/json'}
            return None
        mcp.override = unstable_all
        data = self.make(wire, mcp_request=mcp, gmail=gmail).collect()
        cov = data['coverage']
        self.assertTrue(cov['sent_messages_complete'])
        self.assertEqual(cov['sent_messages_total'], 193)
        self.assertEqual(cov['sent_messages_pages'], 2)
        self.assertEqual(cov['sent_message_metadata_observed'], 193)
        self.assertFalse(cov['messages_complete'])
        self.assertFalse(cov['complete'])
        self.assertEqual(cov['messages_total_entries'], 461)
        self.assertEqual(cov['messages_pages'], 1)
        self.assertEqual(cov['message_metadata_observed'], 100)
        self.assertIn('duplicate_or_invalid_message', cov['reasons'])
        self.assertEqual(data['metrics']['sent_messages'], 193)
        self.assertEqual(data['metrics']['reply_signals'], 1)
        self.assertEqual(data['metrics']['human_replies'], 0)
        originals = [e for e in data['leads'][0]['events'] if e['event_type'] == 'email_sent']
        self.assertEqual(len(originals), 193)
        self.assertTrue(all(e['text'] == content()['body'] and e['receipt'] for e in originals))
        self.assertTrue(any(e['event_type'] == 'unclassified_reply' and e['text'] == 'Literal incoming body'
                            for e in data['leads'][0]['events']))
        gmail.conversation.assert_called_once_with('abc123')
        args = [c[2]['params']['arguments'] for c in mcp.calls if c[2]['method'] == 'tools/call']
        self.assertEqual([(a.get('emailer_message_stats'), a['page']) for a in args],
                         [(['sent'], '1'), (['sent'], '2'), (None, '1'), (None, '2')])
        native_ids = [ident for _, p, _, _ in wire.calls if '/emailer_messages/search?' in p
                      for ident in parse_qs(urlsplit(p).query)['ids[]']]
        self.assertEqual(len(native_ids), 293)
        self.assertEqual(len(native_ids), len(set(native_ids)))
        self.assertEqual(cov['native_message_ids_observed'], 293)

    def test_full_all_query_overlap_with_sent_deduplicates_native_events_and_ids_globally(self):
        sent = [metadata('s'+str(i)) for i in range(193)]
        scheduled = ([metadata('q'+str(i), status='scheduled') for i in range(263)]
                     + [metadata('f'+str(i), status='failed') for i in range(5)])
        wire = Wire(messages=sent+scheduled, contents=[content('s'+str(i)) for i in range(193)])
        data = self.make(wire).collect()
        self.assertTrue(data['coverage']['complete'])
        self.assertTrue(data['coverage']['sent_messages_complete'])
        self.assertTrue(data['coverage']['messages_complete'])
        self.assertEqual(data['coverage']['sent_messages_total'], 193)
        self.assertEqual(data['coverage']['message_metadata_observed'], 461)
        self.assertEqual(data['metrics']['sent_messages'], 193)
        events = data['leads'][0]['events']
        self.assertEqual(len(events), 461)
        self.assertEqual(len({e['provider_id'] for e in events}), 461)
        native_ids = [ident for _, p, _, _ in wire.calls if '/emailer_messages/search?' in p
                      for ident in parse_qs(urlsplit(p).query)['ids[]']]
        self.assertEqual(len(native_ids), 461)
        self.assertEqual(len(set(native_ids)), 461)
        self.assertEqual({ident for ident in native_ids}, {r['id'] for r in wire.messages})

    def test_sent_coverage_requires_verified_content_not_only_filtered_total(self):
        data = self.make(Wire(contents=[])).collect()
        self.assertEqual(data['coverage']['sent_messages_total'], 1)
        self.assertFalse(data['coverage']['sent_messages_complete'])
        self.assertFalse(data['coverage']['complete'])
        self.assertEqual(data['metrics']['sent_messages'], 0)
        self.assertIn('sent_filtered_content_unverified', data['coverage']['reasons'])
        native = [p for _, p, _, _ in self.wire.calls if '/emailer_messages/search?' in p]
        self.assertEqual(len(native), 1)

    def test_only_literal_sent_filter_or_no_filter_allowed_with_distinct_rpc_ids(self):
        base = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {
            'name': 'apollo_emailer_messages_search', 'arguments': {
                'emailer_campaign_id': APOLLO_SEQUENCE, 'page': '1', 'per_page': '100'}}}
        _validate_mcp('POST', '/mcp', base)
        sent = deepcopy(base)
        sent['id'] = 182
        sent['params']['arguments']['emailer_message_stats'] = ['sent']
        _validate_mcp('POST', '/mcp', sent)
        for stats in (None, [], 'sent', ['delivered'], ['sent', 'delivered'], ['sent', 'sent'], ('sent',)):
            bad = deepcopy(sent)
            bad['params']['arguments']['emailer_message_stats'] = stats
            with self.subTest(stats=stats), self.assertRaises(ExternalEmailError):
                _validate_mcp('POST', '/mcp', bad)
        sent['id'] = 2
        with self.assertRaises(ExternalEmailError):
            _validate_mcp('POST', '/mcp', sent)

    def test_explicit_mcp_total_exact_hundred_needs_no_empty_native_page(self):
        data = self.make(Wire(messages=[metadata('m'+str(i), status='scheduled') for i in range(100)]), max_pages=1).collect()
        self.assertTrue(data['coverage']['messages_complete'])
        self.assertEqual(data['coverage']['messages_pages'], 1)
        native = [p for _, p, _, _ in self.wire.calls if '/emailer_messages/search?' in p]
        self.assertEqual(len(native), 10)
        self.assertTrue(all(parse_qs(urlsplit(p).query)['per_page'] == ['10'] for p in native))

    def test_duplicate_message_and_wrong_sequence_owner_contact_fail(self):
        variants = [[metadata(), metadata()], [metadata(emailer_campaign_id='other')],
            [metadata(user_id='other')], [metadata(team_id='other')], [metadata(contact_id='other')]]
        for messages in variants:
            with self.subTest(messages=messages):
                data = self.make(Wire(messages=messages)).collect()
                self.assertFalse(data['coverage']['complete'])
                self.assertFalse(any(p.endswith('get_content') for _, p, _, _ in self.wire.calls))

    def test_mcp_handshake_session_protocol_and_sse_search_arguments(self):
        wire = Wire()
        mcp = FakeMCPTransport(wire, sse=True)
        data = self.make(wire, mcp_request=mcp).collect()
        self.assertTrue(data['coverage']['complete'])
        self.assertEqual([call[2]['method'] for call in mcp.calls],
                         ['initialize', 'notifications/initialized', 'tools/call', 'tools/call'])
        self.assertTrue(all(call[:2] == ('POST', '/mcp') for call in mcp.calls))
        self.assertEqual(mcp.calls[2][2]['params'], {'name': 'apollo_emailer_messages_search',
            'arguments': {'emailer_campaign_id': APOLLO_SEQUENCE, 'page': '1', 'per_page': '100',
                          'emailer_message_stats': ['sent']}})
        self.assertNotIn('Mcp-Session-Id', mcp.calls[0][3])
        for call in mcp.calls[1:]:
            self.assertEqual(call[3]['Mcp-Session-Id'], 'synthetic-session')
            self.assertEqual(call[3]['MCP-Protocol-Version'], '2025-11-25')
        self.assertEqual(data['coverage']['provider_requests'], len(wire.calls)+len(mcp.calls))
        native = [p for _, p, _, _ in wire.calls if '/emailer_messages/search?' in p]
        self.assertEqual(parse_qs(urlsplit(native[0]).query),
                         {'ids[]': ['message-1'], 'page': ['1'], 'per_page': ['10']})

    def test_changed_total_or_duplicate_page_preserves_hundred_verified_originals(self):
        for problem in ('total', 'duplicate', 'rate'):
            with self.subTest(problem=problem):
                wire = Wire(messages=[metadata('m'+str(i)) for i in range(101)],
                            contents=[content('m'+str(i)) for i in range(101)])
                mcp = FakeMCPTransport(wire)
                def corrupt(method, path, body, headers, timeout):
                    if body.get('method') != 'tools/call' or body['params']['arguments']['page'] != '2':
                        return None
                    if problem == 'rate':
                        return 429, b'{}', {}
                    payload = {'pagination': {'page': '2', 'per_page': '100',
                        'total_entries': 102 if problem == 'total' else 101, 'total_pages': 2},
                        'emailer_messages': [{'id': 'm0', 'contact_id': 'contact-1',
                                              'emailer_campaign_id': APOLLO_SEQUENCE}]}
                    return 200, json.dumps({'jsonrpc': '2.0', 'id': body['id'],
                        'result': {'structuredContent': payload}}).encode(), {'Content-Type': 'application/json'}
                mcp.override = corrupt
                data = self.make(wire, mcp_request=mcp).collect()
                self.assertFalse(data['coverage']['complete'])
                self.assertFalse(data['coverage']['messages_complete'])
                self.assertEqual(data['metrics']['sent_messages'], 100)
                events = data['leads'][0]['events']
                self.assertEqual(len(events), 100)
                self.assertTrue(all(e['text'] == content()['body'] and e['receipt'] for e in events))
                expected = {'total': 'apollo_messages_source_changed',
                            'duplicate': 'duplicate_or_invalid_message', 'rate': 'apollo_rate_limited'}[problem]
                self.assertIn(expected, data['coverage']['reasons'])
                calls = [c[2]['params']['arguments'] for c in mcp.calls if c[2]['method'] == 'tools/call']
                self.assertEqual(len(calls), 2 if problem == 'rate' else 4)
                self.assertEqual(len({json.dumps(c, sort_keys=True) for c in calls}), len(calls))
                self.assertEqual(len([p for _, p, _, _ in wire.calls if '/emailer_messages/search?' in p]), 10)

    def test_native_id_batch_rejects_missing_extra_duplicate_contact_sequence_owner(self):
        for problem in ('missing', 'extra', 'duplicate', 'contact', 'sequence', 'owner'):
            with self.subTest(problem=problem):
                wire = Wire(messages=[metadata('m1'), metadata('m2')], contents=[content('m1'), content('m2')])
                def corrupt(method, path, body):
                    if '/emailer_messages/search?' not in path:
                        return None
                    rows = [metadata('m1'), metadata('m2')]
                    if problem == 'missing': rows.pop()
                    elif problem == 'extra': rows[1]['id'] = 'not-enumerated'
                    elif problem == 'duplicate': rows[1]['id'] = 'm1'
                    elif problem == 'contact': rows[1]['contact_id'] = 'other-contact'
                    elif problem == 'sequence': rows[1]['emailer_campaign_id'] = 'other-sequence'
                    elif problem == 'owner': rows[1]['user_id'] = 'other-owner'
                    return 200, json.dumps({'emailer_messages': rows}).encode()
                wire.override = corrupt
                data = self.make(wire).collect()
                self.assertFalse(data['coverage']['complete'])
                self.assertEqual(data['metrics']['sent_messages'], 0)
                self.assertFalse(any(p.endswith('get_content') for _, p, _, _ in wire.calls))

    def test_later_native_error_keeps_preceding_batch_verified(self):
        wire = Wire(messages=[metadata('m'+str(i)) for i in range(21)],
                    contents=[content('m'+str(i)) for i in range(21)])
        def fail(method, path, body):
            if '/emailer_messages/search?' in path and 'm10' in parse_qs(urlsplit(path).query)['ids[]']:
                return 500, b'{}'
            return None
        wire.override = fail
        data = self.make(wire).collect()
        self.assertFalse(data['coverage']['messages_complete'])
        self.assertEqual(data['metrics']['sent_messages'], 11)
        self.assertEqual({e['provider_id'] for e in data['leads'][0]['events']},
                         {'m'+str(i) for i in range(10)} | {'m20'})
        native_ids = [ident for _, path, _, _ in wire.calls if '/emailer_messages/search?' in path
                      for ident in parse_qs(urlsplit(path).query)['ids[]']]
        self.assertEqual(len(native_ids), len(set(native_ids)))
        self.assertEqual(len(native_ids), 21)
        self.assertIn('native_messages_incomplete', data['coverage']['reasons'])

    def test_mcp_protocol_session_headers_ack_rpc_and_tool_errors_fail_closed(self):
        cases = ('version', 'session', 'headers', 'ack', 'rpc-id', 'server-request', 'tool', 'oversized', 'bad-sse')
        for problem in cases:
            with self.subTest(problem=problem):
                wire, mcp = Wire(), FakeMCPTransport(Wire())
                def corrupt(method, path, body, headers, timeout):
                    stage = body['method']
                    if problem == 'version' and stage == 'initialize':
                        result = {'protocolVersion': 'unsupported', 'capabilities': {'tools': {}}}
                        return 200, json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': result}).encode(), {'Content-Type': 'application/json'}
                    if stage != 'tools/call' and not (problem == 'ack' and stage == 'notifications/initialized'):
                        return None
                    if problem == 'session':
                        return 200, b'{}', {'Mcp-Session-Id': 'changed'}
                    if problem == 'headers':
                        return 200, b'{}', [('Mcp-Session-Id', 'a'), ('Mcp-Session-Id', 'b')]
                    if problem == 'ack': return 202, b'not-empty', {}
                    if problem == 'oversized': return 200, b'x'*1_000_001, {'Content-Type': 'application/json'}
                    if problem == 'bad-sse': return 200, b'data: {}', {'Content-Type': 'text/event-stream'}
                    result = {'structuredContent': {'emailer_messages': [], 'pagination': {}}}
                    response = {'jsonrpc': '2.0', 'id': body['id'], 'result': result}
                    if problem == 'rpc-id': response['id'] += 1
                    elif problem == 'server-request': response = {'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call'}
                    elif problem == 'tool': result['isError'] = True
                    return 200, json.dumps(response).encode(), {'Content-Type': 'application/json'}
                mcp.override = corrupt
                data = self.make(wire, mcp_request=mcp).collect()
                self.assertFalse(data['coverage']['complete'])
                self.assertEqual(data['metrics']['sent_messages'], 0)
                self.assertFalse(any('/emailer_messages/search?' in p for _, p, _, _ in wire.calls))

    def test_mcp_outbound_allowlist_rejects_other_tools_methods_and_unpinned_arguments(self):
        valid = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {
            'name': 'apollo_emailer_messages_search', 'arguments': {
                'emailer_campaign_id': APOLLO_SEQUENCE, 'page': '1', 'per_page': '100'}}}
        variants = []
        for field, value in (('emailer_campaign_id', 'other'), ('page', '181'), ('per_page', '10'), ('page', 1)):
            bad = deepcopy(valid)
            bad['params']['arguments'][field] = value
            variants.append(bad)
        for tool in ('apollo_emailer_messages_send', 'apollo_people_enrich', 'apollo_emailer_campaigns_show'):
            bad = deepcopy(valid)
            bad['params']['name'] = tool
            variants.append(bad)
        bad = deepcopy(valid)
        bad['params']['arguments']['sort'] = 'guessed'
        variants.append(bad)
        for bad in variants:
            with self.assertRaises(ExternalEmailError):
                _validate_mcp('POST', '/mcp', bad)
        for method, path in (('GET', '/mcp'), ('POST', '/other'), ('POST', 'https://evil.invalid/mcp')):
            with self.assertRaises(ExternalEmailError):
                _validate_mcp(method, path, valid)
        reader = self.make()
        reader.loader = Mock(side_effect=AssertionError('auth forbidden'))
        with self.assertRaisesRegex(ExternalEmailError, 'apollo_endpoint_blocked'):
            reader._call('GET', reader._native_message_path(['not-enumerated']))
        reader.loader.assert_not_called()

    def test_fixed_mcp_transport_sse_closes_at_matching_frame_without_waiting_for_eof(self):
        response = Mock()
        response.status = 200
        response.getheaders.return_value = [('Content-Type', 'text/event-stream')]
        raw = json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {
            'protocolVersion': '2025-11-25', 'capabilities': {'tools': {}}}}).encode()
        response.read1.side_effect = [b': heartbeat\n\n', b'data: '+raw+b'\n\n',
                                     AssertionError('must not wait for stream EOF')]
        connection = Mock()
        connection.getresponse.return_value = response
        with patch('thunder_os.apollo_campaign_reader.http.client.HTTPSConnection', return_value=connection) as connect:
            status, body, headers = _mcp_https_request('POST', '/mcp', deepcopy(_MCP_INITIALIZE), {}, 1)
        self.assertEqual(status, 200)
        self.assertIn(b'protocolVersion', body)
        self.assertEqual(connect.call_args.args, ('mcp.apollo.io', 443))
        self.assertEqual(connection.request.call_args.args, ('POST', '/mcp'))
        self.assertEqual(response.read1.call_count, 2)
        connection.close.assert_called_once()

    def test_fixed_mcp_transport_rejects_redirect_and_deadline_before_body_read(self):
        for problem in ('redirect', 'deadline'):
            with self.subTest(problem=problem):
                response = Mock(status=302 if problem == 'redirect' else 200)
                connection = Mock()
                connection.getresponse.return_value = response
                with patch('thunder_os.apollo_campaign_reader.http.client.HTTPSConnection', return_value=connection):
                    with patch('thunder_os.apollo_campaign_reader.time.monotonic',
                               side_effect=[0, 2] if problem == 'deadline' else None,
                               return_value=0):
                        with self.assertRaises(ExternalEmailError):
                            _mcp_https_request('POST', '/mcp', deepcopy(_MCP_INITIALIZE), {}, 1)
                response.read1.assert_not_called()
                connection.close.assert_called_once()

    def test_scheduled_and_failed_are_never_sent_or_content_requests(self):
        data = self.make(Wire(messages=[metadata(status='scheduled', due_at=SENT), metadata('m2', status='failed')])).collect()
        self.assertEqual(data['metrics']['sent_messages'], 0)
        self.assertEqual({e['event_type'] for e in data['leads'][0]['events']}, {'scheduled', 'provider_status'})
        self.assertFalse(any(p.endswith('get_content') for _, p, _, _ in self.wire.calls))

    def test_missing_content_preserves_metadata_but_is_partial(self):
        data = self.make(Wire(contents=[])).collect()
        self.assertFalse(data['coverage']['complete'])
        self.assertEqual(data['leads'][0]['events'][0]['provider_id'], 'message-1')
        self.assertFalse(data['leads'][0]['events'][0]['receipt'])

    def test_content_contract_rejects_body_status_format_date_and_recipient(self):
        for change in ({'body': ''}, {'body': None}, {'status': 'scheduled'}, {'body_format': 'html'},
                {'sent_at': '2026-10-25T10:00:00Z'}, {'recipients': [{'email': 'other@example.invalid', 'type': 'to'}]}):
            with self.subTest(change=change):
                data = self.make(Wire(contents=[content(**change)])).collect()
                self.assertFalse(data['coverage']['complete'])
                self.assertEqual(data['metrics']['sent_messages'], 0)

    def test_rate_limit_is_one_attempt_no_inline_retry(self):
        wire = Wire()
        wire.override = lambda *args: (429, b'{"secret":"do-not-leak"}')
        data = self.make(wire).collect()
        self.assertEqual(data['coverage']['reasons'], ['apollo_rate_limited'])
        self.assertEqual(len(wire.calls), 1)
        self.assertNotIn('do-not-leak', json.dumps(data))

    def test_timeout_bad_json_redirect_and_auth_fail_closed(self):
        for status, raw in ((200, b'not-json'), (302, b'{}'), (401, b'{}'), (200, b'[]'), (200, b'x'*1_000_001)):
            wire = Wire()
            wire.override = lambda *args: (status, raw)
            data = self.make(wire).collect()
            self.assertFalse(data['coverage']['complete'])
            self.assertEqual(len(wire.calls), 1)
        reader = self.make()
        reader.request = Mock(side_effect=TimeoutError('PRIVATE'))
        self.assertNotIn('PRIVATE', json.dumps(reader.collect()))
        self.assertEqual(reader.request.call_count, 1)

    def test_call_limit_deadline_and_concurrency(self):
        data = self.make(max_calls=1).collect()
        self.assertIn('apollo_read_budget_exhausted', data['coverage']['reasons'])
        self.assertEqual(len(self.wire.calls), 1)
        ticks = iter([0, 0, 361])
        data = self.make(monotonic=lambda: next(ticks)).collect()
        self.assertIn('apollo_read_budget_exhausted', data['coverage']['reasons'])
        reader = self.make()
        reader.lock.acquire()
        with self.assertRaisesRegex(ExternalEmailError, 'apollo_reader_busy'):
            reader.collect()
        reader.lock.release()

    def test_allowlist_blocks_writers_arbitrary_query_and_content_ids_before_auth(self):
        reader = self.make()
        reader.loader = Mock(side_effect=AssertionError('credential access'))
        for method, path, body in [('POST', '/api/v1/emailer_messages/a/send_now', {}),
                ('GET', 'https://evil.invalid/', None), ('GET', '/api/v1/emailer_messages/search?emailer_campaign_ids%5B%5D='+APOLLO_SEQUENCE+'&page=1&per_page=100', None),
                ('GET', SEQUENCE_PATH.replace('q_name=Ygor', 'q_name=Other'), None),
                ('GET', SEQUENCE_PATH+'&extra=true', None),
                ('POST', SEQUENCE_PATH, {}),
                ('POST', '/api/v1/contacts/search', {'q_keywords': 'x'}),
                ('POST', '/api/v1/emailer_messages/get_content', {'ids': ['a']*11, 'body_format': 'plain'})]:
            with self.assertRaisesRegex(ExternalEmailError, 'apollo_endpoint_blocked'):
                reader._call(method, path, body)
        reader.loader.assert_not_called()
        self.assertEqual(self.wire.calls, [])

    def test_default_private_loader_only_explicit_path(self):
        with patch('thunder_os.apollo_campaign_reader.private_text', return_value='synthetic') as loader:
            wire = Wire()
            reader = ApolloCampaignReadOnly(request=wire, mcp_request=FakeMCPTransport(wire))
            reader.collect()
            self.assertTrue(all(call.args == (KEY_FILE,) for call in loader.call_args_list))
        with patch('thunder_os.apollo_campaign_reader.private_text', side_effect=ExternalEmailError('configuration_pending')):
            self.assertFalse(enabled())

    def test_actual_native_fraction_content_second_truncation_preserves_both_timestamps(self):
        native = '2026-10-22T17:25:51.542+00:00'
        reported = '2026-10-22T17:25:51+00:00'
        data = self.make(Wire(messages=[metadata(completed_at=native)],
                              contents=[content(sent_at=reported)])).collect()
        self.assertTrue(data['coverage']['sent_messages_complete'])
        self.assertEqual(data['metrics']['sent_messages'], 1)
        event = data['leads'][0]['events'][0]
        self.assertEqual(event['occurred_at'], native)
        self.assertEqual(event['native_completed_at'], native)
        self.assertEqual(event['content_sent_at'], reported)
        self.assertEqual(event['timestamp_match'], 'content_second_truncation')

    def test_timestamp_truncation_is_one_direction_same_second_only(self):
        for native, reported, accepted in (
            ('2026-10-22T17:25:51.999999+00:00', '2026-10-22T17:25:51+00:00', True),
            ('2026-10-22T17:25:51.542+00:00', '2026-10-22T17:25:52+00:00', False),
            ('2026-10-22T17:25:51.999999+00:00', '2026-10-22T17:25:52+00:00', False),
            ('2026-10-22T17:25:51.542+00:00', '2026-10-22T17:25:51.000+00:00', False),
            ('2026-10-22T17:25:51+00:00', '2026-10-22T17:25:51.542+00:00', False),
            ('2026-10-22T17:25:51.542001+00:00', '2026-10-22T17:25:51.542002+00:00', False)):
            with self.subTest(native=native, content=reported):
                data = self.make(Wire(messages=[metadata(completed_at=native)],
                                      contents=[content(sent_at=reported)])).collect()
                self.assertEqual(data['metrics']['sent_messages'], 1 if accepted else 0)
                if not accepted:
                    self.assertIn('sent_timestamp_mismatch', data['coverage']['reasons'])

    def test_other_sender_is_factual_outbound_without_cross_mailbox_gmail_binding(self):
        for replied in (False, True):
            gmail = Mock(spec=['conversation', 'contact_search'])
            sender = 'ygor@r3xdigital.tech'
            data = self.make(Wire(messages=[metadata(from_email=sender, replied=replied)]), gmail=gmail).collect()
            self.assertEqual(data['metrics']['sent_messages'], 1)
            self.assertTrue(data['coverage']['sent_messages_complete'])
            event = next(e for e in data['leads'][0]['events'] if e['event_type'] == 'email_sent')
            self.assertEqual(event['sender'], sender)
            self.assertEqual(event['text'], content()['body'])
            gmail.conversation.assert_not_called()
            gmail.contact_search.assert_not_called()
            if replied:
                self.assertIn('gmail_reply_mailbox_unavailable', data['coverage']['reasons'])
                self.assertEqual(data['metrics']['reply_signals'], 1)
                self.assertEqual(data['metrics']['human_replies'], 0)

    def test_invalid_sender_does_not_become_a_verified_sent_original(self):
        for sender in (None, '', 'invalid', 'Ygor <ygor@example.invalid>'):
            with self.subTest(sender=sender):
                data = self.make(Wire(messages=[metadata(from_email=sender)])).collect()
                self.assertEqual(data['metrics']['sent_messages'], 0)
                self.assertIn('invalid_sender', data['coverage']['reasons'])
                self.assertFalse(data['leads'][0]['events'][0]['receipt'])

    def test_actual_distinct_gmail_and_apollo_clocks_match_by_identity_body_and_subject(self):
        native, reported = '2026-10-22T17:25:51.542+00:00', '2026-10-22T17:25:51+00:00'
        for bad in (None, 'id', 'body'):
            with self.subTest(bad=bad):
                original = gmail_message(internal_date='1792689950000')
                if bad == 'id': original['id'] = 'badd1'
                if bad == 'body': original['bodies']['text'] = 'different literal'
                gmail = Mock(spec=['conversation'])
                gmail.conversation.return_value = thread([original, gmail_message('abc2', outgoing=False)])
                data = self.make(Wire(messages=[metadata(completed_at=native, replied=True)],
                                      contents=[content(sent_at=reported)]), gmail=gmail).collect()
                incoming = [e for e in data['leads'][0]['events'] if e['provider'] == 'gmail']
                if bad == 'id':
                    self.assertEqual(incoming, [])
                    self.assertIn('gmail_outbound_original_mismatch', data['coverage']['reasons'])
                elif bad == 'body':
                    self.assertEqual(len(incoming), 1)
                    self.assertEqual(incoming[0]['source_association'], 'thread_context')
                    self.assertIsNone(incoming[0]['lead_email'])
                    self.assertIsNone(incoming[0]['ref_sent'])
                    self.assertIsNone(incoming[0]['human_reply'])
                    self.assertFalse(incoming[0]['receipt'])
                    self.assertIn('gmail_outbound_original_mismatch', data['coverage']['reasons'])
                else:
                    self.assertEqual(len(incoming), 1)
                    self.assertEqual(incoming[0]['gmail_original_date'], '2026-10-22T17:25:50Z')
                    self.assertEqual(incoming[0]['apollo_completed_at'], native)
                    self.assertEqual(incoming[0]['ref_sent'], 'message-1')
                    self.assertEqual(incoming[0]['text'], 'Literal incoming body')
                    self.assertIsNone(incoming[0]['human_reply'])

    def test_gmail_line_ending_only_comparison_keeps_original_copies_and_distinct_dates(self):
        native, reported = '2026-10-22T17:25:51.542+00:00', '2026-10-22T17:25:51+00:00'
        apollo_body = 'Original words\n\nSecond paragraph.\nYgor signature'
        for ending in ('\r\r\n', '\r\n', '\r'):
            for mutation in (None, 'word', 'space', 'strip'):
                with self.subTest(ending=repr(ending), mutation=mutation):
                    gmail_body = apollo_body.replace('\n', ending)
                    if mutation == 'word': gmail_body = gmail_body.replace('words', 'changed')
                    if mutation == 'space': gmail_body = gmail_body.replace('Original words', 'Original  words')
                    if mutation == 'strip': gmail_body += ' '
                    original = gmail_message(internal_date='1792689950000', bodies={'text': gmail_body})
                    incoming_body = 'Keep incoming\r\r\noriginal line endings'
                    incoming = gmail_message('abc2', outgoing=False, bodies={'text': incoming_body})
                    gmail = Mock(spec=['conversation'])
                    gmail.conversation.return_value = thread([original, incoming])
                    data = self.make(Wire(messages=[metadata(completed_at=native, replied=True)],
                        contents=[content(sent_at=reported, body=apollo_body)]), gmail=gmail).collect()
                    outgoing = next(e for e in data['leads'][0]['events'] if e['event_type'] == 'email_sent')
                    self.assertEqual(outgoing['text'], apollo_body)
                    self.assertEqual(outgoing['occurred_at'], native)
                    self.assertEqual(original['bodies']['text'], gmail_body)
                    replies = [e for e in data['leads'][0]['events'] if e['provider'] == 'gmail']
                    if mutation is not None:
                        self.assertEqual(len(replies), 1)
                        self.assertEqual(replies[0]['source_association'], 'thread_context')
                        self.assertEqual(replies[0]['gmail_original_body_match'], 'different')
                        self.assertIsNone(replies[0]['ref_sent'])
                        self.assertIsNone(replies[0]['lead_email'])
                        self.assertIsNone(replies[0]['human_reply'])
                        self.assertFalse(replies[0]['receipt'])
                        self.assertEqual(replies[0]['text'], incoming_body)
                        self.assertIn('gmail_outbound_original_mismatch', data['coverage']['reasons'])
                    else:
                        self.assertEqual(len(replies), 1)
                        self.assertEqual(replies[0]['text'], incoming_body)
                        self.assertEqual(replies[0]['gmail_original_body_match'], 'line_endings_only')
                        self.assertEqual(replies[0]['gmail_original_date'], '2026-10-22T17:25:50Z')
                        self.assertEqual(replies[0]['apollo_completed_at'], native)

    def test_native_seqd_injected_transport_is_get_only_timeout_bounded_and_429_stops(self):
        transport = Mock(return_value=(429, b'{}'))
        gmail = SEQDClient(lambda: 'x'*32, request=transport)
        data = self.make(Wire(messages=[metadata(replied=True)]), gmail=gmail).collect()
        self.assertIn('gmail_rate_limited', data['coverage']['reasons'])
        transport.assert_called_once()
        args = transport.call_args.args
        self.assertEqual(args[0:3], ('GET', '/v1/threads/abc123', None))
        self.assertLessEqual(args[-1], 4)
        self.assertEqual(data['metrics']['human_replies'], 0)

    def test_reply_flag_never_becomes_human_or_original(self):
        data = self.make(Wire(messages=[metadata(replied=True, reply_class=None)])).collect()
        self.assertEqual(data['metrics']['reply_signals'], 1)
        self.assertEqual(data['metrics']['human_replies'], 0)
        signal = next(e for e in data['leads'][0]['events'] if e['event_type'] == 'reply_signal')
        self.assertIsNone(signal['human_reply'])
        self.assertFalse(signal['receipt'])
        self.assertIn('gmail_reply_reader_unavailable', data['coverage']['reasons'])

    def replies(self, messages, **thread_changes):
        gmail = Mock(spec=['conversation'])
        gmail.conversation.return_value = thread(messages, **thread_changes)
        data = self.make(Wire(messages=[metadata(replied=True)]), gmail=gmail).collect()
        gmail.conversation.assert_called_once_with('abc123')
        return data

    def test_exact_outbound_original_incoming_body_unclassified_and_dedupe_ids(self):
        data = self.replies([gmail_message(), gmail_message('abc2', outgoing=False)])
        incoming = next(e for e in data['leads'][0]['events'] if e['provider'] == 'gmail')
        self.assertEqual(incoming['event_type'], 'unclassified_reply')
        self.assertEqual(incoming['text'], 'Literal incoming body')
        self.assertEqual(incoming['gmail_message_id'], 'abc2')
        self.assertEqual(incoming['ref_sent'], 'message-1')
        self.assertIsNone(incoming['human_reply'])
        self.assertEqual(incoming['sender'], LEAD)
        self.assertEqual(data['metrics']['human_replies'], 0)

    def test_third_party_original_preserved_without_canonical_lead_attribution(self):
        data = self.replies([gmail_message(), gmail_message('abc2', outgoing=False, sender='fernanda@example.invalid')])
        incoming = next(e for e in data['leads'][0]['events'] if e['provider'] == 'gmail')
        self.assertEqual(incoming['event_type'], 'third_party_reply')
        self.assertEqual(incoming['sender'], 'fernanda@example.invalid')
        self.assertEqual(incoming['source_association'], 'thread_context')
        self.assertIsNone(incoming['human_reply'])
        self.assertFalse(incoming['receipt'])
        self.assertEqual(incoming['text'], 'Literal incoming body')
        self.assertEqual(data['metrics']['human_replies'], 0)

    def test_automatic_markers_subject_or_headers(self):
        for changes in ({'subject': 'Out of office: back later'}, {'headers': {'Auto-Submitted': 'auto-replied'}},
                {'headers': [{'name': 'Auto-Submitted', 'value': 'auto-replied'}]}):
            data = self.replies([gmail_message(), gmail_message('abc2', outgoing=False, **changes)])
            incoming = next(e for e in data['leads'][0]['events'] if e['provider'] == 'gmail')
            self.assertEqual(incoming['event_type'], 'auto_reply_received')
            self.assertFalse(incoming['human_reply'])

    def test_wrong_thread_outbound_id_body_and_sender_are_held(self):
        for original in (gmail_message('bad1'), gmail_message(subject='wrong subject'),
                gmail_message(thread_id='other'), gmail_message(sender='other@example.invalid'),
                gmail_message(to='other@example.invalid'), gmail_message(complete=False)):
            data = self.replies([original, gmail_message('abc2', outgoing=False)])
            self.assertIn('gmail_outbound_original_mismatch', data['coverage']['reasons'])
            self.assertFalse(any(e['provider'] == 'gmail' for e in data['leads'][0]['events']))

    def test_changed_original_body_exposes_fernanda_only_as_context_without_receipt_or_association(self):
        native_body = 'Podemos conversar.\n\n\n  \n  \n\nYgor signature'
        gmail_body = 'Podemos conversar.\n\n\n\n\nYgor signature'
        for sender, subject in ((LEAD, 'Automatic reply: received'),
                                ('fernanda@example.invalid', 'Original subject')):
            original = gmail_message(bodies={'text': gmail_body})
            incoming = gmail_message('abc2', outgoing=False, sender=sender, subject=subject)
            gmail = Mock(spec=['conversation'])
            gmail.conversation.return_value = thread([original, incoming])
            data = self.make(Wire(messages=[metadata(replied=True)], contents=[content(body=native_body)]), gmail=gmail).collect()
            replies = [e for e in data['leads'][0]['events'] if e['provider'] == 'gmail']
            self.assertEqual(len(replies), 1)
            event = replies[0]
            self.assertEqual(event['sender'], sender)
            self.assertEqual(event['text'], incoming['bodies']['text'])
            self.assertEqual(event['source_association'], 'thread_context')
            self.assertEqual(event['gmail_original_body_match'], 'different')
            self.assertIsNone(event['lead_email'])
            self.assertIsNone(event['ref_sent'])
            self.assertIsNone(event['human_reply'])
            self.assertFalse(event['receipt'])
            self.assertFalse(data['coverage']['complete'])
            self.assertIn('gmail_outbound_original_mismatch', data['coverage']['reasons'])
            self.assertEqual(next(e['text'] for e in data['leads'][0]['events']
                                  if e['event_type'] == 'email_sent'), native_body)
            if sender != LEAD:
                self.assertEqual(event['event_type'], 'third_party_reply')

    def test_partial_gmail_body_visible_without_complete_receipt(self):
        data = self.replies([gmail_message(), gmail_message('abc2', outgoing=False, complete=False)], complete=False)
        incoming = next(e for e in data['leads'][0]['events'] if e['provider'] == 'gmail')
        self.assertEqual(incoming['text'], 'Literal incoming body')
        self.assertFalse(incoming['content_complete'])
        self.assertFalse(data['coverage']['complete'])

    def test_flagged_contact_search_only_after_outgoing_only_thread(self):
        gmail = Mock(spec=['conversation', 'contact_search'])
        gmail.conversation.return_value = thread([gmail_message()])
        gmail.contact_search.return_value = {'status': 'ok', 'mailbox': MAILBOX, 'email': LEAD,
            'context_only': True, 'receipt_eligible': False, 'messages': [], 'next_cursor': None}
        data = self.make(Wire(messages=[metadata(replied=True)]), gmail=gmail).collect()
        gmail.contact_search.assert_called_once_with(LEAD, cursor=None)
        self.assertEqual(data['metrics']['reply_signals'], 1)
        self.assertEqual(data['metrics']['human_replies'], 0)
        self.assertIn('reply_original_unavailable', data['coverage']['reasons'])
        gmail.reset_mock()
        self.make(gmail=gmail).collect()
        gmail.conversation.assert_not_called()
        gmail.contact_search.assert_not_called()

    def test_discovery_five_pages_bound_and_no_absence_claim(self):
        gmail = Mock(spec=['conversation', 'contact_search'])
        gmail.conversation.return_value = thread([gmail_message()])
        def search(addr, cursor=None):
            index = gmail.contact_search.call_count
            return {'status': 'ok', 'mailbox': MAILBOX, 'email': addr, 'context_only': True,
                'receipt_eligible': False, 'messages': [], 'next_cursor': 'cursor'+str(index)}
        gmail.contact_search.side_effect = search
        data = self.make(Wire(messages=[metadata(replied=True)]), gmail=gmail).collect()
        self.assertEqual(gmail.contact_search.call_count, 5)
        self.assertIn('gmail_discovery_page_limit', data['coverage']['reasons'])
        self.assertFalse(data['coverage']['complete'])


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.repo = Mock()
        self.repo.get_state.return_value = None
        self.provider = Mock()
        wire = Wire()
        self.provider.collect.return_value = ApolloCampaignReadOnly(lambda: 'fake', request=wire,
            mcp_request=FakeMCPTransport(wire), clock=lambda: NOW).collect()
        self.reader = ApolloCampaignReader(self.repo, self.provider, enable_check=lambda: True, clock=lambda: self.now)

    def test_default_composition_injects_seqd_without_loading_credentials(self):
        with patch('thunder_os.apollo_campaign_reader.private_text', side_effect=AssertionError('auth access')):
            reader = ApolloCampaignReader(self.repo, enable_check=lambda: False)
            self.assertIsInstance(reader.provider.gmail, SEQDClient)
            reader.snapshot()
            reader.refresh()

    def test_snapshot_cache_only_and_permission_checked_every_access(self):
        self.reader.snapshot()
        self.provider.collect.assert_not_called()
        self.reader.refresh()
        self.repo.put_state.assert_called_once()
        self.assertEqual(self.repo.put_state.call_args.args[:2], (KIND, CAMPAIGN))
        self.reader.enable_check = lambda: False
        self.repo.reset_mock()
        data = self.reader.snapshot()
        self.reader.refresh()
        self.assertEqual(data['status'], 'disabled')
        self.assertEqual(data['leads'], [])
        self.repo.get_state.assert_not_called()
        self.repo.put_state.assert_not_called()

    def test_stale_snapshot_explicit_and_cannot_preserve_complete(self):
        self.reader.refresh()
        self.repo.get_state.return_value = self.repo.put_state.call_args.args[2]
        self.now += timedelta(seconds=181)
        data = self.reader.snapshot()
        self.assertEqual(data['status'], 'stale')
        self.assertTrue(data['stale'])
        self.assertFalse(data['coverage']['complete'])
        self.assertEqual(data['leads'][0]['id'], 'contact-1')

    def test_interval_and_nonblocking_refresh_lock(self):
        self.reader.refresh()
        self.repo.get_state.return_value = self.repo.put_state.call_args.args[2]
        self.reader.refresh()
        self.assertEqual(self.provider.collect.call_count, 1)
        self.reader.lock.acquire()
        self.now += timedelta(seconds=61)
        self.reader.refresh()
        self.assertEqual(self.provider.collect.call_count, 1)
        self.reader.lock.release()

    def test_provider_failure_preserves_original_dates_and_no_secrets(self):
        self.reader.refresh()
        old = self.repo.put_state.call_args.args[2]
        self.repo.get_state.return_value = old
        self.now += timedelta(seconds=61)
        self.provider.collect.side_effect = RuntimeError('PRIVATE')
        self.reader.refresh()
        new = self.repo.put_state.call_args.args[2]
        self.assertEqual(new['observed_at'], old['observed_at'])
        self.assertEqual(new['last_success_at'], old['last_success_at'])
        self.assertEqual(new['leads'], old['leads'])
        self.assertEqual(new['error_code'], 'apollo_read_unavailable')
        self.assertNotIn('PRIVATE', json.dumps(new))

    def test_real_collect_429_partial_empty_preserves_prior_data_dates_and_failed_coverage(self):
        for stage in ('profile', 'mcp_cached_cohort'):
            with self.subTest(stage=stage):
                wire = Wire()
                mcp = FakeMCPTransport(wire)
                provider = ApolloCampaignReadOnly(lambda: 'fake', request=wire, mcp_request=mcp,
                                                  clock=lambda: self.now)
                repo = Mock()
                repo.get_state.return_value = None
                reader = ApolloCampaignReader(repo, provider, enable_check=lambda: True, clock=lambda: self.now)
                reader.refresh()
                old = deepcopy(repo.put_state.call_args.args[2])
                self.assertEqual(old['metrics']['sent_messages'], 1)
                repo.get_state.return_value = old
                wire.calls.clear()
                mcp.calls.clear()
                self.now += timedelta(seconds=61)
                if stage == 'profile':
                    wire.override = lambda *args: (429, b'{}')
                else:
                    mcp.override = lambda method, path, body, headers, timeout: (429, b'{}', {}) if body['method'] == 'tools/call' else None
                reader.refresh()
                failed = repo.put_state.call_args.args[2]
                self.assertEqual(failed['leads'], old['leads'])
                self.assertEqual(failed['metrics']['sent_messages'], 1)
                self.assertEqual(failed['observed_at'], old['observed_at'])
                self.assertEqual(failed['last_success_at'], old['last_success_at'])
                self.assertEqual(failed['last_attempt_at'], self.now.isoformat())
                self.assertEqual(failed['error_code'], 'apollo_rate_limited')
                self.assertEqual(failed['status'], 'error')
                self.assertFalse(failed['coverage']['complete'])
                self.assertFalse(failed['coverage']['messages_complete'])
                self.assertTrue(failed['coverage']['retained_observation'])
                self.assertIn('apollo_rate_limited', failed['coverage']['reasons'])
                self.assertEqual(len(wire.calls), 1 if stage == 'profile' else 2)
                self.assertEqual(len(mcp.calls), 0 if stage == 'profile' else 3)
                repo.get_state.return_value = failed
                self.now += timedelta(seconds=181)
                snapshot = reader.snapshot()
                self.assertTrue(snapshot['stale'])
                self.assertEqual(snapshot['status'], 'stale')
                self.assertEqual(snapshot['observed_at'], old['observed_at'])
                self.assertEqual(snapshot['metrics']['sent_messages'], 1)

    def test_revoked_during_collection_never_persists(self):
        active = [True]
        self.reader.enable_check = lambda: active[0]
        def collect(**kw):
            active[0] = False
            return {}
        self.provider.collect.side_effect = collect
        self.reader.refresh()
        self.repo.put_state.assert_not_called()


if __name__ == '__main__':
    unittest.main()
