"""Offline contract tests for the daily mission metadata collector."""

import ast
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import daily_mission_signals as signals


class DailyMissionSignalsTests(unittest.TestCase):
    def setUp(self):
        mock = patch("daily_mission_signals._read_journal", return_value=b"")
        mock.start()
        self.addCleanup(mock.stop)

    @patch("daily_mission_signals.os.path.isdir", return_value=True)
    @patch("daily_mission_signals.os.readlink")
    @patch("daily_mission_signals._read_bounded")
    def test_success_reports_only_requested_metadata_and_read_commands(self, run, readlink, isdir):
        run.return_value = (b"LoadState=loaded\nActiveState=active\nResult=success\n"
                            b"Environment=private-value\n")
        readlink.side_effect = ["/opt/thunder-os/releases/" + "a" * 40, "releases/" + "b" * 40]
        result = signals.collect_signals()
        self.assertTrue(result["timestamp"].endswith("+00:00"))
        self.assertEqual([item["name"] for item in result["units"]], list(signals.UNITS))
        self.assertTrue(all(item["state"] == "active" and item["result"] == "success"
                            for item in result["units"]))
        self.assertEqual(result["releases"], [
            {"name": "current", "release": "a" * 40},
            {"name": "agent-current", "release": "b" * 40},
        ])
        self.assertNotIn("private", json.dumps(result))
        self.assertEqual(run.call_count, 4)
        for call, name in zip(run.call_args_list, signals.UNITS):
            self.assertEqual(call.args[0], ["/usr/bin/systemctl", "show", name,
                                           "--no-pager", "--property=LoadState,ActiveState,Result"])
            self.assertEqual(call.args[1:], (3, signals.SYSTEMCTL_MAX_BYTES))
        self.assertEqual([call.args[0] for call in readlink.call_args_list], list(signals.RELEASE_LINKS))

    @patch("daily_mission_signals.os.readlink", side_effect=FileNotFoundError)
    @patch("daily_mission_signals._read_bounded")
    def test_errors_timeout_and_missing_paths_are_unknown(self, run, readlink):
        run.side_effect = [subprocess.TimeoutExpired("systemctl", 3),
                           OSError("private"),
                           ValueError("private"),
                           b"LoadState=not-found\nActiveState=inactive\n"]
        result = signals.collect_signals()
        self.assertTrue(all(item["state"] == item["result"] == "unknown" for item in result["units"]))
        self.assertTrue(all(item["release"] == "unknown" for item in result["releases"]))
        self.assertNotIn("private", json.dumps(result))

    @patch("daily_mission_signals.os.readlink", return_value="/opt/thunder-os/releases/release")
    @patch("daily_mission_signals._read_bounded")
    def test_incomplete_or_invalid_fields_are_unknown_and_main_prints_json(self, run, readlink):
        run.return_value = b"LoadState=loaded\nActiveState=active\nResult=private value\n"
        output = io.StringIO()
        with redirect_stdout(output):
            signals.main()
        parsed = json.loads(output.getvalue())
        self.assertTrue(all(item["state"] == "active" and item["result"] == "unknown"
                            for item in parsed["units"]))
        self.assertNotIn("private value", output.getvalue())


    @patch("daily_mission_signals._read_bounded", return_value=b"LoadState=loaded\nActiveState=private-secret\nResult=private-secret\n")
    def test_private_secret_states_are_unknown(self, read):
        result = signals._unit_signal(signals.UNITS[0])
        self.assertEqual((result["state"], result["result"]), ("unknown", "unknown"))
        self.assertNotIn("private-secret", json.dumps(result))

    @patch("daily_mission_signals.os.readlink")
    def test_release_requires_canonical_directory_and_sha(self, readlink):
        for target in ("/opt/thunder-os/releases/private-secret", "/tmp/" + "a" * 40,
                       "/opt/thunder-os/releases/nested/" + "a" * 40,
                       "releases/" + "g" * 40):
            with self.subTest(target=target):
                readlink.return_value = target
                result = signals._release_signal("/opt/thunder-os/current")
                self.assertEqual(result["release"], "unknown")
                self.assertNotIn("private-secret", json.dumps(result))

    def test_release_target_must_be_an_existing_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            releases = root / "releases"
            releases.mkdir()
            link = root / "current"
            target = releases / ("a" * 40)
            link.symlink_to(target)
            with patch.object(signals, "RELEASE_ROOT", str(releases)):
                self.assertEqual(signals._release_signal(str(link))["release"], "unknown")
                target.touch()
                self.assertEqual(signals._release_signal(str(link))["release"], "unknown")
                target.unlink()
                target.mkdir()
                self.assertEqual(signals._release_signal(str(link))["release"], "a" * 40)

    def test_systemctl_voluminous_subprocess_is_bounded_and_reaped(self):
        original = subprocess.Popen
        children = []
        def start(command, **kwargs):
            self.assertEqual(command[0], "/usr/bin/systemctl")
            self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
            child = original([sys.executable, "-c", "import sys; sys.stdout.write('x'*200000); sys.stdout.flush()"], **kwargs)
            children.append(child)
            return child
        with patch("daily_mission_signals.subprocess.Popen", side_effect=start), \
             patch.object(signals, "SYSTEMCTL_MAX_BYTES", 1024):
            result = signals._unit_signal(signals.UNITS[0])
        self.assertEqual((result["state"], result["result"]), ("unknown", "unknown"))
        self.assertIsNotNone(children[0].poll())


