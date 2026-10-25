"""Assisted Gmail handoff. No provider send, scheduler, credentials or CRM writes.

A canonical job is consumed before giving a single-use dispatch ticket to Codex.
Lost tickets stay uncertain. Receipt confirmation uses fresh provider reads.
"""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import os
import re
from email.utils import parsedate_to_datetime
import time

from .models import payload_hash
from .thunder_email_runtime import GmailCadenceAdapter, pinned_policy, require
from .email_readback import GmailReceiptReader, followup_body, verify_followup, _address
from .seqd_client import MAILBOX
from .worker import WorkerRuntime


class CodexAssisted:
    def __init__(self, service, ledger, *, reader=None, enabled=False, policy_loader=None, clock=time.time):
        self.service=service;self.repo=service.repository;self.ledger=ledger
        self.reader=reader or GmailReceiptReader(self.repo);self.enabled=enabled;self.clock=clock
        self.policy_loader=policy_loader
        def send_policy():
            raw=policy_loader()
            return {k:v for k,v in raw.items() if k!='packet_review_hash'}
        self.adapter=GmailCadenceAdapter(self.repo,enabled=enabled,policy_loader=send_policy,clock=clock)
        self.worker=WorkerRuntime(self.repo,ledger,{'email':self.adapter},worker_id='codex-assisted',
            health_path=Path('/unused'),revision='assisted')
        self.worker.authorization_service=service

    def preview(self,cadence_id,step_id):
        cadence=self.repo.get_state('cadence',cadence_id)
        require(cadence is not None,'assisted_cadence_missing')
        context=cadence.get('execution_context',{})
        require(context.get('platform')=='thunder_os','assisted_platform_not_supported')
        rows=[s for s in cadence['steps'] if s['id']==step_id]
        require(len(rows)==1 and rows[0]['channel']=='email','assisted_email_step_required')
        step=rows[0]
        require((not step.get('predecessor_id') and step.get('order')==1) or
                (step.get('predecessor_id') and step.get('order')==2),'assisted_step_order_unsupported')
        binding=self.service.campaign_registry.binding(context['campaign_id'])
        require(binding['providers'].get('email')=='gmail' and binding['provider_accounts'].get('email')==MAILBOX,
                'assisted_gmail_binding_required')
        approval=self.repo.get_state('approval',cadence_id)
        jobs=[j for j in self.repo.effects() if j['payload'].get('cadence_id')==cadence_id and j['payload'].get('step',{}).get('id')==step_id]
        require(len(jobs)<=1,'assisted_job_ambiguous')
        packet={'schema':'thunder_codex_assisted_v1','cadence_id':cadence_id,'step_id':step_id,
            'campaign':deepcopy(context),'cadence_version':cadence['version'],
            'approval_hash':payload_hash(approval),'job_id':jobs[0]['id'] if jobs else None,
            'payload':{'from_address':MAILBOX,'to':step['recipient'],'subject':step['subject'],
                'payload':{'mime_type':'text/plain','charset':'UTF-8','body':{'content':step['content']}},
                'response_fields':['id','thread_id','label_ids']},
            'limits':{'max_attempts':1,'max_recipients':1,'retry_on_unknown':False,'crm_write':False,
                'complete_history_required':True,'sequential':True},
            'return_route':'/api/os/v1/integrations/codex-assisted/',
            'dispatch_authorized':False}
        if step.get('predecessor_id'):
            contract=self._reply_contract(cadence,step,check_history=True)
            packet['reply_contract']=contract
            packet['payload']['reply_message_id']=contract['message_id']
            packet['expected_sent_content']=followup_body(contract,step['content'])
        packet['review_hash']=payload_hash(packet)
        return packet

    def _reply_contract(self,cadence,step,*,check_history=False):
        parents=[j for j in self.repo.effects() if j['payload'].get('cadence_id')==cadence['id']
                 and j['payload'].get('step',{}).get('id')==step['predecessor_id']]
        require(len(parents)==1 and parents[0]['state']=='sent','assisted_predecessor_unconfirmed')
        parent=parents[0];prior=parent['payload']['step'];receipt=parent['receipt_ref']
        require(prior.get('order')==1 and not prior.get('predecessor_id') and prior['recipient']==step['recipient']
                and step['subject']=='Re: '+prior['subject'],'assisted_reply_scope_mismatch')
        before=self.reader._binding(receipt)
        require(before[0]['id']==parent['id'] and before[1]['id']==cadence['id'],'assisted_predecessor_binding')
        message=self.reader.read_message(receipt,reply_metadata=True)
        require(message['to']==prior['recipient'] and message['subject']==prior['subject']
                and message['content']==prior['content'],'assisted_predecessor_content_changed')
        require(not message['source_in_reply_to'] and not message['source_references'],'assisted_predecessor_not_first_email')
        date=message.get('source_date');require(isinstance(date,str) and len(date)<=128 and not any(ord(c)<32 for c in date),'assisted_predecessor_date_invalid')
        require(parsedate_to_datetime(date).tzinfo is not None,'assisted_predecessor_date_invalid')
        contract={'version':1,'receipt':receipt,'effect_id':parent['id'],'message_id':message['id'],
                  'thread_id':message['source_thread_id'],'rfc_message_id':message['source_message_id'],
                  'source_date':date,'original_text':message['content']}
        if check_history:
            response=self.reader.client.conversation(contract['thread_id']);thread=response.get('thread',{})
            require(response.get('mailbox')==MAILBOX and thread.get('id')==contract['thread_id']
                    and thread.get('complete') is True and thread.get('truncated') is False
                    and thread.get('coverage',{}).get('reasons')==[],'assisted_thread_coverage_incomplete')
            messages=thread.get('messages')
            # Any later incoming (human/automatic/bounce) or extra outgoing is a hold.
            # Classification/stop is the existing reviewed reply workflow, never guessed here.
            require(isinstance(messages,list) and len(messages)==1 and messages[0].get('id')==contract['message_id'],
                    'assisted_thread_changed_or_response')
            original=messages[0]
            require(original.get('complete') is True and original.get('coverage',{}).get('reasons')==[]
                    and _address(original.get('from'))==MAILBOX and _address(original.get('to'))==prior['recipient']
                    and original.get('bodies',{}).get('text')==prior['content'] and original.get('subject')==prior['subject'],
                    'assisted_thread_original_mismatch')
        require(self.reader._binding(receipt)==before,'assisted_predecessor_changed_during_read')
        return contract

    def claim(self,cadence_id,step_id,review_hash):
        require(self.enabled is True and self.ledger is not None,'assisted_dispatch_disabled')
        policy=self.adapter._policy()
        with self.repo.transaction():
            packet=self.preview(cadence_id,step_id)
            require(packet['review_hash']==review_hash,'assisted_review_changed')
            if packet.get('reply_contract'):
                require(self.policy_loader().get('packet_review_hash')==review_hash,'assisted_followup_packet_not_approved')
            job=self.repo.effect(packet['job_id']) if packet['job_id'] else None
            require(job and job['state']=='pending' and not job.get('lease_owner'),'assisted_job_unavailable')
            require(self.repo.get_state('codex_assisted',job['id']) is None,'assisted_attempt_consumed')
            step,_=self.adapter._scope(policy,job)
            error=self.worker._runtime_gate(job['payload'],step,job['effect_key'])
            require(error is None,error or 'assisted_gate_failed')
            require(self.adapter._policy()==policy,'assisted_policy_changed')
            if packet.get('reply_contract'):
                require(self.policy_loader().get('packet_review_hash')==review_hash,'assisted_followup_packet_not_approved')
            created,touch=self.ledger.reserve(job['effect_key'],step_id,step)
            require(created,'assisted_touch_already_reserved')
            # No retryable pending job can survive handing authority to another process.
            self.repo.finish_effect(job['id'],'uncertain',None,'assisted_receipt_pending')
            ticket={**packet,'dispatch_authorized':True,'issued_at':int(self.clock()),
                'expires_at':int(min(policy['expires_at'],self.clock()+60)),
                'reservation_id':str(touch['id'])}
            ticket['ticket_hash']=payload_hash(ticket)
            self.repo.append_state('codex_assisted',job['id'],{'ticket':ticket,'phase':'issued'})
        return ticket

    def dispatch(self,job_id,ticket_hash):
        """One-use final decision immediately before the external tool call.

        This cannot fence a remote Codex process atomically. Five-second validity
        bounds that interval; a missing/late result never authorizes another call.
        """
        require(self.enabled is True,'assisted_dispatch_disabled')
        policy=self.adapter._policy()
        with self.repo.transaction():
            row=self.repo.get_state('codex_assisted',job_id);job=self.repo.effect(job_id)
            require(row and row['phase']=='issued' and row['ticket']['ticket_hash']==ticket_hash
                    and row['ticket']['expires_at']>self.clock(),'assisted_ticket_unavailable')
            require(job and job['state']=='uncertain' and job['error_code']=='assisted_receipt_pending',
                    'assisted_job_unavailable')
            step,_=self.adapter._scope(policy,job)
            error=self.worker._runtime_gate(job['payload'],step,job['effect_key'])
            require(error is None,error or 'assisted_gate_failed')
            if row['ticket'].get('reply_contract'):
                require(self.policy_loader().get('packet_review_hash')==row['ticket']['review_hash'], 'assisted_followup_packet_not_approved')
                cadence=self.repo.get_state('cadence',row['ticket']['cadence_id'])
                require(self._reply_contract(cadence,step,check_history=True)==row['ticket']['reply_contract'],
                        'assisted_predecessor_changed')
                # Repeat local authority checks after provider I/O.
                self.adapter._scope(self.adapter._policy(),job)
                error=self.worker._runtime_gate(job['payload'],step,job['effect_key'])
                require(error is None,error or 'assisted_gate_failed')
            if row['ticket'].get('reply_contract'):
                require(self.policy_loader().get('packet_review_hash')==row['ticket']['review_hash'],'assisted_followup_packet_not_approved')
            grant={'ticket_hash':ticket_hash,'dispatch_authorized':True,'issued_at':int(self.clock()),
                   'expires_at':int(min(row['ticket']['expires_at'],self.clock()+5))}
            self.repo.put_state('codex_assisted',job_id,{**row,'phase':'dispatch_issued','grant':grant})
            return grant

    def _proof(self,job_id,receipt):
        row=self.repo.get_state('codex_assisted',job_id);job=self.repo.effect(job_id)
        require(row and row['phase'] in {'dispatch_issued','confirmed'} and job and job['state'] in {'uncertain','needs_reconciliation','sent'},'assisted_attempt_missing')
        ticket=row['ticket'];cadence=self.repo.get_state('cadence',ticket['cadence_id'])
        require(cadence and cadence['execution_context']==ticket['campaign'] and
                self.service.campaign_registry.load(ticket['campaign']['campaign_id'])==ticket['campaign'],
                'assisted_binding_changed')
        step=job['payload']['step'];current=[s for s in cadence['steps'] if s['id']==ticket['step_id']]
        require(len(current)==1 and all(current[0].get(k)==step.get(k) for k in ('channel','recipient','subject','content')),
                'assisted_content_changed')
        content=step['content']
        if ticket.get('reply_contract'):
            require(self._reply_contract(cadence,step)==ticket['reply_contract'],'assisted_predecessor_changed')
            content=verify_followup(self.reader.read_message(receipt,reply_metadata=True),ticket['reply_contract'],step['content'])
        event=self.reader.read_sent_event(receipt,{'mailbox':MAILBOX,'to':step['recipient'],
            'subject':step['subject'],'content':content})
        when=datetime.fromisoformat(event['occurred_at'].replace('Z','+00:00')).timestamp()
        require(row['grant']['issued_at']<=when<=row['grant']['expires_at']+30,'assisted_receipt_outside_attempt')
        require(self.repo.effect(job_id)==job and self.repo.get_state('codex_assisted',job_id)==row
                and self.repo.get_state('cadence',ticket['cadence_id'])==cadence,'assisted_changed_during_read')
        return {'job':job,'row':row,'cadence':cadence,'event':event,'receipt':receipt}

    def receipt_preview(self,job_id,receipt):
        proof=self._proof(job_id,receipt)
        return {'review_hash':payload_hash(proof),'receipt':receipt,'event':proof['event'],'send_attempted':False,'verification':'provider_sent_copy','attempt_attribution':'operator_reported'}

    def receipt_confirm(self,job_id,receipt,review_hash):
        require(self.ledger is not None,'assisted_ledger_unavailable')
        proof=self._proof(job_id,receipt)
        require(payload_hash(proof)==review_hash,'assisted_receipt_review_changed')
        with self.repo.transaction():
            require(self.repo.get_state('codex_assisted',job_id)==proof['row'] and
                    self.repo.get_state('cadence',proof['cadence']['id'])==proof['cadence'] and
                    self.service.campaign_registry.load(proof['row']['ticket']['campaign']['campaign_id'])==proof['row']['ticket']['campaign'],
                    'assisted_binding_changed')
            self.repo.validate_uncertain_receipt(proof['job'],receipt)
            stored=self.ledger.confirm_receipt(proof['job']['effect_key'],proof['job']['payload']['step'],receipt)
            require(stored['status']=='sent' and stored['receipt_ref']==receipt,'assisted_ledger_conflict')
            self.repo.confirm_uncertain_receipt(proof['job'],receipt)
            cadence=proof['cadence'];step_id=proof['row']['ticket']['step_id']
            self.repo.put_state('codex_assisted',job_id,{**proof['row'],'phase':'confirmed','receipt':receipt,
                'verification':'provider_sent_copy','attempt_attribution':'operator_reported',
                'limitation':'Cópia enviada confirmada no Gmail; vínculo à tentativa informado pelo operador.'})
        return {'status':'confirmed','receipt':receipt,'send_attempted':False,'retry_write':False,'verification':'provider_sent_copy','attempt_attribution':'operator_reported'}


def configured_assisted(service):
    from .touch_ledger import TouchLedger
    enabled=(os.environ.get('THUNDER_OS_CODEX_ASSISTED_DISPATCH')=='true' and
             os.environ.get('THUNDER_EXTERNAL_EFFECTS_ENABLED')=='true')
    from .internal_canary import InternalCanaryService
    return CodexAssisted(InternalCanaryService(service),TouchLedger(),enabled=enabled,
        policy_loader=lambda:pinned_policy('THUNDER_OS_CODEX_ASSISTED_POLICY'))
