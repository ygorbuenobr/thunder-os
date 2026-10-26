"""Explicitly reviewed manual divergence; never a receipt or send authorization."""
from copy import deepcopy
from datetime import timezone
from email.utils import parsedate_to_datetime
import re

from .cadence import CadenceEngine
from .email_readback import _address
from .models import payload_hash
from .seqd_client import MAILBOX
from .thunder_email_runtime import require


def verified_record(repo, kind, job_id):
    """Read projection requires the immutable proof, explicit approval and audit event."""
    row = repo.get_state(kind, job_id)
    if not row or kind not in {'manual_observation','manual_observed_reply'}:
        return None
    proof = row.get('proof', {})
    action, event = ('record_manual_divergence','manual_divergence_observed') if kind == 'manual_observation' else ('record_manual_reply_and_pause','manual_reply_internal_pause')
    digest = proof.get('review_hash')
    if (row.get('actor') != 'ygor' or not isinstance(digest,str)
            or payload_hash({k:v for k,v in proof.items() if k!='review_hash'}) != digest
            or row.get('approval') != {'action':action,'review_hash':digest,'confirmed':True}
            or row['approval'].get('confirmed') is not True or proof.get('job_id') != job_id
            or not any(e['type']==event and e['payload'].get('review_hash')==digest for e in repo.events(job_id))):
        return None
    return row


