import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from thunder_os.connection_company import (
    observe_pending_companies, resolution_kind,
)
from thunder_os.connection_ledger import profile_kind
from thunder_os.connection_provider import ConnectionProviderError
from thunder_os.db import Repository


ACCOUNT = "a" * 24
PROFILE = "https://www.linkedin.com/in/person-one"
NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
CATALOG = {"alpha logistics": ("alpha.example", "brain:contas/alpha/ficha.md#sha256:test")}


class ConnectionCompanyTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.repo = Repository(Path(self.scratch.name) / "state.db")
        self.repo.append_state(profile_kind(ACCOUNT), PROFILE, {
            "profile": PROFILE, "connectsafely_account_id": ACCOUNT,
            "status": "needs_identity", "source": "connectsafely_backfill",
            "connected_at": "2026-10-05T00:00:00-03:00",
        })

    def test_exact_consistent_company_is_only_a_candidate(self):
        calls = []
        def fetch(profile):
            calls.append(profile)
            return "Alpha Logistics", "Alpha Logistics"
        result = observe_pending_companies(self.repo, ACCOUNT, CATALOG, fetch, NOW)
        self.assertEqual({"checked": 1, "candidates": 1, "error": None}, result)
        candidate = self.repo.get_state(resolution_kind(ACCOUNT), PROFILE)
        self.assertEqual("candidate", candidate["status"])
        self.assertEqual("alpha.example", candidate["candidate_domain"])
        self.assertNotIn("account_id", candidate)
        self.assertEqual("needs_identity", self.repo.get_state(profile_kind(ACCOUNT), PROFILE)["status"])
        self.assertEqual(1, len(calls))
        self.assertEqual({"checked": 0, "candidates": 0, "error": None},
                         observe_pending_companies(self.repo, ACCOUNT, CATALOG, fetch, NOW))
        self.assertEqual(1, len(calls))

    def test_inconsistent_or_unknown_company_stays_unresolved(self):
        result = observe_pending_companies(self.repo, ACCOUNT, CATALOG,
                                           lambda _: ("Alpha Logistics", "Different Co"), NOW)
        self.assertEqual(0, result["candidates"])
        self.assertEqual("needs_identity", self.repo.get_state(resolution_kind(ACCOUNT), PROFILE)["status"])

    def test_changed_catalog_revokes_candidate_and_error_skips_same_day(self):
        def failure(_):
            raise ConnectionProviderError("provider_rate_limited")
        result = observe_pending_companies(self.repo, ACCOUNT, CATALOG, failure, NOW)
        self.assertEqual("provider_rate_limited", result["error"])
        self.assertEqual("lookup_error", self.repo.get_state(resolution_kind(ACCOUNT), PROFILE)["status"])
        self.assertEqual(0, observe_pending_companies(
            self.repo, ACCOUNT, CATALOG, lambda _: self.fail("repeated same-day lookup"), NOW)["checked"])
        tomorrow = NOW.replace(day=7)
        observe_pending_companies(self.repo, ACCOUNT, CATALOG,
                                  lambda _: ("Alpha Logistics", "Alpha Logistics"), tomorrow)
        self.assertEqual("candidate", self.repo.get_state(resolution_kind(ACCOUNT), PROFILE)["status"])
        observe_pending_companies(self.repo, ACCOUNT, {},
                                  lambda _: self.fail("candidate refetched"), tomorrow)
        self.assertEqual("needs_identity", self.repo.get_state(resolution_kind(ACCOUNT), PROFILE)["status"])
        self.assertNotIn("candidate_domain", self.repo.get_state(resolution_kind(ACCOUNT), PROFILE))

    def test_failed_first_profile_does_not_starve_second(self):
        other = "https://www.linkedin.com/in/person-two"
        self.repo.append_state(profile_kind(ACCOUNT), other, {
            "profile": other, "connectsafely_account_id": ACCOUNT,
            "status": "needs_identity", "source": "connectsafely_backfill",
            "connected_at": "2026-10-05T00:00:00-03:00",
        })
        calls = []
        def fetch(profile):
            calls.append(profile)
            if profile == PROFILE:
                raise ConnectionProviderError("provider_timeout")
            return "Alpha Logistics", "Alpha Logistics"
        result = observe_pending_companies(self.repo, ACCOUNT, CATALOG, fetch, NOW)
        self.assertEqual(2, result["checked"])
        self.assertEqual(1, result["candidates"])
        self.assertEqual("provider_timeout", result["error"])
        self.assertEqual([PROFILE, other], calls)
        self.assertEqual("lookup_error", self.repo.get_state(resolution_kind(ACCOUNT), PROFILE)["status"])
        self.assertEqual("candidate", self.repo.get_state(resolution_kind(ACCOUNT), other)["status"])

    def test_rate_limit_blocks_later_rounds_for_account_day(self):
        other = "https://www.linkedin.com/in/person-two"
        self.repo.append_state(profile_kind(ACCOUNT), other, {
            "profile": other, "connectsafely_account_id": ACCOUNT,
            "status": "needs_identity", "source": "connectsafely_backfill",
            "connected_at": "2026-10-05T00:00:00-03:00",
        })
        calls = []
        def limited(profile):
            calls.append(profile)
            raise ConnectionProviderError("provider_rate_limited")
        first = observe_pending_companies(self.repo, ACCOUNT, CATALOG, limited, NOW)
        second = observe_pending_companies(self.repo, ACCOUNT, CATALOG, limited, NOW)
        self.assertEqual("provider_rate_limited", first["error"])
        self.assertEqual({"checked": 0, "candidates": 0,
                          "error": "provider_rate_limited"}, second)
        self.assertEqual([PROFILE], calls)

    def test_process_interruption_after_reserved_get_blocks_next_round(self):
        calls = []
        def interrupted(profile):
            calls.append(profile)
            raise SystemExit("simulated interruption after provider call")
        with self.assertRaises(SystemExit):
            observe_pending_companies(self.repo, ACCOUNT, CATALOG, interrupted, NOW)
        self.assertEqual({"checked": 0, "candidates": 0,
                          "error": "company_lookup_interrupted"},
                         observe_pending_companies(self.repo, ACCOUNT, CATALOG,
                                                   lambda _: self.fail("second GET"), NOW))
        self.assertEqual([PROFILE], calls)


if __name__ == "__main__":
    unittest.main()
