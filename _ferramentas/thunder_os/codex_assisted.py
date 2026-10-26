"""Assisted Gmail handoff. No provider send, scheduler, credentials or CRM writes.

A canonical job is consumed before giving a single-use dispatch ticket to Codex.
Lost tickets stay uncertain. Receipt confirmation uses fresh provider reads.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import os
import re
from email.utils import parsedate_to_datetime
import time
import math
import hashlib
import json

from .models import payload_hash
from .thunder_email_runtime import GmailCadenceAdapter, pinned_policy, require
from .email_readback import GmailReceiptReader, followup_body, verify_followup, _address
from .seqd_client import MAILBOX
from .worker import WorkerRuntime


MANUAL_JOB_ID='effect-af47828809034c4491f876f9e3849fe7'
MANUAL_RECIPIENT='ygorbueno1555@gmail.com'
MANUAL_WINDOW_SECONDS=24*60*60


@dataclass(frozen=True, slots=True)
class ProvenNotSentProof:
    """Trusted administrative attestation, never an assertion from a panel body."""
    job_id: str
    packet_hash: str
    ticket_hash: str
    grant_hash: str
    tool_not_invoked: bool
    durable_attempt_consumed: bool
    host_marker_hash: str
    exact_query_zero: bool
    evidence_hash: str
    operator_at: float
    failure_code: str
    actor: str='ygor'


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

    def recover_pre_dispatch(self,cadence_id,step_id,review_hash):
        """Explicit administrative renewal, no ticket/dispatch/attempt may exist.

        Each renewal requires a newly approved policy after previous expiry. Keep
        the original recovery and append audit history; never reopen dispatch.
        """
        return self.recover_preclaim(cadence_id,step_id,review_hash,_administrative=True)

    def recover_preclaim(self,cadence_id,step_id,review_hash,*,_administrative=False):
        """One explicit recovery before any ticket; preserve job, scope and history."""
        from .internal_canary import PREFIX
        require(cadence_id.startswith(PREFIX) and step_id=='e1','assisted_recovery_internal_only')
        require(self.enabled is True and self.ledger is not None,'assisted_dispatch_disabled')
        policy=self.adapter._policy()
        with self.repo.transaction():
            row=self.repo.get_state('internal_canary',cadence_id)
            require(row and (_administrative or not row.get('preclaim_recovery')),'assisted_recovery_already_used')
            self.service.check_policy(cadence_id,row['scope'],row['approval_ref'])
            require(policy['issued_at']>row['expires_at'],'assisted_recovery_fresh_policy_required')
            packet=self.preview(cadence_id,step_id)
            require(packet['review_hash']==review_hash,'assisted_review_changed')
            job=self.repo.effect(packet['job_id']) if packet['job_id'] else None
            require(job and job['state']=='pending' and not job.get('lease_owner')
                and not job.get('receipt_ref'),'assisted_recovery_job_not_pending')
            require(self.repo.get_state('codex_assisted',job['id']) is None
                and self.repo.get_state('gmail_cadence_attempt',self.adapter._key(policy)) is None,
                'assisted_recovery_attempt_exists')
            step,_=self.adapter._scope(policy,job)
            evidence=self.ledger.preflight(job['effect_key'],step,raise_blocked=True)
            require(evidence.get('duplicate') is None and evidence.get('blocked') is None,'assisted_recovery_touch_exists')
            # This transaction rolls back renewal if any original runtime gate fails.
            clean={k:v for k,v in row.items() if k not in {'version','updated_at','id'}}
            self.repo.put_state('internal_canary',cadence_id,{**clean,'expires_at':policy['expires_at'],
                'preclaim_recovery':row.get('preclaim_recovery') or {'previous_expires_at':row['expires_at'],'at':self.clock(),
                    'job_id':job['id'],'review_hash':review_hash},
                'recovery_history':row.get('recovery_history',[])+[{'previous_expires_at':row['expires_at'],
                    'at':self.clock(),'job_id':job['id'],'review_hash':review_hash,
                    'policy_hash':payload_hash(policy),'administrative':_administrative}]})
            error=self.worker._runtime_gate(job['payload'],step,job['effect_key'])
            require(error is None,error or 'assisted_gate_failed')
            return {'recovered_preclaim':True,'review_hash':review_hash,'job_id':job['id'],
                'dispatch_authorized':False,'send_attempted':False,'checked_at':self.clock()}

    def reconcile_reservation(self,cadence_id,step_id,review_hash):
        """Explicit recovery of our pre-ticket reservation, never a provider retry."""
        return self.claim(cadence_id,step_id,review_hash,_reconcile=True)

    def claim(self,cadence_id,step_id,review_hash,*,_reconcile=False):
        require(self.enabled is True and self.ledger is not None,'assisted_dispatch_disabled')
        policy=self.adapter._policy()
        # Commit intent BEFORE crossing databases. Failure keeps a durable checkpoint.
        with self.repo.transaction():
            packet=self.preview(cadence_id,step_id)
            require(packet['review_hash']==review_hash,'assisted_review_changed')
            ident=packet['job_id'];prior=self.repo.get_state('assisted_reservation_intent',ident)
            if prior:
                require(prior['review_hash']==review_hash,'assisted_reservation_intent_changed')
            else:
                require(not _reconcile,'assisted_reservation_intent_missing')
                self.repo.append_state('assisted_reservation_intent',ident,
                    {'review_hash':review_hash,'job_id':ident,'created_at':self.clock()})
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
            owned=self.ledger.reservation(job['effect_key'],step)
            if owned:
                require(_reconcile and owned['status']=='reserved' and not owned.get('receipt_ref'),
                        'assisted_reservation_reconciliation_required')
                touch=owned
            else:
                created,touch=self.ledger.reserve(job['effect_key'],step_id,step,strict_new=True)
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
            reserved=self.ledger.reservation(job['effect_key'],step)
            require(reserved and reserved['id']==row['ticket']['reservation_id']
                    and reserved['status']=='reserved' and not reserved.get('receipt_ref'),
                    'assisted_reservation_changed')
            grant={'ticket_hash':ticket_hash,'packet_hash':row['ticket']['review_hash'],'job_id':job_id,'dispatch_authorized':True,'issued_at':int(self.clock()),
                   'expires_at':int(min(row['ticket']['expires_at'],self.clock()+5))}
            self.repo.put_state('codex_assisted',job_id,{**row,'phase':'dispatch_issued','grant':grant})
            return grant

    def _manual_internal(self,job,row):
        """Historical scope checks only: expired send policies are not renewed."""
        from .internal_canary import PREFIX
        ticket=row['ticket'];ident=ticket['cadence_id']
        cadence=self.repo.get_state('cadence',ident)
        internal=self.repo.get_state('internal_canary',ident)
        require(job['id']==MANUAL_JOB_ID and ident.startswith(PREFIX) and internal
                and internal.get('actor')=='ygor','assisted_manual_internal_only')
        scope=internal.get('scope',{});step=job['payload']['step']
        require(scope.get('kind')=='internal_canary' and scope.get('from')==MAILBOX
                and scope.get('to')==MANUAL_RECIPIENT and scope.get('max_attempts')==1
                and scope.get('no_followup') is True and internal.get('scope_hash')==payload_hash(scope),
                'assisted_manual_scope_changed')
        require(cadence and cadence.get('account_id')==ident and cadence.get('person_key')==MANUAL_RECIPIENT
                and not cadence.get('hubspot_contact_id') and len(cadence.get('steps',[]))==1
                and job['payload']['cadence_id']==ident and ticket['step_id']==step['id']=='e1'
                and step.get('channel')=='email' and step.get('order')==1 and not step.get('predecessor_id')
                and not ticket.get('reply_contract') and step.get('recipient')==scope['to']
                and step.get('subject')==scope.get('subject') and step.get('content')==scope.get('content'),
                'assisted_manual_envelope_changed')
        require(all(cadence['steps'][0].get(k)==step.get(k) for k in
                ('id','channel','recipient','subject','content','order','predecessor_id')),
                'assisted_manual_envelope_changed')
        require(cadence['execution_context']==ticket['campaign']
                and self.service.campaign_registry.load(ident)==ticket['campaign'],
                'assisted_binding_changed')
        binding=self.service.campaign_registry.binding(ident)
        require(binding['providers'].get('email')=='gmail' and binding['provider_accounts'].get('email')==MAILBOX,
                'assisted_gmail_binding_required')
        return cadence,internal

    def reconcile_proven_not_sent(self,job_id,proof: ProvenNotSentProof):
        """Consume one known pre-tool failure; never release, requeue or retry it.

        The 24h window observes a future operator sent copy. It is not send
        permission, a renewed grant, or a substitute for the approved envelope.
        """
        require(type(proof) is ProvenNotSentProof,'assisted_manual_typed_proof_required')
        now=self.clock()
        require(proof.actor=='ygor' and proof.failure_code=='RPC_DISPATCH_EXPIRED'
                and proof.job_id==job_id==MANUAL_JOB_ID,'assisted_manual_failure_proof_invalid')
        require(all(value is True for value in (proof.tool_not_invoked,proof.durable_attempt_consumed,
                proof.exact_query_zero)),'assisted_manual_failure_proof_invalid')
        require(type(proof.operator_at) in (int,float) and math.isfinite(proof.operator_at)
                and 0<=now-proof.operator_at<=300,'assisted_manual_attestation_stale')
        require(all(isinstance(value,str) and re.fullmatch(r'[0-9a-f]{64}',value) for value in
                (proof.packet_hash,proof.ticket_hash,proof.grant_hash,proof.evidence_hash,proof.host_marker_hash)),
                'assisted_manual_proof_hash_invalid')
        with self.repo.transaction():
            job=self.repo.effect(job_id);row=self.repo.get_state('codex_assisted',job_id)
            require(job and row and job['state']=='uncertain' and not job.get('receipt_ref')
                    and not job.get('lease_owner') and not job.get('lease_until')
                    and job.get('error_code')=='assisted_receipt_pending' and row.get('phase')=='dispatch_issued'
                    and not row.get('receipt') and not row.get('manual_reconciliation'),
                    'assisted_manual_original_state_changed')
            ticket=row['ticket'];grant=row['grant']
            require(ticket['job_id']==job_id and grant['job_id']==job_id
                    and proof.ticket_hash==ticket['ticket_hash']==grant['ticket_hash']
                    and proof.packet_hash==ticket['review_hash']==grant['packet_hash']
                    and proof.grant_hash==payload_hash(grant)
                    and now>grant['expires_at'] and proof.operator_at>=grant['issued_at'],
                    'assisted_manual_attempt_binding')
            # Exact bytes written by consumeHostAttempt: JSON.stringify in this
            # insertion order, followed by one newline. The root operator hashes
            # the preserved host-ticket file; this service never recreates it.
            marker={'packet_hash':proof.packet_hash,'ticket_hash':proof.ticket_hash,
                    'job_id':job_id,'not_after':grant['expires_at']}
            marker_bytes=(json.dumps(marker,separators=(',',':'),ensure_ascii=False)+'\n').encode('utf-8')
            require(hashlib.sha256(marker_bytes).hexdigest()==proof.host_marker_hash,
                    'assisted_manual_host_marker_mismatch')
            require(payload_hash({k:v for k,v in ticket.items() if k!='ticket_hash'})==ticket['ticket_hash'],
                    'assisted_manual_ticket_changed')
            packet={k:v for k,v in ticket.items() if k not in {'ticket_hash','review_hash','issued_at','expires_at','reservation_id'}}
            packet['dispatch_authorized']=False
            require(payload_hash(packet)==proof.packet_hash,'assisted_manual_packet_changed')
            cadence,internal=self._manual_internal(job,row)
            require(ticket['payload']=={'from_address':MAILBOX,'to':MANUAL_RECIPIENT,
                    'subject':job['payload']['step']['subject'],
                    'payload':{'mime_type':'text/plain','charset':'UTF-8','body':{'content':job['payload']['step']['content']}},
                    'response_fields':['id','thread_id','label_ids']},'assisted_manual_packet_changed')
            attempt_key=payload_hash({'mailbox':MAILBOX,'cadence':cadence['id'],'step':'e1'})
            require(self.repo.get_state('gmail_cadence_attempt',attempt_key) is None,
                    'assisted_manual_gmail_attempt_exists')
            require(self.ledger is not None,'assisted_ledger_unavailable')
            reservation=self.ledger.reservation(job['effect_key'],job['payload']['step'])
            require(reservation and reservation['id']==ticket['reservation_id']
                    and reservation['status']=='reserved' and not reservation.get('receipt_ref'),
                    'assisted_reservation_changed')
            manual={'schema':'internal_operator_receipt_v1','proof':asdict(proof),'host_marker':marker,
                'job_payload_hash':payload_hash(job['payload']),'envelope_hash':payload_hash(ticket['payload']),
                'internal_scope_hash':internal['scope_hash'],'prepared_at':now,
                'observe_until':now+MANUAL_WINDOW_SECONDS,'window_seconds':MANUAL_WINDOW_SECONDS,
                'dispatch_authorized':False,'retry_write':False,'send_attempted':False}
            self.repo.reconcile_proven_not_sent(job,row,manual)
        return {'job_id':job_id,'status':'proved_not_sent','phase':'proved_not_sent',
                'manual_reconciliation':manual,'dispatch_authorized':False,'send_attempted':False,'retry_write':False}

    def _manual_record(self,job,row):
        cadence,internal=self._manual_internal(job,row)
        manual=row.get('manual_reconciliation',{})
        require(manual.get('schema')=='internal_operator_receipt_v1'
                and manual.get('job_payload_hash')==payload_hash(job['payload'])
                and manual.get('envelope_hash')==payload_hash(row['ticket']['payload'])
                and manual.get('internal_scope_hash')==internal['scope_hash']
                and manual.get('proof',{}).get('job_id')==job['id']
                and manual['proof'].get('ticket_hash')==row['ticket']['ticket_hash']
                and manual['proof'].get('grant_hash')==payload_hash(row['grant'])
                and manual.get('dispatch_authorized') is False and manual.get('retry_write') is False
                and manual.get('window_seconds')==MANUAL_WINDOW_SECONDS
                and manual['observe_until']-manual['prepared_at']==MANUAL_WINDOW_SECONDS,
                'assisted_manual_record_changed')
        require(row.get('phase') in {'proved_not_sent','confirmed'} and
                ((job['state']=='failed' and job.get('error_code')=='proved_not_sent' and not job.get('receipt_ref'))
                or (job['state']=='sent' and row.get('phase')=='confirmed' and row.get('receipt')==job.get('receipt_ref'))),
                'assisted_manual_original_state_changed')
        with self.repo.transaction():
            self.repo.validate_operator_receipt(job,row.get('receipt') or 'gmail:manual-preflight')
        return cadence,manual

    def _proof(self,job_id,receipt):
        row=self.repo.get_state('codex_assisted',job_id);job=self.repo.effect(job_id)
        if row and row.get('manual_reconciliation'):
            cadence,manual=self._manual_record(job,row)
            require(manual['prepared_at']<=self.clock()<=manual['observe_until'],'assisted_manual_observation_expired')
            step=job['payload']['step']
            current=cadence['steps'][0]
            require(all(current.get(k)==step.get(k) for k in ('id','channel','recipient','subject','content')),
                    'assisted_content_changed')
            event=self.reader.read_sent_event(receipt,{'mailbox':MAILBOX,'to':step['recipient'],
                'subject':step['subject'],'content':step['content']})
            when=datetime.fromisoformat(event['occurred_at'].replace('Z','+00:00')).timestamp()
            require(manual['prepared_at']<=when<=min(manual['observe_until'],self.clock()+30),
                    'assisted_manual_receipt_outside_window')
            require(self.clock()<=manual['observe_until'],'assisted_manual_observation_expired')
            require(self.repo.effect(job_id)==job and self.repo.get_state('codex_assisted',job_id)==row
                    and self.repo.get_state('cadence',cadence['id'])==cadence,'assisted_changed_during_read')
            return {'job':job,'row':row,'cadence':cadence,'event':event,'receipt':receipt}
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
        return {'review_hash':payload_hash(proof),'receipt':receipt,'event':proof['event'],'dispatch_authorized':False,'retry_write':False,'send_attempted':False,'verification':'provider_sent_copy','attempt_attribution':'operator_reported'}

    def receipt_confirm(self,job_id,receipt,review_hash):
        require(self.ledger is not None,'assisted_ledger_unavailable')
        proof=self._proof(job_id,receipt)
        require(payload_hash(proof)==review_hash,'assisted_receipt_review_changed')
        with self.repo.transaction():
            require(self.repo.get_state('codex_assisted',job_id)==proof['row'] and
                    self.repo.get_state('cadence',proof['cadence']['id'])==proof['cadence'] and
                    self.service.campaign_registry.load(proof['row']['ticket']['campaign']['campaign_id'])==proof['row']['ticket']['campaign'],
                    'assisted_binding_changed')
            manual=bool(proof['row'].get('manual_reconciliation'))
            validate=self.repo.validate_operator_receipt if manual else self.repo.validate_uncertain_receipt
            confirm=self.repo.confirm_operator_receipt if manual else self.repo.confirm_uncertain_receipt
            validate(proof['job'],receipt)
            stored=self.ledger.confirm_receipt(proof['job']['effect_key'],proof['job']['payload']['step'],receipt)
            require(stored['status']=='sent' and stored['receipt_ref']==receipt,'assisted_ledger_conflict')
            confirm(proof['job'],receipt)
            cadence=proof['cadence'];step_id=proof['row']['ticket']['step_id']
            if manual:
                cadence=deepcopy(cadence)
                cadence['steps'][0].update(status='sent',receipt_ref=receipt)
                self.repo.put_state('cadence',cadence['id'],cadence)
            self.repo.put_state('codex_assisted',job_id,{**proof['row'],'phase':'confirmed','receipt':receipt,
                **({'operator_receipt_event':deepcopy(proof['event'])} if manual else {}),
                'verification':'provider_sent_copy','attempt_attribution':'operator_reported',
                'limitation':('Cópia enviada manualmente confirmada no Gmail; tentativa original provada não enviada. '
                    'Autoria e vínculo informados pelo operador; não comprova entrega.' if manual else
                    'Cópia enviada confirmada no Gmail; vínculo à tentativa informado pelo operador.')})
        return {'status':'confirmed','receipt':receipt,'review_hash':review_hash,'dispatch_authorized':False,'send_attempted':False,'retry_write':False,'verification':'provider_sent_copy','attempt_attribution':'operator_reported'}

    def _manual_reply_scope(self,job_id,receipt,thread_id):
        job=self.repo.effect(job_id);row=self.repo.get_state('codex_assisted',job_id)
        require(job and row and row.get('manual_reconciliation') and row.get('phase')=='confirmed'
                and job['state']=='sent' and row.get('receipt')==job.get('receipt_ref')==receipt
                and row.get('verification')=='provider_sent_copy'
                and row.get('attempt_attribution')=='operator_reported','assisted_manual_confirmed_receipt_required')
        self._manual_record(job,row)
        binding=self.reader._binding(receipt)
        require(binding[0]['id']==job_id,'assisted_manual_reply_binding')
        observed=self.reader.read_message(receipt,reply_metadata=True)
        require(observed['source_thread_id']==thread_id and observed['to']==MANUAL_RECIPIENT,
                'assisted_manual_reply_thread_mismatch')
        require(self.reader._binding(receipt)==binding and self.repo.get_state('codex_assisted',job_id)==row,
                'assisted_manual_reply_binding')
        return row,binding

    def _manual_reply(self,job_id,receipt,thread_id,message_id,review=None,*,preview=False):
        from .thunder_email_runtime import ThunderEmailCycle
        row,binding=self._manual_reply_scope(job_id,receipt,thread_id)
        # This local composition does not activate the globally disabled cycle.
        cycle=ThunderEmailCycle(self.repo,reader=self.reader,client=self.reader.client,
                reply_enabled=True,crm_enabled=False)
        require(cycle.reply_control.external_pause_enabled is False,'assisted_manual_external_pause_forbidden')
        # Recheck our additional scope inside ReplyControl's own write transaction.
        # The ordinary cycle and its global flag retain their existing behavior.
        owner=self
        class ManualReplyControl(type(cycle.reply_control)):
            def _provider_match(self,value,correlation):
                require(owner.repo.get_state('codex_assisted',job_id)==row,'assisted_manual_reply_binding')
                owner._manual_record(owner.repo.effect(job_id),row)
                return super()._provider_match(value,correlation)
        cycle.reply_control=ManualReplyControl(self.repo,cycle.reply_control.registry,external_pause_enabled=False)
        result=cycle.preview_reply(receipt,thread_id,message_id)
        require(self.repo.get_state('codex_assisted',job_id)==row and self.reader._binding(receipt)==binding,
                'assisted_manual_reply_binding')
        if not preview:
            # ReplyControl owns its transaction and revalidates provider correlation
            # inside it. An outer transaction is explicitly forbidden by that flow.
            result=cycle.receive_reply(receipt,thread_id,message_id,review=review)
        return {**result,'job_id':job_id,'receipt':receipt,'dispatch_authorized':False,
                'retry_write':False,'send_attempted':False,'external_writes':0}

    def manual_reply_preview(self,job_id,receipt,thread_id,message_id):
        return self._manual_reply(job_id,receipt,thread_id,message_id,preview=True)

    def manual_reply_confirm(self,job_id,receipt,thread_id,message_id,review):
        return self._manual_reply(job_id,receipt,thread_id,message_id,review)


def configured_assisted(service, *, ledger_reader=None, ledger_writer=None):
    from .touch_ledger import TouchLedger, AssistedLedger, assisted_writer_database
    enabled=(os.environ.get('THUNDER_OS_CODEX_ASSISTED_DISPATCH')=='true' and
             os.environ.get('THUNDER_EXTERNAL_EFFECTS_ENABLED')=='true')
    from .internal_canary import InternalCanaryService
    if ledger_writer is None and os.environ.get('THUNDER_OS_CODEX_ASSISTED_WRITER_ENABLED')=='true':
        ledger_writer=TouchLedger(db_context=assisted_writer_database)
    return CodexAssisted(InternalCanaryService(service),AssistedLedger(ledger_reader or TouchLedger(),ledger_writer),enabled=enabled,
        policy_loader=lambda:pinned_policy('THUNDER_OS_CODEX_ASSISTED_POLICY'))
