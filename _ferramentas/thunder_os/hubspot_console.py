from .operation_campaign import OperationCampaign
from .approvals import ApprovalError
"""Human-reviewed CRM console; provider credential is a scoped systemd credential.

No contact messages, deletion, schema changes or background dispatch. An exact
server-held preview must be explicitly accepted while current. Defaults disabled.
"""
import os
import re
import stat
import time
from pathlib import Path
from .db import RepositoryConflict
from .hubspot_executor import HubSpotHTTP, HubSpotExecutor, HubSpotHTTPError
from .integration_hubspot import HubSpotContractError, PORTAL_ID
from .models import payload_hash


def systemd_hubspot_credential():
    explicit=os.environ.get('THUNDER_OS_HUBSPOT_CREDENTIAL_FILE','')
    directory=os.environ.get('CREDENTIALS_DIRECTORY','')
    if explicit:
        path=Path(explicit)
        if not path.is_absolute() or path.name!='hubspot_access':raise HubSpotHTTPError('hubspot_credential_unavailable')
    else:
        if not directory or not Path(directory).is_absolute():raise HubSpotHTTPError('hubspot_credential_unavailable')
        path=Path(directory)/'hubspot_access'
    try:
        descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(descriptor,'r') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size>4096 or info.st_mode&0o077 or info.st_uid not in {0,os.geteuid()}:raise ValueError()
            token=stream.read(4097).strip()
        if not token or any(c.isspace() for c in token):raise ValueError()
        return token
    except Exception:raise HubSpotHTTPError('hubspot_credential_unavailable') from None


