from .operation_campaign import OperationCampaign
from .approvals import ApprovalError
"""Offline copilot console; explicit client and canonical ledger, no activation."""
import json
import math
import re
import time
from datetime import datetime,timedelta,timezone
from uuid import uuid4
from .approvals import ApprovalService,ApprovalError
from .touch_ledger import TouchBlocked
from .connectsafely_client import ConnectSafelyError as ProviderError, canonical_recipient_id
from .db import RepositoryConflict
from .models import payload_hash
from .connectsafely_coordination import CoordinationError


class ConnectSafelyError(ValueError):pass

SAFE_CODES=frozenset({'campaign_confirmation_integration_required','social_attempt_invalid','social_backend_unsupported','social_backfill_review_required','social_capability_scope_invalid','social_capability_ttl_invalid','social_inventory_changed','social_inventory_incomplete','social_inventory_invalid','social_request_invalid','social_reservation_unverified','social_schema_changed','social_transaction_already_open','social_writer_artifact_changed','social_writer_artifact_invalid','social_writer_inventory_changed','social_writer_protocol_unverified','social_writer_unknown','social_attempt_fenced','social_inventory_unavailable','social_inventory_unverified','social_schema_unverified','social_capability_scope_mismatch','dispatch_guard_denied','reservation_not_safely_reusable','social_coordination_unverified','provider_unauthorized','provider_forbidden','provider_rate_limited','provider_rejected',
    'provider_not_found','provider_outcome_unknown','provider_schema_invalid','quota_contract_unverified','quota_exhausted',
    'write_entitlement_unverified','account_identity_mismatch','account_identity_unverified','account_not_ready','recipient_identity_mismatch',
    'conversation_identity_mismatch','transport_unconfigured','invite_receipt_unavailable','receipt_unverified',
    'receipt_missing_or_ambiguous','participants_unverified','conversation_unresolved','writes_disabled','has_reply',
    'comment_parent_receipt_unavailable','reply_parent_receipt_unavailable','social_receipt_unavailable',
    'reaction_receipt_unavailable','post_identity_mismatch','post_author_unverified','parent_author_unverified','post_author_changed',
    'history_evidence_incomplete','history_incomplete','pagination_unverified','parent_comment_unverified',
    'reaction_identity_unverified','owner_identity_unconfigured','existing_comment','existing_reaction','tagging_unsupported',
    'legacy_lookup_incomplete','has_reply_or_suppressed','suppressed','touch_not_dispatchable','current_approval_changed','dispatch_already_authorized','dispatch_callback_not_used'})

def _error(value,default):
    return value if isinstance(value,str) and value in SAFE_CODES else default

def _exception_code(exc,default):
    if isinstance(exc,ApprovalError):
        return {'global pause active':'global_pause','approval expired':'approval_expired'}.get(str(exc),default)
    if isinstance(exc,(ProviderError,ConnectSafelyError,TouchBlocked,CoordinationError)):
        return _error(str(exc),default)
    return default


