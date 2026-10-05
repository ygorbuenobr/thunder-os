"""Explicit reviewed migration; never called by startup. No provider or DSN lookup.

Exact activity identifiers and explicitly reviewed mappings form blocking claims.
Ambiguous/colliding history is preserved in database-enforced quarantine; hints
never establish equivalence. Every historical row remains immutable. Review is
an out-of-band operator decision with an independently supplied manifest hash.
"""
import json
import re
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from dataclasses import dataclass
from pathlib import Path
from .connectsafely_coordination import CoordinationError, require, digest, encoded, SQL_PATH

SOCIAL="canal='linkedin' AND acao IN ('curtida','comentario')"
HINT=re.compile(r'https://www\.linkedin\.com/posts/[^/?#]*-activity-([0-9]{1,25})(?:-[^/?#]*)?/?(?:[?#].*)?')
TARGET=re.compile(r'(?:urn:li:activity:|https://www\.linkedin\.com/feed/update/urn:li:activity:)([0-9]{1,25})/?')


def _value(value):
    if isinstance(value,datetime) and value.tzinfo is not None:value=value.astimezone(timezone.utc)
    if isinstance(value,(datetime,date)):return {'type':type(value).__name__,'value':value.isoformat()}
    if isinstance(value,Decimal):return {'type':'decimal','value':str(value)}
    if isinstance(value,bytes):return {'type':'bytes','hex':value.hex()}
    require(value is None or type(value) in (str,int,float,bool),'social_history_type_unsupported')
    return value


def _touch_table(con):
    # Match the table protected by the installed triggers/FKs, regardless of a
    # caller's DSN/session search_path. Dry-run itself must remain read-only.
    if type(con) is sqlite3.Connection:return 'toques'
    from .connectsafely_coordination_postgres import _PG
    require(type(con) is _PG,'social_backend_unsupported')
    return 'public.toques'


def _rows(con):
    cur=con.execute('SELECT * FROM '+_touch_table(con)+' WHERE '+SOCIAL+' ORDER BY id')
    names=[c[0] for c in cur.description]
    return [dict(zip(names,map(_value,row))) for row in cur.fetchall()]


@dataclass(frozen=True)
class ReviewedMappings:
    """Explicit operator-reviewed resolution artifact, never a boolean trust flag.

    `sha256` is provided independently by the reviewed deployment/migration.
    Mapping records and their supporting sources are pinned. This validates
    integrity/provenance, not the truth of arbitrary operator-authored evidence.
    """
    path:str
    sha256:str

    def verify(self):
        def read(path,pin):
            artifact=Path(path)
            require(artifact.is_absolute() and artifact.is_file() and not artifact.is_symlink(),'social_mapping_source_invalid')
            require(artifact.stat().st_size<=1000000,'social_mapping_source_invalid')
            raw=artifact.read_bytes()
            require(isinstance(pin,str) and re.fullmatch('[a-f0-9]{64}',pin) and digest(raw)==pin,'social_mapping_source_changed')
            return raw
        data=json.loads(read(self.path,self.sha256))
        require(isinstance(data,dict) and set(data)=={'version','decision_ref','mappings'} and data['version']==1
                and isinstance(data['decision_ref'],str) and data['decision_ref'].strip()
                and isinstance(data['mappings'],list),'social_mapping_review_invalid')
        urls=set()
        for row in data['mappings']:
            require(isinstance(row,dict) and set(row)=={'source_url','post_urn','source_path','source_sha256','source_ref'}
                    and isinstance(row['source_url'],str) and row['source_url'].startswith('https://www.linkedin.com/')
                    and row['source_url'] not in urls and isinstance(row['post_urn'],str)
                    and re.fullmatch(r'urn:li:activity:[0-9]{1,25}',row['post_urn'])
                    and isinstance(row['source_ref'],str) and row['source_ref'].strip(),'social_mapping_review_invalid')
            urls.add(row['source_url']);read(row['source_path'],row['source_sha256'])
        return data


