"""S46 C1/C2: synthetic HTTP/SEQD fixtures only, no auth files or network.

Named assertions cover endpoint allowlist, complete/partial contacts, stable IDs,
source changes, cache age, budgets, scheduled/sent/content proof, reply signals,
exact Gmail originals/third parties, and repository-only snapshots. Integration
with real Apollo/Gmail and the final independent verifier belong to main.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from thunder_os.apollo_campaign_reader import (ApolloCampaignReadOnly, ApolloCampaignReader,
    APOLLO_SEQUENCE, APOLLO_TEAM, OWNER, CAMPAIGN, KIND, KEY_FILE, SEQUENCE_PATH, enabled, _validate_mcp, _mcp_https_request, _MCP_INITIALIZE, INTERVAL, SNAPSHOT_TTL, COHORT_TTL, _scope_identity, ENDPOINT_MINUTE_LIMIT, ENDPOINT_HOUR_LIMIT, ENDPOINT_DAY_LIMIT)
from thunder_os.external_email_import import ExternalEmailError
from thunder_os.seqd_client import MAILBOX, SEQDClient
from thunder_os.db import Repository

_OperationalApolloCampaignReader = ApolloCampaignReader
def ApolloCampaignReader(repo, *args, **kwargs):
    if not isinstance(repo, Repository):
        reader = object.__new__(_OperationalApolloCampaignReader)
        reader.repo = repo
        reader.provider = (args[0] if args else None) or ApolloCampaignReadOnly(gmail=SEQDClient())
        reader.enable_check = kwargs.get('enable_check', enabled)
        reader.clock = kwargs.get('clock') or (lambda: datetime.now(timezone.utc))
        reader.lock, reader.stop_event, reader.thread = threading.Lock(), threading.Event(), None
        reader._runtime_lock = threading.Lock()
        reader._runtime = {'thread_started': False, 'in_progress': False, 'phase': 'idle',
                           'last_attempt_at': None, 'cooldown_until': None,
                           'last_loop_error': None, 'last_refresh_error': None}
        return reader
    return _OperationalApolloCampaignReader(repo, *args, **kwargs)

NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
SENT = '2026-10-26T10:00:00Z'
LEAD = 'lead@example.invalid'


class CohortPresentationTests(unittest.TestCase):
    def test_snapshot_separates_observed_cohort_from_retained_history(self):
        current = {'id': 'current', 'membership': {'emailer_campaign_id': APOLLO_SEQUENCE}, 'events': []}
        old = {'id': 'old', 'events': []}
        state = {'status': 'partial', 'observed_at': SENT, 'leads': [current, old],
                 'coverage': {'complete': False, 'contacts_complete': True,
                              'contacts_observed_at': NOW.isoformat(), 'messages_complete': False},
                 'cohort_cache': {'complete': True, 'account_id': APOLLO_TEAM, 'owner_id': OWNER,
                                  'external_campaign_id': APOLLO_SEQUENCE,
                                  'observed_at': NOW.isoformat(), 'members': [current]}}
        repo = Mock()
        repo.get_state.return_value = state
        snapshot = ApolloCampaignReader(repo, enable_check=lambda: True, clock=lambda: NOW).snapshot()
        self.assertEqual(snapshot['cohort_observation']['count'], 1)
        self.assertEqual([lead['cohort_presence'] for lead in snapshot['leads']], ['observed', 'historical'])
        self.assertEqual(snapshot['observed_at'], SENT)
        self.assertFalse(snapshot['coverage']['messages_complete'])
        self.assertNotIn('cohort_presence', state['leads'][0])

    def test_same_id_different_email_keeps_history_historical(self):
        current = {'id': 'same', 'email': 'new@example.invalid',
                   'membership': {'emailer_campaign_id': APOLLO_SEQUENCE}, 'events': []}
        old = {'id': 'same', 'email': 'old@example.invalid', 'events': [
            {'provider': 'apollo', 'provider_id': 'old-event', 'event_type': 'email_sent'}]}
        repo = Mock(); repo.get_state.return_value = {'leads': [old]}
        data = {'leads': [current], 'observed_at': NOW.isoformat(),
                'coverage': {'complete': True, 'contacts_complete': True,
                             'contacts_observed_at': NOW.isoformat()}, 'metrics': {},
                'cohort_cache': {'complete': True, 'account_id': APOLLO_TEAM, 'owner_id': OWNER,
                                 'external_campaign_id': APOLLO_SEQUENCE,
                                 'observed_at': NOW.isoformat(), 'members': [current]}}
        provider = Mock(); provider.collect.return_value = data
        reader = ApolloCampaignReader(repo, provider, enable_check=lambda: True, clock=lambda: NOW)
        reader.refresh()
        repo.get_state.return_value = repo.put_state.call_args.args[2]
        leads = reader.snapshot()['leads']
        self.assertEqual([(x['email'], x['cohort_presence']) for x in leads],
                         [('new@example.invalid', 'observed'), ('old@example.invalid', 'historical')])
        self.assertEqual(leads[1]['events'][0]['provider_id'], 'old-event')

    def test_snapshot_does_not_label_history_when_cohort_scope_or_time_invalid(self):
        state = {'status': 'partial', 'leads': [{'id': 'old', 'events': []}],
                 'coverage': {'contacts_complete': True, 'contacts_observed_at': NOW.isoformat()},
                 'cohort_cache': {'complete': True, 'account_id': 'other', 'owner_id': OWNER,
                                  'external_campaign_id': APOLLO_SEQUENCE,
                                  'observed_at': NOW.isoformat(), 'members': []}}
        repo = Mock(); repo.get_state.return_value = state
        snapshot = ApolloCampaignReader(repo, enable_check=lambda: True, clock=lambda: NOW).snapshot()
        self.assertIsNone(snapshot['cohort_observation']['count'])
        self.assertEqual(snapshot['leads'][0]['cohort_presence'], 'unknown')

    def test_rejects_invalid_membership_and_timestamp(self):
        repo = Mock()
        for membership, stamp in [({'emailer_campaign_id': 'wrong'}, NOW.isoformat()),
                                  (None, NOW.isoformat()),
                                  ({'emailer_campaign_id': APOLLO_SEQUENCE}, '2026-10-26')]:
            with self.subTest(membership=membership, stamp=stamp):
                member = {'id': 'lead', 'membership': membership}
                repo.get_state.return_value = {'status': 'partial', 'leads': [member],
                    'coverage': {'contacts_complete': True, 'contacts_observed_at': stamp},
                    'cohort_cache': {'complete': True, 'account_id': APOLLO_TEAM, 'owner_id': OWNER,
                        'external_campaign_id': APOLLO_SEQUENCE, 'observed_at': stamp, 'members': [member]}}
                snap = ApolloCampaignReader(repo, enable_check=lambda: True, clock=lambda: NOW).snapshot()
                self.assertIsNone(snap['cohort_observation']['count'])
                self.assertEqual(snap['leads'][0]['cohort_presence'], 'unknown')


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
                    'id', 'contact_id', 'emailer_campaign_id', 'status', 'replied', 'completed_at', 'delivered', 'bounced', 'variant',
                    'open_count', 'click_count', 'snapshot_timestamp', 'opened', 'clicked'}} for row in rows]}}
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

    def test_orphans_do_not_abort_pages_or_become_events(self):
        orphan = [metadata('orphan-' + str(i), contact_id='unknown-contact') for i in range(100)]
        wire = Wire(messages=orphan + [metadata()], contents=[content()])
        reader = self.make(wire)
        first = reader.collect()
        cov = first['coverage']
        self.assertEqual(cov['messages_pages'], 2)
        self.assertEqual(cov['sent_messages_pages'], 2)
        self.assertFalse(cov['complete'])
        self.assertFalse(cov['messages_complete'])
        self.assertIn('message_contact_outside_cohort', cov['reasons'])
        self.assertEqual(cov['unreconciled_messages_count'], 100)
        diagnostic = json.dumps(cov['unreconciled_messages'])
        self.assertNotIn('unknown-contact', diagnostic)
        self.assertNotIn('orphan-', diagnostic)
        self.assertNotIn(LEAD, diagnostic)
        self.assertEqual([e['provider_id'] for e in first['leads'][0]['events']], ['message-1'])
        second = reader.collect(first)
        self.assertEqual(second['coverage']['unreconciled_messages_count'], 100)
        self.assertEqual([e['provider_id'] for e in second['leads'][0]['events']], ['message-1'])

    def test_orphan_reconciles_only_after_identity_joins_cohort(self):
        wire = Wire(messages=[metadata('message-2', contact_id='contact-2')],
                    contents=[content('message-2')])
        reader = self.make(wire)
        first = reader.collect()
        self.assertFalse(first['coverage']['complete'])
        self.assertFalse(first['leads'][0]['events'])
        wire.contacts.append(contact('contact-2'))
        cached = reader.collect(first)
        self.assertFalse(cached['coverage']['messages_complete'])
        self.assertNotIn('contact-2', [lead['id'] for lead in cached['leads']])
        reader.clock = lambda: NOW + timedelta(seconds=COHORT_TTL + 1)
        second = reader.collect(cached)
        self.assertTrue(second['coverage']['messages_complete'])
        self.assertEqual(next(lead for lead in second['leads'] if lead['id'] == 'contact-2')['events'][0]['provider_id'], 'message-2')

    def test_large_cohort_resumes_after_deadline_and_reaches_messages(self):
        rows = [contact('c'+str(i), scoped=i < 285) for i in range(7800)]
        wire = Wire(contacts=rows, messages=[], contents=[])
        elapsed = [0.0]
        def delay(method, path, body):
            if path.endswith('contacts/search'):
                elapsed[0] += 3.1
            return None
        wire.override = delay
        reader = ApolloCampaignReadOnly(lambda: 'fake', request=wire,
            clock=lambda: NOW + timedelta(seconds=elapsed[0]),
            monotonic=lambda: elapsed[0], mcp_request=FakeMCPTransport(wire))
        first = reader.collect()
        self.assertFalse(first['coverage']['contacts_complete'])
        elapsed[0] += 901
        second = reader.collect(first)
        calls = sum(path.endswith('contacts/search') for _, path, _, _ in wire.calls)
        self.assertEqual(calls, 78)
        self.assertTrue(second['coverage']['contacts_complete'])
        self.assertTrue(second['coverage']['messages_complete'])
        self.assertEqual(len(second['leads']), 285)

    def test_cohort_scan_resumes_past_page_180_across_cycles(self):
        rows = [contact('c'+str(i), scoped=i < 285) for i in range(18101)]
        wire = Wire(contacts=rows, messages=[], contents=[])
        elapsed = [0.0]
        def advance(method, path, body):
            if path.endswith('contacts/search'):
                elapsed[0] += 2.0
            return None
        wire.override = advance
        reader = ApolloCampaignReadOnly(lambda: 'fake', request=wire, max_pages=90,
            clock=lambda: NOW + timedelta(seconds=elapsed[0]),
            monotonic=lambda: elapsed[0], mcp_request=FakeMCPTransport(wire))
        first = reader.collect()
        self.assertEqual(first['contact_scan_checkpoint']['next_page'], 91)
        self.assertFalse(first['coverage']['complete'])
        elapsed[0] += 901
        second = reader.collect(first)
        self.assertEqual(second['contact_scan_checkpoint']['next_page'], 181)
        self.assertFalse(second['coverage']['complete'])
        elapsed[0] += 901
        third = reader.collect(second)
        pages = [body['page'] for _, path, body, _ in wire.calls if path.endswith('contacts/search')]
        self.assertEqual(pages, list(range(1, 183)))
        self.assertTrue(third['coverage']['contacts_complete'])
        self.assertTrue(third['coverage']['messages_complete'])
        self.assertFalse(third['coverage']['complete'])
        self.assertIn('contacts_scan_spans_refreshes', third['coverage']['reasons'])
        self.assertEqual(len(third['leads']), 285)

    def test_finished_multicycle_scan_does_not_claim_atomic_membership_or_new_source_time(self):
        rows = [contact('c'+str(i), scoped=i < 285) for i in range(18101)]
        wire = Wire(contacts=rows, messages=[], contents=[])
        elapsed = [0.0]
        def advance(method, path, body):
            if path.endswith('contacts/search'):
                elapsed[0] += 2.0
            return None
        wire.override = advance
        clock = lambda: NOW + timedelta(seconds=elapsed[0])
        reader = ApolloCampaignReadOnly(lambda: 'fake', request=wire, max_pages=90,
            clock=clock, monotonic=lambda: elapsed[0], mcp_request=FakeMCPTransport(wire))
        first = reader.collect()
        # The first contact leaves the sequence while the total stays unchanged.
        wire.contacts[0]['contact_campaign_statuses'] = []
        elapsed[0] += 901
        second = reader.collect(first)
        elapsed[0] += 901
        finished = reader.collect(second)
        self.assertTrue(finished['coverage']['contacts_complete'])
        self.assertTrue(finished['coverage']['messages_complete'])
        self.assertFalse(finished['coverage']['complete'])
        self.assertFalse(finished['coverage']['atomic_snapshot'])
        self.assertIn('contacts_scan_spans_refreshes', finished['coverage']['reasons'])
        self.assertEqual(finished['coverage']['contacts_observed_at'], NOW.isoformat())
        self.assertGreater(finished['coverage']['contacts_age_seconds'], SNAPSHOT_TTL)
        self.assertEqual(len(finished['leads']), 285)  # stale membership cannot be ruled out
        wire.calls.clear()
        cached = reader.collect(finished)
        self.assertTrue(cached['coverage']['contacts_cached'])
        self.assertFalse(cached['coverage']['complete'])
        self.assertEqual(cached['coverage']['contacts_observed_at'], NOW.isoformat())
        self.assertFalse(any(path.endswith('contacts/search') for _, path, _, _ in wire.calls))

    def test_each_membership_observation_keeps_its_own_time_across_cycles(self):
        wire = Wire(contacts=[contact('c'+str(i)) for i in range(101)], messages=[])
        elapsed = [0.0]
        def advance(method, path, body):
            if path.endswith('contacts/search'):
                elapsed[0] += 2
            return None
        wire.override = advance
        clock = lambda: NOW + timedelta(seconds=elapsed[0])
        reader = ApolloCampaignReadOnly(lambda: 'fake', request=wire, max_pages=1,
            clock=clock, monotonic=lambda: elapsed[0], mcp_request=FakeMCPTransport(wire))
        first = reader.collect()
        elapsed[0] += 901
        second = reader.collect(first)
        times = {row['id']: row.get('membership_observed_at') for row in second['leads']}
        self.assertEqual(times['c0'], (NOW + timedelta(seconds=2)).isoformat())
        self.assertEqual(times['c100'], (NOW + timedelta(seconds=905)).isoformat())
        self.assertFalse(second['coverage']['complete'])
        cached = reader.collect(second)
        self.assertEqual({row['id']: row.get('membership_observed_at') for row in cached['leads']}, times)

    def test_provider_ceiling_holds_without_repeating_scan(self):
        wire = Wire(contacts=[contact('c'+str(i)) for i in range(100)], messages=[])
        def too_many(method, path, body):
            if path.endswith('contacts/search'):
                return 200, json.dumps({'contacts': wire.contacts,
                    'pagination': {'page': body['page'], 'per_page': 100, 'total_entries': 50001}}).encode()
            return None
        wire.override = too_many
        reader = self.make(wire)
        first = reader.collect()
        self.assertIn('apollo_contacts_provider_limit', first['coverage']['reasons'])
        self.assertFalse(first['coverage']['complete'])
        wire.calls.clear()
        second = reader.collect(first)
        self.assertEqual(wire.calls, [])
        self.assertFalse(second['coverage']['complete'])
        self.assertIn('apollo_contacts_provider_limit', second['coverage']['reasons'])

    def test_tampered_scan_checkpoint_restarts_and_is_not_authority(self):
        rows = [contact('c'+str(i), scoped=i < 3) for i in range(7800)]
        wire = Wire(contacts=rows, messages=[])
        elapsed = [0.0]
        def delay(method, path, body):
            if path.endswith('contacts/search'):
                elapsed[0] += 3.1
            return None
        wire.override = delay
        reader = ApolloCampaignReadOnly(lambda: 'fake', request=wire,
            clock=lambda: NOW + timedelta(seconds=elapsed[0]),
            monotonic=lambda: elapsed[0], mcp_request=FakeMCPTransport(wire))
        partial = reader.collect()
        partial['contact_scan_checkpoint']['source_total'] = 1
        elapsed[0] += 901
        wire.calls.clear()
        after = reader.collect(previous=partial)
        pages = [body['page'] for _, path, body, _ in wire.calls if path.endswith('/contacts/search')]
        self.assertEqual(pages[0], 1)
        self.assertFalse(after['coverage']['complete'])
        self.assertNotIn('cohort_cache', after)

    def test_resume_keeps_original_scan_age_and_partial_coverage(self):
        rows = [contact('c'+str(i), scoped=i < 3) for i in range(7800)]
        wire = Wire(contacts=rows, messages=[])
        elapsed = [0.0]
        def delay(method, path, body):
            if path.endswith('contacts/search'):
                elapsed[0] += 3.1
            return None
        wire.override = delay
        reader = ApolloCampaignReadOnly(lambda: 'fake', request=wire,
            clock=lambda: NOW + timedelta(seconds=elapsed[0]),
            monotonic=lambda: elapsed[0], mcp_request=FakeMCPTransport(wire))
        first = reader.collect()
        elapsed[0] += 901
        second = reader.collect(first)
        self.assertEqual(second['cohort_cache']['observed_at'], NOW.isoformat())
        self.assertFalse(second['coverage']['complete'])
        self.assertIn('contacts_scan_spans_refreshes', second['coverage']['reasons'])
        third = reader.collect(second)
        self.assertFalse(third['coverage']['complete'])
        self.assertIn('contacts_scan_spans_refreshes', third['coverage']['reasons'])

    def test_transient_resume_failure_preserves_valid_checkpoint(self):
        rows = [contact('c'+str(i), scoped=i < 3) for i in range(7800)]
        wire = Wire(contacts=rows, messages=[])
        elapsed = [0.0]
        def delay(method, path, body):
            if path.endswith('contacts/search'):
                elapsed[0] += 3.1
            return None
        wire.override = delay
        reader = ApolloCampaignReadOnly(lambda: 'fake', request=wire,
            clock=lambda: NOW + timedelta(seconds=elapsed[0]),
            monotonic=lambda: elapsed[0], mcp_request=FakeMCPTransport(wire))
        first = reader.collect()
        elapsed[0] += 901
        def unavailable(method, path, body):
            if path.endswith('contacts/search'):
                raise OSError('synthetic')
            return None
        wire.override = unavailable
        second = reader.collect(first)
        self.assertEqual(second['contact_scan_checkpoint'], first['contact_scan_checkpoint'])

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

    def test_cohort_cache_six_hours_then_rescan_and_scope_check(self):
        reader = self.make()
        previous = reader.collect()
        previous['cohort_cache']['observed_at'] = (NOW-timedelta(hours=5, minutes=59)).isoformat()
        self.wire.calls.clear()
        data = reader.collect(previous)
        self.assertTrue(data['coverage']['contacts_cached'])
        self.assertFalse(any(p.endswith('contacts/search') for _, p, _, _ in self.wire.calls))
        self.assertEqual(data['coverage']['contacts_observed_at'], previous['cohort_cache']['observed_at'])
        for change in ({'observed_at': (NOW-timedelta(hours=6)).isoformat()}, {'account_id': 'other'}, {'complete': False}):
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
    def test_complete_refresh_retains_unobserved_contact_and_event(self):
        old = {'id': 'old', 'email': LEAD, 'events': [{'provider': 'apollo', 'provider_id': 'old-event', 'event_type': 'email_sent'}]}
        previous = {'leads': [old], 'observed_at': SENT}
        data = {'leads': [], 'observed_at': NOW.isoformat(), 'coverage': {'complete': True}, 'metrics': {}}
        repo = Mock(); repo.get_state.return_value = previous
        provider = Mock(); provider.collect.return_value = data
        ApolloCampaignReader(repo, provider, enable_check=lambda: True, clock=lambda: NOW).refresh()
        saved = repo.put_state.call_args.args[2]
        self.assertEqual(saved['leads'][0]['events'][0]['provider_id'], 'old-event')
        self.assertTrue(saved['coverage']['retained_observation'])

    def test_complete_historical_message_survives_incomplete_refresh(self):
        old_event = {'provider': 'apollo', 'provider_id': 'event', 'event_type': 'reply_received',
                     'content_complete': True, 'text': 'Complete historical message'}
        new_event = {'provider': 'apollo', 'provider_id': 'event', 'event_type': 'scheduled',
                     'content_complete': False, 'text': None}
        repo = Mock(); repo.get_state.return_value = {'leads': [{'id': 'lead', 'email': LEAD,
                                                                  'events': [old_event]}]}
        provider = Mock(); provider.collect.return_value = {'leads': [{'id': 'lead', 'email': LEAD,
            'events': [new_event]}], 'coverage': {'complete': True}, 'metrics': {}}
        ApolloCampaignReader(repo, provider, enable_check=lambda: True, clock=lambda: NOW).refresh()
        self.assertEqual(repo.put_state.call_args.args[2]['leads'][0]['events'][0], old_event)

    def test_interrupted_checkpoint_uses_new_cohort_time(self):
        saved = {'value': {'coverage': {'contacts_observed_at': SENT}, 'leads': []}}
        repo = Mock(); repo.get_state.side_effect = lambda *_: deepcopy(saved['value'])
        repo.put_state.side_effect = lambda _k, _c, value: saved.update(value=deepcopy(value))
        cache = {'complete': True, 'source_total': 1, 'account_id': APOLLO_TEAM, 'owner_id': OWNER,
                 'external_campaign_id': APOLLO_SEQUENCE, 'observed_at': NOW.isoformat(),
                 'members': [{'id': 'new', 'membership': {'emailer_campaign_id': APOLLO_SEQUENCE}}]}
        provider = Mock()
        def interrupted(*, previous, on_cohort):
            on_cohort(cache, {})
            raise RuntimeError('interrupted')
        provider.collect.side_effect = interrupted
        reader = ApolloCampaignReader(repo, provider, enable_check=lambda: True, clock=lambda: NOW)
        reader.refresh()
        self.assertEqual(saved['value']['coverage']['contacts_observed_at'], NOW.isoformat())
        self.assertEqual(reader.snapshot()['cohort_observation']['count'], 1)
    def setUp(self):
        self.now = NOW
        self.repo = Mock()
        self.repo.get_state.return_value = None
        self.provider = Mock()
        wire = Wire()
        self.provider.collect.return_value = ApolloCampaignReadOnly(lambda: 'fake', request=wire,
            mcp_request=FakeMCPTransport(wire), clock=lambda: NOW).collect()
        self.reader = ApolloCampaignReader(self.repo, self.provider, enable_check=lambda: True, clock=lambda: self.now)

    def test_atomic_budget_initializes_from_legacy_snapshot(self):
        import tempfile
        from pathlib import Path
        from thunder_os.db import Repository
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository(Path(tmp) / 'fixture.db')
            legacy = {'version': 1, 'scope': _scope_identity(),
                      'entries': {'contacts_search': [(NOW - timedelta(seconds=1)).isoformat()]}}
            repo.put_state(KIND, CAMPAIGN, {'request_budget': legacy})
            provider = ApolloCampaignReadOnly(lambda: 'fake', clock=lambda: NOW)
            provider.request_budget = {'version': 1, 'scope': _scope_identity(), 'entries': {}}
            provider.reserve_atomic(repo, 'contacts_search')
            state = repo.get_state('apollo_reader_budget', CAMPAIGN)
            self.assertEqual(state['budget']['entries']['contacts_search'],
                             legacy['entries']['contacts_search'] + [NOW.isoformat()])

    def test_offline_budget_roundtrip_is_idempotent_and_preserves_checkpoint(self):
        import tempfile
        from pathlib import Path
        from thunder_os.apollo_campaign_reader import transition_budget_offline
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository(Path(tmp) / 'fixture.db')
            old = {'version': 1, 'scope': _scope_identity(), 'entries': {'contacts_search': [NOW.isoformat()]}}
            new = {'version': 1, 'scope': _scope_identity(), 'entries': {'messages_search': [NOW.isoformat()]}}
            repo.put_state(KIND, CAMPAIGN, {'request_budget': old, 'cohort_cache': {'complete': True}})
            repo.put_state('apollo_reader_budget', CAMPAIGN, {'budget': new})
            with self.assertRaisesRegex(ExternalEmailError, 'apollo_writers_not_declared_stopped'):
                transition_budget_offline(repo, writers_stopped=False, clock=lambda: NOW)
            first = transition_budget_offline(repo, writers_stopped=True, clock=lambda: NOW)
            second = transition_budget_offline(repo, writers_stopped=True, clock=lambda: NOW)
            self.assertEqual(first['budget'], second['budget'])
            self.assertEqual(first['budget']['entries'], {'contacts_search': [NOW.isoformat()], 'messages_search': [NOW.isoformat()]})
            self.assertTrue(repo.get_state(KIND, CAMPAIGN)['cohort_cache']['complete'])
            self.assertEqual(repo.get_state(KIND, CAMPAIGN)['request_budget'], repo.get_state('apollo_reader_budget', CAMPAIGN)['budget'])

    def test_offline_transition_rolls_back_if_interrupted_before_commit(self):
        import tempfile
        from pathlib import Path
        from thunder_os.apollo_campaign_reader import transition_budget_offline
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository(Path(tmp) / 'fixture.db')
            old = {'version': 1, 'scope': _scope_identity(), 'entries': {}}
            repo.put_state(KIND, CAMPAIGN, {'request_budget': old, 'cohort_cache': {'complete': True}})
            original = repo.put_states_atomic
            def interrupted(updates):
                original(updates)
                raise RuntimeError('synthetic interruption')
            with patch.object(repo, 'put_states_atomic', side_effect=interrupted):
                with self.assertRaisesRegex(RuntimeError, 'synthetic interruption'):
                    transition_budget_offline(repo, writers_stopped=True, clock=lambda: NOW)
            self.assertEqual(repo.get_state(KIND, CAMPAIGN)['request_budget'], old)
            self.assertIsNone(repo.get_state('apollo_reader_budget', CAMPAIGN))

    def test_offline_transition_retry_after_commit_is_idempotent(self):
        import tempfile
        from pathlib import Path
        from thunder_os.apollo_campaign_reader import transition_budget_offline
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository(Path(tmp) / 'fixture.db')
            old = {'version': 1, 'scope': _scope_identity(), 'entries': {'profile': [NOW.isoformat()]}}
            repo.put_state(KIND, CAMPAIGN, {'request_budget': old, 'cohort_cache': {'complete': True}})
            original = repo.get_state
            def interrupted(kind, key):
                if kind == KIND and getattr(repo._local, 'connection', None) is None:
                    raise OSError('synthetic readback interruption')
                return original(kind, key)
            with patch.object(repo, 'get_state', side_effect=interrupted):
                with self.assertRaisesRegex(OSError, 'synthetic readback interruption'):
                    transition_budget_offline(repo, writers_stopped=True, clock=lambda: NOW)
            result = transition_budget_offline(repo, writers_stopped=True, clock=lambda: NOW)
            self.assertEqual(result['budget'], old)

    def test_offline_transition_commit_succeeds_then_reports_error(self):
        import tempfile
        from pathlib import Path
        from contextlib import contextmanager
        from thunder_os.apollo_campaign_reader import transition_budget_offline
        class CommitReportedUnknown(Repository):
            @contextmanager
            def transaction(self):
                nested = getattr(self._local, 'connection', None) is not None
                with super().transaction() as con:
                    yield con
                if not nested:
                    raise OSError('synthetic error after commit')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'fixture.db'
            repo = Repository(path)
            old = {'version': 1, 'scope': _scope_identity(), 'entries': {'profile': [NOW.isoformat()]}}
            repo.put_state(KIND, CAMPAIGN, {'request_budget': old})
            uncertain = CommitReportedUnknown(path)
            with self.assertRaisesRegex(OSError, 'synthetic error after commit'):
                transition_budget_offline(uncertain, writers_stopped=True, clock=lambda: NOW)
            retry = transition_budget_offline(repo, writers_stopped=True, clock=lambda: NOW)
            self.assertEqual(retry['budget'], old)
            self.assertIsNotNone(repo.get_state('apollo_budget_transition_backup', retry['backup_id']))

    def test_offline_transition_preserves_common_base_without_double_count(self):
        import tempfile
        from pathlib import Path
        from thunder_os.apollo_campaign_reader import transition_budget_offline
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository(Path(tmp) / 'fixture.db')
            old = {'version': 1, 'scope': _scope_identity(), 'entries': {'profile': [NOW.isoformat()]}}
            repo.put_state(KIND, CAMPAIGN, {'request_budget': old})
            transition_budget_offline(repo, writers_stopped=True, clock=lambda: NOW)
            provider = ApolloCampaignReadOnly(lambda: 'fake', clock=lambda: NOW)
            provider.reserve_atomic(repo, 'profile')
            result = transition_budget_offline(repo, writers_stopped=True, clock=lambda: NOW)
            self.assertEqual(result['budget']['entries']['profile'], [NOW.isoformat(), NOW.isoformat()])

    def test_offline_transition_expiry_ambiguity_and_legacy_consumer(self):
        import tempfile
        from pathlib import Path
        from thunder_os.apollo_campaign_reader import transition_budget_offline
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository(Path(tmp) / 'fixture.db')
            old = {'version': 1, 'scope': _scope_identity(), 'entries': {'profile': [(NOW-timedelta(days=1)).isoformat(), NOW.isoformat()]}}
            new = {'version': 1, 'scope': _scope_identity(), 'entries': {'profile': [NOW.isoformat()]}}
            repo.put_state(KIND, CAMPAIGN, {'request_budget': old, 'cohort_cache': {'complete': True, 'source_total': 9}})
            repo.put_state('apollo_reader_budget', CAMPAIGN, {'budget': new})
            result = transition_budget_offline(repo, writers_stopped=True, clock=lambda: NOW)
            self.assertEqual(result['budget']['entries']['profile'], [NOW.isoformat(), NOW.isoformat()])
            legacy = ApolloCampaignReadOnly(lambda: 'fake', clock=lambda: NOW)
            legacy._load_budget(repo.get_state(KIND, CAMPAIGN))
            self.assertEqual(legacy.request_budget, result['budget'])
            backup = repo.get_state('apollo_budget_transition_backup', result['backup_id'])
            self.assertEqual(backup['originals'], {'legacy': old, 'atomic': new})
            self.assertEqual(repo.get_state(KIND, CAMPAIGN)['cohort_cache'], {'complete': True, 'source_total': 9})
            self.assertEqual(transition_budget_offline(repo, writers_stopped=True, clock=lambda: NOW)['budget'], result['budget'])
            huge = {'version': 1, 'scope': _scope_identity(), 'entries': {'profile': [NOW.isoformat()]*901}}
            repo.put_state(KIND, CAMPAIGN, {'request_budget': huge})
            repo.put_state('apollo_reader_budget', CAMPAIGN, {'budget': huge})
            with self.assertRaisesRegex(ExternalEmailError, 'apollo_transition_budget_ambiguous'):
                transition_budget_offline(repo, writers_stopped=True, clock=lambda: NOW)
            self.assertEqual(repo.get_state(KIND, CAMPAIGN)['request_budget'], huge)

    def test_offline_transition_rejects_corrupted_backup_on_repeat(self):
        import tempfile
        from pathlib import Path
        from thunder_os.apollo_campaign_reader import transition_budget_offline
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository(Path(tmp) / 'fixture.db')
            old = {'version': 1, 'scope': _scope_identity(), 'entries': {}}
            repo.put_state(KIND, CAMPAIGN, {'request_budget': old})
            result = transition_budget_offline(repo, writers_stopped=True, clock=lambda: NOW)
            repo.put_state('apollo_budget_transition_backup', result['backup_id'], {'originals': {'corrupt': True}})
            with self.assertRaisesRegex(ExternalEmailError, 'apollo_transition_backup_conflict'):
                transition_budget_offline(repo, writers_stopped=True, clock=lambda: NOW)

    def test_offline_transition_rejects_lost_or_corrupt_backup_after_reservation(self):
        import tempfile
        from pathlib import Path
        from thunder_os.apollo_campaign_reader import transition_budget_offline
        for damage in ('missing', 'corrupt'):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as tmp:
                repo = Repository(Path(tmp) / 'fixture.db')
                old = {'version': 1, 'scope': _scope_identity(), 'entries': {}}
                repo.put_state(KIND, CAMPAIGN, {'request_budget': old})
                first = transition_budget_offline(repo, writers_stopped=True, clock=lambda: NOW)
                provider = ApolloCampaignReadOnly(lambda: 'fake', clock=lambda: NOW)
                provider.reserve_atomic(repo, 'profile')
                with repo.transaction() as con:
                    if damage == 'missing':
                        con.execute('DELETE FROM thunder_state WHERE kind=? AND entity_id=?',
                                    ('apollo_budget_transition_backup', first['backup_id']))
                    else:
                        repo.put_state('apollo_budget_transition_backup', first['backup_id'], {'originals': {'corrupt': True}})
                before = repo.get_state(KIND, CAMPAIGN)
                with self.assertRaises(ExternalEmailError):
                    transition_budget_offline(repo, writers_stopped=True, clock=lambda: NOW)
                self.assertEqual(repo.get_state(KIND, CAMPAIGN), before)

    def test_transition_after_real_refresh_preserves_prior_backup_reference(self):
        import tempfile
        from pathlib import Path
        from thunder_os.apollo_campaign_reader import transition_budget_offline
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository(Path(tmp) / 'fixture.db')
            old = {'version': 1, 'scope': _scope_identity(), 'entries': {}}
            repo.put_state(KIND, CAMPAIGN, {'request_budget': old})
            first = transition_budget_offline(repo, writers_stopped=True, clock=lambda: NOW)
            wire = Wire(contacts=[], messages=[])
            provider = ApolloCampaignReadOnly(lambda: 'fake', request=wire,
                mcp_request=FakeMCPTransport(wire), clock=lambda: NOW)
            _OperationalApolloCampaignReader(repo, provider, enable_check=lambda: True, clock=lambda: NOW).refresh()
            self.assertEqual(repo.get_state(KIND, CAMPAIGN).get('apollo_transition_backup_id'), first['backup_id'])
            transition_budget_offline(repo, writers_stopped=True, clock=lambda: NOW)

    def test_offline_transition_rejects_missing_or_mismatched_backup_reference(self):
        import tempfile
        from pathlib import Path
        from thunder_os.apollo_campaign_reader import transition_budget_offline
        for field in ('missing_snapshot', 'mismatched_atomic'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                repo = Repository(Path(tmp) / 'fixture.db')
                old = {'version': 1, 'scope': _scope_identity(), 'entries': {}}
                repo.put_state(KIND, CAMPAIGN, {'request_budget': old})
                transition_budget_offline(repo, writers_stopped=True, clock=lambda: NOW)
                if field == 'missing_snapshot':
                    row = repo.get_state(KIND, CAMPAIGN)
                    row.pop('apollo_transition_backup_id')
                    repo.put_state(KIND, CAMPAIGN, row)
                else:
                    row = repo.get_state('apollo_reader_budget', CAMPAIGN)
                    row['backup_id'] = 'wrong'
                    repo.put_state('apollo_reader_budget', CAMPAIGN, row)
                with self.assertRaisesRegex(ExternalEmailError, 'apollo_transition_backup_conflict'):
                    transition_budget_offline(repo, writers_stopped=True, clock=lambda: NOW)

    def test_atomic_budget_rejects_invalid_legacy_value(self):
        import tempfile
        from pathlib import Path
        from thunder_os.db import Repository
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository(Path(tmp) / 'fixture.db')
            repo.put_state(KIND, CAMPAIGN, {'request_budget': {}})
            provider = ApolloCampaignReadOnly(lambda: 'fake', clock=lambda: NOW)
            with self.assertRaisesRegex(ExternalEmailError, 'apollo_budget_cache_invalid'):
                provider.reserve_atomic(repo, 'contacts_search')
            self.assertIsNone(repo.get_state('apollo_reader_budget', CAMPAIGN))

    def test_restart_uses_atomic_budget_over_invalid_legacy_snapshot(self):
        import tempfile
        from pathlib import Path
        from thunder_os.db import Repository
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository(Path(tmp) / 'fixture.db')
            budget = {'version': 1, 'scope': _scope_identity(), 'entries': {}}
            repo.put_state('apollo_reader_budget', CAMPAIGN, {'budget': budget})
            repo.put_state(KIND, CAMPAIGN, {'request_budget': {}})
            wire = Wire(messages=[])
            provider = ApolloCampaignReadOnly(lambda: 'fake', request=wire,
                mcp_request=FakeMCPTransport(wire), clock=lambda: NOW)
            provider._budget_repo = repo
            data = provider.collect(previous=repo.get_state(KIND, CAMPAIGN))
            self.assertNotIn('apollo_budget_cache_invalid', data['coverage']['reasons'])
            self.assertTrue(data['coverage']['contacts_complete'])

    def test_atomic_budget_failure_stops_before_provider_call(self):
        from contextlib import contextmanager
        @contextmanager
        def broken():
            raise OSError('synthetic storage failure')
            yield
        repo = Mock()
        repo.transaction.side_effect = broken
        transport = Mock()
        provider = ApolloCampaignReadOnly(lambda: 'fake', request=transport, clock=lambda: NOW)
        provider._budget_repo = repo
        provider.ends = provider.monotonic() + 10
        provider.calls = 0
        provider.rate_limited = False
        provider.endpoint_budget_exhausted = False
        provider.request_budget = {'version': 1, 'scope': _scope_identity(), 'entries': {}}
        with self.assertRaises(OSError):
            provider._call('GET', '/api/v1/users/api_profile')
        transport.assert_not_called()

    def test_real_repository_rejects_unintegrated_provider(self):
        import tempfile
        from pathlib import Path
        from thunder_os.db import Repository
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository(Path(tmp) / 'fixture.db')
            with self.assertRaisesRegex(ExternalEmailError, 'apollo_transactional_adapter_required'):
                ApolloCampaignReader(repo, Mock(), enable_check=lambda: True)

    def test_nontransactional_repository_rejected_by_default(self):
        with self.assertRaisesRegex(ExternalEmailError, 'apollo_transactional_repository_required'):
            _OperationalApolloCampaignReader(Mock(), Mock(), enable_check=lambda: True)

    def test_direct_default_transport_requires_durable_budget(self):
        provider = ApolloCampaignReadOnly(lambda: 'synthetic')
        provider.ends = provider.monotonic() + 10
        provider.calls = 0
        provider.rate_limited = False
        provider.endpoint_budget_exhausted = False
        provider.request_budget = {'version': 1, 'scope': _scope_identity(), 'entries': {}}
        with self.assertRaisesRegex(ExternalEmailError, 'apollo_transactional_repository_required'):
            provider._call('GET', '/api/v1/users/api_profile')

    def test_direct_default_mcp_requires_durable_budget(self):
        provider = ApolloCampaignReadOnly(lambda: 'synthetic')
        with self.assertRaisesRegex(ExternalEmailError, 'apollo_transactional_repository_required'):
            provider._mcp_call({}, 1)

    def test_valid_mcp_call_reserves_before_fake_transport(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository(Path(tmp) / 'fixture.db')
            wire = Wire()
            fake = FakeMCPTransport(wire)
            observed, before_mcp = [], []
            def transport(method, path, body, headers, timeout):
                if body['method'] == 'tools/call':
                    budget = repo.get_state('apollo_reader_budget', CAMPAIGN)['budget']
                    observed.append(len(budget['entries'].get('messages_search', [])))
                return fake(method, path, body, headers, timeout)
            provider = ApolloCampaignReadOnly(lambda: 'fake', request=wire, mcp_request=transport, clock=lambda: NOW)
            original_mcp = provider._mcp_call
            def mcp_call(message, expected_id):
                if message['method'] == 'tools/call':
                    row = repo.get_state('apollo_reader_budget', CAMPAIGN)
                    before_mcp.append(len(row['budget']['entries'].get('messages_search', [])) if row else 0)
                return original_mcp(message, expected_id)
            provider._mcp_call = mcp_call
            reader = _OperationalApolloCampaignReader(repo, provider, enable_check=lambda: True, clock=lambda: NOW)
            reader.refresh()
            self.assertTrue(observed)
            self.assertEqual(observed, [count + 1 for count in before_mcp])

    def test_legacy_rollback_cannot_see_new_atomic_reservation(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository(Path(tmp) / 'fixture.db')
            legacy = {'version': 1, 'scope': _scope_identity(), 'entries': {}}
            repo.put_state(KIND, CAMPAIGN, {'request_budget': legacy})
            provider = ApolloCampaignReadOnly(lambda: 'fake', clock=lambda: NOW)
            provider.request_budget = deepcopy(legacy)
            provider.reserve_atomic(repo, 'contacts_search')
            old_view = repo.get_state(KIND, CAMPAIGN)['request_budget']
            current = repo.get_state('apollo_reader_budget', CAMPAIGN)['budget']
            self.assertNotEqual(old_view, current)
            self.assertEqual(current['entries']['contacts_search'], [NOW.isoformat()])

    def test_atomic_commit_failure_rolls_back_before_request(self):
        import tempfile
        from pathlib import Path
        from contextlib import contextmanager
        class CommitFails(Repository):
            @contextmanager
            def transaction(self):
                with super().transaction() as con:
                    yield con
                    raise OSError('synthetic commit failure')
        with tempfile.TemporaryDirectory() as tmp:
            repo = CommitFails(Path(tmp) / 'fixture.db')
            transport = Mock()
            provider = ApolloCampaignReadOnly(lambda: 'fake', request=transport, clock=lambda: NOW)
            provider._budget_repo = repo
            provider.ends = provider.monotonic() + 10
            provider.calls = 0
            provider.rate_limited = False
            provider.endpoint_budget_exhausted = False
            with self.assertRaisesRegex(OSError, 'synthetic commit failure'):
                provider._call('GET', '/api/v1/users/api_profile')
            transport.assert_not_called()
            self.assertIsNone(repo.get_state('apollo_reader_budget', CAMPAIGN))

    def test_unhandled_failure_after_checkpoint_does_not_erase_cohort(self):
        saved = {'value': {'last_success_at': '2026-10-22T15:00:00+00:00',
                           'coverage': {'contents_complete': True, 'sent_messages_complete': True}}}
        self.repo.get_state.side_effect = lambda *_: deepcopy(saved.get('value'))
        self.repo.put_state.side_effect = lambda _kind, _campaign, value: saved.update(value=deepcopy(value))
        cache = {'complete': True, 'source_total': 7800, 'contacts': []}
        budget = {'entries': {'contacts_search': ['2026-10-23T15:00:00+00:00']}}
        def interrupted(*, previous, on_cohort):
            on_cohort(cache, budget)
            raise RuntimeError('synthetic failure after checkpoint')
        self.provider.collect.side_effect = interrupted
        self.reader.refresh()
        self.assertEqual(saved['value']['cohort_cache'], cache)
        self.assertEqual(saved['value']['request_budget'], budget)
        self.assertEqual(saved['value']['status'], 'error')
        self.assertFalse(saved['value']['coverage']['complete'])
        self.assertFalse(saved['value']['coverage']['messages_complete'])
        self.assertFalse(saved['value']['coverage']['contents_complete'])
        self.assertFalse(saved['value']['coverage']['sent_messages_complete'])
        self.assertEqual(saved['value']['last_success_at'], '2026-10-22T15:00:00+00:00')

    def test_cohort_is_persisted_before_message_phase_failure(self):
        wire = Wire(messages=[])
        provider = ApolloCampaignReadOnly(lambda: 'fake', request=wire,
            mcp_request=FakeMCPTransport(wire), clock=lambda: self.now)
        reader = ApolloCampaignReader(self.repo, provider, enable_check=lambda: True, clock=lambda: self.now)
        observed = []
        def fail_after_persist(result):
            observed.append(deepcopy(self.repo.put_state.call_args.args[2])
                            if self.repo.put_state.call_args else None)
            raise ExternalEmailError('apollo_read_unavailable')
        provider._messages = fail_after_persist
        reader.refresh()
        self.assertEqual(len(observed), 1)
        self.assertIsNotNone(observed[0])
        self.assertTrue(observed[0]['cohort_cache']['complete'])
        self.assertFalse(observed[0]['coverage']['complete'])
        self.assertEqual(len(observed[0]['request_budget']['entries']['contacts_search']), 1)
        stored = self.repo.put_state.call_args.args[2]
        self.assertTrue(stored['cohort_cache']['complete'])
        self.assertFalse(stored['coverage']['complete'])

    def test_two_refreshes_resume_cohort_and_reach_messages(self):
        rows = [contact('c'+str(i), scoped=i < 285) for i in range(7800)]
        wire = Wire(contacts=rows, messages=[], contents=[])
        elapsed = [0.0]
        wire.override = lambda method, path, body: (
            elapsed.__setitem__(0, elapsed[0] + 3.1) if path.endswith('contacts/search') else None)
        clock = lambda: NOW + timedelta(seconds=elapsed[0])
        provider = ApolloCampaignReadOnly(lambda: 'fake', request=wire,
            clock=clock, monotonic=lambda: elapsed[0], mcp_request=FakeMCPTransport(wire))
        saved = {}
        repo = Mock()
        repo.get_state.side_effect = lambda *_: deepcopy(saved.get('value'))
        repo.put_state.side_effect = lambda _kind, _campaign, value: saved.update(value=deepcopy(value))
        reader = ApolloCampaignReader(repo, provider, enable_check=lambda: True, clock=clock)
        reader.refresh()
        self.assertFalse(saved['value']['coverage']['complete'])
        self.assertTrue(saved['value']['contact_scan_checkpoint'])
        elapsed[0] += 901
        reader.refresh()
        self.assertEqual(sum(path.endswith('contacts/search') for _, path, _, _ in wire.calls), 78)
        self.assertTrue(saved['value']['coverage']['contacts_complete'])
        self.assertTrue(saved['value']['coverage']['messages_complete'])
        self.assertEqual(len(saved['value']['leads']), 285)

    def test_default_composition_injects_seqd_without_loading_credentials(self):
        with patch('thunder_os.apollo_campaign_reader.private_text', side_effect=AssertionError('auth access')):
            reader = ApolloCampaignReader(self.repo, enable_check=lambda: False)
            self.assertIsInstance(reader.provider.gmail, SEQDClient)
            reader.snapshot()
            reader.refresh()

    def test_stop_waits_for_inflight_refresh_and_worker_exits(self):
        entered, release = threading.Event(), threading.Event()
        def controlled_refresh():
            entered.set()
            release.wait(2)
        self.reader.refresh = controlled_refresh
        self.reader.start()
        self.assertTrue(entered.wait(1))
        result = []
        stopper = threading.Thread(target=lambda: result.append(self.reader.stop()))
        stopper.start()
        try:
            self.assertTrue(self.reader.stop_event.wait(1))
            self.assertTrue(stopper.is_alive())
        finally:
            release.set()
            stopper.join(3)
        self.assertEqual(result, [True])
        self.assertFalse(self.reader.thread.is_alive())

    def test_stop_reports_timeout_if_refresh_has_not_finished(self):
        thread = Mock()
        thread.is_alive.return_value = True
        self.reader.thread = thread
        self.assertFalse(self.reader.stop())
        thread.join.assert_called_once_with(timeout=6)
        self.assertTrue(self.reader.stop_event.is_set())

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
        self.now += timedelta(seconds=1801)
        data = self.reader.snapshot()
        self.assertEqual(data['status'], 'stale')
        self.assertTrue(data['stale'])
        self.assertFalse(data['coverage']['complete'])
        self.assertEqual(data['leads'][0]['id'], 'contact-1')

    def test_snapshot_marks_old_contacts_stale_even_with_recent_messages(self):
        self.reader.refresh()
        state = deepcopy(self.repo.put_state.call_args.args[2])
        state['coverage']['contacts_observed_at'] = (NOW - timedelta(seconds=SNAPSHOT_TTL + 1)).isoformat()
        state['observed_at'] = NOW.isoformat()
        self.repo.get_state.return_value = state
        snapshot = self.reader.snapshot()
        self.assertTrue(snapshot['stale'])
        self.assertEqual(snapshot['status'], 'stale')
        self.assertFalse(snapshot['coverage']['complete'])
        self.assertFalse(snapshot['metrics']['complete'])

    def test_snapshot_rejects_missing_or_invalid_complete_contact_timestamp(self):
        self.reader.refresh()
        original = deepcopy(self.repo.put_state.call_args.args[2])
        for bad in (None, 1, {}, 'bad-date'):
            with self.subTest(value=bad):
                state = deepcopy(original)
                state['coverage']['contacts_observed_at'] = bad
                self.repo.get_state.return_value = state
                snapshot = self.reader.snapshot()
                self.assertTrue(snapshot['stale'])
                self.assertFalse(snapshot['coverage']['complete'])
        del state['coverage']['contacts_observed_at']
        self.repo.get_state.return_value = state
        self.assertTrue(self.reader.snapshot()['stale'])

    def test_snapshot_degrades_malformed_global_timestamp_and_coverage(self):
        self.reader.refresh()
        original = deepcopy(self.repo.put_state.call_args.args[2])
        for key, bad in (('observed_at', 1), ('coverage', None), ('coverage', 'invalid')):
            with self.subTest(key=key, value=bad):
                state = deepcopy(original)
                state[key] = bad
                self.repo.get_state.return_value = state
                snapshot = self.reader.snapshot()
                self.assertNotEqual(snapshot['status'], 'ok')
                self.assertFalse(snapshot['coverage']['complete'])
                self.assertFalse(snapshot['metrics']['complete'])
        state = deepcopy(original)
        del state['coverage']
        self.repo.get_state.return_value = state
        snapshot = self.reader.snapshot()
        self.assertNotEqual(snapshot['status'], 'ok')
        self.assertFalse(snapshot['metrics']['complete'])

    def test_snapshot_degrades_old_missing_or_invalid_message_observation(self):
        self.reader.refresh()
        original = deepcopy(self.repo.put_state.call_args.args[2])
        for bad in ((NOW - timedelta(seconds=SNAPSHOT_TTL + 1)).isoformat(), None, 1, {}, 'bad-date'):
            with self.subTest(value=bad):
                state = deepcopy(original)
                state['coverage']['messages_observed_at'] = bad
                self.repo.get_state.return_value = state
                snapshot = self.reader.snapshot()
                self.assertTrue(snapshot['stale'])
                self.assertFalse(snapshot['coverage']['complete'])
        del state['coverage']['messages_observed_at']
        self.repo.get_state.return_value = state
        self.assertTrue(self.reader.snapshot()['stale'])

    def test_runtime_observation_reports_initial_and_cooldown_without_provider_call(self):
        before = self.reader.snapshot()['runtime']
        self.assertFalse(before['thread_started'])
        self.assertFalse(before['in_progress'])
        self.repo.get_state.return_value = {'next_attempt_at': (self.now + timedelta(seconds=300)).isoformat(), 'error_code': 'apollo_endpoint_budget_exhausted'}
        self.reader.refresh()
        after = self.reader.snapshot()['runtime']
        self.assertEqual(after['phase'], 'cooldown')
        self.assertEqual(after['cooldown_until'], (self.now + timedelta(seconds=300)).isoformat())
        self.provider.collect.assert_not_called()

    def test_runtime_observation_shows_inflight_without_exposing_provider_data(self):
        entered, release = threading.Event(), threading.Event()
        result = deepcopy(self.provider.collect.return_value)
        def collect(**kwargs):
            entered.set()
            release.wait(2)
            return result
        self.provider.collect.side_effect = collect
        thread = threading.Thread(target=self.reader.refresh)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            runtime = self.reader.snapshot()['runtime']
            self.assertTrue(runtime['in_progress'])
            self.assertEqual(runtime['phase'], 'collecting')
            self.assertIsNotNone(runtime['last_attempt_at'])
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(self.reader.snapshot()['runtime']['in_progress'])

    def test_runtime_observation_distinguishes_saved_cohort_from_initial_collection(self):
        entered, release = threading.Event(), threading.Event()
        result = deepcopy(self.provider.collect.return_value)
        def collect(**kwargs):
            kwargs['on_cohort']({'complete': True}, {'entries': {}})
            entered.set()
            release.wait(2)
            return result
        self.provider.collect.side_effect = collect
        thread = threading.Thread(target=self.reader.refresh)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertEqual(self.reader.snapshot()['runtime']['phase'], 'collecting_messages')
        finally:
            release.set()
            thread.join(3)

    def test_thread_exception_is_sanitized_and_visible(self):
        entered = threading.Event()
        def fail():
            entered.set()
            raise RuntimeError('SECRET https://private.invalid signed body')
        self.reader.refresh = fail
        self.reader.start()
        try:
            self.assertTrue(entered.wait(1))
            self.reader.stop()
            runtime = self.reader.snapshot()['runtime']
            self.assertTrue(runtime['thread_started'])
            self.assertEqual(runtime['last_loop_error'], 'apollo_reader_loop_error')
            self.assertNotIn('SECRET', json.dumps(runtime))
            self.assertNotIn('private.invalid', json.dumps(runtime))
        finally:
            self.reader.stop()

    def test_runtime_distinguishes_normal_wait_from_provider_cooldown(self):
        future = (self.now + timedelta(seconds=300)).isoformat()
        self.repo.get_state.return_value = {'next_attempt_at': future, 'error_code': None}
        self.reader.refresh()
        self.assertEqual(self.reader.snapshot()['runtime']['phase'], 'waiting_interval')
        self.assertIsNone(self.reader.snapshot()['runtime']['cooldown_until'])
        self.repo.get_state.return_value = {'next_attempt_at': future, 'error_code': 'apollo_endpoint_budget_exhausted'}
        self.reader.refresh()
        self.assertEqual(self.reader.snapshot()['runtime']['phase'], 'cooldown')
        self.assertEqual(self.reader.snapshot()['runtime']['cooldown_until'], future)
        self.repo.get_state.return_value = {'last_attempt_at': self.now.isoformat()}
        self.reader.refresh()
        self.assertEqual(self.reader.snapshot()['runtime']['phase'], 'waiting_interval')
        self.assertFalse(self.reader.snapshot()['runtime']['in_progress'])

    def test_runtime_exposes_sanitized_collection_failure_and_thread_liveness(self):
        self.provider.collect.side_effect = RuntimeError('SECRET signed-url')
        self.reader.refresh()
        runtime = self.reader.snapshot()['runtime']
        self.assertEqual(runtime['phase'], 'error')
        self.assertEqual(runtime['last_refresh_error'], 'apollo_refresh_error')
        self.assertFalse(runtime['thread_alive'])
        self.assertNotIn('SECRET', json.dumps(runtime))
        self.assertNotIn('signed-url', json.dumps(runtime))

    def test_runtime_cooldown_detects_rate_limit_among_coverage_reasons(self):
        future = (self.now + timedelta(seconds=300)).isoformat()
        self.repo.get_state.return_value = {'next_attempt_at': future, 'error_code': 'other',
            'coverage': {'reasons': ['other', 'apollo_rate_limited']}}
        self.reader.refresh()
        self.assertEqual(self.reader.snapshot()['runtime']['phase'], 'cooldown')

    def test_runtime_reports_partial_collection_without_leaking_error_text(self):
        data = deepcopy(self.provider.collect.return_value)
        data['coverage'] = {'complete': False, 'contacts_complete': False,
                            'messages_complete': False, 'reasons': ['apollo_read_unavailable']}
        self.provider.collect.return_value = data
        self.reader.refresh()
        runtime = self.reader.snapshot()['runtime']
        self.assertEqual(runtime['phase'], 'partial')
        self.assertEqual(runtime['last_refresh_error'], 'apollo_refresh_error')
        self.assertFalse(runtime['in_progress'])

    def test_interval_and_nonblocking_refresh_lock(self):
        self.reader.refresh()
        self.repo.get_state.return_value = self.repo.put_state.call_args.args[2]
        self.reader.refresh()
        self.assertEqual(self.provider.collect.call_count, 1)
        self.reader.lock.acquire()
        self.now += timedelta(seconds=901)
        self.reader.refresh()
        self.assertEqual(self.provider.collect.call_count, 1)
        self.reader.lock.release()

    def test_provider_failure_preserves_original_dates_and_no_secrets(self):
        self.reader.refresh()
        old = self.repo.put_state.call_args.args[2]
        self.repo.get_state.return_value = old
        self.now += timedelta(seconds=901)
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
                self.now += timedelta(seconds=901)
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
                self.now += timedelta(seconds=1801)
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


class S47Tests(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.wire = Wire()
        self.mcp = FakeMCPTransport(self.wire)
        self.provider = ApolloCampaignReadOnly(lambda: 'fake', request=self.wire,
            mcp_request=self.mcp, clock=lambda: self.now)
        network = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        network.start()
        self.addCleanup(network.stop)

    def reset_calls(self):
        self.wire.calls.clear()
        self.mcp.calls.clear()

    def test_structured_text_error_recognizes_minute_hour_day_and_persists_restart_cooldown(self):
        for window, seconds in (('minute', 60), ('hour', 3600), ('day', 86400)):
            with self.subTest(window=window):
                self.mcp.calls.clear()
                def limited(method, path, body, headers, timeout):
                    if body['method'] != 'tools/call': return None
                    payload = {'error_details': {'code': 'USAGE.RATE_LIMIT.API_RATE_LIMIT_EXCEEDED',
                                                'context': {'window': {'value': window}}}}
                    result = {'isError': False, 'content': [{'type': 'text', 'text': json.dumps(payload)}]}
                    return 200, json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': result}).encode(), {'Content-Type': 'application/json'}
                self.mcp.override = limited
                data = self.provider.collect()
                self.assertEqual(data['coverage']['reasons'], ['apollo_rate_limited'])
                self.assertEqual(data['rate_limit_window'], window)
                self.assertEqual(data['next_attempt_at'], (self.now+timedelta(seconds=seconds)).isoformat())
                self.assertEqual(len([c for c in self.mcp.calls if c[2]['method'] == 'tools/call']), 1)
                repo, transport = Mock(), Mock(side_effect=AssertionError('must not call provider'))
                data.update(last_attempt_at=self.now.isoformat(), error_code='apollo_rate_limited')
                repo.get_state.return_value = data
                restarted = ApolloCampaignReader(repo, transport, enable_check=lambda: True, clock=lambda: self.now)
                restarted.refresh()
                transport.collect.assert_not_called()
                snapshot = restarted.snapshot()
                self.assertEqual(snapshot['next_attempt_at'], data['next_attempt_at'])
                self.assertEqual(snapshot['poll_interval_seconds'], 900)
                # Direct provider users also respect the persisted typed cooldown.
                self.reset_calls()
                self.provider.collect(previous=data)
                self.assertEqual(self.wire.calls, [])
                self.assertEqual(self.mcp.calls, [])

    def test_legacy_error_text_does_not_guess_hour_and_native_retry_after_uses_verified_deadline(self):
        self.wire.override = lambda *args: (200, b'not-json USAGE.RATE_LIMIT.API_RATE_LIMIT_EXCEEDED hour')
        invalid = self.provider.collect()
        self.assertIsNone(invalid['rate_limit_window'])
        self.assertEqual(invalid['next_attempt_at'], (self.now+timedelta(seconds=900)).isoformat())
        self.assertNotIn('apollo_rate_limited', invalid['coverage']['reasons'])
        self.now = datetime(2026, 10, 26, 21, 53, 51, tzinfo=timezone.utc)
        self.wire.override = lambda *args: (429, b'{}', {'Retry-After': '1606'})
        limited = self.provider.collect()
        self.assertEqual(limited['next_attempt_at'], '2026-10-26T22:20:37+00:00')
        repo, fake = Mock(), Mock()
        limited['last_attempt_at'] = self.now.isoformat()
        repo.get_state.return_value = limited
        reader = ApolloCampaignReader(repo, fake, enable_check=lambda: True, clock=lambda: self.now)
        self.now = datetime(2026, 10, 26, 22, 20, 36, tzinfo=timezone.utc)
        reader.refresh()
        fake.collect.assert_not_called()
        self.now += timedelta(seconds=1)
        fake.collect.return_value = invalid
        reader.refresh()
        fake.collect.assert_called_once()

    def test_repeated_461_metadata_unchanged_calls_eleven_and_retains_proof_date(self):
        self.wire.messages = ([metadata('m'+str(i)) for i in range(193)]
                              + [metadata('q'+str(i), status='scheduled') for i in range(268)])
        self.wire.contents = [content('m'+str(i)) for i in range(193)]
        first = self.provider.collect()
        self.assertTrue(first['coverage']['complete'])
        first_proof = first['leads'][0]['events'][0]['source_observed_at']
        self.now += timedelta(seconds=900)
        self.reset_calls()
        second = self.provider.collect(previous=first)
        self.assertTrue(second['coverage']['complete'])
        self.assertEqual(len(self.wire.calls)+len(self.mcp.calls), 11)
        self.assertEqual(second['coverage']['cached_message_count'], 461)
        self.assertTrue(second['coverage']['contacts_cached'])
        self.assertEqual(second['coverage']['contacts_age_seconds'], 900)
        self.assertEqual(second['coverage']['contacts_cache_ttl_seconds'], 21600)
        event = second['leads'][0]['events'][0]
        self.assertEqual(event['source_observed_at'], first_proof)
        self.assertEqual(event['metadata_observed_at'], self.now.isoformat())
        self.assertTrue(event['content_cached'])
        self.assertFalse(any('/emailer_messages/search?' in p or p.endswith('get_content') for _, p, _, _ in self.wire.calls))

    def test_volatile_opens_reuse_but_reply_or_variant_change_forces_new_native_proof(self):
        first = self.provider.collect()
        self.now += timedelta(seconds=900)
        self.wire.messages[0].update(open_count=99, click_count=3, opened=True, snapshot_timestamp=self.now.isoformat())
        self.reset_calls()
        reused = self.provider.collect(previous=first)
        self.assertEqual(len(self.wire.calls), 2)
        self.assertEqual(reused['leads'][0]['events'][0]['source_observed_at'], NOW.isoformat())
        self.now += timedelta(seconds=900)
        self.wire.messages[0]['variant'] = 'changed-body-variant'
        self.reset_calls()
        changed = self.provider.collect(previous=reused)
        self.assertEqual(len([p for _, p, _, _ in self.wire.calls if '/emailer_messages/search?' in p]), 1)
        self.assertEqual(changed['leads'][0]['events'][0]['source_observed_at'], self.now.isoformat())
        self.now += timedelta(seconds=900)
        self.wire.messages[0]['replied'] = True
        self.reset_calls()
        replied = self.provider.collect(previous=changed)
        self.assertEqual(len([p for _, p, _, _ in self.wire.calls if '/emailer_messages/search?' in p]), 1)
        self.assertEqual(replied['metrics']['reply_signals'], 1)

    def test_invalid_cache_integrity_identity_and_expiry_require_readback(self):
        first = self.provider.collect()
        for damage in ('body', 'scope', 'digest', 'expiry'):
            with self.subTest(damage=damage):
                old = deepcopy(first)
                entry = old['message_cache']['message-1']
                if damage == 'body': entry['content']['body'] = 'corrupted'
                elif damage == 'scope': entry['scope']['account_id'] = 'wrong'
                elif damage == 'digest': entry['integrity'] = 'invalid'
                else: self.now = NOW+timedelta(days=1)
                self.reset_calls()
                result = self.provider.collect(previous=old)
                self.assertEqual(result['metrics']['sent_messages'], 1)
                self.assertEqual(len([p for _, p, _, _ in self.wire.calls if '/emailer_messages/search?' in p]), 1)
                self.assertEqual(next(e['text'] for e in result['leads'][0]['events'] if e['event_type'] == 'email_sent'), content()['body'])
                self.now = NOW

    def test_per_endpoint_minute_hour_day_budget_blocks_before_tool_and_survives_cache(self):
        base = self.provider.collect()
        for window, limit, age in (('minute', ENDPOINT_MINUTE_LIMIT, 0),
                                   ('hour', ENDPOINT_HOUR_LIMIT, 61), ('day', ENDPOINT_DAY_LIMIT, 3601)):
            old = deepcopy(base)
            old['request_budget']['entries']['messages_search'] = [(self.now-timedelta(seconds=age)).isoformat()]*limit
            self.reset_calls()
            result = self.provider.collect(previous=old)
            self.assertIn('apollo_endpoint_budget_exhausted', result['coverage']['reasons'])
            self.assertEqual(result['rate_limit_window'], window)
            self.assertFalse(any(c[2]['method'] == 'tools/call' for c in self.mcp.calls))
            self.assertFalse(any('/emailer_messages/search?' in p for _, p, _, _ in self.wire.calls))
            self.assertEqual(len(result['request_budget']['entries']['messages_search']), limit)
        bad = deepcopy(base)
        bad['request_budget']['scope']['account_id'] = 'other'
        self.reset_calls()
        invalid = self.provider.collect(previous=bad)
        self.assertEqual(invalid['coverage']['reasons'], ['apollo_budget_cache_invalid'])
        self.assertEqual(invalid['request_budget'], bad['request_budget'])
        self.assertEqual(self.wire.calls, [])

    def test_four_real_shaped_thread_messages_include_outgoing_to_fernanda_context_only(self):
        native = '2026-10-22T17:25:51.542+00:00'
        reported = '2026-10-22T17:25:51+00:00'
        original = gmail_message(internal_date='1792689950000')
        fernanda1 = gmail_message('abc2', outgoing=False, sender='fernanda@example.invalid',
                                 internal_date='1792690000000')
        followup = gmail_message('abc3', to='fernanda@example.invalid',
            internal_date='1792771200000', bodies={'text': 'Actual Ygor 23/10 followup'})
        fernanda2 = gmail_message('abc4', outgoing=False, sender='fernanda@example.invalid',
            bodies={'text': 'Actual Fernanda 26/10 incoming'})
        gmail = Mock(spec=['conversation'])
        gmail.conversation.return_value = thread([original, fernanda1, followup, fernanda2])
        self.provider.gmail = gmail
        self.wire.messages = [metadata(completed_at=native, replied=True)]
        self.wire.contents = [content(sent_at=reported)]
        data = self.provider.collect()
        events = data['leads'][0]['events']
        actual = [e for e in events if e['event_type'] != 'reply_signal']
        self.assertEqual(len(actual), 4)
        out = next(e for e in events if e['provider_id'] == 'abc3')
        self.assertEqual(out['event_type'], 'thread_message_observed')
        self.assertEqual(out['text'], followup['bodies']['text'])
        self.assertEqual(out['direction'], 'outgoing')
        self.assertTrue(out['context_only'])
        self.assertFalse(out['receipt_eligible'])
        self.assertFalse(out['receipt'])
        self.assertIsNone(out['human_reply'])
        self.assertIsNone(out['lead_email'])
        self.assertIsNone(out['ref_sent'])
        self.assertEqual(out['source_association'], 'thread_context')
        self.assertEqual(data['metrics']['sent_messages'], 1)
        self.assertEqual(data['metrics']['human_replies'], 0)
        self.assertEqual(data['metrics']['third_party_replies'], 2)
        self.assertEqual(len({(e['provider'], e['provider_id']) for e in events}), len(events))
        self.now += timedelta(seconds=900)
        gmail.conversation.return_value = thread([original, fernanda1, followup, fernanda2], complete=False, truncated=True)
        repeated = self.provider.collect(previous=data)
        self.assertIn('gmail_thread_partial', repeated['coverage']['reasons'])
        self.assertEqual(len([e for e in repeated['leads'][0]['events'] if e['provider_id'] == 'abc3']), 1)
        self.assertEqual(repeated['metrics']['sent_messages'], 1)

    def test_partial_one_of_two_then_error_keeps_both_originals_and_old_observation_date(self):
        self.wire.messages = [metadata('m1'), metadata('m2')]
        self.wire.contents = [content('m1'), content('m2')]
        repo = Mock()
        repo.get_state.return_value = None
        reader = ApolloCampaignReader(repo, self.provider, enable_check=lambda: True, clock=lambda: self.now)
        reader.refresh()
        old = deepcopy(repo.put_state.call_args.args[2])
        self.assertEqual(old['metrics']['sent_messages'], 2)
        self.now += timedelta(seconds=900)
        partial = deepcopy(old)
        partial['leads'][0]['events'] = partial['leads'][0]['events'][:1]
        partial['metrics']['sent_messages'] = 1
        partial['observed_at'] = self.now.isoformat()
        partial['coverage'].update(complete=False, messages_complete=False, reasons=['apollo_rate_limited'])
        partial['next_attempt_at'] = (self.now+timedelta(seconds=900)).isoformat()
        fake = Mock()
        fake.collect.return_value = partial
        reader.provider = fake
        repo.get_state.return_value = old
        reader.refresh()
        retained = deepcopy(repo.put_state.call_args.args[2])
        self.assertEqual(retained['metrics']['sent_messages'], 2)
        self.assertEqual(len(retained['leads'][0]['events']), 2)
        self.assertEqual(retained['observed_at'], old['observed_at'])
        self.assertFalse(retained['coverage']['complete'])
        self.assertEqual(retained['partial_observation']['observed_at'], self.now.isoformat())
        self.now += timedelta(seconds=900)
        repo.get_state.return_value = retained
        fake.collect.side_effect = RuntimeError('unavailable')
        reader.refresh()
        failed = repo.put_state.call_args.args[2]
        self.assertEqual(failed['metrics']['sent_messages'], 2)
        self.assertEqual(failed['observed_at'], old['observed_at'])
        self.assertEqual(failed['leads'], old['leads'])

    def test_fresh_source_partial_reply_advances_metadata_date_but_not_body_proof(self):
        repo = Mock()
        repo.get_state.return_value = None
        reader = ApolloCampaignReader(repo, self.provider, enable_check=lambda: True, clock=lambda: self.now)
        reader.refresh()
        old = deepcopy(repo.put_state.call_args.args[2])
        self.now += timedelta(seconds=1800)
        self.wire.messages[0]['replied'] = True
        gmail = Mock(spec=['conversation'])
        gmail.conversation.return_value = thread([gmail_message(bodies={'text': 'different literal'}),
                                                  gmail_message('abc2', outgoing=False)])
        self.provider.gmail = gmail
        repo.get_state.return_value = old
        reader.refresh()
        fresh = repo.put_state.call_args.args[2]
        self.assertTrue(fresh['coverage']['contacts_complete'])
        self.assertTrue(fresh['coverage']['messages_complete'])
        self.assertFalse(fresh['coverage']['complete'])
        self.assertEqual(fresh['observed_at'], self.now.isoformat())
        original = next(e for e in fresh['leads'][0]['events'] if e['event_type'] == 'email_sent')
        self.assertEqual(original['source_observed_at'], old['leads'][0]['events'][0]['source_observed_at'])
        repo.get_state.return_value = fresh
        self.assertFalse(reader.snapshot()['stale'])

    def test_daemon_wakes_sixty_seconds_without_using_ui_poll_as_source_cadence(self):
        repo = Mock()
        provider = Mock()
        reader = ApolloCampaignReader(repo, provider, enable_check=lambda: True, clock=lambda: self.now)
        reader.refresh = Mock()
        reader.stop_event = Mock()
        reader.stop_event.is_set.side_effect = [False, True]
        with patch('thunder_os.apollo_campaign_reader.threading.Thread') as factory:
            reader.start()
            factory.call_args.kwargs['target']()
        reader.refresh.assert_called_once()
        reader.stop_event.wait.assert_called_once_with(60)
        self.assertEqual(INTERVAL, 900)
        self.assertEqual(SNAPSHOT_TTL, 1800)
        self.assertEqual(COHORT_TTL, 21600)



from datetime import datetime,timezone
from pathlib import Path
import tempfile,threading,unittest
from thunder_os.db import Repository
NOW=datetime(2026,10,26,12,tzinfo=timezone.utc)
class Provider(ApolloCampaignReadOnly):
 def __init__(self,callback):
  super().__init__(credential_loader=lambda:'fake',clock=lambda:NOW)
  self.action=callback
 def collect(self,previous=None,on_cohort=None):
  self.request_budget={'version':1,'scope':_scope_identity(),'entries':{}}
  self._reserve_endpoint('contacts_search')
  return self.action(self,on_cohort)
class RepoBarrier(Repository):
 def __init__(self,repo,barrier): self.repo,self.barrier,self.once=repo,barrier,False
 def get_state(self,*args):
  value=self.repo.get_state(*args)
  if args[0]==KIND and not self.once:self.once=True;self.barrier.wait(timeout=3)
  return value
 def transaction(self):return self.repo.transaction()
 def put_state(self,*args):return self.repo.put_state(*args)
 def put_states_atomic(self,*args):return self.repo.put_states_atomic(*args)
class Cases(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.repo=Repository(Path(self.tmp.name)/'test.db')
 def test_snapshot_conflict_reports_terminal_runtime_state(self):
  from thunder_os.db import RepositoryConflict
  class ConflictingRepo(Repository):
   def put_states_atomic(self, changes):
    if any(item[0] == KIND for item in changes):
     raise RepositoryConflict('synthetic conflict')
    return super().put_states_atomic(changes)
  repo=ConflictingRepo(Path(self.tmp.name)/'conflict.db')
  def action(provider, callback):
   callback({'complete':True,'source_total':1},provider.request_budget)
  reader=ApolloCampaignReader(repo,Provider(action),enable_check=lambda:True,clock=lambda:NOW)
  reader.refresh()
  runtime=reader.snapshot()['runtime']
  self.assertFalse(runtime['in_progress'])
  self.assertEqual(runtime['phase'],'error')
  self.assertEqual(runtime['last_refresh_error'],'apollo_snapshot_conflict')
 def test_reservations_after_checkpoint(self):
  def action(p,callback):
   callback({'complete':True,'source_total':1},p.request_budget.copy())
   p._reserve_endpoint('messages_search')
   raise RuntimeError('synthetic')
  p=Provider(action)
  ApolloCampaignReader(self.repo,p,enable_check=lambda:True,clock=lambda:NOW).refresh()
  state=self.repo.get_state(KIND,CAMPAIGN)
  self.assertEqual(self.repo.get_state('apollo_reader_budget',CAMPAIGN)['budget']['entries'].get('messages_search'),[NOW.isoformat()])
 def test_two_readers_both_read_before_first_checkpoint(self):
  barrier=threading.Barrier(2); a_written=threading.Event();b_done=threading.Event();errors=[]
  def first(p,callback):
   callback({'complete':True,'source_total':1},p.request_budget);a_written.set()
   if not b_done.wait(3):raise AssertionError('second reader timeout')
   raise RuntimeError('first')
  def second(p,callback):
   if not a_written.wait(3):raise AssertionError('first reader timeout')
   callback({'complete':True,'source_total':2},p.request_budget);raise RuntimeError('second')
  def run(provider,done=None):
   try:ApolloCampaignReader(RepoBarrier(self.repo,barrier),Provider(provider),enable_check=lambda:True,clock=lambda:NOW).refresh()
   except Exception as e:errors.append(repr(e))
   finally:
    if done:done.set()
  a=threading.Thread(target=run,args=(first,));b=threading.Thread(target=run,args=(second,b_done));a.start();b.start();a.join(5);b.join(5)
  self.assertFalse(a.is_alive() or b.is_alive());self.assertEqual(errors,[])
  self.assertEqual(self.repo.get_state(KIND,CAMPAIGN)['cohort_cache']['source_total'],1)

 def test_two_processes_reserve_without_losing_same_timestamp(self):
  import multiprocessing
  ctx=multiprocessing.get_context('fork');start=ctx.Event()
  def reserve(path):
   repo=Repository(path)
   p=ApolloCampaignReadOnly(lambda:'fake',clock=lambda:NOW)
   p.request_budget={'version':1,'scope':_scope_identity(),'entries':{}}
   if not start.wait(3):raise RuntimeError('barrier timeout')
   p.reserve_atomic(repo,'contacts_search')
  procs=[ctx.Process(target=reserve,args=(self.repo.path,)) for _ in range(2)]
  for p in procs:p.start()
  start.set()
  for p in procs:p.join(5)
  self.assertEqual([p.exitcode for p in procs],[0,0])
  stamps=self.repo.get_state('apollo_reader_budget',CAMPAIGN)['budget']['entries']['contacts_search']
  self.assertEqual(stamps,[NOW.isoformat(),NOW.isoformat()])

 def test_two_real_collectors_share_budget_and_reject_stale_snapshot(self):
  from thunder_os.db import RepositoryConflict
  barrier=threading.Barrier(2); errors=[]; conflicts=[]; wires=[Wire(contacts=[],messages=[]),Wire(contacts=[],messages=[])]
  class TrackingRepo(RepoBarrier):
   def put_states_atomic(self,*args):
    try:return super().put_states_atomic(*args)
    except RepositoryConflict:
     conflicts.append(True)
     raise
  def run(wire):
   try:
    provider=ApolloCampaignReadOnly(lambda:'fake',request=wire,mcp_request=FakeMCPTransport(wire),clock=lambda:NOW)
    ApolloCampaignReader(TrackingRepo(self.repo,barrier),provider,enable_check=lambda:True,clock=lambda:NOW).refresh()
   except Exception as e:errors.append(repr(e))
  threads=[threading.Thread(target=run,args=(wire,)) for wire in wires]
  for t in threads:t.start()
  for t in threads:t.join(5)
  self.assertFalse(any(t.is_alive() for t in threads));self.assertEqual(errors,[])
  budget=self.repo.get_state('apollo_reader_budget',CAMPAIGN)['budget']['entries']
  self.assertEqual(len(budget['profile']),2)
  self.assertEqual(len(budget['contacts_search']),2)
  self.assertIsNotNone(self.repo.get_state(KIND,CAMPAIGN))
  self.assertTrue(conflicts, 'one stale snapshot write must be rejected')


if __name__ == '__main__':
    unittest.main()
