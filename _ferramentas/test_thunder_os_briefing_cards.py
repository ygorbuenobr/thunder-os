"""Synthetic SQLite fixtures only; no panel imports, providers or real DBs."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from thunder_os.briefing_cards import BriefingError, briefing_id, put_briefing


DDL = "CREATE TABLE itens (id TEXT PRIMARY KEY, dados JSON NOT NULL, atualizado TEXT NOT NULL)"


def card():
    return {"id": briefing_id("email", "synthetic-account/thread-1"),
            "origem": "briefing", "tipo": "pedido", "status": "pendente",
            "titulo": "Synthetic briefing", "porque": "Synthetic inbound request",
            "contexto": [{"quem": "Synthetic sender", "quando": "2026-10-20T12:00:00Z",
                          "texto": "Synthetic message"}],
            "fatos": ["Synthetic fact"], "rascunho": "Synthetic draft"}


class BriefingCardsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="briefing-test-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "fixture.sqlite"
        self.con = sqlite3.connect(self.path, timeout=0.1)
        self.addCleanup(self.con.close)
        self.con.execute(DDL)

    def snapshot(self):
        return self.con.execute("SELECT id,dados,atualizado FROM itens ORDER BY id").fetchall()

    def assert_error(self, code, item=None, version=None, con=None):
        with self.assertRaises(BriefingError) as caught:
            put_briefing(con or self.con, card() if item is None else item, version)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def seed(self, data):
        self.con.execute("INSERT INTO itens VALUES (?,?,?)",
                         (card()["id"], json.dumps(data), "synthetic-original"))
        self.con.commit()

    def test_identity_is_stable_unambiguous_and_channel_scoped(self):
        raw = b'["email","synthetic-account/thread-1"]'
        self.assertEqual(card()["id"], "briefing-" + hashlib.sha256(raw).hexdigest())
        self.assertNotEqual(briefing_id("a", "bc"), briefing_id("ab", "c"))
        self.assertNotEqual(briefing_id("email", "1"), briefing_id("linkedin", "1"))
        for channel, thread in [("", "1"), ("a" * 65, "1"), ("email", "x" * 1025),
                                ("email", None), ("email", "x\n"), ("email", "\ud800")]:
            with self.subTest(channel=channel):
                with self.assertRaises(BriefingError):
                    briefing_id(channel, thread)

    def test_create_returns_durable_panel_shape_without_mutating_input(self):
        item = card()
        original = deepcopy(item)
        schema_before = self.con.execute("SELECT type,name,sql FROM sqlite_master").fetchall()
        result = put_briefing(self.con, item)
        self.assertEqual(item, original)
        self.assertEqual(result["version"], 1)
        self.assertFalse(self.con.in_transaction)
        with closing(sqlite3.connect(self.path)) as reader:
            row = reader.execute("SELECT id,dados,atualizado FROM itens").fetchone()
        data = json.loads(row[1])
        self.assertNotIn("id", data)
        self.assertEqual(result, dict(data, id=row[0]))
        self.assertEqual(row[2], result["atualizado"])
        self.assertEqual(schema_before, self.con.execute("SELECT type,name,sql FROM sqlite_master").fetchall())

    def test_exact_replay_is_no_write_even_with_stale_version(self):
        original = put_briefing(self.con, card())
        before, changes = self.snapshot(), self.con.total_changes
        for version in (None, 1, 99):
            self.assertEqual(put_briefing(self.con, card(), version), original)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.con.total_changes, changes)

    def test_update_requires_current_version_and_retry_does_not_bump(self):
        put_briefing(self.con, card())
        changed = dict(card(), titulo="Revised")
        self.assert_error("version_conflict", changed)
        self.assert_error("version_conflict", changed, 2)
        updated = put_briefing(self.con, changed, 1)
        self.assertEqual(updated["version"], 2)
        self.assertEqual(put_briefing(self.con, changed, 1), updated)
        self.assert_error("version_conflict", card(), 1)
        self.assertEqual(json.loads(self.snapshot()[0][1])["titulo"], "Revised")

    def test_missing_row_and_invalid_versions_cannot_create(self):
        self.assert_error("version_conflict", version=1)
        for version in (0, -1, True, 1.0, "1", 2**63):
            self.assert_error("invalid_expected_version", version=version)
        self.assertEqual(self.snapshot(), [])

    def test_whitelist_rejects_authority_and_effect_fields(self):
        for key in ("fila_ref", "acoes", "actions", "aprovacao", "approval", "decisao",
                    "_fila_pendente", "_descarte", "version", "atualizado", "conta"):
            with self.subTest(key=key):
                self.assert_error("invalid_fields", dict(card(), **{key: False}))
        self.assertEqual(self.snapshot(), [])

    def test_input_shape_types_sizes_and_timestamp_validation(self):
        cases = [("id", "briefing-'; DROP TABLE itens;--"), ("origem", "aceite"),
                 ("tipo", "dm"), ("status", "aprovado"), ("titulo", "x" * 241),
                 ("porque", "x" * 4001), ("rascunho", "x" * 16001),
                 ("titulo", None), ("titulo", "  "), ("titulo", "\x00"),
                 ("titulo", "\ud800"), ("contexto", {}), ("contexto", [{}]),
                 ("contexto", [card()["contexto"][0]] * 51), ("fatos", "fact"),
                 ("fatos", [False]), ("fatos", ["x"] * 51), ("fatos", ["x" * 2001])]
        for key, value in cases:
            with self.subTest(key=key, value_type=type(value).__name__):
                with self.assertRaises(BriefingError):
                    put_briefing(self.con, dict(card(), **{key: value}))
        for value in (None, [], {"id": card()["id"]}):
            with self.assertRaises(BriefingError):
                put_briefing(self.con, value)
        for when in ("yesterday", "2026-10-20", "2026-10-20T12:00:00", "x" * 65):
            item = card()
            item["contexto"][0]["quando"] = when
            self.assert_error("invalid_quando", item)
        item = card()
        item["contexto"][0]["actions"] = []
        self.assert_error("invalid_contexto", item)
        self.assert_error("item_too_large", dict(card(), fatos=["x" * 2000] * 40))
        self.assert_error("item_too_large", dict(card(), fatos=["\u20ac" * 2000] * 12))
        self.assertEqual(self.snapshot(), [])

    def test_other_origin_or_type_collision_preserves_row(self):
        self.seed({"origem": "aceite", "tipo": "pedido", "status": "pendente"})
        before = self.snapshot()
        self.assert_error("origin_collision")
        self.assertEqual(self.snapshot(), before)

    def test_every_nonpending_status_and_discard_marker_are_protected(self):
        for status in ("descartado", "aprovado", "na-fila", "enviado", "concluido",
                       "rejeitado", "copiar", "unknown", None):
            with self.subTest(status=status):
                self.con.execute("DELETE FROM itens")
                self.con.commit()
                self.seed({"origem": "briefing", "tipo": "pedido", "status": status,
                           "version": 1})
                before = self.snapshot()
                self.assert_error("terminal_or_protected", version=1)
                self.assertEqual(self.snapshot(), before)
        self.con.execute("DELETE FROM itens")
        self.con.commit()
        item = card()
        item.pop("id")
        self.seed(dict(item, _descarte={}, version=1))
        self.assert_error("terminal_or_protected", version=1)

    def test_pending_legacy_or_effect_metadata_never_gets_overwritten(self):
        result = put_briefing(self.con, card())
        result.pop("id")
        for change in ({"fila_ref": "synthetic"}, {"actions": []}, {"version": True}):
            data = dict(result, **change)
            self.con.execute("UPDATE itens SET dados=?", (json.dumps(data),))
            self.con.commit()
            before = self.snapshot()
            with self.assertRaises(BriefingError):
                put_briefing(self.con, card(), 1)
            self.assertEqual(self.snapshot(), before)
        result.pop("version")
        self.con.execute("UPDATE itens SET dados=?", (json.dumps(result),))
        self.con.commit()
        self.assert_error("invalid_stored_item")

    def test_corrupt_json_rolls_back_and_connection_remains_usable(self):
        for raw in ("{", "[]", "null", "x" * 65537):
            self.con.execute("INSERT OR REPLACE INTO itens VALUES (?,?,?)", (card()["id"], raw, "old"))
            self.con.commit()
            before = self.snapshot()
            self.assert_error("invalid_stored_item")
            self.assertFalse(self.con.in_transaction)
            self.assertEqual(self.snapshot(), before)

    def test_sql_and_html_are_literal_data_and_do_not_execute(self):
        text = "'); DROP TABLE itens;-- <script>alert('synthetic')</script>"
        result = put_briefing(self.con, dict(card(), titulo=text, rascunho=text))
        self.assertEqual(result["titulo"], text)
        self.assertEqual(json.loads(self.snapshot()[0][1])["rascunho"], text)
        self.assertEqual(len(self.snapshot()), 1)

    def test_active_transaction_is_not_committed_or_rolled_back(self):
        self.con.execute("INSERT INTO itens VALUES ('unrelated','{}','old')")
        self.assert_error("active_transaction")
        self.assertTrue(self.con.in_transaction)
        self.assertEqual(self.snapshot(), [("unrelated", "{}", "old")])
        self.con.rollback()
        self.assertEqual(self.snapshot(), [])

    def test_missing_schema_does_not_create_anything(self):
        with closing(sqlite3.connect(":memory:")) as con:
            self.assert_error("unsupported_schema", con=con)
            self.assertEqual(con.execute("SELECT name FROM sqlite_master").fetchall(), [])

    def test_wrong_schema_and_trigger_effects_are_refused(self):
        with closing(sqlite3.connect(":memory:")) as con:
            con.execute("CREATE TABLE itens (id TEXT, dados TEXT, atualizado TEXT)")
            self.assert_error("unsupported_schema", con=con)
        for temporary in (False, True):
            with closing(sqlite3.connect(":memory:")) as con:
                con.execute(DDL)
                con.execute("CREATE TABLE outbox (payload TEXT)")
                con.execute("CREATE " + ("TEMP " if temporary else "") +
                            "TRIGGER synthetic AFTER INSERT ON main.itens BEGIN "
                            "INSERT INTO outbox VALUES (NEW.dados); END")
                self.assert_error("unsupported_triggers", con=con)
                self.assertEqual(con.execute("SELECT * FROM itens").fetchall(), [])
                self.assertEqual(con.execute("SELECT * FROM outbox").fetchall(), [])

    def test_temp_table_cannot_shadow_target(self):
        self.con.execute("CREATE TEMP TABLE itens (id TEXT, dados TEXT, atualizado TEXT)")
        put_briefing(self.con, card())
        self.assertEqual(self.con.execute("SELECT count(*) FROM main.itens").fetchone()[0], 1)
        self.assertEqual(self.con.execute("SELECT count(*) FROM temp.itens").fetchone()[0], 0)

    def test_locked_database_is_recoverable_and_timeout_restored(self):
        with closing(sqlite3.connect(self.path)) as other:
            other.execute("BEGIN IMMEDIATE")
            before = self.con.execute("PRAGMA busy_timeout").fetchone()[0]
            self.assert_error("busy")
            self.assertFalse(self.con.in_transaction)
            self.assertEqual(self.con.execute("PRAGMA busy_timeout").fetchone()[0], before)
        self.assertEqual(put_briefing(self.con, card())["version"], 1)

    def test_concurrent_updates_have_one_winner_without_lost_update(self):
        put_briefing(self.con, card())
        barrier = threading.Barrier(2)

        def update(title):
            with closing(sqlite3.connect(self.path, timeout=1)) as con:
                barrier.wait(timeout=5)
                try:
                    return put_briefing(con, dict(card(), titulo=title), 1)
                except BriefingError as exc:
                    return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(update, ("First", "Second")))
        self.assertEqual(results.count("version_conflict"), 1)
        winner = next(x for x in results if isinstance(x, dict))
        self.assertEqual(winner["version"], 2)
        self.assertEqual(json.loads(self.snapshot()[0][1])["titulo"], winner["titulo"])

    def test_concurrent_identical_creation_is_idempotent(self):
        barrier = threading.Barrier(2)

        def create(_):
            with closing(sqlite3.connect(self.path, timeout=1)) as con:
                barrier.wait(timeout=5)
                return put_briefing(con, card())

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0]["version"], 1)
        self.assertEqual(len(self.snapshot()), 1)

    def test_autocommit_connection_and_sqlite_row_factory(self):
        with closing(sqlite3.connect(self.path, isolation_level=None)) as con:
            con.row_factory = sqlite3.Row
            self.assertEqual(put_briefing(con, card())["version"], 1)
            self.assertFalse(con.in_transaction)

    def test_readonly_failure_has_no_sensitive_error_text(self):
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as con:
            self.assert_error("storage_error", con=con)
        self.assertEqual(self.snapshot(), [])

    def test_commit_failure_rolls_back_insert_and_restores_connection(self):
        def deny_commit(action, arg1, arg2, database, source):
            if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.con.set_authorizer(deny_commit)
        try:
            self.assert_error("storage_error")
            self.assertFalse(self.con.in_transaction)
            self.assertEqual(self.snapshot(), [])
        finally:
            self.con.set_authorizer(None)
        self.assertEqual(put_briefing(self.con, card())["version"], 1)

    def test_discard_audit_and_timestamp_survive_stale_replay(self):
        data = put_briefing(self.con, card())
        data.pop("id")
        data.update(status="descartado", decisao={"acao": "descartar", "quando": "synthetic"},
                    _descarte={"ator": "ygor", "quando": "synthetic", "origem": "painel"})
        self.con.execute("UPDATE itens SET dados=?", (json.dumps(data),))
        self.con.commit()
        before, changes = self.snapshot(), self.con.total_changes
        for version in (None, 1):
            self.assert_error("terminal_or_protected", version=version)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.con.total_changes, changes)

    def test_supported_field_boundaries_and_empty_optional_content(self):
        item = dict(card(), titulo="x" * 240, porque="x" * 4000,
                    rascunho="x" * 16000, contexto=[], fatos=[])
        self.assertEqual(put_briefing(self.con, item)["version"], 1)
        item["rascunho"] = ""
        self.assertEqual(put_briefing(self.con, item, 1)["version"], 2)

    def test_version_exhaustion_never_overflows_or_rewrites(self):
        data = put_briefing(self.con, card())
        data.pop("id")
        data["version"] = 2**63 - 1
        self.con.execute("UPDATE itens SET dados=?", (json.dumps(data),))
        self.con.commit()
        before = self.snapshot()
        self.assert_error("version_exhausted", dict(card(), titulo="Revised"), 2**63 - 1)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
