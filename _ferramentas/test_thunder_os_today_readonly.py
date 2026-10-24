"""PRD4 H01-H06/H11: synthetic SQLite, read-port doubles, no network or model."""
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import painel_servidor as panel
from thunder_os.cadence_readonly import HubSpotReadonlyAdapter, OWNER_ID, PORTAL_ID
from thunder_os.db import Repository
from thunder_os.hubspot_executor import HubSpotHTTP
from thunder_os.models import payload_hash
from thunder_os.panel_verification import VerificationError
from thunder_os.seqd_client import MAILBOX
from thunder_os.service import ThunderService
from thunder_os.today_readonly import TodayReadonly, TodayError, ReadonlyRepository

NOW = datetime(2026, 10, 23, 15, tzinfo=timezone.utc).timestamp()
PEER = 'commercial@example.invalid'


def message(ident='m1', hour=10, outgoing=False, **extra):
    return {'id': ident, 'date': f'2026-10-23T{hour:02}:00:00Z', 'outgoing': outgoing,
            'text': 'Synthetic commercial question', 'sender': PEER, 'subject': 'Synthetic',
            'body_complete': True,
            'canonical_id': 'gmail:' + ident, **extra}


def task(ident='10', title='Call confirmed contact', **props):
    return {'id': ident, 'archived': False, 'properties': {'hubspot_owner_id': OWNER_ID,
        'hs_task_subject': title, 'hs_task_body': 'Recorded call context',
        'hs_timestamp': '2026-10-23T20:00:00Z', 'hs_task_status': 'NOT_STARTED',
        'hs_task_type': 'CALL', **props}}


class ReadPort:
    def __init__(self):
        self.calls, self.tasks, self.emails, self.deals = [], [], [], []
        self.links, self.contacts = {}, {}
        self.contact_rows = None
        self.fail = set()

    def __call__(self, method, path, body, headers, timeout):
        self.calls.append((method, path, deepcopy(body), timeout))
        if path in self.fail:
            raise OSError('sensitive provider text must not leak')
        if path == '/account-info/v3/details':
            result = {'portalId': PORTAL_ID}
        elif path == '/crm/v3/owners/' + OWNER_ID:
            result = {'id': OWNER_ID, 'archived': False}
        elif path.startswith('/crm/v3/properties/'):
            schemas = {
                'tasks': set(task()['properties']) | {'hs_task_sequence_id', 'hs_task_sequence_step_enrollment_id',
                    'hs_task_sequence_step_enrollment_contact_id'},
                'emails': {'hubspot_owner_id', 'hs_email_direction', 'hs_timestamp', 'hs_email_subject',
                    'hs_email_text', 'hs_email_headers', 'hs_email_from_email', 'hs_email_message_id'},
                'contacts': {'email', 'campanha_ativa', 'executor'},
                'deals': {'dealname', 'hubspot_owner_id', 'hs_is_closed', 'notes_next_activity_date',
                    'hs_next_step', 'campanha_ativa'}}
            result = {'results': [{'name': name} for name in schemas[path.rsplit('/', 1)[-1]]]}
        elif path == '/crm/v3/objects/contacts/search':
            wanted = set(body['filterGroups'][0]['filters'][0]['values'])
            rows = self.contact_rows if self.contact_rows is not None else [
                {'id': ident, 'properties': properties, 'archived': False}
                for ident, properties in self.contacts.items() if properties.get('email', '').lower() in wanted]
            result = {'results': deepcopy(rows), 'total': len(rows)}
        elif path.endswith('/search'):
            rows = getattr(self, path.split('/')[-2])
            result = {'results': deepcopy(rows), 'total': len(rows)}
        elif path.startswith('/crm/v4/associations/') and path.endswith('/batch/read'):
            kind = path.split('/')[4]
            result = {'status': 'COMPLETE', 'results': [
                {'from': {'id': item['id']}, 'to': [{'toObjectId': cid} for cid in self.links.get((kind, item['id']), [])]}
                for item in body['inputs']]}
        elif path == '/crm/v3/objects/contacts/batch/read':
            result = {'status': 'COMPLETE', 'results': [
                {'id': item['id'], 'properties': self.contacts[item['id']], 'archived': False}
                for item in body['inputs'] if item['id'] in self.contacts]}
        else:
            raise AssertionError((method, path))
        return 200, json.dumps(result).encode()


class TodayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = Repository(self.root/'panel.db')
        with closing(sqlite3.connect(self.repo.path)) as con, con:
            con.execute('CREATE TABLE itens(id TEXT PRIMARY KEY,dados JSON NOT NULL,atualizado TEXT NOT NULL)')
        self.service = ThunderService(self.repo)
        self.port = ReadPort()
        self.runner = Mock(metadata={'provider': 'hermes_codex_oauth'})
        self.elapsed = [0]
        self.today = TodayReadonly(self.service, clock=lambda: NOW, monotonic=lambda: self.elapsed[0],
            runner=self.runner,
            hubspot=HubSpotReadonlyAdapter(lambda: 'fixture-token', request=self.port, sleep=lambda _: None),
            deals=HubSpotHTTP(lambda: 'fixture-token', request=self.port))
        self.addCleanup(patch.stopall)
        patch('socket.create_connection', side_effect=AssertionError('network forbidden')).start()
        patch('socket.socket.connect', side_effect=AssertionError('network forbidden')).start()

    def card(self, ident='legacy-fixture', **changes):
        value = {'status': 'pendente', 'tipo': 'dm', 'conta': 'synthetic-account',
            'titulo': 'Old synthetic suggestion', 'criado': '2026-10-01T12:00:00Z',
            'contexto': [], 'texto': 'Historical unapproved copy', **changes}
        with closing(sqlite3.connect(self.repo.path)) as con, con:
            con.execute('INSERT INTO itens VALUES(?,?,?)', (ident, json.dumps(value), '2026-10-01'))
        return value

    def state(self):
        with closing(sqlite3.connect(self.repo.path)) as con, con:
            return (con.execute('SELECT * FROM itens ORDER BY id').fetchall(),
                    con.execute('SELECT * FROM thunder_state ORDER BY kind,entity_id').fetchall(),
                    con.execute('SELECT * FROM thunder_effect_jobs').fetchall())

    def identity(self, contact='700', peer=PEER):
        self.repo.put_state('identity', 'identity-fixture', {'actor': 'ygor', 'status': 'canonical',
            'canonical_account': 'commercial.invalid', 'canonical_person': peer,
            'records': [{'domain': 'commercial.invalid', 'person_key': peer, 'source': source,
                         'hubspot_contact_id': contact} for source in ('fixture:crm', 'fixture:mailbox')]})

    def gmail(self, *, complete=True, body='Synthetic inbound', peer=PEER):
        messages = [{'id': 'ab12', 'thread_id': 'de34', 'sender': peer, 'subject': 'Synthetic commercial',
            'received_at': '2026-10-23T09:00:00Z', 'outgoing': False, 'content': body, 'body_complete': True}]
        self.repo.put_state('gmail_thread', 'thread-fixture', {'mailbox': MAILBOX,
            'messages': messages, 'source_hash': payload_hash(messages), 'complete': complete})
        return messages

    def crm_email(self, **properties):
        self.port.links[('emails', '900')] = ['700']
        self.port.contacts['700'] = {'campanha_ativa': 'BR-CRM-FIXTURE', 'executor': 'ygor'}
        row = {'id': '900', 'properties': {
            'hubspot_owner_id': OWNER_ID, 'hs_email_direction': 'INCOMING_EMAIL',
            'hs_timestamp': '2026-10-23T10:00:00Z', 'hs_email_from_email': PEER,
            'hs_email_subject': 'Synthetic CRM inbound', 'hs_email_text': 'Synthetic CRM original',
            **properties}}
        self.port.emails = [row]
        return row

    def thread(self, messages, **changes):
        self.today._mx_contacts, self.today._mx_people, self.today._mx_codes = set(), set(), set()
        return self.today._thread(source='fixture', ident='thread:fixture', title='Synthetic',
            messages=messages, complete=True, action={'label': 'Revisar', 'url': 'https://app.instantly.ai/app/unibox'}, **changes)

    def test_H01_H05_all_owned_due_tasks_and_calls_not_just_campaign_titles(self):
        self.port.tasks = [task(), task('11', hs_timestamp='2026-10-20T20:00:00Z'),
            task('12', hs_task_status='COMPLETED'), task('13', hubspot_owner_id='999'),
            task('14', hs_timestamp='2026-10-24T20:00:00Z'), task('15', hs_timestamp=None),
            task('16', title='[BR-CADENCIA-V1][LI1] Synthetic | LINKEDIN')]
        before = self.state()
        result = self.today.snapshot()
        items = result['blocks']['do']['items']
        self.assertEqual({item['id'] for item in items}, {'hubspot:task:10', 'hubspot:task:11', 'hubspot:task:16'})
        self.assertEqual(items[0]['id'], 'hubspot:task:11')
        self.assertEqual(next(i for i in items if i['id'].endswith(':10'))['link_status'], 'sem_vinculo')
        for item in items:
            self.assertEqual(item['owner_id'], OWNER_ID)
            self.assertTrue(item['date'])
            self.assertEqual(set(item['action']), {'label', 'url'})
        self.assertEqual(before, self.state())
        self.runner.assert_not_called()

    def test_H02_mx_prefixes_and_registered_legacy_alias_excluded_without_changing_campaigns(self):
        self.repo.put_state('campaign_config', 'MX27-MTY', {'config': {'campaign_id': 'MX27-MTY',
            'batches': [{'source_codes': ['old-exact-code']}]}})
        self.port.tasks = [task(str(i+10), title=f'[{code}][LI1] Synthetic | LINKEDIN')
            for i, code in enumerate(['MX27-MTY-L01', 'MX27-CDMX-D0-20261021', 'old-exact-code', 'BR-CADENCIA-V1'])]
        before = self.state()
        result = self.today.snapshot()
        self.assertEqual([i['id'] for i in result['blocks']['do']['items']], ['hubspot:task:13'])
        self.assertEqual(before, self.state())
        self.assertIsNotNone(self.repo.get_state('campaign_config', 'MX27-MTY'))

    def test_H02_unlabelled_mx_reply_excluded_by_verified_task_contact_and_email(self):
        self.identity()
        self.gmail()
        self.port.tasks = [task(title='[MX27-MTY][LI1] Synthetic | LINKEDIN', hs_task_status='COMPLETED')]
        self.port.links[('tasks', '10')] = ['700']
        self.port.contacts['700'] = {'email': PEER, 'campanha_ativa': '', 'executor': ''}
        result = self.today.snapshot()
        self.assertIn('700', self.today._mx_contacts)
        self.assertIn(PEER, self.today._mx_people)
        self.assertEqual(result['blocks']['respond']['items'], [])
        self.assertEqual(result['blocks']['do']['items'], [])

    def test_H03_oldest_unanswered_inbound_and_last_message(self):
        result = self.thread([message(hour=8), message('m2', hour=9), message('sent', hour=7, outgoing=True)])
        item = result['respond'][0]
        self.assertEqual(item['date'], '2026-10-23T08:00:00Z')
        self.assertEqual(item['last_message'], 'Synthetic commercial question')
        self.assertEqual([m['id'] for m in item['thread']['messages']], ['sent', 'm1', 'm2'])
        self.assertTrue(item['thread']['complete'])
        self.assertEqual(self.thread([message(), message('sent', hour=11, outgoing=True)]), {})

    def test_H03_partial_history_never_claims_unanswered(self):
        self.identity(); self.gmail(complete=False)
        result = self.today.snapshot()
        self.assertEqual(result['blocks']['respond']['items'], [])
        self.assertEqual(result['blocks']['do']['items'][0]['triage_reason'], 'partial_history_no_silence_inference')
        self.assertNotIn('draft', result['blocks']['do']['items'][0])

    def test_H03_bounce_auto_newsletter_headers_and_unknown_direction_are_not_commercial_replies(self):
        for extra in [{'subject': 'Out of office: Synthetic'}, {'sender': 'mailer-daemon@example.invalid'},
                      {'headers': [{'name': 'Auto-Submitted', 'value': 'auto-replied'}]},
                      {'headers': [{'name': 'List-Id', 'value': 'fixture.newsletter'}]},
                      {'outgoing': None}]:
            with self.subTest(extra=extra):
                self.assertEqual(self.thread([message(**extra)]), {})

    def test_H03_human_optout_and_body_keywords_stay_visible_without_blind_send(self):
        before = self.state()
        for body in ['Me descadastre, por favor.', 'Please unsubscribe me.',
                     'Temos uma vaga para falar sobre recrutamento.', 'Gostei da newsletter.']:
            with self.subTest(body=body):
                result = self.thread([message('sent', hour=8, outgoing=True), message(text=body)], contact_id='700')
                self.assertEqual(result['respond'][0]['last_message'], body)
                self.assertTrue(result['respond'][0]['thread']['complete'])
        self.assertEqual(before, self.state())
        self.runner.assert_not_called()

    def test_H03_missing_or_false_body_complete_never_qualifies_for_respond_or_draft(self):
        for complete in (None, False):
            with self.subTest(complete=complete):
                result = self.thread([message(body_complete=complete)])
                self.assertNotIn('respond', result)
                self.assertFalse(result['do'][0]['history_complete'])
                self.assertNotIn('draft', result['do'][0])

    def test_H03_personal_gmail_without_commercial_identity_not_promoted(self):
        self.gmail()
        result = self.today.snapshot()
        self.assertEqual(result['blocks']['respond']['items'], [])

    def test_H03_sources_fail_independently_and_errors_are_sanitized(self):
        self.port.fail.add('/crm/v3/objects/emails/search')
        self.port.tasks = [task()]
        result = self.today.snapshot()
        self.assertEqual(len(result['blocks']['do']['items']), 1)
        sources = {s['id']: s for s in result['blocks']['do']['sources']}
        self.assertNotEqual(sources['hubspot_tasks']['status'], 'offline')
        self.assertEqual(sources['hubspot_emails']['status'], 'offline')
        self.assertNotIn('sensitive provider', json.dumps(result))

    def test_H03_canonical_message_dedup_between_gmail_and_seqd(self):
        self.identity(); self.gmail()
        seqd = SimpleNamespace(read_enabled=True, port=Mock())
        seqd.port.messages.return_value = {'mailbox': MAILBOX, 'messages': [{'id': 'ab12', 'thread_id': 'de34'}]}
        seqd.port.conversation.return_value = {'mailbox': MAILBOX, 'thread': {'complete': True, 'truncated': False,
            'messages': [{'id': 'aa11', 'from': MAILBOX, 'to': PEER, 'internal_date': '1792742400000',
                'labelIds': ['SENT'], 'subject': 'Synthetic outbound', 'bodies': {'text': 'Commercial followup'}, 'headers': [], 'complete': True},
                {'id': 'ab12', 'from': PEER, 'to': MAILBOX, 'internal_date': '1792746000000',
                'labelIds': ['INBOX'], 'message_id': '<fixture@example.invalid>', 'subject': 'Synthetic',
                'bodies': {'text': 'Synthetic inbound'}, 'headers': [], 'complete': True}]}}
        self.today.seqd = seqd
        result = self.today.snapshot()
        self.assertEqual(len(result['blocks']['respond']['items']), 1)
        self.assertEqual(result['blocks']['respond']['items'][0]['source_ids'], ['gmail', 'seqd'])

    def test_H04_GET_never_calls_runner_and_missing_draft_is_explicit(self):
        self.identity(); self.gmail()
        item = self.today.snapshot()['blocks']['respond']['items'][0]
        self.assertEqual(item['draft_status'], 'missing')
        self.assertTrue(item['draft_limitation'])
        self.assertNotIn('draft', item)
        self.runner.assert_not_called()

    def test_H03_instantly_complete_snapshot_responds_and_partial_only_triages(self):
        self.identity()
        self.repo.put_state('instantly_reader_snapshot', 'BR-INSTANTLY-01', {
            'status': 'ok', 'last_success_at': datetime.fromtimestamp(NOW, timezone.utc).isoformat(),
            'coverage': {'complete': True}, 'leads': [{'email': PEER, 'events': [
                {'provider_id': 'observed-outbound', 'occurred_at': '2026-10-23T09:00:00Z',
                 'event_type': 'email_sent', 'text': 'Commercial question', 'content_complete': True},
                {'provider_id': 'observed-email', 'occurred_at': '2026-10-23T10:00:00Z',
                 'event_type': 'reply_received', 'text': 'Question', 'subject': 'Synthetic', 'content_complete': True}]}]})
        self.today.instantly = SimpleNamespace(enable_check=lambda: True,
            clock=lambda: datetime.fromtimestamp(NOW, timezone.utc))
        result = self.today.snapshot()
        self.assertEqual(len(result['blocks']['respond']['items']), 1)
        self.assertEqual(result['blocks']['respond']['items'][0]['campaign_id'], 'BR-INSTANTLY-01')
        self.repo.put_state('instantly_reader_snapshot', 'BR-INSTANTLY-01', {
            **self.repo.get_state('instantly_reader_snapshot', 'BR-INSTANTLY-01'), 'status': 'partial',
            'coverage': {'complete': False}})
        self.elapsed[0] = 61
        result = self.today.snapshot()
        self.assertEqual(result['blocks']['respond']['items'], [])
        self.assertEqual(result['blocks']['do']['items'][0]['triage_reason'], 'partial_history_no_silence_inference')

    def test_H03_hubspot_inbound_requires_owner_or_exact_mailbox_and_never_infers_silence(self):
        base = {'hs_email_direction': 'INCOMING_EMAIL', 'hs_timestamp': '2026-10-23T10:00:00Z',
                'hs_email_subject': 'Synthetic', 'hs_email_text': 'Question',
                'hs_email_headers': json.dumps({'from': {'email': PEER}, 'to': [{'email': MAILBOX}]})}
        self.port.emails = [{'id': '900', 'properties': base},
            {'id': '901', 'properties': {**base, 'hs_email_headers': '{}'}},
            {'id': '902', 'properties': {**base, 'hs_email_subject': 'Automatic reply: Synthetic'}}]
        result = self.today.snapshot()
        self.assertEqual(result['blocks']['respond']['items'], [])
        self.assertEqual(result['blocks']['do']['items'], [])
        source = next(s for s in result['blocks']['do']['sources'] if s['id'] == 'hubspot_emails')
        self.assertEqual(source['unclassified_count'], 1)
        self.assertIn('crm_thread_history_incomplete', source['limitations'])

    def test_hubspot_original_full_body_is_observed_with_unknown_human_and_no_action(self):
        original = '  Original <literal> & text.\n\n' + 'Exact CRM line with  spaces.\n' * 3000 + 'End.  '
        self.crm_email(hs_email_text=original)
        before = self.state()
        result = self.today.snapshot()
        self.assertEqual(len(result['observed_threads']), 1)
        observed = result['observed_threads'][0]
        self.assertEqual(observed['contact_id'], '700')
        self.assertEqual(observed['campaign_id'], 'BR-CRM-FIXTURE')
        self.assertEqual(observed['review_status'], 'leitura_pendente')
        self.assertTrue(observed['observation_only'])
        self.assertFalse(observed['thread']['complete'])
        self.assertEqual(observed['date'], '2026-10-23T10:00:00Z')
        self.assertEqual(observed['source']['id'], 'hubspot_emails')
        self.assertEqual(observed['source']['collected_at'], datetime.fromtimestamp(NOW, timezone.utc).isoformat().replace('+00:00', 'Z'))
        message = observed['thread']['messages'][0]
        self.assertEqual(message['text'], original)
        self.assertTrue(message['body_complete'])
        self.assertEqual(message['direction'], 'incoming')
        self.assertIsNone(message['human_reply'])
        self.assertIsNone(message['receipt'])
        self.assertEqual(message['source_id'], 'hubspot:email:900')
        self.assertEqual(result['blocks']['respond']['items'], [])
        self.assertEqual(result['blocks']['do']['items'], [])
        self.assertEqual(self.today._packets, {})
        self.assertEqual(before, self.state())
        self.runner.assert_not_called()

    def test_hubspot_missing_invalid_or_html_only_body_never_claims_complete_original(self):
        for body in (None, '', '  ', {'summary': 'Not an original body'}):
            with self.subTest(body=body):
                self.crm_email(hs_email_text=body, hs_email_html='<p>HTML is not hs_email_text</p>')
                self.today._expires = 0
                result = self.today.snapshot()
                observed = result['observed_threads'][0]
                self.assertFalse(observed['thread']['messages'][0]['body_complete'])
                self.assertEqual(observed['thread']['messages'][0]['text'], body if isinstance(body, str) else '')
                self.assertFalse(observed['thread']['complete'])
                self.assertEqual(result['blocks']['respond']['items'], [])
                self.assertEqual(result['blocks']['do']['items'], [])
        self.runner.assert_not_called()

    def test_hubspot_observation_requires_unique_resolved_id_campaign_and_event_date(self):
        for failure in ('unlinked', 'ambiguous', 'unresolved', 'no_campaign', 'mx', 'mixed_mx', 'invalid_date'):
            with self.subTest(failure=failure):
                self.crm_email()
                self.port.contacts['701'] = {'campanha_ativa': 'MX27-FIXTURE', 'executor': 'ygor'}
                if failure == 'unlinked': self.port.links[('emails', '900')] = []
                if failure == 'ambiguous':
                    self.port.contacts['701']['campanha_ativa'] = 'BR-CRM-FIXTURE'
                    self.port.links[('emails', '900')] = ['700', '701']
                if failure == 'unresolved': self.port.contacts.pop('700')
                if failure == 'no_campaign': self.port.contacts['700']['campanha_ativa'] = ''
                if failure == 'mx': self.port.contacts['700']['campanha_ativa'] = 'MX27-FIXTURE'
                if failure == 'mixed_mx': self.port.links[('emails', '900')] = ['700', '701']
                if failure == 'invalid_date': self.port.emails[0]['properties']['hs_timestamp'] = 'unknown'
                self.today._expires = 0
                result = self.today.snapshot()
                self.assertEqual(result['observed_threads'], [])
                self.assertEqual(result['blocks']['respond']['items'], [])
                self.assertEqual(result['blocks']['do']['items'], [])

    def test_hubspot_inbound_plus_later_full_outgoing_keeps_chronology_without_pending(self):
        self.crm_email()
        messages = self.gmail()
        messages[0]['received_at'] = '2026-10-23T10:00:00Z'
        messages.append({**messages[0], 'id': 'ab13', 'received_at': '2026-10-23T11:00:00Z',
                         'outgoing': True, 'sender': MAILBOX, 'content': 'Full outgoing after CRM inbound'})
        self.repo.put_state('gmail_thread', 'thread-fixture', {'mailbox': MAILBOX,
            'messages': messages, 'source_hash': payload_hash(messages), 'complete': True})
        before = self.state()
        result = self.today.snapshot()
        self.assertEqual(len(result['observed_threads']), 2)
        observed = next(t for t in result['observed_threads'] if t['source']['id'] == 'gmail')
        self.assertTrue(observed['thread']['complete'])
        self.assertEqual([m['direction'] for m in observed['thread']['messages']], ['incoming', 'outgoing'])
        self.assertEqual(observed['thread']['messages'][-1]['text'], messages[-1]['content'])
        self.assertEqual(result['blocks']['respond']['items'], [])
        self.assertEqual(result['blocks']['do']['items'], [])
        self.assertEqual(self.today._packets, {})
        self.assertEqual(before, self.state())
        self.runner.assert_not_called()

    def test_hubspot_automated_messages_never_become_pending_read_observations(self):
        for change in ({'hs_email_subject': 'Delivery failure: Synthetic'},
                       {'hs_email_from_email': 'mailer-daemon@example.invalid'},
                       {'hs_email_headers': json.dumps({'headers': [{'name': 'Auto-Submitted', 'value': 'auto-replied'}]})}):
            with self.subTest(change=change):
                self.crm_email(**change)
                self.today._expires = 0
                result = self.today.snapshot()
                self.assertEqual(result['observed_threads'], [])
                self.assertEqual(result['blocks']['respond']['items'], [])
                self.assertEqual(result['blocks']['do']['items'], [])

    def test_H03_395_historical_inbound_records_are_metadata_never_work(self):
        self.port.emails = [{'id': str(i + 1000), 'properties': {
            'hubspot_owner_id': OWNER_ID, 'hs_email_direction': 'INCOMING_EMAIL',
            'hs_timestamp': '2026-01-01T10:00:00Z', 'hs_email_subject': 'Synthetic old reply'}} for i in range(395)]
        before = self.state()
        result = self.today.snapshot()
        self.assertEqual(result['blocks']['do']['items'], [])
        self.assertEqual(result['blocks']['respond']['items'], [])
        source = next(s for s in result['blocks']['do']['sources'] if s['id'] == 'hubspot_emails')
        self.assertEqual(source['unclassified_count'], 395)
        self.assertEqual(before, self.state())

    def test_H03_exact_email_lookup_is_ephemeral_owned_unique_and_bounded(self):
        self.today.snapshot()
        self.port.contacts['700'] = {'email': PEER, 'hubspot_owner_id': OWNER_ID, 'campanha_ativa': 'BR-fixture'}
        before = self.state()
        self.today._resolve_observed_emails({PEER})
        self.assertEqual(self.today._contact_for({PEER}), '700')
        self.assertEqual(self.today._campaign_for({PEER}), 'BR-fixture')
        self.assertEqual(before, self.state())
        self.today._resolve_observed_emails({PEER})
        lookups = [body for method, path, body, _ in self.port.calls if path == '/crm/v3/objects/contacts/search']
        self.assertEqual(len(lookups), 1)
        self.assertEqual(lookups[0]['filterGroups'][0]['filters'], [
            {'propertyName': 'email', 'operator': 'IN', 'values': [PEER]}])
        self.today._resolve_observed_emails({f'contact{i}@example.invalid' for i in range(200)})
        last = [body for _, path, body, _ in self.port.calls if path == '/crm/v3/objects/contacts/search'][-1]
        self.assertLessEqual(len(last['filterGroups'][0]['filters'][0]['values']), 100)
        self.assertLessEqual(len(self.today._email_lookups), 100)

    def test_H03_exact_email_rejects_collisions_wrong_owner_wrong_email_and_archived(self):
        good = {'id': '700', 'properties': {'email': PEER, 'hubspot_owner_id': OWNER_ID}}
        cases = [[good, {**good, 'id': '701'}],
            [{'id': '700', 'properties': {'email': PEER, 'hubspot_owner_id': '999'}}],
            [{'id': '700', 'properties': {'email': 'other@example.invalid', 'hubspot_owner_id': OWNER_ID}}],
            [{**good, 'archived': True}]]
        for rows in cases:
            with self.subTest(rows=rows):
                self.today._expires = 0
                self.today.snapshot()
                self.port.contact_rows = rows
                self.today._resolve_observed_emails({PEER})
                self.assertIsNone(self.today._contact_for({PEER}))

    def test_H03_seqd_three_pages_exact_email_contact_and_thunder_thread_required(self):
        self.port.contacts['700'] = {'email': PEER, 'hubspot_owner_id': OWNER_ID}
        port = Mock()
        port.messages.side_effect = [{'mailbox': MAILBOX, 'messages': [{'id': str(i), 'thread_id': 'de34'}],
            'next_cursor': str(i + 1)} for i in range(3)]
        thread = {'complete': True, 'truncated': False, 'messages': [
            {'id': 'aa11', 'from': MAILBOX, 'to': PEER, 'internal_date': '1792742400000',
             'labelIds': ['SENT'], 'bodies': {'text': 'Commercial question'}, 'complete': True, 'headers': []},
            {'id': 'ab12', 'from': PEER, 'to': MAILBOX, 'internal_date': '1792746000000',
             'labelIds': ['INBOX'], 'bodies': {'text': 'Commercial reply'}, 'complete': True, 'headers': []}]}
        port.conversation.return_value = {'mailbox': MAILBOX, 'thread': thread}
        self.today.seqd = SimpleNamespace(read_enabled=True, port=port)
        before = self.state()
        result = self.today.snapshot()
        self.assertEqual(port.messages.call_count, 3)
        self.assertEqual([c.kwargs for c in port.messages.call_args_list], [
            {'limit': 10}, {'limit': 10, 'cursor': '1'}, {'limit': 10, 'cursor': '2'}])
        self.assertEqual(port.conversation.call_count, 1)
        self.assertEqual(result['blocks']['respond']['items'][0]['contact_id'], '700')
        self.assertEqual(before, self.state())
        port.messages.side_effect = None
        port.messages.return_value = {'mailbox': MAILBOX, 'messages': [{'thread_id': 'de34'}]}
        for replacement in [thread['messages'][1:], [{**thread['messages'][0]},
                {**thread['messages'][1], 'headers': [{'name': 'Auto-Submitted', 'value': 'auto-replied'}]}]]:
            with self.subTest(replacement=replacement):
                port.conversation.return_value = {'mailbox': MAILBOX, 'thread': {**thread, 'messages': replacement}}
                self.today._expires = 0
                self.assertEqual(self.today.snapshot()['blocks']['respond']['items'], [])

    def test_H04_persisted_draft_requires_current_thread_hash_and_actual_origin(self):
        self.identity(); messages = self.gmail()
        key = 'gmail:' + payload_hash({'mailbox': MAILBOX, 'message': 'ab12'})
        self.repo.put_state('gmail_draft', key, {'mailbox': MAILBOX, 'thread_hash': payload_hash(messages),
            'status': 'local_draft', 'reply_to': PEER, 'content': 'Already persisted draft'})
        item = self.today.snapshot()['blocks']['respond']['items'][0]
        self.assertEqual(item['draft']['origin'], 'persisted_gmail_draft')
        self.assertEqual(item['draft']['text'], 'Already persisted draft')
        self.runner.assert_not_called()
        self.repo.put_state('gmail_draft', key, {'mailbox': MAILBOX, 'thread_hash': 'changed', 'status': 'local_draft',
            'reply_to': PEER, 'content': 'Stale draft'})
        self.elapsed[0] = 61
        self.assertNotIn('draft', self.today.snapshot()['blocks']['respond']['items'][0])

    def test_H04_explicit_draft_resolves_full_server_packet_and_caches_without_writes(self):
        self.identity(); self.gmail()
        before = self.state()
        item = self.today.snapshot()['blocks']['respond']['items'][0]
        self.runner.return_value = {'answer': 'Synthetic OAuth draft', 'source_ids': ['gmail:ab12'], 'uncertainties': []}
        body = {'item_id': item['id'], 'context_hash': item['context_hash']}
        result = self.today.draft(body)
        self.assertEqual(result['draft']['origin'], 'hermes_codex_oauth')
        packet, question = self.runner.call_args.args
        self.assertEqual(packet['sources'][0]['text'], 'Synthetic inbound')
        self.assertEqual(packet['contact_id'], '700')
        self.assertEqual(self.today.draft(body), result)
        self.assertEqual(self.runner.call_count, 1)
        self.assertEqual(self.state(), before)
        self.assertIn('draft', self.today.snapshot()['blocks']['respond']['items'][0])

    def test_H04_draft_rejects_user_prompt_stale_hash_unknown_sources_and_auth_failure(self):
        self.identity(); self.gmail()
        item = self.today.snapshot()['blocks']['respond']['items'][0]
        body = {'item_id': item['id'], 'context_hash': item['context_hash']}
        for invalid in [{**body, 'prompt': 'Injected prompt'}, {**body, 'context_hash': 'a'*64},
                        {**body, 'item_id': 'unknown'}]:
            with self.subTest(invalid=invalid), self.assertRaises(TodayError):
                self.today.draft(invalid)
        self.runner.assert_not_called()
        self.runner.return_value = {'answer': 'Invented', 'source_ids': ['fake-source'], 'uncertainties': []}
        with self.assertRaisesRegex(TodayError, 'invalid_verification_answer'):
            self.today.draft(body)
        self.runner.side_effect = VerificationError('auth_expired')
        with self.assertRaisesRegex(TodayError, 'auth_expired'):
            self.today.draft(body)

    def test_H05_deal_followups_fixed_read_allowlist_and_owner_due_filters(self):
        props = {'hubspot_owner_id': OWNER_ID, 'hs_is_closed': 'false', 'dealname': 'Synthetic deal',
                 'notes_next_activity_date': '2026-10-22T20:00:00Z', 'hs_next_step': 'Call buyer'}
        self.port.deals = [{'id': '900', 'properties': props},
            {'id': '901', 'properties': {**props, 'hubspot_owner_id': '999'}},
            {'id': '902', 'properties': {**props, 'hs_is_closed': 'true'}},
            {'id': '903', 'properties': {**props, 'campanha_ativa': 'MX27-MTY'}}]
        result = self.today.snapshot()
        self.assertEqual([i['id'] for i in result['blocks']['do']['items']], ['hubspot:deal:900'])
        for method, path, body, timeout in self.port.calls:
            self.assertIn(method, {'GET', 'POST'})
            if method == 'POST':
                self.assertTrue(path.endswith(('/search', '/batch/read')), path)
            self.assertLessEqual(timeout, 8)
        filters = next(b['filterGroups'][0]['filters'] for m, p, b, _ in self.port.calls if p.endswith('/deals/search'))
        self.assertIn({'propertyName': 'hubspot_owner_id', 'operator': 'EQ', 'value': OWNER_ID}, filters)

    def test_H02_mx_deal_without_campaign_property_excluded_by_contact_association(self):
        self.port.tasks = [task(title='[MX27-MTY][LI1] Synthetic | LINKEDIN')]
        self.port.links[('tasks', '10')] = ['700']
        self.port.contacts['700'] = {'email': PEER}
        self.port.deals = [{'id': '900', 'properties': {'hubspot_owner_id': OWNER_ID,
            'hs_is_closed': 'false', 'dealname': 'Generic subject without campaign',
            'notes_next_activity_date': '2026-10-22T20:00:00Z'}}]
        self.port.links[('deals', '900')] = ['700']
        self.assertEqual(self.today.snapshot()['blocks']['do']['items'], [])

    def test_H05_unavailable_deal_associations_are_explicit_partial_triage(self):
        self.port.fail.add('/crm/v4/associations/deals/contacts/batch/read')
        self.port.deals = [{'id': '900', 'properties': {'hubspot_owner_id': OWNER_ID,
            'hs_is_closed': 'false', 'dealname': 'Synthetic',
            'notes_next_activity_date': '2026-10-22T20:00:00Z'}}]
        result = self.today.snapshot()
        self.assertEqual(result['blocks']['do']['items'][0]['triage_reason'], 'deal_association_partial')
        self.assertEqual(next(s for s in result['blocks']['do']['sources'] if s['id'] == 'hubspot_deals')['status'], 'partial')

    def test_H06_37_legacy_archived_and_generic_referral_retained_without_identity_hardcoding(self):
        for index in range(36):
            self.card('old-' + str(index))
        self.card('synthetic-referral', titulo='Buyer indicou financeiro',
            contexto=[{'quem': 'Buyer', 'quando': '2026-10-05 10:00', 'texto': 'Contact this colleague'}])
        before = self.state()
        result = self.today.snapshot()
        self.assertEqual(result['archive']['count'], 37)
        self.assertEqual(result['blocks']['approve']['items'], [])
        item = result['blocks']['do']['items'][0]
        self.assertEqual(item['id'], 'legacy:synthetic-referral')
        self.assertEqual(item['context'][0]['texto'], 'Contact this colleague')
        self.assertEqual(item['historical_text'], 'Historical unapproved copy')
        self.assertNotIn('draft', item)
        self.assertEqual(before, self.state())

    def test_H06_mx_legacy_history_remains_in_archive_but_never_in_today(self):
        self.card('synthetic-mx-referral', titulo='[MX27-MTY] Buyer indicou financeiro',
            contexto=[{'quem': 'Buyer', 'texto': 'Here is the referred contact'}])
        before = self.state()
        result = self.today.snapshot()
        self.assertEqual(result['archive']['count'], 1)
        self.assertEqual(result['archive']['items'][0]['id'], 'legacy:synthetic-mx-referral')
        self.assertEqual(result['blocks']['do']['items'], [])
        self.assertEqual(before, self.state())

    def test_H01_cache_60_seconds_defensive_copy_and_single_collection_under_concurrency(self):
        self.port.tasks = [task()]
        first = self.today.snapshot()
        count = len(self.port.calls)
        first['blocks']['do']['items'].clear()
        errors = []
        def fetch():
            try:
                self.assertEqual(len(self.today.snapshot()['blocks']['do']['items']), 1)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=fetch) for _ in range(5)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.port.calls), count)
        self.elapsed[0] = 60
        self.today.snapshot()
        self.assertGreater(len(self.port.calls), count)

    def test_H11_repository_readonly_enforced_even_if_writer_called_accidentally(self):
        ro = ReadonlyRepository(self.repo.path)
        with self.assertRaises(sqlite3.OperationalError):
            ro.put_state('forbidden', 'fixture', {'attempt': True})
        self.assertIsNone(self.repo.get_state('forbidden', 'fixture'))

    def handler(self, actor='ygor', method='GET', nonce=True):
        handler = object.__new__(panel.Handler)
        handler.server = SimpleNamespace(thunder_service=self.service, thunder_today_readonly=self.today,
            thunder_origin='https://panel.example.invalid', thunder_nonces={
                'fixture-nonce': (actor, time.monotonic()+60)} if nonce else {})
        handler.command, handler.client_address = method, ('127.0.0.1', 1234)
        handler.headers = {'X-Authenticated-User': actor, 'Origin': handler.server.thunder_origin,
                           'X-Thunder-Nonce': 'fixture-nonce'}
        handler.responder = lambda status, body: (status, body)
        handler.corpo = lambda: {}
        return handler

    def test_H01_endpoint_auth_ygor_get_no_query_and_legacy_today_unchanged(self):
        for actor, method, suffix, expected in [('ygor', 'GET', '', 200), ('', 'GET', '', 401),
                ('codex', 'GET', '', 403), ('ygor', 'POST', '', 405), ('ygor', 'GET', '?refresh=1', 405)]:
            handler = self.handler(actor, method)
            if expected == 200:
                status, body = handler.thunder_api(urlsplit('/api/os/v1/today-work'))
                self.assertEqual(status, 200)
                self.assertTrue(body['read_only'])
            else:
                with self.assertRaises(panel.Erro) as exc:
                    handler.thunder_api(urlsplit('/api/os/v1/today-work' + suffix))
                self.assertEqual(exc.exception.status, expected)
        handler = self.handler()
        handler.server.thunder_service = SimpleNamespace(today=lambda: {'compat_fixture': True})
        status, body = handler.thunder_api(urlsplit('/api/os/v1/today'))
        self.assertEqual(status, 200)
        self.assertTrue(body['compat_fixture'])

    def test_H04_draft_endpoint_requires_real_nonce_origin_ygor(self):
        for actor, nonce, origin, expected in [('ygor', False, None, 401),
                ('codex', True, None, 403), ('ygor', True, 'https://wrong.invalid', 401)]:
            handler = self.handler(actor, 'POST', nonce)
            if origin: handler.headers['Origin'] = origin
            with self.assertRaises(panel.Erro) as exc:
                handler.thunder_api(urlsplit('/api/os/v1/today-work/draft'))
            self.assertEqual(exc.exception.status, expected)
        self.runner.assert_not_called()


