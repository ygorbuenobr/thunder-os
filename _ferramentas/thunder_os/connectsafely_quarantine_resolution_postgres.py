"""Explicit PostgreSQL resolution migration and fail-closed runtime verification.

No environment lookup, provider access, auto-migration or production activation.
"""
import json,re
from pathlib import Path
from .connectsafely_coordination import require,digest,encoded,FAMILY,HEX,POST
from .connectsafely_coordination_postgres import PostgresSocialCoordinator,_PG,verify_schema,SCHEMA,OWNER,PG_SQL
from .connectsafely_backfill import verify_backfill,ReviewedMappings
from .connectsafely_quarantine_resolution import plan_resolution
from .connectsafely_quarantine_resolution_evidence import ReviewedReconciliation
SQL=Path(__file__).with_name('connectsafely_quarantine_resolution.postgres.sql')
CLAUDE_ROLE='thunder_claude_social'
CLAUDE_SIGNATURES=('claude_reserve(text,text,text,text,text,text)',
                   'claude_complete(bigint,text,text,text)','claude_readback(bigint)')


def verify_claude_role(con):
    """Accept NOLOGIN staging or enabled LOGIN, but never inherited/shared DML."""
    flags=con.execute('SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=?',(CLAUDE_ROLE,)).fetchone()
    require(flags is not None and not any(flags),'social_claude_role_unverified')
    require(not con.execute('SELECT 1 FROM pg_auth_members WHERE member=?::regrole OR roleid=?::regrole',(CLAUDE_ROLE,CLAUDE_ROLE)).fetchone(),'social_claude_role_unverified')
    for schema in ('public',SCHEMA):
        require(not con.execute('SELECT has_schema_privilege(?,?,\'CREATE\')',(CLAUDE_ROLE,schema)).fetchone()[0],'social_claude_overprivileged')
    for table in ('public.toques','public.thunder_effect_touches')+tuple(SCHEMA+'.'+t for t in ('writers','claims','attempts','audit','history','backfill','quarantine','resolution','resolution_evidence','scoped_exception','effective_quarantine')):
        require(not con.execute('SELECT has_table_privilege(?,?,\'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER\')',(CLAUDE_ROLE,table)).fetchone()[0],'social_claude_overprivileged')
        require(not con.execute('SELECT has_any_column_privilege(?,?,\'SELECT,INSERT,UPDATE,REFERENCES\')',(CLAUDE_ROLE,table)).fetchone()[0],'social_claude_overprivileged')
    functions=con.execute('SELECT p.oid,p.proname,has_function_privilege(?,p.oid,\'EXECUTE\') FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=?',(CLAUDE_ROLE,SCHEMA)).fetchall()
    require({name for _,name,allowed in functions if allowed}=={'claude_reserve','claude_complete','claude_readback'},'social_claude_overprivileged')


def _enroll_claude(con,inventory):
    """Enroll one pinned writer without replacing another writer's contract."""
    pin=inventory.verify()
    expected={(w.writer_id,w.artifact_sha256,w.audit_sha256,pin,w.database_role) for w in inventory.writers}
    claude=[r for r in expected if r[0]=='haiku-connectsafely' and r[4]==CLAUDE_ROLE]
    require(len(claude)==1 and all(r[4]!=CLAUDE_ROLE or r==claude[0] for r in expected),'social_claude_inventory_invalid')
    actual=set(con.execute('SELECT writer_id,artifact_sha256,audit_sha256,inventory_hash,database_role FROM thunder_social_writers').fetchall())
    require(actual<=expected and expected-actual<=set(claude),'social_writer_inventory_changed')
    if claude[0] not in actual:
        con.execute('INSERT INTO thunder_social_writers(writer_id,artifact_sha256,audit_sha256,inventory_hash,database_role) VALUES(?,?,?,?,?)',claude[0])


def resolution_function_sources():
    return {name:body for name,body in re.findall(r'CREATE OR REPLACE FUNCTION thunder_social_coordination\.(\w+)\([^;]*?\nAS \$\$(.*?)\$\$;',SQL.read_text(),re.S)}


