#!/usr/bin/env python3
"""Prepare a detached, isolated staging checkout without network access."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time


class PreparationError(Exception):
    pass


MAX_OUTPUT = 64 * 1024 * 1024
MAX_ERROR = 16 * 1024
MAX_DEPENDENCY = 16 * 1024 * 1024
GIT_TIMEOUT = 60


def git(*args, input_bytes=None, limit=MAX_OUTPUT, pass_fds=()):
    # No inherited Git configuration, transports, hooks, credentials, or helpers.
    env = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LC_ALL": "C",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
           "GIT_ALLOW_PROTOCOL": "", "GIT_NO_REPLACE_OBJECTS": "1",
           "GIT_NO_LAZY_FETCH": "1", "GIT_TEMPLATE_DIR": os.devnull}
    cmd = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
           "-c", "protocol.allow=never", *map(str, args)]
    # A file avoids deadlock between stdin and the bounded output pipes.
    with tempfile.TemporaryFile() as source:
        if input_bytes is not None:
            source.write(input_bytes)
            source.seek(0)
        try:
            proc = subprocess.Popen(cmd, env=env, stdin=source, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, start_new_session=True, pass_fds=pass_fds)
        except OSError as exc:
            raise PreparationError("git could not start") from exc
        sel = selectors.DefaultSelector()
        out, err = bytearray(), bytearray()
        deadline = time.monotonic() + GIT_TIMEOUT
        try:
            for stream, target, cap in ((proc.stdout, out, limit), (proc.stderr, err, MAX_ERROR)):
                os.set_blocking(stream.fileno(), False)
                sel.register(stream, selectors.EVENT_READ, (target, cap))
            while sel.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PreparationError("git deadline exceeded")
                events = sel.select(remaining)
                if not events:
                    raise PreparationError("git deadline exceeded")
                for key, _ in events:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        sel.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    target, cap = key.data
                    if len(target) + len(chunk) > cap:
                        raise PreparationError("git output limit exceeded")
                    target.extend(chunk)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PreparationError("git deadline exceeded")
            try:
                code = proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                raise PreparationError("git deadline exceeded") from exc
            if code:
                raise PreparationError("git command failed")
            return bytes(out)
        finally:
            sel.close()
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
            for stream in (proc.stdout, proc.stderr):
                stream.close()


def text_git(*args, **kwargs):
    try:
        return git(*args, **kwargs).decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise PreparationError("invalid git output") from exc


def read_dependency(path, digest):
    if not hasattr(os, "O_NOFOLLOW"):
        raise PreparationError("dependency validation requires O_NOFOLLOW")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("toques dependency must be a regular file")
            data = stream.read(MAX_DEPENDENCY + 1)
            after = os.fstat(stream.fileno())
            if len(data) > MAX_DEPENDENCY:
                raise ValueError("toques dependency exceeds size limit")
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("toques dependency changed during read")
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("toques dependency SHA-256 mismatch")
        return data
    except OSError as exc:
        raise PreparationError("dependency validation failed: toques dependency must be a regular file") from exc
    except ValueError as exc:
        raise PreparationError(f"dependency validation failed: {exc}") from exc


def prepare(repo, expected, candidate, toques, validated_toques):
    repo = Path(repo).resolve(strict=True)
    candidate = Path(candidate).absolute()
    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", expected):
        raise PreparationError("expected commit must be a full hexadecimal SHA")
    expected = expected.lower()
    parent = candidate.parent.resolve(strict=True)
    candidate = parent / candidate.name
    if candidate == repo or repo in candidate.parents:
        raise PreparationError("candidate must be outside the source repository")
    validated_toques = Path(validated_toques).absolute()
    if validated_toques.parent.resolve(strict=True) != parent or validated_toques.name == candidate.name:
        raise PreparationError("validated dependency must be a separate file in candidate parent")
    validated_toques = parent / validated_toques.name
    # Require a normal repository with no alternate object stores, partial clone,
    # includes, or promisor packs. Do not invoke an object-reading Git command
    # until these checks pass. Linked worktrees are deliberately unsupported.
    gitdir = repo / ".git"
    if not gitdir.is_dir() or gitdir.is_symlink():
        raise PreparationError("source must be a standalone repository root")
    config = git("config", "--no-includes", "--file", gitdir / "config", "--list", limit=1024 * 1024).lower()
    if re.search(rb"(?m)^(?:include(?:if)?\.|extensions\.partialclone=|remote\..*\.promisor=|remote\..*\.partialclonefilter=)", config):
        raise PreparationError("partial or externally configured repositories are unsupported")
    objects = gitdir / "objects"
    if objects.is_symlink() or (gitdir / "config").is_symlink():
        raise PreparationError("external object or config paths are unsupported")
    if (objects / "info/alternates").exists() or any((objects / "pack").glob("*.promisor")):
        raise PreparationError("external or promisor object stores are unsupported")
    # Git follows symlinks in its object store; those could silently read outside
    # the repository even with network transports disabled.
    for directory, dirs, files in os.walk(objects, followlinks=False):
        if any((Path(directory) / name).is_symlink() for name in dirs + files):
            raise PreparationError("symlinks in object store are unsupported")
    actual = text_git("-C", repo, "rev-parse", "--verify", f"{expected}^{{commit}}", limit=256)
    if actual != expected:
        raise PreparationError("expected SHA does not resolve to the exact commit")
    if text_git("-C", repo, "rev-parse", "--is-shallow-repository", limit=16) != "false":
        raise PreparationError("shallow repositories are unsupported")
    origin = text_git("-C", repo, "rev-parse", "--verify",
                      "refs/remotes/origin/main", limit=256)
    if origin != expected:
        raise PreparationError("expected commit must equal source origin/main")
    tree = text_git("-C", repo, "rev-parse", f"{expected}^{{tree}}", limit=256)
    # pack-objects is given exactly one positive revision and no exclusions,
    # producing the complete reachable graph, not other references.
    pack = git("-C", repo, "pack-objects", "--stdout", "--revs",
               input_bytes=(expected + "\n").encode("ascii"))
    if len(expected) == 64:
        header = f"# v3 git bundle\n@object-format=sha256\n{expected} refs/heads/bundle-source\n\n"
    else:
        header = f"# v2 git bundle\n{expected} refs/heads/bundle-source\n\n"
    bundle = header.encode("ascii") + pack
    # Directory descriptor anchors the atomic reservation to the validated parent.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    parent_fd = os.open(parent, flags)
    try:
        info = os.fstat(parent_fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise PreparationError("candidate parent must be owned by caller and not writable by others")
        try:
            os.mkdir(candidate.name, 0o700, dir_fd=parent_fd)
        except FileExistsError as exc:
            raise PreparationError("candidate already exists") from exc
        # A renamed parent would make the pathname ambiguous; fail closed.
        named = os.stat(parent, follow_symlinks=False)
        if (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino):
            raise PreparationError("candidate parent changed")
        candidate_fd = os.open(candidate.name, flags, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
    try:
        return _populate(repo, expected, candidate, candidate_fd, toques, validated_toques,
                         origin, tree, bundle, pack)
    finally:
        os.close(candidate_fd)


def _populate(repo, expected, candidate, candidate_fd, toques, validated_toques,
              origin, tree, bundle, pack):
    # Keep all writes anchored to the opened directory even if a pathname is
    # renamed. Git children explicitly inherit the same descriptor.
    work = Path(f"/proc/self/fd/{candidate_fd}")
    pass_fd = (candidate_fd,)
    archive = git("-C", repo, "archive", "--format=tar", expected)
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            for item in tar:
                parts = Path(item.name).parts
                if not parts or any(p in (".", "..", ".git") for p in parts) or item.name.startswith("/"):
                    raise PreparationError("unsafe archive path")
                path = work.joinpath(*parts)
                if item.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                elif item.isfile():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    data = tar.extractfile(item).read()
                    with path.open("xb") as stream:
                        stream.write(data)
                    path.chmod(0o755 if item.mode & 0o111 else 0o644)
                else:
                    raise PreparationError("unsupported archive entry")
        objformat = "sha256" if len(expected) == 64 else "sha1"
        git("init", "-q", f"--object-format={objformat}", work, pass_fds=pass_fd)
        # Verify the complete local bundle before importing its pack. No bundle
        # refs are imported; only the explicitly checked origin ref is written.
        with tempfile.TemporaryFile() as bundle_file:
            bundle_file.write(bundle)
            bundle_file.flush()
            git("-C", work, "bundle", "verify", f"/proc/self/fd/{bundle_file.fileno()}",
                pass_fds=pass_fd + (bundle_file.fileno(),))
        git("-C", work, "unpack-objects", input_bytes=pack, pass_fds=pass_fd)
        git("-C", work, "add", "-f", "--all", "--", ".", pass_fds=pass_fd)
        if text_git("-C", work, "write-tree", limit=256, pass_fds=pass_fd) != tree:
            raise PreparationError("extracted tree differs from commit")
        git("-C", work, "update-ref", "--no-deref", "HEAD", expected, pass_fds=pass_fd)
        git("-C", work, "update-ref", "refs/remotes/origin/main", origin, pass_fds=pass_fd)
        if text_git("-C", work, "rev-parse", "--verify", "refs/remotes/origin/main^{commit}",
                    limit=256, pass_fds=pass_fd) != origin:
            raise PreparationError("origin/main mismatch")
        git("-C", work, "fsck", "--full", "--strict", "--no-reflogs", pass_fds=pass_fd)
        contract = json.loads((work / "runtime/dependencies.json").read_text(encoding="utf-8"))
        digest = contract["dependencies"]["toques.py"]["sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid SHA-256")
        validated = read_dependency(toques, digest)
        # The consumer gets a separate explicit path, never data inside .git.
        # The caller-selected parent was already checked for owner and permissions.
        if text_git("-C", repo, "rev-parse", "--verify",
                    "refs/remotes/origin/main", limit=256) != expected:
            raise PreparationError("source origin/main changed")
        try:
            current = os.stat(candidate, follow_symlinks=False)
            opened = os.fstat(candidate_fd)
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise PreparationError("candidate path changed during preparation")
        except OSError as exc:
            raise PreparationError("candidate path changed during preparation") from exc
        parent_fd = os.open(candidate.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(parent_fd)
            if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise PreparationError("candidate parent must be owned by caller and not writable by others")
            fd = os.open(validated_toques.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent_fd)
            with os.fdopen(fd, "wb") as stream:
                stream.write(validated)
                saved = os.fstat(stream.fileno())
            named = os.stat(validated_toques.name, dir_fd=parent_fd, follow_symlinks=False)
            if (saved.st_dev, saved.st_ino) != (named.st_dev, named.st_ino):
                raise PreparationError("validated dependency path changed")
        finally:
            os.close(parent_fd)
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError) as exc:
        raise PreparationError("dependency or tree validation failed") from exc
    try:
        actual = os.stat(candidate, follow_symlinks=False)
        opened = os.fstat(candidate_fd)
        if (actual.st_dev, actual.st_ino) != (opened.st_dev, opened.st_ino):
            raise PreparationError("candidate path changed during preparation")
    except OSError as exc:
        raise PreparationError("candidate path changed during preparation") from exc
    if text_git("-C", repo, "rev-parse", "--verify",
                "refs/remotes/origin/main", limit=256) != expected:
        raise PreparationError("source origin/main changed")
    return candidate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="source repository root")
    parser.add_argument("--expected-commit", required=True, help="full expected commit SHA")
    parser.add_argument("--candidate", required=True, help="nonexistent candidate directory")
    parser.add_argument("--toques", required=True, help="external toques.py regular file")
    parser.add_argument("--validated-toques", required=True,
                        help="new validated dependency file beside the candidate")
    args = parser.parse_args()
    try:
        candidate = prepare(args.repo, args.expected_commit, args.candidate, args.toques, args.validated_toques)
    except (PreparationError, OSError):
        exc = sys.exc_info()[1]
        message = str(exc) if isinstance(exc, PreparationError) else "filesystem error"
        print(f"preparation failed: {message}", file=sys.stderr)
        return 1
    print(f"prepared detached candidate: {candidate} at {args.expected_commit.lower()}")
    print(f"validated dependency: {Path(args.validated_toques).absolute()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
