"""Restricted briefing storage in the existing panel ``main.itens`` table.

No schema creation, panel imports, network, approval or queue effects. The caller
owns the connection and must authorize access before calling this synchronous
library (use a worker thread with its own connection from async code).

``briefing_id(canal, thread)`` hashes the exact producer identity, case-sensitive,
without normalization. The producer must use a stable, account-scoped thread.
``put_briefing(con, item, None)`` creates version 1. To change content, pass the
last returned version. An exact content replay returns the stored card even with
a stale version, without writing. Only pending, strictly shaped cards are mutable.
Input contains exactly INPUT_FIELDS; version/atualizado are output-only fields.
All text is stored literally; rendering/HTML escaping belongs to the UI.
"""

from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3


MAX_BYTES = 65536
MAX_VERSION = 2**63 - 1
INPUT_FIELDS = frozenset({
    "id", "origem", "tipo", "titulo", "porque", "contexto", "fatos",
    "rascunho", "status",
})


class BriefingError(ValueError):
    """Stable, non-sensitive error code in ``code`` and exception message."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _text(value, limit, field, *, empty=False):
    if type(value) is not str or len(value) > limit:
        raise BriefingError("invalid_" + field)
    if not empty and not value.strip():
        raise BriefingError("invalid_" + field)
    if any((ord(c) < 32 and c not in "\n\r\t") or 0xD800 <= ord(c) <= 0xDFFF
           for c in value):
        raise BriefingError("invalid_" + field)
    return value


def briefing_id(canal, thread):
    """Return briefing- plus full SHA-256 of compact UTF-8 JSON [canal,thread]."""
    for field, value, limit in (("canal", canal, 64), ("thread", thread, 1024)):
        _text(value, limit, field)
        if any(ord(c) < 32 for c in value):
            raise BriefingError("invalid_" + field)
    raw = json.dumps([canal, thread], ensure_ascii=False, separators=(",", ":"))
    return "briefing-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _encode(value):
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False,
                     sort_keys=True, separators=(",", ":"))
    if len(raw.encode("utf-8")) > MAX_BYTES:
        raise BriefingError("item_too_large")
    return raw


def _validate(item):
    if type(item) is not dict or item.keys() != INPUT_FIELDS:
        raise BriefingError("invalid_fields")
    if type(item["id"]) is not str or not re.fullmatch(r"briefing-[0-9a-f]{64}", item["id"]):
        raise BriefingError("invalid_id")
    if item["origem"] != "briefing" or item["tipo"] != "pedido" or item["status"] != "pendente":
        raise BriefingError("invalid_kind_or_status")
    for field, limit in (("titulo", 240), ("porque", 4000), ("rascunho", 16000)):
        _text(item[field], limit, field, empty=field == "rascunho")
    if type(item["contexto"]) is not list or len(item["contexto"]) > 50:
        raise BriefingError("invalid_contexto")
    for entry in item["contexto"]:
        if type(entry) is not dict or entry.keys() != {"quem", "quando", "texto"}:
            raise BriefingError("invalid_contexto")
        _text(entry["quem"], 240, "quem")
        _text(entry["quando"], 64, "quando")
        try:
            when = datetime.fromisoformat(entry["quando"].replace("Z", "+00:00"))
        except ValueError:
            raise BriefingError("invalid_quando") from None
        if when.tzinfo is None or when.utcoffset() is None:
            raise BriefingError("invalid_quando")
        _text(entry["texto"], 8000, "texto")
    if type(item["fatos"]) is not list or len(item["fatos"]) > 50:
        raise BriefingError("invalid_fatos")
    for fact in item["fatos"]:
        _text(fact, 2000, "fato")
    # Detach validated nested data from the producer before acquiring a DB lock.
    return json.loads(_encode(item))


def _schema(con):
    kind = con.execute("SELECT type FROM main.sqlite_master WHERE name='itens'").fetchone()
    columns = [tuple(row)[1:6] for row in con.execute("PRAGMA main.table_info(itens)")]
    expected = [("id", "TEXT", 0, None, 1), ("dados", "JSON", 1, None, 0),
                ("atualizado", "TEXT", 1, None, 0)]
    if not kind or kind[0] != "table" or columns != expected:
        raise BriefingError("unsupported_schema")
    # Existing triggers could turn an otherwise restricted write into an effect.
    for schema in ("main", "temp"):
        if con.execute(f"SELECT 1 FROM {schema}.sqlite_master "
                       "WHERE type='trigger' AND lower(tbl_name)='itens' LIMIT 1").fetchone():
            raise BriefingError("unsupported_triggers")


def _stored(raw, item_id):
    if type(raw) is not str or len(raw.encode("utf-8")) > MAX_BYTES:
        raise BriefingError("invalid_stored_item")
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise BriefingError("invalid_stored_item") from None
    if type(data) is not dict:
        raise BriefingError("invalid_stored_item")
    if data.get("origem") != "briefing" or data.get("tipo") != "pedido":
        raise BriefingError("origin_collision")
    if data.get("status") != "pendente" or "_descarte" in data:
        raise BriefingError("terminal_or_protected")
    if data.keys() != (INPUT_FIELDS - {"id"}) | {"version", "atualizado"}:
        raise BriefingError("invalid_stored_item")
    if type(data["version"]) is not int or not 1 <= data["version"] <= MAX_VERSION:
        raise BriefingError("invalid_stored_version")
    _text(data["atualizado"], 64, "stored_timestamp")
    content = {k: v for k, v in data.items() if k not in ("version", "atualizado")}
    _validate(dict(content, id=item_id))
    return data, content


def put_briefing(connection, item, expected_version=None):
    """Atomically create/update; return stored fields plus id after COMMIT.

    Raises BriefingError with a stable code, including version_conflict,
    terminal_or_protected, origin_collision, busy, storage_error. Owns its short
    transaction; refuses an already active caller transaction. Lock wait is
    capped at 1000ms (preserves a shorter caller timeout), restored on exit.
    Does not close the connection. No automatic retries or schema migration.
    """
    item = _validate(item)
    if expected_version is not None and (
        type(expected_version) is not int or not 1 <= expected_version <= MAX_VERSION
    ):
        raise BriefingError("invalid_expected_version")
    if not isinstance(connection, sqlite3.Connection):
        raise BriefingError("invalid_connection")
    if connection.in_transaction:
        raise BriefingError("active_transaction")
    item_id = item.pop("id")
    timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
    connection.execute("PRAGMA busy_timeout=" + str(min(timeout, 1000)))
    started = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        started = True
        _schema(connection)
        row = connection.execute("SELECT dados FROM main.itens WHERE id=?", (item_id,)).fetchone()
        if row:
            previous, content = _stored(row[0], item_id)
            if content == item:
                connection.execute("COMMIT")
                return dict(previous, id=item_id)
            if expected_version != previous["version"]:
                raise BriefingError("version_conflict")
            if previous["version"] == MAX_VERSION:
                raise BriefingError("version_exhausted")
            version = previous["version"] + 1
        else:
            if expected_version is not None:
                raise BriefingError("version_conflict")
            version = 1
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        data = dict(item, version=version, atualizado=now)
        raw = _encode(data)
        if row:
            connection.execute("UPDATE main.itens SET dados=?, atualizado=? WHERE id=?",
                               (raw, now, item_id))
        else:
            connection.execute("INSERT INTO main.itens (id,dados,atualizado) VALUES (?,?,?)",
                               (item_id, raw, now))
        connection.execute("COMMIT")
        return dict(data, id=item_id)
    except sqlite3.Error as exc:
        code = getattr(exc, "sqlite_errorcode", 0) or 0
        raise BriefingError("busy" if code & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                            else "storage_error") from None
    finally:
        if started and connection.in_transaction:
            connection.execute("ROLLBACK")
        connection.execute("PRAGMA busy_timeout=" + str(timeout))
