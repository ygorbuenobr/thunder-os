"""Read-only SEQD receipt evidence; no send, reconciliation or OAuth fallback.

The deployed bridge's normalized message omits SENT and Cc/Bcc evidence and
therefore cannot satisfy this contract. A complete raw Gmail full message or an
explicitly complete normalized message is required; missing evidence fails closed.
"""
import base64
from copy import deepcopy
from email import policy
from email.parser import HeaderParser
import os
import re

from .campaign_registry import CampaignRegistry
from .seqd_client import MAILBOX, SEQDClient, _request as _seqd_request


class EmailReadbackError(ValueError):
    pass


def _readonly_request(method, path, body, headers, timeout):
    _require(method == 'GET' and body is None and
             (path == '/v1/status' or re.fullmatch(r'/v1/messages/[a-f0-9]{1,64}', path)),
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
        if name in {'from', 'to', 'subject', 'cc', 'bcc'}:
            _require(name not in values and not any(ord(c) < 32 or ord(c) == 127 for c in value))
            values[name] = value
    _require(not values.get('cc') and not values.get('bcc'), 'email_multiple_recipients')
    if 'subject' in values:
        subject = HeaderParser(policy=policy.default).parsestr('Subject: '+values['subject']+'\n\n')['Subject']
        _require(not subject.defects)
        values['subject'] = str(subject)
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
            for header in part_headers:
                _require(isinstance(header, dict) and isinstance(header.get('name'), str)
                         and isinstance(header.get('value'), str))
                if header['name'].lower() == 'content-type':
                    charset = re.search(r'charset\s*=\s*["\']?([^;\s"\']+)', header['value'], re.I)
                    _require(charset is None or charset.group(1).lower() in {'utf-8', 'us-ascii'})
            data = body.get('data')
            _require(isinstance(data, str) and len(data) <= 90000
                     and re.fullmatch(r'[A-Za-z0-9_-]*={0,2}', data) is not None and len(data) % 4 != 1)
            raw = base64.b64decode(data+'='*((-len(data)) % 4), altchars=b'-_', validate=True)
            _require(type(body.get('size')) is int and body['size'] == len(raw))
            decoded += len(raw)
            _require(decoded <= 65536)
            parts.append(raw.decode('utf-8', errors='strict'))

    visit(payload, 0)
    _require(len(parts) == 1)
    return {'from': values.get('from'), 'to': values.get('to'), 'subject': values.get('subject'),
            'body': ''.join(parts), 'sent': True, 'cc': '', 'bcc': '', 'truncated': False}


class GmailReceiptReader:
    provider_name = 'gmail'
    account_id = MAILBOX

    def __init__(self, repository, client=None):
        self.repository = repository
        self.client = client if client is not None else SEQDClient(request=_readonly_request)

    def read_message(self, receipt_ref):
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
            _require(not message.get('truncated'), 'email_body_incomplete')
            if 'payload' in message:
                observed = _raw_message(message)
                # If both schemas are supplied they must describe the same message.
                for field in ('from', 'to', 'subject', 'body', 'sent', 'cc', 'bcc', 'truncated'):
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
            return {'id': identifier, 'mailbox': response['mailbox'], 'from': sender, 'to': target,
                    'subject': subject,
                    'content': _text(observed.get('body'), 65536), 'sent': True}
        except EmailReadbackError:
            raise
        except Exception:
            raise EmailReadbackError('email_read_unavailable') from None

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
