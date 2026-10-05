"""Explicit two-process ConnectSafely runtime. Never installs or migrates anything.

A live factory is trusted executable deployment code, pinned by SHA256, not a
credential plugin discovered from environment. It must perform its own dynamic
DB/inventory verification; a static ready=true file is never sufficient.
"""
from contextlib import closing
import argparse
import re
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import threading
import time
from types import SimpleNamespace
from http.server import ThreadingHTTPServer
from .db import Repository, _OwnedConnection, _BorrowedConnection
from .connectsafely_application import ConnectSafelyApplication
from .connectsafely_composition import ConnectSafelyComposition, compose_connectsafely, SyntheticDirectTransport


class ExistingRepository(Repository):
    def _connect(self):
        active=getattr(self._local,'connection',None)
        if active is not None:return _BorrowedConnection(active)
        con=sqlite3.connect(Path(self.path).as_uri()+'?mode=rw',uri=True,
            timeout=5,isolation_level=None,factory=_OwnedConnection)
        con.row_factory=sqlite3.Row
        con.execute('PRAGMA foreign_keys=ON')
        return con

    def migrate(self):
        path = Path(self.path)
        if not path.is_file():
            raise ValueError('repository_must_be_provisioned_offline')
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as con:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'thunder_state','thunder_schema','thunder_effect_jobs','thunder_events','thunder_outbox','thunder_work_items'} <= tables:
                raise ValueError('repository_schema_not_ready')
            if con.execute('SELECT version FROM thunder_schema').fetchall() != [(1,)]:
                raise ValueError('repository_schema_not_ready')
            # Table names alone do not prove that durable approval/job identities
            # are unique. A restored or hand-created DB must retain these keys.
            keys = {'thunder_state': ('kind','entity_id'), 'thunder_work_items': ('id',),
                    'thunder_effect_jobs': ('id',), 'thunder_events': ('id',),
                    'thunder_outbox': ('id',)}
            for table, expected in keys.items():
                columns = con.execute('PRAGMA table_info('+table+')').fetchall()
                actual = tuple(row[1] for row in sorted(columns,key=lambda row:row[5]) if row[5])
                if actual != expected:
                    raise ValueError('repository_identity_constraints_missing')
            for table, column in (('thunder_work_items','idempotency_key'),('thunder_effect_jobs','effect_key')):
                indexes = con.execute('PRAGMA index_list('+table+')').fetchall()
                unique = []
                for index in indexes:
                    if index[2] and not index[4]:
                        name = index[1].replace('"','""')
                        unique.append(tuple(row[2] for row in con.execute('PRAGMA index_info("'+name+'")')))
                if (column,) not in unique:
                    raise ValueError('repository_identity_constraints_missing')


def absolute(value):
    if not isinstance(value,str) or not Path(value).is_absolute():
        raise ValueError('absolute_explicit_path_required')
    return Path(value).resolve()


def load_config(path):
    raw=absolute(str(path)).read_bytes()
    cfg=json.loads(raw)
    if not isinstance(cfg,dict) or cfg.get('version') != 1:
        raise ValueError('runtime_config_invalid')
    if cfg.get('mode','disabled') not in ('disabled','synthetic','live'):
        raise ValueError('runtime_mode_invalid')
    if type(cfg.get('execution_enabled',False)) is not bool:
        raise ValueError('explicit_boolean_required')
    cfg['_digest']=hashlib.sha256(raw).hexdigest()
    absolute(cfg['repository'])
    return cfg


def build(cfg, repository=None):
    repo=repository if repository is not None else ExistingRepository(absolute(cfg['repository']))
    if Path(repo.path).resolve()!=absolute(cfg['repository']):raise ValueError('repository_configuration_mismatch')
    mode=cfg.get('mode','disabled')
    enabled=cfg.get('execution_enabled',False) and os.environ.get('THUNDER_EXTERNAL_EFFECTS_ENABLED','').lower()=='true'
    if mode=='disabled' or (mode=='live' and not enabled):
        if mode=='disabled' and cfg.get('execution_enabled',False):raise ValueError('disabled_mode_cannot_execute')
        return ConnectSafelyComposition(ConnectSafelyApplication(repo))
    if mode=='synthetic':
        from .connectsafely_client import ConnectSafelyClient
        from .connectsafely_ledger import ConnectSafelyLedger
        from .providers.connectsafely import ConnectSafelyAdapter
        db=absolute(cfg['synthetic']['touch_database'])
        if not db.is_file():raise ValueError('touch_schema_must_be_provisioned_offline')
        with closing(sqlite3.connect(db.as_uri()+'?mode=ro',uri=True)) as con:
            for table in ('toques','respostas','thunder_suppressions','thunder_effect_touches'):
                con.execute('SELECT 1 FROM '+table+' LIMIT 0')
        # The canonical ledger prioritizes this explicit path over PostgreSQL env.
        # No .env discovery and no migrations. Synthetic ports cannot open sockets.
        os.environ['TOQUES_SQLITE']=str(db)
        fixture=cfg['synthetic']
        clock=(lambda:fixture['clock']) if type(fixture.get('clock')) in (int,float) else time.time
        client=ConnectSafelyClient(fixture['account_id'],fixture['public_id'],
            request=SyntheticDirectTransport(fixture['account_id'],fixture['responses']),
            self_profile_id=fixture['self_profile_id'],dm_quota_feature=fixture['dm_quota_feature'],clock=clock)
        composition=compose_connectsafely(repo,ConnectSafelyAdapter(client),direct_ledger=ConnectSafelyLedger(),
            simulation=True,execution_enabled=enabled,clock=clock)
        bind_config(repo,cfg)
        return composition
    if cfg.get('factory')=='builtin-connectsafely-v1':
        from .connectsafely_live import build as live_build
        composition=live_build(repo,cfg)
        bind_config(repo,cfg)
        return composition
    entry=cfg.get('factory',{})
    path=absolute(entry.get('path'))
    data=path.read_bytes()
    if hashlib.sha256(data).hexdigest()!=entry.get('sha256'):
        raise ValueError('factory_artifact_mismatch')
    # Execute only the bytes verified above (not a second import susceptible to TOCTOU).
    namespace={'__name__':'thunder_connectsafely_audited_factory','__file__':str(path)}
    exec(compile(data,str(path),'exec'),namespace)
    composition=namespace['build'](repo,dict(cfg))
    if type(composition) is not ConnectSafelyComposition or composition.application.repo is not repo:
        raise ValueError('factory_contract_invalid')
    app=composition.application
    if app.simulation or app.execution_enabled is not enabled or not callable(app.readiness):
        raise ValueError('factory_modes_or_readiness_invalid')
    # Reuse canonical typed facade validation; social transport independently
    # demands its DB capability. A factory is deployment code, never user input.
    from .providers.connectsafely import ConnectSafelyAdapter
    if type(app.provider) is not ConnectSafelyAdapter:
        raise ValueError('canonical_provider_required')
    bind_config(repo,cfg)
    return composition


