import hashlib
import importlib.util
import concurrent.futures
from unittest import mock
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("daily_mission_prepare.py")


class PrepareTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base / "source"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        self.toques = self.base / "dependency" / "toques.py"
        self.toques.parent.mkdir()
        self.toques.write_text("synthetic dependency\n")
        (self.repo / "runtime").mkdir()
        (self.repo / "runtime/dependencies.json").write_text(json.dumps({
            "dependencies": {"toques.py": {"sha256": hashlib.sha256(self.toques.read_bytes()).hexdigest()}}
        }))
        (self.repo / "tracked.txt").write_text("committed\n")
        (self.repo / ".gitignore").write_text("ignored.txt\n")
        self.git("add", ".")
        self.git("commit", "-qm", "synthetic root")
        self.parent_sha = self.git("rev-parse", "HEAD").strip()
        (self.repo / "tracked.txt").write_text("committed\n")
        (self.repo / "history.txt").write_text("second commit\n")
        self.git("add", "history.txt")
        self.git("commit", "-qm", "synthetic second")
        self.sha = self.git("rev-parse", "HEAD").strip()
        self.git("update-ref", "refs/remotes/origin/main", self.sha)
        self.branch = self.git("branch", "--show-current").strip()
        (self.repo / "tracked.txt").write_text("unsaved\n")
        (self.repo / "ignored.txt").write_text("private\n")
        (self.repo / "untracked.db").write_text("private\n")
        self.candidate = self.base / "candidate"
        self.validated_toques = self.base / "toques.py"

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.repo, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10).stdout

    def run_cli(self, sha=None, candidate=None, toques=None):
        return subprocess.run([sys.executable, str(SCRIPT), "--repo", str(self.repo),
                               "--expected-commit", sha or self.sha, "--candidate", str(candidate or self.candidate),
                               "--toques", str(toques or self.toques),
                               "--validated-toques", str(self.validated_toques)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)

    def test_prepare_uses_committed_tree_detached_without_private_files_or_hardlinks(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.candidate).strip(), self.sha)
        branch = subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=self.candidate,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertNotEqual(branch.returncode, 0)
        self.assertEqual((self.candidate / "tracked.txt").read_text(), "committed\n")
        self.assertEqual((self.repo / "tracked.txt").read_text(), "unsaved\n")
        for name in ("ignored.txt", "untracked.db", "toques.py"):
            self.assertFalse((self.candidate / name).exists())
        source_object = self.repo / ".git/objects" / self.sha[:2] / self.sha[2:]
        candidate_object = self.candidate / ".git/objects" / self.sha[:2] / self.sha[2:]
        self.assertNotEqual(os.stat(source_object).st_ino, os.stat(candidate_object).st_ino)
        self.assertEqual(self.git("branch", "--show-current").strip(), self.branch)

    def test_wrong_or_invalid_sha_fails_without_candidate(self):
        for sha in ("0" * 40, "e1b6302", "not-a-sha"):
            with self.subTest(sha=sha):
                result = self.run_cli(sha=sha)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("failed", result.stderr)
                self.assertFalse(self.candidate.exists())

    def test_existing_candidate_is_preserved(self):
        self.candidate.mkdir()
        marker = self.candidate / "keep"
        marker.write_text("do not remove")
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(marker.read_text(), "do not remove")

    def test_invalid_dependency_fails_and_leaves_candidate_for_inspection(self):
        self.toques.write_text("wrong")
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SHA-256 mismatch", result.stderr)
        self.assertTrue(self.candidate.is_dir())

    def test_symlink_dependency_is_rejected(self):
        link = self.base / "link.py"
        link.symlink_to(self.toques)
        result = self.run_cli(toques=link)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("regular file", result.stderr)

    def test_candidate_inside_source_is_rejected(self):
        result = self.run_cli(candidate=self.repo / "candidate")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside", result.stderr)
        self.assertFalse((self.repo / "candidate").exists())


    def test_history_origin_and_fsck_are_complete(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD^", cwd=self.candidate).strip(), self.parent_sha)
        self.assertEqual(self.git("rev-parse", "refs/remotes/origin/main", cwd=self.candidate).strip(), self.sha)
        self.assertEqual(self.git("rev-list", "--count", "HEAD", cwd=self.candidate).strip(), "2")
        self.assertIn("synthetic root", self.git("log", "--format=%s", cwd=self.candidate))
        self.assertIn("history.txt", self.git("diff", "--name-only", "HEAD^", "HEAD", cwd=self.candidate))
        self.git("fsck", "--full", "--strict", cwd=self.candidate)
        refs = self.git("for-each-ref", "--format=%(refname)", cwd=self.candidate).splitlines()
        self.assertEqual(refs, ["refs/remotes/origin/main"])

    def test_sha256_repository_history(self):
        repo = self.base / "source-sha256"
        repo.mkdir()
        init = subprocess.run(["git", "init", "-q", "--object-format=sha256", str(repo)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        if init.returncode:
            self.skipTest("Git does not support sha256 repositories")
        self.git("config", "user.email", "test@example.invalid", cwd=repo)
        self.git("config", "user.name", "Test", cwd=repo)
        (repo / "runtime").mkdir()
        (repo / "runtime/dependencies.json").write_text(json.dumps({
            "dependencies": {"toques.py": {"sha256": hashlib.sha256(self.toques.read_bytes()).hexdigest()}}
        }))
        self.git("add", ".", cwd=repo)
        self.git("commit", "-qm", "root", cwd=repo)
        parent = self.git("rev-parse", "HEAD", cwd=repo).strip()
        (repo / "second").write_text("content")
        self.git("add", "second", cwd=repo)
        self.git("commit", "-qm", "second", cwd=repo)
        self.repo = repo
        self.sha = self.git("rev-parse", "HEAD", cwd=repo).strip()
        self.git("update-ref", "refs/remotes/origin/main", self.sha, cwd=repo)
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD^", cwd=self.candidate).strip(), parent)
        self.git("fsck", "--full", "--strict", cwd=self.candidate)

    def test_descendant_of_origin_is_rejected_before_reservation(self):
        self.git("update-ref", "refs/remotes/origin/main", self.parent_sha)
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("origin/main", result.stderr)
        self.assertFalse(self.candidate.exists())
        self.assertFalse(self.validated_toques.exists())

    def test_origin_change_during_preparation_fails_without_success(self):
        spec = importlib.util.spec_from_file_location("prepare_origin_race_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        real_git = module.git
        changed = []
        def swap(*args, **kwargs):
            if "archive" in args and not changed:
                self.git("update-ref", "refs/remotes/origin/main", self.parent_sha)
                changed.append(True)
            return real_git(*args, **kwargs)
        with mock.patch.object(module, "git", side_effect=swap):
            with self.assertRaisesRegex(module.PreparationError, "origin/main"):
                module.prepare(self.repo, self.sha, self.candidate, self.toques, self.validated_toques)
        self.assertTrue(changed)
        self.assertFalse(self.validated_toques.exists())

    def test_origin_change_at_end_fails_without_success(self):
        spec = importlib.util.spec_from_file_location("prepare_origin_late_race_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        real_stat = module.os.stat
        changed = []
        def swap(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            if Path(path) == self.candidate and self.validated_toques.exists() and not changed:
                self.git("update-ref", "refs/remotes/origin/main", self.parent_sha)
                changed.append(True)
            return result
        with mock.patch.object(module.os, "stat", side_effect=swap):
            with self.assertRaisesRegex(module.PreparationError, "origin/main"):
                module.prepare(self.repo, self.sha, self.candidate, self.toques, self.validated_toques)
        self.assertTrue(changed)

    def test_source_head_can_differ_from_origin(self):
        self.git("checkout", "-qb", "other")
        (self.repo / "other.txt").write_text("other checkout")
        self.git("add", "other.txt")
        self.git("commit", "-qm", "different HEAD")
        self.assertNotEqual(self.git("rev-parse", "HEAD").strip(), self.sha)
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.candidate).strip(), self.sha)
        self.assertEqual(self.validated_toques.name, "toques.py")
        self.assertEqual(self.validated_toques.read_bytes(), self.toques.read_bytes())

    def test_missing_origin_and_unrelated_origin_fail_before_reservation(self):
        self.git("update-ref", "-d", "refs/remotes/origin/main")
        self.assertNotEqual(self.run_cli().returncode, 0)
        self.assertFalse(self.candidate.exists())
        self.git("checkout", "--orphan", "unrelated")
        self.git("rm", "-rf", ".")
        (self.repo / "unrelated").write_text("other root")
        self.git("add", "unrelated")
        self.git("commit", "-qm", "unrelated")
        unrelated = self.git("rev-parse", "HEAD").strip()
        self.git("update-ref", "refs/remotes/origin/main", unrelated)
        self.assertNotEqual(self.run_cli().returncode, 0)
        self.assertFalse(self.candidate.exists())

    def test_missing_parent_object_fails_closed(self):
        object_file = self.repo / ".git/objects" / self.parent_sha[:2] / self.parent_sha[2:]
        object_file.unlink()
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.validated_toques.exists())

    def test_shallow_repository_is_rejected(self):
        (self.repo / ".git/shallow").write_text(self.parent_sha + "\n")
        self.assertNotEqual(self.run_cli().returncode, 0)
        self.assertFalse(self.candidate.exists())

    def test_dependency_destination_in_checkout_or_preexisting_is_rejected(self):
        self.validated_toques = self.candidate / "toques.py"
        self.assertNotEqual(self.run_cli().returncode, 0)
        self.assertFalse(self.candidate.exists())
        self.validated_toques = self.base / "already.py"
        self.validated_toques.write_text("preserve")
        self.assertNotEqual(self.run_cli().returncode, 0)
        self.assertEqual(self.validated_toques.read_text(), "preserve")

    def test_other_ref_objects_are_not_copied(self):
        (self.repo / "extra.txt").write_text("unrelated secret")
        self.git("add", "extra.txt")
        self.git("commit", "-qm", "other ref")
        other = self.git("rev-parse", "HEAD").strip()
        self.git("branch", "extra", other)
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        probe = subprocess.run(["git", "cat-file", "-e", other], cwd=self.candidate,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertNotEqual(probe.returncode, 0)
        self.assertFalse((self.candidate / "extra.txt").exists())

    def test_partial_clone_fails_before_object_access(self):
        self.git("config", "remote.origin.url", "ext::sh -c touch-MUST-NOT-RUN")
        self.git("config", "remote.origin.promisor", "true")
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("partial", result.stderr)
        self.assertFalse(self.candidate.exists())

    def test_alternates_and_config_include_fail_closed(self):
        alternate = self.repo / ".git/objects/info/alternates"
        alternate.parent.mkdir(parents=True, exist_ok=True)
        alternate.write_text("/nonexistent\n")
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.candidate.exists())
        alternate.unlink()
        self.git("config", "include.path", "/nonexistent")
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.candidate.exists())

    def test_symlink_object_store_is_rejected(self):
        (self.repo / ".git/objects/aa").symlink_to(self.base, target_is_directory=True)
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("object store", result.stderr)
        self.assertFalse(self.candidate.exists())

    def test_concurrent_reservation_has_one_winner(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.run_cli(), range(6)))
        self.assertEqual(sum(r.returncode == 0 for r in results), 1,
                         [r.stderr for r in results])
        self.assertEqual((self.candidate / "tracked.txt").read_text(), "committed\n")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.candidate).strip(), self.sha)

    def test_validated_dependency_bytes_are_preserved(self):
        original = self.toques.read_bytes()
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.toques.write_bytes(b"changed after validation")
        self.assertEqual(self.validated_toques.read_bytes(), original)
        self.assertFalse((self.candidate / ".git/validated-toques.py").exists())
        self.assertIn(str(self.validated_toques), result.stdout)

    def test_candidate_replacement_fails_without_writing_replacement(self):
        spec = importlib.util.spec_from_file_location("prepare_race_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        real_git = module.git
        moved = self.base / "reserved-moved"
        def swap(*args, **kwargs):
            if "archive" in args and not moved.exists():
                self.candidate.rename(moved)
                self.candidate.mkdir()
                (self.candidate / "marker").write_text("untouched")
            return real_git(*args, **kwargs)
        with mock.patch.object(module, "git", side_effect=swap):
            with self.assertRaisesRegex(module.PreparationError, "candidate path changed"):
                module.prepare(self.repo, self.sha, self.candidate, self.toques, self.validated_toques)
        self.assertEqual(sorted(p.name for p in self.candidate.iterdir()), ["marker"])
        self.assertEqual((self.candidate / "marker").read_text(), "untouched")

    def test_dependency_swap_to_symlink_is_rejected(self):
        spec = importlib.util.spec_from_file_location("prepare_under_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original_open = module.os.open
        def swapped(path, flags, *args, **kwargs):
            if Path(path) == self.toques:
                self.toques.unlink()
                self.toques.symlink_to(self.base / "secret")
            return original_open(path, flags, *args, **kwargs)
        with mock.patch.object(module.os, "open", side_effect=swapped):
            with self.assertRaises(module.PreparationError):
                module.read_dependency(self.toques, "0" * 64)

    def test_subprocess_output_and_deadline_are_bounded_and_sanitized(self):
        spec = importlib.util.spec_from_file_location("prepare_limits_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        real_popen = subprocess.Popen
        for program in (
            "import sys;sys.stdout.write('x'*100000)",
            "import sys;sys.stderr.write('SECRET'*10000)",
            "import time;time.sleep(5)",
        ):
            with self.subTest(program=program):
                def spawn(*args, **kwargs):
                    return real_popen([sys.executable, "-c", program], **kwargs)
                with mock.patch.object(module.subprocess, "Popen", side_effect=spawn),                      mock.patch.object(module, "GIT_TIMEOUT", 0.2),                      mock.patch.object(module, "MAX_ERROR", 1024):
                    with self.assertRaises(module.PreparationError) as error:
                        module.git("status", limit=1024)
                self.assertNotIn("SECRET", str(error.exception))

    def test_export_ignore_tree_mismatch_fails_closed(self):
        (self.repo / ".gitattributes").write_text("tracked.txt export-ignore\n")
        self.git("add", ".gitattributes")
        self.git("commit", "-qm", "attributes")
        sha = self.git("rev-parse", "HEAD").strip()
        self.git("update-ref", "refs/remotes/origin/main", sha)
        result = self.run_cli(sha=sha)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tree differs", result.stderr)


if __name__ == "__main__":
    unittest.main()
