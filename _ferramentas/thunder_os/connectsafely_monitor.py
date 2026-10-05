"""Single bounded GET-only monitor iteration; scheduling belongs to systemd.

Never sends, migrates, discovers credentials, or retries within an iteration.
A persistent authorization denial requires explicit operator intervention.
"""
import argparse
from contextlib import contextmanager
import fcntl
import http.client
import json
import math
import os
from pathlib import Path
import time
from .connectsafely_client import ConnectSafelyClient, ConnectSafelyError, require, urn
from .connectsafely_inbox import ConnectSafelyInbox
from .connectsafely_transport import ConnectSafelyHTTPS
from .models import payload_hash

KIND='connectsafely_monitor'
KEY='global'
CADENCE=600
MAX_BACKOFF=3600
SAFE_ERRORS={'provider_unauthorized','provider_forbidden','provider_rate_limited',
             'account_identity_mismatch','recipient_identity_mismatch','participants_unverified',
             'pagination_unverified','provider_schema_invalid','provider_outcome_unknown'}


def validate_config(config):
    common={'version','repository','account_id','account_public_id','self_profile_id','api_credential_file'}
    require(isinstance(config,dict) and type(config.get('version')) is int and config['version'] in (1,2),'monitor_config_invalid')
    keys=common|({'scopes'} if config['version']==1 else {'start_ms'})
    require(set(config)==keys,'monitor_config_invalid')
    for key in ('repository','api_credential_file'):
        require(isinstance(config[key],str) and Path(config[key]).is_absolute(),'monitor_config_invalid')
    client=ConnectSafelyClient(config['account_id'],config['account_public_id'],self_profile_id=config['self_profile_id'])
    require(client._profile_identity(client.self_profile_id) is not None,'monitor_config_invalid')
    if config['version']==2:
        require(type(config['start_ms']) is int and 0<config['start_ms']<10**16,'monitor_config_invalid')
        return json.loads(json.dumps(config))
    scopes=config['scopes'];require(isinstance(scopes,list) and len(scopes)==1,'monitor_config_invalid')
    for scope in scopes:
        require(isinstance(scope,dict) and set(scope)=={'plan','conversation_urn','anchor_ms'},'monitor_config_invalid')
        client._plan(scope['plan'])
        require(scope['plan']['action']=='dm' and urn(scope['conversation_urn']),'monitor_config_invalid')
        require(scope['plan'].get('conversation_urn') in (None,scope['conversation_urn']),'monitor_config_invalid')
        require(type(scope['anchor_ms']) is int and 0<scope['anchor_ms']<10**16,'monitor_config_invalid')
    return json.loads(json.dumps(config))


def read_connection(host,port,**options):
    # Six sequential calls fit within the v2 external 270-second process limit.
    options['timeout']=40
    return http.client.HTTPSConnection(host,port,**options)


class ReadOnlyHTTPS(ConnectSafelyHTTPS):
    def _route(self,method,path,query,body):
        require(method=='GET' and body is None,'monitor_read_only')
        return super()._route(method,path,query,body)


def status(repository,*,clock=time.time):
    row=repository.get_state('connectsafely_account_monitor',KEY) or repository.get_state(KIND,KEY) or {}
    finish=row.get('last_finished_at')
    fresh=type(finish) in (int,float) and math.isfinite(finish) and 0<=clock()-finish<=CADENCE*2
    fields=('state','last_finished_at','next_run_at','observed_count','incoming_count','window_complete','error_code','failures','scope_count','scope','account_public_id','listed_count','pending_count','lag_seconds','coverage','get_count','rounds','duplicates','from_cache','provider_freshness','thread_blocked_count','blocked_codes')
    result={k:row[k] for k in fields if k in row}
    result.update(state=row.get('state','not_started'),healthy=row.get('state')=='ok' and fresh,
                  stale=not fresh,read_only=True,cadence_seconds=CADENCE,scope_count=row.get('scope_count',0))
    return result


