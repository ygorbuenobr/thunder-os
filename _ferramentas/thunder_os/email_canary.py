"""Explicit one-shot email admission. No provider, migrations or secret discovery.

A pinned operator policy narrows an already enabled global gate; it never enables
that gate. Claims live in the existing transactional repository and are retained
across failures/restarts. A new policy cannot retry the same reviewed snapshot.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import time
from uuid import uuid4
from .seqd_client import MAILBOX_ID


class CanaryError(ValueError):
    pass


def restricted_mode():
    # Unknown configured modes fail closed instead of becoming standard mode.
    return os.environ.get('THUNDER_OS_EFFECT_MODE','standard')!='standard'


def check(value,code):
    if not value:raise CanaryError(code)


def hash_object(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


class EmailCanary:
    def __init__(self,repository,*,clock=time.time):
        self.repo=repository;self.clock=clock;self.token=None;self.policy=self._load()
        self.key=hash_object({k:self.policy[k] for k in ('mailbox_id','recipient','preview_id','expected_version','content_hash')})

    def _load(self):
        check(os.environ.get('THUNDER_OS_EFFECT_MODE')=='canary','canary_mode_invalid')
        check(os.environ.get('THUNDER_EXTERNAL_EFFECTS_ENABLED','').lower()=='true','external_effects_disabled')
        try:
            path=Path(os.environ.get('THUNDER_OS_CANARY_POLICY_FILE',''))
            check(path.is_absolute() and path.is_file() and not path.is_symlink(),'canary_policy_unavailable')
            check(path.stat().st_size<=16384,'canary_policy_invalid')
            raw=path.read_bytes()
            check(hashlib.sha256(raw).hexdigest()==os.environ.get('THUNDER_OS_CANARY_POLICY_SHA256'),'canary_policy_changed')
            def unique(pairs):
                d={}
                for key,value in pairs:
                    check(key not in d,'canary_policy_invalid');d[key]=value
                return d
            p=json.loads(raw,object_pairs_hook=unique)
            fields={'version','approval_ref','mailbox_id','recipient','preview_id','expected_version','content_hash','issued_at','expires_at'}
            check(isinstance(p,dict) and set(p)==fields and type(p['version']) is int and p['version']==1,'canary_policy_invalid')
            check(p['mailbox_id']==MAILBOX_ID and isinstance(p['approval_ref'],str) and 0<len(p['approval_ref'])<=512,'canary_scope_invalid')
            check(isinstance(p['recipient'],str) and p['recipient']==p['recipient'].strip().lower() and '@' in p['recipient'],'canary_scope_invalid')
            check(isinstance(p['preview_id'],str) and 0<len(p['preview_id'])<=128 and type(p['expected_version']) is int and p['expected_version']>0,'canary_scope_invalid')
            check(isinstance(p['content_hash'],str) and len(p['content_hash'])==64 and all(c in '0123456789abcdef' for c in p['content_hash']),'canary_scope_invalid')
            check(all(type(p[k]) in (int,float) and math.isfinite(p[k]) for k in ('issued_at','expires_at')),'canary_expired')
            check(p['issued_at']<=self.clock()<p['expires_at'] and 0<p['expires_at']-p['issued_at']<=900,'canary_expired')
            return p
        except CanaryError:raise
        except Exception:raise CanaryError('canary_policy_unavailable') from None

    def _current(self):
        check(self._load()==self.policy,'canary_policy_changed')
        check(not (self.repo.get_state('control','global') or {}).get('paused'),'canary_paused')

    def _scope(self,body):
        p=self.policy
        check(body.get('preview_id')==p['preview_id'] and type(body.get('expected_version')) is int
              and body['expected_version']==p['expected_version'] and body.get('content_hash')==p['content_hash'],'canary_scope_mismatch')
        preview=self.repo.get_state('seqd_preview',p['preview_id'])
        check(preview and preview.get('version')==p['expected_version'] and preview.get('content_hash')==p['content_hash']
              and preview.get('plan',{}).get('to')==p['recipient'],'canary_scope_mismatch')
        check(preview.get('state')=='review_required' and preview.get('expires_at',0)>self.clock(),'canary_preview_stale')

    def claim(self,body,actor):
        check(actor=='ygor','canary_human_required')
        with self.repo.transaction():
            self._current()
            check(self.repo.get_state('email_canary_claim',self.key) is None,'canary_already_claimed')
            self._scope(body)
            self.token=uuid4().hex
            self.repo.append_state('email_canary_claim',self.key,{'policy_hash':hash_object(self.policy),'token':self.token,
                'phase':'claimed','preview_id':self.policy['preview_id'],'created_at':self.clock(),'retry_write':False})
        return self

    def before_dispatch(self,envelope):
        from datetime import datetime
        with self.repo.transaction():
            self._current()
            row=self.repo.get_state('email_canary_claim',self.key)
            check(self.token and row and row.get('token')==self.token and row.get('phase')=='claimed'
                  and row.get('policy_hash')==hash_object(self.policy),'canary_already_claimed')
            p=self.policy
            check(envelope.get('preview_id')==p['preview_id'] and envelope.get('version')==p['expected_version']
                and envelope.get('content_hash')==p['content_hash'] and envelope.get('to')==p['recipient'],'canary_scope_mismatch')
            expires=datetime.fromisoformat(envelope['expires_at'].replace('Z','+00:00')).timestamp()
            check(self.clock()<expires<=p['expires_at'],'canary_expired')
            self.repo.put_state('email_canary_claim',self.key,{**row,'phase':'dispatch_started','dispatch_at':self.clock()})
        return True

    def reconcile_scope(self,body,actor):
        check(actor=='ygor' and body=={'preview_id':self.policy['preview_id']},'canary_scope_mismatch')
        self._current()
        row=self.repo.get_state('email_canary_claim',self.key)
        check(row and row.get('phase')=='dispatch_started','canary_not_dispatched')
        return True


def canary_view(repository,payload,preview_id=None):
    """Read-only availability; never claims a policy from a GET/status request."""
    if not restricted_mode():return payload
    allowed=False;scope=None
    try:
        guard=EmailCanary(repository);scope=guard.policy['preview_id'];guard._current()
        guard._scope({'preview_id':scope,'expected_version':guard.policy['expected_version'],'content_hash':guard.policy['content_hash']})
        allowed=(preview_id is None or preview_id==scope) and repository.get_state('email_canary_claim',guard.key) is None
    except CanaryError:pass
    result={**payload,'canary_only':True,'canary_preview_id':scope}
    for key in ('send_enabled','send_available'):
        if key in result:result[key]=result[key] is True and allowed
    if not allowed:result['blockers']=[*result.get('blockers',[]),'canary_scope_unavailable']
    return result