def bind_config(repo,cfg):
    with repo.transaction():
        previous=repo.get_state('connectsafely_runtime_config','active')
        if previous is not None and previous.get('config_sha256')!=cfg['_digest']:
            raise ValueError('runtime_configuration_mismatch_review_required')
        if previous is None:repo.put_state('connectsafely_runtime_config','active',{'config_sha256':cfg['_digest']})


def heartbeat(path,cfg,role,state,processed=0):
    target=absolute(path)
    value={'role':'connectsafely-'+role,'state':state,'updated_at':time.time(),
           'config_sha256':cfg['_digest'],'release':cfg.get('release','unknown'),
           'simulation':cfg.get('mode')=='synthetic','processed':processed,'pid':os.getpid()}
    temp=target.with_name(target.name+'.'+str(os.getpid())+'.tmp')
    temp.write_text(json.dumps(value));os.replace(temp,target)


def individual_mode(cfg):
    return cfg.get('mode')=='live' and (cfg.get('live',{}).get('social_enabled',True) is False or cfg.get('live',{}).get('execution_scope') is not None)


def validate_effect_selection(cfg,once,job_id):
    if individual_mode(cfg) and (once is not True or not isinstance(job_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',job_id)):
        raise ValueError('individual_worker_requires_once_and_job_id')
    if job_id is not None and once is not True:raise ValueError('explicit_job_requires_once')


def effects(cfg,composition,*,once=False,job_id=None):
    validate_effect_selection(cfg,once,job_id)
    app=composition.application;stop=threading.Event();count=0
    interval=cfg.get('poll_seconds',10)
    if type(interval) not in (int,float) or not 5<=interval<=60:raise ValueError('bounded_poll_interval_required')
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:stop.set())
    try:
        while not stop.is_set():
            ready=app.status('ygor')['enqueue_enabled']
            state='ready' if ready else 'blocked'
            heartbeat(cfg['effect_health'],cfg,'effects',state,count)
            # No build_runtime(): that legacy entrypoint may migrate other ledgers.
            if ready and (app.process_one(job_id=job_id) if job_id is not None else app.process_one()):count+=1
            if once:break
            stop.wait(interval)
    except Exception:
        heartbeat(cfg['effect_health'],cfg,'effects','failed',count)
        raise RuntimeError('effect_runtime_failed') from None
    finally:
        heartbeat(cfg['effect_health'],cfg,'effects','stopped',count)


def web(cfg,composition):
    from painel_servidor import main as panel_main
    absolute(cfg['panel_root'])
    origin=cfg.get('public_origin','')
    from urllib.parse import urlsplit
    if not origin.startswith('https://') or urlsplit(origin).path:raise ValueError('explicit_https_origin_required')
    return panel_main(['servir'], connectsafely_factory=lambda repo:build(cfg,repo), connectsafely_runtime=cfg)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',required=True)
    parser.add_argument('--role',choices=('web','effects'),required=True)
    parser.add_argument('--once',action='store_true',help='At most one queued job; never a retry loop')
    parser.add_argument('--job-id',help='Exact individually approved job; requires --once')
    args=parser.parse_args()
    cfg=load_config(args.config)
    if args.role=='effects':validate_effect_selection(cfg,args.once,args.job_id)
    elif args.once or args.job_id is not None:parser.error('job selectors require effects role')
    composition=build(cfg)
    if args.role=='web':raise SystemExit(web(cfg,composition))
    else:effects(cfg,composition,once=args.once,job_id=args.job_id)

if __name__=='__main__':main()
