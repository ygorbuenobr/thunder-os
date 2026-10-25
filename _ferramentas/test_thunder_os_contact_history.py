"""R3 delegated checklist: isolated HTTP fakes; no SQL, credentials or network.

Binding sources: PRD R3 and the delegated contact-history contract (2026-10-25).
S1 identity: fresh lookup + binding hash, never names (identity_changed,
canonical_profile, same_name, portal_mismatch).
S2 CRM: exact contact associations, no cadence membership/global task snapshot,
all pages and last outbound (no_cadence_contact, last_outbound, partial_pages).
S3 Gmail: verified aliases + persisted hash + exact known thread only, refreshed
reply/new outbound, discovery always partial (known_thread_reply_new_outbound).
S4 failures: 429/down/invalid pages are visible, independent sources survive;
plain originals are escaped by the later UI, never summarized (plaintext).
Landing: observe the existing batch response without extra requests; callers
own authentication/UI escaping. No caching, state writes, retries or SQL here.
Swept: validation S1/S3; dependency failure S4; ordering/dedup S2/S3; lifecycle
request-local only; authorization/routes/UI and independent review owned by main.
Each test_* below is an executable proof; no separate competing plan artifact.
"""
from copy import deepcopy
from datetime import datetime, timezone
from html import escape
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from test_thunder_os_new_journey_hubspot import EMAIL, PROFILE, FakeHTTP
from thunder_os.cadence_readonly import ACTIVITY_PROPERTIES, OWNER_ID, CadenceReadonlyConsole
from thunder_os.hubspot_executor import HubSpotHTTP
from thunder_os.hubspot_read_budget import HubSpotReadBudget
from thunder_os.models import payload_hash
from thunder_os.new_journey_hubspot import HubSpotEligibilityRead
from thunder_os.new_journey_qualification import candidate
from thunder_os.seqd_client import MAILBOX, SEQDClient

NOW = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)


def email(ident, text, *, direction='EMAIL', when='2026-10-25T10:00:00Z'):
    return {'id': ident, 'properties': {'hs_timestamp': when, 'hs_email_subject': 'Original subject',
        'hs_email_text': text, 'hs_email_direction': direction, 'hs_email_status': 'SENT'}}


class ActivitiesHTTP:
    def __init__(self):
        self.calls, self.fail, self.pages = [], {}, None
        self.portal = '51412240'
        self.emails = [email('301', 'First question', when='2026-10-24T10:00:00Z'),
                       email('302', 'Human reply', direction='INCOMING_EMAIL'),
                       email('303', 'Last seller outbound', when='2026-10-25T11:00:00Z')]

    def __call__(self, method, path, body, headers, timeout):
        self.calls.append((method, path, deepcopy(body)))
        assert 0 < timeout <= 8
        base = urlsplit(path).path
        if base in self.fail:
            return self.fail[base], b'{"error":"private provider payload"}'
        if base == '/account-info/v3/details':
            result = {'portalId': self.portal}
        elif base == '/crm/v3/owners/' + OWNER_ID:
            result = {'id': OWNER_ID, 'archived': False}
        elif base.startswith('/crm/v3/properties/'):
            result = {'results': [{'name': name} for name in ACTIVITY_PROPERTIES[base.rsplit('/', 1)[-1]]]}
        elif base.startswith('/crm/v4/objects/contacts/123/associations/'):
            if base.endswith('/emails'):
                cursor = parse_qs(urlsplit(path).query).get('after', [''])[0]
                result = self.pages[cursor] if self.pages else {
                    'results': [{'toObjectId': row['id']} for row in self.emails]}
            else:
                result = {'results': []}
        elif base == '/crm/v3/objects/emails/batch/read':
            ids = {row['id'] for row in body['inputs']}
            result = {'status': 'COMPLETE', 'results': [row for row in self.emails if row['id'] in ids]}
        else:
            raise AssertionError('unexpected history endpoint: ' + base)
        return 200, json.dumps(result).encode()


