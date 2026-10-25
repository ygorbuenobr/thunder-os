"""Scoped Gmail connector for the existing worker, plus receipt/reply composition.

OFF by default. No scheduler, contact creation, new credentials or implicit send.
Worker owns the canonical touch reservation; the connector never reserves it twice.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time

from .models import payload_hash
from .seqd_client import SEQDClient, MAILBOX, MAILBOX_ID, WORKSPACE_ID, canonical
from .providers.result import ProviderResult
from .email_readback import GmailReceiptReader
from .contact_history import ContactHistoryReadonly
from .campaign_registry import CampaignRegistry
from .reply_control import ReplyControl
from .external_email_import import ExternalEmailImport, HubSpotExternalEmail, plan_email, _validate_plan


def require(ok, code):
    if not ok:
        raise ValueError(code)


def pinned_policy(prefix):
    """Only an explicit operator file/hash; never discover credentials or policies."""
    path = Path(os.environ.get(prefix+'_FILE',''))
    require(path.is_absolute() and path.is_file() and not path.is_symlink(), 'gmail_policy_unavailable')
    raw = path.read_bytes()
    require(len(raw) <= 16384 and hashlib.sha256(raw).hexdigest() == os.environ.get(prefix+'_SHA256'), 'gmail_policy_changed')
    def unique(pairs):
        result = {}
        for key,value in pairs:
            require(key not in result,'gmail_policy_duplicate');result[key]=value
        return result
    return json.loads(raw,object_pairs_hook=unique)


def _hash(value):
    return hashlib.sha256(canonical(value)).hexdigest()


class GmailCadenceAdapter:
    provider_name = 'gmail'
    account_id = MAILBOX

    def __init__(self, repo, *, client=None, enabled=False, policy_loader=None, clock=time.time):
        self.repo,self.client,self.enabled=repo,client or SEQDClient(),enabled
        self.policy_loader,self.clock=policy_loader,clock
        self.capability='configured' if enabled is True else 'disabled'

    def _policy(self):
        require(self.enabled is True,'gmail_cadence_disabled')
        require(callable(self.policy_loader),'gmail_policy_unavailable')
        p=self.policy_loader()
        fields={'version','approval_ref','mailbox','campaign_id','cadence_id','step_id','effect_key',
                'recipient','subject','content_hash','issued_at','expires_at','max_attempts'}
        require(isinstance(p,dict) and set(p)==fields and type(p['version']) is int and p['version']==1 and type(p['max_attempts']) is int and p['max_attempts']==1,'gmail_policy_invalid')
        require(p['mailbox']==MAILBOX and all(isinstance(p[k],str) and p[k] for k in
            ('approval_ref','campaign_id','cadence_id','step_id','effect_key','recipient','subject','content_hash')),'gmail_policy_scope')
        require(type(p['issued_at']) in (int,float) and type(p['expires_at']) in (int,float)
                and p['issued_at']<=self.clock()<p['expires_at'] and 0<p['expires_at']-p['issued_at']<=900,'gmail_policy_expired')
        return p

    def _scope(self,p,job):
        payload=job['payload'];step=payload['step']
        cadence=self.repo.get_state('cadence',p['cadence_id'])
        approval=self.repo.get_state('approval',p['cadence_id'])
        require(cadence and approval and cadence.get('status')=='approved' and approval.get('status')=='active','gmail_cadence_not_approved')
        context=cadence.get('execution_context',{})
        require(context.get('campaign_id')==p['campaign_id'] and payload.get('cadence_id')==p['cadence_id']
                and step.get('id')==p['step_id'] and step.get('channel')=='email'
                and job['effect_key']==p['effect_key'] and payload.get('approval_hash')==p['effect_key']
                and step.get('recipient')==p['recipient'] and step.get('subject')==p['subject']
                and payload_hash(step.get('content'))==p['content_hash'],'gmail_policy_scope_mismatch')
        require(not (self.repo.get_state('control','global') or {}).get('paused'),'gmail_global_pause')
        # Reuse the worker's canonical campaign and content authorization, not a caller flag.
        registry=CampaignRegistry(self.repo);binding=registry.binding(p['campaign_id'])
        require(registry.load(p['campaign_id'])==context and binding['providers'].get('email')=='gmail'
                and binding['provider_accounts'].get('email')==MAILBOX,'gmail_campaign_binding_changed')
        require(approval.get('step_hashes',{}).get(p['step_id'])==p['effect_key'],'gmail_approval_changed')
        return step,approval

    @staticmethod
    def _key(p):
        return payload_hash({'mailbox':MAILBOX,'cadence':p['cadence_id'],'step':p['step_id']})

    def prepare_job(self,job):
        """Durably consume one scope BEFORE worker's dispatch fence/provider call."""
        p=self._policy();step,approval=self._scope(p,job);key=self._key(p)
        require(getattr(self.repo._local,'connection',None) is None,'gmail_outer_transaction_blocked')
        with self.repo.transaction():
            require(self.repo.get_state('gmail_cadence_attempt',key) is None,'gmail_attempt_already_reserved')
            expires=min(p['expires_at'],self.clock()+300,datetime.fromisoformat(approval['expires_at'].replace('Z','+00:00')).timestamp())
            require(expires>self.clock(),'gmail_approval_expired')
            plan={'to':step['recipient'],'subject':step['subject'],'body':step['content'],'thread_id':None}
            preview='cadence-'+payload_hash({'job':job['id'],'policy':p})[:48]
            envelope={'preview_id':preview,'version':1,'content_hash':_hash({'mailbox':MAILBOX_ID,'workspace':WORKSPACE_ID,
                'preview_id':preview,'version':1,**plan}), 'approved_actor':'YGOR',
                'expires_at':datetime.fromtimestamp(expires,timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z'),
                'idempotency_key':_hash({'mailbox':MAILBOX_ID,'workspace':WORKSPACE_ID,**plan}),**plan}
            self.repo.append_state('gmail_cadence_attempt',key,{'job_id':job['id'],'policy':deepcopy(p),
                'envelope':envelope,'phase':'reserved','retry_write':False})

    def send(self,effect):
        p=self._policy();key=self._key(p);row=self.repo.get_state('gmail_cadence_attempt',key)
        require(row and row['phase']=='reserved' and row['policy']==p,'gmail_attempt_not_dispatchable')
        job=self.repo.effect(row['job_id'])
        require(job and job['state']=='pending' and job.get('lease_owner')
                and getattr(self.repo._local,'dispatch_fence',None)==(job['id'],job['lease_owner']),
                'gmail_dispatch_fence_required')
        step,_=self._scope(p,job)
        require(effect=={'target':step['recipient'],'subject':step['subject'],'content':step['content'],'effect_id':step['id']},'gmail_effect_mismatch')
        self.repo.put_state('gmail_cadence_attempt',key,{**row,'phase':'dispatch_started'})
        try:
            result=self.client.send(deepcopy(row['envelope']))
            confirmed=(result.get('mailbox')==MAILBOX and result.get('status')=='sent'
                and isinstance(result.get('provider_id'),str) and re.fullmatch(r'[a-f0-9]{1,64}',result['provider_id']))
            receipt='gmail:'+result['provider_id'] if confirmed else None
            self.repo.put_state('gmail_cadence_attempt',key,{**row,'phase':'sent' if confirmed else 'uncertain','receipt_ref':receipt})
            return ProviderResult(effect['effect_id'],'confirmed' if confirmed else 'uncertain',receipt)
        except Exception:
            self.repo.put_state('gmail_cadence_attempt',key,{**row,'phase':'uncertain'})
            return ProviderResult(effect['effect_id'],'uncertain',error_code='gmail_send_uncertain')

    def reconcile(self,cadence_id,step_id):
        """Read remote receipt only; never calls send and never reopens a claim."""
        key=payload_hash({'mailbox':MAILBOX,'cadence':cadence_id,'step':step_id})
        row=self.repo.get_state('gmail_cadence_attempt',key)
        require(row and row['phase'] in {'dispatch_started','uncertain','sent'},'gmail_attempt_not_started')
        result=self.client.reconcile(row['envelope']['idempotency_key'])
        return {'provider_result':result,'retry_write':False,'scope':'receipt_reconciliation_only'}


def gmail_source(value):
    require(isinstance(value,dict) and set(value)=={'provider','account_id','external_campaign_id','campaign_id','provider_id'},'gmail_source_invalid')
    require(value['provider']=='gmail' and value['account_id']==MAILBOX
            and value['external_campaign_id']==value['campaign_id']
            and isinstance(value['campaign_id'],str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}',value['campaign_id'])
            and isinstance(value['provider_id'],str) and re.fullmatch(r'[a-f0-9]{1,64}',value['provider_id']),'gmail_source_mismatch')
    return deepcopy(value)


def validate_gmail_plan(plan):
    require(plan.get('object_type')=='emails','gmail_contact_creation_forbidden')
    _validate_plan(plan,source_validator=gmail_source)


class ThunderEmailCycle:
    """Existing cadence receipt -> CRM log; verified reply -> existing reply control."""
    def __init__(self,repo,*,reader=None,client=None,crm=None,crm_enabled=False,authorize=None,reply_enabled=False,ledger=None,reconcile_enabled=False):
        self.repo=repo;self.client=client or SEQDClient();self.reader=reader or GmailReceiptReader(repo,self.client)
        self.authorize=authorize;self.reply_enabled=reply_enabled
        self.ledger=ledger;self.reconcile_enabled=reconcile_enabled
        self.importer=ExternalEmailImport(repo,crm,enabled=crm_enabled,authorize=self._authorize,plan_validator=validate_gmail_plan)
        self.reply_control=ReplyControl(repo,CampaignRegistry(repo),external_pause_enabled=False)

    def _reconciliation(self,cadence_id,step_id):
        require(self.reconcile_enabled is True,'gmail_reconciliation_disabled')
        key=payload_hash({'mailbox':MAILBOX,'cadence':cadence_id,'step':step_id})
        attempt=self.repo.get_state('gmail_cadence_attempt',key)
        require(attempt and attempt['phase'] in {'dispatch_started','uncertain','sent','confirmed'},'gmail_attempt_not_started')
        job=self.repo.effect(attempt['job_id']);cadence=self.repo.get_state('cadence',cadence_id)
        require(job and cadence and job['state'] in {'uncertain','needs_reconciliation','sent'}
                and not job.get('lease_owner') and job.get('error_code')!='duplicate_consumed_effect','gmail_job_not_reconcilable')
        step=job['payload']['step'];context=cadence['execution_context'];registry=CampaignRegistry(self.repo)
        binding=registry.binding(context['campaign_id'])
        require(job['payload']['cadence_id']==cadence_id and step['id']==step_id
                and registry.load(context['campaign_id'])==context and binding['providers'].get('email')=='gmail'
                and binding['provider_accounts'].get('email')==MAILBOX,'gmail_reconciliation_binding_changed')
        current=[s for s in cadence['steps'] if s['id']==step_id]
        require(len(current)==1 and all(current[0].get(k)==step.get(k) for k in ('channel','recipient','subject','content')),'gmail_reconciliation_step_changed')
        envelope=attempt['envelope'];policy=attempt['policy']
        require(policy['effect_key']==job['effect_key']==job['payload']['approval_hash']
                and policy['cadence_id']==cadence_id and policy['step_id']==step_id
                and policy['campaign_id']==context['campaign_id']
                and envelope['to']==step['recipient'] and envelope['subject']==step['subject']
                and envelope['body']==step['content'],'gmail_attempt_binding_changed')
        result=self.client.reconcile(envelope['idempotency_key'])
        require(result.get('mailbox')==MAILBOX and result.get('status')=='sent'
                and isinstance(result.get('provider_id'),str) and re.fullmatch(r'[a-f0-9]{1,64}',result['provider_id']), 'gmail_receipt_not_confirmed')
        receipt='gmail:'+result['provider_id']
        message=self.reader.read_message(receipt)
        business_hash=_hash({'mailbox':MAILBOX_ID,'workspace':WORKSPACE_ID,'to':envelope['to'],
            'subject':envelope['subject'],'body':envelope['body'],'thread_id':envelope.get('thread_id')})
        require(envelope['idempotency_key']==business_hash and policy['recipient']==step['recipient']
                and policy['subject']==step['subject'] and policy['content_hash']==payload_hash(step['content']),
                'gmail_attempt_scope_mismatch')
        message_id='<thunderos.'+business_hash+'@thundermustard.com>'
        require(result.get('message_id')==message_id==message.get('source_message_id')
                and message['to']==step['recipient'] and message['subject']==step['subject']
                and message['content']==step['content'],'gmail_reconciliation_evidence_mismatch')
        require(self.repo.effect(job['id'])==job and self.repo.get_state('cadence',cadence_id)==cadence
                and self.repo.get_state('gmail_cadence_attempt',key)==attempt
                and registry.load(context['campaign_id'])==context,'gmail_reconciliation_changed_during_read')
        proof={'job':job,'cadence':cadence,'attempt':attempt,'receipt_ref':receipt,'message':message}
        return key,proof

    def preview_reconciliation(self,cadence_id,step_id):
        _,proof=self._reconciliation(cadence_id,step_id)
        return {'review_hash':payload_hash(proof),'receipt_ref':proof['receipt_ref'],
                'effect_id':proof['job']['id'],'send_attempted':False,'scope':'historical_receipt_confirmation'}

    def confirm_reconciliation(self,cadence_id,step_id,review_hash):
        key,proof=self._reconciliation(cadence_id,step_id)
        require(payload_hash(proof)==review_hash,'gmail_reconciliation_review_changed')
        require(self.ledger is not None,'gmail_reconciliation_ledger_required')
        job=proof['job'];receipt=proof['receipt_ref'];cadence=proof['cadence']
        with self.repo.transaction():
            require(self.repo.effect(job['id'])==job and self.repo.get_state('cadence',cadence_id)==cadence
                    and self.repo.get_state('gmail_cadence_attempt',key)==proof['attempt']
                    and CampaignRegistry(self.repo).load(cadence['execution_context']['campaign_id'])==cadence['execution_context'],
                    'gmail_reconciliation_changed')
            # Ledger commits first. A crash afterwards is resumable with the same receipt,
            # never by releasing/requeueing the reservation or dispatching again.
            self.repo.validate_uncertain_receipt(job,receipt)
            stored=self.ledger.confirm_receipt(job['effect_key'],job['payload']['step'],receipt)
            require(stored['status']=='sent' and stored['receipt_ref']==receipt,'gmail_ledger_receipt_conflict')
            self.repo.confirm_uncertain_receipt(job,receipt)
            steps=[{**s,'status':'sent'} if s['id']==step_id and s.get('status') in {'uncertain','needs_reconciliation','failed'} else s for s in cadence['steps']]
            self.repo.put_state('cadence',cadence_id,{**cadence,'steps':steps})
            self.repo.put_state('gmail_cadence_attempt',key,{**proof['attempt'],'phase':'confirmed','receipt_ref':receipt})
        return {'status':'confirmed','receipt_ref':receipt,'send_attempted':False,'retry_write':False}

    def prepare_email(self,receipt):
        before=self.reader._binding(receipt)
        job,cadence,context,binding=before
        step=job['payload']['step'];contact=cadence.get('hubspot_contact_id')
        require(isinstance(contact,str) and re.fullmatch(r'[1-9][0-9]{0,29}',contact),'gmail_canonical_contact_required')
        event=self.reader.read_sent_event(receipt,{'mailbox':MAILBOX,'to':step['recipient'],'subject':step['subject'],'content':step['content']})
        require(self.reader._binding(receipt)==before,'gmail_receipt_binding_changed')
        event['source'].update(campaign_id=context['campaign_id'],external_campaign_id=context['campaign_id'])
        plan=plan_email(event,contact,source_validator=gmail_source)
        return plan

    def _authorize(self,plan):
        require(callable(self.authorize) and self.authorize(deepcopy(plan)) is True,'gmail_crm_authority_required')
        require(plan==self.prepare_email('gmail:'+plan['source']['provider_id']),'gmail_receipt_binding_changed')
        require(self.authorize(deepcopy(plan)) is True,'gmail_crm_authority_required')
        return True

    def log_email(self,plan):
        return self.importer.execute(plan)

    def preview_reply(self,receipt,thread_id,message_id):
        return self.receive_reply(receipt,thread_id,message_id,review=None,_preview=True)

    def receive_reply(self,receipt,thread_id,message_id,*,review,_preview=False):
        require(self.reply_enabled is True,'gmail_reply_ingest_disabled')
        # Review is an exact human approval of a provider-read message, not text supplied as evidence.
        require(_preview or isinstance(review,dict) and set(review)=={'actor','human_reply','message_hash'}
                and review['actor']=='ygor' and review['human_reply'] is True,'gmail_human_review_required')
        before=self.reader._binding(receipt)
        job,cadence,context,_=before
        outgoing=self.reader.read_sent_event(receipt,{'mailbox':MAILBOX,'to':job['payload']['step']['recipient'],
            'subject':job['payload']['step']['subject'],'content':job['payload']['step']['content']})
        response=self.client.conversation(thread_id);thread=response.get('thread',{})
        require(response.get('mailbox')==MAILBOX and thread.get('id')==thread_id,'gmail_thread_mismatch')
        events=[ContactHistoryReadonly._gmail_event(m,thread_id,{outgoing['lead_email']}) for m in thread.get('messages',[])]
        sent=[e for e in events if e and e['id']=='gmail:'+outgoing['source']['provider_id'] and e['direction']=='outgoing']
        incoming=[e for e in events if e and e['id']=='gmail:'+message_id and e['direction']=='incoming']
        require(len(sent)==1 and len(incoming)==1 and sent[0]['body_complete'] and incoming[0]['body_complete']
                and sent[0]['text']==outgoing['text'] and sent[0]['subject']==outgoing['subject'],'gmail_thread_evidence_incomplete')
        event=incoming[0]
        require(event['date'] and sent[0]['date'] and event['date']>=sent[0]['date']
                and (_preview or payload_hash(event)==review['message_hash']),'gmail_reply_review_mismatch')
        require(self.reader._binding(receipt)==before,'gmail_reply_binding_changed')
        if _preview:
            return {'message':event,'message_hash':payload_hash(event),'external_writes':0,'send_attempted':False}
        result=self.reply_control.ingest({'id':'gmail-'+message_id,'channel':'email','content':event['text'],
            'received_at':event['date'],'hubspot_contact_id':cadence['hubspot_contact_id'],
            'campaign_id':context['campaign_id'],'human_reply':True,'identity_verified':True,
            'source':{'provider':'gmail','account_id':MAILBOX,'provider_message_id':message_id}},'verified_source')
        current=self.repo.get_state('cadence',cadence['id'])
        return {**result,'cadence_readback':{'id':cadence['id'],'status':current.get('status'),
            'steps':deepcopy(current['steps'])},'external_writes':0,'send_attempted':False}


def configured_adapter(repo):
    if os.environ.get('THUNDER_OS_GMAIL_CADENCE_ENABLED')!='true':
        return GmailCadenceAdapter(repo)
    require(os.environ.get('THUNDER_EXTERNAL_EFFECTS_ENABLED')=='true','gmail_external_effects_disabled')
    return GmailCadenceAdapter(repo,enabled=True,policy_loader=lambda:pinned_policy('THUNDER_OS_GMAIL_CADENCE_POLICY'))


class ScopedHubSpotEmail(HubSpotExternalEmail):
    """Serialize local binding changes with the final authorization and CRM write."""
    def __init__(self,repo,*args,**kwargs):
        super().__init__(*args,**kwargs);self.repo=repo

    def write(self,plan):
        with self.repo.transaction():
            return super().write(plan)


def configured_cycle(repo):
    """Separate approval for CRM; enabling send never enables CRM or reply ingest."""
    from .hubspot_console import systemd_hubspot_credential
    def authorize(plan):
        from .email_canary import restricted_mode
        require(not restricted_mode(),'gmail_crm_restricted_mode')
        require(os.environ.get('THUNDER_EXTERNAL_EFFECTS_ENABLED')=='true','gmail_external_effects_disabled')
        require(not (repo.get_state('control','global') or {}).get('paused'),'gmail_global_pause')
        p=pinned_policy('THUNDER_OS_GMAIL_CRM_POLICY')
        require(set(p)=={'approval_ref','portal_id','contact_id','plan_hash','issued_at','expires_at'}
            and isinstance(p['approval_ref'],str) and p['approval_ref']
            and type(p['issued_at']) in (int,float) and type(p['expires_at']) in (int,float)
            and p['issued_at']<=time.time()<p['expires_at'] and 0<p['expires_at']-p['issued_at']<=900
            and p['portal_id']==plan['portal_id'] and p['contact_id']==plan['contact_id']
            and p['plan_hash']==plan['plan_hash'],'gmail_crm_policy_mismatch')
        return True
    enabled=os.environ.get('THUNDER_OS_GMAIL_CRM_ENABLED')=='true'
    port=ScopedHubSpotEmail(repo,systemd_hubspot_credential,enabled=enabled,authorize=authorize,plan_validator=validate_gmail_plan)
    from .touch_ledger import TouchLedger
    cycle=ThunderEmailCycle(repo,ledger=TouchLedger(),reconcile_enabled=os.environ.get('THUNDER_OS_GMAIL_RECONCILE_ENABLED')=='true',crm=port,crm_enabled=enabled,authorize=authorize,
        reply_enabled=os.environ.get('THUNDER_OS_GMAIL_REPLY_INGEST_ENABLED')=='true')
    port.authorize=cycle._authorize
    return cycle
