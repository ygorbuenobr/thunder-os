"""Read historical task evidence without authorizing or performing any effect.

The sole legacy alias below is an explicit batch mapping, not a prefix rule.
CRM confirmations are recorded evidence, not live Gmail reads or delivery proof.
"""
from datetime import datetime
from html.parser import HTMLParser
import json
import re
from .seqd_client import MAILBOX
from .mexico_receipt import MARKER, validate_receipt

BATCH_CAMPAIGNS = {'MX27-CDMX-D0-20261021': 'MX27-CDMX'}
PIPE_TITLE = re.compile(r'^([A-Za-z0-9_-]{1,100})\s*\|\s*([EL][1-9][0-9]?)\s*\|\s*([^|\r\n]+)\s*\|\s*([^|\r\n]+)$')
DIRECT = re.compile(r'E1 CONFIRMADO Gmail ([a-f0-9]{6,64}) em ([^;]+); destinatario ([^;\s]+); SHA256 ([a-f0-9]{64})\.')
RECONCILED = re.compile(r'E1 CONFIRMADO POR RECONCILIACAO SOMENTE LEITURA: Gmail ([a-f0-9]{6,64}); destinatario ([^;\s]+); SHA256 ([a-f0-9]{64})\. Nenhum reenvio\.')
RESULT = re.compile(r'RESULTADO E1 [0-9]{2}/[0-9]{2}/[0-9]{4}: (.+)', re.S)


class _Blocks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self.current, self.parts, self.hidden = [], None, [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'iframe', 'object'}:
            self.hidden += 1
        elif tag in {'pre', 'p'} and not self.hidden:
            self.current, self.parts = tag, []

    def handle_data(self, data):
        if self.current and not self.hidden:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'iframe', 'object'}:
            self.hidden = max(0, self.hidden - 1)
        elif tag == self.current and not self.hidden:
            self.blocks.append((tag, ''.join(self.parts).strip()))
            self.current, self.parts = None, []


def pipe_title(title):
    match = PIPE_TITLE.fullmatch(title)
    if not match or match[1] not in BATCH_CAMPAIGNS:
        return None
    return {'batch': match[1], 'campaign': BATCH_CAMPAIGNS[match[1]],
            'touch': match[2], 'company': match[4].strip(),
            'channel': 'email' if match[2].startswith('E') else 'linkedin'}


