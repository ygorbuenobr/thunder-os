"""Incremental reconciliation of one observed, fixed 41-card legacy batch.

The manifest is produced from the panel itself, never from caller decisions.
Provider observations are fetched afresh on every run. No LinkedIn write exists.
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import stat

from .connection_reconcile_runtime import EXPECTED_CARDS, MAX_CARDS, audit_cards


class ReconciliationBlocked(Exception):
    pass


IMMUTABLE = ("tipo", "texto", "perfil_url", "post_url", "criado", "conta", "pessoa_chave")


def _fingerprint(card):
    fields = {key: card.get(key) for key in IMMUTABLE}
    return sha256(json.dumps(fields, ensure_ascii=True, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _rows(path):
    if not path.is_file() or path.is_symlink():
        raise ReconciliationBlocked("panel_snapshot_unavailable")
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as con:
            con.execute("PRAGMA query_only=ON")
            con.execute("BEGIN")
            rows = con.execute("SELECT id,dados FROM itens ORDER BY id").fetchall()
        return [(card_id, raw, json.loads(raw)) for card_id, raw in rows]
    except (sqlite3.Error, TypeError, ValueError, UnicodeError):
        raise ReconciliationBlocked("panel_snapshot_invalid") from None


def create_legacy_manifest(db_path, manifest_path):
    """Create exactly once from the live pending baseline, before any apply."""
    path = Path(db_path).expanduser().resolve()
    manifest = Path(manifest_path).expanduser().absolute()
    if manifest.exists() or manifest.is_symlink() or not manifest.parent.is_dir():
        raise ReconciliationBlocked("legacy_manifest_exists_or_invalid")
    selected = [(key, card) for key, _, card in _rows(path)
                if isinstance(card, dict) and card.get("status") == "pendente"
                and card.get("tipo") in EXPECTED_CARDS]
    if (len(selected) != MAX_CARDS
            or Counter(card["tipo"] for _, card in selected) != EXPECTED_CARDS):
        raise ReconciliationBlocked("panel_baseline_changed")
    data = {"schema_version": 1, "entries": [
        {"id": key, "type": card["tipo"], "fingerprint": _fingerprint(card)}
        for key, card in selected]}
    encoded = (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode()
    try:
        fd = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        raise ReconciliationBlocked("legacy_manifest_unavailable") from None
    return sha256(encoded).hexdigest()


def _manifest(path):
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_uid not in {0, os.geteuid()} or info.st_size > 20_000):
            raise ValueError
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data["entries"]
        ids = [row["id"] for row in entries]
        if (data["schema_version"] != 1 or len(entries) != MAX_CARDS
                or len(ids) != len(set(ids))
                or Counter(row["type"] for row in entries) != EXPECTED_CARDS
                or any(not isinstance(row["id"], str)
                       or not isinstance(row["fingerprint"], str)
                       or len(row["fingerprint"]) != 64 for row in entries)):
            raise ValueError
        return entries
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        raise ReconciliationBlocked("legacy_manifest_invalid") from None


def _batch(path, entries):
    by_id = {key: (raw, card) for key, raw, card in _rows(path)}
    selected = []
    for entry in entries:
        found = by_id.get(entry["id"])
        if found is None:
            raise ReconciliationBlocked("panel_original_card_missing")
        raw, card = found
        if (not isinstance(card, dict) or card.get("tipo") != entry["type"]
                or _fingerprint(card) != entry["fingerprint"]):
            raise ReconciliationBlocked("panel_original_card_changed")
        selected.append((entry["id"], raw, {**card, "id": entry["id"]}))
    return selected


def reconcile_legacy_batch(db_path, lock_path, manifest_path, bindings, client, *, apply=False):
    """Audit the original batch and optionally apply only fresh positive results."""
    path = Path(db_path).expanduser().resolve()
    lock = Path(lock_path).expanduser().resolve(strict=False)
    manifest = Path(manifest_path).expanduser().absolute()
    if not path.is_file() or not lock.parent.is_dir() or type(apply) is not bool:
        raise ReconciliationBlocked("reconciliation_target_invalid")
    entries = _manifest(manifest)
    selected = _batch(path, entries)
    report = audit_cards([card for _, _, card in selected], bindings, client)
    if not report["baseline_complete"]:
        raise ReconciliationBlocked("panel_baseline_changed")
    report["applied"] = []
    if not apply:
        return report
    decisions = {item["card_id"]: item for item in report["items"]
                 if item["decision"] in {"close", "superseded"}}
    if not decisions:
        return report
    with lock.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            with closing(sqlite3.connect(path, timeout=30)) as con:
                con.execute("BEGIN IMMEDIATE")
                try:
                    current = {key: raw for key, raw in con.execute(
                        "SELECT id,dados FROM itens ORDER BY id").fetchall()}
                    if any(current.get(key) != raw for key, raw, _ in selected):
                        raise ReconciliationBlocked("panel_changed_during_readback")
                    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    for card_id, _, card in selected:
                        decision = decisions.get(card_id)
                        if decision is None or card.get("status") != "pendente":
                            continue
                        status = "enviado" if decision["decision"] == "close" else "descartado"
                        card.pop("id", None)
                        card["status"] = status
                        card["atualizado"] = timestamp
                        card["reconciliacao_externa"] = {
                            "source": "connectsafely_readback",
                            "decision": decision["decision"],
                            "reason": decision["reason"],
                            "suggested_copy_sent_confirmed": decision["decision"] == "close",
                            "evidence": decision["evidence"],
                            "previous_sha256": sha256(current[card_id].encode()).hexdigest(),
                            "observed_at": timestamp,
                        }
                        con.execute("UPDATE itens SET dados=?, atualizado=? WHERE id=?",
                                    (json.dumps(card, ensure_ascii=False, allow_nan=False),
                                     timestamp, card_id))
                        report["applied"].append({"card_id": card_id, "status": status})
                    con.commit()
                except BaseException:
                    con.rollback()
                    raise
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    return report