class ApprovalProjectionTests(unittest.TestCase):
    def test_H06_actual_current_sheet_identity_and_confirmation_gates(self):
        from test_thunder_os_campaign_confirmation import CampaignConfirmationTests
        fixture = CampaignConfirmationTests('runTest')
        fixture.setUp()
        try:
            with closing(sqlite3.connect(fixture.repo.path)) as con, con:
                con.execute('CREATE TABLE itens(id TEXT,dados JSON,atualizado TEXT)')
            today = TodayReadonly(fixture.service, clock=lambda: NOW)
            result = today.snapshot()
            items = result['blocks']['approve']['items']
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]['execution_confirmation'], fixture.service.campaign_execution_review(fixture.cadence))
            self.assertEqual(items[0]['action']['review_target'], {'kind': 'cadence', 'id': 'batch-1'})
            self.assertIsNone(fixture.repo.get_state('approval', 'batch-1'))
            self.assertEqual(fixture.repo.effects(), [])
            (fixture.sheet_store.root/'campaign.md').write_text('Source changed after review')
            today._expires = 0
            self.assertEqual(today.snapshot()['blocks']['approve']['items'], [])
            fixture.adapter.send.assert_not_called()
        finally:
            fixture.doCleanups()


if __name__ == '__main__':
    unittest.main()
