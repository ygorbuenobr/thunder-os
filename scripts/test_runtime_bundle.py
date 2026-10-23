from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_runtime_bundle import build_bundle
from check_runtime_bundle import BundleError, check_bundle


class RuntimeBundleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.toques = self.root / "external" / "toques.py"
        self.toques.parent.mkdir()
        self.toques.write_text("# isolated test dependency\n", encoding="utf-8")
        self.digest = hashlib.sha256(self.toques.read_bytes()).hexdigest()
        self.contract = self.repo / "runtime/dependencies.json"
        self.write_repo_file("_ferramentas/painel.py", "# panel\n")
        self.write_repo_file("_ferramentas/painel_servidor.py", "# server\n")
        self.write_repo_file("_ferramentas/painel_modelo.html", "<html></html>\n")
        self.write_repo_file("_ferramentas/thunder_os/__init__.py", "# OS\n")
        self.write_repo_file("_ferramentas/thunder_os/schema.sqlite.sql", "CREATE TABLE x(id INTEGER);\n")
        self.write_repo_file(
            "runtime/dependencies.json",
            json.dumps({
                "schema_version": 1,
                "dependencies": {"toques.py": {
                    "bundle_path": "_ferramentas/toques.py", "sha256": self.digest,
                }},
            }),
        )
        self.run_git("init", "-q")
        self.run_git("config", "user.email", "bundle-test@example.invalid")
        self.run_git("config", "user.name", "Bundle Test")
        self.run_git("add", ".")
        self.run_git("commit", "-qm", "fixture")
        self.output = self.root / "staging" / "candidate"
        self.output.parent.mkdir()

    def run_git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    def write_repo_file(self, name: str, content: str) -> Path:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def build(self) -> dict:
        return build_bundle(self.repo, self.output, self.toques, self.contract)

    def test_build_contains_only_committed_os_code_and_pinned_dependency(self) -> None:
        manifest = self.build()
        self.assertEqual(manifest["source_sha"], self.run_git("rev-parse", "HEAD"))
        self.assertEqual(manifest["dependency_sha256"], self.digest)
        self.assertEqual(check_bundle(self.output, self.contract), manifest)
        self.assertEqual(
            set(manifest["files"]),
            {
                "_ferramentas/painel.py",
                "_ferramentas/painel_servidor.py",
                "_ferramentas/painel_modelo.html",
                "_ferramentas/thunder_os/__init__.py",
                "_ferramentas/thunder_os/schema.sqlite.sql",
                "_ferramentas/toques.py",
            },
        )

    def test_missing_or_wrong_external_dependency_refused(self) -> None:
        self.toques.unlink()
        with self.assertRaisesRegex(BundleError, "regular file"):
            self.build()
        self.assertFalse(self.output.exists())
        self.toques.write_text("# wrong dependency\n", encoding="utf-8")
        with self.assertRaisesRegex(BundleError, "SHA256 mismatch"):
            self.build()
        self.assertFalse(self.output.exists())

    def test_briefing_sources_and_readonly_agent_dropin_are_bundled(self) -> None:
        paths = [
            '_ferramentas/chief_briefing.py',
            '_ferramentas/chief_briefing_runner.py',
            '_ferramentas/chief_read_snapshot.py',
            '_ferramentas/chief_briefing_cards.py',
            '_ferramentas/seqd_mail_readonly.py',
            '_ferramentas/seqd_mail_readonly.cjs',
            'runtime/systemd/thunder-os-agent-email-readonly.conf',
            '_ferramentas/seqd_bridge/bridge.cjs',
            '_ferramentas/seqd_bridge/native.cjs',
            '_ferramentas/seqd_bridge/server.cjs',
            '_ferramentas/seqd_bridge/contract.cjs',
            '_ferramentas/seqd_bridge/journal.cjs',
            '_ferramentas/seqd_bridge/quota.cjs',
        ]
        for name in paths:
            self.write_repo_file(name, '# synthetic source only\n')
        self.write_repo_file('_ferramentas/seqd_bridge/bridge.test.cjs', '// tests only\n')
        self.run_git('add', '.')
        self.run_git('commit', '-qm', 'fixture: add briefing sources')
        manifest = self.build()
        for name in paths:
            self.assertIn(name, manifest['files'])
            self.assertEqual((self.output/name).read_text(), '# synthetic source only\n')
        self.assertFalse(any('credentials' in name or 'receipts.sqlite' in name
                             for name in manifest['files']))
        self.assertNotIn('_ferramentas/seqd_bridge/bridge.test.cjs',manifest['files'])

    def test_hermes_dropin_is_hashed_in_candidate(self) -> None:
        name = "runtime/systemd/thunder-os-agent-hermes-ro.conf"
        source = self.write_repo_file(name, "[Service]\nReadOnlyPaths=/etc/thunder-os\n")
        self.run_git("add", name)
        self.run_git("commit", "-qm", "fixture agent drop-in")
        manifest = self.build()
        self.assertEqual(
            manifest["files"][name]["sha256"],
            hashlib.sha256(source.read_bytes()).hexdigest(),
        )
        self.assertEqual((self.output / name).read_bytes(), source.read_bytes())

    def test_dirty_tree_refused_including_untracked_file(self) -> None:
        self.write_repo_file("_ferramentas/thunder_os/new.py", "# uncommitted\n")
        with self.assertRaisesRegex(BundleError, "dirty"):
            self.build()
        self.assertFalse(self.output.exists())

    def test_tracked_symlink_and_staging_path_escape_refused(self) -> None:
        link = self.repo / "_ferramentas/thunder_os/escape.py"
        link.symlink_to(self.toques)
        self.run_git("add", str(link.relative_to(self.repo)))
        self.run_git("commit", "-qm", "add symlink")
        with self.assertRaisesRegex(BundleError, "regular Git blob"):
            self.build()
        link.unlink()
        self.run_git("add", "-u")
        self.run_git("commit", "-qm", "remove symlink")
        escape = self.root / "staging" / "repo-link"
        escape.symlink_to(self.repo, target_is_directory=True)
        with self.assertRaisesRegex(BundleError, "outside the source repository"):
            build_bundle(self.repo, escape / "candidate", self.toques, self.contract)

    def test_manifest_path_escape_and_hash_tamper_refused(self) -> None:
        self.build()
        source = self.output / "_ferramentas/painel.py"
        source.write_text("# tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(BundleError, "hash or size mismatch"):
            check_bundle(self.output, self.contract)
        source.write_text("# panel\n", encoding="utf-8")
        manifest_file = self.output / "thunder-os-manifest.json"
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        manifest["files"]["../escape.py"] = manifest["files"]["_ferramentas/painel.py"]
        manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(BundleError, "forbidden bundle path"):
            check_bundle(self.output, self.contract)
        del manifest["files"]["../escape.py"]
        manifest["files"]["_ferramentas/thunder_os/escape\u0000.py"] = manifest["files"]["_ferramentas/painel.py"]
        manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(BundleError, "forbidden bundle path"):
            check_bundle(self.output, self.contract)

    def test_env_db_and_extra_files_never_enter_candidate(self) -> None:
        self.write_repo_file(".env", "FAKE_SECRET=1\n")
        self.write_repo_file("brain/state.db", "not a real database\n")
        self.run_git("add", "-f", ".env", "brain/state.db")
        self.run_git("commit", "-qm", "add forbidden fixture data")
        self.build()
        self.assertFalse((self.output / ".env").exists())
        self.assertFalse((self.output / "brain/state.db").exists())
        (self.output / ".env").write_text("FAKE_SECRET=1\n", encoding="utf-8")
        with self.assertRaisesRegex(BundleError, "unlisted files"):
            check_bundle(self.output, self.contract)

    def test_existing_output_refused(self) -> None:
        self.output.mkdir()
        with self.assertRaisesRegex(BundleError, "already exists"):
            self.build()


if __name__ == "__main__":
    unittest.main()
