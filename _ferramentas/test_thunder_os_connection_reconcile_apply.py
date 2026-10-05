"""Fixed legacy-batch reconciliation: no caller-provided decisions."""

import copy
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from thunder_os.connection_reconcile_apply import (
    ReconciliationBlocked, create_legacy_manifest, reconcile_legacy_batch,
)
from thunder_os.connectsafely_social_client import ConnectSafelySocialClient
from test_thunder_os_connection_reconcile_runtime import (
    ACCOUNT, OWNER, DM, DM_BINDING, FakeProvider,
)


class ReconcileApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = root / "painel.db"
        self.lock = root / ".painel.lock"
        self.manifest = root / "legacy-batch.json"
        with closing(sqlite3.connect(self.db)) as con:
            con.execute("CREATE TABLE itens (id TEXT PRIMARY KEY,dados JSON NOT NULL,atualizado TEXT NOT NULL)")
            for kind, n in (("dm", 21), ("comentario", 18), ("whatsapp", 2)):
                for i in range(n):
                    key = f"{kind}-{i}"
                    con.execute("INSERT INTO itens VALUES (?,?,?)",
                                (key, json.dumps({"tipo": kind, "status": "pendente",
                                                  "texto": "private copy"}), "before"))
            con.commit()
        create_legacy_manifest(self.db, self.manifest)

    def status(self, key):
        with closing(sqlite3.connect(self.db)) as con:
            row = con.execute("SELECT dados FROM itens WHERE id=?", (key,)).fetchone()
        return json.loads(row[0])

    def test_preview_never_writes_and_apply_needs_fresh_receipt(self):
        result = {"baseline_complete": True, "items": [
            {"card_id": "dm-0", "decision": "close", "reason": "positive_provider_readback",
             "evidence": {"reference_sha256": "a" * 64}},
            {"card_id": "dm-1", "decision": "superseded", "reason": "later_thread_event",
             "evidence": {"reference_sha256": "b" * 64}},
        ]}
        with patch("thunder_os.connection_reconcile_apply.audit_cards",
                   side_effect=lambda *_: copy.deepcopy(result)):
            preview = reconcile_legacy_batch(self.db, self.lock, self.manifest, {}, object())
            self.assertEqual(preview["applied"], [])
            self.assertEqual(self.status("dm-0")["status"], "pendente")
            applied = reconcile_legacy_batch(self.db, self.lock, self.manifest,
                                             {}, object(), apply=True)
            repeated = reconcile_legacy_batch(self.db, self.lock, self.manifest,
                                              {}, object(), apply=True)
        self.assertEqual(len(applied["applied"]), 2)
        self.assertEqual(repeated["applied"], [])
        self.assertEqual(self.status("dm-0")["status"], "enviado")
        self.assertEqual(self.status("dm-1")["status"], "descartado")
        self.assertFalse(self.status("dm-1")["reconciliacao_externa"]["suggested_copy_sent_confirmed"])
        self.assertEqual(self.status("dm-2")["status"], "pendente")

    def test_changed_baseline_fails_without_updates(self):
        with closing(sqlite3.connect(self.db)) as con:
            con.execute("DELETE FROM itens WHERE id='dm-0'")
            con.commit()
        with self.assertRaisesRegex(ReconciliationBlocked, "panel_original_card_missing"):
            reconcile_legacy_batch(self.db, self.lock, self.manifest, {}, object(), apply=True)

    def test_concurrent_card_change_rolls_back_all(self):
        def changed(*args):
            with closing(sqlite3.connect(self.db)) as con:
                con.execute("UPDATE itens SET dados=? WHERE id='dm-1'",
                            (json.dumps({"tipo": "dm", "status": "pendente", "texto": "changed"}),))
                con.commit()
            return {"baseline_complete": True, "items": [
                {"card_id": "dm-0", "decision": "close", "reason": "positive_provider_readback",
                 "evidence": {"reference_sha256": "a" * 64}}]}
        with patch("thunder_os.connection_reconcile_apply.audit_cards", side_effect=changed):
            with self.assertRaisesRegex(ReconciliationBlocked, "panel_changed_during_readback"):
                reconcile_legacy_batch(self.db, self.lock, self.manifest,
                                       {}, object(), apply=True)
        self.assertEqual(self.status("dm-0")["status"], "pendente")

    def test_same_count_replacement_does_not_replace_original_identity(self):
        with closing(sqlite3.connect(self.db)) as con:
            con.execute("DELETE FROM itens WHERE id='dm-0'")
            con.execute("INSERT INTO itens VALUES (?,?,?)",
                        ("dm-replacement", json.dumps({"tipo": "dm", "status": "pendente",
                                                       "texto": "private copy"}), "now"))
            con.commit()
        with self.assertRaisesRegex(ReconciliationBlocked, "panel_original_card_missing"):
            reconcile_legacy_batch(self.db, self.lock, self.manifest, {}, object())

    def test_provider_readback_then_apply_and_resume(self):
        with closing(sqlite3.connect(self.db)) as con:
            con.execute("UPDATE itens SET dados=? WHERE id='dm-0'",
                        (json.dumps({k: v for k, v in DM.items() if k != "id"}),))
            con.commit()
        # New original batch for this fixture is observer-produced from the DB.
        self.manifest.unlink()
        create_legacy_manifest(self.db, self.manifest)
        provider = FakeProvider()
        client = ConnectSafelySocialClient(ACCOUNT, "owner-fixture",
                                            self_profile_id=OWNER, request=provider)
        binding = {"dm-0": {**DM_BINDING, "card_id": "dm-0"}}
        report = reconcile_legacy_batch(self.db, self.lock, self.manifest,
                                        binding, client, apply=True)
        self.assertEqual(report["applied"], [{"card_id": "dm-0", "status": "enviado"}])
        self.assertEqual(self.status("dm-0")["status"], "enviado")
        self.assertEqual(reconcile_legacy_batch(self.db, self.lock, self.manifest,
                                                binding, client, apply=True)["applied"], [])
