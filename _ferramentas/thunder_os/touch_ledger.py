"""Persistent adapter over the canonical touch ledger and its shared person lock."""

from __future__ import annotations

from typing import Any, Mapping

try:
    import toques  # type: ignore
except ImportError:  # Package tests may import through the repository root.
    from _ferramentas import toques


STATUS_FROM_DB = {
    "reservado": "reserved", "feito": "sent", "erro": "failed",
    "incerto": "uncertain", "liberado": "released",
}
STATUS_TO_DB = {"sent": "feito", "failed": "erro", "uncertain": "incerto"}


class TouchBlocked(toques.ErroBanco):
    pass


class TouchLedger:
    def __init__(self, *, db_context=None):
        self._explicit_db_context = db_context is not None
        self._banco = db_context or toques._banco

    def migrate(self) -> None:
        if self._explicit_db_context:
            raise ValueError("explicit_schema_provisioning_required")
        toques.migrar()
        with self._banco() as (connection, _marker):
            connection.cursor().execute(
                """CREATE TABLE IF NOT EXISTS thunder_suppressions (
                    canal text NOT NULL, pessoa_chave text NOT NULL, reason text NOT NULL,
                    created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(canal,pessoa_chave)
                )"""
            )
            connection.cursor().execute(
                """CREATE TABLE IF NOT EXISTS thunder_effect_touches (
                    effect_key text PRIMARY KEY, touch_id bigint NOT NULL
                )"""
            )

    @staticmethod
    def _scope(step: Mapping[str, Any], effect_key: str) -> tuple[str, str, str, str]:
        channel = str(step["channel"])
        person = toques.normalizar_pessoa(channel, str(step["recipient"]))
        if channel == "linkedin" and step.get("action") == "like":
            action = "curtida"
            target = toques.normalizar_alvo(action, str(step.get("post_url", "")))
        elif channel == "email":
            action = "email"
            target = toques.normalizar_alvo(action, str(step.get("subject", "")))
        elif channel == "linkedin":
            action = "mensagem"
            target = toques.normalizar_alvo(action, f"thunder-os#{effect_key}")
        else:
            action = "whatsapp"
            target = toques.normalizar_alvo(action, effect_key)
        return channel, action, person, target

    def validate_identity(self, step: Mapping[str, Any]) -> str:
        return self._scope(step, "identity-check")[2]

    @staticmethod
    def identity_destination(person: str) -> tuple[str, str] | None:
        matches = []
        for channel in ("email", "linkedin", "whatsapp"):
            try:
                matches.append((channel, toques.normalizar_pessoa(channel, person)))
            except ValueError:
                continue
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _lock(connection: Any, marker: str, channel: str, person: str) -> None:
        if marker == "?":
            connection.execute("BEGIN IMMEDIATE")
        else:
            connection.cursor().execute(
                "SELECT pg_advisory_xact_lock(hashtext(" + marker + "))", (f"{channel}:{person}",)
            )

    def reserve(self, effect_key: str, step_id: str, step: Mapping[str, Any]) -> tuple[bool, dict[str, Any]]:
        channel, action, person, target = self._scope(step, effect_key)
        with self._banco() as (connection, marker):
            self._lock(connection, marker, channel, person)
            cursor = connection.cursor()
            cursor.execute(
                "SELECT reason FROM thunder_suppressions WHERE canal=" + marker + " AND pessoa_chave=" + marker,
                (channel, person),
            )
            if cursor.fetchone():
                raise TouchBlocked("suppressed")
            cursor.execute(
                "SELECT id FROM respostas WHERE canal=" + marker + " AND pessoa_chave=" + marker + " LIMIT 1",
                (channel, person),
            )
            if cursor.fetchone():
                raise TouchBlocked("has_reply")
            cursor.execute(
                "SELECT id,status,provider_id,detalhe FROM toques WHERE canal=" + marker
                + " AND acao=" + marker + " AND pessoa_chave=" + marker + " AND alvo=" + marker
                + " AND status<>'liberado'",
                (channel, action, person, target),
            )
            existing = cursor.fetchone()
            if existing:
                cursor.execute(
                    "INSERT INTO thunder_effect_touches(effect_key,touch_id) VALUES ("
                    + marker + "," + marker + ") ON CONFLICT(effect_key) DO NOTHING",
                    (effect_key, existing[0]),
                )
                return False, {
                    "id": str(existing[0]), "status": STATUS_FROM_DB[existing[1]],
                    "receipt_ref": existing[2], "error_code": existing[3],
                }
            fields = (channel, action, person, target, "reservado", "thunder-os", step_id)
            cursor.execute(
                "INSERT INTO toques(canal,acao,pessoa_chave,alvo,status,agente,ref) VALUES ("
                + ",".join([marker] * len(fields)) + ") RETURNING id",
                fields,
            )
            touch_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO thunder_effect_touches(effect_key,touch_id) VALUES ("
                + marker + "," + marker + ")",
                (effect_key, touch_id),
            )
            return True, {"id": str(touch_id), "status": "reserved"}

    def preflight(
        self, effect_key: str, step: Mapping[str, Any], *, raise_blocked: bool = False,
    ) -> dict[str, Any]:
        channel, action, person, target = self._scope(step, effect_key)
        with self._banco() as (connection, marker):
            cursor = connection.cursor()
            cursor.execute(
                "SELECT reason FROM thunder_suppressions WHERE canal=" + marker + " AND pessoa_chave=" + marker,
                (channel, person),
            )
            suppression = cursor.fetchone()
            cursor.execute(
                "SELECT id FROM respostas WHERE canal=" + marker + " AND pessoa_chave=" + marker + " LIMIT 1",
                (channel, person),
            )
            reply = cursor.fetchone()
            cursor.execute(
                "SELECT id,status,provider_id FROM toques WHERE canal=" + marker + " AND acao=" + marker
                + " AND pessoa_chave=" + marker + " AND alvo=" + marker + " AND status<>'liberado'",
                (channel, action, person, target),
            )
            duplicate = cursor.fetchone()
        blocked = "suppressed" if suppression else "has_reply" if reply else None
        if blocked and raise_blocked:
            raise TouchBlocked(blocked)
        return {
            "identity": person, "channel": channel, "blocked": blocked,
            "duplicate": ({"touch_id": str(duplicate[0]), "status": STATUS_FROM_DB[duplicate[1]],
                           "receipt_ref": duplicate[2]} if duplicate else None),
        }

    def finish(
        self, effect_key: str, status: str, receipt_ref: str | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        if status not in STATUS_TO_DB:
            raise ValueError("invalid touch status")
        with self._banco() as (connection, marker):
            cursor = connection.cursor()
            cursor.execute(
                "SELECT touch_id FROM thunder_effect_touches WHERE effect_key=" + marker,
                (effect_key,),
            )
            row = cursor.fetchone()
            if not row:
                raise TouchBlocked("reservation missing")
            cursor.execute(
                "UPDATE toques SET status=" + marker + ",provider_id=" + marker + ",detalhe=" + marker
                # Confirmation is terminal, including its original receipt.
                # The predicate is evaluated under the database row/write lock,
                # so a delayed uncertain response cannot undo reconciliation.
                + ",atualizado_em=CURRENT_TIMESTAMP WHERE id=" + marker + " AND status<>'feito'",
                (STATUS_TO_DB[status], receipt_ref, error_code, row[0]),
            )
            cursor.execute(
                "SELECT id,status,provider_id,detalhe FROM toques WHERE id=" + marker,
                (row[0],),
            )
            stored = cursor.fetchone()
            if not stored:
                raise TouchBlocked("reservation missing")
            return {
                "id": str(stored[0]), "status": STATUS_FROM_DB[stored[1]],
                "receipt_ref": stored[2], "error_code": stored[3],
            }

    def release_failed_for_retry(self, effect_key: str) -> dict[str, Any]:
        with self._banco() as (connection, marker):
            cursor = connection.cursor()
            cursor.execute(
                "SELECT t.id,t.status FROM thunder_effect_touches e JOIN toques t ON t.id=e.touch_id WHERE e.effect_key=" + marker,
                (effect_key,),
            )
            row = cursor.fetchone()
            if not row:
                return {"touch_id": None, "status": "not_reserved"}
            if row[1] != "erro":
                raise TouchBlocked("touch is not safely releasable")
            cursor.execute("UPDATE toques SET status='liberado',atualizado_em=CURRENT_TIMESTAMP WHERE id=" + marker, (row[0],))
            cursor.execute("DELETE FROM thunder_effect_touches WHERE effect_key=" + marker, (effect_key,))
            return {"touch_id": str(row[0]), "status": "released"}

    @staticmethod
    def _suppression_scope(channel, person, identities):
        return sorted({(channel, toques.normalizar_pessoa(channel, person))} | {
            (c, toques.normalizar_pessoa(c, p)) for c, p in identities})

    @staticmethod
    def _write_suppressions(cursor, marker, identities, reason):
        for channel, person in identities:
            cursor.execute(
                "INSERT INTO thunder_suppressions(canal,pessoa_chave,reason) VALUES ("
                + ",".join([marker] * 3) + ") ON CONFLICT(canal,pessoa_chave) DO UPDATE SET reason=excluded.reason",
                (channel, person, reason),
            )

    def suppress(self, channel: str, person: str, reason: str, *, identities=()) -> None:
        scope = self._suppression_scope(channel, person, identities)
        with self._banco() as (connection, marker):
            self._lock_scope(connection, marker, scope)
            self._write_suppressions(connection.cursor(), marker, scope, reason)

    def _lock_scope(self, connection, marker, scope):
        # SQLite has one write lock; PostgreSQL takes person locks in stable order.
        for index, (channel, person) in enumerate(scope):
            if marker != "?" or index == 0:
                self._lock(connection, marker, channel, person)

    def record_reply(
        self, channel: str, person: str, classification: str, summary: str, reference: str,
        preceding_touch_id: str,
        *, identities=(),
    ) -> int:
        mapped = {
            "positive": "positiva", "objection": "objecao", "negative": "negativa",
            "opt_out": "negativa", "risk": "negativa", "unrelated": "neutra",
        }[classification]
        person_key = toques.normalizar_pessoa(channel, person)
        scope = self._suppression_scope(channel, person, identities)
        with self._banco() as (connection, marker):
            self._lock_scope(connection, marker, scope)
            cursor = connection.cursor()
            cursor.execute(
                "SELECT id FROM toques WHERE id=" + marker + " AND canal=" + marker
                + " AND pessoa_chave=" + marker + " AND status='feito'",
                (preceding_touch_id, channel, person_key),
            )
            touch = cursor.fetchone()
            if not touch:
                raise TouchBlocked("preceding_touch_mismatch")
            values = (channel, person_key, None, None, touch[0], mapped, summary, reference)
            cursor.execute(
                "INSERT INTO respostas (canal,pessoa_chave,pessoa_nome,conta,toque_id,classificacao,resumo,ref) VALUES ("
                + ",".join([marker] * 8) + ") ON CONFLICT (canal,pessoa_chave,ref) DO NOTHING",
                values,
            )
            cursor.execute(
                "SELECT id FROM respostas WHERE canal=" + marker + " AND pessoa_chave=" + marker
                + " AND ref=" + marker,
                (channel, person_key, reference),
            )
            reply_id = int(cursor.fetchone()[0])
            if classification in {"negative", "opt_out", "risk"}:
                self._write_suppressions(cursor, marker, scope, classification)
            return reply_id
