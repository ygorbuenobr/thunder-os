"""Receipt contract tests with isolated SQLite, synthetic messages and no sockets."""
import base64
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from thunder_os.campaign_registry import CampaignRegistry
from thunder_os.db import Repository
from thunder_os.email_readback import (EmailReadbackError, GmailReceiptReader,
                                       configured_email_readers, _readonly_request)
from thunder_os.models import ContractError, as_payload
from thunder_os.seqd_client import MAILBOX, SEQDError
from thunder_os.service import ThunderService


IDENTIFIER = 'abc123'
RECEIPT = 'gmail:'+IDENTIFIER


def message():
    # Complete normalized evidence is synthetic; this fixture does not claim
    # deployment or live provider qualification.
    return {'mailbox': MAILBOX, 'status': 'ok', 'message': {
        'id': IDENTIFIER, 'thread_id': 'def456', 'from': 'Synthetic Sender <'+MAILBOX+'>',
        'to': 'Synthetic Target <recipient@example.invalid>', 'subject': 'Synthetic subject',
        'body': 'Synthetic content\nSecond line.', 'sent': True, 'cc': '', 'bcc': '',
        'truncated': False}}


def raw_message():
    body = b'Synthetic content\nSecond line.'
    return {'mailbox': MAILBOX, 'status': 'ok', 'message': {
        'id': IDENTIFIER, 'threadId': 'def456', 'labelIds': ['SENT'], 'payload': {
            'mimeType': 'text/plain', 'headers': [
                {'name': name, 'value': value} for name, value in (
                    ('From', MAILBOX), ('To', 'recipient@example.invalid'),
                    ('Subject', 'Synthetic subject'), ('Content-Type', 'text/plain; charset=UTF-8'))],
            'body': {'size': len(body), 'data': base64.urlsafe_b64encode(body).decode().rstrip('=')}}}}


def alternative_message(content='Synthetic content\nSecond line.', suffix=None, html_suffix=None):
    row = raw_message()
    suffix = ('\n\nSynthetic Sender\n' + MAILBOX + ' <' + MAILBOX + '>') if suffix is None else suffix
    html_suffix = ('<div class="gmail_signature"><div>Synthetic Sender</div><a href="mailto:' +
                   MAILBOX + '">' + MAILBOX + '</a></div>') if html_suffix is None else html_suffix
    def leaf(kind, text):
        body = text.encode()
        return {'mimeType': kind, 'headers': [{'name': 'Content-Type', 'value': kind + '; charset=UTF-8'}],
                'body': {'size': len(body), 'data': base64.urlsafe_b64encode(body).decode().rstrip('=')}}
    payload = row['message']['payload']
    payload['mimeType'] = 'multipart/alternative'
    payload['headers'] = [h for h in payload['headers'] if h['name'] != 'Content-Type'] + [
        {'name': 'Content-Type', 'value': 'multipart/alternative; boundary="synthetic"'},
        {'name': 'Date', 'value': 'Mon, 26 Oct 2026 14:09:55 -0300'}]
    payload['body'] = {'size': 0}
    payload['parts'] = [leaf('text/plain', content + suffix),
                        leaf('text/html', '<div>Synthetic content<br>Second line.</div>' + html_suffix)]
    return row


def bridge_alternative():
    raw = alternative_message()
    payload = raw['message']['payload']
    headers = payload['headers'] + [{'name': 'Message-ID', 'value': '<synthetic@example.invalid>'}]
    values = {h['name'].lower(): h['value'] for h in headers}
    parts = [{'mime_type': p['mimeType'], 'body': base64.urlsafe_b64decode(p['body']['data'] + '===').decode(),
              'part_id': str(n)} for n, p in enumerate(payload['parts'])]
    return {'mailbox': MAILBOX, 'status': 'ok', 'message': {
        'id': IDENTIFIER, 'thread_id': 'def456', 'labelIds': ['SENT'], 'sent': True,
        'truncated': False, 'headers_complete': True, 'headers': headers,
        'from': values['from'], 'to': values['to'], 'subject': values['subject'],
        'date': values['date'], 'message_id': values['message-id'], 'cc': '', 'bcc': '',
        'body': parts[0]['body'], 'context_only': True, 'receipt_eligible': False,
        'mime_evidence': {'root_type': 'multipart/alternative', 'selected_type': 'text/plain', 'alternatives': parts}}}


class ReadClient:
    def __init__(self, response=None):
        self.response = message() if response is None else response
        self.calls = []
        self.after_read = lambda: None
        self.profile = {'mailbox': MAILBOX, 'status': 'ready'}

    def status(self):
        self.calls.append(('status',))
        return deepcopy(self.profile)

    def read(self, identifier):
        self.calls.append(('read', identifier))
        self.after_read()
        return deepcopy(self.response)


