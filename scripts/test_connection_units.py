"""Static checks for candidate units; no service is installed or started here.

Rollout prerequisites: verify the active release and exact inbound config,
including schema version, repository path, account identity, and credential
permissions. Snapshot the repository and config, retain a known-good release,
and rehearse restore before enabling either timer. Confirm database schema
compatibility and that the repository is writable inside the service sandbox.
After installation, verify unit syntax with systemd-analyze on the target,
then run each service once in isolation and check GET-only provider activity,
checkpoint/readback, process exit, and timer state before enabling timers.

Rollback: disable both new timers, stop both new services, restore the prior
unit/release selection, and verify the previous journey. Restore the database
snapshot only after checking forward/backward schema compatibility. Do not
stop or modify the existing inbound monitor as part of this local change.
"""

from __future__ import annotations

import configparser
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_runtime_bundle import build_bundle


UNITS = Path(__file__).resolve().parent.parent / "runtime/systemd"
ACTIVE = "/opt/thunder-os/current"
CONFIG = "/var/lib/thunder-os-inbound-monitor/config.json"
BRAIN = "/root/.hermes/profiles/ygor-personal/brain"
LOCK = "/var/lib/thunder-os-inbound-monitor/connections-intake.lock"


def read_unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    with (UNITS / name).open(encoding="utf-8") as stream:
        parser.read_file(stream)
    return parser


