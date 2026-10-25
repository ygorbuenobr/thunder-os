"""Read-only SEQD receipt evidence; no send, reconciliation or OAuth fallback.

A complete raw Gmail full message or an explicitly complete normalized message
is required. Historical HTML/thread context is not receipt evidence. Missing or
contradictory MIME evidence fails closed; reviewed line endings remain exact.
"""
import base64
from copy import deepcopy
from email import policy
from email.parser import HeaderParser
from email.utils import parsedate_to_datetime
from datetime import timezone
import os
import re

from .campaign_registry import CampaignRegistry
from .seqd_client import MAILBOX, SEQDClient, _request as _seqd_request


class EmailReadbackError(ValueError):
    pass


def _readonly_request(method, path, body, headers, timeout):
    _require(method == 'GET' and body is None and
             (path == '/v1/status' or re.fullmatch(r'/v1/(?:messages|threads)/[a-f0-9]{1,64}', path)),
             'email_route_blocked')
    return _seqd_request(method, path, None, headers, min(timeout, 5))


def _require(value, code='email_message_invalid'):
    if not value:
        raise EmailReadbackError(code)


def _receipt_id(receipt):
    match = re.fullmatch(r'(?:gmail:|email:gmail:)([a-f0-9]{1,64})', receipt) if isinstance(receipt, str) else None
    _require(match, 'email_receipt_invalid')
    return match.group(1)


def _text(value, limit):
    _require(isinstance(value, str) and bool(value.strip()) and len(value.encode('utf-8')) <= limit)
    _require(not any(ord(c) < 32 and c not in '\n\r\t' for c in value))
    return value


def _address(value):
    _text(value, 2048)
    _require(not any(ord(c) < 32 or ord(c) == 127 for c in value))
    header = HeaderParser(policy=policy.default).parsestr('To: '+value+'\n\n')['To']
    _require(not header.defects and len(header.addresses) == 1)
    _require(all(group.display_name is None for group in header.groups))
    address = header.addresses[0]
    _require(address.username and address.domain and '.' in address.domain)
    return address.addr_spec.lower()


