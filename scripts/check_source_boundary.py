from __future__ import annotations

import subprocess
import sys
from pathlib import PurePosixPath


ALLOWED_FILES = {
    ".specs/features/thunder-os-v1/campaign-execution-confirmation.md",
    "scripts/test_campaign_confirmation_offline.py",
    "scripts/test_campaign_confirmation_panel.cjs",
    "AGENTS.md",
    "README.md",
    ".gitignore",
    ".checks/thunder-os-source.md",
    ".checks/connection-copilot.md",
    ".specs/features/thunder-os-v1/spec.md",
    ".specs/features/thunder-os-v1/context.md",
    ".specs/features/thunder-os-v1/design.md",
    "scripts/check_source_boundary.py",
    "scripts/build_runtime_bundle.py",
    "scripts/check_runtime_bundle.py",
    "scripts/test_runtime_bundle.py",
    "scripts/test_connection_units.py",
    "scripts/provision_claude_social.py",
    "runtime/dependencies.json",
    "runtime/systemd/thunder-os-agent-pythonpath.conf",
    "runtime/systemd/thunder-os-agent-hermes-ro.conf",
    "runtime/systemd/thunder-os-agent-pythonpath-v1.md",
    "runtime/systemd/thunder-os-connections-live.service",
    "runtime/systemd/thunder-os-connections-live.timer",
    "runtime/systemd/thunder-os-connections-backfill.service",
    "runtime/systemd/thunder-os-connections-backfill.timer",
    "_ferramentas/painel.py",
    "_ferramentas/painel_servidor.py",
    "_ferramentas/painel_modelo.html",
}
ALLOWED_TESTS = {
    "test_thunder_os_campaign_registry.py",
    "test_thunder_os_campaign_confirmation.py",
    "test_thunder_os_legacy_campaign_gate.py",
    "test_thunder_os_cadencia_radar_f3b.py",
    "test_thunder_os_cadencia_hermes_f3.py",
    "test_thunder_os_cadencia_hubspot_f1.py",
    "test_thunder_os_connectsafely_quarantine_resolution.py",
    "test_thunder_os_connectsafely_unicode.py",
    "test_thunder_os_connectsafely_panel_wiring.py",
    "test_thunder_os_text_budget.py",
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
    "test_thunder_os_connection_intake.py",
    "test_thunder_os_connection_ledger.py",
    "test_thunder_os_connection_provider.py",
    "test_thunder_os_connection_backfill.py",
    "test_thunder_os_connection_catalog.py",
    "test_thunder_os_connection_company.py",
    "test_thunder_os_connection_runtime.py",
    "test_thunder_os_connection_worker.py",
    "test_thunder_os_connection_panel.py",
    "test_thunder_os_connection_reconcile.py",
    "test_thunder_os_connection_reconcile_runtime.py",
    "test_thunder_os_connection_reconcile_apply.py",
    "test_thunder_os_connection_reconcile_cli.py",
    "test_thunder_os_connectsafely_client.py",
    "test_thunder_os_connectsafely_transport.py",
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
    if path.startswith("_ferramentas/thunder_os/"):
        return "__pycache__" not in parts and path.endswith((".py", ".sql"))
    if path.startswith("_ferramentas/test_thunder_os_"):
        return PurePosixPath(path).name in ALLOWED_TESTS
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
