"""Read-only proposal for resolving phase-0 quarantine; never applies or authorizes.

Canonical post mappings do not establish the originating account or a receipt.
Unmapped rows remain globally blocking. Existing quarantine/history are retained.
"""
from .connectsafely_backfill import ReviewedMappings, _plan
from .connectsafely_coordination import require, digest, encoded


def plan_resolution(rows, mappings, *, expected_history_hash):
    """Build a pinned review artifact, not a runtime grant or migration.

    Caller supplies the exact immutable history rows. Account bindings require a
    separate approved source before any SQL narrowing can become operational.
    """
    require(type(mappings) is ReviewedMappings, 'social_mapping_review_invalid')
    actual = digest(encoded(rows))
    require(actual == expected_history_hash, 'social_resolution_history_changed')
    candidate = _plan(rows, mappings)
    entries = []
    for item in candidate['manifest']['entries']:
        # Even a uniquely mapped row remains a fence, never a fabricated receipt.
        # Account unknown deliberately means all accounts, rather than safe/new.
        exact = item['post_urn'] is not None and item['quarantine_scope'] != 'global'
        entries.append({'touch_id': item['touch_id'], 'family': item['family'],
                        'account_scope': 'all_accounts',
                        'scope': 'target' if exact else 'global',
                        'post_urn': item['post_urn'] if exact else None,
                        'receipt_verified': False,
                        'reason': 'canonical_target_fence' if exact else item['reason']})
    result = {'version': 1, 'read_only': True, 'runtime_authorized': False,
              'history_sha256': actual, 'mapping_sha256': mappings.sha256,
              'retained_quarantine_count': len(rows), 'entries': entries,
              'global_blockers': sum(e['scope'] == 'global' for e in entries),
              'required_migration': 'append_only_resolution_and_all_database_guards',
              'account_narrowing_authorized': False}
    return {**result, 'proposal_sha256': digest(encoded(result))}

import json
from contextlib import closing
import sqlite3
from pathlib import Path
from .connectsafely_coordination import (SQLiteSocialCoordinator, definitions, SQL_PATH,
                                       FAMILY, HEX, POST)
RESOLUTION_SQL = Path(__file__).with_suffix('.sqlite.sql')


def _history(con):
    return [json.loads(r[0]) for r in con.execute('SELECT row_json FROM thunder_social_history ORDER BY touch_id')]


def _expected_schema():
    with closing(sqlite3.connect(':memory:')) as con:
        con.execute('CREATE TABLE toques(id INTEGER PRIMARY KEY,canal TEXT,acao TEXT,pessoa_chave TEXT,alvo TEXT,status TEXT,agente TEXT,provider_id TEXT)')
        con.executescript(SQL_PATH.read_text()+RESOLUTION_SQL.read_text())
        return definitions(con)


def verify_resolution(con):
    """Revalidate pinned evidence and exact append-only materialization."""
    require(definitions(con)==_expected_schema(),'social_resolution_schema_changed')
    rows=_history(con);seen=set()
    for pin,raw,path,mapping_pin in con.execute('SELECT * FROM thunder_social_resolution_evidence ORDER BY proposal_sha256'):
        proposal=json.loads(raw)
        expected=plan_resolution(rows,ReviewedMappings(path,mapping_pin),expected_history_hash=proposal['history_sha256'])
        require(expected==proposal and expected['proposal_sha256']==pin,'social_resolution_evidence_changed')
        expected_rows={(e['touch_id'],e['family'],e['post_urn'],pin) for e in proposal['entries'] if e['scope']=='target'}
        actual={tuple(r) for r in con.execute('SELECT * FROM thunder_social_resolution WHERE proposal_sha256=?',(pin,))}
        require(actual.issubset(expected_rows),'social_resolution_materialization_changed')
        for touch,family,post,_ in expected_rows:
            row=con.execute('SELECT family,post_urn FROM thunder_social_resolution WHERE touch_id=?',(touch,)).fetchone()
            require(row and tuple(row)==(family,post),'social_resolution_materialization_changed')
        require(not seen.intersection(r[0] for r in actual),'social_resolution_conflict');seen.update(r[0] for r in actual)
    require(con.execute('SELECT count(*) FROM thunder_social_resolution').fetchone()[0]==len(seen),'social_resolution_unverified')
    return con.execute("SELECT count(*) FROM thunder_social_effective_quarantine WHERE scope='global'").fetchone()[0]


