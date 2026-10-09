"""PostgreSQL capability issuer with catalog/role verification, explicit connection.

No DSN/env/credential discovery. Migration is never automatic. Actual deployment
inventory remains the responsibility of the independently audited composition.
"""
from contextlib import contextmanager
from pathlib import Path
import re
import time
from .connectsafely_coordination import (SQLiteSocialCoordinator,SocialCapability,CoordinationError,require,HEX,IDENT,FAMILY,POST)

PG_SQL=Path(__file__).with_name('connectsafely_coordination.postgres.sql')
SCHEMA='thunder_social_coordination'
OWNER='thunder_social_owner'
TABLES={'thunder_social_writers':SCHEMA+'.writers','thunder_social_claims':SCHEMA+'.claims','thunder_social_attempts':SCHEMA+'.attempts','thunder_social_audit':SCHEMA+'.audit','thunder_social_history':SCHEMA+'.history','thunder_social_backfill':SCHEMA+'.backfill','thunder_social_quarantine':SCHEMA+'.quarantine'}

class _PG:
    def __init__(self,connection):self.connection=connection
    def execute(self,sql,args=()):
        for old,new in TABLES.items():sql=sql.replace(old,new)
        sql=sql.replace('?','%s')
        cur=self.connection.cursor();cur.execute(sql,args);return cur


def function_sources():
    raw=PG_SQL.read_text()
    return {name:body for name,body in re.findall(r'CREATE FUNCTION thunder_social_coordination\.(\w+)\([^;]*?\nAS \$\$(.*?)\$\$;',raw,re.S)}


