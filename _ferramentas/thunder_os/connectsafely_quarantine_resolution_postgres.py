"""Explicit PostgreSQL resolution migration and fail-closed runtime verification.

No environment lookup, provider access, auto-migration or production activation.
"""
import json,re
from pathlib import Path
from .connectsafely_coordination import require,digest,encoded,FAMILY,HEX,POST
from .connectsafely_coordination_postgres import PostgresSocialCoordinator,_PG,verify_schema,SCHEMA,OWNER,PG_SQL
from .connectsafely_backfill import verify_backfill,ReviewedMappings
from .connectsafely_quarantine_resolution import plan_resolution
SQL=Path(__file__).with_name('connectsafely_quarantine_resolution.postgres.sql')


def resolution_function_sources():
    return {name:body for name,body in re.findall(r'CREATE OR REPLACE FUNCTION thunder_social_coordination\.(\w+)\([^;]*?\nAS \$\$(.*?)\$\$;',SQL.read_text(),re.S)}


def verify_resolution_schema(con):
    verify_schema(con,resolved=True)
    view=con.execute("SELECT r.rolname,c.relkind,pg_get_viewdef(c.oid,true) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_roles r ON r.oid=c.relowner WHERE n.nspname=? AND c.relname='effective_quarantine'",(SCHEMA,)).fetchone()
    # The deparser's whitespace is irrelevant; the exact SQL tokens are pinned.
    expected="""SELECT q.touch_id, CASE WHEN r.touch_id IS NULL THEN q.scope ELSE 'target'::text END AS scope, q.family, COALESCE(r.post_urn, q.post_urn) AS post_urn FROM thunder_social_coordination.quarantine q LEFT JOIN thunder_social_coordination.resolution r ON r.touch_id = q.touch_id AND r.family = q.family;"""
    norm=lambda x:re.sub(r'\s+','',x).replace('(','').replace(')','')
    require(view and tuple(view[:2])==(OWNER,'v') and norm(view[2])==norm(expected),'social_resolution_view_changed')
    for table,expected_cols in [('resolution', [('touch_id','bigint'),('family','text'),('post_urn','text'),('proposal_sha256','text')]),('resolution_evidence',[('proposal_sha256','text'),('proposal_json','jsonb'),('mapping_path','text'),('mapping_sha256','text')])]:
        cols=con.execute("SELECT a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull FROM pg_attribute a WHERE a.attrelid=?::regclass AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum",(SCHEMA+'.'+table,)).fetchall()
        require([(r[0],r[1]) for r in cols]==expected_cols and all(r[2] for r in cols),'social_resolution_schema_changed')
    fks=con.execute("SELECT c.conrelid::regclass::text,c.confrelid::regclass::text,pg_get_constraintdef(c.oid),c.convalidated,c.condeferrable FROM pg_constraint c WHERE c.connamespace=?::regnamespace AND c.contype='f' AND c.conrelid=?::regclass",(SCHEMA,SCHEMA+'.resolution')).fetchall()
    expected_fks={'FOREIGN KEY (touch_id) REFERENCES thunder_social_coordination.quarantine(touch_id)','FOREIGN KEY (proposal_sha256) REFERENCES thunder_social_coordination.resolution_evidence(proposal_sha256)'}
    require(len(fks)==2 and {r[2] for r in fks}==expected_fks and all(r[3] and not r[4] for r in fks),'social_resolution_schema_changed')


def verify_resolution_data(con):
    rows=[json.loads(r[0]) for r in con.execute('SELECT row_json FROM thunder_social_history ORDER BY touch_id').fetchall()]
    seen=set()
    for pin,proposal,path,mapping_pin in con.execute('SELECT * FROM thunder_social_coordination.resolution_evidence ORDER BY proposal_sha256').fetchall():
        expected=plan_resolution(rows,ReviewedMappings(path,mapping_pin),expected_history_hash=proposal['history_sha256'])
        require(proposal==expected and pin==expected['proposal_sha256'],'social_resolution_evidence_changed')
        expected_rows={(e['touch_id'],e['family'],e['post_urn'],pin) for e in proposal['entries'] if e['scope']=='target'}
        actual={tuple(r) for r in con.execute('SELECT * FROM thunder_social_coordination.resolution WHERE proposal_sha256=?',(pin,)).fetchall()}
        require(actual<=expected_rows and not seen.intersection(r[0] for r in actual),'social_resolution_materialization_changed')
        for touch,family,post,_ in expected_rows:
            row=con.execute('SELECT family,post_urn FROM thunder_social_coordination.resolution WHERE touch_id=?',(touch,)).fetchone()
            require(row and tuple(row)==(family,post),'social_resolution_materialization_changed')
        seen.update(r[0] for r in actual)
    require(con.execute('SELECT count(*) FROM thunder_social_coordination.resolution').fetchone()[0]==len(seen),'social_resolution_unverified')
    return con.execute("SELECT count(*) FROM thunder_social_coordination.effective_quarantine WHERE scope='global'").fetchone()[0]