def install_resolution_sqlite(con,mappings,*,history_manifest_hash,reviewed_proposal_sha256):
    """Explicit isolated migration; never startup, GET, or production activation."""
    from .connectsafely_backfill import verify_backfill
    from .connectsafely_coordination import expected_definitions
    require(type(con) is sqlite3.Connection and not con.in_transaction,'social_resolution_transaction_invalid')
    try:
        con.execute('BEGIN IMMEDIATE')
        verify_backfill(con,history_manifest_hash,allow_quarantine=True)
        rows=_history(con)
        proposal=plan_resolution(rows,mappings,expected_history_hash=digest(encoded(rows)))
        require(proposal['proposal_sha256']==reviewed_proposal_sha256,'social_resolution_review_mismatch')
        installed='thunder_social_resolution' in definitions(con)
        if installed:
            verify_resolution(con)
            prior=con.execute('SELECT proposal_json FROM thunder_social_resolution_evidence WHERE proposal_sha256=?',(reviewed_proposal_sha256,)).fetchone()
            if prior:
                require(json.loads(prior[0])==proposal,'social_resolution_evidence_changed');con.commit();return False
        else:
            require(definitions(con)==expected_definitions(),'social_schema_changed')
            # executescript commits implicitly: execute complete SQL statements
            # individually to keep DDL and evidence under this transaction.
            statement=''
            for line in RESOLUTION_SQL.read_text().splitlines(True):
                statement+=line
                if sqlite3.complete_statement(statement):con.execute(statement);statement=''
            require(not statement.strip(),'social_resolution_sql_invalid')
        con.execute('INSERT INTO thunder_social_resolution_evidence VALUES(?,?,?,?)',(reviewed_proposal_sha256,encoded(proposal).decode(),mappings.path,mappings.sha256))
        for entry in proposal['entries']:
            if entry['scope']=='target':
                prior=con.execute('SELECT family,post_urn FROM thunder_social_resolution WHERE touch_id=?',(entry['touch_id'],)).fetchone()
                if prior:
                    require(tuple(prior)==(entry['family'],entry['post_urn']),'social_resolution_conflict')
                else:
                    con.execute('INSERT INTO thunder_social_resolution VALUES(?,?,?,?)',(entry['touch_id'],entry['family'],entry['post_urn'],reviewed_proposal_sha256))
        verify_resolution(con);con.commit();return True
    except Exception:
        con.rollback();raise


class SQLiteResolvedSocialCoordinator(SQLiteSocialCoordinator):
    """Offline execution proof using the reviewed resolution schema.

    Not accepted as PostgreSQL/live readiness by social console composition.
    Unproven account identities continue fencing all accounts.
    """
    def _verify(self,con):
        from .connectsafely_backfill import verify_backfill
        inventory_hash=self.inventory.verify()
        verify_backfill(con,self.backfill_manifest_hash,allow_quarantine=True)
        unresolved=verify_resolution(con)
        require(unresolved==0,'social_quarantine_unresolved')
        actual={tuple(r) for r in con.execute('SELECT writer_id,artifact_sha256,audit_sha256,inventory_hash FROM thunder_social_writers')}
        expected={(w.writer_id,w.artifact_sha256,w.audit_sha256,inventory_hash) for w in self.inventory.writers}
        require(actual==expected,'social_writer_inventory_changed')
        unknown=con.execute("SELECT 1 FROM toques WHERE canal='linkedin' AND acao IN ('curtida','comentario') AND id NOT IN (SELECT touch_id FROM thunder_social_history) AND (agente IS NULL OR agente NOT IN (SELECT writer_id FROM thunder_social_writers)) LIMIT 1").fetchone()
        require(not unknown,'social_writer_unknown')
        return inventory_hash

    def _scope(self,con,effect,account,action,post):
        import re
        require(isinstance(effect,str) and HEX.fullmatch(effect) and isinstance(account,str) and re.fullmatch(r'[a-f0-9]{24}',account),'social_capability_scope_invalid')
        require(action in FAMILY and isinstance(post,str) and POST.fullmatch(post),'social_capability_scope_invalid')
        require(not con.execute("SELECT 1 FROM thunder_social_effective_quarantine WHERE scope='global' OR (family=? AND post_urn=?) LIMIT 1",(FAMILY[action],post)).fetchone(),'social_quarantine_unresolved')
        row=con.execute("SELECT t.id,t.status,t.provider_id FROM thunder_effect_touches e JOIN toques t ON t.id=e.touch_id JOIN thunder_social_claims c ON c.touch_id=t.id WHERE t.id NOT IN (SELECT touch_id FROM thunder_social_history) AND e.effect_key=? AND c.family=? AND c.post_urn=?",(effect,FAMILY[action],post)).fetchone()
        require(row and row[1]=='reservado' and row[2] is None,'social_reservation_unverified')
        return row[0]
