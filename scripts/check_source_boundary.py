from __future__ import annotations

import subprocess
import sys
from pathlib import PurePosixPath


ALLOWED_FILES = {
    "AGENTS.md",
    "README.md",
    ".gitignore",
    ".checks/thunder-os-source.md",
    ".specs/features/thunder-os-v1/spec.md",
    ".specs/features/thunder-os-v1/context.md",
    ".specs/features/thunder-os-v1/design.md",
    "scripts/check_source_boundary.py",
}
ALLOWED_TESTS = {
    "test_thunder_os_models.py",
    "test_thunder_os_db_integracao.py",
    "test_thunder_os_approvals.py",
    "test_thunder_os_cadence.py",
    "test_thunder_os_research.py",
    "test_thunder_os_research_producer.py",
    "test_thunder_os_knowledge.py",
    "test_thunder_os_learning.py",
    "test_thunder_os_hermes_dependency.py",
    "test_thunder_os_hermes_auth_ro.py",
}
FORBIDDEN_PARTS = {
    "brain",
    "contas",
    "fila-envios.md",
    "fila-linkedin.md",
    "fila-convites.md",
    "fila-comentarios.md",
    "_log",
    "secrets",
    "credentials",
}


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], check=True, capture_output=True
    )
    return [entry.decode() for entry in result.stdout.split(b"\0") if entry]


def allowed(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if any(part.lower() in FORBIDDEN_PARTS for part in parts):
        return False
    if path in ALLOWED_FILES:
        return True
    if path.startswith("_ferramentas/thunder_os/") and path.endswith(".py"):
        return "__pycache__" not in parts
    if path.startswith("_ferramentas/test_thunder_os_"):
        return PurePosixPath(path).name in ALLOWED_TESTS
    if path.startswith("_ferramentas/thunder_os/"):
        return path.endswith(".py")
    return False


def main() -> int:
    try:
        paths = tracked_files()
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"cannot inspect git index: {exc}", file=sys.stderr)
        return 2
    rejected = [path for path in paths if not allowed(path)]
    if rejected:
        print("forbidden or unclassified tracked paths:", file=sys.stderr)
        print("\n".join(rejected), file=sys.stderr)
        return 1
    required_roots = ("_ferramentas/thunder_os/", "_ferramentas/test_thunder_os_")
    if not all(any(path.startswith(root) for path in paths) for root in required_roots):
        print("expected source package and selected tests are missing", file=sys.stderr)
        return 1
    print(f"source boundary ok: {len(paths)} tracked paths")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