class Monitor:
    def __init__(self,repository,config,*,request=None,clock=time.time):
        self.config=validate_config(config);self.repo=repository;self.clock=clock
        self.kind='connectsafely_account_monitor' if config['version']==2 else KIND
        require(Path(repository.path).resolve()==Path(config['repository']).resolve(),'monitor_config_invalid')
        if request is None:
            from .connectsafely_live import credential
            request=ReadOnlyHTTPS(config['account_id'],credential(config['api_credential_file']),connection_factory=read_connection)
        require(callable(request),'transport_unconfigured')
        self.request=request

    def run_once(self):
        now=self.clock();digest=payload_hash(self.config)
        with self.repo.transaction():
            old=self.repo.get_state(self.kind,KEY) or {}
            require(not old.get('config_hash') or old['config_hash']==digest,'monitor_config_changed')
            if old.get('state')=='blocked' or now<old.get('next_run_at',0):return status(self.repo,clock=self.clock)
            if old.get('state')=='running':
                self.repo.put_state(self.kind,KEY,{**old,'state':'backoff','error_code':'previous_run_interrupted',
                    'last_finished_at':now,'next_run_at':now+CADENCE,'failures':old.get('failures',0)+1})
                return status(self.repo,clock=self.clock)
            current=self.repo.put_state(self.kind,KEY,{**{k:old[k] for k in ('scope','account_public_id','listed_count','pending_count','lag_seconds','coverage','rounds','duplicates','from_cache','provider_freshness','thread_blocked_count','blocked_codes') if k in old},
                **({'scope':'account_inbox','account_public_id':self.config['account_public_id']} if self.config['version']==2 else {}),
                'config_hash':digest,'state':'running','started_at':now,
                'scope_count':len(self.config.get('scopes',[None])),'next_run_at':now+(270 if self.config['version']==2 else 180),'failures':old.get('failures',0),'last_finished_at':old.get('last_finished_at')})
        calls=0
        def bounded(method,path,query,body):
            nonlocal calls
            require(method=='GET' and body is None,'monitor_read_only')
            require(calls<(6 if self.config['version']==2 else 4),'monitor_budget_exhausted');calls+=1
            return self.request(method,path,query,body)
        client=ConnectSafelyClient(self.config['account_id'],self.config['account_public_id'],
            self_profile_id=self.config['self_profile_id'],request=bounded,clock=self.clock)
        result=None;error=None
        try:
            if self.config['version']==2:
                from .connectsafely_account_inbox import AccountInbox
                result=AccountInbox(self.repo,client,start_ms=self.config['start_ms']).poll()
            else:
                scope=self.config['scopes'][0]
                result=ConnectSafelyInbox(self.repo,client).poll(scope['plan'],scope['conversation_urn'],anchor_ms=scope['anchor_ms'])
        except Exception as exc:
            error=str(exc) if isinstance(exc,ConnectSafelyError) and str(exc) in SAFE_ERRORS else 'provider_outcome_unknown'
        finish=self.clock();failures=old.get('failures',0)+1 if error else 0
        blocked=error in ('provider_unauthorized','provider_forbidden')
        record={'config_hash':digest,'state':'blocked' if blocked else 'backoff' if error else 'ok',
            'started_at':now,'last_finished_at':finish,'next_run_at':None if blocked else finish+(min(MAX_BACKOFF,CADENCE*2**min(failures-1,3)) if error else CADENCE),
            'error_code':error,'failures':failures,'get_count':calls,'scope_count':len(self.config.get('scopes',[None])),
            'observed_count':result['observed_count'] if result else 0,
            'incoming_count':result.get('incoming_count',len(result.get('incoming',[]))) if result else 0,
            'window_complete':result['window_complete'] if result else False}
        if self.config['version']==2:
            record.update(scope='account_inbox',account_public_id=self.config['account_public_id'],rounds=old.get('rounds',0)+1)
            for field in ('listed_count','pending_count','lag_seconds','coverage','duplicates','from_cache','provider_freshness','thread_blocked_count','blocked_codes'):
                record[field]=(result or {}).get(field,old.get(field))
        with self.repo.transaction():
            require(self.repo.get_state(self.kind,KEY)==current,'monitor_attempt_changed')
            self.repo.put_state(self.kind,KEY,record)
        return status(self.repo,clock=self.clock)


@contextmanager
def exclusive_lock(path):
    fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ConnectSafelyError('monitor_locked') from None
        yield
    finally:os.close(fd)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);args=parser.parse_args()
    try:
        config=validate_config(json.loads(Path(args.config).read_text()))
        from .connectsafely_runtime import ExistingRepository
        with exclusive_lock(config['repository']+'.connectsafely-monitor.lock'):
            result=Monitor(ExistingRepository(config['repository']),config).run_once()
        print(json.dumps(result));return 0
    except Exception as exc:
        code=str(exc) if isinstance(exc,ConnectSafelyError) and str(exc) in {'monitor_locked','monitor_config_invalid','monitor_config_changed'} else 'monitor_start_failed'
        print(json.dumps({'state':'blocked','healthy':False,'error_code':code}));return 1

if __name__=='__main__':raise SystemExit(main())
