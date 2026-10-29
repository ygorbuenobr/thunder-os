import subprocess
import unittest
from unittest.mock import patch
import seqd_mail_readonly as m


class TestLauncher(unittest.TestCase):
    def test_inspect_and_cleanup_timeout_still_safe(self):
        with patch.object(m.subprocess, "run", side_effect=subprocess.TimeoutExpired("synthetic", 1)):
            value = m.read()
        self.assertEqual(value, {"state": "unavailable", "error_code": "seqd_read_timeout"})

    def test_docker_missing_still_safe(self):
        with patch.object(m.subprocess, "run", side_effect=FileNotFoundError("synthetic")):
            value = m.read()
        self.assertEqual(value, {"state": "unavailable", "error_code": "seqd_reader_failed"})


if __name__ == "__main__":
    unittest.main()