def verify_schema(con, *, resolved=False):
    """Catalog proof shared by runtime and explicit admin migration, no caller bypass."""
    owner=con.execute('SELECT rolcanlogin,rolsuper,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=?',(OWNER,)).fetchone()
    require(owner and not any(owner),'social_owner_unverified')
    # Never treat metadata written by the installer as proof of enforcement.
    functions=con.execute("SELECT p.proname,p.prosrc,p.prosecdef,p.proconfig,r.rolname,p.pronargs FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_roles r ON r.oid=p.proowner WHERE n.nspname=?",(SCHEMA,)).fetchall()
    sources=function_sources()
    if resolved:
        from .connectsafely_quarantine_resolution_postgres import resolution_function_sources
        sources.update(resolution_function_sources())
    require(len(functions)==len(sources) and {row[0] for row in functions}==set(sources),'social_schema_changed')
    arities={'guard_touch':0,'claim_touch':0,'immutable_row':0,'guard_attempt':0,'issue':8,'consume':8,'retire':3}
    if resolved:
        arities.update(person_key=1,is_blocked=4,claude_assert_writer=0,claude_reserve=6,claude_complete=4,claude_readback=1)
    for name,body,definer,config,owner_name,nargs in functions:
        require(name in sources and body==sources[name] and definer and owner_name==OWNER and nargs==arities[name]
                and config and [v.replace(' ','') for v in config]==['search_path=pg_catalog,thunder_social_coordination'],'social_schema_changed')
    expected_triggers={('public','toques','thunder_social_guard',31,'guard_touch'),('public','toques','thunder_social_claim',5,'claim_touch'),
      (SCHEMA,'claims','thunder_social_claim_immutable',27,'immutable_row'),(SCHEMA,'audit','thunder_social_audit_immutable',27,'immutable_row'),
      (SCHEMA,'attempts','thunder_social_attempt_monotonic',27,'guard_attempt'),
      (SCHEMA,'history','thunder_social_history_immutable',27,'immutable_row'),(SCHEMA,'backfill','thunder_social_backfill_immutable',27,'immutable_row'),(SCHEMA,'quarantine','thunder_social_quarantine_immutable',27,'immutable_row')}
    if resolved:
        expected_triggers |= {(SCHEMA,'resolution','thunder_social_resolution_immutable',27,'immutable_row'),(SCHEMA,'resolution_evidence','thunder_social_resolution_evidence_immutable',27,'immutable_row'),(SCHEMA,'scoped_exception','thunder_social_scoped_exception_immutable',27,'immutable_row')}
    triggers=con.execute("SELECT n.nspname,c.relname,t.tgname,t.tgtype,p.proname,t.tgenabled,pn.nspname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_namespace pn ON pn.oid=p.pronamespace WHERE NOT t.tgisinternal AND (n.nspname=? OR t.tgname IN ('thunder_social_guard','thunder_social_claim'))",(SCHEMA,)).fetchall()
    require(len(triggers)==len(expected_triggers) and {tuple(row[:5]) for row in triggers}==expected_triggers
            and all(row[5]=='A' and row[6]==SCHEMA for row in triggers),'social_schema_changed')
    for name in ('writers','claims','attempts','audit','history','backfill','quarantine') + (('resolution','resolution_evidence','scoped_exception') if resolved else ()):
        meta=con.execute("SELECT r.rolname,c.relkind,c.relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_roles r ON r.oid=c.relowner WHERE n.nspname=? AND c.relname=?",(SCHEMA,name)).fetchone()
        require(meta==(OWNER,'r',False),'social_schema_changed')
    # Unconditional unique indexes, valid and immediate, enforce cross-writer claims.
    indexes=con.execute("SELECT c.relname,i.indisunique,i.indisvalid,i.indimmediate,pg_get_expr(i.indpred,i.indrelid),ARRAY(SELECT a.attname FROM unnest(i.indkey) WITH ORDINALITY k(num,ord) JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.num ORDER BY k.ord) FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=?",(SCHEMA,)).fetchall()
    required={('claims',('family','post_urn')),('claims',('touch_id',)),('attempts',('effect_key','attempt_token')),('writers',('writer_id',)),('quarantine',('touch_id',)),('history',('touch_id',)),('backfill',('manifest_hash',))}
    if resolved:required |= {('resolution',('touch_id',)),('resolution_evidence',('proposal_sha256',)),('scoped_exception',('touch_id',))}
    proven={(table,tuple(cols)) for table,unique,valid,immediate,predicate,cols in indexes if unique and valid and immediate and predicate is None}
    require(required<=proven,'social_schema_changed')
    active=[r for r in indexes if r[0]=='attempts' and tuple(r[5])==('effect_key',) and all(r[1:4])]
    require(len(active)==1 and active[0][4]=="(state = ANY (ARRAY['issued'::text, 'consumed'::text]))",'social_schema_changed')