def message(ident, text, *, outgoing=False, alias=EMAIL, when='2026-10-25T10:00:00Z', thread='abc123'):
    return {'id': ident, 'thread_id': thread, 'from': MAILBOX if outgoing else alias,
        'to': alias if outgoing else MAILBOX, 'subject': 'Original Gmail',
        'internal_date': str(int(datetime.fromisoformat(when.replace('Z', '+00:00')).timestamp() * 1000)),
        'labelIds': ['SENT'] if outgoing else ['INBOX'], 'bodies': {'text': text, 'html': None},
        'complete': True, 'coverage': {'reasons': []}}


def thread_response(messages, ident='abc123'):
    return {'mailbox': MAILBOX, 'status': 'ok', 'thread': {'id': ident, 'messages': messages,
        'context_only': True, 'receipt_eligible': False, 'complete': True, 'truncated': False,
        'coverage': {'scope': 'thread', 'reasons': []}}}


def persisted(ident='abc123', alias=EMAIL):
    messages = [{'id': 'aa1', 'thread_id': ident, 'sender': alias, 'outgoing': False,
        'received_at': '2026-10-24T10:00:00Z', 'subject': 'Known original',
        'content': 'Persisted original', 'body_complete': True}]
    return {'id': payload_hash({'mailbox': MAILBOX, 'thread': ident}), 'mailbox': MAILBOX,
        'messages': messages, 'source_hash': payload_hash(messages), 'complete': True}


class Repo:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def list_state(self, kind):
        self.calls.append(kind)
        assert kind == 'gmail_thread'
        return deepcopy(self.rows)


