"""Additive SQLite repository for workflow state and durable outbox."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
from threading import local
from typing import Any, Iterator
from uuid import uuid4

from .models import WorkItem, as_payload, parse_work_item, payload_hash


class RepositoryConflict(RuntimeError):
    pass


class Repository:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._local = local()
        self.migrate()

    def _connect(self) -> sqlite3.Connection:
        active = getattr(self._local, "connection", None)
        if active is not None:
            return _BorrowedConnection(active)
        con = sqlite3.connect(self.path, timeout=5, isolation_level=None, factory=_OwnedConnection)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def migrate(self) -> None:
        with self._connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS thunder_schema (
                    version INTEGER NOT NULL
                );
                INSERT INTO thunder_schema(version)
                    SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM thunder_schema);
                CREATE TABLE IF NOT EXISTS thunder_work_items (
                    id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    version INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS thunder_events (
                    id TEXT PRIMARY KEY,
                    entity_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS thunder_outbox (
                    id TEXT PRIMARY KEY,
                    entity_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    lease_owner TEXT,
                    lease_until TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS thunder_state (
                    kind TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(kind, entity_id)
                );
                CREATE TABLE IF NOT EXISTS thunder_effect_jobs (
                    id TEXT PRIMARY KEY,
                    effect_key TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    lease_owner TEXT,
                    lease_until TEXT,
                    receipt_ref TEXT,
                    error_code TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        active = getattr(self._local, "connection", None)
        if active is not None:
            yield active
            return
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            self._local.connection = con
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            self._local.connection = None
            con.close()

    def save_with_event(
        self,
        item: WorkItem,
        expected_version: int,
        event_type: str,
        outbox_kind: str | None = None,
    ) -> WorkItem:
        now = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(as_payload(item), ensure_ascii=False, sort_keys=True)
        event_payload = json.dumps({
            "id": item.id, "status": item.status.value, "version": item.version,
            "actor": item.actor, "next_action": item.next_action,
            "result_present": item.result is not None,
            "result_hash": payload_hash(item.result) if item.result is not None else None,
        }, ensure_ascii=False, sort_keys=True)
        with self.transaction() as con:
            row = con.execute("SELECT version FROM thunder_work_items WHERE id=?", (item.id,)).fetchone()
            current = row["version"] if row else 0
            if current != expected_version or item.version != expected_version + 1:
                raise RepositoryConflict("stale writer")
            if row:
                changed = con.execute(
                    "UPDATE thunder_work_items SET version=?, payload=?, updated_at=? WHERE id=? AND version=?",
                    (item.version, payload, now, item.id, expected_version),
                ).rowcount
                if changed != 1:
                    raise RepositoryConflict("concurrent writer")
            else:
                try:
                    con.execute(
                        "INSERT INTO thunder_work_items VALUES (?, ?, ?, ?, ?)",
                        (item.id, item.idempotency_key, item.version, payload, now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise RepositoryConflict("duplicate work or idempotency key") from exc
            event_id = uuid4().hex
            con.execute(
                "INSERT INTO thunder_events VALUES (?, ?, ?, ?, ?)",
                (event_id, item.id, event_type, event_payload, now),
            )
            if outbox_kind:
                con.execute(
                    "INSERT INTO thunder_outbox(id, entity_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                    (uuid4().hex, item.id, outbox_kind, payload, now),
                )
        return item

    def get_work(self, item_id: str) -> WorkItem | None:
        with self._connect() as con:
            row = con.execute("SELECT payload FROM thunder_work_items WHERE id=?", (item_id,)).fetchone()
        return parse_work_item(json.loads(row["payload"])) if row else None

    def list_work(self, limit: int = 100) -> list[WorkItem]:
        """Read newest requests without migrating or mutating workflow state."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("work limit must be between 1 and 1000")
        with self._connect() as con:
            rows = con.execute(
                "SELECT payload FROM thunder_work_items ORDER BY updated_at DESC,rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [parse_work_item(json.loads(row["payload"])) for row in rows]

    def get_by_idempotency_key(self, key: str) -> WorkItem | None:
        with self._connect() as con:
            row = con.execute("SELECT payload FROM thunder_work_items WHERE idempotency_key=?", (key,)).fetchone()
        return parse_work_item(json.loads(row["payload"])) if row else None

    def events(self, entity_id: str) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT id, type, payload, created_at FROM thunder_events WHERE entity_id=? ORDER BY rowid",
                (entity_id,),
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def outbox(self) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM thunder_outbox ORDER BY rowid").fetchall()
        return [dict(row) for row in rows]

    def claim_outbox(self, worker_id: str, now: datetime, lease_for: timedelta, *, kind: str | None = None) -> dict[str, Any] | None:
        now = now.astimezone(timezone.utc)
        with self.transaction() as con:
            row = con.execute(
                """SELECT * FROM thunder_outbox
                   WHERE state='pending' AND (lease_until IS NULL OR lease_until <= ?) AND (? IS NULL OR kind=?)
                   ORDER BY rowid LIMIT 1""",
                (now.isoformat(), kind, kind),
            ).fetchone()
            if not row:
                return None
            lease_until = (now + lease_for).isoformat()
            claim_owner = f"{worker_id}:{uuid4().hex}"
            changed = con.execute(
                """UPDATE thunder_outbox SET lease_owner=?, lease_until=?
                   WHERE id=? AND (lease_until IS NULL OR lease_until <= ?)""",
                (claim_owner, lease_until, row["id"], now.isoformat()),
            ).rowcount
            if changed != 1:
                return None
            result = dict(row)
            result.update(lease_owner=claim_owner, lease_until=lease_until)
            return result

    def put_state(self, kind: str, entity_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self.transaction() as con:
            row = con.execute(
                "SELECT version FROM thunder_state WHERE kind=? AND entity_id=?", (kind, entity_id)
            ).fetchone()
            version = (row["version"] if row else 0) + 1
            con.execute(
                """INSERT INTO thunder_state(kind,entity_id,version,payload,updated_at)
                   VALUES (?,?,?,?,?) ON CONFLICT(kind,entity_id) DO UPDATE SET
                   version=excluded.version,payload=excluded.payload,updated_at=excluded.updated_at""",
                (kind, entity_id, version, encoded, now),
            )
        return {**payload, "id": entity_id, "version": version, "updated_at": now}

    @staticmethod
    def _put_state_in(con, kind, entity_id, payload, now, expected_version=None):
        row = con.execute("SELECT version FROM thunder_state WHERE kind=? AND entity_id=?", (kind, entity_id)).fetchone()
        version = row["version"] if row else 0
        if expected_version is not None and version != expected_version:
            raise RepositoryConflict("stale state version")
        version += 1
        con.execute("""INSERT INTO thunder_state VALUES (?,?,?,?,?) ON CONFLICT(kind,entity_id)
                    DO UPDATE SET version=excluded.version,payload=excluded.payload,updated_at=excluded.updated_at""",
                    (kind, entity_id, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), now))
        return {**payload, "id": entity_id, "version": version, "updated_at": now}

    def put_states_atomic(self, updates):
        """Compare versions and save all states, or roll back the entire transition."""
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as con:
            return [self._put_state_in(con, kind, key, payload, now, expected)
                    for kind, key, payload, expected in updates]

    def append_state(self, kind: str, entity_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Append an immutable state record; IDs are caller-stable and never overwritten."""
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as con:
            try:
                con.execute("INSERT INTO thunder_state VALUES (?,?,?,?,?)",
                            (kind, entity_id, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), now))
            except sqlite3.IntegrityError as exc:
                raise RepositoryConflict("immutable state already exists") from exc
        return {**payload, "id": entity_id, "version": 1, "updated_at": now}

    def get_state(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT version,payload,updated_at FROM thunder_state WHERE kind=? AND entity_id=?",
                (kind, entity_id),
            ).fetchone()
        if not row:
            return None
        return {**json.loads(row["payload"]), "id": entity_id, "version": row["version"], "updated_at": row["updated_at"]}

    def list_state(self, kind: str) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT entity_id,version,payload,updated_at FROM thunder_state WHERE kind=? ORDER BY rowid",
                (kind,),
            ).fetchall()
        return [
            {**json.loads(row["payload"]), "id": row["entity_id"], "version": row["version"], "updated_at": row["updated_at"]}
            for row in rows
        ]

    def reserve_delegated_action(self, actor: str, action: str, day: str) -> int:
        """Atomically enforce the persisted kill switch, scope and daily ceiling."""
        key = f"{actor}:{day}"
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as con:
            row = con.execute(
                "SELECT payload FROM thunder_state WHERE kind='delegation' AND entity_id=?", (actor,)
            ).fetchone()
            delegation = json.loads(row["payload"]) if row else None
            if (not delegation or not delegation.get("enabled")
                    or action not in delegation.get("scopes", [])):
                raise RepositoryConflict("delegation is disabled or out of scope")
            usage_row = con.execute(
                "SELECT version,payload FROM thunder_state WHERE kind='delegation_usage' AND entity_id=?", (key,)
            ).fetchone()
            usage = json.loads(usage_row["payload"]) if usage_row else {"count": 0}
            count = int(usage.get("count", 0))
            if count >= int(delegation["daily_limit"]):
                raise RepositoryConflict("daily delegation limit reached")
            payload = json.dumps({"count": count + 1, "daily_limit_date": day}, sort_keys=True)
            version = (usage_row["version"] if usage_row else 0) + 1
            con.execute(
                """INSERT INTO thunder_state(kind,entity_id,version,payload,updated_at)
                   VALUES ('delegation_usage',?,?,?,?) ON CONFLICT(kind,entity_id) DO UPDATE SET
                   version=excluded.version,payload=excluded.payload,updated_at=excluded.updated_at""",
                (key, version, payload, now),
            )
        return count + 1

    def enqueue_effect(self, effect_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        job_id = f"effect-{uuid4().hex}"
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self.transaction() as con:
            existing = con.execute(
                "SELECT * FROM thunder_effect_jobs WHERE effect_key=?", (effect_key,)
            ).fetchone()
            if existing:
                if existing["payload"] != encoded:
                    raise RepositoryConflict("effect key payload mismatch")
                return dict(existing)
            con.execute(
                "INSERT INTO thunder_effect_jobs(id,effect_key,payload,updated_at) VALUES (?,?,?,?)",
                (job_id, effect_key, encoded, now),
            )
        return {"id": job_id, "effect_key": effect_key, "payload": payload, "state": "pending"}

    def claim_effect(self, worker_id: str, now: datetime, lease_for: timedelta) -> dict[str, Any] | None:
        now = now.astimezone(timezone.utc)
        with self.transaction() as con:
            row = con.execute(
                """SELECT * FROM thunder_effect_jobs WHERE state='pending'
                   AND (lease_until IS NULL OR lease_until<=?) ORDER BY rowid LIMIT 1""",
                (now.isoformat(),),
            ).fetchone()
            if not row:
                return None
            lease_until = (now + lease_for).isoformat()
            claim_owner = f"{worker_id}:{uuid4().hex}"
            changed = con.execute(
                """UPDATE thunder_effect_jobs SET lease_owner=?,lease_until=?,updated_at=?
                   WHERE id=? AND state='pending' AND (lease_until IS NULL OR lease_until<=?)""",
                (claim_owner, lease_until, now.isoformat(), row["id"], now.isoformat()),
            ).rowcount
            if changed != 1:
                return None
            result = dict(row)
            result.update(payload=json.loads(row["payload"]), lease_owner=claim_owner,
                          lease_until=lease_until, updated_at=now.isoformat())
            return result

    @property
    def dispatch_fence_active(self) -> bool:
        return getattr(self._local, "dispatch_fence", None) is not None

    @contextmanager
    def effect_dispatch_fence(self, job_id, lease_owner):
        """Linearize config/approval changes and one dispatch in this database.

        BEGIN IMMEDIATE prevents any other writer/claimant until receipt or
        uncertainty is durably recorded. A revocation waiting behind a dispatch
        takes effect afterwards; it cannot undo an already-started remote call.
        """
        if not isinstance(lease_owner,str) or not lease_owner.strip():
            raise RepositoryConflict("dispatch owner required")
        if self.dispatch_fence_active:
            raise RepositoryConflict("nested dispatch fence")
        with self.transaction() as con:
            row = con.execute(
                "SELECT state,lease_owner,lease_until FROM thunder_effect_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            now = datetime.now(timezone.utc).isoformat()
            if (not row or row["state"] != "pending" or row["lease_owner"] != lease_owner
                    or not row["lease_until"] or row["lease_until"] <= now):
                raise RepositoryConflict("dispatch lease lost")
            self._local.dispatch_fence = (job_id, lease_owner)
            try:
                yield
            finally:
                self._local.dispatch_fence = None

    def finish_effect(self, job_id: str, state: str, receipt_ref: str | None, error_code: str | None, *, lease_owner: str | None = None) -> None:
        if state not in {"sent", "failed", "uncertain", "needs_reconciliation"}:
            raise ValueError("invalid effect terminal state")
        fenced = getattr(self._local, "dispatch_fence", None) == (job_id, lease_owner)
        with self.transaction() as con:
            changed = con.execute(
                """UPDATE thunder_effect_jobs SET state=?,receipt_ref=?,error_code=?,lease_owner=NULL,
                   lease_until=NULL,updated_at=? WHERE id=? AND state='pending' AND (lease_owner=? OR (lease_owner IS NULL AND ? IS NULL)) AND (lease_owner IS NULL OR lease_until>? OR ?)""",
                (state, receipt_ref, error_code, datetime.now(timezone.utc).isoformat(), job_id, lease_owner, lease_owner, datetime.now(timezone.utc).isoformat(), fenced),
            ).rowcount
            if changed != 1:
                raise RepositoryConflict("effect is not pending")

    def effect(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM thunder_effect_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        if result["state"] == "sent":
            result["sent_at"] = result["updated_at"]
        return result

    def effects(self) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM thunder_effect_jobs ORDER BY rowid").fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"]), **({"sent_at": row["updated_at"]} if row["state"] == "sent" else {})} for row in rows]

    def reconciliation_effects(self, *, after_rowid: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        """Page registered effects, including consumed jobs, without loading all history."""
        if type(after_rowid) is not int or after_rowid < 0:
            raise ValueError("invalid effect cursor")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("effect limit must be between 1 and 100")
        with self._connect() as con:
            rows = con.execute(
                "SELECT rowid AS reconciliation_cursor,* FROM thunder_effect_jobs "
                "WHERE rowid>? ORDER BY rowid LIMIT ?", (after_rowid, limit),
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def mark_effect_reconciliation(self, snapshot: dict[str, Any], evidence: dict[str, Any]) -> bool:
        """CAS one observed effect and append its incident atomically, retaining receipts."""
        touches = evidence.get("touches", [])
        if (len({touch.get("touch_id") for touch in touches}) < 2
                or any(touch.get("status") != "sent" or not touch.get("touch_id") for touch in touches)):
            raise ValueError("two confirmed touches required")
        alert_id = "duplicate-effect:" + snapshot["id"]
        code = "duplicate_consumed_effect"
        with self.transaction() as con:
            current = self.effect(snapshot["id"])
            fields = ("id", "effect_key", "payload", "state", "updated_at", "lease_owner",
                      "lease_until", "receipt_ref", "error_code")
            if not current or any(current[field] != snapshot[field] for field in fields):
                raise RepositoryConflict("effect changed during reconciliation")
            alert = self.get_state("operator_alert", alert_id)
            if current["state"] == "needs_reconciliation" and current["error_code"] == code and alert:
                return False
            changed = con.execute(
                "UPDATE thunder_effect_jobs SET state='needs_reconciliation',error_code=?,"
                "lease_owner=NULL,lease_until=NULL,updated_at=? "
                "WHERE id=? AND effect_key=? AND state=? AND updated_at=? "
                "AND lease_owner IS ? AND lease_until IS ?",
                (code, datetime.now(timezone.utc).isoformat(), current["id"], current["effect_key"],
                 current["state"], current["updated_at"], current["lease_owner"], current["lease_until"]),
            ).rowcount
            if changed != 1:
                raise RepositoryConflict("effect changed during reconciliation")
            if not alert:
                self.append_state("operator_alert", alert_id, {
                    "active": True, "priority": "high", "owner": "ygor", "code": code,
                    "reason": code, "effect_id": current["id"], "effect_key": current["effect_key"],
                    "cadence_id": current["payload"].get("cadence_id"),
                    "step_id": current["payload"].get("step", {}).get("id"),
                    "title": "Reconciliar execucoes duplicadas",
                    "message": "Dois ou mais toques confirmados no mesmo escopo. Historico preservado.",
                    "next_action": "Ygor: conferir os recibos e reconciliar o incidente sem novo envio.",
                    "duplicate_evidence": evidence,
                })
        return True

    def validate_uncertain_receipt(self, snapshot, receipt):
        """Preflight under the caller's write transaction before a ledger commit."""
        if getattr(self._local,'connection',None) is None:
            raise RepositoryConflict('reconciliation transaction required')
        current = self.effect(snapshot['id'])
        if current != snapshot or current['state'] not in {'uncertain','needs_reconciliation','sent'}:
            raise RepositoryConflict('reconciliation snapshot changed')
        if (current.get('error_code') == 'duplicate_consumed_effect'
                or current.get('lease_owner') or current.get('receipt_ref') not in (None, receipt)):
            raise RepositoryConflict('reconciliation receipt conflict')
        if self._local.connection.execute('SELECT id FROM thunder_effect_jobs WHERE receipt_ref IN (?,?) AND id<>?',
                (receipt,'email:'+receipt,current['id'])).fetchone():
            raise RepositoryConflict('receipt already bound')
        return current

    def confirm_uncertain_receipt(self, snapshot, receipt):
        """CAS historical confirmation. Never queue work or renew approval."""
        with self.transaction() as con:
            current = self.validate_uncertain_receipt(snapshot,receipt)
            con.execute("UPDATE thunder_effect_jobs SET state='sent',receipt_ref=?,error_code=NULL,updated_at=? WHERE id=?",
                        (receipt,datetime.now(timezone.utc).isoformat(),current['id']))
            return self.effect(current['id'])

    def requeue_confirmed_effect(self, snapshot: dict[str, Any], confirmation_hash: str) -> dict[str, Any]:
        """CAS a safely failed effect after the caller persists fresh campaign authority."""
        if not isinstance(confirmation_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", confirmation_hash):
            raise ValueError("invalid execution confirmation hash")
        with self.transaction() as con:
            current = self.effect(snapshot["id"])
            fields = ("id", "effect_key", "payload", "state", "updated_at", "lease_owner",
                      "lease_until", "receipt_ref", "error_code")
            if not current or any(current[field] != snapshot[field] for field in fields):
                raise RepositoryConflict("effect changed during confirmed retry")
            if current["state"] != "failed" or current["error_code"] not in {
                "provider_rejected", "email_rejected", "hubspot_rejected", "channel_not_configured"
            }:
                raise RepositoryConflict("effect is not safely retryable")
            if confirmation_hash == current["payload"].get("execution_confirmation_hash"):
                raise RepositoryConflict("fresh campaign confirmation required")
            payload = {**current["payload"], "execution_confirmation_hash": confirmation_hash}
            changed = con.execute(
                "UPDATE thunder_effect_jobs SET payload=?,state='pending',lease_owner=NULL,"
                "lease_until=NULL,error_code=NULL,receipt_ref=NULL,updated_at=? "
                "WHERE id=? AND effect_key=? AND state='failed' AND updated_at=? "
                "AND error_code=? AND lease_owner IS ? AND lease_until IS ?",
                (json.dumps(payload, ensure_ascii=False, sort_keys=True),
                 datetime.now(timezone.utc).isoformat(), current["id"], current["effect_key"],
                 current["updated_at"], current["error_code"], current["lease_owner"], current["lease_until"]),
            ).rowcount
            if changed != 1:
                raise RepositoryConflict("effect changed during confirmed retry")
            return self.effect(current["id"])

    def retry_effect(self, job_id: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as con:
            row = con.execute("SELECT * FROM thunder_effect_jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["state"] != "failed" or row["error_code"] not in {
                "provider_rejected", "email_rejected", "hubspot_rejected", "channel_not_configured"
            }:
                raise RepositoryConflict("effect is not safely retryable")
            con.execute(
                "UPDATE thunder_effect_jobs SET state='pending',lease_owner=NULL,lease_until=NULL,error_code=NULL,updated_at=? WHERE id=?",
                (now, job_id),
            )
        return self.effect(job_id)  # type: ignore[return-value]

    def renew_lease(self, table, job_id, lease_owner, lease_for=timedelta(seconds=30)):
        if table not in {"thunder_effect_jobs", "thunder_outbox"}:
            raise ValueError("invalid queue")
        now = datetime.now(timezone.utc)
        with self.transaction() as con:
            changed = con.execute(f"UPDATE {table} SET lease_until=? WHERE id=? AND state='pending' AND lease_owner=? AND lease_until>?",
                ((now + lease_for).isoformat(), job_id, lease_owner, now.isoformat())).rowcount
            if changed != 1:
                raise RepositoryConflict("lease lost")

    def defer_effect(self, job_id, lease_owner, reason, delay=timedelta(seconds=5)):
        with self.transaction() as con:
            changed = con.execute("UPDATE thunder_effect_jobs SET lease_owner=NULL,lease_until=?,error_code=? WHERE id=? AND state='pending' AND lease_owner=?",
                ((datetime.now(timezone.utc) + delay).isoformat(), reason, job_id, lease_owner)).rowcount
            if changed != 1:
                raise RepositoryConflict("lease lost")

    def finish_outbox(self, job_id, lease_owner, *, state="done"):
        if state not in {"done", "failed"}:
            raise ValueError("invalid outbox state")
        with self.transaction() as con:
            changed = con.execute("UPDATE thunder_outbox SET state=?,lease_owner=NULL,lease_until=NULL WHERE id=? AND state='pending' AND lease_owner=? AND lease_until>?",
                                  (state, job_id, lease_owner, datetime.now(timezone.utc).isoformat())).rowcount
            if changed != 1:
                raise RepositoryConflict("outbox lease lost")

    @contextmanager
    def outbox_completion(self, job_id, lease_owner):
        """All repository writes inside this scope and the acknowledgement commit together."""
        with self.transaction() as con:
            row = con.execute("SELECT id FROM thunder_outbox WHERE id=? AND state='pending' AND lease_owner=? AND lease_until>?",
                              (job_id, lease_owner, datetime.now(timezone.utc).isoformat())).fetchone()
            if not row:
                raise RepositoryConflict("outbox lease lost")
            yield self
            self.finish_outbox(job_id, lease_owner)

    def approve_cadence_atomic(self, cadence_id, expected_version, review_hash, approval_payload, cadence_payload, *, review_hasher, research_version=None):
        with self.transaction() as con:
            current = self.get_state("cadence", cadence_id)
            if not current or current["version"] != expected_version or review_hasher(current) != review_hash:
                raise RepositoryConflict("stale cadence review")
            if research_version is not None:
                research = self.get_state("research", current["account_id"])
                if not research or research["version"] != research_version:
                    raise RepositoryConflict("stale research review")
            now = datetime.now(timezone.utc).isoformat()
            approval = self._put_state_in(con, "approval", cadence_id, approval_payload, now)
            self._put_state_in(con, "cadence", cadence_id, cadence_payload, now, expected_version)
            return approval


class _BorrowedConnection:
    """Prevent nested read contexts from committing their caller's transaction."""
    def __init__(self, connection):
        self.connection = connection
    def __getattr__(self, name):
        return getattr(self.connection, name)
    def __enter__(self):
        return self.connection
    def __exit__(self, *_args):
        return False
    def close(self):
        pass


class _OwnedConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()
