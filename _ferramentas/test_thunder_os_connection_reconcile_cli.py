"""No provider calls: binding and recoverable receipt contract."""

import copy
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from thunder_os.connection_reconcile_cli import (
    _finish_receipt, _recover_receipt, _write_receipt, bindings_for_batch,
)
from thunder_os.connection_reconcile_apply import (
    _manifest, create_legacy_manifest, reconcile_legacy_batch,
)


class ReconcileCLIContractTests(unittest.TestCase):
    def test_bindings_require_exact_provider_identifiers(self):
        rows = [
            ("dm-ok", "", {"tipo": "dm", "perfil_url": "https://www.linkedin.com/in/diego/"}),
            ("dm-unsafe", "", {"tipo": "dm", "perfil_url": "https://www.linkedin.com/in/diego/?x=1"}),
            ("comment-ok", "", {"tipo": "comentario", "post_url":
                                 "https://www.linkedin.com/feed/update/urn:li:activity:123/"}),
            ("comment-no", "", {"tipo": "comentario", "post_url": "https://other.example/post"}),
            ("wa", "", {"tipo": "whatsapp", "perfil_url": "https://www.linkedin.com/in/diego/"}),
        ]
        found = bindings_for_batch(rows, "a" * 24, "owner-public",
                                   "urn:li:fsd_profile:ACoOWNER")
        self.assertEqual(set(found), {"dm-ok", "comment-ok"})
        self.assertEqual(found["dm-ok"]["recipient_public_id"], "diego")
        self.assertEqual(found["comment-ok"]["post_urn"], "urn:li:activity:123")

    def test_post_commit_receipt_failure_retains_recoverable_journal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db, lock, manifest, receipt = (root / name for name in
                                            ("panel.db", "panel.lock", "batch.json", "receipt.json"))
            with sqlite3.connect(db) as connection:
                connection.execute("CREATE TABLE itens (id TEXT PRIMARY KEY,dados JSON,atualizado TEXT)")
                for kind, count in (("dm", 21), ("comentario", 18), ("whatsapp", 2)):
                    for index in range(count):
                        connection.execute("INSERT INTO itens VALUES (?,?,?)", (
                            f"{kind}-{index}", json.dumps({"tipo": kind, "status": "pendente",
                                                             "texto": "draft"}), "before"))
            create_legacy_manifest(db, manifest)
            source_sha = "a" * 40
            manifest_sha = sha256(manifest.read_bytes()).hexdigest()
            _write_receipt(receipt, {"status": "started", "source_sha": source_sha,
                                     "manifest_sha256": manifest_sha})
            finding = {"baseline_complete": True, "items": [
                {"card_id": "dm-0", "decision": "superseded", "reason": "later_thread_event",
                 "evidence": {"reference_sha256": "b" * 64}}]}
            with patch("thunder_os.connection_reconcile_apply.audit_cards",
                       side_effect=lambda *_: copy.deepcopy(finding)):
                result = reconcile_legacy_batch(db, lock, manifest, {}, object(), apply=True)
            self.assertEqual(result["applied"], [{"card_id": "dm-0", "status": "descartado"}])
            with patch("thunder_os.connection_reconcile_cli.os.replace", side_effect=OSError):
                with self.assertRaises(OSError):
                    _finish_receipt(receipt, {"status": "complete"})
            self.assertEqual(json.loads(receipt.read_text())["status"], "started")
            recovered = _recover_receipt(receipt, db, _manifest(manifest),
                                         source_sha, manifest_sha)
            self.assertEqual(recovered["verified_changes"], 1)
            final = json.loads(receipt.read_text())
            self.assertEqual(final["status"], "recovered_from_panel")
            changed = next(item for item in final["items"] if item["card_id"] == "dm-0")
            self.assertEqual(changed["decision"], "superseded")
            self.assertEqual(changed["status"], "descartado")

    def test_journal_directory_sync_failure_prevents_apply(self):
        with tempfile.TemporaryDirectory() as temp:
            receipt = Path(temp) / "receipt.json"
            calls = []
            def fail_second_sync(fd):
                calls.append(fd)
                if len(calls) == 2:
                    raise OSError("directory sync failed")
            with patch("thunder_os.connection_reconcile_cli.os.fsync", side_effect=fail_second_sync):
                with self.assertRaises(OSError):
                    _write_receipt(receipt, {"status": "started"})
            self.assertEqual(len(calls), 2)
            self.assertEqual(json.loads(receipt.read_text())["status"], "started")

    def test_final_directory_sync_failure_can_verify_existing_final(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            receipt = root / "receipt.json"
            _write_receipt(receipt, {"status": "started", "source_sha": "a" * 40,
                                     "manifest_sha256": "b" * 64})
            final = {"mode": "apply", "source_sha": "a" * 40,
                     "manifest_sha256": "b" * 64, "report": {"items": [1, 2]}}
            with patch("thunder_os.connection_reconcile_cli._sync_directory", side_effect=OSError):
                with self.assertRaises(OSError):
                    _finish_receipt(receipt, final)
            self.assertEqual(json.loads(receipt.read_text()), final)
            result = _recover_receipt(receipt, root / "unused.db", [], "a" * 40, "b" * 64)
            self.assertEqual(result["status"], "existing_final_verified")
            self.assertEqual(result["items"], 2)


if __name__ == "__main__":
    unittest.main()