def apply_postgres_resolution(connection,mappings,*,history_manifest_hash,reviewed_proposal_sha256):
    require(connection.get_transaction_status()==0,'social_transaction_already_open');con=_PG(connection)
    try:
        con.execute('SET LOCAL search_path=pg_catalog,public');con.execute("SET LOCAL lock_timeout='5s'");con.execute('SELECT pg_advisory_xact_lock(871290431)');con.execute('LOCK TABLE public.toques IN SHARE ROW EXCLUSIVE MODE')
        verify_backfill(con,history_manifest_hash,allow_quarantine=True)
        rows=[json.loads(r[0]) for r in con.execute('SELECT row_json FROM thunder_social_history ORDER BY touch_id').fetchall()]
        proposal=plan_resolution(rows,mappings,expected_history_hash=digest(encoded(rows)))
        require(proposal['proposal_sha256']==reviewed_proposal_sha256,'social_resolution_review_mismatch')
        exists=con.execute("SELECT to_regclass('thunder_social_coordination.resolution')").fetchone()[0]
        if exists:
            verify_resolution_schema(con);verify_resolution_data(con)
            prior=con.execute('SELECT proposal_json FROM thunder_social_coordination.resolution_evidence WHERE proposal_sha256=?',(reviewed_proposal_sha256,)).fetchone()
            if prior:
                require(prior[0]==proposal,'social_resolution_evidence_changed');connection.commit();return False
        else:
            verify_schema(con);sql=SQL.read_text();require(sql.count('BEGIN;')==1 and sql.endswith('ROLLBACK;\n'),'social_resolution_sql_changed')
            connection.cursor().execute(sql.replace('BEGIN;','',1)[:-len('ROLLBACK;\n')])
        con.execute('INSERT INTO thunder_social_coordination.resolution_evidence VALUES(?,?::jsonb,?,?)',(reviewed_proposal_sha256,encoded(proposal).decode(),mappings.path,mappings.sha256))
        for e in proposal['entries']:
            if e['scope']!='target':continue
            prior=con.execute('SELECT family,post_urn FROM thunder_social_coordination.resolution WHERE touch_id=?',(e['touch_id'],)).fetchone()
            if prior:require(tuple(prior)==(e['family'],e['post_urn']),'social_resolution_conflict')
            else:con.execute('INSERT INTO thunder_social_coordination.resolution VALUES(?,?,?,?)',(e['touch_id'],e['family'],e['post_urn'],reviewed_proposal_sha256))
        verify_resolution_schema(con);verify_resolution_data(con);connection.commit();return True
    except Exception:connection.rollback();raise


class PostgresResolvedSocialCoordinator(PostgresSocialCoordinator):
    def _schema(self,con):
        verify_resolution_schema(con)
        for table in ('resolution','resolution_evidence','effective_quarantine'):
            for privilege in ('INSERT','UPDATE','DELETE','TRUNCATE','TRIGGER'):
                require(not con.execute('SELECT has_table_privilege(current_user,?,?)',(SCHEMA+'.'+table,privilege)).fetchone()[0],'social_caller_overprivileged')
            for privilege in ('INSERT','UPDATE','REFERENCES'):
                require(not con.execute('SELECT has_any_column_privilege(current_user,?,?)',(SCHEMA+'.'+table,privilege)).fetchone()[0],'social_caller_overprivileged')
    def _history(self,con):
        verify_backfill(con,self.backfill_manifest_hash,allow_quarantine=True)
        require(verify_resolution_data(con)==0,'social_quarantine_unresolved')
    def _scope(self,con,effect,account,action,post):
        require(isinstance(effect,str) and HEX.fullmatch(effect) and isinstance(account,str) and re.fullmatch(r'[a-f0-9]{24}',account),'social_capability_scope_invalid')
        require(action in FAMILY and isinstance(post,str) and POST.fullmatch(post),'social_capability_scope_invalid')
        require(not con.execute("SELECT 1 FROM thunder_social_coordination.effective_quarantine WHERE scope='global' OR (family=? AND post_urn=?) LIMIT 1",(FAMILY[action],post)).fetchone(),'social_quarantine_unresolved')
        row=con.execute("SELECT t.id,t.status,t.provider_id FROM thunder_effect_touches e JOIN toques t ON t.id=e.touch_id JOIN thunder_social_claims c ON c.touch_id=t.id WHERE t.id NOT IN (SELECT touch_id FROM thunder_social_history) AND e.effect_key=? AND c.family=? AND c.post_urn=?",(effect,FAMILY[action],post)).fetchone()
        require(row and row[1]=='reservado' and row[2] is None,'social_reservation_unverified');return row[0]


def restore_global_guards(connection):
    """Reinstate raw quarantine guards, retaining evidence/claims/receipts.

    Runtime readiness deliberately fails after restoring old function bodies.
    No destructive schema down migration or grants are performed.
    """
    require(connection.get_transaction_status()==0,'social_transaction_already_open')
    try:
        con=_PG(connection);con.execute('SELECT pg_advisory_xact_lock(871290431)');con.execute('LOCK TABLE public.toques IN SHARE ROW EXCLUSIVE MODE')
        verify_resolution_schema(con)
        raw=PG_SQL.read_text()
        for name in ('guard_touch','issue','consume'):
            start=raw.index('CREATE FUNCTION thunder_social_coordination.'+name+'(');end=raw.index('$$;',start)+3
            connection.cursor().execute(raw[start:end].replace('CREATE FUNCTION','CREATE OR REPLACE FUNCTION',1))
        connection.commit()
    except Exception:connection.rollback();raise
