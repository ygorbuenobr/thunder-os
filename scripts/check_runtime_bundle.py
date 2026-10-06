"""Validate a Thunder OS candidate against its manifest and dependency contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath


DEFAULT_CONTRACT = Path(__file__).resolve().parent.parent / "runtime/dependencies.json"
PANEL_FILES = {
    "_ferramentas/painel.py",
    "_ferramentas/painel_servidor.py",
    "_ferramentas/painel_modelo.html",
}
DEPENDENCY_PATH = "_ferramentas/toques.py"
MANIFEST_PATH = "thunder-os-manifest.json"
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
GIT_SHA_RE = re.compile(r"[0-9a-f]{40,64}\Z")


class BundleError(ValueError):
    pass


def safe_path(name: str) -> bool:
    if (
        not isinstance(name, str)
        or not name
        or "\\" in name
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
    ):
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and all(part not in (".", "..", "") for part in name.split("/"))


def source_path(name: str) -> bool:
    if not safe_path(name):
        return False
    if name in PANEL_FILES:
        return True
    if name in {
        "runtime/systemd/thunder-os-agent-pythonpath.conf",
        "runtime/systemd/thunder-os-agent-hermes-ro.conf",
        "runtime/systemd/thunder-os-connections-live.service",
        "runtime/systemd/thunder-os-connections-live.timer",
        "runtime/systemd/thunder-os-connections-backfill.service",
        "runtime/systemd/thunder-os-connections-backfill.timer",
    }:
        return True
    return (
        name.startswith("_ferramentas/thunder_os/")
        and name.endswith((".py", ".sql"))
        and not {"brain", "data", "secrets"}.intersection(PurePosixPath(name).parts)
    )


def load_contract(path: Path) -> str:
    try:
        contract = json.loads(path.read_text(encoding="utf-8"))
        dependency = contract["dependencies"]["toques.py"]
        if contract["schema_version"] != 1 or dependency["bundle_path"] != DEPENDENCY_PATH:
            raise BundleError("invalid dependency contract")
        digest = dependency["sha256"]
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise BundleError("invalid toques.py SHA256 in dependency contract")
        return digest
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise BundleError(f"cannot load dependency contract: {exc}") from exc


def hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def check_bundle(bundle: Path, contract: Path = DEFAULT_CONTRACT, expected_source_sha: str | None = None) -> dict:
    bundle = bundle.absolute()
    if bundle.is_symlink() or not bundle.is_dir():
        raise BundleError("candidate must be a real directory")
    manifest_file = bundle / MANIFEST_PATH
    if manifest_file.is_symlink() or not manifest_file.is_file():
        raise BundleError("Thunder OS manifest missing or symlinked")
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleError(f"invalid manifest: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise BundleError("unsupported manifest schema")
    source_sha = manifest.get("source_sha")
    if not isinstance(source_sha, str) or not GIT_SHA_RE.fullmatch(source_sha):
        raise BundleError("invalid source SHA")
    if expected_source_sha is not None and source_sha != expected_source_sha:
        raise BundleError("source SHA differs from expected commit")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise BundleError("manifest has no files")
    if not PANEL_FILES.issubset(files) or not any(
        name.startswith("_ferramentas/thunder_os/") for name in files
    ):
        raise BundleError("required panel or Thunder OS source is missing")
    expected_dependency = load_contract(contract)
    if manifest.get("dependency_sha256") != expected_dependency:
        raise BundleError("dependency SHA256 differs from contract")
    for name, metadata in files.items():
        if name != DEPENDENCY_PATH and not source_path(name):
            raise BundleError(f"forbidden bundle path: {name}")
        if not safe_path(name) or not isinstance(metadata, dict):
            raise BundleError(f"invalid bundle entry: {name}")
        digest, size = metadata.get("sha256"), metadata.get("size")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest) or type(size) is not int or size < 0:
            raise BundleError(f"invalid hash or size: {name}")
        if name == DEPENDENCY_PATH and digest != expected_dependency:
            raise BundleError("toques.py hash differs from contract")
        entry = bundle / name
        if any(part.is_symlink() for part in (entry, *entry.parents) if part != bundle and bundle in part.parents):
            raise BundleError(f"symlink in bundle path: {name}")
        if not entry.is_file() or hash_file(entry) != (digest, size):
            raise BundleError(f"hash or size mismatch: {name}")
    actual = set()
    for entry in bundle.rglob("*"):
        if entry.is_symlink():
            raise BundleError(f"symlink in candidate: {entry.relative_to(bundle)}")
        if entry.is_file():
            actual.add(entry.relative_to(bundle).as_posix())
        elif not entry.is_dir():
            raise BundleError(f"unsupported candidate entry: {entry.relative_to(bundle)}")
    if actual != set(files) | {MANIFEST_PATH}:
        raise BundleError("candidate contains missing or unlisted files")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--expected-source-sha")
    args = parser.parse_args()
    try:
        manifest = check_bundle(args.candidate, DEFAULT_CONTRACT, args.expected_source_sha)
    except (BundleError, OSError) as exc:
        print(f"bundle invalid: {exc}", file=sys.stderr)
        return 1
    print(f"bundle valid: {manifest['source_sha']} ({len(manifest['files'])} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