class PostgresSocialCoordinator(SQLiteSocialCoordinator):
    @contextmanager
    def _transaction(self):
        raw=None
        try:
            raw=self.connect()
            import psycopg2.extensions
            require(type(raw) is psycopg2.extensions.connection,'social_backend_unsupported')
            con=_PG(raw)
            con.execute("SET LOCAL search_path=pg_catalog,public")
            con.execute('SELECT pg_advisory_xact_lock(871290431)')
            yield con
            raw.commit()
        except CoordinationError:
            if raw:raw.rollback()
            raise
        except Exception:
            if raw:raw.rollback()
            raise CoordinationError('social_coordination_unverified') from None
        finally:
            if raw:raw.close()

    def _schema(self,con):
        verify_schema(con)

    def _history(self,con):
        from .connectsafely_backfill import verify_backfill
        verify_backfill(con,self.backfill_manifest_hash)

    def _verify(self,con):
        inventory_hash=self.inventory.verify()
        privileged=con.execute("SELECT 1 FROM pg_roles WHERE rolname IN (current_user,session_user) AND (rolsuper OR rolcreaterole OR rolcreatedb OR rolbypassrls)").fetchone()
        require(not privileged,'social_caller_overprivileged')
        for role in ('pg_execute_server_program','pg_write_server_files','pg_read_server_files'):
            require(not con.execute('SELECT pg_has_role(current_user,?,\'MEMBER\')',(role,)).fetchone()[0],'social_caller_overprivileged')
        require(not con.execute("SELECT has_schema_privilege(current_user,'public','CREATE')").fetchone()[0],'social_caller_overprivileged')
        owner=con.execute('SELECT rolcanlogin,rolsuper,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=?',(OWNER,)).fetchone()
        require(owner and not any(owner),'social_owner_unverified')
        require(not con.execute('SELECT pg_has_role(current_user,?,\'MEMBER\')',(OWNER,)).fetchone()[0],'social_caller_overprivileged')
        require(not con.execute("SELECT has_schema_privilege(current_user,?,'CREATE')",(SCHEMA,)).fetchone()[0],'social_caller_overprivileged')
        self._schema(con)
        for name in ('writers','claims','attempts','audit','history','backfill','quarantine'):
            for permission in ('INSERT','UPDATE','DELETE','TRUNCATE','TRIGGER'):
                require(not con.execute('SELECT has_table_privilege(current_user,?,?)',(SCHEMA+'.'+name,permission)).fetchone()[0],'social_caller_overprivileged')
        require(not con.execute("SELECT has_table_privilege(current_user,'public.toques','TRIGGER')").fetchone()[0],'social_caller_overprivileged')
        require(not con.execute("SELECT pg_has_role(current_user,c.relowner,'MEMBER') FROM pg_class c WHERE c.oid='public.toques'::regclass").fetchone()[0],'social_caller_overprivileged')
        actual={tuple(r) for r in con.execute('SELECT writer_id,artifact_sha256,audit_sha256,inventory_hash,database_role FROM thunder_social_writers').fetchall()}
        require(all(isinstance(w.database_role,str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}',w.database_role) for w in self.inventory.writers),'social_writer_role_unverified')
        expected={(w.writer_id,w.artifact_sha256,w.audit_sha256,inventory_hash,w.database_role) for w in self.inventory.writers}
        require(actual==expected,'social_writer_inventory_changed')
        self._history(con)
        unknown=con.execute("SELECT 1 FROM public.toques WHERE canal='linkedin' AND acao IN ('curtida','comentario') AND id NOT IN (SELECT touch_id FROM thunder_social_history) AND (agente IS NULL OR agente NOT IN (SELECT writer_id FROM thunder_social_writers)) LIMIT 1").fetchone()
        require(not unknown,'social_writer_unknown')
        return inventory_hash

    def readiness(self):
        result=super().readiness();result['backend']='postgresql';return result

    def issue(self,effect_key,*,account_id,action,post_urn,attempt_token,request_hash):
        require(isinstance(request_hash,str) and HEX.fullmatch(request_hash) and isinstance(attempt_token,str) and IDENT.fullmatch(attempt_token),'social_attempt_invalid')
        with self._transaction() as con:
            inventory=self._verify(con);self._scope(con,effect_key,account_id,action,post_urn)
            expiry=con.execute('SELECT thunder_social_coordination.issue(?,?,?,?,?,?,?,?)',(effect_key,attempt_token,account_id,action,post_urn,request_hash,inventory,self.ttl)).fetchone()[0]
        return SocialCapability(self,effect_key,attempt_token,account_id,action,post_urn,request_hash,expiry)

    def _consume(self,cap,*,action,account_id,post_urn,request_hash):
        require((action,account_id,post_urn,request_hash)==(cap.action,cap.account_id,cap.post_urn,cap.request_hash),'social_capability_scope_mismatch')
        with self._transaction() as con:
            inventory=self._verify(con)
            return con.execute('SELECT thunder_social_coordination.consume(?,?,?,?,?,?,?,?)',(cap.effect_key,cap.attempt_token,account_id,action,post_urn,request_hash,cap.expires_at,inventory)).fetchone()[0] is True

    def retire_unconsumed(self,effect_key,attempt_token):
        with self._transaction() as con:
            inventory=self._verify(con)
            return con.execute('SELECT thunder_social_coordination.retire(?,?,?)',(effect_key,attempt_token,inventory)).fetchone()[0] is True