class JournalHistoryTests(unittest.TestCase):
    @staticmethod
    def entry(message):
        return json.dumps({"MESSAGE": message, "_SYSTEMD_UNIT": "private-id"}).encode() + b"\n"

    @patch("daily_mission_signals._read_journal")
    def test_json_allowlist_and_private_data(self, read):
        read.return_value = (
            self.entry(json.dumps({"status": "ok", "mode": "live", "observed": 7,
                                   "research": 2, "jobs_ensured": 1,
                                   "name": "PRIVATE_NAME", "urn": "PRIVATE_URN"}))
            + self.entry(json.dumps({"status": "blocked", "error_code": "provider_timeout",
                                      "reason": "duplicate_profile"}))
            + self.entry(json.dumps({"status": "ok", "mode": "PRIVATE_MODE",
                                      "reason": "PRIVATE_REASON", "error_code": "PRIVATE_CODE",
                                      "observed": True, "research": -1, "jobs_ensured": 10**100}))
            + self.entry("PRIVATE_TEXT")
            + self.entry(json.dumps({"status": ["PRIVATE_LIST"]}))
            + b"invalid PRIVATE_BYTES\n"
        )
        result = signals._journal_signal(signals.JOURNAL_UNITS[0])
        self.assertEqual((result["status"], result["coverage"], result["window_hours"]),
                         ("ok", "partial", 24))
        self.assertEqual(result["entries_scanned"], 6)
        self.assertEqual(len(result["events"]), 3)
        self.assertEqual(result["events"][0]["observed"], 7)
        self.assertEqual(result["events"][1]["error_code"], "provider_timeout")
        self.assertEqual(result["events"][1]["reason"], "duplicate_profile")
        self.assertEqual(result["events"][2]["mode"], "unknown")
        self.assertEqual(result["events"][2]["observed"], "unknown")
        self.assertEqual(result["events"][2]["research"], "unknown")
        self.assertEqual(result["events"][2]["jobs_ensured"], "unknown")
        self.assertNotIn("PRIVATE", json.dumps(result))

    @patch("daily_mission_signals._read_journal")
    def test_each_intake_and_backfill_error_code_survives_in_event(self, read):
        codes = (
            "connection_profile_invalid", "connection_order_invalid",
            "connection_date_invalid", "connection_window_invalid",
            "connection_page_invalid", "connection_row_invalid",
            "connection_cursor_invalid", "connection_window_incomplete",
            "activation_date_invalid", "connection_account_ambiguous",
            "backfill_configuration_invalid", "backfill_identity_invalid",
            "backfill_anchor_missing", "backfill_window_incomplete",
        )
        for code in codes:
            with self.subTest(code=code):
                read.return_value = self.entry(json.dumps({"status": "blocked", "error_code": code}))
                events = signals._journal_signal(signals.JOURNAL_UNITS[0])["events"]
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["error_code"], code)

    def test_all_static_runtime_producers_are_allowlisted(self):
        # Include _require and the provider HTTP status map; dynamic values are never trusted.
        root = Path(__file__).resolve().parents[1] / "_ferramentas" / "thunder_os"
        producers = ("connection_runtime", "connection_provider", "connection_catalog",
                     "connection_ledger", "connection_intake", "connection_backfill",
                     "connection_company")
        errors = {"ConnectionRuntimeError", "ConnectionProviderError", "ConnectionIntakeError"}
        found = set()
        for module in producers:
            tree = ast.parse((root / (module + ".py")).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                name = node.func.id
                if name not in errors and not (module == "connection_provider" and name == "_require"):
                    continue
                index = 1 if name == "_require" else 0
                self.assertGreater(len(node.args), index, (module, node.lineno))
                arg = node.args[index]
                if isinstance(arg, ast.Name):
                    # _require forwards its argument; _get selects from a fixed map.
                    self.assertEqual((module, name, arg.id),
                                     ("connection_provider", "ConnectionProviderError", "code"))
                    continue
                self.assertIsInstance(arg, (ast.Constant, ast.IfExp), (module, node.lineno))
                literals = {item.value for item in ast.walk(arg)
                            if isinstance(item, ast.Constant) and isinstance(item.value, str)}
                self.assertTrue(literals, (module, node.lineno))
                found.update(literals)
            if module == "connection_provider":
                maps = [node.value for node in ast.walk(tree)
                        if isinstance(node, ast.Assign) and len(node.targets) == 1
                        and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "code"
                        and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
                        and node.value.func.attr == "get" and isinstance(node.value.func.value, ast.Dict)]
                self.assertEqual(len(maps), 1)
                found.update(ast.literal_eval(value) for value in maps[0].func.value.values)
                self.assertEqual(len(maps[0].args), 2)
                found.add(ast.literal_eval(maps[0].args[1]))
        self.assertFalse(found - signals._ALLOWED["error_code"],
                         "Missing static codes: " + repr(sorted(found - signals._ALLOWED["error_code"])))
        self.assertTrue({"provider_unauthorized", "provider_rate_limited",
                         "account_catalog_unavailable", "connection_poll_conflict"} <= found)
        with patch("daily_mission_signals._read_journal") as read:
            for code in sorted(found):
                with self.subTest(code=code):
                    read.return_value = self.entry(json.dumps({"status": "blocked", "error_code": code,
                                                               "private": "PRIVATE_SECRET"}))
                    result = signals._journal_signal(signals.JOURNAL_UNITS[0])
                    self.assertEqual(result["events"][0]["error_code"], code)
                    self.assertNotIn("PRIVATE_SECRET", json.dumps(result))
            for value in ("PRIVATE_SECRET", "provider_timeout PRIVATE_SECRET", ["provider_timeout"],
                          {"code": "provider_timeout"}, None):
                read.return_value = self.entry(json.dumps({"status": "blocked", "error_code": value}))
                result = signals._journal_signal(signals.JOURNAL_UNITS[0])
                self.assertEqual(result["events"][0]["error_code"], "unknown")
                self.assertNotIn("PRIVATE_SECRET", json.dumps(result))

    @patch("daily_mission_signals._read_journal")
    def test_failure_is_unknown_and_excess_lines_are_truncated(self, read):
        for failure, code in ((subprocess.TimeoutExpired("private", 3), "journal_timeout"),
                              (OSError("PRIVATE"), "journal_unavailable"),
                              (ValueError("PRIVATE"), "journal_unavailable"),
                              (ValueError("volume_exceeded"), "journal_volume_exceeded")):
            read.side_effect = failure
            result = signals._journal_signal(signals.JOURNAL_UNITS[0])
            self.assertEqual((result["status"], result["coverage"], result["events"]),
                             ("unknown", "unknown", None))
            self.assertEqual(result["error_code"], code)
            self.assertNotIn("PRIVATE", json.dumps(result))
        read.side_effect = None
        read.return_value = self.entry("{}") * (signals.JOURNAL_MAX_ENTRIES + 1)
        result = signals._journal_signal(signals.JOURNAL_UNITS[0])
        self.assertEqual(result["coverage"], "partial")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["entries_scanned"], 500)

    def test_real_subprocess_timeout_and_byte_limit_offline(self):
        original = subprocess.Popen
        for program in ("import time; time.sleep(10)",
                        "import sys; sys.stdout.write('x'*200000); sys.stdout.flush()"):
            children = []
            def start(command, **kwargs):
                self.assertEqual(command[0], "/usr/bin/journalctl")
                self.assertIn("--since=-24 hours", command)
                self.assertIn("--lines=501", command)
                child = original([sys.executable, "-c", program], **kwargs)
                children.append(child)
                return child
            with self.subTest(program=program), patch("daily_mission_signals.subprocess.Popen", side_effect=start), \
                 patch.object(signals, "JOURNAL_TIMEOUT_SECONDS", 0.1), \
                 patch.object(signals, "JOURNAL_MAX_BYTES", 1024):
                result = signals._journal_signal(signals.JOURNAL_UNITS[0])
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(result["coverage"], "unknown")
                self.assertEqual(result["error_code"], "journal_timeout" if "sleep" in program else "journal_volume_exceeded")
                self.assertIsNotNone(children[0].poll())

    @patch("daily_mission_signals._read_journal", return_value=b"")
    def test_both_services_have_separate_sources(self, read):
        for name in signals.JOURNAL_UNITS:
            result = signals._journal_signal(name)
            self.assertEqual(result["unit"], name)
            self.assertEqual(result["events"], [])
        self.assertEqual([c.args[0] for c in read.call_args_list], list(signals.JOURNAL_UNITS))


if __name__ == "__main__":
    unittest.main()
