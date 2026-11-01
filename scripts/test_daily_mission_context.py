"""Offline tests for the daily mission context wrapper."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

import daily_mission_context as context
import daily_mission_prepare as prepare
import daily_mission_signals as signals


def complete_signals():
    return {
        "units": [{"name": name, "state": "active", "result": "success"}
                  for name in signals.UNITS],
        "releases": [{"name": Path(path).name, "release": "b" * 40}
                     for path in signals.RELEASE_LINKS],
        "history": [{"unit": name, "status": "ok", "coverage": "complete",
                     "truncated": False, "events": []}
                    for name in signals.JOURNAL_UNITS],
    }


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.candidate = self.root / "candidate"
        self.dependency = self.root / "validated.py"
        self.sha = "a" * 40
        self.args = ["--repo", str(self.root), "--expected-commit", self.sha,
                     "--candidate", str(self.candidate), "--toques", str(self.root / "toques.py"),
                     "--validated-toques", str(self.dependency)]

    def run_context(self, args=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
             patch("socket.socket", side_effect=AssertionError("network")), \
             patch("subprocess.Popen", side_effect=AssertionError("process")):
            code = context.main(self.args if args is None else args)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(len(out.getvalue().splitlines()), 1)
        self.assertFalse(self.candidate.exists())
        self.assertFalse(self.dependency.exists())
        return code, json.loads(out.getvalue())

    @patch.object(signals, "collect_signals", side_effect=complete_signals)
    @patch.object(prepare, "prepare")
    def test_success_calls_only_helpers_and_returns_context(self, prep, collect):
        prep.return_value = self.candidate
        code, result = self.run_context()
        self.assertEqual(code, 0)
        self.assertEqual(result, {"status": "ok", "state": "collected", "sha": self.sha,
                                  "candidate": str(self.candidate),
                                  "validated_dependency": str(self.dependency),
                                  "signals": complete_signals()})
        prep.assert_called_once_with(str(self.root), self.sha, str(self.candidate),
                                     str(self.root / "toques.py"), str(self.dependency))
        collect.assert_called_once_with()

    @patch.object(signals, "collect_signals", return_value={"history": [{"status": "unknown"}]})
    @patch.object(prepare, "prepare")
    def test_unknown_is_reported_without_readiness(self, prep, collect):
        prep.return_value = self.candidate
        code, result = self.run_context()
        self.assertEqual(code, 0)
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["signals"]["history"][0]["status"], "unknown")

    @patch.object(signals, "_read_journal", return_value=b"")
    @patch.object(signals, "_release_signal")
    @patch.object(signals, "_unit_signal")
    @patch.object(prepare, "prepare")
    def test_real_collector_schema_reports_partial_with_mocked_reads(self, prep, unit, release, journal):
        prep.return_value = self.candidate
        unit.side_effect = lambda name: {"name": name, "state": "active", "result": "success"}
        release.side_effect = lambda path: {"name": Path(path).name, "release": "b" * 40}
        code, result = self.run_context()
        self.assertEqual((code, result["status"], result["state"]), (0, "ok", "partial"))
        self.assertTrue(all(item["coverage"] == "partial" for item in result["signals"]["history"]))
        self.assertEqual(journal.call_count, 2)

    @patch.object(signals, "collect_signals")
    @patch.object(prepare, "prepare")
    def test_coverage_tracks_history_and_ignores_optional_event_unknowns(self, prep, collect):
        prep.return_value = self.candidate
        data = complete_signals()
        data["history"][0]["events"] = [{"status": "ok", "reason": "unknown",
                                           "error_code": "unknown", "observed": "unknown"}]
        for coverage, truncated, expected in (("complete", False, "collected"),
                                               ("partial", False, "partial"),
                                               ("complete", True, "partial")):
            with self.subTest(coverage=coverage, truncated=truncated):
                data["history"][0].update(coverage=coverage, truncated=truncated)
                collect.return_value = data
                code, result = self.run_context()
                self.assertEqual((code, result["status"], result["state"]), (0, "ok", expected))

    @patch.object(signals, "collect_signals")
    @patch.object(prepare, "prepare")
    def test_missing_malformed_or_unknown_required_fields_never_claim_completeness(self, prep, collect):
        prep.return_value = self.candidate
        examples = [{}, {"units": []}]
        for group in ("units", "releases", "history"):
            data = complete_signals()
            data[group].pop()
            examples.append(data)
            data = complete_signals()
            data[group][0] = copy.deepcopy(data[group][1])
            examples.append(data)
        for group, field, value in (("units", "state", "unknown"), ("units", "result", None),
                                    ("releases", "release", "unknown"),
                                    ("history", "status", "unknown"),
                                    ("history", "coverage", "unknown"),
                                    ("history", "truncated", None),
                                    ("history", "events", None)):
            data = complete_signals()
            data[group][0][field] = value
            examples.append(data)
        for data in examples:
            with self.subTest(data=data):
                collect.return_value = data
                code, result = self.run_context()
                self.assertEqual((code, result["status"], result["state"]), (0, "ok", "unknown"))

    @patch.object(signals, "collect_signals")
    @patch.object(prepare, "prepare", side_effect=RuntimeError("PRIVATE_SECRET"))
    def test_prepare_error_is_sanitized_and_skips_signals(self, prep, collect):
        code, result = self.run_context()
        self.assertEqual((code, result), (1, {"status": "error", "state": "error", "stage": "preparation"}))
        collect.assert_not_called()
        self.assertNotIn("PRIVATE_SECRET", json.dumps(result))

    @patch.object(signals, "collect_signals", side_effect=RuntimeError("PRIVATE_SECRET"))
    @patch.object(prepare, "prepare")
    def test_signal_error_is_sanitized(self, prep, collect):
        prep.return_value = self.candidate
        code, result = self.run_context()
        self.assertEqual((code, result), (1, {"status": "error", "state": "error", "stage": "signals"}))
        self.assertNotIn("PRIVATE_SECRET", json.dumps(result))

    def test_invalid_arguments_are_json_without_sensitive_values(self):
        code, result = self.run_context(["--unknown", "PRIVATE_SECRET"])
        self.assertEqual((code, result), (1, {"status": "error", "state": "error", "stage": "arguments"}))
        self.assertNotIn("PRIVATE_SECRET", json.dumps(result))

    @patch.object(signals, "collect_signals", return_value={"bad": object()})
    @patch.object(prepare, "prepare")
    def test_serialization_failure_returns_error(self, prep, collect):
        prep.return_value = self.candidate
        code, result = self.run_context()
        self.assertEqual((code, result), (1, {"status": "error", "state": "error", "stage": "serialization"}))


if __name__ == "__main__":
    unittest.main()
