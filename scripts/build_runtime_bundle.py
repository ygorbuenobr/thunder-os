"""Build a local, immutable Thunder OS candidate from a clean Git commit."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from check_runtime_bundle import (
    BundleError,
    DEPENDENCY_PATH,
    MANIFEST_PATH,
    PANEL_FILES,
    check_bundle,
    load_contract,
    source_path,
)


DEFAULT_REPO = Path(__file__).resolve().parent.parent


def git(repo: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True
        ).stdout
    except subprocess.CalledProcessError as exc:
        raise BundleError(f"git {' '.join(args[:2])} failed") from exc


def committed_sources(repo: Path) -> tuple[str, dict[str, bytes]]:
    root = Path(os.fsdecode(git(repo, "rev-parse", "--show-toplevel")).strip()).resolve()
    if root != repo.resolve():
        raise BundleError("repository path is not the Git root")
    if git(repo, "status", "--porcelain=v1", "--untracked-files=all").strip():
        raise BundleError("Git worktree is dirty; commit all source before building")
    source_sha = git(repo, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
    entries = git(repo, "ls-tree", "-r", "-z", "HEAD").split(b"\0")
    sources = {}
    for entry in entries:
        if not entry:
            continue
        info, raw_name = entry.split(b"\t", 1)
        name = raw_name.decode("utf-8")
        if not source_path(name):
            continue
        mode, kind, object_sha = info.decode().split(" ")
        if mode not in ("100644", "100755") or kind != "blob":
            raise BundleError(f"source is not a regular Git blob: {name}")
        sources[name] = git(repo, "cat-file", "blob", object_sha)
    if not PANEL_FILES.issubset(sources) or not any(
        name.startswith("_ferramentas/thunder_os/") for name in sources
    ):
        raise BundleError("commit lacks required panel or Thunder OS source")
    return source_sha, sources


def build_bundle(repo: Path, output: Path, toques: Path, contract: Path) -> dict:
    repo = repo.resolve()
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise BundleError("candidate output already exists")
    resolved_output = output.resolve(strict=False)
    if resolved_output == repo or repo in resolved_output.parents:
        raise BundleError("candidate output must be outside the source repository")
    source_sha, sources = committed_sources(repo)
    expected_hash = load_contract(contract)
    if toques.name != "toques.py" or toques.is_symlink() or not toques.is_file():
        raise BundleError("explicit external toques.py must be a regular file")
    resolved_toques = toques.resolve()
    if resolved_toques == repo or repo in resolved_toques.parents:
        raise BundleError("toques.py must be supplied from outside the repository")
    dependency_data = toques.read_bytes()
    if hashlib.sha256(dependency_data).hexdigest() != expected_hash:
        raise BundleError("external toques.py SHA256 mismatch")
    if not output.parent.is_dir():
        raise BundleError("candidate staging parent does not exist")
    output.mkdir()
    files = {}
    for name, data in sorted(sources.items()):
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    target = output / DEPENDENCY_PATH
    target.write_bytes(dependency_data)
    files[DEPENDENCY_PATH] = {"sha256": expected_hash, "size": len(dependency_data)}
    manifest = {
        "schema_version": 1,
        "source_sha": source_sha,
        "dependency_sha256": expected_hash,
        "files": files,
    }
    (output / MANIFEST_PATH).write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    check_bundle(output, contract, source_sha)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new candidate directory in staging")
    parser.add_argument("--toques", type=Path, required=True, help="external toques.py file")
    args = parser.parse_args()
    try:
        manifest = build_bundle(DEFAULT_REPO, args.output, args.toques, DEFAULT_REPO / "runtime/dependencies.json")
    except (BundleError, OSError, UnicodeError) as exc:
        print(f"bundle build refused: {exc}", file=sys.stderr)
        return 1
    print(f"candidate built and verified: {args.output} ({manifest['source_sha']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