def verify_resolution_schema(con):
    verify_schema(con,resolved=True)
    view=con.execute("SELECT r.rolname,c.relkind,pg_get_viewdef(c.oid,true) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_roles r ON r.oid=c.relowner WHERE n.nspname=? AND c.relname='effective_quarantine'",(SCHEMA,)).fetchone()
    # The deparser's whitespace is irrelevant; the exact SQL tokens are pinned.
    expected="""SELECT q.touch_id, CASE WHEN r.touch_id IS NOT NULL THEN 'target'::text WHEN s.touch_id IS NOT NULL THEN 'scoped_inconclusive'::text ELSE q.scope END AS scope, q.family, COALESCE(r.post_urn, q.post_urn) AS post_urn FROM thunder_social_coordination.quarantine q LEFT JOIN thunder_social_coordination.resolution r ON r.touch_id = q.touch_id AND r.family = q.family LEFT JOIN thunder_social_coordination.scoped_exception s ON s.touch_id = q.touch_id AND s.family = q.family;"""
    norm=lambda x:re.sub(r'\s+','',x).replace('(','').replace(')','')
    require(view and tuple(view[:2])==(OWNER,'v') and norm(view[2])==norm(expected),'social_resolution_view_changed')
    for table,expected_cols in [('resolution', [('touch_id','bigint'),('family','text'),('post_urn','text'),('proposal_sha256','text')]),('resolution_evidence',[('proposal_sha256','text'),('proposal_json','jsonb'),('mapping_path','text'),('mapping_sha256','text')]),('scoped_exception',[('touch_id','bigint'),('family','text'),('person_keys','text[]'),('target_urls','text[]'),('hint_post_urns','text[]'),('proposal_sha256','text')])]:
        cols=con.execute("SELECT a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull FROM pg_attribute a WHERE a.attrelid=?::regclass AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum",(SCHEMA+'.'+table,)).fetchall()
        require([(r[0],r[1]) for r in cols]==expected_cols and all(r[2] for r in cols),'social_resolution_schema_changed')
    fks=con.execute("SELECT c.conrelid::regclass::text,c.confrelid::regclass::text,pg_get_constraintdef(c.oid),c.convalidated,c.condeferrable FROM pg_constraint c WHERE c.connamespace=?::regnamespace AND c.contype='f' AND c.conrelid=?::regclass",(SCHEMA,SCHEMA+'.resolution')).fetchall()
    expected_fks={'FOREIGN KEY (touch_id) REFERENCES thunder_social_coordination.quarantine(touch_id)','FOREIGN KEY (proposal_sha256) REFERENCES thunder_social_coordination.resolution_evidence(proposal_sha256)'}
    require(len(fks)==2 and {r[2] for r in fks}==expected_fks and all(r[3] and not r[4] for r in fks),'social_resolution_schema_changed')
    fks=con.execute("SELECT pg_get_constraintdef(oid),convalidated,condeferrable FROM pg_constraint WHERE conrelid=?::regclass AND contype='f'",(SCHEMA+'.scoped_exception',)).fetchall()
    require(len(fks)==2 and {r[0] for r in fks}==expected_fks and all(r[1] and not r[2] for r in fks),'social_resolution_schema_changed')


def verify_resolution_data(con):
    rows=[json.loads(r[0]) for r in con.execute('SELECT row_json FROM thunder_social_history ORDER BY touch_id').fetchall()]
    seen=set();scoped_seen=set()
    quarantined={r[0] for r in con.execute('SELECT touch_id FROM thunder_social_quarantine').fetchall()}
    for pin,proposal,path,mapping_pin in con.execute('SELECT * FROM thunder_social_coordination.resolution_evidence ORDER BY proposal_sha256').fetchall():
        review_type=ReviewedReconciliation if proposal.get('version')==2 else ReviewedMappings
        expected=plan_resolution(rows,review_type(path,mapping_pin),expected_history_hash=proposal['history_sha256'])
        require(proposal==expected and pin==expected['proposal_sha256'],'social_resolution_evidence_changed')
        expected_rows={(e['touch_id'],e['family'],e['post_urn'],pin) for e in proposal['entries'] if e['scope']=='target' and e['touch_id'] in quarantined}
        actual={tuple(r) for r in con.execute('SELECT * FROM thunder_social_coordination.resolution WHERE proposal_sha256=?',(pin,)).fetchall()}
        require(actual<=expected_rows and not seen.intersection(r[0] for r in actual),'social_resolution_materialization_changed')
        for touch,family,post,_ in expected_rows:
            row=con.execute('SELECT family,post_urn FROM thunder_social_coordination.resolution WHERE touch_id=?',(touch,)).fetchone()
            require(row and tuple(row)==(family,post),'social_resolution_materialization_changed')
        seen.update(r[0] for r in actual)
        scoped=[(e['touch_id'],e['family'],e['person_keys'],e['target_urls'],e['hint_post_urns'],pin) for e in proposal['entries'] if e['scope']=='scoped_inconclusive']
        material=con.execute('SELECT * FROM thunder_social_coordination.scoped_exception WHERE proposal_sha256=? ORDER BY touch_id',(pin,)).fetchall()
        require(material==sorted(scoped) and not scoped_seen.intersection(r[0] for r in scoped),'social_resolution_materialization_changed')
        scoped_seen.update(r[0] for r in scoped)
    require(con.execute('SELECT count(*) FROM thunder_social_coordination.resolution').fetchone()[0]==len(seen),'social_resolution_unverified')
    require(not seen.intersection(scoped_seen) and con.execute('SELECT count(*) FROM thunder_social_coordination.scoped_exception').fetchone()[0]==len(scoped_seen),'social_resolution_unverified')
    return con.execute("SELECT count(*) FROM thunder_social_coordination.effective_quarantine WHERE scope='global'").fetchone()[0]


