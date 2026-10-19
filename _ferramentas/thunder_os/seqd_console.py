from .operation_campaign import OperationCampaign
from .approvals import ApprovalError
"""Ygor-only, server-held email previews and durable one-attempt journal.

HTTP caller MUST verify the current confirm_nonce via its existing nonce gate.
No OAuth in this process. Reconcile is remote read-only even though HTTP POST.
"""
import hashlib
import os
from pathlib import Path
import re
import time
from datetime import datetime, timezone
from uuid import uuid4
from .seqd_client import SEQDClient, SEQDError, MAILBOX, MAILBOX_ID, WORKSPACE_ID, canonical
from .db import RepositoryConflict
from .email_ledger import SEQDEmailLedger
from .touch_ledger import TouchLedger, TouchBlocked, toques


def _hash(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _plan(body):
    if not isinstance(body,dict) or set(body)-{'to','subject','body','thread_id'} or not {'to','subject','body'}<=set(body):
        raise SEQDError('invalid_preview')
    recipient=body['to']
    if (not isinstance(recipient,str) or len(recipient)>254 or recipient!=recipient.strip()
            or not re.fullmatch(r'[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}',recipient)):
        raise SEQDError('invalid_recipient')
    subject,text=body['subject'],body['body']
    if (not isinstance(subject,str) or not subject.strip() or len(subject.encode('utf-8'))>512
            or any(ord(c)<32 or ord(c)==127 for c in subject)):
        raise SEQDError('invalid_subject')
    if (not isinstance(text,str) or not text.strip() or len(text.encode('utf-8'))>65536
            or any(ord(c)<32 and c not in '\r\n\t' for c in text) or '\x7f' in text):
        raise SEQDError('invalid_body')
    thread=body.get('thread_id')
    if thread is not None: SEQDClient._id(thread)
    return {'to':recipient.lower(),'subject':subject,'body':text,'thread_id':thread}


def canonical_ledger_readiness():
    """Metadata/SELECT only; never migrate, reserve or test-write a database."""
    if re.search(r'(?:default_)?transaction_read_only\s*=\s*(?:on|true|1)(?:\s|$)',os.environ.get('PGOPTIONS',''),re.I):
        return 'canonical_ledger_readonly'
    sqlite_path=os.environ.get('TOQUES_SQLITE','').strip()
    if sqlite_path and not Path(sqlite_path).is_file():
        return 'canonical_ledger_schema_unavailable'
    required={
        'toques':'id,canal,acao,pessoa_chave,alvo,status,provider_id,detalhe,agente,ref,atualizado_em',
        'respostas':'id,canal,pessoa_chave',
        'thunder_suppressions':'canal,pessoa_chave,reason',
        'thunder_effect_touches':'effect_key,touch_id',
    }
    try:
        with toques._banco() as (connection,marker):
            cursor=connection.cursor()
            if marker=='?':
                cursor.execute('PRAGMA query_only')
                if cursor.fetchone()[0]: return 'canonical_ledger_readonly'
            else:
                cursor.execute("SELECT current_setting('transaction_read_only')")
                if cursor.fetchone()[0]=='on': return 'canonical_ledger_readonly'
                for table in required:
                    cursor.execute('SELECT to_regclass(%s)',(table,))
                    if cursor.fetchone()[0] is None: return 'canonical_ledger_schema_unavailable'
                for table in ('toques','thunder_effect_touches'):
                    cursor.execute("SELECT has_table_privilege(%s,'INSERT'),has_table_privilege(%s,'UPDATE')",(table,table))
                    if not all(cursor.fetchone()): return 'canonical_ledger_readonly'
            for table,columns in required.items():
                cursor.execute('SELECT '+columns+' FROM '+table+' LIMIT 0')
        return None
    except Exception:
        return 'canonical_ledger_schema_unavailable'


class SEQDConsole:
    def __init__(self,repository,transport=None,*,read_enabled=False,send_enabled=False,clock=time.time,ledger=None,dispatch_authorized=None):
        self.repo,self.port=repository,transport or SEQDClient()
        self.ledger=ledger if ledger is not None else SEQDEmailLedger()
        self.dispatch_authorized=dispatch_authorized
        self.read_enabled,self.send_enabled,self.clock=read_enabled,send_enabled,clock

    def _send_allowed(self):
        control=self.repo.get_state('control','global')
        return (self.send_enabled is True and not (control and control.get('paused'))
                and (self.dispatch_authorized is None or self.dispatch_authorized() is True))

    def _ledger_readiness(self):
        if isinstance(self.ledger,SEQDEmailLedger):
            return self.ledger.readiness()
        return canonical_ledger_readiness()

    def _actor(self,actor):
        if actor!='ygor': raise SEQDError('human_authority_required')
        if self.read_enabled is not True: raise SEQDError('seqd_not_configured')

    def status(self,actor):
        self._actor(actor)
        try:
            result=self.port.status()
            if result.get('mailbox')!=MAILBOX or result.get('status')!='ready': raise SEQDError('seqd_unavailable')
            blocker=self._ledger_readiness()
            return {'mailbox':MAILBOX,'status':'ready','read_verified':True,'blockers':[blocker] if blocker else [],
                    'send_available':self._send_allowed() and result.get('send_enabled') is True and blocker is None,
                    'send_enabled':self._send_allowed() and result.get('send_enabled') is True and blocker is None,'review_required':True}
        except Exception:
            return {'mailbox':MAILBOX,'status':'unavailable','read_verified':False,'send_available':False,'send_enabled':False,'review_required':True}

    def messages(self,actor,limit=10,cursor=None):
        self._actor(actor)
        if type(limit) is not int or not 1<=limit<=10: raise SEQDError('invalid_limit')
        return self.port.messages(limit,cursor)

    def read(self,identifier,actor):
        self._actor(actor); SEQDClient._id(identifier)
        return self.port.read(identifier)

    def read_preview(self,key,actor):
        self._actor(actor); SEQDClient._id(key)
        preview=self.repo.get_state('seqd_preview',key)
        if not preview: raise SEQDError('preview_not_found')
        blocker=self._ledger_readiness()
        preview={**preview,'send_enabled':self._send_allowed() and blocker is None,'blockers':[blocker] if blocker else []}
        if preview.get('effect_key'):
            preview={**preview,'result':self.repo.get_state('seqd_attempt',preview['effect_key'])}
        return preview

    def prepare(self,body,actor):
        self._actor(actor)
        plan=_plan(body); key=uuid4().hex
        blocker=self._ledger_readiness()
        snapshot={'mailbox':MAILBOX_ID,'workspace':WORKSPACE_ID,'preview_id':key,'version':1,**plan}
        return self.repo.append_state('seqd_preview',key,{'plan':plan,'content_hash':_hash(snapshot),
            'expires_at':self.clock()+300,'state':'review_required','mailbox':MAILBOX,'created_by':actor,'send_enabled':self._send_allowed() and blocker is None,'blockers':[blocker] if blocker else []})

    @staticmethod
    def _result(result):
        if (isinstance(result,dict) and result.get('mailbox')==MAILBOX and result.get('status')=='sent'
                and isinstance(result.get('provider_id'),str) and re.fullmatch(r'[A-Za-z0-9_-]{1,200}',result['provider_id'])):
            return {'status':'sent','receipt_ref':'gmail:'+result['provider_id'],'retry_write':False}
        return {'status':'uncertain','error_code':'requires_reconciliation','retry_write':False}

    def campaign_gate(self):
        return OperationCampaign(self.repo, 'seqd', 'seqd', WORKSPACE_ID + ":" + MAILBOX_ID, 'email', clock=self.clock)

    def campaign_review(self, body, actor):
        self._actor(actor)
        if not isinstance(body, dict) or set(body) != {'preview_id', 'campaign_id'}:
            raise SEQDError('campaign_review_invalid')
        return self.campaign_gate().review(body['preview_id'], body['campaign_id'])

    def execute(self,body,actor,*,dispatch_guard=None,dispatch_expires_at=None):
        self._actor(actor)
        if not isinstance(body, dict) or 'execution_confirmation' not in body:
            raise SEQDError('campaign_confirmation_required')
        gate = self.campaign_gate()
        try:
            campaign_record = gate.approve(body, actor)
        except ApprovalError as exc:
            raise SEQDError(str(exc)) from None
        body = {k:v for k,v in body.items() if k != 'execution_confirmation'}
        from .email_canary import restricted_mode, EmailCanary
        if restricted_mode():
            guard=getattr(dispatch_guard,'__self__',None)
            if (type(guard) is not EmailCanary or getattr(dispatch_guard,'__func__',None) is not EmailCanary.before_dispatch
                    or Path(guard.repo.path).resolve()!=Path(self.repo.path).resolve()
                    or dispatch_expires_at!=guard.policy['expires_at']):
                raise SEQDError('canary_authority_required')
        if not self._send_allowed():
            if restricted_mode():
                return {'status':'blocked','error_code':'seqd_sends_disabled_or_paused','retry_write':False}
            raise SEQDError('seqd_sends_disabled_or_paused')
        if (not isinstance(body,dict) or set(body)!={'preview_id','expected_version','content_hash','confirm_nonce'}
                or type(body.get('expected_version')) is not int
                or not isinstance(body.get('confirm_nonce'),str) or not 16<=len(body['confirm_nonce'])<=256):
            raise SEQDError('exact_review_required')
        SEQDClient._id(body['preview_id'])
        blocker=self._ledger_readiness()
        if blocker: raise SEQDError(blocker)
        with self.repo.transaction():
            preview=self.repo.get_state('seqd_preview',body['preview_id'])
            if not preview or preview['version']!=body['expected_version'] or preview['content_hash']!=body['content_hash']:
                raise RepositoryConflict('preview_changed')
            if preview['state']!='review_required' or preview['expires_at']<=self.clock(): raise SEQDError('preview_not_current')
            plan=_plan(preview['plan'])
            snapshot={'mailbox':MAILBOX_ID,'workspace':WORKSPACE_ID,'preview_id':preview['id'],'version':preview['version'],**plan}
            if _hash(snapshot)!=preview['content_hash']: raise RepositoryConflict('preview_changed')
            effect=_hash({'mailbox':MAILBOX_ID,'workspace':WORKSPACE_ID,**plan})
            existing=self.repo.get_state('seqd_attempt',effect)
            if existing:
                self.repo.put_state('seqd_preview',preview['id'],{**preview,'state':'attempted','effect_key':effect})
                return {'status':existing.get('status','uncertain'),'retry_write':False,'replayed':True,'receipt_ref':existing.get('receipt_ref')}
            envelope={'preview_id':preview['id'],'version':preview['version'],'content_hash':preview['content_hash'],
                'approved_actor':'YGOR','expires_at':datetime.fromtimestamp(preview['expires_at'],timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z'),
                'idempotency_key':effect,**plan}
            if dispatch_expires_at is not None:
                envelope['expires_at']=datetime.fromtimestamp(min(preview['expires_at'],dispatch_expires_at),timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
            self.repo.append_state('seqd_attempt',effect,{'status':'uncertain','preview_id':preview['id'],'retry_write':False})
            self.repo.put_state('seqd_preview',preview['id'],{**preview,'state':'attempted','effect_key':effect})
        dispatch_started=False
        try:
            # Reuse legacy email identity/subject dedupe and shared person lock.
            # No reserve/migrate on preview; missing ledger fails closed here.
            created,touch=self.ledger.reserve(effect,preview['id'],{
                'id':preview['id'],'channel':'email','recipient':plan['to'],
                'subject':plan['subject'],'content':plan['body'],
                'mailbox_id':MAILBOX_ID,'approval_hash':preview['content_hash']})
            if not created:
                result={'status':'blocked','error_code':'canonical_touch_exists','retry_write':False}
            else:
                with self.repo.transaction():
                    current=self.repo.get_state('seqd_attempt',effect)
                    self.repo.put_state('seqd_attempt',effect,{**current,'canonical_owned':True})
                if isinstance(self.ledger,SEQDEmailLedger):
                    self.ledger.validate(effect)
                else:
                    # Explicitly injected legacy fixture uses the same live checks.
                    self.ledger.preflight(effect,{'channel':'email','recipient':plan['to'],
                        'subject':plan['subject']},raise_blocked=True)
                if not self._send_allowed():
                    raise SEQDError('seqd_sends_disabled_or_paused')
                dispatch_error = None
                with gate.execution_guard(campaign_record, 'seqd_attempt', effect):
                    if dispatch_guard is not None and dispatch_guard(envelope) is not True:
                        raise SEQDError('dispatch_authority_denied')
                    if not self._send_allowed():
                        raise SEQDError('seqd_sends_disabled_or_paused')
                    dispatch_started=True
                    try:
                        result=self._result(self.port.send(envelope))
                    except Exception as exc:
                        # Commit one-shot/canary markers even after a lost response.
                        dispatch_error = exc
                        result=self._result(None)
                    current=self.repo.get_state('seqd_attempt',effect)
                    self.repo.put_state('seqd_attempt',effect,{**current,**result})
                if dispatch_error is not None:
                    raise dispatch_error
                self.ledger.finish(effect,'sent' if result['status']=='sent' else 'uncertain',result.get('receipt_ref'))
        except TouchBlocked:
            result=(self._result(None) if dispatch_started else
                    {'status':'blocked','error_code':'canonical_contact_blocked','retry_write':False})
        except Exception:
            result=self._result(None)
        with self.repo.transaction():
            # Never downgrade a read-confirmed receipt if reconciliation won a race.
            current=self.repo.get_state('seqd_attempt',effect)
            if current.get('status')!='sent': self.repo.put_state('seqd_attempt',effect,{**current,**result})
            else: result={k:current[k] for k in ('status','receipt_ref','retry_write')}
        return result

    def reconcile(self,body,actor):
        self._actor(actor)
        if not isinstance(body,dict) or set(body)!={'preview_id'}: raise SEQDError('invalid_reconciliation')
        SEQDClient._id(body['preview_id'])
        preview=self.repo.get_state('seqd_preview',body['preview_id'])
        if not preview or not preview.get('effect_key'): raise SEQDError('no_attempt_to_reconcile')
        effect=preview['effect_key']
        attempt=self.repo.get_state('seqd_attempt',effect)
        if not attempt.get('canonical_owned'):
            return {'status':attempt['status'],'error_code':'canonical_reconciliation_required','retry_write':False}
        try:
            result=self._result(self.port.reconcile(effect))
            if result['status']=='sent': self.ledger.finish(effect,'sent',result['receipt_ref'])
        except Exception: result=self._result(None)
        with self.repo.transaction():
            previous=self.repo.get_state('seqd_attempt',effect)
            if previous['status']=='sent': return {k:previous[k] for k in ('status','receipt_ref','retry_write')}
            self.repo.put_state('seqd_attempt',effect,{**previous,**result})
        return result