def _raw_message(message):
    labels = message.get('labelIds')
    _require(isinstance(labels, list) and all(isinstance(x, str) for x in labels)
             and 'SENT' in labels and 'DRAFT' not in labels, 'email_sent_evidence_required')
    payload = message.get('payload')
    _require(isinstance(payload, dict))
    # A plain alternative does not prove the complete MIME message's contents.
    _require(payload.get('mimeType') == 'text/plain'
             and payload.get('parts') in (None, []), 'email_mime_unsupported')
    headers = payload.get('headers')
    _require(isinstance(headers, list) and len(headers) <= 500)
    values = {}
    for header in headers:
        _require(isinstance(header, dict) and isinstance(header.get('name'), str)
                 and isinstance(header.get('value'), str))
        name, value = header['name'].lower(), header['value']
        if name in {'from', 'to', 'subject', 'cc', 'bcc', 'date', 'message-id',
                    'content-type', 'content-disposition', 'content-transfer-encoding'}:
            _require(name not in values and not any(ord(c) < 32 or ord(c) == 127 for c in value))
            values[name] = value
    _require(not values.get('cc') and not values.get('bcc'), 'email_multiple_recipients')
    if 'subject' in values:
        subject = HeaderParser(policy=policy.default).parsestr('Subject: '+values['subject']+'\n\n')['Subject']
        _require(not subject.defects)
        values['subject'] = str(subject)
    disposition = values.get('content-disposition', '')
    _require(not re.match(r'attachment\b', disposition, re.I)
             and not re.search(r'(?:^|;)\s*filename\s*=', disposition, re.I), 'email_mime_unsupported')
    parts, decoded, nodes = [], 0, 0

    def visit(part, depth):
        nonlocal decoded, nodes
        nodes += 1
        _require(isinstance(part, dict) and nodes <= 64 and depth <= 8)
        body = part.get('body', {})
        _require(isinstance(body, dict) and not part.get('filename') and not body.get('attachmentId'))
        mime = part.get('mimeType')
        if mime == 'text/plain':
            part_headers = part.get('headers', [])
            _require(isinstance(part_headers, list))
            ascii_charset = False
            for header in part_headers:
                _require(isinstance(header, dict) and isinstance(header.get('name'), str)
                         and isinstance(header.get('value'), str))
                if header['name'].lower() == 'content-type':
                    _require(re.match(r'^text/plain(?:\s*;|$)', header['value'], re.I)
                             and len(re.findall(r'(?:^|;)\s*charset\s*=', header['value'], re.I)) <= 1,
                             'email_mime_unsupported')
                    charset = re.search(r'charset\s*=\s*["\']?([^;\s"\']+)', header['value'], re.I)
                    _require(charset is None or charset.group(1).lower() in {'utf-8', 'us-ascii'})
                    ascii_charset = charset is not None and charset.group(1).lower() == 'us-ascii'
            data = body.get('data')
            _require(isinstance(data, str) and len(data) <= 90000
                     and re.fullmatch(r'[A-Za-z0-9_-]*={0,2}', data) is not None and len(data) % 4 != 1)
            raw = base64.b64decode(data+'='*((-len(data)) % 4), altchars=b'-_', validate=True)
            _require(not ascii_charset or raw.isascii(), 'email_mime_unsupported')
            _require(type(body.get('size')) is int and body['size'] == len(raw))
            decoded += len(raw)
            _require(decoded <= 65536)
            parts.append(raw.decode('utf-8', errors='strict'))

    visit(payload, 0)
    _require(len(parts) == 1)
    return {'from': values.get('from'), 'to': values.get('to'), 'subject': values.get('subject'),
            'body': ''.join(parts), 'sent': True, 'cc': '', 'bcc': '', 'truncated': False, 'date': values.get('date'), 'message_id':values.get('message-id')}