def _plan(rows,mappings=None):
    require(mappings is None or type(mappings) is ReviewedMappings,'social_mapping_review_invalid')
    reviewed=mappings.verify() if mappings else None
    bindings={r['source_url']:r for r in reviewed['mappings']} if reviewed else {}
    entries=[];blockers=[];seen={};hints={}
    for row in rows:
        target=row.get('alvo');match=TARGET.fullmatch(target) if isinstance(target,str) else None
        hint=HINT.fullmatch(target) if isinstance(target,str) else None
        post='urn:li:activity:'+match[1] if match else None
        mapped=bindings.get(target) if isinstance(target,str) else None
        if mapped:
            require(post is None or post==mapped['post_urn'],'social_mapping_conflicts_with_exact_identity')
            post=mapped['post_urn']
        entry={'touch_id':row['id'],'row':row,'family':row['acao'],'post_urn':post,
               'hint_post_urn':'urn:li:activity:'+hint[1] if hint else None,
               'rule':'reviewed-mapping-v1' if mapped else 'exact-activity-v1' if match else None,
               'disposition':'claim' if post else 'quarantine',
               'quarantine_scope':None if post else 'global',
               'reason':None if post else 'identity_not_proven',
               'receipt_evidence':'historic_receipt_unverified' if row.get('provider_id') else 'no_receipt'}
        entries.append(entry)
        if post:seen.setdefault((row['acao'],post),[]).append(entry)
        if hint:hints.setdefault((row['acao'],entry['hint_post_urn']),[]).append(entry)
    # Never pick a representative from colliding rows, including same-person rows.
    for (family,post),group in sorted(seen.items()):
        if len(group)>1:
            for entry in group:entry.update(disposition='quarantine',quarantine_scope='target',reason='identity_collision')
    for group in hints.values():
        if len(group)>1 and any(entry['post_urn'] is None for entry in group):
            # Do not partially promote one member of an unresolved hint collision.
            # Retain any reviewed mapping in the manifest, but fence the whole group.
            for entry in group:entry.update(disposition='quarantine',quarantine_scope='global',reason='unproven_hint_collision')
    for entry in entries:
        if entry['disposition']=='quarantine':
            blockers.append({'code':entry['reason'],'touch_ids':[entry['touch_id']],
                             'scope':entry['quarantine_scope'],'family':entry['family'],
                             'post_urn':entry['post_urn'],'hint_post_urn':entry['hint_post_urn']})
    manifest={'version':2,'policy':'preserve-quarantine-history-v2','entries':entries,'blockers':blockers,
              'mapping_evidence':{'path':mappings.path,'sha256':mappings.sha256,'decision_ref':reviewed['decision_ref']} if mappings else None}
    global_count=sum(e['quarantine_scope']=='global' for e in entries)
    return {'manifest':manifest,'manifest_hash':digest(encoded(manifest)),
            'ready':global_count==0,'migration_ready':True,'social_count':len(rows),
            'claim_count':sum(e['disposition']=='claim' for e in entries),
            'quarantined_count':len(blockers),'global_quarantine_count':global_count,
            'target_quarantine_count':len(blockers)-global_count}


def dry_run(con,*,mappings=None):
    """Read-only deterministic plan. `ready` means no global quarantine, not approval.

    `migration_ready` may be true while all social execution remains blocked.
    Raw history and aliases are never rewritten or inferred to be equivalent.
    """
    return _plan(_rows(con),mappings)


def _review(plan,expected):
    require(isinstance(expected,str) and digest(encoded(plan['manifest']))==expected==plan['manifest_hash'],'social_backfill_review_mismatch')
    require(plan['migration_ready'],'social_backfill_blocked')