def apply_postgres_resolution(connection,mappings,*,history_manifest_hash,reviewed_proposal_sha256,inventory=None):
    require(connection.get_transaction_status()==0,'social_transaction_already_open');con=_PG(connection)
    try:
        con.execute('SET LOCAL search_path=pg_catalog,public');con.execute("SET LOCAL lock_timeout='5s'");con.execute("SET LOCAL statement_timeout='30s'");con.execute('SELECT pg_advisory_xact_lock(871290431)');con.execute('LOCK TABLE public.toques IN SHARE ROW EXCLUSIVE MODE')
        con.execute('LOCK TABLE thunder_social_coordination.writers IN SHARE ROW EXCLUSIVE MODE')
        verify_backfill(con,history_manifest_hash,allow_quarantine=True)
        rows=[json.loads(r[0]) for r in con.execute('SELECT row_json FROM thunder_social_history ORDER BY touch_id').fetchall()]
        proposal=plan_resolution(rows,mappings,expected_history_hash=digest(encoded(rows)))
        require(proposal['proposal_sha256']==reviewed_proposal_sha256,'social_resolution_review_mismatch')
        exists=con.execute("SELECT to_regclass('thunder_social_coordination.resolution')").fetchone()[0]
        if exists:
            verify_resolution_schema(con);verify_resolution_data(con)
            prior=con.execute('SELECT proposal_json FROM thunder_social_coordination.resolution_evidence WHERE proposal_sha256=?',(reviewed_proposal_sha256,)).fetchone()
            if prior:
                require(prior[0]==proposal,'social_resolution_evidence_changed')
                if inventory is not None: _enroll_claude(con,inventory)
                verify_claude_role(con)
                connection.commit();return False
        else:
            verify_schema(con);sql=SQL.read_text();require(sql.count('BEGIN;')==1 and sql.endswith('ROLLBACK;\n'),'social_resolution_sql_changed')
            connection.cursor().execute(sql.replace('BEGIN;','',1)[:-len('ROLLBACK;\n')])
        con.execute('INSERT INTO thunder_social_coordination.resolution_evidence VALUES(?,?::jsonb,?,?)',(reviewed_proposal_sha256,encoded(proposal).decode(),mappings.path,mappings.sha256))
        for e in proposal['entries']:
            if e['scope']=='scoped_inconclusive':
                con.execute('INSERT INTO thunder_social_coordination.scoped_exception VALUES(?,?,?,?,?,?)',(e['touch_id'],e['family'],e['person_keys'],e['target_urls'],e['hint_post_urns'],reviewed_proposal_sha256))
            if e['scope']!='target':continue
            # History can also contain preexisting canonical claims. Those remain
            # authoritative without inserting a resolution FK to a missing fence.
            if not con.execute('SELECT 1 FROM thunder_social_quarantine WHERE touch_id=?',(e['touch_id'],)).fetchone():continue
            prior=con.execute('SELECT family,post_urn FROM thunder_social_coordination.resolution WHERE touch_id=?',(e['touch_id'],)).fetchone()
            if prior:require(tuple(prior)==(e['family'],e['post_urn']),'social_resolution_conflict')
            else:con.execute('INSERT INTO thunder_social_coordination.resolution VALUES(?,?,?,?)',(e['touch_id'],e['family'],e['post_urn'],reviewed_proposal_sha256))
        if inventory is not None:_enroll_claude(con,inventory)
        verify_resolution_schema(con);verify_resolution_data(con);verify_claude_role(con);connection.commit();return True
    except Exception:connection.rollback();raise


class PostgresResolvedSocialCoordinator(PostgresSocialCoordinator):
    def _schema(self,con):
        verify_resolution_schema(con)
        verify_claude_role(con)
        for table in ('resolution','resolution_evidence','scoped_exception','effective_quarantine'):
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
        require(row and row[1]=='reservado' and row[2] is None,'social_reservation_unverified')
        blocked=con.execute("SELECT 1 FROM public.toques t JOIN thunder_social_coordination.scoped_exception s ON (?=ANY(s.hint_post_urns) OR t.alvo=ANY(s.target_urls) OR lower(rtrim(regexp_replace(t.pessoa_chave,?,''),'/'))=ANY(s.person_keys)) WHERE t.id=? LIMIT 1",(post,'^https?://(www[.])?linkedin[.]com',row[0])).fetchone()
        require(not blocked,'social_quarantine_unresolved')
        return row[0]


def restore_global_guards(connection):
    """Reinstate raw quarantine guards, retaining evidence/claims/receipts.

    Runtime readiness deliberately fails after restoring old function bodies.
    No destructive schema down migration or grants are performed.
    """
    require(connection.get_transaction_status()==0,'social_transaction_already_open')
    try:
        con=_PG(connection);con.execute("SET LOCAL lock_timeout='5s'");con.execute("SET LOCAL statement_timeout='30s'");con.execute('SELECT pg_advisory_xact_lock(871290431)');con.execute('LOCK TABLE public.toques IN SHARE ROW EXCLUSIVE MODE')
        verify_resolution_schema(con)
        raw=PG_SQL.read_text()
        for name in ('guard_touch','issue','consume'):
            start=raw.index('CREATE FUNCTION thunder_social_coordination.'+name+'(');end=raw.index('$$;',start)+3
            connection.cursor().execute(raw[start:end].replace('CREATE FUNCTION','CREATE OR REPLACE FUNCTION',1))
        connection.commit()
    except Exception:connection.rollback();raise
