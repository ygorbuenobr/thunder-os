"""Concrete live wiring; invoked only by explicitly enabled audited runtime.

All paths/pins are deployment inputs. Secrets are read lazily inside connection
callbacks, never returned/logged. This module does not migrate or call providers
when building the composition. Evidence must be approved and pinned externally.
"""
from contextlib import contextmanager
import hashlib
import json
import os
import re
from pathlib import Path
import time
from .connectsafely_composition import compose_connectsafely
from .connectsafely_client import ConnectSafelyClient
from .connectsafely_social_client import ConnectSafelySocialClient
from .connectsafely_transport import ConnectSafelyHTTPS
from .connectsafely_social_transport import ConnectSafelySocialHTTPS
from .connectsafely_ledger import ConnectSafelyLedger
from .connectsafely_social_console import ConnectSafelySocialLedger
from .connectsafely_coordination import WriterEvidence,InventoryEvidence
from .connectsafely_coordination_postgres import PostgresSocialCoordinator
from .providers.connectsafely import ConnectSafelyAdapter


def explicit_path(value):
    path=Path(value)
    if not path.is_absolute() or path.is_symlink():raise ValueError('explicit_regular_file_required')
    return path


def evidence(spec):
    raw=explicit_path(spec['path']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=spec['sha256']:raise ValueError('evidence_changed')
    data=json.loads(raw)
    if data.get('approved_by')!='ygor' or not data.get('approval_ref') or data.get('expires_at',0)<=time.time():
        raise ValueError('approved_current_evidence_required')
    # Every supporting artifact must also be pinned, not only an arbitrary URL.
    if not data.get('sources'):raise ValueError('evidence_sources_required')
    for source in data['sources']:
        if hashlib.sha256(explicit_path(source['path']).read_bytes()).hexdigest()!=source['sha256']:
            raise ValueError('evidence_source_changed')
    return data


def credential(path):
    def read():
        value=explicit_path(path).read_text().strip()
        if not value or '\n' in value:raise ValueError('credential_unavailable')
        return value
    return read


def build_panel_composition(repository, *, config_path, config_sha256):
    """Load the reviewed runtime contract without permitting executable plugins.

    The same config digest is bound to the canonical repository by runtime.build.
    Changing that binding remains an explicit operational reconciliation.
    """
    from .connectsafely_runtime import load_config, build as runtime_build
    if (not isinstance(config_sha256, str)
            or not re.fullmatch(r'[0-9a-f]{64}', config_sha256)):
        raise ValueError('reviewed_config_digest_required')
    cfg = load_config(explicit_path(config_path))
    if cfg['_digest'] != config_sha256:
        raise ValueError('panel_config_artifact_mismatch')
    if cfg.get('mode') != 'live' or cfg.get('factory') != 'builtin-connectsafely-v1':
        raise ValueError('panel_builtin_live_factory_required')
    live = cfg.get('live')
    if not isinstance(live, dict) or type(live.get('social_enabled')) is not bool:
        raise ValueError('explicit_social_mode_required')
    scope = live.get('execution_scope')
    if not isinstance(scope, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', scope):
        raise ValueError('individual_execution_scope_required')
    try:
        composition = runtime_build(cfg, repository)
    except ValueError as exc:
        if str(exc) != 'approved_current_evidence_required':
            raise
        # Expired/unapproved evidence disables this adapter, not the whole panel.
        from .connectsafely_application import ConnectSafelyApplication
        from .connectsafely_composition import ConnectSafelyComposition
        composition = ConnectSafelyComposition(ConnectSafelyApplication(repository))
    composition.application.direct_target_resolution = not live['social_enabled']
    return composition


def direct_ledger_readiness(connect,identity):
    """Metadata/SELECT only against the exact runtime role; never test-write."""
    con=None
    try:
        if (not isinstance(identity,dict) or set(identity)!={'database','role'}
                or not all(isinstance(v,str) and v for v in identity.values())):
            raise ValueError('direct_database_identity_required')
        con=connect()
        with con.cursor() as cur:
            cur.execute("SELECT current_database(),current_user,session_user,current_setting('transaction_read_only'),current_setting('default_transaction_read_only')")
            row=cur.fetchone()
            if row!=(identity['database'],identity['role'],identity['role'],'off','off'):
                raise ValueError('direct_database_identity_or_readonly')
        con.rollback()
        with con.cursor() as cur:
            cur.execute('SET TRANSACTION READ ONLY')
            cur.execute('SET LOCAL search_path=pg_catalog,public')
            cur.execute("SET LOCAL statement_timeout='5s'")
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname IN (current_user,session_user) AND (rolsuper OR rolcreaterole OR rolcreatedb OR rolbypassrls)")
            if cur.fetchone():raise ValueError('direct_role_overprivileged')
            for role in ('pg_execute_server_program','pg_write_server_files','pg_read_server_files'):
                cur.execute("SELECT pg_has_role(current_user,%s,'MEMBER')",(role,))
                if cur.fetchone()[0]:raise ValueError('direct_role_overprivileged')
            cur.execute("SELECT has_schema_privilege(current_user,'public','CREATE')")
            if cur.fetchone()[0]:raise ValueError('direct_role_overprivileged')
            columns={
                'toques':'id,canal,acao,pessoa_chave,alvo,status,provider_id,detalhe,agente,ref,atualizado_em',
                'respostas':'id,canal,pessoa_chave',
                'thunder_suppressions':'canal,pessoa_chave,reason',
                'thunder_effect_touches':'effect_key,touch_id'}
            insert_columns={'toques':{'canal','acao','pessoa_chave','alvo','status','agente','ref'},
                            'thunder_effect_touches':{'effect_key','touch_id'}}
            update_columns={'toques':{'status','provider_id','detalhe','atualizado_em','ref'}}
            for table,names in columns.items():
                qualified='public.'+table
                cur.execute('SELECT '+names+' FROM '+qualified+' LIMIT 0')
                cur.execute("SELECT relkind,relrowsecurity,relforcerowsecurity,pg_has_role(current_user,relowner,'MEMBER') FROM pg_class WHERE oid=%s::regclass",(qualified,))
                if cur.fetchone()!=('r',False,False,False):raise ValueError('direct_relation_unverified')
                for name in names.split(','):
                    cur.execute("SELECT has_column_privilege(current_user,%s,%s,'SELECT')",(qualified,name))
                    if not cur.fetchone()[0]:raise ValueError('direct_ledger_privileges_unverified')
                for privilege in ('DELETE','TRUNCATE','REFERENCES','TRIGGER'):
                    cur.execute('SELECT has_table_privilege(current_user,%s,%s)',(qualified,privilege))
                    if cur.fetchone()[0]:raise ValueError('direct_role_overprivileged')
                cur.execute("SELECT attname,has_column_privilege(current_user,attrelid,attnum,'INSERT'),has_column_privilege(current_user,attrelid,attnum,'UPDATE') FROM pg_attribute WHERE attrelid=%s::regclass AND attnum>0 AND NOT attisdropped",(qualified,))
                privileges=cur.fetchall()
                actual_insert={name for name,insert,update in privileges if insert}
                actual_update={name for name,insert,update in privileges if update}
                if actual_insert!=insert_columns.get(table,set()) or actual_update!=update_columns.get(table,set()):
                    raise ValueError('direct_ledger_privileges_unverified')
            cur.execute("SELECT pg_get_serial_sequence('public.toques','id')")
            sequence=cur.fetchone()[0]
            if not sequence:raise ValueError('direct_sequence_unverified')
            cur.execute("SELECT has_sequence_privilege(current_user,%s,'USAGE'),has_sequence_privilege(current_user,%s,'UPDATE')",(sequence,sequence))
            if cur.fetchone()!=(True,False):raise ValueError('direct_sequence_unverified')
        return {'ready':True,'backend':'postgresql','scope':'direct_ledger_metadata'}
    except Exception:return {'ready':False,'blocker':'direct_ledger_unverified'}
    finally:
        if con is not None:
            con.rollback();con.close()


def build(repository,cfg):
    live=cfg['live'];social_enabled=live.get('social_enabled',True)
    if type(social_enabled) is not bool:raise ValueError('explicit_social_mode_required')
    coordination=live.get('social_coordination','base')
    if type(coordination) is not str or coordination not in {'base','reviewed_resolution'}:raise ValueError('explicit_social_coordination_required')
    scope=live.get('execution_scope')
    if not social_enabled and (not isinstance(scope,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',scope)):
        raise ValueError('individual_execution_scope_required')
    account=live['account_id'];public=live['public_id'];owner=live['self_profile_id']
    token=credential(live['api_credential_file']);dsn=credential(live['database_credential_file'])
    session_read_write = live.get('database_session_read_write', False)
    if type(session_read_write) is not bool:
        raise ValueError('explicit_database_session_mode_required')
    def connect():
        import psycopg2
        kwargs = {'connect_timeout': 5}
        if session_read_write:
            # Connection-local opt-in; preserve the panel's global PGOPTIONS guard.
            kwargs['options'] = '-c default_transaction_read_only=off'
        return psycopg2.connect(dsn(), **kwargs)
    @contextmanager
    def database():
        con=connect()
        try:
            with con.cursor() as cur:
                cur.execute('SET LOCAL search_path=pg_catalog,public')
            yield con,'%s'
            con.commit()
        except Exception:
            con.rollback();raise
        finally:con.close()
    coordinator=None
    if social_enabled:
        inv=live['inventory']
        inventory=InventoryEvidence(tuple(WriterEvidence(**w) for w in inv['writers']),
            tuple(inv['expected_writer_ids']),inv['inventory_path'],inv['inventory_sha256'])
        from .connectsafely_quarantine_resolution_postgres import PostgresResolvedSocialCoordinator
        coordinator_type=PostgresResolvedSocialCoordinator if coordination=='reviewed_resolution' else PostgresSocialCoordinator
        coordinator=coordinator_type(connect,inventory,backfill_manifest_hash=live.get('backfill_manifest_hash'))
    # Feature names alone never enable quota: approved source evidence is checked
    # before enqueue/dispatch through application readiness, and quota at provider.
    contracts=evidence(live['account_and_quota_evidence'])
    if (contracts.get('account_id'),contracts.get('public_id'),contracts.get('self_profile_id'))!=(account,public,owner):
        raise ValueError('account_evidence_mismatch')
    features=contracts.get('quota_features',{})
    direct=ConnectSafelyClient(account,public,request=ConnectSafelyHTTPS(account,token),
        self_profile_id=owner,dm_quota_feature=features.get('dm'))
    social=(ConnectSafelySocialClient(account,public,request=ConnectSafelySocialHTTPS(account,token),
        self_profile_id=owner,reaction_quota_feature=features.get('react_post')) if social_enabled else None)
    from .connectsafely_read_targets import ReadTargetResolver,TargetReadHTTPS
    read_targets=ReadTargetResolver(repository,ConnectSafelyClient(account,public,self_profile_id=owner,request=TargetReadHTTPS(account,token)))
    def authors(plan):
        try:return read_targets.author(plan)
        except Exception:pass
        rows=evidence(live['author_evidence'])['bindings']
        matches=[r for r in rows if r.get('post_urn')==plan['post_urn'] and r.get('post_url')==plan['post_url']
            and r.get('parent_comment_urn')==plan.get('parent_comment_urn')]
        if len(matches)!=1:raise ValueError('author_evidence_ambiguous')
        return matches[0]
    def action_ready(action=None):
        if (os.environ.get('THUNDER_EXTERNAL_EFFECTS_ENABLED','').lower()!='true'
                or os.environ.get('THUNDER_OS_COPILOT_MODE','')!='human_approval'):
            return {'ready':False,'blocker':'individual_execution_gates_disabled'}
        if action not in (None,'dm','invite','react_post','comment','reply'):
            return {'ready':False,'blocker':'unsupported_action'}
        direct_action=action in ('dm','invite') or (action is None and not social_enabled)
        if not social_enabled and not direct_action:return {'ready':False,'blocker':'social_disabled'}
        try:
            from .legacy_writer_exclusion import verify_legacy_exclusion
            excluded=live.get('excluded_writers',[])
            if direct_action and not excluded:raise ValueError('direct_writer_exclusions_required')
            for writer in excluded:
                verify_legacy_exclusion(writer['path'],expected_sha256=writer['sha256'])
            current=evidence(live['account_and_quota_evidence'])
            if current!=contracts:raise ValueError('account_evidence_changed')
            if direct_action:
                if action=='dm' and not features.get('dm'):
                    return {'ready':False,'blocker':'dm_quota_contract_unverified'}
                return direct_ledger_readiness(connect,live.get('database_identity'))
            # Target authors are validated by the console from a fresh scoped
            # observation or an explicitly pinned source, not preapproved here.
            observation=coordinator.readiness()
            return {'ready':observation.get('verified') is True,'coordination':observation}
        except Exception:return {'ready':False,'blocker':'live_evidence_unverified'}
    def ready():return action_ready()
    composition=compose_connectsafely(repository,ConnectSafelyAdapter(direct,social_client=social),
        direct_ledger=ConnectSafelyLedger(db_context=database),
        social_ledger=ConnectSafelySocialLedger(db_context=database) if social_enabled else None,
        coordinator=coordinator,author_resolver=authors if social_enabled else None,
        execution_enabled=cfg['execution_enabled'],readiness=ready,
        action_readiness=action_ready,execution_scope=scope)
    if composition.application.social is not None:
        composition.application.social.activation_evidence=ready
    from .connectsafely_targets import resolve_target
    # Approved local resolution remains available; explicit read endpoint uses the
    # independent read-only observer, never the effects transport.
    if not social_enabled:
        def resolve_direct_target(body):
            action = body.get('action') if isinstance(body, dict) else None
            if action not in ('dm', 'invite') or not action_ready(action).get('ready'):
                return {'resolved':False,'status':'blocked','read_only':True,
                        'actions_available':False,'error_code':'target_evidence_unavailable'}
            return read_targets(body)
        composition.application.resolve_target = resolve_direct_target
    else:
        composition.application.resolve_target=lambda body:resolve_target(body,account_id=account,public_id=public,
            load_evidence=lambda:evidence(live['author_evidence']))
    return composition