def verify_backfill(con,expected,*,allow_quarantine=False):
    manifests=con.execute('SELECT manifest_hash,manifest_json FROM thunder_social_backfill').fetchall()
    history=con.execute('SELECT touch_id,row_json,family,post_urn,manifest_hash FROM thunder_social_history ORDER BY touch_id').fetchall()
    quarantines=con.execute('SELECT touch_id,scope,family,post_urn,reason FROM thunder_social_quarantine ORDER BY touch_id').fetchall()
    if not manifests:
        require(not history and not quarantines and expected is None,'social_backfill_unverified');return
    require(len(manifests)==1 and manifests[0][0]==expected,'social_backfill_unverified')
    manifest=json.loads(manifests[0][1])
    require(digest(encoded(manifest))==expected and manifest.get('version')==2,'social_backfill_changed')
    mapping=manifest.get('mapping_evidence')
    mappings=ReviewedMappings(mapping['path'],mapping['sha256']) if mapping else None
    entries=manifest['entries']
    # Recompute all classifications; a pinned self-inconsistent manifest is invalid.
    plan=_plan([e['row'] for e in entries],mappings)
    require(plan['manifest']==manifest,'social_backfill_changed')
    require(len(entries)==len(history),'social_backfill_changed')
    current={r['id']:r for r in _rows(con)}
    expected_quarantine=[]
    for entry,row in zip(entries,history):
        require(tuple(row)==(entry['touch_id'],encoded(entry['row']).decode(),entry['family'],entry['post_urn'],expected),'social_backfill_changed')
        require(current.get(entry['touch_id'])==entry['row'],'social_history_changed')
        claim=con.execute('SELECT family,post_urn FROM thunder_social_claims WHERE touch_id=?',(entry['touch_id'],)).fetchone()
        if entry['disposition']=='claim':
            require(claim and tuple(claim)==(entry['family'],entry['post_urn']),'social_backfill_claim_missing')
        else:
            require(not claim,'social_quarantine_promoted')
            expected_quarantine.append((entry['touch_id'],entry['quarantine_scope'],entry['family'],entry['post_urn'] if entry['quarantine_scope']=='target' else None,entry['reason']))
    require([tuple(r) for r in quarantines]==expected_quarantine,'social_quarantine_changed')
    if not allow_quarantine:require(plan['global_quarantine_count']==0,'social_quarantine_unresolved')
    return {k:plan[k] for k in ('social_count','claim_count','quarantined_count','global_quarantine_count','target_quarantine_count')}


def _seed(con,plan):
    manifest=plan['manifest'];pin=plan['manifest_hash']
    con.execute('INSERT INTO thunder_social_backfill VALUES(?,?)',(pin,encoded(manifest).decode()))
    for entry in manifest['entries']:
        con.execute('INSERT INTO thunder_social_history VALUES(?,?,?,?,?)',(entry['touch_id'],encoded(entry['row']).decode(),entry['family'],entry['post_urn'],pin))
        if entry['disposition']=='claim':
            con.execute('INSERT INTO thunder_social_claims VALUES(?,?,?)',(entry['family'],entry['post_urn'],entry['touch_id']))
        else:
            con.execute('INSERT INTO thunder_social_quarantine VALUES(?,?,?,?,?)',(entry['touch_id'],entry['quarantine_scope'],entry['family'],entry['post_urn'] if entry['quarantine_scope']=='target' else None,entry['reason']))


def _sqlite_statements(con,script):
    # executescript would silently COMMIT an existing transaction. Never use it here.
    statement=''
    for line in script.splitlines(True):
        statement+=line
        if sqlite3.complete_statement(statement):con.execute(statement);statement=''
    require(not statement.strip(),'social_migration_invalid')


def apply_sqlite(con,inventory,*,reviewed_manifest_hash,mappings=None):
    require(type(con) is sqlite3.Connection and not con.in_transaction,'social_transaction_already_open')
    pin=inventory.verify()
    try:
        con.execute('BEGIN IMMEDIATE')
        exists=con.execute("SELECT 1 FROM sqlite_master WHERE name='thunder_social_backfill'").fetchone()
        if exists:
            from .connectsafely_coordination import definitions,expected_definitions
            require(definitions(con)==expected_definitions(),'social_schema_changed')
            verify_backfill(con,reviewed_manifest_hash,allow_quarantine=True)
            actual={tuple(r) for r in con.execute('SELECT * FROM thunder_social_writers')}
            require(actual=={(w.writer_id,w.artifact_sha256,w.audit_sha256,pin) for w in inventory.writers},'social_writer_inventory_changed')
            con.commit();return {'applied':False,'manifest_hash':reviewed_manifest_hash}
        plan=dry_run(con,mappings=mappings);_review(plan,reviewed_manifest_hash)
        _sqlite_statements(con,SQL_PATH.read_text())
        con.executemany('INSERT INTO thunder_social_writers VALUES(?,?,?,?)',[(w.writer_id,w.artifact_sha256,w.audit_sha256,pin) for w in inventory.writers])
        _seed(con,plan);verify_backfill(con,reviewed_manifest_hash,allow_quarantine=True)
        con.commit();return {'applied':True,'manifest_hash':reviewed_manifest_hash}
    except Exception:
        con.rollback();raise


