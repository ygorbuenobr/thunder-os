import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from thunder_os.connection_intake import ConnectionIntakeError, profile_key
from thunder_os.connection_ledger import (
    LIVE_CHECKPOINT, profile_kind, record_live_poll, research_request_kind,
)
from thunder_os.db import Repository, RepositoryConflict


ACTIVATION = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
FIRST_POLL = datetime(2026, 10, 5, 13, tzinfo=timezone.utc)
SECOND_POLL = datetime(2026, 10, 6, 13, tzinfo=timezone.utc)


def page(rows, *, account_id="cs-a", end=True, cursor=0, next_cursor=None):
    return {
        "connections": rows, "success": True, "accountId": account_id,
        "sortBy": "recentlyAdded", "startIndex": cursor, "count": len(rows),
        "endOfList": end, "nextStartIndex": next_cursor,
    }


def connection(slug, day):
    return {"profileUrl": f"https://linkedin.com/in/{slug}",
            "connectedDate": f"Conexão feita em {day} de outubro de 2026"}


class ConnectionLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "disposable.sqlite"
        self.repo = Repository(self.path)

    def poll(self, response, *, account_id="cs-a", observed_at=FIRST_POLL,
             account_by_profile=None, fit_evidence_by_account=None, contacted=None, max_pages=5):
        return record_live_poll(
            self.repo, lambda cursor: response, account_id=account_id,
            activation_at=ACTIVATION, observed_at=observed_at,
            account_by_profile=account_by_profile or {},
            fit_evidence_by_account=fit_evidence_by_account if fit_evidence_by_account is not None else {
                "acme.example": "brain:contas/acme/ficha.md#sha256:fixture"},
            contacted=contacted or set(),
            max_pages=max_pages,
        )

    def test_complete_live_poll(self):
        with sqlite3.connect(self.path) as con:
            con.execute("CREATE TABLE legacy_records (id TEXT PRIMARY KEY, value TEXT)")
            con.execute("INSERT INTO legacy_records VALUES ('old', 'preserved')")
        self.repo.migrate()
        self.repo.put_state("connection_backfill_checkpoint", "cs-a", {"cursor": 17})
        baseline = [connection("same-day", 5), connection("older", 4)]
        self.assertEqual([], self.poll(page(baseline)))
        canonical = profile_key(baseline[0]["profileUrl"])
        self.assertEqual("historical", self.repo.get_state(profile_kind("cs-a"), canonical)["status"])
        self.assertEqual([], self.repo.list_state(research_request_kind("cs-a")))

        new = connection("mapped", 6)
        unknown = connection("unknown", 6)
        second = page([new, unknown, *baseline])
        decisions = self.poll(
            second, observed_at=SECOND_POLL,
            account_by_profile={new["profileUrl"]: "acme.example"},
        )
        self.assertEqual(["research", "needs_identity"], [item["status"] for item in decisions])
        mapped = profile_key(new["profileUrl"])
        request = self.repo.get_state(research_request_kind("cs-a"), mapped)
        self.assertEqual("acme.example", request["account_id"])
        self.assertEqual("exact_profile_mapping", request["source"])
        self.assertIn("fit_evidence", request)
        self.assertIsNone(self.repo.get_state(research_request_kind("cs-a"), profile_key(unknown["profileUrl"])))
        self.assertEqual(4, len(self.repo.list_state(profile_kind("cs-a"))))
        self.assertEqual(2, self.repo.get_state(LIVE_CHECKPOINT, "cs-a")["version"])
        self.assertEqual(17, self.repo.get_state("connection_backfill_checkpoint", "cs-a")["cursor"])
        self.assertIsNone(self.repo.get_state(LIVE_CHECKPOINT, "cs-b"))
        other_decisions = self.poll(page([new], account_id="cs-b"),
                                    account_id="cs-b", observed_at=SECOND_POLL)
        self.assertEqual(["needs_identity"], [item["status"] for item in other_decisions])
        self.assertEqual(1, len(self.repo.list_state(profile_kind("cs-b"))))
        self.assertEqual([], self.repo.list_state(research_request_kind("cs-b")))
        with sqlite3.connect(self.path) as con:
            self.assertEqual(("preserved",), con.execute("SELECT value FROM legacy_records WHERE id='old'").fetchone())

    def test_invalid_poll_is_atomic(self):
        old = connection("old", 5)
        self.poll(page([old]))
        before_checkpoint = self.repo.get_state(LIVE_CHECKPOINT, "cs-a")
        before_profiles = self.repo.list_state(profile_kind("cs-a"))
        before_requests = self.repo.list_state(research_request_kind("cs-a"))
        fresh = connection("fresh", 6)
        older = connection("older", 4)
        invalid = [
            (page([fresh], account_id="other"), 5, "connection_page_invalid"),
            (page([fresh], end=False, next_cursor=1), 1, "connection_window_incomplete"),
            (page([older, fresh]), 5, "connection_order_invalid"),
        ]
        for response, max_pages, error in invalid:
            with self.subTest(response=response):
                with self.assertRaisesRegex(ConnectionIntakeError, error):
                    self.poll(response, observed_at=SECOND_POLL,
                              account_by_profile={fresh["profileUrl"]: "acme.example"},
                              max_pages=max_pages)
                self.assertEqual(before_checkpoint, self.repo.get_state(LIVE_CHECKPOINT, "cs-a"))
                self.assertEqual(before_profiles, self.repo.list_state(profile_kind("cs-a")))
                self.assertEqual(before_requests, self.repo.list_state(research_request_kind("cs-a")))

        with sqlite3.connect(self.path) as con:
            con.execute("""CREATE TRIGGER reject_live_checkpoint BEFORE UPDATE ON thunder_state
                WHEN NEW.kind='connection_live_checkpoint' BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected failure"):
            self.poll(page([fresh, old]), observed_at=SECOND_POLL,
                      account_by_profile={fresh["profileUrl"]: "acme.example"})
        self.assertEqual(before_checkpoint, self.repo.get_state(LIVE_CHECKPOINT, "cs-a"))
        self.assertEqual(before_profiles, self.repo.list_state(profile_kind("cs-a")))
        self.assertEqual(before_requests, self.repo.list_state(research_request_kind("cs-a")))

    def test_repeated_poll_is_idempotent(self):
        self.poll(page([]))
        row = connection("Alice", 6)
        mapping = {row["profileUrl"]: "acme.example"}
        first = self.poll(page([row]), observed_at=SECOND_POLL, account_by_profile=mapping)
        self.assertEqual(["research"], [item["status"] for item in first])
        key = profile_key(row["profileUrl"])
        checkpoint = self.repo.get_state(LIVE_CHECKPOINT, "cs-a")
        profile = self.repo.get_state(profile_kind("cs-a"), key)
        request = self.repo.get_state(research_request_kind("cs-a"), key)

        self.repo = Repository(self.path)
        equivalent = {**row, "profileUrl": "https://www.linkedin.com/in/%41lice?trk=feed"}
        self.assertEqual([], self.poll(page([equivalent]), observed_at=SECOND_POLL,
                                       account_by_profile=mapping))
        self.assertEqual(checkpoint, self.repo.get_state(LIVE_CHECKPOINT, "cs-a"))
        self.assertEqual(profile, self.repo.get_state(profile_kind("cs-a"), key))
        self.assertEqual(request, self.repo.get_state(research_request_kind("cs-a"), key))
        self.assertEqual(1, len(self.repo.list_state(profile_kind("cs-a"))))
        self.assertEqual(1, len(self.repo.list_state(research_request_kind("cs-a"))))

        with self.assertRaisesRegex(ConnectionIntakeError, "connection_poll_conflict"):
            self.poll(page([]), observed_at=SECOND_POLL)
        with self.assertRaisesRegex(ConnectionIntakeError, "connection_poll_stale"):
            self.poll(page([row]), observed_at=FIRST_POLL)
        self.assertEqual(checkpoint, self.repo.get_state(LIVE_CHECKPOINT, "cs-a"))

        barrier = threading.Barrier(2)
        results = []
        errors = []
        concurrent_repos = [Repository(self.path) for _ in range(2)]

        def repeat(repo):
            barrier.wait()
            try:
                results.append(record_live_poll(
                    repo, lambda cursor: page([row], account_id="cs-c"),
                    account_id="cs-c", activation_at=ACTIVATION,
                    observed_at=SECOND_POLL, account_by_profile=mapping,
                    fit_evidence_by_account={"acme.example": "brain:contas/acme/ficha.md#sha256:fixture"},
                    contacted=set(),
                ))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=repeat, args=(repo,)) for repo in concurrent_repos]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], errors)
        self.assertCountEqual([[], [first[0]]], results)
        self.assertEqual(1, len(self.repo.list_state(profile_kind("cs-c"))))
        self.assertEqual(1, len(self.repo.list_state(research_request_kind("cs-c"))))
        self.assertEqual(1, self.repo.get_state(LIVE_CHECKPOINT, "cs-c")["version"])

    def test_exact_identity_without_fit_stays_unqueued(self):
        self.poll(page([]))
        row = connection("candidate", 6)
        result = self.poll(page([row]), observed_at=SECOND_POLL,
                           account_by_profile={row["profileUrl"]: "acme.example"},
                           fit_evidence_by_account={})
        self.assertEqual("needs_fit", result[0]["status"])
        key = profile_key(row["profileUrl"])
        self.assertEqual("acme.example", self.repo.get_state(profile_kind("cs-a"), key)["account_id"])
        self.assertIsNone(self.repo.get_state(research_request_kind("cs-a"), key))

    def test_existing_live_profile_reclassifies_with_catalog_provenance(self):
        self.poll(page([]))
        row = connection("later-mapped", 6)
        key = profile_key(row["profileUrl"])
        self.assertEqual("needs_identity", self.poll(page([row]), observed_at=SECOND_POLL)[0]["status"])
        checkpoint = self.repo.get_state(LIVE_CHECKPOINT, "cs-a")
        mapping = {row["profileUrl"]: "acme.example"}

        identity = self.poll(page([row]), observed_at=SECOND_POLL,
                             account_by_profile=mapping, fit_evidence_by_account={})
        self.assertEqual(["needs_fit"], [item["status"] for item in identity])
        needs_fit = self.repo.get_state(profile_kind("cs-a"), key)
        self.assertEqual(2, needs_fit["version"])
        self.assertEqual("acme.example", needs_fit["account_id"])
        self.assertTrue(needs_fit["mapping_provenance"].startswith("account_by_profile#sha256:"))
        self.assertTrue(needs_fit["catalog_version"].startswith("sha256:"))
        self.assertIsNone(self.repo.get_state(research_request_kind("cs-a"), key))

        fit = {"acme.example": "brain:contas/acme/ficha.md#sha256:verified"}
        promoted = self.poll(page([row]), observed_at=SECOND_POLL,
                             account_by_profile=mapping, fit_evidence_by_account=fit)
        self.assertEqual(["research"], [item["status"] for item in promoted])
        profile = self.repo.get_state(profile_kind("cs-a"), key)
        request = self.repo.get_state(research_request_kind("cs-a"), key)
        self.assertEqual(3, profile["version"])
        self.assertNotEqual(needs_fit["catalog_version"], profile["catalog_version"])
        self.assertEqual(profile["catalog_version"], request["catalog_version"])
        self.assertEqual(profile["mapping_provenance"], request["mapping_provenance"])
        self.assertEqual(profile["version"], request["profile_version"])
        self.assertEqual(fit["acme.example"], request["fit_evidence"])
        self.assertEqual("exact_profile_mapping", request["source"])
        self.assertEqual(checkpoint, self.repo.get_state(LIVE_CHECKPOINT, "cs-a"))
        self.assertEqual([], self.poll(page([row]), observed_at=SECOND_POLL,
                                       account_by_profile=mapping, fit_evidence_by_account=fit))
        self.assertEqual(profile, self.repo.get_state(profile_kind("cs-a"), key))
        self.assertEqual(request, self.repo.get_state(research_request_kind("cs-a"), key))

    def test_invalid_page_and_conflicting_mapping_cannot_reclassify(self):
        self.poll(page([]))
        row = connection("unresolved", 6)
        self.poll(page([row]), observed_at=SECOND_POLL)
        key = profile_key(row["profileUrl"])
        before = self.repo.get_state(profile_kind("cs-a"), key)
        checkpoint = self.repo.get_state(LIVE_CHECKPOINT, "cs-a")
        mapping = {row["profileUrl"]: "acme.example"}
        fit = {"acme.example": "brain:contas/acme/ficha.md#sha256:verified"}
        with self.assertRaisesRegex(ConnectionIntakeError, "connection_page_invalid"):
            self.poll(page([row], account_id="wrong"), observed_at=SECOND_POLL,
                      account_by_profile=mapping, fit_evidence_by_account=fit)
        self.assertEqual(before, self.repo.get_state(profile_kind("cs-a"), key))
        self.assertEqual(checkpoint, self.repo.get_state(LIVE_CHECKPOINT, "cs-a"))
        self.assertIsNone(self.repo.get_state(research_request_kind("cs-a"), key))

        self.poll(page([row]), observed_at=SECOND_POLL,
                  account_by_profile=mapping, fit_evidence_by_account={})
        needs_fit = self.repo.get_state(profile_kind("cs-a"), key)
        with self.assertRaisesRegex(ConnectionIntakeError, "connection_account_ambiguous"):
            self.poll(page([row]), observed_at=SECOND_POLL,
                      account_by_profile={row["profileUrl"]: "other.example"},
                      fit_evidence_by_account={"other.example": "catalogue/other"})
        self.assertEqual(needs_fit, self.repo.get_state(profile_kind("cs-a"), key))
        self.assertIsNone(self.repo.get_state(research_request_kind("cs-a"), key))

        with sqlite3.connect(self.path) as con:
            con.execute("""CREATE TRIGGER reject_research_request BEFORE INSERT ON thunder_state
                WHEN NEW.kind='connection_research_request:cs-a'
                BEGIN SELECT RAISE(ABORT, 'injected request failure'); END""")
        with self.assertRaises(RepositoryConflict) as failure:
            self.poll(page([row]), observed_at=SECOND_POLL,
                      account_by_profile=mapping, fit_evidence_by_account=fit)
        self.assertIn("injected request failure", str(failure.exception.__cause__))
        self.assertEqual(needs_fit, self.repo.get_state(profile_kind("cs-a"), key))
        self.assertEqual(checkpoint, self.repo.get_state(LIVE_CHECKPOINT, "cs-a"))
        self.assertIsNone(self.repo.get_state(research_request_kind("cs-a"), key))


if __name__ == "__main__":
    unittest.main()
