"""SQLite coordination proof; no automatic installation or production activation.

Inventory hashes prove which reviewed artifacts were supplied, not discovery of
all processes on a host. Deployment must supply its independently audited full
inventory. PostgreSQL uses the separate connectsafely_coordination_postgres verifier
with explicit role, catalog and function-body checks. No boolean, environment variable, or browser proof enables
this boundary. A database administrator remains outside the SQLite threat model.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import time
from contextlib import contextmanager
from functools import lru_cache


class CoordinationError(ValueError):
    pass


def require(value,code):
    if not value:raise CoordinationError(code)


def digest(value):return sha256(value).hexdigest()
def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()
SQL_PATH=Path(__file__).with_name('connectsafely_coordination.sqlite.sql')
POST=re.compile(r'urn:li:activity:[0-9]{1,25}')
IDENT=re.compile(r'[A-Za-z0-9_.:-]{1,160}')
HEX=re.compile(r'[a-f0-9]{64}')
FAMILY={'comment':'comentario','reply':'comentario','react_post':'curtida'}


@dataclass(frozen=True)
class WriterEvidence:
    writer_id:str
    artifact_path:str
    artifact_sha256:str
    audit_path:str
    audit_sha256:str
    protocol:str='reserve-before-effect-v1'
    database_role:str|None=None


@dataclass(frozen=True)
class InventoryEvidence:
    writers:tuple[WriterEvidence,...]
    expected_writer_ids:tuple[str,...]
    inventory_path:str
    inventory_sha256:str

    def verify(self):
        require(isinstance(self.writers,tuple) and self.writers and all(type(w) is WriterEvidence for w in self.writers),'social_inventory_incomplete')
        require(isinstance(self.expected_writer_ids,tuple) and all(isinstance(i,str) for i in self.expected_writer_ids) and len(self.expected_writer_ids)==len(set(self.expected_writer_ids)),'social_inventory_incomplete')
        ids=[w.writer_id for w in self.writers]
        require(len(ids)==len(set(ids)) and set(ids)==set(self.expected_writer_ids),'social_inventory_incomplete')
        require(all(isinstance(i,str) and IDENT.fullmatch(i) for i in ids),'social_inventory_invalid')
        # The inventory itself must enumerate the exact reviewed writer artifacts.
        expected={'version':1,'writers':[{'id':w.writer_id,'artifact_sha256':w.artifact_sha256,'audit_sha256':w.audit_sha256,'protocol':w.protocol,**({'database_role':w.database_role} if w.database_role is not None else {})} for w in self.writers]}
        try:
            path=Path(self.inventory_path)
            require(path.is_file() and not path.is_symlink(),'social_inventory_invalid')
            require(path.stat().st_size<=100000,'social_inventory_invalid')
            raw=path.read_bytes()
            require(len(raw)<=100000 and HEX.fullmatch(self.inventory_sha256) and digest(raw)==self.inventory_sha256,'social_inventory_changed')
            require(json.loads(raw)==expected,'social_inventory_incomplete')
            for writer in self.writers:
                require(writer.protocol=='reserve-before-effect-v1','social_writer_protocol_unverified')
                for filename,pin in [(writer.artifact_path,writer.artifact_sha256),(writer.audit_path,writer.audit_sha256)]:
                    artifact=Path(filename)
                    require(HEX.fullmatch(pin) and artifact.is_file() and not artifact.is_symlink(),'social_writer_artifact_invalid')
                    require(artifact.stat().st_size<=10000000 and digest(artifact.read_bytes())==pin,'social_writer_artifact_changed')
        except CoordinationError:raise
        except Exception:raise CoordinationError('social_inventory_unavailable') from None
        return digest(encoded(expected))


def definitions(con):
    return {r[0]:r[1] for r in con.execute("SELECT name,sql FROM sqlite_master WHERE name LIKE 'thunder_social_%' AND sql IS NOT NULL")}


@lru_cache(maxsize=1)
def expected_definitions():
    with sqlite3.connect(':memory:') as con:
        con.execute('CREATE TABLE toques(id INTEGER PRIMARY KEY,canal TEXT,acao TEXT,pessoa_chave TEXT,alvo TEXT,status TEXT,agente TEXT,provider_id TEXT)')
        con.executescript(SQL_PATH.read_text())
        return definitions(con)


def install_sqlite(connection,inventory):
    """Explicit migration for an isolated DB. Existing social data blocks install.

    Backfill/alias reconciliation needs a separate audited migration. Never run
    implicitly on application startup or provider read.
    """
    require(type(connection) is sqlite3.Connection,'social_backend_unsupported')
    inventory_hash=inventory.verify()
    require(not connection.in_transaction,'social_transaction_already_open')
    require(connection.execute("SELECT count(*) FROM toques WHERE canal='linkedin' AND acao IN ('curtida','comentario')").fetchone()[0]==0,'social_backfill_review_required')
    try:
        connection.executescript('BEGIN IMMEDIATE;\n'+SQL_PATH.read_text())
        require(connection.execute("SELECT count(*) FROM toques WHERE canal='linkedin' AND acao IN ('curtida','comentario')").fetchone()[0]==0,'social_backfill_review_required')
        connection.executemany('INSERT INTO thunder_social_writers VALUES(?,?,?,?)',[(w.writer_id,w.artifact_sha256,w.audit_sha256,inventory_hash) for w in inventory.writers])
        connection.commit()
    except Exception:
        connection.rollback();raise CoordinationError('social_installation_failed') from None


class SQLiteSocialCoordinator:
    def __init__(self,connect,inventory,*,clock=time.time,ttl_seconds=30,backfill_manifest_hash=None):
        require(callable(connect) and isinstance(inventory,InventoryEvidence),'social_coordination_unverified')
        require(type(ttl_seconds) is int and 1<=ttl_seconds<=30,'social_capability_ttl_invalid')
        self.connect,self.inventory,self.clock,self.ttl=connect,inventory,clock,ttl_seconds
        self.backfill_manifest_hash=backfill_manifest_hash

    @contextmanager
    def _transaction(self):
        con=None
        try:
            con=self.connect()
            require(type(con) is sqlite3.Connection,'social_backend_unsupported')
            con.execute('PRAGMA foreign_keys=ON')
            con.execute('BEGIN IMMEDIATE')
            yield con
            con.commit()
        except CoordinationError:
            if con:con.rollback()
            raise
        except Exception:
            if con:con.rollback()
            raise CoordinationError('social_coordination_unverified') from None
        finally:
            if con:con.close()

    def _verify(self,con):
        inventory_hash=self.inventory.verify()
        require(definitions(con)==expected_definitions(),'social_schema_changed')
        actual={tuple(r) for r in con.execute('SELECT writer_id,artifact_sha256,audit_sha256,inventory_hash FROM thunder_social_writers')}
        expected={(w.writer_id,w.artifact_sha256,w.audit_sha256,inventory_hash) for w in self.inventory.writers}
        require(actual==expected,'social_writer_inventory_changed')
        from .connectsafely_backfill import verify_backfill
        verify_backfill(con,self.backfill_manifest_hash)
        unknown=con.execute("SELECT 1 FROM toques WHERE canal='linkedin' AND acao IN ('curtida','comentario') AND id NOT IN (SELECT touch_id FROM thunder_social_history) AND (agente IS NULL OR agente NOT IN (SELECT writer_id FROM thunder_social_writers)) LIMIT 1").fetchone()
        require(not unknown,'social_writer_unknown')
        return inventory_hash

    def readiness(self):
        try:
            with self._transaction() as con:self._verify(con)
            return {'verified':True,'backend':'sqlite','live_inventory_proven':False}
        except CoordinationError as exc:return {'verified':False,'error_code':str(exc),'live_inventory_proven':False}

    def _scope(self,con,effect,account,action,post):
        require(isinstance(effect,str) and HEX.fullmatch(effect) and isinstance(account,str) and re.fullmatch(r'[a-f0-9]{24}',account),'social_capability_scope_invalid')
        require(action in FAMILY and isinstance(post,str) and POST.fullmatch(post),'social_capability_scope_invalid')
        require(not con.execute("SELECT 1 FROM thunder_social_quarantine WHERE scope='global' OR (family=? AND post_urn=?) LIMIT 1",(FAMILY[action],post)).fetchone(),'social_quarantine_unresolved')
        row=con.execute("SELECT t.id,t.status,t.provider_id FROM thunder_effect_touches e JOIN toques t ON t.id=e.touch_id JOIN thunder_social_claims c ON c.touch_id=t.id WHERE t.id NOT IN (SELECT touch_id FROM thunder_social_history) AND e.effect_key=? AND c.family=? AND c.post_urn=?",(effect,FAMILY[action],post)).fetchone()
        require(row and row[1]=='reservado' and row[2] is None,'social_reservation_unverified')
        return row[0]

    def issue(self,effect_key,*,account_id,action,post_urn,attempt_token,request_hash):
        require(isinstance(request_hash,str) and HEX.fullmatch(request_hash),'social_request_invalid')
        require(isinstance(attempt_token,str) and IDENT.fullmatch(attempt_token),'social_attempt_invalid')
        with self._transaction() as con:
            inventory_hash=self._verify(con);touch=self._scope(con,effect_key,account_id,action,post_urn)
            # A token is never reusable after consumption, retirement or expiry.
            require(not con.execute('SELECT 1 FROM thunder_social_attempts WHERE effect_key=? AND (attempt_token=? OR state IN (\'issued\',\'consumed\'))',(effect_key,attempt_token)).fetchone(),'social_attempt_fenced')
            expiry=self.clock()+self.ttl
            con.execute('INSERT INTO thunder_social_attempts VALUES(?,?,?,?,?,?,?,?,?)',(effect_key,attempt_token,touch,account_id,action,post_urn,request_hash,'issued',expiry))
            con.execute('INSERT INTO thunder_social_audit(effect_key,attempt_token,event,at,inventory_hash) VALUES(?,?,?,?,?)',(effect_key,attempt_token,'issued',self.clock(),inventory_hash))
        return SocialCapability(self,effect_key,attempt_token,account_id,action,post_urn,request_hash,expiry)

    def _consume(self,cap,*,action,account_id,post_urn,request_hash):
        require((action,account_id,post_urn,request_hash)==(cap.action,cap.account_id,cap.post_urn,cap.request_hash),'social_capability_scope_mismatch')
        with self._transaction() as con:
            inventory_hash=self._verify(con);touch=self._scope(con,cap.effect_key,account_id,action,post_urn)
            row=con.execute('SELECT touch_id,account_id,action,post_urn,request_hash,state,expires_at FROM thunder_social_attempts WHERE effect_key=? AND attempt_token=?',(cap.effect_key,cap.attempt_token)).fetchone()
            require(row and tuple(row[:5])==(touch,account_id,action,post_urn,request_hash) and row[5]=='issued' and row[6]>self.clock() and row[6]==cap.expires_at,'social_attempt_fenced')
            con.execute("UPDATE thunder_social_attempts SET state='consumed' WHERE effect_key=? AND attempt_token=? AND state='issued'",(cap.effect_key,cap.attempt_token))
            con.execute('INSERT INTO thunder_social_audit(effect_key,attempt_token,event,at,inventory_hash) VALUES(?,?,?,?,?)',(cap.effect_key,cap.attempt_token,'consumed',self.clock(),inventory_hash))
        return True

    def retire_unconsumed(self,effect_key,attempt_token):
        with self._transaction() as con:
            inventory_hash=self._verify(con)
            prior=con.execute('SELECT state FROM thunder_social_attempts WHERE effect_key=? AND attempt_token=?',(effect_key,attempt_token)).fetchone()
            if prior and prior[0]=='retired':return True
            changed=con.execute("UPDATE thunder_social_attempts SET state='retired' WHERE effect_key=? AND attempt_token=? AND state='issued'",(effect_key,attempt_token)).rowcount
            require(changed==1,'social_attempt_fenced')
            con.execute('INSERT INTO thunder_social_audit(effect_key,attempt_token,event,at,inventory_hash) VALUES(?,?,?,?,?)',(effect_key,attempt_token,'retired_not_dispatched',self.clock(),inventory_hash))
        return True


@dataclass(frozen=True)
class SocialCapability:
    _coordinator:SQLiteSocialCoordinator
    effect_key:str
    attempt_token:str
    account_id:str
    action:str
    post_urn:str
    request_hash:str
    expires_at:float

    def require(self,*,action,account_id,post_urn,request_hash):
        return self._coordinator._consume(self,action=action,account_id=account_id,post_urn=post_urn,request_hash=request_hash)

    def validate(self,*,action,account_id,post_urn,request_hash):
        require((action,account_id,post_urn,request_hash)==(self.action,self.account_id,self.post_urn,self.request_hash),'social_capability_scope_mismatch')
        with self._coordinator._transaction() as con:
            self._coordinator._verify(con)
            touch=self._coordinator._scope(con,self.effect_key,self.account_id,self.action,self.post_urn)
            row=con.execute('SELECT touch_id,account_id,action,post_urn,request_hash,state,expires_at FROM thunder_social_attempts WHERE effect_key=? AND attempt_token=?',(self.effect_key,self.attempt_token)).fetchone()
            require(row and tuple(row[:5])==(touch,self.account_id,self.action,self.post_urn,self.request_hash) and row[5]=='issued' and row[6]>self._coordinator.clock() and row[6]==self.expires_at,'social_attempt_fenced')
        return True