def rollback_sqlite(con,*,reviewed_manifest_hash):
    """Pre-effect rollback only. Historic data untouched; refuses any new activity."""
    require(type(con) is sqlite3.Connection and not con.in_transaction,'social_transaction_already_open')
    try:
        con.execute('BEGIN IMMEDIATE')
        from .connectsafely_coordination import definitions,expected_definitions
        require(definitions(con)==expected_definitions(),'social_schema_changed')
        _rollback_check(con,reviewed_manifest_hash)
        objects=con.execute("SELECT type,name FROM sqlite_master WHERE name LIKE 'thunder_social_%' AND type IN ('trigger','table') ORDER BY CASE type WHEN 'trigger' THEN 0 ELSE 1 END").fetchall()
        for kind,name in objects:
            if kind=='trigger':con.execute('DROP TRIGGER '+name)
        for name in ('attempts','audit','claims','quarantine','history','backfill','writers'):con.execute('DROP TABLE thunder_social_'+name)
        con.commit()
    except Exception:
        con.rollback();raise


def _rollback_check(con,pin):
    counts=verify_backfill(con,pin,allow_quarantine=True)
    require(not con.execute('SELECT 1 FROM thunder_social_attempts LIMIT 1').fetchone() and not con.execute('SELECT 1 FROM thunder_social_audit LIMIT 1').fetchone(),'social_rollback_after_activity')
    require(con.execute('SELECT COUNT(*) FROM thunder_social_claims').fetchone()[0]==counts['claim_count'],'social_rollback_after_activity')
    require(not con.execute('SELECT 1 FROM '+_touch_table(con)+' WHERE '+SOCIAL+' AND id NOT IN (SELECT touch_id FROM thunder_social_history) LIMIT 1').fetchone(),'social_rollback_after_activity')