class HubSpotConsole:
    def __init__(self,repository,transport=None,*,read_enabled=False,write_enabled=False,clock=time.time,dispatch_authorized=None):
        self.repo,self.port=repository,transport or HubSpotHTTP(systemd_hubspot_credential,creation_mode='business_ledger')
        self.read_enabled,self.write_enabled,self.clock=read_enabled,write_enabled,clock
        self.dispatch_authorized=dispatch_authorized

    def _write_allowed(self):
        control=self.repo.get_state('control','global')
        return (self.write_enabled is True and not (control and control.get('paused'))
                and (self.dispatch_authorized is None or self.dispatch_authorized() is True))

    def _actor(self,actor):
        if actor!='ygor':raise HubSpotContractError('human_authority_required')
        if self.read_enabled is not True:raise HubSpotContractError('hubspot_not_configured')

    def status(self,actor):
        self._actor(actor)
        try:
            portal=self.port.portal()
            return {'provider':'hubspot','portal_id':portal,'read_verified':True,'verified_at':self.clock(),
                    'review_required':True,'write_enabled':self._write_allowed(),
                    'can_create_contacts':self._write_allowed(),'can_update_existing':self._write_allowed(),
                    'can_create_companies_or_deals':self._write_allowed(),'outbound_messages_enabled':False,
                    'blockers':[], 'limitations':['business_identity_ledger_does_not_serialize_external_writers','uncertain_writes_require_reconciliation_without_retry']}
        except (HubSpotHTTPError,HubSpotContractError):
            return {'provider':'hubspot','read_verified':False,'write_enabled':False,'outbound_messages_enabled':False,
                    'blockers':['provider_identity_or_access_unavailable']}

    def read_preview(self,key,actor):
        self._actor(actor)
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',key):raise HubSpotContractError('invalid_preview_id')
        result=self.repo.get_state('hubspot_preview',key)
        if not result:raise HubSpotContractError('preview_not_found')
        return {**result,'write_enabled':self._write_allowed()}

    def prepare(self,body,actor):
        self._actor(actor)
        if not isinstance(body,dict) or set(body)-{'idempotency_key','object_type','properties','record_id'}:
            raise HubSpotContractError('invalid_preview')
        if body.get('object_type') not in {'contacts','companies','deals'} or not isinstance(body.get('properties'),dict) or not body['properties'] or any(not isinstance(k,str) or not isinstance(v,str) for k,v in body['properties'].items()):
            raise HubSpotContractError('invalid_preview_properties')
        key=body.get('idempotency_key')
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',key):raise HubSpotContractError('invalid_idempotency_key')
        fingerprint=payload_hash(body)
        existing=self.repo.get_state('hubspot_preview',key)
        if existing:
            if existing['request_hash']!=fingerprint:raise RepositoryConflict('preview_key_changed')
            return existing
        plan=self.port.prepare(body.get('object_type'),body.get('properties'),key,body.get('record_id'))
        with self.repo.transaction():
            existing=self.repo.get_state('hubspot_preview',key)
            if existing:
                if existing['request_hash']!=fingerprint:raise RepositoryConflict('preview_key_changed')
                return existing
            return self.repo.append_state('hubspot_preview',key,{'plan':plan,'request_hash':fingerprint,
                'state':'review_required','expires_at':self.clock()+300,'created_by':actor,
                'write_enabled':self._write_allowed()})

    def campaign_gate(self):
        return OperationCampaign(self.repo, 'hubspot', 'hubspot', PORTAL_ID, 'hubspot', clock=self.clock)

    def campaign_review(self, body, actor):
        self._actor(actor)
        if not isinstance(body, dict) or set(body) != {'preview_id', 'campaign_id'}:
            raise HubSpotContractError('campaign_review_invalid')
        return self.campaign_gate().review(body['preview_id'], body['campaign_id'])

    def execute(self,body,actor):
        self._actor(actor)
        if not isinstance(body, dict) or 'execution_confirmation' not in body:
            raise HubSpotContractError('campaign_confirmation_required')
        gate = self.campaign_gate()
        try:
            campaign_record = gate.approve(body, actor)
        except ApprovalError as exc:
            raise HubSpotContractError(str(exc)) from None
        body = {k:v for k,v in body.items() if k != 'execution_confirmation'}
        if not self._write_allowed():raise HubSpotContractError('hubspot_writes_disabled_or_paused')
        if (not isinstance(body,dict) or set(body)!={'preview_id','expected_version','plan_hash','confirm'}
                or body.get('confirm') is not True or type(body.get('expected_version')) is not int):
            raise HubSpotContractError('exact_review_required')
        with self.repo.transaction():
            preview=self.repo.get_state('hubspot_preview',body['preview_id'])
            if not preview or preview['version']!=body['expected_version'] or preview['plan']['plan_hash']!=body['plan_hash']:
                raise RepositoryConflict('preview_changed')
            if preview['state']!='review_required' or preview['expires_at']<=self.clock():raise HubSpotContractError('preview_not_current')
            approved=self.repo.put_state('hubspot_preview',preview['id'],{**preview,'state':'approved','approved_by':actor})
        def authorize(plan):
            current=self.repo.get_state('hubspot_preview',approved['id'])
            return bool(self._write_allowed() and current and current['state']=='approved'
                        and current['version']==approved['version'] and current['expires_at']>self.clock()
                        and current['plan']['plan_hash']==plan['plan_hash'])
        # Per-execution wrapper: do not mutate the shared transport across threads.
        port=self.port
        self_repo=self.repo
        class GuardedTransport:
            def __getattr__(self,name):return getattr(port,name)
            def write(self,request):
                if authorize(approved['plan']) is not True:
                    raise HubSpotContractError('hubspot_writes_disabled_or_paused')
                effect,_ = HubSpotExecutor(self_repo, port)._keys(approved['plan'])
                with gate.execution_guard(campaign_record, 'hubspot_attempt', effect):
                    if authorize(approved['plan']) is not True:
                        raise HubSpotContractError('hubspot_writes_disabled_or_paused')
                    return port.write(request)
        try:
            result=HubSpotExecutor(self.repo,GuardedTransport(),authorize=authorize,human_review=authorize,enabled=True).execute(approved['plan'])
        except (HubSpotHTTPError,HubSpotContractError):
            result={'status':'needs_human','error_code':'execution_requires_reconciliation','retry_write':False}
        with self.repo.transaction():
            current=self.repo.get_state('hubspot_preview',approved['id'])
            if current['version']!=approved['version']:raise RepositoryConflict('approval_changed')
            return self.repo.put_state('hubspot_preview',approved['id'],{**current,'state':'processed','result':result})

    def reconcile(self,body,actor):
        self._actor(actor)
        if not isinstance(body,dict) or set(body)!={'preview_id','expected_version','plan_hash'}:
            raise HubSpotContractError('exact_review_required')
        preview=self.repo.get_state('hubspot_preview',body['preview_id'])
        if not preview or preview['version']!=body['expected_version'] or preview['plan']['plan_hash']!=body['plan_hash']:
            raise RepositoryConflict('preview_changed')
        if preview['state'] not in {'approved','processed'}:raise HubSpotContractError('no_execution_to_reconcile')
        executor=HubSpotExecutor(self.repo,self.port)
        effect,_=executor._keys(preview['plan'])
        if not self.repo.get_state('hubspot_attempt',effect):
            result={'status':'not_attempted','retry_write':False}
        else:
            try:result=executor.reconcile(preview['plan'])
            except (HubSpotHTTPError,HubSpotContractError):result={'status':'pending_reconciliation','retry_write':False}
        with self.repo.transaction():
            current=self.repo.get_state('hubspot_preview',preview['id'])
            if current['version']!=preview['version']:raise RepositoryConflict('preview_changed')
            return self.repo.put_state('hubspot_preview',preview['id'],{**current,'state':'processed','result':result})