class ConnectSafelyConsole:
    def __init__(self,repository,client,ledger,*,account_id,account_public_id,writes_enabled=False,clock=time.time):
        self.repo,self.client,self.ledger=repository,client,ledger
        self.account_id,self.account_public_id=account_id,account_public_id
        self.writes_enabled,self.clock=writes_enabled,clock
        self.approvals=ApprovalService(lambda: bool((self.repo.get_state('control','global') or {}).get('paused')))

    def _actor(self,actor):
        if actor!='ygor':raise ConnectSafelyError('human_authority_required')

    def _identity(self):
        result=self.client.verify_identity()
        if (not isinstance(result,dict) or result.get('verified') is not True or result.get('account_id')!=self.account_id
            or result.get('account_public_id')!=self.account_public_id):raise ConnectSafelyError('account_identity_unverified')

    def _plan(self,body):
        fields={'account_id','account_public_id','action','recipient_public_id','recipient_profile_urn','channel','messaging_channel','content','conversation_urn'}
        if not isinstance(body,dict) or set(body)-fields or fields-{'conversation_urn'}-set(body):raise ConnectSafelyError('invalid_plan')
        plan={**body,'conversation_urn':body.get('conversation_urn')}
        if (plan['account_id']!=self.account_id or plan['account_public_id']!=self.account_public_id
            or plan['action'] not in {'dm','invite'} or plan['channel']!='linkedin' or plan['messaging_channel']!='linkedin_inbox'):
            raise ConnectSafelyError('unsupported_scope')
        try:plan['recipient_public_id']=canonical_recipient_id(plan['recipient_public_id'])
        except ProviderError:raise ConnectSafelyError('invalid_recipient') from None
        if not isinstance(plan['recipient_profile_urn'],str) or not re.fullmatch(r'urn:li:(?:fsd_profile|fs_miniProfile|member):[A-Za-z0-9_-]{1,200}',plan['recipient_profile_urn']):raise ConnectSafelyError('invalid_recipient_urn')
        if plan['conversation_urn'] is not None and (not isinstance(plan['conversation_urn'],str) or not re.fullmatch(r'urn:li:[A-Za-z0-9_:(),-]{1,400}',plan['conversation_urn'])):raise ConnectSafelyError('invalid_conversation')
        if plan['action']=='invite' and plan['conversation_urn'] is not None:raise ConnectSafelyError('invite_has_no_thread')
        content=plan['content']
        if not isinstance(content,str) or '\x00' in content or len(content)> (300 if plan['action']=='invite' else 8000) or (plan['action']=='dm' and not content.strip()):raise ConnectSafelyError('invalid_content')
        return plan

    def _step(self,plan,preview_id,version):
        return {'id':preview_id,'recipient':'https://www.linkedin.com/in/'+plan['recipient_public_id'],
                'channel':'linkedin','content':json.dumps({'intent':plan,'version':version},sort_keys=True,separators=(',',':'),ensure_ascii=False),
                'order':1,'interval_hours':0}

    def _canonical_step(self,step,plan):
        return {**step,'action':plan['action'],'content':plan['content'],
                'messaging_channel':plan['messaging_channel'],'conversation_urn':plan['conversation_urn']}

    def _check_additional_targets(self,plan):
        pass

    def _send(self,plan,before_dispatch,execution_context=None):
        return self.client.send(plan,before_dispatch=before_dispatch)

    def _reconcile(self,plan,attempted_at):
        return self.client.reconcile(plan,attempted_at)

    def _open_attempt(self,effect,plan,attempt_token,touch_id):
        pass

    def _retire_preflight(self,effect,attempt):
        pass

    def _event(self,effect,attempt,phase):
        self.repo.append_state('connectsafely_attempt_history',effect+':'+attempt['attempt_token']+':'+phase,
            {'effect_key':effect,'attempt_token':attempt['attempt_token'],'phase':phase,'at':self.clock(),
             'preview_id':attempt['preview_id'],'approval_hash':attempt['approval_hash']})

    def prepare(self,body,actor=None):
        self._actor(actor);plan=self._plan(body);self._identity()
        key=uuid4().hex
        return self.repo.append_state('connectsafely_preview',key,{'plan':plan,'review_hash':payload_hash({'plan':plan,'version':1}),
            'expires_at':self.clock()+300,'state':'review_required'})

    def read_preview(self,key,actor=None):
        self._actor(actor)
        item=self.repo.get_state('connectsafely_preview',key)
        if not item:raise ConnectSafelyError('preview_missing')
        result=self.repo.get_state('connectsafely_attempt',item['effect_key']) if item.get('effect_key') else None
        if result and item.get('attempt_token') and result.get('attempt_token')!=item['attempt_token']:
            result=self.repo.get_state('connectsafely_attempt_archive',item['effect_key']+':'+item['attempt_token'])
        return {**item,'result':result}

    @staticmethod
    def _result(result):
        extra={}
        observation=result.get('invitation_observation') if isinstance(result,dict) else None
        if isinstance(observation,dict):
            fields={'account_id':str,'recipient_public_id':str,'recipient_profile_urn':str,
                    'relationship_status':str,'observed_at':(int,float),'invitation_id':str,
                    'invitation_urn':str,'sent_at':(int,float),'complete_history':bool}
            required={'account_id','recipient_public_id','recipient_profile_urn','relationship_status','observed_at'}
            if (required<=set(observation)<=set(fields)
                    and observation['relationship_status'] in {'NOT_CONNECTED','INVITATION_SENT','INVITATION_RECEIVED','CONNECTED'}
                    and all((type(v) in fields[k] if isinstance(fields[k],tuple) else type(v) is fields[k])
                            and (len(v)<=512 if isinstance(v,str) else math.isfinite(v)) for k,v in observation.items())):
                extra={'invitation_observation':dict(observation)}
        if isinstance(result,dict) and result.get('status')=='sent' and isinstance(result.get('receipt_ref'),str) and re.fullmatch(r'[A-Za-z0-9_:(),/.=-]{1,512}',result['receipt_ref']):
            return {'status':'sent','receipt_ref':result['receipt_ref'],'retry_write':False,
                    'confirmation_kind':result.get('confirmation_kind') if result.get('confirmation_kind') in {'provider_receipt','observed_outbound'} else 'unclassified',
                    'write_attribution_confirmed':result.get('write_attribution_confirmed') is True and result.get('confirmation_kind')=='provider_receipt',**extra}
        if isinstance(result,dict) and result.get('status')=='blocked':return {'status':'blocked','retry_write':False,'error_code':_error(result.get('error_code'),'provider_denied'),**extra}
        return {'status':'uncertain','retry_write':False,'error_code':_error(result.get('error_code') if isinstance(result,dict) else None,'requires_reconciliation'),**extra}

    def campaign_gate(self):
        return OperationCampaign(self.repo, 'connectsafely', 'connectsafely', self.account_id, 'linkedin', clock=self.clock)

    def campaign_review(self, body, actor):
        self._actor(actor)
        if not isinstance(body, dict) or set(body) != {'preview_id', 'campaign_id'}:
            raise ConnectSafelyError('campaign_review_invalid')
        return self.campaign_gate().review(body['preview_id'], body['campaign_id'])

    def execute(self,body,actor=None,*,dispatch_guard=None):
        self._actor(actor)
        if not isinstance(body, dict) or 'execution_confirmation' not in body:
            raise ConnectSafelyError('campaign_confirmation_required')
        gate = self.campaign_gate()
        try:
            campaign_record = gate.approve(body, actor)
        except ApprovalError as exc:
            raise ConnectSafelyError(str(exc)) from None
        body = {k:v for k,v in body.items() if k != 'execution_confirmation'}
        if self.writes_enabled is not True or self.ledger is None:raise ConnectSafelyError('writes_disabled_or_ledger_missing')
        if not isinstance(body,dict) or set(body)!={'preview_id','expected_version','review_hash'} or type(body.get('expected_version')) is not int:raise ConnectSafelyError('exact_review_required')
        self._identity()
        with self.repo.transaction():
            preview=self.repo.get_state('connectsafely_preview',body['preview_id'])
            if not preview or preview['version']!=body['expected_version'] or preview['review_hash']!=body['review_hash']:raise RepositoryConflict('preview_changed')
            if preview['state']!='review_required' or preview['expires_at']<=self.clock():raise ConnectSafelyError('preview_expired_or_consumed')
            plan=self._plan(preview['plan'])
            if preview['review_hash']!=payload_hash({'plan':plan,'version':preview['version']}):raise RepositoryConflict('preview_changed')
            step=self._step(plan,preview['id'],preview['version']);now=datetime.fromtimestamp(self.clock(),timezone.utc)
            pack=self.approvals.approve_pack(preview['id'],preview['version'],[step],actor,datetime.fromtimestamp(preview['expires_at'],timezone.utc),now)
            authorization=self.approvals.authorize_step(step,pack,now)
            effect=payload_hash(plan)
            old=self.repo.get_state('connectsafely_attempt',effect)
            reusable=bool(old and old.get('phase')=='recoverable_not_dispatched' and old.get('canonical_owned') and not old.get('receipt_ref'))
            if old and not reusable:
                self.repo.put_state('connectsafely_preview',preview['id'],{**preview,'state':'attempted','effect_key':effect,'attempt_token':old.get('attempt_token')})
                return {**old,'retry_write':False,'replayed':True}
            token=uuid4().hex
            claimed=self.repo.put_state('connectsafely_preview',preview['id'],{**preview,'state':'attempted','effect_key':effect,'attempt_token':token})
            attempt={'status':'uncertain','phase':'preflight','attempt_token':token,'preview_id':preview['id'],
                'attempted_at':self.clock(),'approval_hash':authorization.effect_hash,'plan':plan,
                'canonical_owned':False,'retry_write':False}
            if old:self.repo.put_state('connectsafely_attempt',effect,attempt)
            else:self.repo.append_state('connectsafely_attempt',effect,attempt)
            self._event(effect,attempt,'preflight')
        canonical_step=self._canonical_step(step,plan)
        dispatched=False
        try:
            if reusable:
                created,touch=self.ledger.resume_not_dispatched(effect,old['canonical_touch_id'],old['canonical_ref'],preview['id'],canonical_step)
            else:created,touch=self.ledger.reserve(effect,preview['id'],canonical_step)
            if not created:result={'status':'blocked','error_code':'canonical_duplicate_or_ambiguous','retry_write':False}
            else:
                with self.repo.transaction():
                    current=self.repo.get_state('connectsafely_attempt',effect)
                    if current.get('attempt_token')!=token or current['phase']!='preflight':raise RepositoryConflict('attempt_changed')
                    self.repo.put_state('connectsafely_attempt',effect,{**current,'canonical_owned':True,'canonical_touch_id':str(touch['id']),'canonical_ref':preview['id']})
                execution_context=self._open_attempt(effect,plan,token,str(touch['id']))
                if execution_context is not None:
                    with self.repo.transaction():
                        current=self.repo.get_state('connectsafely_attempt',effect)
                        if current.get('attempt_token')!=token:raise RepositoryConflict('attempt_changed')
                        self.repo.put_state('connectsafely_attempt',effect,{**current,'coordination_issued':True})
                def before_dispatch():
                    nonlocal dispatched
                    if dispatched:raise ConnectSafelyError('dispatch_already_authorized')
                    if self.writes_enabled is not True:raise ConnectSafelyError('writes_disabled')
                    with self.repo.transaction():
                        current=self.repo.get_state('connectsafely_preview',preview['id'])
                        attempt=self.repo.get_state('connectsafely_attempt',effect)
                        if (not current or current['version']!=claimed['version'] or current['state']!='attempted'
                            or current.get('effect_key')!=effect or current['review_hash']!=preview['review_hash']
                            or current['plan']!=plan or not attempt or attempt['status']!='uncertain'
                            or attempt.get('attempt_token')!=token or attempt.get('phase')!='dispatch_started'
                            or attempt.get('approval_hash')!=authorization.effect_hash or not attempt.get('canonical_owned')):
                            raise ConnectSafelyError('current_approval_changed')
                        self._plan(current['plan'])
                        self.approvals.authorize_step(step,pack,datetime.fromtimestamp(self.clock(),timezone.utc))
                        reply=self.repo.get_state('connectsafely_sender',payload_hash({'account':self.account_id,'sender_urn':plan['recipient_profile_urn']}))
                        if reply and reply.get('has_reply') is True:raise ConnectSafelyError('has_reply')
                        self.ledger.validate(effect,canonical_step)
                        self._check_additional_targets(plan)
                        if dispatch_guard is not None and dispatch_guard() is not True:raise ConnectSafelyError('dispatch_guard_denied')
                        started={**attempt,'attempted_at':self.clock(),'phase':'dispatch_started'}
                        self.repo.put_state('connectsafely_attempt',effect,started)
                        self._event(effect,started,'dispatch_started')
                    dispatched=True
                    return True
                # Durable pessimistic send marker before the configuration fence.
                # A crash or lost response cannot be mistaken for zero dispatch.
                with self.repo.transaction():
                    current=self.repo.get_state('connectsafely_attempt',effect)
                    if current.get('attempt_token') != token: raise RepositoryConflict('attempt_changed')
                    self.repo.put_state('connectsafely_attempt',effect,{**current,'phase':'dispatch_started'})
                with gate.execution_guard(campaign_record, 'connectsafely_attempt', effect):
                    result=self._result(self._send(plan,before_dispatch,execution_context))
                if not dispatched:raise ConnectSafelyError('dispatch_callback_not_used')
                stored=self.ledger.finish(effect,'sent' if result['status']=='sent' else 'uncertain',result.get('receipt_ref'))
                if stored['status']=='sent':result={**result,'status':'sent','receipt_ref':stored['receipt_ref'],'retry_write':False}
        except Exception as exc:
            result={'status':'uncertain' if dispatched else 'blocked','error_code':_exception_code(exc,'requires_reconciliation' if dispatched else 'preflight_blocked'),'retry_write':False}
            # Only a completed, typed preflight denial proves zero dispatch.
            # Unknown exceptions, contract violations and process crashes do not.
            known=(isinstance(exc,(ProviderError,ConnectSafelyError,ApprovalError,TouchBlocked,CoordinationError))
                   and result['error_code'] not in {'preflight_blocked','requires_reconciliation','dispatch_callback_not_used'})
            with self.repo.transaction():
                current=self.repo.get_state('connectsafely_attempt',effect)
                if current.get('attempt_token')!=token:raise RepositoryConflict('attempt_changed')
                if current.get('phase')=='dispatch_started':result['status']='uncertain'
                elif known and current.get('canonical_owned') and current.get('phase')=='preflight':
                    closed={**current,'phase':'not_dispatched','zero_dispatch_proof':'typed_preflight_returned_before_callback'}
                    self.repo.put_state('connectsafely_attempt',effect,closed)
                    self._event(effect,closed,'not_dispatched')
                elif not known and result['status']!='sent':result['status']='uncertain'
        return self._store(effect,result,attempt_token=token)

    def _store(self,effect,result,*,attempt_token=None):
        with self.repo.transaction():
            old=self.repo.get_state('connectsafely_attempt',effect)
            if attempt_token is not None and old.get('attempt_token')!=attempt_token:raise RepositoryConflict('attempt_changed')
            if old['status']=='sent':return old
            stored=self.repo.put_state('connectsafely_attempt',effect,{**old,**result})
        return {**result,'phase':stored.get('phase'),'attempt_token':stored.get('attempt_token'),
                'attempt_version':stored['version'],'recovery_available':stored.get('phase')=='not_dispatched'}

    def recover_not_dispatched(self,body,actor=None):
        self._actor(actor)
        if not isinstance(body,dict) or set(body)!={'preview_id','expected_attempt_version'} or type(body.get('expected_attempt_version')) is not int:
            raise ConnectSafelyError('exact_recovery_review_required')
        with self.repo.transaction():
            preview=self.repo.get_state('connectsafely_preview',body['preview_id'])
            effect=preview.get('effect_key') if preview else None
            attempt=self.repo.get_state('connectsafely_attempt',effect) if effect else None
            if not attempt or attempt['version']!=body['expected_attempt_version'] or preview.get('attempt_token')!=attempt.get('attempt_token'):
                raise RepositoryConflict('recovery_attempt_changed')
            if (attempt.get('phase')!='not_dispatched' or attempt.get('status')!='blocked' or not attempt.get('canonical_owned')
                or attempt.get('receipt_ref') or attempt.get('zero_dispatch_proof')!='typed_preflight_returned_before_callback'):
                raise ConnectSafelyError('attempt_not_safely_recoverable')
            plan=attempt['plan']
            step=self._canonical_step(self._step(plan,attempt['preview_id'],1),plan)
            self.ledger.check_not_dispatched(effect,attempt['canonical_touch_id'],attempt['canonical_ref'],step)
            self._retire_preflight(effect,attempt)
            recovered={**attempt,'phase':'recoverable_not_dispatched','recovered_by':actor,'recovered_at':self.clock()}
            self.repo.append_state('connectsafely_attempt_archive',effect+':'+attempt['attempt_token'],recovered)
            stored=self.repo.put_state('connectsafely_attempt',effect,recovered)
            self._event(effect,stored,'recoverable_not_dispatched')
            return {'status':'recoverable_not_dispatched','requires_new_approval':True,'retry_write':False,'attempt_version':stored['version']}

    def reconcile(self,body,actor=None,*,reconciliation_guard=None):
        self._actor(actor)
        if not isinstance(body,dict) or set(body)!={'preview_id'}:raise ConnectSafelyError('invalid_reconciliation')
        preview=self.read_preview(body['preview_id'],actor);attempt=preview['result']
        if not attempt or not attempt.get('canonical_owned') or attempt.get('phase')!='dispatch_started':raise ConnectSafelyError('no_owned_dispatched_attempt')
        if (attempt.get('preview_id')!=preview['id'] or attempt.get('attempt_token')!=preview.get('attempt_token')
                or attempt.get('plan')!=preview['plan'] or payload_hash(preview['plan'])!=preview.get('effect_key')):
            raise RepositoryConflict('attempt_changed')
        if attempt.get('status')=='sent':return attempt
        try:
            self._identity();result=self._result(self._reconcile(attempt['plan'],attempt['attempted_at']))
        except Exception:result=self._result(None)
        # No provider reads hold the local write lock. Recheck the exact attempt
        # after IO, before allowing an observed receipt to finish its owned touch.
        with self.repo.transaction():
            current=self.repo.get_state('connectsafely_attempt',preview['effect_key'])
            current_preview=self.repo.get_state('connectsafely_preview',preview['id'])
            if (not current or not current_preview or current['version']!=attempt['version']
                    or current.get('attempt_token')!=attempt['attempt_token']
                    or current_preview['version']!=preview['version']
                    or current_preview.get('attempt_token')!=attempt['attempt_token']):
                raise RepositoryConflict('attempt_changed')
            if reconciliation_guard is not None and reconciliation_guard() is not True:
                raise RepositoryConflict('attempt_changed')
            if result['status']=='sent':
                if attempt['plan']['action']=='invite':
                    step=self._canonical_step(self._step(attempt['plan'],preview['id'],1),attempt['plan'])
                    self.ledger.reconcile_invitation(preview['effect_key'],attempt['canonical_touch_id'],
                        attempt['canonical_ref'],step,result['receipt_ref'])
                else:self.ledger.finish(preview['effect_key'],'sent',result['receipt_ref'])
            return self._store(preview['effect_key'],result,attempt_token=attempt['attempt_token'])