def apply_postgres(connection,inventory,*,reviewed_manifest_hash,mappings=None):
    """Explicit admin migration; callers supply isolated/admin connection, no discovery."""
    from .connectsafely_coordination_postgres import _PG,PG_SQL,verify_schema
    require(connection.get_transaction_status()==0,'social_transaction_already_open')
    con=_PG(connection);pin=inventory.verify()
    try:
        con.execute('SET LOCAL search_path=pg_catalog,public')
        con.execute('SELECT pg_advisory_xact_lock(871290431)')
        con.execute('LOCK TABLE public.toques IN SHARE ROW EXCLUSIVE MODE')
        # This mapping belongs to the canonical ledger, not this migration.
        # Never invent/provision it or grant its runtime access implicitly.
        require(con.execute("SELECT to_regclass('public.thunder_effect_touches')").fetchone()[0] is not None,'social_effect_mapping_schema_required')
        columns=con.execute("SELECT a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull FROM pg_attribute a WHERE a.attrelid='public.thunder_effect_touches'::regclass AND a.attnum>0 AND NOT a.attisdropped").fetchall()
        by_name={row[0]:(row[1],row[2]) for row in columns}
        require(by_name.get('effect_key')==('text',True) and by_name.get('touch_id')==('bigint',True),'social_effect_mapping_schema_required')
        keys=con.execute("SELECT ARRAY(SELECT a.attname FROM unnest(i.indkey) WITH ORDINALITY k(num,ord) JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.num ORDER BY k.ord) FROM pg_index i WHERE i.indrelid='public.thunder_effect_touches'::regclass AND i.indisprimary AND i.indisvalid AND i.indimmediate AND i.indpred IS NULL").fetchall()
        require(keys==[(['effect_key'],)],'social_effect_mapping_schema_required')
        # Hold an ACCESS SHARE relation lock through installation against DDL drift.
        con.execute('SELECT effect_key,touch_id FROM public.thunder_effect_touches LIMIT 0')
        if con.execute("SELECT to_regclass('thunder_social_coordination.backfill')").fetchone()[0]:
            verify_schema(con)
            verify_backfill(con,reviewed_manifest_hash,allow_quarantine=True)
            # Full runtime schema/role verification is separately mandatory.
            actual={tuple(r) for r in con.execute('SELECT writer_id,artifact_sha256,audit_sha256,inventory_hash,database_role FROM thunder_social_writers').fetchall()}
            require(actual=={(w.writer_id,w.artifact_sha256,w.audit_sha256,pin,w.database_role) for w in inventory.writers},'social_writer_inventory_changed')
            connection.commit();return {'applied':False,'manifest_hash':reviewed_manifest_hash}
        plan=dry_run(con,mappings=mappings);_review(plan,reviewed_manifest_hash)
        # Retained NOLOGIN roles may be reused only with audited memberships.
        # Never reset/drop preexisting role grants to make installation succeed.
        members=con.execute("SELECT target.rolname,member.rolname FROM pg_auth_members m JOIN pg_roles target ON target.oid=m.roleid JOIN pg_roles member ON member.oid=m.member WHERE target.rolname IN ('thunder_social_owner','thunder_social_runtime') OR member.rolname IN ('thunder_social_owner','thunder_social_runtime')").fetchall()
        allowed={( 'thunder_social_runtime',w.database_role) for w in inventory.writers}
        require(set(members)<=allowed,'social_existing_role_membership_unverified')
        sql=PG_SQL.read_text()
        guard="DO $$ BEGIN\n IF EXISTS(SELECT 1 FROM public.toques WHERE canal='linkedin' AND acao IN ('curtida','comentario')) THEN RAISE EXCEPTION 'social_backfill_review_required'; END IF;\nEND $$;"
        require(sql.count(guard)==1,'social_migration_invalid')
        sql=sql.replace(guard,'').replace('BEGIN;\n','',1)
        require(sql.endswith('COMMIT;\n'),'social_migration_invalid');sql=sql[:-len('COMMIT;\n')]
        # Raw cursor avoids the query adapter's table-name replacements inside SQL bodies.
        connection.cursor().execute(sql)
        for w in inventory.writers:
            require(isinstance(w.database_role,str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}',w.database_role),'social_writer_role_unverified')
            con.execute('INSERT INTO thunder_social_writers(writer_id,artifact_sha256,audit_sha256,inventory_hash,database_role) VALUES(?,?,?,?,?)',(w.writer_id,w.artifact_sha256,w.audit_sha256,pin,w.database_role))
        _seed(con,plan);verify_backfill(con,reviewed_manifest_hash,allow_quarantine=True);verify_schema(con)
        connection.commit();return {'applied':True,'manifest_hash':reviewed_manifest_hash}
    except Exception:
        connection.rollback();raise


def rollback_postgres(connection,*,reviewed_manifest_hash):
    """Removes only this schema/triggers, retaining NOLOGIN roles and all history."""
    from .connectsafely_coordination_postgres import _PG,verify_schema
    require(connection.get_transaction_status()==0,'social_transaction_already_open')
    con=_PG(connection)
    try:
        con.execute('SET LOCAL search_path=pg_catalog,public')
        con.execute('SELECT pg_advisory_xact_lock(871290431)')
        con.execute('LOCK TABLE public.toques IN SHARE ROW EXCLUSIVE MODE')
        verify_schema(con)
        _rollback_check(con,reviewed_manifest_hash)
        # RESTRICT dropping enumerated functions/tables avoids cascading into caller objects.
        con.execute('DROP TRIGGER thunder_social_guard ON public.toques')
        con.execute('DROP TRIGGER thunder_social_claim ON public.toques')
        for name in ('attempts','audit','claims','quarantine','history','backfill','writers'):
            con.execute('DROP TABLE thunder_social_coordination.'+name)
        signatures=('guard_touch()','claim_touch()','immutable_row()','guard_attempt()',
                    'issue(text,text,text,text,text,text,text,integer)',
                    'consume(text,text,text,text,text,text,double precision,text)','retire(text,text,text)')
        for sig in signatures:con.execute('DROP FUNCTION thunder_social_coordination.'+sig)
        con.execute('DROP SCHEMA thunder_social_coordination')
        connection.commit()
    except Exception:
        connection.rollback();raise
