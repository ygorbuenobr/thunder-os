import tempfile
import unittest
from pathlib import Path

from thunder_os.connection_catalog import load_connection_catalog
from thunder_os.connection_intake import ConnectionIntakeError


class ConnectionCatalogTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.brain = Path(self.scratch.name)
        (self.brain / "contas").mkdir()

    def ficha(self, name, domain, profile, *, sector="Distribuição B2B"):
        folder = self.brain / "contas" / name
        folder.mkdir()
        (folder / "ficha.md").write_text(
            f"# Fixture\n- Site: https://{domain}\n- Setor: {sector}\n\n## Decisores\n"
            f"| Pessoa | LinkedIn |\n|---|---|\n| Fixture | {profile} |\n"
            "\n## Histórico operacional\nCredencial fictícia não pertence ao contexto.\n",
            encoding="utf-8")

    def test_exact_profile_and_sector_evidence_only(self):
        profile = "https://www.linkedin.com/in/fixture-person?trk=source"
        self.ficha("account-one", "account.example", profile)
        mapping, fit = load_connection_catalog(self.brain)
        self.assertEqual({"https://www.linkedin.com/in/fixture-person": "account.example"}, mapping)
        self.assertEqual(1, len(fit))
        self.assertTrue(fit["account.example"].startswith("brain:contas/account-one/ficha.md#sha256:"))
        self.assertNotIn("Credencial", repr((mapping, fit)))

    def test_unrelated_profile_and_nonfit_sector_are_not_promoted(self):
        self.ficha("retail", "retail.example", "https://linkedin.com/in/person-one", sector="Varejo B2C")
        mapping, fit = load_connection_catalog(self.brain)
        self.assertEqual("retail.example", mapping["https://www.linkedin.com/in/person-one"])
        self.assertEqual({}, fit)

    def test_conflicting_profile_fails_closed(self):
        profile = "https://linkedin.com/in/same-person"
        self.ficha("first", "first.example", profile)
        self.ficha("second", "second.example", profile)
        with self.assertRaisesRegex(ConnectionIntakeError, "connection_account_ambiguous"):
            load_connection_catalog(self.brain)

    def test_invalid_profile_does_not_block_valid_exact_links(self):
        self.ficha("account", "account.example", "https://linkedin.com/in/person%2Fbad")
        path = self.brain / "contas" / "account" / "ficha.md"
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n## Decisor\nhttps://linkedin.com/in/person-good\n")
        mapping, fit = load_connection_catalog(self.brain)
        self.assertEqual({"https://www.linkedin.com/in/person-good": "account.example"}, mapping)
        self.assertIn("account.example", fit)

    def test_extra_profile_path_is_not_truncated_into_valid_identity(self):
        self.ficha("account", "account.example", "https://linkedin.com/in/person-good/evil")
        mapping, _ = load_connection_catalog(self.brain)
        self.assertEqual({}, mapping)

    def test_symlink_and_invalid_site_are_ignored(self):
        outside = self.brain / "outside.md"
        outside.write_text("# Outside\n- Site: https://outside.example\n## Decisores\nhttps://linkedin.com/in/outside")
        (self.brain / "contas" / "linked").symlink_to(self.brain)
        self.ficha("invalid", "valid.example:bad", "https://linkedin.com/in/invalid")
        self.assertEqual(({}, {}), load_connection_catalog(self.brain))


if __name__ == "__main__":
    unittest.main()