class ContactHistoryTests(unittest.TestCase):
    def setUp(self):
        self.time = [1.0]
        def sleep(seconds):
            self.time[0] += seconds
        self.budget = HubSpotReadBudget(monotonic=lambda: self.time[0], sleep=sleep)
        guard = patch('thunder_os.hubspot_read_budget.SHARED_READ_BUDGET', self.budget)
        guard.start()
        self.addCleanup(guard.stop)
        for target in ('socket.socket.connect', 'socket.create_connection', 'socket.getaddrinfo',
                       'sqlite3.connect'):
            guard = patch(target, side_effect=AssertionError('network/SQL forbidden'))
            mocked = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(mocked.assert_not_called)
        self.http, self.activity = FakeHTTP(), ActivitiesHTTP()
        self.hubspot = HubSpotHTTP(self.http.loader, request=self.http, read_budget=self.budget)
        self.context = candidate(HubSpotEligibilityRead(self.hubspot).lookup(EMAIL, profile=PROFILE))
        self.assertIsNotNone(self.context)
        self.http.calls.clear()
        self.cadence = CadenceReadonlyConsole(lambda: 'synthetic-cadence', request=self.activity,
            monotonic=lambda: self.time[0], sleep=sleep, read_budget=self.budget)
        self.row = {'email': EMAIL, 'linkedin_url': PROFILE, 'company': 'Wrong CSV company name'}
        self.repo, self.gmail_calls = Repo([persisted()]), []
        self.gmail_response = thread_response([message('aa1', 'Original reply')])

        def request(method, path, body, headers, timeout):
            self.gmail_calls.append((method, path, body, timeout))
            self.assertLessEqual(timeout, 4)
            self.assertEqual((method, body), ('GET', None))
            return 200, json.dumps(self.gmail_response).encode()
        self.seqd = SEQDClient(lambda: 'x' * 48, request=request)

    def read(self, *, gmail=False, **kwargs):
        from thunder_os.contact_history import ContactHistoryReadonly
        return ContactHistoryReadonly(self.hubspot, self.cadence,
            seqd=self.seqd if gmail else None, repo=self.repo if gmail else None,
            clock=lambda: NOW).read(kwargs.get('row', self.row), kwargs.get('context', self.context))

    def source(self, result, provider):
        return next(row for row in result['sources'] if row['provider'] == provider)

    def test_identity_changed_blocks_all_history_before_activity_and_mail_reads(self):
        self.http.companies['500']['properties']['domain'] = 'different.invalid'
        result = self.read(gmail=True)
        self.assertEqual(result['events'], [])
        self.assertFalse(result['complete'])
        self.assertIn('identity_changed', self.source(result, 'hubspot_identity')['reason'])
        self.assertEqual((self.activity.calls, self.gmail_calls, self.repo.calls), ([], [], []))

    def test_fresh_identity_every_read_no_cached_authority(self):
        from thunder_os.contact_history import ContactHistoryReadonly
        reader = ContactHistoryReadonly(self.hubspot, self.cadence, clock=lambda: NOW)
        self.assertTrue(reader.read(self.row, self.context)['events'])
        self.http.contacts['123']['properties']['hs_additional_emails'] = 'changed@mail.invalid'
        self.assertFalse(reader.read(self.row, self.context)['events'])
        self.assertEqual(sum(path == '/account-info/v3/details' for _, path, *_ in self.http.calls), 2)

    def test_canonical_profile_alias_passed_with_exact_contact_email(self):
        self.row['linkedin_url'] = ''
        with patch.object(HubSpotEligibilityRead, 'lookup', autospec=True,
                          wraps=None) as lookup:
            lookup.return_value = HubSpotEligibilityRead(self.hubspot)._empty()
            self.read()
            self.assertEqual(lookup.call_args.kwargs,
                {'contact_id': '123', 'profile': PROFILE})
            self.assertEqual(lookup.call_args.args[1], EMAIL)

    def test_same_name_never_resolves_contact_or_company(self):
        result = self.read(row={'company': 'Synthetic B2B', 'name': 'Synthetic person'}, context={})
        self.assertEqual(result['events'], [])
        self.assertIsNone(result['contact_id'])
        self.assertFalse(self.http.calls)

    def test_last_outbound_and_original_incoming_are_returned_in_order(self):
        result = self.read()
        self.assertEqual([row['direction'] for row in result['events']],
                         ['outgoing', 'incoming', 'outgoing'])
        self.assertEqual(result['events'][-1]['text'], 'Last seller outbound')
        self.assertEqual(result['contact_id'], '123')
        self.assertEqual(result['company_id'], '500')
        self.assertEqual(result['observed_at'], '2026-10-25T12:00:00Z')
        self.assertTrue(result['read_only'])
        self.assertTrue(self.source(result, 'hubspot')['complete'])

    def test_no_cadence_contact_uses_activities_only_and_verifies_session_identity(self):
        with patch.object(self.cadence, 'snapshot', side_effect=AssertionError('global snapshot forbidden')):
            result = self.read()
        self.assertEqual(len(result['events']), 3)
        paths = [path for _, path, _ in self.activity.calls]
        self.assertEqual(paths[:2], ['/account-info/v3/details', '/crm/v3/owners/' + OWNER_ID])
        self.assertFalse(any('tasks' in path or '/search' in path for path in paths))

    def test_partial_pages_retain_observed_emails_without_complete_claim(self):
        self.cadence.adapter.max_pages = 1
        self.activity.pages = {'': {'results': [{'toObjectId': '301'}],
                                  'paging': {'next': {'after': 'next'}}}}
        result = self.read()
        self.assertEqual([event['id'] for event in result['events']], ['hubspot:emails:301'])
        self.assertFalse(self.source(result, 'hubspot')['complete'])
        self.assertIn('page_limit', self.source(result, 'hubspot')['reason'])

    def test_second_page_fetched_and_duplicate_association_not_duplicate_event(self):
        self.activity.pages = {
            '': {'results': [{'toObjectId': '301'}], 'paging': {'next': {'after': 'next'}}},
            'next': {'results': [{'toObjectId': '301'}, {'toObjectId': '303'}]}}
        result = self.read()
        self.assertEqual([event['text'] for event in result['events']], ['First question', 'Last seller outbound'])
        self.assertTrue(self.source(result, 'hubspot')['complete'])
        self.assertTrue(any('after=next' in path for _, path, _ in self.activity.calls))

    def test_missing_paging_cursor_is_partial(self):
        self.activity.pages = {'': {'results': [{'toObjectId': '301'}],
                                  'paging': {'next': {'link': 'https://untrusted.invalid'}}}}
        result = self.read()
        self.assertFalse(self.source(result, 'hubspot')['complete'])
        self.assertEqual(len(result['events']), 1)

    def test_plaintext_preserves_literal_html_for_later_ui_escape(self):
        text = '  Original <img src=x onerror=alert(1)> & 2 < 3\nNext line  '
        self.activity.emails[0]['properties']['hs_email_text'] = text
        result = self.read()
        self.assertEqual(result['events'][0]['text'], text)
        self.assertTrue(result['events'][0]['body_complete'])
        self.assertIn('&lt;img', escape(result['events'][0]['text']))
        self.assertNotIn('<img', escape(result['events'][0]['text']))

    def test_known_thread_reply_new_outbound_all_refreshed_not_inbox_search(self):
        self.gmail_response = thread_response([
            message('aa1', 'Initial seller', outgoing=True, when='2026-10-24T09:00:00Z'),
            message('aa2', 'Human reply', when='2026-10-24T10:00:00Z'),
            message('aa3', 'Latest outbound', outgoing=True, when='2026-10-25T11:30:00Z')])
        result = self.read(gmail=True)
        events = [event for event in result['events'] if event['provider'] == 'gmail']
        self.assertEqual([event['text'] for event in events], ['Initial seller', 'Human reply', 'Latest outbound'])
        self.assertEqual([event['direction'] for event in events], ['outgoing', 'incoming', 'outgoing'])
        self.assertEqual(self.gmail_calls[0][:3], ('GET', '/v1/threads/abc123', None))
        self.assertEqual(len(self.gmail_calls), 1)
        self.assertFalse(self.source(result, 'gmail')['complete'])
        self.assertIn('discovery', self.source(result, 'gmail')['reason'])
        self.assertFalse(result['complete'])

    def test_verified_secondary_alias_binds_known_thread(self):
        self.repo.rows = [persisted(alias='alias@mail.invalid')]
        self.gmail_response = thread_response([message('aa1', 'Alias original', alias='alias@mail.invalid')])
        result = self.read(gmail=True)
        self.assertTrue(any(event['text'] == 'Alias original' for event in result['events']))

    def test_unknown_alias_hash_mailbox_or_thread_never_calls_gmail(self):
        for mutation in ('alias', 'hash', 'mailbox', 'thread'):
            self.repo.rows = [persisted()]
            if mutation == 'alias': self.repo.rows = [persisted(alias='other@mail.invalid')]
            if mutation == 'hash': self.repo.rows[0]['source_hash'] = 'bad'
            if mutation == 'mailbox': self.repo.rows[0]['mailbox'] = 'other@mail.invalid'
            if mutation == 'thread':
                self.repo.rows[0]['messages'][0]['thread_id'] = '../bad'
                self.repo.rows[0]['source_hash'] = payload_hash(self.repo.rows[0]['messages'])
            with self.subTest(mutation=mutation):
                result = self.read(gmail=True)
                self.assertFalse(self.gmail_calls)
                self.assertFalse(result['complete'])
                self.assertFalse(any(event['provider'] == 'gmail' for event in result['events']))

    def test_429_activities_visible_while_gmail_survives(self):
        self.activity.fail['/crm/v4/objects/contacts/123/associations/emails'] = 429
        result = self.read(gmail=True)
        self.assertIn('429', self.source(result, 'hubspot')['reason'])
        self.assertFalse(self.source(result, 'hubspot')['complete'])
        self.assertTrue(any(event['provider'] == 'gmail' for event in result['events']))
        self.assertNotIn('private provider payload', json.dumps(result))

    def test_gmail_down_keeps_crm_originals_no_retry(self):
        calls = []
        def failed(*args):
            calls.append(args[1])
            return 503, b'{"private":"payload"}'
        self.seqd._request = failed
        result = self.read(gmail=True)
        self.assertEqual(len(result['events']), 3)
        self.assertIn('unavailable', self.source(result, 'gmail')['reason'])
        self.assertIn('503', self.source(result, 'gmail')['reason'])
        self.assertEqual(calls, ['/v1/threads/abc123'])
        self.assertFalse(result['complete'])

    def test_identity_429_visible_and_no_activity_or_mail_requests(self):
        self.http.fail['/account-info/v3/details'] = 429
        result = self.read(gmail=True)
        self.assertIn('429', self.source(result, 'hubspot_identity')['reason'])
        self.assertFalse(result['events'])
        self.assertEqual((self.activity.calls, self.gmail_calls), ([], []))

    def test_gmail_429_keeps_crm_and_is_visible(self):
        self.seqd._request = lambda *args: (429, b'{}')
        result = self.read(gmail=True)
        self.assertEqual(len(result['events']), 3)
        self.assertIn('429', self.source(result, 'gmail')['reason'])
        self.assertEqual(self.source(result, 'gmail')['status'], 'unavailable')

    def test_one_failed_known_thread_does_not_hide_another(self):
        self.repo.rows.append(persisted('abc124'))
        def request(method, path, body, headers, timeout):
            if path.endswith('abc123'):
                return 503, b'{}'
            return 200, json.dumps(thread_response([
                message('aa4', 'Independent thread survives', thread='abc124')], 'abc124')).encode()
        self.seqd._request = request
        result = self.read(gmail=True)
        self.assertTrue(any(event['text'] == 'Independent thread survives' for event in result['events']))
        self.assertIn('unavailable', self.source(result, 'gmail')['reason'])
        self.assertFalse(result['complete'])

    def test_gmail_html_fallback_and_plaintext_are_untrusted_originals(self):
        self.gmail_response['thread']['messages'][0]['bodies'] = {
            'text': None, 'html': '<p>Original &amp; intact</p><script>hidden()</script>'}
        result = self.read(gmail=True)
        event = next(event for event in result['events'] if event['provider'] == 'gmail')
        self.assertEqual(event['text'], 'Original & intact')
        self.assertTrue(event['body_complete'])
        self.gmail_response['thread']['messages'][0]['bodies']['text'] = '  2 < 3 & <tag>\nOriginal  '
        result = self.read(gmail=True)
        event = next(event for event in result['events'] if event['provider'] == 'gmail')
        self.assertEqual(event['text'], '  2 < 3 & <tag>\nOriginal  ')

    def test_outgoing_draft_and_unrelated_message_are_not_contact_history(self):
        draft = message('aa2', 'Unsent draft', outgoing=True)
        draft['labelIds'] = ['SENT', 'DRAFT']
        self.gmail_response['thread']['messages'].extend([
            draft, message('aa3', 'Unrelated person', alias='other@mail.invalid')])
        result = self.read(gmail=True)
        self.assertEqual([event['text'] for event in result['events'] if event['provider'] == 'gmail'],
                         ['Original reply'])
        self.assertIn('identity_unverified', self.source(result, 'gmail')['reason'])

    def test_portal_mismatch_never_reads_activity_records_but_gmail_survives(self):
        self.activity.portal = '999'
        result = self.read(gmail=True)
        self.assertEqual(len(self.activity.calls), 1)
        self.assertTrue(any(event['provider'] == 'gmail' for event in result['events']))
        self.assertIn('portal_mismatch', self.source(result, 'hubspot')['reason'])

    def test_wrong_live_participant_or_mailbox_does_not_leak_thread(self):
        for change in ('participant', 'mailbox'):
            self.gmail_response = thread_response([message('aa1', 'Other person', alias='other@mail.invalid')])
            if change == 'mailbox': self.gmail_response['mailbox'] = 'other@mail.invalid'
            with self.subTest(change=change):
                result = self.read(gmail=True)
                self.assertFalse(any(event['provider'] == 'gmail' for event in result['events']))
                self.assertFalse(self.source(result, 'gmail')['complete'])

    def test_partial_gmail_body_not_promoted_to_complete(self):
        self.gmail_response['thread'].update(complete=False, truncated=True)
        self.gmail_response['thread']['messages'][0].update(complete=False)
        result = self.read(gmail=True)
        event = next(event for event in result['events'] if event['provider'] == 'gmail')
        self.assertEqual(event['text'], 'Original reply')
        self.assertFalse(event['body_complete'])
        self.assertIn('partial', self.source(result, 'gmail')['reason'])

    def test_contract_field_sets_and_event_ids_are_stable(self):
        first, second = self.read(gmail=True), self.read(gmail=True)
        self.assertEqual(set(first), {'contact_id', 'company_id', 'observed_at',
            'events', 'sources', 'complete', 'read_only'})
        self.assertEqual(first, second)
        for event in first['events']:
            self.assertEqual(set(event), {'id', 'provider', 'date', 'direction', 'subject',
                'text', 'body_complete', 'source_url', 'thread_id'})
        for source in first['sources']:
            self.assertEqual(set(source), {'provider', 'status', 'complete', 'reason'})

    def test_unconfigured_gmail_and_empty_known_threads_never_claim_absence(self):
        result = self.read()
        self.assertFalse(result['complete'])
        self.assertEqual(self.source(result, 'gmail')['status'], 'unavailable')
        self.repo.rows = []
        result = self.read(gmail=True)
        self.assertFalse(self.gmail_calls)
        self.assertIn('no_verified_known_threads', self.source(result, 'gmail')['reason'])
        self.assertFalse(result['complete'])

    def test_gmail_budget_stops_before_next_thread_without_retry(self):
        self.repo.rows.append(persisted('abc124'))
        ticks = [1.0]
        original = self.seqd._request
        def slow(*args):
            ticks[0] += 16
            return original(*args)
        self.seqd._request = slow
        with patch('thunder_os.contact_history.time', SimpleNamespace(monotonic=lambda: ticks[0])):
            result = self.read(gmail=True)
        self.assertEqual(len(self.gmail_calls), 1)
        self.assertIn('budget_exhausted', self.source(result, 'gmail')['reason'])

    def test_invalid_persisted_thread_does_not_hide_an_independent_known_thread(self):
        invalid = persisted('abc124')
        invalid['messages'][0]['thread_id'] = []
        invalid['source_hash'] = payload_hash(invalid['messages'])
        self.repo.rows.insert(0, invalid)
        result = self.read(gmail=True)
        self.assertEqual(len(self.gmail_calls), 1)
        self.assertTrue(any(event['provider'] == 'gmail' for event in result['events']))
        self.assertIn('identity_unverified', self.source(result, 'gmail')['reason'])

    def test_clock_accepts_epoch_and_rejects_unzoned_or_invalid_values_before_provider(self):
        from thunder_os.contact_history import ContactHistoryReadonly
        result = ContactHistoryReadonly(self.hubspot, self.cadence,
            clock=lambda: NOW.timestamp()).read(self.row, self.context)
        self.assertEqual(result['observed_at'], '2026-10-25T12:00:00Z')
        count = len(self.http.calls)
        for value in (float('nan'), datetime(2026, 10, 25), True, 'yesterday'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'clock_invalid'):
                ContactHistoryReadonly(self.hubspot, self.cadence, clock=lambda: value).read(self.row, self.context)
        self.assertEqual(len(self.http.calls), count)


if __name__ == '__main__':
    unittest.main()
