from __future__ import annotations

import subprocess
import sys
from pathlib import PurePosixPath


ALLOWED_FILES = {
    "_ferramentas/test_campaign_sheet_support.py",
    "scripts/test_campaign_sheet_panel.cjs",
    "scripts/test_today_panel.cjs",
    "scripts/test_journey_panel.cjs",
    "requirements-test.txt",
    "_ferramentas/chief_briefing.py",
    "_ferramentas/chief_briefing_runner.py",
    "_ferramentas/chief_read_snapshot.py",
    "_ferramentas/chief_briefing_cards.py",
    "_ferramentas/seqd_mail_readonly.py",
    "_ferramentas/seqd_mail_readonly.cjs",
    "_ferramentas/test_chief_briefing.py",
    "_ferramentas/test_chief_briefing_runner.py",
    "_ferramentas/test_chief_read_snapshot.py",
    "_ferramentas/test_chief_briefing_cards.py",
    "runtime/systemd/thunder-os-agent-email-readonly.conf",
    "scripts/provision_instantly_ingress.py",
    "scripts/mexico_e1_runner.cjs",
    "scripts/mexico_toques_bridge.py",
    "scripts/recover_mexico_runner.cjs",
    "scripts/test_mexico_e1_runner.cjs",
    "docs/branches.md",
    "docs/task3-membership.md",
    "docs/external-email-integrations.md",
    "docs/task4-reply-control.md",
    "docs/task5-linkedin-preparation.md",
    "docs/task6-campaign-sources.md",
    "docs/task7-hermes-verification.md",
    "docs/eval-trajectory.md",
    "_ferramentas/test_briefing_cards_ui.cjs",
    "_ferramentas/test_painel_cadencias.py",
    "_ferramentas/test_painel_cadencias_ui.cjs",
    ".checks/panel-pending-discard.md",
    "scripts/test_panel_discard.cjs",
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
    ".specs/features/connection-intake-provider-compat/plan.md",
    ".specs/features/connection-intake-provider-compat/checks.md",
    ".specs/features/connection-intake-provider-compat/verification.md",
    "scripts/check_source_boundary.py",
    "scripts/build_runtime_bundle.py",
    "scripts/check_runtime_bundle.py",
    "scripts/test_runtime_bundle.py",
    "scripts/test_connection_units.py",
    "scripts/provision_claude_social.py",
    "scripts/agenttrail_mac_publish.py",
    "scripts/provision_agenttrail_ingress.py",
    "runtime/dependencies.json",
    "runtime/systemd/thunder-os-agent-pythonpath.conf",
    "runtime/systemd/thunder-os-agent-hermes-ro.conf",
    "runtime/systemd/thunder-os-agent-pythonpath-v1.md",
    "runtime/systemd/thunder-os-connections-live.service",
    "runtime/systemd/thunder-os-connections-live.timer",
    "runtime/systemd/thunder-os-connections-backfill.service",
    "runtime/systemd/thunder-os-connections-backfill.timer",
    "runtime/systemd/thunder-os-agenttrail.service",
    "runtime/launchd/com.thunder.os-agenttrail.plist",
    "runtime/launchd/com.thunder.os-agenttrail-tunnel.plist",
    "runtime/launchd/com.thunder.os-agenttrail-publish.plist",
    "_ferramentas/painel.py",
    "_ferramentas/painel_servidor.py",
    "_ferramentas/painel_modelo.html",
}
ALLOWED_TESTS = {
    "test_thunder_os_journey_http.py",
    "test_thunder_os_journey_readonly.py",
    "test_thunder_os_account_cadence_readonly.py",
    "test_thunder_os_step_approval.py",
    "test_thunder_os_today_readonly.py",
    "test_thunder_os_email_context.py",
    "test_thunder_os_email_readback.py",
    "test_thunder_os_effect_reconciliation.py",
    "test_thunder_os_instantly_reader.py",
    "test_thunder_os_instantly_ingress.py",
    "test_thunder_os_eval_trajectory.py",
    "test_thunder_os_campaign_scoreboard.py",
    "test_thunder_os_panel_verification.py",
    "test_thunder_os_campaign_sources.py",
    "test_thunder_os_linkedin_preparation.py",
    "test_thunder_os_reply_control.py",
    "test_thunder_os_external_email_import.py",
    "test_thunder_os_apollo_readonly.py",
    "test_thunder_os_instantly_webhook.py",
    "test_thunder_os_campaign_membership.py",
    "test_thunder_os_cadence_contact_campaign.py",
    "test_thunder_os_mexico_runner.py",
    "test_thunder_os_cadence_evidence.py",
    "test_thunder_os_cadence_readonly.py",
    "test_thunder_os_panel_discard.py",
    "test_thunder_os_campaign_registry.py",
    "test_thunder_os_campaign_sheet.py",
    "test_thunder_os_operation_campaign.py",
    "test_thunder_os_migrated_operations.py",
    "test_thunder_os_campaign_confirmation.py",
    "test_thunder_os_legacy_campaign_gate.py",
    "test_thunder_os_cadencia_radar_f3b.py",
    "test_thunder_os_cadencia_hermes_f3.py",
    "test_thunder_os_cadencia_hubspot_f1.py",
    "test_thunder_os_agenttrail_observer.py",
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
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], check=True, capture_output=True
    )
    return [entry.decode() for entry in result.stdout.split(b"\0") if entry]


def allowed(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if any(part.lower() in FORBIDDEN_PARTS for part in parts):
        return False
    if path in ALLOWED_FILES:
        return True
    if path.startswith('_ferramentas/seqd_bridge/'):
        return PurePosixPath(path).name in {
            'bridge.cjs','native.cjs','server.cjs','contract.cjs','journal.cjs','quota.cjs',
            'bridge.test.cjs','readonly.test.cjs','context.test.cjs','process-fixture.cjs',
            'contract-vector.json',
        } and len(parts)==3
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
    print(f"source boundary ok: {len(paths)} tracked and unignored source paths")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
