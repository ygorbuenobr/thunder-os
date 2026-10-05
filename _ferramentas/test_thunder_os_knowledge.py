import tempfile
import threading
import unittest
from pathlib import Path

from thunder_os.knowledge import KnowledgeConflict, KnowledgeError, KnowledgeStore


class KnowledgeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "thunder").mkdir()
        (self.root / "contas/acme").mkdir(parents=True)
        (self.root / "thunder/empresa.md").write_text("---\nstatus: validado\n---\n# Thunder\n", encoding="utf-8")
        (self.root / "contas/acme/ficha.md").write_text("# Acme\n", encoding="utf-8")
        self.store = KnowledgeStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_safe_read_returns_provenance_hash_and_explicit_validation(self):
        snapshot = self.store.read("thunder/empresa.md")
        self.assertEqual("thunder/empresa.md", snapshot.provenance)
        self.assertEqual(64, len(snapshot.content_hash))
        self.assertTrue(snapshot.validated)
        missing = self.store.read("thunder/ausente.md", required=False)
        self.assertFalse(missing.validated)
        self.assertEqual("missing", missing.issue)

    def test_traversal_and_symlink_escape_are_rejected(self):
        outside = self.root.parent / "secret-thunder-os.txt"
        outside.write_text("secret", encoding="utf-8")
        self.addCleanup(outside.unlink)
        (self.root / "thunder/link.md").symlink_to(outside)
        for path in ("../secret-thunder-os.txt", "thunder/link.md"):
            with self.assertRaisesRegex(KnowledgeError, "outside brain root"):
                self.store.read(path)

    def test_source_hash_mismatch_and_invalid_fact_do_not_promote(self):
        current = self.store.read("contas/acme/ficha.md")
        with self.assertRaisesRegex(KnowledgeConflict, "source hash mismatch"):
            self.store.promote_validated_facts("acme", [{"fact": "Cresceu", "source": "https://example.com", "date": "2026-09-29", "status": "validado"}], "wrong")
        conflicts = list((self.root / "_reconciliation").glob("acme-*.json"))
        self.assertEqual(len(conflicts), 1)
        conflict = __import__("json").loads(conflicts[0].read_text(encoding="utf-8"))
        self.assertEqual((conflict["status"], conflict["current"]["content"], conflict["incoming"]["facts"][0]["fact"]),
                         ("needs_reconciliation", "# Acme\n", "Cresceu"))
        with self.assertRaisesRegex(KnowledgeError, "fact is not validated"):
            self.store.promote_validated_facts("acme", [{"fact": "Talvez", "source": "", "date": "2024-01-01", "status": "rascunho"}], current.content_hash)
        self.assertEqual("# Acme\n", (self.root / "contas/acme/ficha.md").read_text())

    def test_concurrent_append_preserves_each_validated_fact_once(self):
        first = self.store.read("contas/acme/ficha.md")
        results = []
        barrier = threading.Barrier(2)

        def append(index):
            barrier.wait()
            try:
                results.append(self.store.promote_validated_facts("acme", [{"fact": f"Fato {index}", "source": f"https://example.com/{index}", "date": "2026-09-29", "status": "validado"}], first.content_hash))
            except KnowledgeConflict:
                results.append("conflict")

        threads = [threading.Thread(target=append, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(1, sum(result == "conflict" for result in results))
        content = (self.root / "contas/acme/ficha.md").read_text()
        self.assertEqual(1, content.count("Fato "))


if __name__ == "__main__":
    unittest.main()