class ConnectionUnitTests(unittest.TestCase):
    def test_agent_candidate_import_uses_both_pythonpath_entries(self) -> None:
        contract_text = (UNITS / "thunder-os-agent-pythonpath-v1.md").read_text(encoding="utf-8")
        dropin_text = (UNITS / "thunder-os-agent-pythonpath.conf").read_text(encoding="utf-8")
        self.assertIn("# Thunder OS agent Python path contract v1", contract_text)
        self.assertIn(
            "Environment=PYTHONPATH=/opt/thunder-os/agent-current:"
            "/opt/thunder-os/agent-current/_ferramentas", dropin_text,
        )
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            repo = root / "repo"
            repo.mkdir()
            external = root / "external/toques.py"
            external.parent.mkdir()
            external.write_text("class ErroBanco(Exception):\n    pass\n", encoding="utf-8")
            expected_hash = hashlib.sha256(external.read_bytes()).hexdigest()
            files = {
                "_ferramentas/painel.py": b"# fixture panel\n",
                "_ferramentas/painel_servidor.py": b"# fixture server\n",
                "_ferramentas/painel_modelo.html": b"<html></html>\n",
                "_ferramentas/thunder_os/__init__.py": b"",
                "_ferramentas/thunder_os/touch_ledger.py":
                    (UNITS.parent.parent / "_ferramentas/thunder_os/touch_ledger.py").read_bytes(),
                "runtime/dependencies.json": json.dumps({
                    "schema_version": 1,
                    "dependencies": {"toques.py": {
                        "bundle_path": "_ferramentas/toques.py", "sha256": expected_hash,
                    }},
                }).encode(),
            }
            for name, content in files.items():
                path = repo / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            for args in (
                ("init", "-q"),
                ("config", "user.email", "unit-test@example.invalid"),
                ("config", "user.name", "Unit Test"),
                ("add", "."),
                ("commit", "-qm", "candidate fixture"),
            ):
                subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
            candidate = root / "candidate"
            build_bundle(repo, candidate, external, repo / "runtime/dependencies.json")
            self.assertTrue((candidate / "_ferramentas/toques.py").is_file())
            env = {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(candidate),
            }
            root_only = subprocess.run(
                [sys.executable, "-c", "import toques"], cwd=candidate, env=env,
                capture_output=True, text=True, timeout=10,
            )
            self.assertNotEqual(0, root_only.returncode)
            self.assertIn("ModuleNotFoundError", root_only.stderr)
            env["PYTHONPATH"] = str(candidate) + os.pathsep + str(candidate / "_ferramentas")
            combined = subprocess.run(
                [sys.executable, "-c",
                 "import toques; from _ferramentas.thunder_os import touch_ledger; "
                 "assert touch_ledger.toques is toques"],
                cwd=candidate, env=env, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(0, combined.returncode, combined.stderr)

    def test_entrypoint_imports_and_exits_with_isolated_toques_stub(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            Path(scratch, "toques.py").write_text(
                "class ErroBanco(Exception):\n    pass\n", encoding="utf-8"
            )
            env = {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": scratch + os.pathsep + str(UNITS.parent.parent / "_ferramentas"),
            }
            result = subprocess.run(
                [sys.executable, "-m", "thunder_os.connection_runtime", "--help"],
                cwd=UNITS.parent.parent, env=env, capture_output=True, text=True,
                timeout=10,
            )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("--config", result.stdout)
        self.assertIn("--brain", result.stdout)
        self.assertIn("--mode", result.stdout)

    def test_services_call_only_connection_runtime_with_shared_lock(self) -> None:
        commands = {}
        for mode in ("live", "backfill"):
            with self.subTest(mode=mode):
                unit = read_unit(f"thunder-os-connections-{mode}.service")
                service = unit["Service"]
                self.assertEqual("oneshot", service["Type"])
                self.assertEqual("root", service["User"])
                self.assertEqual(ACTIVE, service["WorkingDirectory"])
                self.assertEqual("no", service["Restart"])
                self.assertEqual("30s", service["TimeoutStopSec"])
                self.assertEqual("0077", service["UMask"])
                command = shlex.split(service["ExecStart"])
                self.assertEqual(
                    ["/usr/bin/flock", "--exclusive", "--nonblock",
                     "--conflict-exit-code", "75", LOCK,
                     "/usr/bin/python3", "-m", "thunder_os.connection_runtime",
                     "--config", CONFIG, "--brain", BRAIN, "--mode", mode],
                    command,
                )
                commands[mode] = command
                self.assertNotIn("ExecStartPre", service)
                self.assertNotIn("ExecStartPost", service)
                self.assertNotIn("EnvironmentFile", service)
                self.assertNotIn("THUNDER_EXTERNAL_EFFECTS_ENABLED=true", service["ExecStart"])
        self.assertEqual(commands["live"][5], commands["backfill"][5])

    def test_services_protect_source_config_and_brain(self) -> None:
        for mode, timeout in (("live", "300s"), ("backfill", "900s")):
            with self.subTest(mode=mode):
                raw = (UNITS / f"thunder-os-connections-{mode}.service").read_text(encoding="utf-8")
                service = read_unit(f"thunder-os-connections-{mode}.service")["Service"]
                self.assertIn("Environment=PYTHONPATH=" + ACTIVE + "/_ferramentas\n", raw)
                self.assertIn("Environment=THUNDER_EXTERNAL_EFFECTS_ENABLED=false\n", raw)
                self.assertEqual("strict", service["ProtectSystem"])
                self.assertEqual("read-only", service["ProtectHome"])
                self.assertEqual("true", service["NoNewPrivileges"])
                self.assertEqual(
                    ["/var/lib/thunder-os-inbound-monitor",
                     "/root/.hermes/profiles/ygor-personal/painel-dados"],
                    [line.split("=", 1)[1] for line in raw.splitlines()
                     if line.startswith("ReadWritePaths=")],
                )
                self.assertEqual(timeout, service["TimeoutStartSec"])
                self.assertEqual(
                    [ACTIVE, CONFIG, BRAIN],
                    [line.split("=", 1)[1] for line in raw.splitlines()
                     if line.startswith("ReadOnlyPaths=")],
                )

    def test_live_timer_is_frequent_and_backfill_is_daily(self) -> None:
        live = read_unit("thunder-os-connections-live.timer")["Timer"]
        backfill = read_unit("thunder-os-connections-backfill.timer")["Timer"]
        self.assertEqual("thunder-os-connections-live.service", live["Unit"])
        self.assertEqual("*:0/10", live["OnCalendar"])
        self.assertNotIn("OnUnitInactiveSec", live)
        self.assertEqual("thunder-os-connections-backfill.service", backfill["Unit"])
        self.assertEqual("daily", backfill["OnCalendar"])
        self.assertEqual("true", backfill["Persistent"])
        self.assertNotIn("OnUnitInactiveSec", backfill)
        for mode in ("live", "backfill"):
            unit = read_unit(f"thunder-os-connections-{mode}.timer")
            self.assertEqual("timers.target", unit["Install"]["WantedBy"])

    def test_rollout_notes_require_snapshot_readback_and_rollback(self) -> None:
        notes = __doc__ or ""
        for requirement in (
            "Snapshot the repository and config", "known-good release",
            "systemd-analyze", "GET-only provider activity", "checkpoint/readback",
            "disable both new timers", "schema compatibility",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, notes)


if __name__ == "__main__":
    unittest.main()
