import json
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from thunder_os.connection_ledger import profile_kind, research_request_kind
from thunder_os.connection_provider import ConnectionProviderError
from thunder_os.connection_runtime import ConnectionRuntimeError, load_config, main, run_once
from thunder_os.db import Repository


ACCOUNT = "a" * 24
PROFILE = "https://www.linkedin.com/in/fixture-person"


class ConnectionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.brain = self.root / "brain"
        account = self.brain / "contas" / "fixture"
        account.mkdir(parents=True)
        (account / "ficha.md").write_text(
            "# Fixture\n- Site: https://fixture.example\n- Setor: Distribuição B2B\n"
            f"\n## Decisores\n| Fixture | {PROFILE} |\n", encoding="utf-8")
        self.database = self.root / "state.db"
        Repository(self.database)
        self.config = {"repository": str(self.database), "account_id": ACCOUNT}

    def pages(self, rows):
        def fetch(cursor):
            batch = rows[cursor:cursor + 10]
            return {"success": True, "accountId": ACCOUNT, "sortBy": "recentlyAdded",
                    "startIndex": cursor, "count": len(batch), "connections": batch,
                    "endOfList": not batch, "nextStartIndex": None if not batch else cursor + len(batch)}
        return fetch

    def run_main(self, mode, fetch_page):
        provider = SimpleNamespace(fetch_page=fetch_page,
                                   fetch_profile_company=lambda _: (None, None))
        stdout = io.StringIO()
        stderr = io.StringIO()
        argv = ["connection-runtime", "--config", str(self.root / "config.json"),
                "--brain", str(self.brain), "--mode", mode]
        with (mock.patch("thunder_os.connection_runtime.load_config", return_value=self.config),
              mock.patch("thunder_os.connection_runtime._provider", return_value=provider),
              mock.patch.object(sys, "argv", argv), redirect_stdout(stdout), redirect_stderr(stderr)):
            code = main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_main_projects_live_success_and_incomplete_error(self):
        success_cursors = []
        today = datetime.now(timezone.utc).astimezone(ZoneInfo("America/Sao_Paulo")).date()
        today_value = f"{today.isoformat()}T12:00:00-03:00"
        older_value = f"{(today - timedelta(days=1)).isoformat()}T12:00:00-03:00"

        def boundary(cursor):
            success_cursors.append(cursor)
            rows = [{"profileUrl": f"https://linkedin.com/in/live-{index}",
                     "connectedDate": today_value}
                    for index in range(cursor, cursor + 10)]
            if cursor == 50:
                rows[-1]["connectedDate"] = older_value
            return {"success": True, "accountId": ACCOUNT, "sortBy": "recentlyAdded",
                    "startIndex": cursor, "count": len(rows), "connections": rows,
                    "endOfList": False, "nextStartIndex": cursor + len(rows)}

        code, stdout, stderr = self.run_main("live", boundary)
        self.assertEqual((0, ""), (code, stderr))
        self.assertEqual("ok", json.loads(stdout)["status"])
        self.assertEqual([0, 10, 20, 30, 40, 50], success_cursors)

        incomplete_cursors = []

        def incomplete(cursor):
            incomplete_cursors.append(cursor)
            row = {"profileUrl": f"https://linkedin.com/in/incomplete-{cursor}",
                   "connectedDate": today_value}
            return {"success": True, "accountId": ACCOUNT, "sortBy": "recentlyAdded",
                    "startIndex": cursor, "count": 1, "connections": [row],
                    "endOfList": False, "nextStartIndex": cursor + 1}

        code, stdout, stderr = self.run_main("live", incomplete)
        self.assertEqual((1, ""), (code, stdout))
        self.assertEqual({"status": "blocked", "error_code": "connection_window_incomplete"},
                         json.loads(stderr))
        self.assertEqual(list(range(20)), incomplete_cursors)

    def test_main_projects_backfill_success_and_row_error(self):
        def invalid(_cursor):
            raise ConnectionProviderError("connection_row_invalid")

        code, stdout, stderr = self.run_main("backfill", invalid)
        self.assertEqual((1, ""), (code, stdout))
        self.assertEqual({"status": "blocked", "error_code": "connection_row_invalid"},
                         json.loads(stderr))

        code, stdout, stderr = self.run_main("backfill", self.pages([]))
        self.assertEqual((0, ""), (code, stderr))
        result = json.loads(stdout)
        self.assertEqual(("ok", "backfill", True),
                         (result["status"], result["mode"], result["complete"]))

    def test_live_read_to_research_request_with_no_effect_job(self):
        first = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        self.assertEqual(0, run_once(self.config, brain=self.brain, mode="live",
                                     fetch_page=self.pages([]), now=first)["research"])
        row = {"profileUrl": PROFILE, "connectedDate": "Conexão feita em 5 de outubro de 2026"}
        result = run_once(self.config, brain=self.brain, mode="live",
                          fetch_page=self.pages([row]), now=first.replace(hour=13))
        self.assertEqual(1, result["research"])
        repo = Repository(self.database)
        self.assertEqual("research", repo.get_state(profile_kind(ACCOUNT), PROFILE)["status"])
        request = repo.get_state(research_request_kind(ACCOUNT), PROFILE)
        self.assertTrue(request["fit_evidence"].startswith("brain:contas/fixture/ficha.md#sha256:"))
        self.assertEqual(1, len(repo.list_work()))
        self.assertEqual("research", repo.list_work()[0].context["capability"])
        self.assertEqual("agent_job", repo.outbox()[0]["kind"])
        self.assertEqual([], repo.effects())

    def test_placeholder_sector_does_not_enqueue_research(self):
        fiche = self.brain / "contas" / "fixture" / "ficha.md"
        row = {"profileUrl": PROFILE, "connectedDate": "Conexão feita em 5 de outubro de 2026"}
        first = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        run_once(self.config, brain=self.brain, mode="live", fetch_page=self.pages([]), now=first)
        for sector in ("A definir", "Não verificado (pendente)",
                       "[A definir]", "(Não verificado)"):
            fiche.write_text("# Fixture\n- Site: https://fixture.example\n"
                             f"- Setor: {sector}\n\n## Decisores\n| Fixture | {PROFILE} |\n",
                             encoding="utf-8")
            result = run_once(self.config, brain=self.brain, mode="live",
                              fetch_page=self.pages([row]), now=first.replace(hour=13))
            repo = Repository(self.database)
            self.assertEqual(0, result["jobs_ensured"])
            self.assertEqual("needs_fit", repo.get_state(profile_kind(ACCOUNT), PROFILE)["status"])
            self.assertIsNone(repo.get_state(research_request_kind(ACCOUNT), PROFILE))
            self.assertEqual([], repo.list_work())
            self.assertEqual([], repo.effects())

    def test_protected_config_rejects_world_readable_or_symlinked_credential(self):
        credential = self.root / "credential"
        credential.write_text("synthetic-token-not-used", encoding="utf-8")
        credential.chmod(0o600)
        config = self.root / "config.json"
        config.write_text(json.dumps({**self.config, "version": 1,
                                      "account_public_id": "fixture-owner",
                                      "api_credential_file": str(credential)}), encoding="utf-8")
        config.chmod(0o600)
        self.assertEqual(ACCOUNT, load_config(config)["account_id"])
        credential.chmod(0o644)
        with self.assertRaisesRegex(ConnectionRuntimeError, "protected_file_invalid"):
            load_config(config)
        credential.chmod(0o600)
        link = self.root / "credential-link"
        link.symlink_to(credential)
        config.write_text(json.dumps({**self.config, "version": 1,
                                      "account_public_id": "fixture-owner",
                                      "api_credential_file": str(link)}), encoding="utf-8")
        with self.assertRaisesRegex(ConnectionRuntimeError, "protected_file_invalid"):
            load_config(config)

    def test_active_monitor_v2_config_is_accepted_without_weakening_validation(self):
        credential = self.root / "credential-v2"
        credential.write_text("synthetic-token-not-used", encoding="utf-8")
        credential.chmod(0o600)
        config = self.root / "config-v2.json"
        payload = {**self.config, "version": 2, "account_public_id": "fixture-owner",
                   "api_credential_file": str(credential), "self_profile_id": "ACoFIXTURE",
                   "start_ms": 1790000000000}
        config.write_text(json.dumps(payload), encoding="utf-8")
        config.chmod(0o600)
        self.assertEqual(2, load_config(config)["version"])
        config.write_text(json.dumps({**payload,
                                      "self_profile_id": "urn:li:fsd_profile:ACoFIXTURE"}),
                          encoding="utf-8")
        self.assertEqual(2, load_config(config)["version"])
        config.write_text(json.dumps({**payload, "start_ms": "1790000000000"}), encoding="utf-8")
        with self.assertRaisesRegex(ConnectionRuntimeError, "connection_config_invalid"):
            load_config(config)
        config.write_text(json.dumps({**payload, "extra_secret": "no"}), encoding="utf-8")
        with self.assertRaisesRegex(ConnectionRuntimeError, "connection_config_invalid"):
            load_config(config)

    def test_research_queue_has_shared_daily_cap_and_replays_pending_next_day(self):
        fiche = self.brain / "contas" / "fixture" / "ficha.md"
        profiles = [f"https://www.linkedin.com/in/fixture-person-{index}" for index in range(7)]
        fiche.write_text("# Fixture\n- Site: https://fixture.example\n- Setor: Distribuição B2B\n"
                         "\n## Decisores\n" + "\n".join(f"| Person | {url} |" for url in profiles),
                         encoding="utf-8")
        baseline = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        run_once(self.config, brain=self.brain, mode="live", fetch_page=self.pages([]), now=baseline)
        rows = [{"profileUrl": url, "connectedDate": "Conexão feita em 5 de outubro de 2026"}
                for url in profiles]
        first = run_once(self.config, brain=self.brain, mode="live",
                         fetch_page=self.pages(rows), now=baseline.replace(hour=13))
        self.assertEqual(5, first["jobs_ensured"])
        self.assertEqual(5, len(Repository(self.database).list_work()))
        replay = run_once(self.config, brain=self.brain, mode="live",
                          fetch_page=self.pages(rows), now=baseline.replace(hour=14))
        self.assertEqual(0, replay["jobs_ensured"])
        tomorrow = run_once(self.config, brain=self.brain, mode="live",
                            fetch_page=self.pages(rows), now=baseline.replace(day=6))
        self.assertEqual(2, tomorrow["jobs_ensured"])
        self.assertEqual(7, len(Repository(self.database).list_work()))

    def test_company_candidate_never_creates_research_without_exact_profile_link(self):
        fiche = self.brain / "contas" / "fixture" / "ficha.md"
        fiche.write_text("# Alpha Logistics\n- Site: https://fixture.example\n"
                         "- Setor: Distribuição B2B\n", encoding="utf-8")
        unknown = "https://www.linkedin.com/in/new-person"
        row = {"profileUrl": unknown, "connectedDate": "Conexão feita em 5 de outubro de 2026"}
        first = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        run_once(self.config, brain=self.brain, mode="live", fetch_page=self.pages([]), now=first)
        second = run_once(self.config, brain=self.brain, mode="live",
                          fetch_page=self.pages([row]),
                          fetch_profile_company=lambda _: ("Alpha Logistics", "Alpha Logistics"),
                          now=first.replace(hour=13))
        self.assertEqual(0, second["research"])
        self.assertEqual(0, second["jobs_ensured"])
        self.assertEqual({"checked": 1, "candidates": 1, "error": None},
                         second["company_resolution"])
        self.assertEqual("needs_identity", Repository(self.database).get_state(
            profile_kind(ACCOUNT), unknown)["status"])
        third = run_once(self.config, brain=self.brain, mode="live",
                         fetch_page=self.pages([row]),
                         fetch_profile_company=lambda _: self.fail("profile refetched"),
                         now=first.replace(hour=14))
        self.assertEqual(0, third["jobs_ensured"])
        repo = Repository(self.database)
        self.assertNotIn("account_id", repo.get_state(profile_kind(ACCOUNT), unknown))
        self.assertEqual(0, len(repo.list_work()))
        self.assertEqual([], repo.effects())


if __name__ == "__main__":
    unittest.main()
