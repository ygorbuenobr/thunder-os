import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from thunder_os.agenttrail_observer import panel_state, project_state, read_mac_snapshot


class AgenttrailObserverTests(unittest.TestCase):
    def test_projection_does_not_return_paths_or_raw_transcript(self):
        state = project_state({
            "app": "agenttrail-kitchen",
            "executors": [{"provider": "codex", "state": "working",
                           "project": "/private/accounts/customer/thunder-os",
                           "currentTask": "Review account\n  next step",
                           "lastEventAt": 12, "tool": "raw secret", "cwd": "/private/accounts/customer"}],
            "discoveryLimited": False,
        }, "mac", 100)
        self.assertEqual(state["sessions"][0], {
            "provider": "codex", "state": "working", "project": "thunder-os",
            "last_event_at": 12,
        })
        self.assertNotIn("raw secret", json.dumps(state))
        self.assertNotIn("/private", json.dumps(state))
        self.assertNotIn("Review account", json.dumps(state))

    def test_invalid_providers_do_not_consume_session_limit(self):
        raw = {"app": "agenttrail-kitchen", "executors": [
            {"provider": "other"} for _ in range(30)
        ] + [{"provider": "codex", "state": "working"}]}
        self.assertEqual(len(project_state(raw, "mac")["sessions"]), 1)

    def test_invalid_input_rejected(self):
        with self.assertRaises(ValueError):
            project_state({"app": "other", "executors": []}, "vps")

    def test_stale_mac_snapshot_is_not_presented_as_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mac.json"
            path.write_text(json.dumps({"host": "mac", "status": "available",
                                        "observed_at": 100, "sessions": []}))
            with self.assertRaises(ValueError):
                read_mac_snapshot(path, now=281)
            self.assertEqual(read_mac_snapshot(path, now=280)["host"], "mac")

    def test_mac_snapshot_drops_unexpected_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mac.json"
            path.write_text(json.dumps({"host": "mac", "status": "available", "observed_at": 100,
                                        "secret": "not for panel", "sessions": [{"provider": "codex",
                                        "project": "/private/customer/thunder-os", "state": "working",
                                        "task": "Review", "raw": "not for panel"}]}))
            result = read_mac_snapshot(path, now=101)
            self.assertNotIn("not for panel", json.dumps(result))
            self.assertNotIn("Review", json.dumps(result))
            self.assertEqual(result["sessions"][0]["project"], "thunder-os")

    def test_failed_observers_remain_visible_as_unavailable(self):
        with patch("thunder_os.agenttrail_observer.read_kitchen", side_effect=OSError), patch(
            "thunder_os.agenttrail_observer.read_mac_snapshot", side_effect=OSError
        ):
            result = panel_state()
        self.assertEqual(result["hosts"]["mac"]["status"], "unavailable")
        self.assertEqual(result["hosts"]["vps"]["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