class GmailReceiptReader:
    provider_name = 'gmail'
    account_id = MAILBOX

    def __init__(self, repository, client=None):
        self.repository = repository
        self.client = client if client is not None else SEQDClient(request=_readonly_request)

    def read_message(self, receipt_ref, *, reply_metadata=False):
        """Observe one message without requiring or creating an internal job."""
        identifier = _receipt_id(receipt_ref)
        try:
            profile = self.client.status()
            _require(isinstance(profile, dict) and profile.get('mailbox') == MAILBOX
                     and profile.get('status') == 'ready', 'email_profile_unavailable')
            response = self.client.read(identifier)
            _require(isinstance(response, dict) and response.get('mailbox') == MAILBOX
                     and response.get('status') == 'ok', 'email_account_mismatch')
            message = response.get('message')
            _require(isinstance(message, dict) and message.get('id') == identifier, 'email_message_id_mismatch')
            for scope in (response, message):
                _require(scope.get('context_only', False) is False
                         and scope.get('receipt_eligible', True) is True, 'email_context_not_receipt')
            _require(not message.get('truncated'), 'email_body_incomplete')
            if 'payload' in message:
                observed = _raw_message(message)
                # If both schemas are supplied they must describe the same message.
                for field in ('from', 'to', 'subject', 'body', 'sent', 'cc', 'bcc', 'truncated', 'date', 'message_id'):
                    _require(field not in message or message[field] == observed[field], 'email_raw_mismatch')
            else:
                observed = message
                _require(message.get('truncated') is False, 'email_body_incomplete')
                _require(message.get('sent') is True, 'email_sent_evidence_required')
                _require(message.get('cc') == '' and message.get('bcc') == '', 'email_recipient_evidence_required')
                if 'labelIds' in message:
                    _require(isinstance(message['labelIds'], list) and 'SENT' in message['labelIds']
                             and 'DRAFT' not in message['labelIds'], 'email_sent_evidence_required')
            sender, target = _address(observed.get('from')), _address(observed.get('to'))
            _require(sender == MAILBOX, 'email_sender_mismatch')
            subject = _text(observed.get('subject'), 2048)
            _require(not any(ord(c) < 32 or ord(c) == 127 for c in subject))
            metadata = {}
            if reply_metadata:
                headers = message.get('payload', {}).get('headers') if 'payload' in message else message.get('headers')
                _require('payload' in message or message.get('headers_complete') is True, 'email_reply_headers_incomplete')
                _require(isinstance(headers,list) and len(headers)<=500,'email_reply_headers_incomplete')
                selected = {}
                for item in headers:
                    _require(isinstance(item,dict) and isinstance(item.get('name'),str) and isinstance(item.get('value'),str),'email_reply_headers_invalid')
                    name,value=item['name'].lower(),item['value']
                    if name in {'message-id','in-reply-to','references'}:
                        _require(name not in selected and len(value)<=8192 and not any(ord(c)<32 or ord(c)==127 for c in value),'email_reply_headers_ambiguous')
                        selected[name]=value
                mid=selected.get('message-id','')
                _require(observed.get('message_id') in (None,mid),'email_reply_headers_mismatch')
                _require(re.fullmatch(r'<[^<>\s]{1,500}>',mid),'email_reply_message_id_invalid')
                thread=message.get('thread_id',message.get('threadId'))
                _require(isinstance(thread,str) and re.fullmatch(r'[a-f0-9]{1,64}',thread),'email_reply_thread_invalid')
                metadata={'source_thread_id':thread,'source_message_id':mid,
                          'source_in_reply_to':selected.get('in-reply-to',''),'source_references':selected.get('references','')}
            return {'id': identifier, 'mailbox': response['mailbox'], 'from': sender, 'to': target,
                    'subject': subject, **metadata,
                    'content': _text(observed.get('body'), 65536), 'sent': True,
                    **({'source_message_id':observed['message_id']} if observed.get('message_id') is not None else {}),
                    **({'source_date':observed['date']} if observed.get('date') is not None else {})}
        except EmailReadbackError:
            raise
        except Exception:
            raise EmailReadbackError('email_read_unavailable') from None

    def read_sent_event(self, receipt_ref, expected):
        """Prepare evidence for an exact reviewed one-shot; never create CRM data.

        Date comes from the provider's sent message, never the collector clock.
        This proves a sent copy in the mailbox, not delivery or a human reply.
        No cadence/effect is fabricated for a SEQD one-shot receipt.
        """
        _require(isinstance(expected, dict) and set(expected) == {'mailbox','to','subject','content'},
                 'email_expected_scope_invalid')
        _require(expected.get('mailbox') == MAILBOX, 'email_account_mismatch')
        observed = self.read_message(receipt_ref)
        _require(all(observed.get(key) == expected[key] for key in expected), 'email_reviewed_content_mismatch')
        try:
            raw = observed.get('source_date')
            _require(isinstance(raw,str) and 0 < len(raw) <= 128
                     and not any(ord(c)<32 or ord(c)==127 for c in raw), 'email_source_time_missing')
            when = parsedate_to_datetime(raw)
            _require(when.tzinfo is not None, 'email_source_time_unverified')
            occurred = when.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
        except EmailReadbackError:
            raise
        except Exception:
            raise EmailReadbackError('email_source_time_unverified') from None
        return {'event_type':'email_sent','source':{'provider':'gmail','account_id':MAILBOX,
                'provider_id':observed['id']}, 'lead_email':observed['to'],
                'subject':observed['subject'], 'text':observed['content'], 'occurred_at':occurred,
                'time_source':'provider_message_date_header', 'receipt_ref':receipt_ref,
                'scope':'sent_copy_not_delivery', 'read_only':True, 'external_writes':0}

    def read_bound_sent_event(self, receipt_ref):
        before=self._binding(receipt_ref);job=before[0];step=job['payload']['step']
        assisted=self.repository.get_state('codex_assisted',job['id']) or {}
        contract=assisted.get('ticket',{}).get('reply_contract')
        content=step['content']
        if contract:
            _require(assisted.get('phase')=='confirmed','email_followup_not_confirmed')
            content=verify_followup(self.read_message(receipt_ref,reply_metadata=True),contract,step['content'])
        event=self.read_sent_event(receipt_ref,{'mailbox':MAILBOX,'to':step['recipient'],'subject':step['subject'],'content':content})
        _require(self._binding(receipt_ref)==before,'email_binding_changed')
        if contract:event.update(approved_new_text=step['content'],quote_source_id=contract['message_id'])
        return event

    def _binding(self, receipt_ref):
        identifier = _receipt_id(receipt_ref)
        aliases = {'gmail:'+identifier, 'email:gmail:'+identifier}
        matches = [job for job in self.repository.effects() if job.get('receipt_ref') in aliases]
        _require(len(matches) == 1, 'email_receipt_binding_ambiguous')
        job = matches[0]
        _require(job.get('state') == 'sent', 'email_effect_not_sent')
        step = job['payload']['step']
        _require(isinstance(step.get('id'), str) and bool(step['id'])
                 and step.get('channel') == 'email', 'email_effect_binding_invalid')
        cadence = self.repository.get_state('cadence', job['payload']['cadence_id'])
        _require(isinstance(cadence, dict), 'email_cadence_binding_invalid')
        steps = [row for row in cadence['steps'] if row.get('id') == step['id']]
        _require(len(steps) == 1 and steps[0].get('status') not in {'failed', 'uncertain', 'needs_reconciliation'}
                 and all(steps[0].get(key) == step.get(key) for key in ('channel', 'recipient', 'subject', 'content')),
                 'email_step_binding_invalid')
        registry = CampaignRegistry(self.repository)
        context = registry.load(cadence['execution_context']['campaign_id'])
        binding = registry.binding(context['campaign_id'])
        _require(context == cadence['execution_context']
                 and binding['providers'].get('email') == self.provider_name
                 and binding['provider_accounts'].get('email') == self.account_id, 'email_campaign_binding_invalid')
        return deepcopy((job, cadence, context, binding))

    def readback(self, receipt_ref):
        """Bind observed content to a unique local sent job, never a provider effect ID.

        effect_id is the job payload's internal step ID, as required by the
        service readback protocol. Gmail does not supply this identifier.
        """
        try:
            before = self._binding(receipt_ref)
            message = self.read_message(receipt_ref)
            assisted=self.repository.get_state('codex_assisted',before[0]['id']) or {}
            if assisted.get('ticket',{}).get('reply_contract'):
                event=self.read_bound_sent_event(receipt_ref)
                message['content']=event['approved_new_text']
            _require(self._binding(receipt_ref) == before, 'email_binding_changed')
            step = before[0]['payload']['step']
            return {'target': message['to'], 'subject': message['subject'],
                    'content': message['content'], 'effect_id': step['id']}
        except EmailReadbackError:
            raise
        except Exception:
            raise EmailReadbackError('email_binding_unavailable') from None


def configured_email_readers(repository, *, read_enabled=None, client=None):
    if read_enabled is None:
        read_enabled = os.environ.get('THUNDER_OS_SEQD_READ_ENABLED') == 'true'
    return {'email': GmailReceiptReader(repository, client)} if read_enabled is True else {}


def followup_body(contract,new_text):
    _require(isinstance(contract,dict) and contract.get('version')==1,'email_followup_contract_invalid')
    return new_text+'\n\nOn '+contract['source_date']+', '+MAILBOX+' wrote:\n'+'\n'.join('> '+line for line in contract['original_text'].split('\n'))


def verify_followup(message,contract,new_text):
    expected=followup_body(contract,new_text)
    _require(message.get('source_thread_id')==contract['thread_id']
        and message.get('source_in_reply_to')==contract['rfc_message_id']
        and message.get('source_references')==contract['rfc_message_id'], 'email_followup_thread_mismatch')
    _require(message.get('content')==expected,'email_followup_quote_mismatch')
    return expected