class ManualObservation:
    def __init__(self, assisted):
        self.a, self.repo = assisted, assisted.repo

    def scope(self, job_id):
        job = self.repo.effect(job_id)
        row = self.repo.get_state('codex_assisted', job_id)
        require(job and row, 'manual_observation_missing')
        cadence, window = self.a._manual_record(job, row)
        require(job['state'] == 'failed' and job.get('error_code') == 'proved_not_sent'
                and not job.get('receipt_ref') and row['phase'] == 'proved_not_sent', 'manual_original_terminal_required')
        require(window['prepared_at'] <= self.a.clock() <= window['observe_until'], 'manual_observation_expired')
        return job, row, cadence, window

    def read(self, message_id, window, *, sent):
        require(isinstance(message_id, str) and re.fullmatch(r'[a-f0-9]{1,64}', message_id), 'manual_message_id')
        client = self.a.reader.client
        profile = client.status()
        require(profile.get('mailbox') == MAILBOX and profile.get('status') == 'ready', 'manual_mailbox')
        response = client.read(message_id)
        require(response.get('mailbox') == MAILBOX and response.get('status') == 'ok', 'manual_mailbox')
        m = response.get('message', {})
        require(m.get('id') == message_id and m.get('truncated') is False and m.get('headers_complete') is True,
                'manual_incomplete')
        require(isinstance(m.get('body'), str) and 0 < len(m['body'].encode()) <= 65536, 'manual_body')
        require(isinstance(m.get('thread_id'), str) and re.fullmatch(r'[a-f0-9]{1,64}', m['thread_id']), 'manual_thread')
        labels = m.get('labelIds')
        require(isinstance(labels, list) and all(isinstance(x, str) for x in labels)
                and 'DRAFT' not in labels and ('SENT' in labels) is sent and m.get('sent') is sent, 'manual_direction')
        headers = m.get('headers')
        require(isinstance(headers, list) and len(headers) <= 500, 'manual_headers')
        values = {}
        for h in headers:
            require(isinstance(h, dict) and isinstance(h.get('name'), str) and isinstance(h.get('value'), str), 'manual_headers')
            key, value = h['name'].lower(), h['value']
            require(not any(ord(c) < 32 or ord(c) == 127 for c in value), 'manual_headers')
            if key in {'from','to','cc','bcc','subject','date','message-id','in-reply-to','auto-submitted'}:
                require(key not in values, 'manual_ambiguous_header')
                values[key] = value
        for key in ('from','to','subject','date','message-id'):
            require(values.get(key), 'manual_headers')
        require(not values.get('cc') and not values.get('bcc'), 'manual_recipients')
        for key in ('from','to','subject','date'):
            require(m.get(key) == values[key], 'manual_header_mismatch')
        require(m.get('message_id') == values['message-id'] and re.fullmatch(r'<[^<>\s]+@[^<>\s]+>', values['message-id']), 'manual_rfc_id')
        from .codex_assisted import MANUAL_RECIPIENT
        expected = {'to': MANUAL_RECIPIENT}
        require(_address(values['from']) == (MAILBOX if sent else expected['to'])
                and _address(values['to']) == (expected['to'] if sent else MAILBOX), 'manual_identity')
        date = parsedate_to_datetime(values['date'])
        require(date.tzinfo is not None and window['prepared_at'] <= date.timestamp() <= min(window['observe_until'], self.a.clock()+30), 'manual_date')
        require(values.get('auto-submitted', 'no').lower() == 'no', 'manual_automated_reply')
        mime = m.get('mime_evidence')
        if m.get('context_only') is True:
            require(m.get('receipt_eligible') is False and isinstance(mime, dict)
                    and mime.get('root_type') == 'multipart/alternative' and mime.get('selected_type') == 'text/plain', 'manual_mime')
            parts = mime.get('alternatives', [])
            require(isinstance(parts, list) and 1 <= len(parts) <= 2
                    and all(isinstance(p, dict) and p.get('mime_type') in {'text/plain','text/html'}
                            and isinstance(p.get('body'), str) for p in parts), 'manual_mime')
            require(len({p['mime_type'] for p in parts}) == len(parts)
                    and [p['body'] for p in parts if p['mime_type']=='text/plain'] == [m['body']]
                    and sum(len(p['body'].encode()) for p in parts) <= 65536, 'manual_mime')
        else:
            require(m.get('receipt_eligible', True) is True, 'manual_mime')
        return {'message': deepcopy(m), 'headers': values, 'content_hash': payload_hash(m['body']),
                'evidence_hash': payload_hash(response), 'occurred_at': date.astimezone(timezone.utc).isoformat(),
                'source': {'provider':'gmail','mailbox':MAILBOX,'message_id':message_id,'thread_id':m['thread_id']}}

    def preview(self, job_id, message_id, declared_author):
        require(isinstance(declared_author, str) and 0 < len(declared_author.strip()) <= 200
                and not any(ord(c)<32 for c in declared_author), 'manual_author_required')
        job, row, cadence, window = self.scope(job_id)
        observed = self.read(message_id, window, sent=True)
        step = job['payload']['step']
        require(observed['message']['subject'] == step['subject'], 'manual_subject')
        require(observed['message']['body'] != step['content'] or observed['message'].get('context_only') is True, 'manual_divergence_required')
        result = {'job_id':job_id,'cadence_id':cadence['id'],'observation':observed,
                  'declared_author':declared_author,'attribution':'operator_declared_not_verified',
                  'original_job_hash':payload_hash(job),'manual_window_hash':payload_hash(window),
                  'expected_content':step['content'],'expected_content_hash':payload_hash(step['content']),
                  'differences':{'content_equal':observed['message']['body']==step['content'],
                                 'multipart':observed['message'].get('context_only') is True},
                  'classification':'manual_divergent_observation','automatic_success':False,
                  'receipt_pass':False,'dispatch_authorized':False}
        result['review_hash'] = payload_hash(result)
        return result

    @staticmethod
    def authorize(approval, action, review, actor):
        require(actor == 'ygor' and isinstance(approval, dict) and approval.get('confirmed') is True and approval == {
            'action':action,'review_hash':review,'confirmed':True}, 'manual_specific_authorization_required')

    def record(self, job_id, message_id, declared_author, review_hash, approval, *, actor):
        self.authorize(approval, 'record_manual_divergence', review_hash, actor)
        proof = self.preview(job_id, message_id, declared_author)
        require(proof['review_hash'] == review_hash, 'manual_review_changed')
        with self.repo.transaction():
            job, row, cadence, window = self.scope(job_id)
            require(payload_hash(job)==proof['original_job_hash'] and payload_hash(window)==proof['manual_window_hash'], 'manual_state_changed')
            prior = self.repo.get_state('manual_observation', job_id)
            if prior:
                require(prior['proof']==proof, 'manual_observation_conflict')
                return prior
            result = self.repo.append_state('manual_observation', job_id, {'proof':proof,'approval':deepcopy(approval),'actor':actor})
            self.repo.append_event(job_id, 'manual_divergence_observed', {'review_hash':review_hash,'message_id':message_id,'automatic_success':False})
            return result

    def reply_preview(self, job_id, message_id):
        job, row, cadence, window = self.scope(job_id)
        saved = verified_record(self.repo, 'manual_observation', job_id)
        require(saved is not None, 'manual_observation_required')
        original = saved['proof']
        # Freshly revalidate the observed sent copy; no receipt binding is invented.
        again = self.preview(job_id, original['observation']['source']['message_id'], original['declared_author'])
        require(again==original, 'manual_observation_changed')
        reply = self.read(message_id, window, sent=False)
        require(reply['source']['thread_id']==original['observation']['source']['thread_id']
                and reply['headers'].get('in-reply-to')==original['observation']['headers']['message-id']
                and reply['occurred_at']>=original['observation']['occurred_at'], 'manual_reply_correlation')
        prior = self.repo.get_state('manual_observed_reply', job_id)
        cadence_hash = payload_hash(cadence)
        if prior:
            require(prior.get('paused_cadence_hash') == cadence_hash, 'manual_pause_state_changed')
            cadence_hash = prior['proof']['cadence_hash']
        result = {'job_id':job_id,'cadence_id':cadence['id'],'cadence_hash':cadence_hash,'observation_hash':original['review_hash'],
                  'reply':reply,'action':'record_manual_reply_and_pause','automatic_success':False,'receipt_pass':False}
        result['review_hash']=payload_hash(result)
        return result

    def reply_record(self, job_id, message_id, review_hash, approval, *, actor):
        self.authorize(approval, 'record_manual_reply_and_pause', review_hash, actor)
        proof = self.reply_preview(job_id, message_id)
        require(proof['review_hash']==review_hash, 'manual_review_changed')
        with self.repo.transaction():
            job, row, cadence, window = self.scope(job_id)
            observed = self.repo.get_state('manual_observation', job_id)
            require(observed['proof']['review_hash']==proof['observation_hash'], 'manual_state_changed')
            prior = self.repo.get_state('manual_observed_reply', job_id)
            if prior:
                require(prior['proof']==proof and cadence.get('status')=='paused_reply', 'manual_reply_conflict')
                return prior
            require(payload_hash(cadence)==proof['cadence_hash'], 'manual_cadence_changed')
            require(cadence.get('status') in {'approved','active','uncertain'}, 'manual_cadence_not_pauseable')
            value = {k:v for k,v in cadence.items() if k not in {'id','version','updated_at'}}
            value.update(status='paused_reply', steps=CadenceEngine().pause_for_reply(cadence['steps']),
                         pause_reason='Resposta a envio manual divergente; tentativa automatica nao enviada',
                         manual_observation_job_id=job_id)
            self.repo.put_states_atomic([('cadence',cadence['id'],value,cadence['version'])])
            paused_hash = payload_hash(self.repo.get_state('cadence',cadence['id']))
            result = self.repo.append_state('manual_observed_reply',job_id,{'proof':proof,'approval':deepcopy(approval),'actor':actor,'paused_cadence_hash':paused_hash})
            self.repo.append_event(job_id,'manual_reply_internal_pause',{'review_hash':review_hash,'cadence_id':cadence['id'],
                'message_id':message_id,'automatic_success':False,'external_pause':False,'crm_write':False})
            return result