def task_evidence(raw_body, task, pipe):
    result = {'outcome': 'completed_without_receipt' if task['completed'] else 'pending',
              'receipt': None, 'reason': None, 'reason_code': None,
              'retry_allowed': False, 'evidence_scope': 'hubspot_task_body'}
    if not pipe or not isinstance(raw_body, str):
        return result
    parser = _Blocks()
    parser.feed(raw_body)
    metadata = []
    for tag, text in parser.blocks:
        if tag != 'pre':
            continue
        try:
            value = json.loads(text)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict) and value.get('marker') != MARKER and value.get('batch') == pipe['batch']:
            metadata.append(value)
    if len(metadata) != 1:
        return result
    meta = metadata[0]
    cid = meta.get('contact_id')
    if (any(not isinstance(meta.get(key), str) or not meta[key] for key in
            ('contact_id', 'campaign', 'batch', 'task_key', 'provider', 'account', 'recipient', 'sha256_subject_newline_text'))
            or not re.fullmatch(r'[1-9][0-9]{0,29}', cid)
            or meta.get('campaign') != 'Mexico2027-networking'
            or meta.get('task_key') != f"{pipe['batch']}:{cid}:{task['touch']}"
            or meta.get('provider') != 'Gmail'
            or meta.get('account') != MAILBOX
            or not re.fullmatch(r'[^\s@;]+@[^\s@;]+\.[^\s@;]+', meta['recipient'])
            or not re.fullmatch(r'[a-f0-9]{64}', meta['sha256_subject_newline_text'])):
        return result
    # Process outcome paragraphs in document order: a later reconciliation wins;
    # a later uncertain result must not silently inherit an earlier confirmation.
    for tag, text in parser.blocks:
        if tag == 'pre':
            try:
                receipt = json.loads(text)
            except (ValueError, TypeError):
                continue
            if not isinstance(receipt, dict) or receipt.get('marker') != MARKER:
                continue
            try:
                validate_receipt(receipt, {'campaign_id': pipe['campaign'], 'batch': pipe['batch'],
                    'hubspot_task_id': task['id'], 'contact_id': cid, 'touch': task['touch'],
                    'sender': MAILBOX, 'recipient': meta['recipient'],
                    'payload_sha256': meta['sha256_subject_newline_text']})
                if (not task['completed'] or task['link_status'] != 'linked' or task['contact_ids'] != [cid]):
                    raise ValueError('RECEIPT_CONTACT_LINK_UNVERIFIED')
            except ValueError:
                result.update(outcome='unverified', receipt=None, reason_code='RECEIPT_BINDING_MISMATCH')
                continue
            result.update(outcome='sent_with_receipt', reason=None, reason_code=None,
                receipt={'provider': 'gmail', 'message_id': receipt['message_id'], 'task_id': task['id'],
                    'contact_id': cid, 'batch': pipe['batch'], 'campaign_id': pipe['campaign'],
                    'touch': task['touch'], 'payload_sha256': receipt['payload_sha256'],
                    'confirmed_at': receipt['confirmed_at'], 'verification': 'recorded_confirmation',
                    'source': 'hubspot_task_body', 'reconciled': True, 'key': receipt['key']})
            continue
        if tag != 'p':
            continue
        if task['touch'] != 'E1':
            continue
        direct, reconciled, outcome = DIRECT.fullmatch(text), RECONCILED.fullmatch(text), RESULT.fullmatch(text)
        if direct or reconciled:
            if task['link_status'] != 'linked' or task['contact_ids'] != [cid]:
                result.update(receipt=None, outcome='unverified', reason_code='RECEIPT_CONTACT_LINK_UNVERIFIED',
                              reason='Recibo registrado; vínculo exato com o contato não verificado.')
                continue
            match = direct or reconciled
            mid, recipient, digest = match[1], match[3] if direct else match[2], match[4] if direct else match[3]
            if (recipient != meta.get('recipient') or digest != meta.get('sha256_subject_newline_text')
                    or not task['completed']):
                result.update(receipt=None, outcome='unverified', reason_code='RECEIPT_METADATA_MISMATCH')
                continue
            stamp = None
            if direct:
                try:
                    stamp = datetime.fromisoformat(match[2].replace('Z', '+00:00'))
                    if stamp.tzinfo is None:
                        raise ValueError()
                except ValueError:
                    result.update(receipt=None, outcome='unverified', reason_code='RECEIPT_DATE_INVALID')
                    continue
            result.update(outcome='sent_with_receipt', reason=None, reason_code=None,
                receipt={'provider': 'gmail', 'message_id': mid, 'task_id': task['id'],
                         'contact_id': cid, 'batch': pipe['batch'], 'payload_sha256': digest,
                         'confirmed_at': stamp.isoformat() if stamp else None,
                         'verification': 'recorded_confirmation', 'source': 'hubspot_task_body',
                         'reconciled': bool(reconciled)})
        elif outcome:
            reason = outcome[1]
            code, state = None, 'unverified'
            if 'Resultado nao confirmado; sem retry.' in reason:
                code, state = 'GMAIL_SEND_403_NO_RECEIPT', 'uncertain_no_retry'
            elif reason.startswith('Historico LinkedIn incompleto:') and reason.endswith('E1 nao enviado.'):
                code, state = 'LINKEDIN_HISTORY_INCOMPLETE', 'blocked'
            elif reason.startswith('Busca Gmail falhou HTTP403/') and reason.endswith('nenhum envio tentado.'):
                code, state = 'GMAIL_PREFLIGHT_403', 'blocked'
            elif reason == 'Endereco Extrapolated sem validacao; E1 nao enviado.':
                code, state = 'EMAIL_UNVALIDATED', 'blocked'
            elif reason.endswith('Bloqueio absoluto preservado.'):
                code, state = 'IDENTITY_HOLD', 'blocked'
            elif reason.startswith('Resposta humana') and reason.endswith('Suspender cadencia fria nos dois canais.'):
                code, state = 'HUMAN_REPLY_LINKEDIN', 'paused_reply'
            result.update(outcome=state, receipt=None, reason=reason, reason_code=code)
    return result
