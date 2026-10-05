import tempfile
import unittest
from datetime import date
from pathlib import Path

from thunder_os.connection_backfill import BACKFILL_CHECKPOINT, record_backfill_batch
from thunder_os.connection_intake import ConnectionIntakeError
from thunder_os.connection_ledger import LIVE_CHECKPOINT, profile_kind, research_request_kind
from thunder_os.db import Repository


ACCOUNT = "connectsafely-fixture"


def provider(rows):
    def fetch(cursor):
        batch = rows[cursor:cursor + 10]
        return {"success": True, "accountId": ACCOUNT, "sortBy": "recentlyAdded",
                "startIndex": cursor, "connections": batch, "count": len(batch),
                "nextStartIndex": cursor + len(batch), "endOfList": cursor + len(batch) >= len(rows)}
    return fetch


def connection(number):
    return {"profileUrl": f"https://www.linkedin.com/in/person-{number}",
            "connectedDate": "Conexão feita em 4 de outubro de 2026"}


class ConnectionBackfillTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.repo = Repository(Path(self.scratch.name) / "state.db")

    def test_daily_limit_and_cursor_isolation(self):
        self.repo.put_state(LIVE_CHECKPOINT, ACCOUNT, {"marker": "live"})
        rows = [connection(index) for index in range(125)]
        result = record_backfill_batch(self.repo, provider(rows), account_id=ACCOUNT,
                                      today=date(2026, 10, 5), account_by_profile={}, fit_evidence_by_account={})
        self.assertEqual(100, result["processed"])
        self.assertEqual(100, self.repo.get_state(BACKFILL_CHECKPOINT, ACCOUNT)["next_start_index"])
        self.assertEqual({"marker": "live", "id": ACCOUNT, "version": 1,
                          "updated_at": self.repo.get_state(LIVE_CHECKPOINT, ACCOUNT)["updated_at"]},
                         self.repo.get_state(LIVE_CHECKPOINT, ACCOUNT))
        self.assertEqual(0, record_backfill_batch(self.repo, lambda _: self.fail("fetch after limit"),
                         account_id=ACCOUNT, today=date(2026, 10, 5),
                         account_by_profile={}, fit_evidence_by_account={})["processed"])
        next_day = record_backfill_batch(self.repo, provider(rows), account_id=ACCOUNT,
                                         today=date(2026, 10, 6), account_by_profile={}, fit_evidence_by_account={})
        self.assertEqual(25, next_day["processed"])
        self.assertTrue(next_day["complete"])
        self.assertEqual(125, len(self.repo.list_state(profile_kind(ACCOUNT))))

    def test_identity_and_research_limits(self):
        rows = [connection(index) for index in range(8)]
        mapping = {rows[index]["profileUrl"]: "account.example" for index in range(7)}
        canonical = {url: account for url, account in mapping.items()}
        result = record_backfill_batch(self.repo, provider(rows), account_id=ACCOUNT,
                                      today=date(2026, 10, 5), account_by_profile=canonical,
                                      fit_evidence_by_account={"account.example": "catalogue/decision-1"})
        self.assertEqual(8, result["processed"])
        self.assertEqual(5, result["research"])
        self.assertEqual(5, len(self.repo.list_state(research_request_kind(ACCOUNT))))
        self.assertEqual(["research"] * 5 + ["qualified_waiting_budget"] * 2 + ["needs_identity"],
                         [item["status"] for item in result["decisions"]])
        next_day = record_backfill_batch(self.repo, lambda _: self.fail("complete backfill refetched"),
                                          account_id=ACCOUNT, today=date(2026, 10, 6),
                                          account_by_profile=canonical,
                                          fit_evidence_by_account={"account.example": "catalogue/decision-1"})
        self.assertEqual(2, next_day["research"])
        self.assertEqual(7, len(self.repo.list_state(research_request_kind(ACCOUNT))))
        self.assertEqual(0, record_backfill_batch(self.repo, lambda _: self.fail("refetched"),
                         account_id=ACCOUNT, today=date(2026, 10, 6), account_by_profile=canonical,
                         fit_evidence_by_account={"account.example": "catalogue/decision-1"})["research"])

    def test_shifted_anchor_fails_without_state_change(self):
        rows = [connection(index) for index in range(110)]
        record_backfill_batch(self.repo, provider(rows), account_id=ACCOUNT,
                              today=date(2026, 10, 5), account_by_profile={}, fit_evidence_by_account={})
        before = self.repo.get_state(BACKFILL_CHECKPOINT, ACCOUNT)
        shifted = [connection(index) for index in range(80, 95)] + [connection(index) for index in range(100, 120)]
        def wrong_page(cursor):
            response = provider(shifted)(cursor)
            response["startIndex"] = cursor
            return response
        with self.assertRaisesRegex(ConnectionIntakeError, "backfill_anchor_missing"):
            record_backfill_batch(self.repo, wrong_page, account_id=ACCOUNT,
                                  today=date(2026, 10, 6), account_by_profile={}, fit_evidence_by_account={})
        self.assertEqual(before, self.repo.get_state(BACKFILL_CHECKPOINT, ACCOUNT))

    def test_invalid_next_page_does_not_promote_waiting_profile(self):
        rows = [connection(index) for index in range(110)]
        mapping = {row["profileUrl"]: "account.example" for row in rows}
        record_backfill_batch(self.repo, provider(rows), account_id=ACCOUNT,
                              today=date(2026, 10, 5), account_by_profile=mapping,
                              fit_evidence_by_account={"account.example": "catalogue/decision-1"})
        waiting = [row for row in self.repo.list_state(profile_kind(ACCOUNT))
                   if row["status"] == "qualified_waiting_budget"]
        self.assertTrue(waiting)
        live_key = "https://www.linkedin.com/in/live-needs-fit"
        self.repo.put_state(profile_kind(ACCOUNT), live_key, {
            "profile": live_key, "connectsafely_account_id": ACCOUNT,
            "connected_at": "2026-10-06T00:00:00-03:00", "status": "needs_fit",
            "account_id": "live.example", "source": "connectsafely",
        })
        mapping[live_key] = "live.example"
        before_live = self.repo.get_state(profile_kind(ACCOUNT), live_key)
        shifted = [connection(index) for index in range(80, 95)] + [connection(index) for index in range(105, 110)]
        before = self.repo.get_state(BACKFILL_CHECKPOINT, ACCOUNT)
        with self.assertRaisesRegex(ConnectionIntakeError, "backfill_anchor_missing"):
            record_backfill_batch(self.repo, provider(shifted), account_id=ACCOUNT,
                                  today=date(2026, 10, 6), account_by_profile=mapping,
                                  fit_evidence_by_account={"account.example": "catalogue/decision-1",
                                                           "live.example": "catalogue/live-v2"})
        self.assertEqual(before, self.repo.get_state(BACKFILL_CHECKPOINT, ACCOUNT))
        self.assertEqual("qualified_waiting_budget", self.repo.get_state(profile_kind(ACCOUNT), waiting[0]["profile"])["status"])
        self.assertEqual(before_live, self.repo.get_state(profile_kind(ACCOUNT), live_key))
        self.assertIsNone(self.repo.get_state(research_request_kind(ACCOUNT), live_key))

    def test_complete_backfill_reclassifies_historical_and_live_profiles(self):
        historical = connection(1)
        record_backfill_batch(self.repo, provider([historical]), account_id=ACCOUNT,
                              today=date(2026, 10, 5), account_by_profile={}, fit_evidence_by_account={})
        historical_key = historical["profileUrl"]
        live_key = "https://www.linkedin.com/in/live-unresolved"
        self.repo.put_state(profile_kind(ACCOUNT), live_key, {
            "profile": live_key, "connectsafely_account_id": ACCOUNT,
            "connected_at": "2026-10-06T00:00:00-03:00", "status": "needs_fit",
            "account_id": "live.example", "source": "connectsafely",
        })
        no_fetch = lambda _: self.fail("completed backfill fetched provider page")
        mapping = {historical_key: "history.example", live_key: "live.example"}
        no_fit = record_backfill_batch(self.repo, no_fetch, account_id=ACCOUNT,
                                       today=date(2026, 10, 6),
                                       account_by_profile=mapping, fit_evidence_by_account={})
        self.assertEqual(0, no_fit["research"])
        historical_state = self.repo.get_state(profile_kind(ACCOUNT), historical_key)
        self.assertEqual("needs_fit", historical_state["status"])
        self.assertEqual(2, historical_state["version"])
        self.assertTrue(historical_state["catalog_version"].startswith("sha256:"))
        self.assertIsNone(self.repo.get_state(research_request_kind(ACCOUNT), historical_key))

        fit = {"history.example": "catalogue/history-v2", "live.example": "catalogue/live-v2"}
        result = record_backfill_batch(self.repo, no_fetch, account_id=ACCOUNT,
                                       today=date(2026, 10, 6),
                                       account_by_profile=mapping, fit_evidence_by_account=fit)
        self.assertEqual(2, result["research"])
        for profile, expected_source in ((historical_key, "historical_backfill"),
                                         (live_key, "exact_profile_mapping")):
            state = self.repo.get_state(profile_kind(ACCOUNT), profile)
            request = self.repo.get_state(research_request_kind(ACCOUNT), profile)
            self.assertEqual("research", state["status"])
            self.assertEqual(expected_source, request["source"])
            self.assertEqual(state["version"], request["profile_version"])
            self.assertEqual(state["catalog_version"], request["catalog_version"])
        self.assertEqual(0, record_backfill_batch(
            self.repo, no_fetch, account_id=ACCOUNT, today=date(2026, 10, 6),
            account_by_profile=mapping, fit_evidence_by_account=fit,
        )["research"])
        self.assertEqual(2, len(self.repo.list_state(research_request_kind(ACCOUNT))))

    def test_new_mapping_waits_for_next_day_research_budget(self):
        rows = [connection(index) for index in range(6)]
        mapping = {row["profileUrl"]: "account.example" for row in rows[:5]}
        fit = {"account.example": "catalogue/decision-1"}
        record_backfill_batch(self.repo, provider(rows), account_id=ACCOUNT,
                              today=date(2026, 10, 5),
                              account_by_profile=mapping, fit_evidence_by_account=fit)
        unknown = rows[5]["profileUrl"]
        self.assertEqual("needs_identity", self.repo.get_state(profile_kind(ACCOUNT), unknown)["status"])
        mapping[unknown] = "account.example"
        no_fetch = lambda _: self.fail("completed backfill fetched provider page")
        same_day = record_backfill_batch(self.repo, no_fetch, account_id=ACCOUNT,
                                         today=date(2026, 10, 5),
                                         account_by_profile=mapping, fit_evidence_by_account=fit)
        self.assertEqual(0, same_day["research"])
        self.assertEqual("qualified_waiting_budget",
                         self.repo.get_state(profile_kind(ACCOUNT), unknown)["status"])
        self.assertIsNone(self.repo.get_state(research_request_kind(ACCOUNT), unknown))
        next_day = record_backfill_batch(self.repo, no_fetch, account_id=ACCOUNT,
                                         today=date(2026, 10, 6),
                                         account_by_profile=mapping, fit_evidence_by_account=fit)
        self.assertEqual(1, next_day["research"])
        self.assertEqual("research", self.repo.get_state(profile_kind(ACCOUNT), unknown)["status"])
        self.assertEqual(6, len(self.repo.list_state(research_request_kind(ACCOUNT))))


if __name__ == "__main__":
    unittest.main()