class EmailReadbackTests(unittest.TestCase):
    def setUp(self):
        for name in ('socket.socket', 'socket.create_connection'):
            guard = patch(name, side_effect=AssertionError('test network forbidden'))
            guard.start()
            self.addCleanup(guard.stop)
        self.tmp = tempfile.TemporaryDirectory(prefix='email-readback-')
        self.addCleanup(self.tmp.cleanup)
        self.repo = Repository(Path(self.tmp.name)/'workflow.db')
        self.client = ReadClient()
        self.reader = GmailReceiptReader(self.repo, self.client)
        self.registry = CampaignRegistry(self.repo)
        self.config = {'campaign_id': 'synthetic-gmail', 'campaign_version': '1',
            'rules_version': '1', 'run_id': 'synthetic-run', 'platform': 'codex_local',
            'providers': {'email': 'gmail'}, 'rules': 'Synthetic only',
            'configuration': {'flow_version': '1', 'provider_accounts': {'email': MAILBOX}}}
        self.registry.register(self.config, 'ygor', 0)
        self.step = {'id': 'internal-step', 'channel': 'email',
            'recipient': 'recipient@example.invalid', 'subject': 'Synthetic subject',
            'content': 'Synthetic content\nSecond line.'}
        self.cadence = self.repo.append_state('cadence', 'synthetic-cadence', {
            'account_id': 'example.invalid', 'person_key': 'recipient@example.invalid',
            'steps': [self.step], 'execution_context': self.registry.load('synthetic-gmail')})

    def seed(self, *, receipt=RECEIPT, state='sent', key='synthetic-effect', step=None):
        job = self.repo.enqueue_effect(key, {'cadence_id': self.cadence['id'],
                                           'step': deepcopy(self.step if step is None else step)})
        if state != 'pending':
            self.repo.finish_effect(job['id'], state, receipt, None)
        return self.repo.effect(job['id'])

    def expected(self):
        return {'target': self.step['recipient'], 'subject': self.step['subject'],
                'content': self.step['content'], 'effect_id': self.step['id']}

    def test_positive_profile_then_body_read_and_internal_binding(self):
        job = self.seed()
        for receipt in (RECEIPT, 'email:gmail:'+IDENTIFIER):
            with self.subTest(receipt=receipt):
                self.assertEqual(self.reader.readback(receipt), self.expected())
        self.assertEqual(self.client.calls, [('status',), ('read', IDENTIFIER)]*2)
        self.assertEqual(self.repo.effects(), [job])
        self.assertEqual(self.reader.provider_name, 'gmail')
        self.assertEqual(self.reader.account_id, MAILBOX)
        self.assertFalse(hasattr(self.reader, 'send'))
        self.assertFalse(hasattr(self.reader, 'reconcile'))

    def test_raw_full_message_is_observed_without_internal_job(self):
        self.client.response = raw_message()
        observed = self.reader.read_message(RECEIPT)
        self.assertEqual(observed, {'id': IDENTIFIER, 'mailbox': MAILBOX, 'from': MAILBOX,
            'to': self.step['recipient'], 'subject': self.step['subject'],
            'content': self.step['content'], 'sent': True})
        self.assertEqual(self.repo.effects(), [])
        with self.assertRaisesRegex(EmailReadbackError, 'binding_ambiguous'):
            self.reader.readback(RECEIPT)
        self.assertEqual(self.client.calls, [('status',), ('read', IDENTIFIER)])

    def test_no_visible_html_attributes_after_signature(self):
        for extra in ('<img src="https://example.invalid/banner.png" alt="UNAPPROVED commercial paragraph">',
                      '<input value="UNAPPROVED commercial paragraph">'):
            row = bridge_alternative()
            row['message']['mime_evidence']['alternatives'][1]['body'] += extra
            self.client.response = row
            with self.subTest(extra=extra), self.assertRaises(EmailReadbackError):
                self.reader.read_sent_event(RECEIPT, {'mailbox': MAILBOX, 'to': self.step['recipient'], 'subject': self.step['subject'], 'content': self.step['content']})

    def test_list_and_quote_markers_cannot_be_reflowed(self):
        for approved, observed in (('- First item\n- Second item', '-\nFirst item\n- Second item'),
                                   ('> First quote\n> Second quote', '>\nFirst quote\n> Second quote')):
            row = bridge_alternative()
            body = row['message']['body'].replace('Synthetic content\nSecond line.', observed, 1)
            row['message']['body'] = body
            row['message']['mime_evidence']['alternatives'][0]['body'] = body
            self.client.response = row
            with self.subTest(approved=approved), self.assertRaises(EmailReadbackError):
                self.reader.read_sent_event(RECEIPT, {'mailbox': MAILBOX, 'to': self.step['recipient'], 'subject': self.step['subject'], 'content': approved})

    def test_html_extra_outreach_before_signature_is_rejected(self):
        row = bridge_alternative()
        html = row['message']['mime_evidence']['alternatives'][1]
        html['body'] = '<p>UNAPPROVED commercial paragraph</p>' + html['body']
        self.client.response = row
        with self.assertRaises(EmailReadbackError):
            self.reader.read_sent_event(RECEIPT, {'mailbox': MAILBOX, 'to': self.step['recipient'], 'subject': self.step['subject'], 'content': self.step['content']})

    def test_html_body_without_signature_and_visible_elements_before_it_are_verified(self):
        expected = {'mailbox': MAILBOX, 'to': self.step['recipient'], 'subject': self.step['subject'], 'content': self.step['content']}
        for extra in ('<img alt="Unapproved banner" src="https://example.invalid/image">',
                      '<img src="https://example.invalid/image">'):
            row = bridge_alternative()
            row['message']['mime_evidence']['alternatives'][1]['body'] = extra + row['message']['mime_evidence']['alternatives'][1]['body']
            self.client.response = row
            with self.subTest(extra=extra), self.assertRaises(EmailReadbackError):
                self.reader.read_sent_event(RECEIPT, expected)
        row = alternative_message(suffix='', html_suffix='')
        self.client.response = row
        self.assertFalse(self.reader.read_sent_event(RECEIPT, expected)['signature_excluded'])
        part = row['message']['payload']['parts'][1]
        content = '<div>Different unapproved commercial message</div>'.encode()
        part['body'] = {'size': len(content), 'data': base64.urlsafe_b64encode(content).decode().rstrip('=')}
        self.client.response = row
        with self.assertRaises(EmailReadbackError):
            self.reader.read_sent_event(RECEIPT, expected)

    def test_exact_plain_cannot_bypass_failed_html_proof_during_readback(self):
        self.seed()
        row = alternative_message(suffix='', html_suffix='')
        html = b'<p>Different unapproved commercial message</p>'
        row['message']['payload']['parts'][1]['body'] = {
            'size': len(html), 'data': base64.urlsafe_b64encode(html).decode().rstrip('=')}
        self.client.response = row
        with self.assertRaises(EmailReadbackError):
            self.reader.readback(RECEIPT)

    def test_alternative_signature_preserves_original_and_binds_approved_text(self):
        self.seed()
        self.client.response = alternative_message('Synthetic content  \r\nSecond line.\t')
        observed = self.reader.read_message(RECEIPT)
        self.assertIn('Synthetic Sender', observed['content'])
        self.assertEqual(self.reader.readback(RECEIPT), self.expected())
        event = self.reader.read_sent_event(RECEIPT, {'mailbox': MAILBOX,
            'to': self.step['recipient'], 'subject': self.step['subject'], 'content': self.step['content']})
        self.assertEqual(event['text'], observed['content'], 'The original is never replaced by the approved excerpt')
        self.assertEqual(event['approved_text'], self.step['content'])
        self.assertTrue(event['signature_excluded'])
        self.assertEqual(event['content_evidence']['html_body'], observed['html_body'])
        self.assertIs(event['content_evidence']['mime_complete'], True)
        self.assertEqual(event['content_evidence']['attachment_count'], 0)

    def test_alternative_changed_prefix_or_extra_non_signature_text_stays_blocked(self):
        self.seed()
        cases = [alternative_message('Changed content\nSecond line.'),
                 alternative_message(' Synthetic content\nSecond line.'),
                 alternative_message('Synthetic  content\nSecond line.'),
                 alternative_message(suffix='\n\nExtra commercial paragraph\nSynthetic Sender\n' + MAILBOX + ' <' + MAILBOX + '>'),
                 alternative_message(html_suffix='<div class="gmail_signature">Synthetic Sender</div><div>Extra paragraph</div>'),
                 alternative_message(html_suffix='<div class="gmail_signature">Synthetic Sender</div><div class="gmail_signature">Other signature</div>'),
                 alternative_message(html_suffix='<div>Not a signature</div>'),
                 alternative_message(suffix='\n\nSynthetic Sender\nOther address')]
        for row in cases:
            with self.subTest(row=row['message']['payload']['parts'][0]['body']):
                self.client.response = row
                try:
                    observed = self.reader.readback(RECEIPT)
                except EmailReadbackError:
                    continue
                self.assertNotEqual(observed['content'], self.step['content'])

    def test_gmail_wraps_only_inside_paragraphs_and_never_changes_words(self):
        self.seed()
        self.client.response = alternative_message('Synthetic\r\ncontent\r\nSecond line.')
        self.assertEqual(self.reader.readback(RECEIPT), self.expected())
        for content in ('Synthetic content\n\nSecond line.', 'Synthetic content\nSecond Line.',
                        'Synthetic content.\nSecond line.', 'Synthetic content\n> Second line.'):
            self.client.response = alternative_message(content)
            self.assertNotEqual(self.reader.readback(RECEIPT)['content'], self.step['content'])

    def test_missing_plain_duplicate_parts_and_attachments_rejected(self):
        base = alternative_message()
        variants = []
        for parts in ([base['message']['payload']['parts'][1]],
                      base['message']['payload']['parts'] + [base['message']['payload']['parts'][0]]):
            row = deepcopy(base);row['message']['payload']['parts'] = parts;variants.append(row)
        for field, value in (('filename', 'document.pdf'), ('body', {'attachmentId': 'file', 'size': 1})):
            row = deepcopy(base);row['message']['payload']['parts'][1][field] = value;variants.append(row)
        row = deepcopy(base);row['message']['payload']['parts'][1]['headers'].append(
            {'name': 'Content-Disposition', 'value': 'attachment'});variants.append(row)
        for row in variants:
            self.client.response = row
            with self.assertRaises(EmailReadbackError):
                self.reader.read_message(RECEIPT)

    def test_complete_fresh_id_frame_requires_new_content_proof_preserves_source_scope(self):
        self.seed()
        self.client.response = bridge_alternative()
        before = deepcopy(self.client.response)
        self.assertEqual(self.reader.readback(RECEIPT), self.expected())
        event = self.reader.read_sent_event(RECEIPT, {'mailbox': MAILBOX, 'to': self.step['recipient'],
            'subject': self.step['subject'], 'content': self.step['content']})
        self.assertTrue(event['signature_excluded'])
        self.assertTrue(event['source_context_only'])
        self.assertEqual(event['content_validation'], 'approved_prefix_and_signature')
        self.assertEqual(self.client.response, before, 'Never relabel the upstream source as receipt eligible')
        wrong = {**self.step, 'content': 'Different approved text'}
        with self.assertRaises(EmailReadbackError):
            self.reader.read_sent_event(RECEIPT, {'mailbox': MAILBOX, 'to': wrong['recipient'],
                'subject': wrong['subject'], 'content': wrong['content']})

    def test_generic_context_partial_or_conflicting_mime_frames_never_become_receipts(self):
        base = bridge_alternative()
        variants = []
        for key, value in (('headers_complete', False), ('truncated', True), ('sent', False),
                           ('body', 'spliced plain text'), ('attachments', []), ('filename', 'file.pdf')):
            row = deepcopy(base);row['message'][key] = value;variants.append(row)
        row = deepcopy(base);row['context_only'] = True;row['receipt_eligible'] = False;variants.append(row)
        for parts in (base['message']['mime_evidence']['alternatives'][:1],
                      base['message']['mime_evidence']['alternatives'] * 2):
            row = deepcopy(base);row['message']['mime_evidence']['alternatives'] = parts;variants.append(row)
        row = deepcopy(base);row['message']['headers'].append({'name': 'To', 'value': 'other@example.invalid'});variants.append(row)
        row = deepcopy(base);row['message']['headers'][-1]['value'] = '<different@example.invalid>';variants.append(row)
        for row in variants:
            self.client.response = row
            with self.assertRaises(EmailReadbackError):
                self.reader.read_message(RECEIPT)

    def test_invalid_receipt_rejects_before_get(self):
        for receipt in (None, '', 'feito', 'gmail:', 'gmail:ABC123', 'gmail:../send',
                        'email:seqd:abc123', 'email:other:abc123', {'receipt': RECEIPT},
                        'gmail:'+('a'*65), RECEIPT+'\n'):
            with self.subTest(receipt=receipt), self.assertRaises(EmailReadbackError):
                self.reader.read_message(receipt)
        self.assertEqual(self.client.calls, [])

    def test_invalid_message_table(self):
        variants = []
        for field, value in (('id', 'wrong'), ('from', 'other@example.invalid'),
                ('from', MAILBOX+', other@example.invalid'), ('to', 'a@example.invalid,b@example.invalid'),
                ('to', 'Group: a@example.invalid;'), ('to', 'not-an-email'),
                ('to', 'recipient@example.invalid\r\nBcc: other@example.invalid'),
                ('cc', 'other@example.invalid'), ('bcc', 'other@example.invalid'),
                ('sent', False), ('sent', 1), ('labelIds', ['SENT', 'DRAFT']),
                ('labelIds', []), ('body', ''), ('body', None), ('body', 'a'*65537),
                ('subject', ''), ('subject', 'Subject\r\nOther'), ('truncated', True), ('truncated', None)):
            row = message();row['message'][field] = value;variants.append(row)
        for field in ('from', 'to', 'subject', 'body', 'sent', 'cc', 'bcc', 'truncated'):
            row = message();del row['message'][field];variants.append(row)
        for field, value in (('mailbox', 'other@example.invalid'), ('status', 'unavailable'), ('message', None)):
            row = message();row[field] = value;variants.append(row)
        for index, row in enumerate(variants):
            self.client.response = row
            with self.subTest(index=index), self.assertRaises(EmailReadbackError):
                self.reader.read_message(RECEIPT)

    def test_deployed_bridge_schema_fails_closed_missing_sent_cc_bcc(self):
        deployed = message()
        for field in ('sent', 'cc', 'bcc'):
            del deployed['message'][field]
        self.client.response = deployed
        with self.assertRaisesRegex(EmailReadbackError, 'sent_evidence_required'):
            self.reader.read_message(RECEIPT)

    def test_profile_invalid_prevents_body_get(self):
        for profile in (None, {}, {'mailbox': MAILBOX, 'status': 'ok'},
                        {'mailbox': 'other@example.invalid', 'status': 'ready'}):
            self.client.profile = profile;self.client.calls = []
            with self.subTest(profile=profile), self.assertRaisesRegex(EmailReadbackError, 'profile_unavailable'):
                self.reader.read_message(RECEIPT)
            self.assertEqual(self.client.calls, [('status',)])

    def test_raw_mismatch_and_incomplete_mime_rejected(self):
        variants = []
        for key, value in (('body', 'Different'), ('from', 'other@example.invalid'),
                           ('to', 'other@example.invalid'), ('subject', 'Different'), ('sent', False)):
            row = raw_message();row['message'][key] = value;variants.append(row)
        for labels in ([], ['SENT', 'DRAFT'], None):
            row = raw_message();row['message']['labelIds'] = labels;variants.append(row)
        for body in ({'size': 1, 'data': 'a'}, {'size': 99, 'data': 'YQ'},
                     {'size': 0}, {'size': 1, 'data': 'YQ', 'attachmentId': 'attachment'}):
            row = raw_message();row['message']['payload']['body'] = body;variants.append(row)
        row = raw_message();row['message']['payload']['mimeType'] = 'text/html';variants.append(row)
        row = raw_message();row['message']['payload']['headers'].append({'name': 'Bcc', 'value': 'other@example.invalid'});variants.append(row)
        row = raw_message();row['message']['payload']['headers'].append({'name': 'To', 'value': self.step['recipient']});variants.append(row)
        for index, row in enumerate(variants):
            self.client.response = row
            with self.subTest(index=index), self.assertRaises(EmailReadbackError):
                self.reader.read_message(RECEIPT)

    def test_context_only_history_never_becomes_exact_receipt(self):
        for scope in ('response', 'message'):
            for flag, value in (('context_only', True), ('receipt_eligible', False)):
                response = message()
                (response if scope == 'response' else response['message'])[flag] = value
                self.client.response = response
                with self.subTest(scope=scope, flag=flag), self.assertRaisesRegex(
                        EmailReadbackError, 'email_context_not_receipt'):
                    self.reader.read_message(RECEIPT)

    def test_raw_ambiguous_mime_headers_and_attachment_disposition_fail_closed(self):
        variants = [
            [('Content-Type', 'text/html; charset=UTF-8')],
            [('content-type', 'text/plain; charset=UTF-8'), ('Content-Type', 'text/plain; charset=UTF-8')],
            [('Content-Type', 'text/plain; charset=UTF-8; charset=iso-8859-1')],
            [('Content-Type', 'text/plain; charset=UTF-8'), ('Content-Disposition', 'attachment')],
            [('Content-Type', 'text/plain; charset=UTF-8'), ('Content-Disposition', 'inline; filename=hidden.txt')],
            [('Content-Type', 'text/plain; charset=UTF-8'), ('Content-Disposition', 'inline'), ('content-disposition', 'attachment')],
        ]
        for extra in variants:
            response = raw_message()
            payload = response['message']['payload']
            payload['headers'] = [h for h in payload['headers'] if h['name'].lower() != 'content-type']
            payload['headers'] += [{'name': name, 'value': value} for name, value in extra]
            self.client.response = response
            with self.subTest(headers=extra), self.assertRaises(EmailReadbackError):
                self.reader.read_message(RECEIPT)

    def test_raw_ascii_claim_cannot_hide_non_ascii_utf8(self):
        response = raw_message()
        body = 'Conteúdo não ASCII'.encode('utf-8')
        payload = response['message']['payload']
        payload['body'] = {'size': len(body), 'data': base64.urlsafe_b64encode(body).decode()}
        next(h for h in payload['headers'] if h['name'] == 'Content-Type')['value'] = 'text/plain; charset=us-ascii'
        self.client.response = response
        with self.assertRaises(EmailReadbackError):
            self.reader.read_message(RECEIPT)

    def test_binding_missing_wrong_state_and_ambiguous_reject_before_get(self):
        with self.assertRaises(EmailReadbackError):self.reader.readback(RECEIPT)
        self.seed(state='uncertain')
        with self.assertRaises(EmailReadbackError):self.reader.readback(RECEIPT)
        self.seed(key='second-sent')
        with self.assertRaises(EmailReadbackError):self.reader.readback(RECEIPT)
        self.assertEqual(self.client.calls, [])

    def test_binding_alias_duplicate_is_ambiguous(self):
        self.seed()
        self.seed(key='alias-job', receipt='email:gmail:'+IDENTIFIER)
        with self.assertRaisesRegex(EmailReadbackError, 'binding_ambiguous'):
            self.reader.readback(RECEIPT)
        self.assertEqual(self.client.calls, [])

    def test_binding_provider_account_channel_and_context_are_exact(self):
        self.seed()
        for kind in ('provider', 'account', 'context', 'step', 'duplicate_step', 'conflict'):
            with self.subTest(kind=kind):
                with self.repo.transaction():
                    if kind in {'provider', 'account'}:
                        config = deepcopy(self.config)
                        if kind == 'provider':config['providers']['email'] = 'seqd'
                        else:config['configuration']['provider_accounts']['email'] = 'other@example.invalid'
                        old = self.repo.get_state('campaign_config', self.config['campaign_id'])
                        self.registry.register(config, 'ygor', old['version'])
                        changed = deepcopy(self.cadence)
                        changed['execution_context'] = self.registry.load(self.config['campaign_id'])
                    else:
                        changed = deepcopy(self.cadence)
                        if kind == 'context':changed['execution_context']['run_id'] = 'other'
                        if kind == 'step':changed['steps'][0]['channel'] = 'linkedin'
                        if kind == 'duplicate_step':changed['steps'].append(deepcopy(changed['steps'][0]))
                        if kind == 'conflict':changed['steps'][0]['status'] = 'uncertain'
                    self.repo.put_state('cadence', self.cadence['id'], changed)
                    with self.assertRaises(EmailReadbackError):self.reader.readback(RECEIPT)
                    # Restore the canonical fixture using the repository's versioning.
                    old = self.repo.get_state('campaign_config', self.config['campaign_id'])
                    self.registry.register(self.config, 'ygor', old['version'])
                    self.cadence['execution_context'] = self.registry.load(self.config['campaign_id'])
                    self.repo.put_state('cadence', self.cadence['id'], self.cadence)
        self.assertEqual(self.client.calls, [])

    def test_readback_returns_observed_mismatch_never_expected_payload(self):
        self.seed()
        for field, result_field in (('to', 'target'), ('subject', 'subject'), ('body', 'content')):
            self.client.response = message()
            value = 'other@example.invalid' if field == 'to' else 'Different observed text'
            self.client.response['message'][field] = value
            with self.subTest(field=field):
                self.assertEqual(self.reader.readback(RECEIPT)[result_field], value)

    def test_fresh_job_and_cadence_changes_reject_after_get(self):
        self.seed()
        def insert_duplicate():self.seed(key='concurrent-duplicate')
        self.client.after_read = insert_duplicate
        with self.assertRaisesRegex(EmailReadbackError, 'binding_ambiguous'):
            self.reader.readback(RECEIPT)
        self.assertEqual(self.client.calls, [('status',), ('read', IDENTIFIER)])

    def test_fresh_campaign_revision_change_rejects_after_get(self):
        self.seed()
        self.client.after_read = lambda: self.registry.register(self.config, 'ygor', 1)
        with self.assertRaises(EmailReadbackError):self.reader.readback(RECEIPT)
        self.assertEqual(self.client.calls, [('status',), ('read', IDENTIFIER)])

    def test_fresh_job_state_and_cadence_changes_reject_after_get(self):
        job = self.seed()
        def change_job():
            with self.repo.transaction() as connection:
                connection.execute('UPDATE thunder_effect_jobs SET state=? WHERE id=?',
                                   ('needs_reconciliation', job['id']))
        self.client.after_read = change_job
        with self.assertRaisesRegex(EmailReadbackError, 'effect_not_sent'):
            self.reader.readback(RECEIPT)
        with self.repo.transaction() as connection:
            connection.execute('UPDATE thunder_effect_jobs SET state=? WHERE id=?', ('sent', job['id']))
        def change_cadence():
            cadence = self.repo.get_state('cadence', self.cadence['id'])
            self.repo.put_state('cadence', self.cadence['id'], cadence)
        self.client.after_read = change_cadence
        with self.assertRaisesRegex(EmailReadbackError, 'binding_changed'):
            self.reader.readback(RECEIPT)

    def test_failure_timeout_http_without_retry_or_send(self):
        for method in ('status', 'read'):
            for error in (TimeoutError('synthetic secret'), SEQDError('seqd_http_error')):
                with self.subTest(method=method, error=type(error).__name__), \
                        patch.object(self.client, method, side_effect=error) as call:
                    with self.assertRaisesRegex(EmailReadbackError, '^email_read_unavailable$'):
                        self.reader.read_message(RECEIPT)
                    call.assert_called_once()
        self.assertFalse(hasattr(self.client, 'send'))

    def test_factory_no_network_default_optin_is_lazy_independent_of_send(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch('thunder_os.email_readback.SEQDClient') as client:
            self.assertEqual(configured_email_readers(self.repo), {})
            client.assert_not_called()
        for read, send in (('true', 'false'), ('true', 'true'), ('false', 'true'), ('TRUE', 'true')):
            with self.subTest(read=read, send=send), patch.dict(os.environ, {
                    'THUNDER_OS_SEQD_READ_ENABLED': read, 'THUNDER_OS_SEQD_SEND_ENABLED': send}, clear=True), \
                    patch('thunder_os.seqd_client.credential_file', side_effect=AssertionError('credential forbidden')), \
                    patch('thunder_os.email_readback._seqd_request') as request:
                readers = configured_email_readers(self.repo)
                self.assertEqual(set(readers), {'email'} if read == 'true' else set())
                request.assert_not_called()
        self.assertEqual(configured_email_readers(self.repo, read_enabled=False, client=self.client), {})
        reader = configured_email_readers(self.repo, read_enabled=True, client=self.client)['email']
        self.assertIs(reader.client, self.client)
        self.assertEqual(self.client.calls, [])

    def test_default_client_reuses_get_only_transport_with_five_second_timeout(self):
        reader = GmailReceiptReader(self.repo)
        reader.client._loader = lambda: 'synthetic_'+'x'*40
        responses = [(200, json.dumps({'status': 'ready', 'mailbox': MAILBOX}).encode()),
                     (200, json.dumps(message()).encode())]
        with patch('thunder_os.email_readback._seqd_request', side_effect=responses) as request:
            reader.read_message(RECEIPT)
        self.assertEqual([(c.args[0], c.args[1], c.args[2], c.args[4]) for c in request.call_args_list],
                         [('GET', '/v1/status', None, 5), ('GET', '/v1/messages/'+IDENTIFIER, None, 5)])
        for method, path, body in (('POST', '/v1/send', {}), ('POST', '/v1/reconcile', {}),
                                   ('GET', '/v1/messages', None), ('GET', '/v1/status?x=1', None)):
            with self.subTest(path=path), patch('thunder_os.email_readback._seqd_request') as request:
                with self.assertRaisesRegex(EmailReadbackError, 'route_blocked'):
                    _readonly_request(method, path, body, {}, 40)
                request.assert_not_called()

    def test_default_client_http_failure_has_no_second_get(self):
        reader = GmailReceiptReader(self.repo)
        reader.client._loader = lambda: 'synthetic_'+'x'*40
        with patch('thunder_os.email_readback._seqd_request', return_value=(503, b'{}')) as request:
            with self.assertRaises(EmailReadbackError):reader.read_message(RECEIPT)
            request.assert_called_once()

    def test_service_integration_verifies_observed_and_preserves_work_on_mismatch(self):
        job = self.seed()
        service = ThunderService(self.repo, effect_readers={'email': self.reader})
        for mismatch in (True, False):
            with self.subTest(mismatch=mismatch):
                item = service.request_work({'objective': 'Verify synthetic result',
                    'context': {'effect_id': job['id'], 'cadence_id': self.cadence['id'], 'channel': 'email'}},
                    'ygor', 'synthetic-report-'+str(mismatch))
                item = service.claim(item.id, item.version, 'ygor')
                before_events = self.repo.events(item.id)
                report = {'effect_id': job['id'], 'cadence_id': self.cadence['id'], 'channel': 'email',
                    'status': 'sent', 'receipt_ref': RECEIPT, 'readback': self.expected(),
                    'evidence': [{'source': 'synthetic', 'claim': 'synthetic only'}]}
                self.client.response = message()
                if mismatch:self.client.response['message']['body'] = 'Different observed text'
                if mismatch:
                    with self.assertRaisesRegex(ContractError, 'readback mismatch'):
                        service.record_job_result(item.id, item.version, report, 'ygor')
                    self.assertEqual(as_payload(self.repo.get_work(item.id)), as_payload(item))
                    self.assertEqual(self.repo.events(item.id), before_events)
                else:
                    stored = service.record_job_result(item.id, item.version, report, 'ygor')
                    self.assertEqual(stored.status.value, 'succeeded')
                    self.assertEqual(stored.result['verification']['scope'], 'message_in_sender_account_not_delivery')
                self.assertEqual(self.repo.effects(), [job])

    def test_additional_mime_content_cannot_promote_work_or_append_success(self):
        job = self.seed()
        service = ThunderService(self.repo, effect_readers={'email': self.reader})
        for mime in ('multipart/mixed', 'multipart/alternative', 'text/plain'):
            for missing in (False, True):
                with self.subTest(mime=mime, missing_html_data=missing):
                    response = raw_message()
                    plain = deepcopy(response['message']['payload'])
                    extra = {'mimeType': 'text/html', 'body': {'size': 88}}
                    if not missing:
                        extra['body']['data'] = base64.urlsafe_b64encode(b'x'*88).decode()
                    response['message']['payload'] = {
                        **plain, 'mimeType': mime, 'parts': [plain, extra]}
                    self.client.response = response
                    item = service.request_work({'objective': 'Verify complete synthetic message',
                        'context': {'effect_id': job['id'], 'cadence_id': self.cadence['id'],
                                    'channel': 'email'}}, 'ygor', 'mime-'+mime+str(missing))
                    item = service.claim(item.id, item.version, 'ygor')
                    before_events = self.repo.events(item.id)
                    report = {'effect_id': job['id'], 'cadence_id': self.cadence['id'],
                        'channel': 'email', 'status': 'sent', 'receipt_ref': RECEIPT,
                        'readback': self.expected(), 'evidence': [{'source': 'synthetic'}]}
                    with self.assertRaisesRegex(ContractError, 'readback unavailable'):
                        service.record_job_result(item.id, item.version, report, 'ygor')
                    self.assertEqual(as_payload(self.repo.get_work(item.id)), as_payload(item))
                    self.assertEqual(self.repo.events(item.id), before_events)
                    self.assertEqual(self.repo.effects(), [job])

    def test_panel_main_real_assembly_is_lazy_and_read_flag_is_independent_of_send(self):
        import painel_servidor as panel
        from thunder_os.instantly_reader import InstantlyReader
        from thunder_os.panel_verification import HermesVerifyProcess
        for read_enabled, send_enabled in (('true', 'false'), ('false', 'true')):
            with self.subTest(read_enabled=read_enabled, send_enabled=send_enabled):
                server = SimpleNamespace(serve_forever=Mock())
                root = Path(self.tmp.name)/('panel-'+read_enabled);root.mkdir()
                brain, state = root/'brain', root/'state'
                brain.mkdir();state.mkdir()
                with patch.dict(os.environ, {'PAINEL_DB': str(state/'panel.db'),
                        'THUNDER_OS_SEQD_READ_ENABLED': read_enabled,
                        'THUNDER_OS_SEQD_SEND_ENABLED': send_enabled,
                        'THUNDER_EXTERNAL_EFFECTS_ENABLED': 'false'}, clear=True), \
                        patch.object(panel, 'RAIZ', brain), \
                        patch.object(panel, 'ThreadingHTTPServer', return_value=server), \
                        patch.object(panel, 'configure_connectsafely_server'), \
                        patch.object(InstantlyReader, 'start') as start, \
                        patch.object(InstantlyReader, 'stop'), \
                        patch.object(HermesVerifyProcess, 'configured') as hermes, \
                        patch('thunder_os.email_readback._seqd_request') as request, \
                        patch('thunder_os.seqd_client.credential_file', side_effect=AssertionError('credential forbidden')):
                    self.assertEqual(panel.main(['servir']), 0)
                    readers = server.thunder_service._effect_readers
                    self.assertEqual(set(readers), {'email'} if read_enabled == 'true' else set())
                    if readers:
                        self.assertIsInstance(readers['email'], GmailReceiptReader)
                        self.assertIs(readers['email'].repository, server.thunder_service.repository)
                    request.assert_not_called();hermes.assert_not_called();start.assert_called_once()
                    server.serve_forever.assert_called_once_with()

    def test_consumer_main_real_assembly_is_lazy_without_loop_or_reporter(self):
        from thunder_os import continuous_consumer as consumer_module
        for enabled in (None, 'true'):
            with self.subTest(enabled=enabled):
                root = Path(self.tmp.name)/('consumer-'+str(enabled));root.mkdir()
                argv = ['--mode', 'operation']
                for option in ('db', 'brain', 'bundles', 'hermes-root', 'profile', 'python', 'runtime-root', 'health'):
                    argv += ['--'+option, str(root/option)]
                environment = {'THUNDER_EXTERNAL_EFFECTS_ENABLED': 'false',
                               'THUNDER_OS_EXTERNAL_EFFECTS_ENABLED': 'false'}
                if enabled is not None:environment['THUNDER_OS_SEQD_READ_ENABLED'] = enabled
                stop = Mock();stop.is_set.return_value = True
                with patch.dict(os.environ, environment, clear=True), \
                        patch.object(consumer_module.threading, 'Event', return_value=stop), \
                        patch.object(consumer_module.threading, 'Thread') as thread, \
                        patch.object(consumer_module.signal, 'signal'), \
                        patch.object(consumer_module, 'atomic_json') as health, \
                        patch.object(consumer_module, 'source_revision', return_value='synthetic'), \
                        patch.object(consumer_module, 'KnowledgeStore'), \
                        patch.object(consumer_module, 'TrustedEvidence'), \
                        patch.object(consumer_module, 'process_factory'), \
                        patch.object(consumer_module, 'ContinuousConsumer') as consumer, \
                        patch('thunder_os.email_readback._seqd_request') as request, \
                        patch('thunder_os.seqd_client.credential_file', side_effect=AssertionError('credential forbidden')):
                    self.assertEqual(consumer_module.main(argv), 0)
                    service = consumer.call_args.args[0]
                    self.assertEqual(set(service._effect_readers), {'email'} if enabled == 'true' else set())
                    if enabled == 'true':
                        self.assertIsInstance(service._effect_readers['email'], GmailReceiptReader)
                        self.assertIs(service._effect_readers['email'].repository, service.repository)
                    consumer.return_value.run_once.assert_not_called()
                    thread.return_value.start.assert_called_once()
                    thread.return_value.join.assert_called_once_with(timeout=6)
                    health.assert_called_once();request.assert_not_called()



class OneShotEvidenceTests(unittest.TestCase):
    def expected(self):
        return {'mailbox':MAILBOX,'to':'recipient@example.invalid',
                'subject':'Synthetic subject','content':'Synthetic content\nSecond line.'}
    def test_provider_date_and_exact_reviewed_content(self):
        row=message();row['message']['date']='Sun, 25 Oct 2026 10:00:00 -0300'
        result=GmailReceiptReader(Mock(),ReadClient(row)).read_sent_event(RECEIPT,self.expected())
        self.assertEqual(result['occurred_at'],'2026-10-25T13:00:00.000Z')
        self.assertEqual(result['scope'],'sent_copy_not_delivery');self.assertEqual(result['external_writes'],0)
    def test_plain_crlf_original_is_preserved_and_review_accepts_lf_equivalence(self):
        response = raw_message()
        body = b'Synthetic content\r\nSecond line.'
        response['message']['payload']['body'] = {
            'size': len(body), 'data': base64.urlsafe_b64encode(body).decode().rstrip('=')}
        response['message']['payload']['headers'].append({
            'name': 'Date', 'value': 'Sun, 25 Oct 2026 13:00:00 +0000'})
        reader = GmailReceiptReader(Mock(), ReadClient(response))
        self.assertEqual(reader.read_message(RECEIPT)['content'], body.decode())
        normalized = reader.read_sent_event(RECEIPT, self.expected())
        self.assertEqual(normalized['text'], body.decode())
        self.assertEqual(normalized['approved_text'], self.expected()['content'])
        exact = dict(self.expected(), content=body.decode())
        event = reader.read_sent_event(RECEIPT, exact)
        self.assertEqual(event['text'], body.decode())
        self.assertEqual(event['scope'], 'sent_copy_not_delivery')
        self.assertEqual(event['external_writes'], 0)

    def test_historical_html_with_visible_matching_text_is_not_exact_mime_proof(self):
        for mime in ('text/html', 'multipart/alternative'):
            response = raw_message()
            plain = deepcopy(response['message']['payload'])
            html = '<p>Synthetic content<br>Second line.</p>'.encode()
            response['message']['payload'] = {
                **plain, 'mimeType': mime,
                'body': {'size': len(html), 'data': base64.urlsafe_b64encode(html).decode()},
                **({'parts': [plain]} if mime.startswith('multipart') else {})}
            with self.subTest(mime=mime), self.assertRaisesRegex(EmailReadbackError, 'email_mime_unsupported'):
                GmailReceiptReader(Mock(), ReadClient(response)).read_sent_event(RECEIPT, self.expected())

    def test_missing_naive_or_invalid_date_is_not_collection_time(self):
        for date in (None,'bad','Sun, 25 Oct 2026 10:00:00 -0000'):
            row=message();row['message']['date']=date
            with self.assertRaises(EmailReadbackError):
                GmailReceiptReader(Mock(),ReadClient(row)).read_sent_event(RECEIPT,self.expected())
    def test_review_mismatch_and_no_fabricated_campaign(self):
        row=message();row['message']['date']='Sun, 25 Oct 2026 13:00:00 +0000'
        expected=self.expected();expected['to']='other@example.invalid'
        with self.assertRaises(EmailReadbackError):GmailReceiptReader(Mock(),ReadClient(row)).read_sent_event(RECEIPT,expected)
        result=GmailReceiptReader(Mock(),ReadClient(row)).read_sent_event(RECEIPT,self.expected())
        self.assertNotIn('campaign_id',result['source'])
    def test_raw_date_header_supported(self):
        row=raw_message();row['message']['payload']['headers'].append({'name':'Date','value':'Sun, 25 Oct 2026 13:00:00 +0000'})
        result=GmailReceiptReader(Mock(),ReadClient(row)).read_sent_event(RECEIPT,self.expected())
        self.assertEqual(result['occurred_at'],'2026-10-25T13:00:00.000Z')

if __name__ == '__main__':
    unittest.main()
