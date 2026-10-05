import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from thunder_os.connection_backfill import record_backfill_batch
from thunder_os.connection_intake import PROVIDER_CALENDAR, profile_key
from thunder_os.connection_ledger import profile_kind, research_request_kind
from thunder_os.connection_work import enqueue_connection_research
from thunder_os.continuous_consumer import ContinuousConsumer, EligibleRepository
from thunder_os.db import Repository, RepositoryConflict
from thunder_os.models import ContractError
from thunder_os.service import ThunderService


ACCOUNT = "cs-fixture"
PROFILE = profile_key("https://linkedin.com/in/research-person")
COMPANY = "account.example"
NOW = datetime.now(timezone.utc)


class Owner:
    def __init__(self):
        self.paused = False

    def block_reason(self):
        return "agent_paused" if self.paused else None


class ConnectionWorkerTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.path = Path(self.scratch.name) / "state.sqlite"
        self.repo = Repository(self.path)
        self.service = ThunderService(self.repo)
        self.owner = Owner()
        self.eligible = EligibleRepository(self.repo, self.owner)
        self.repo.put_state("agent_control", "global", {
            "enabled": True, "paused": False,
            "activation_at": (NOW - timedelta(days=1)).isoformat(),
        })
        row = {"profileUrl": PROFILE, "connectedDate": "Conexão feita em 4 de outubro de 2026"}
        def fetch(cursor):
            rows = [row][cursor:cursor + 10]
            return {"success": True, "accountId": ACCOUNT, "sortBy": "recentlyAdded",
                    "startIndex": cursor, "connections": rows, "count": len(rows),
                    "nextStartIndex": cursor + len(rows), "endOfList": True}
        record_backfill_batch(self.repo, fetch, account_id=ACCOUNT, today=date(2026, 10, 5),
                              account_by_profile={PROFILE: COMPANY},
                              fit_evidence_by_account={COMPANY: "catalogue/decision-1"})

    def enqueue(self):
        return enqueue_connection_research(self.service,
                                           connectsafely_account_id=ACCOUNT, profile=PROFILE)

    def claim(self):
        return self.eligible.claim_outbox("local-test", NOW, timedelta(seconds=30), kind="agent_job")

    def enqueue_backlog(self, count):
        items = [self.enqueue()]
        for number in range(1, count):
            profile = profile_key(f"https://linkedin.com/in/research-person-{number}")
            self.repo.append_state(profile_kind(ACCOUNT), profile, {
                "profile": profile, "connectsafely_account_id": ACCOUNT,
                "account_id": COMPANY, "status": "research", "source": "connectsafely_backfill",
            })
            self.repo.append_state(research_request_kind(ACCOUNT), profile, {
                "profile": profile, "connectsafely_account_id": ACCOUNT,
                "account_id": COMPANY, "status": "pending", "source": "historical_backfill",
                "fit_evidence": "catalogue/decision-1",
            })
            items.append(enqueue_connection_research(
                self.service, connectsafely_account_id=ACCOUNT, profile=profile))
        return items

    @staticmethod
    def local_midnight_boundary():
        local_day = NOW.astimezone(PROVIDER_CALENDAR).date()
        before = datetime.combine(local_day, time(23, 59), PROVIDER_CALENDAR)
        return before, before + timedelta(minutes=2)

    def test_restart_keeps_one_job(self):
        first = self.enqueue()
        self.assertEqual("connection-monitor", first.actor)
        self.assertEqual("research", first.context["capability"])
        self.assertEqual(COMPANY, first.context["account_id"])
        self.assertEqual(PROFILE, first.context["connection_profile"])
        self.assertEqual(ACCOUNT, first.context["connectsafely_account_id"])
        reopened = ThunderService(Repository(self.path))
        second = enqueue_connection_research(reopened,
                                             connectsafely_account_id=ACCOUNT, profile=PROFILE)
        self.assertEqual(first.id, second.id)
        self.assertEqual(1, len(self.repo.outbox()))
        lease = self.claim()
        self.assertIsNotNone(lease)
        self.assertEqual(first.id, lease["entity_id"])
        restarted = EligibleRepository(Repository(self.path), self.owner)
        self.assertIsNone(restarted.claim_outbox("restarted", NOW, timedelta(seconds=30), kind="agent_job"))
        self.repo.finish_outbox(lease["id"], lease["lease_owner"])
        self.assertIsNone(restarted.claim_outbox("restarted", NOW + timedelta(seconds=31),
                                                timedelta(seconds=30), kind="agent_job"))
        self.assertEqual(1, len(self.repo.outbox()))

    def test_duplicate_enqueue_and_missing_fit(self):
        self.assertEqual(self.enqueue().id, self.enqueue().id)
        self.assertEqual(1, len(self.repo.outbox()))
        self.repo.put_state(research_request_kind(ACCOUNT), PROFILE, {
            "profile": PROFILE, "connectsafely_account_id": ACCOUNT,
            "account_id": COMPANY, "status": "pending", "source": "exact_profile_mapping",
        })
        self.repo.put_state(profile_kind(ACCOUNT), PROFILE, {
            "profile": PROFILE, "connectsafely_account_id": ACCOUNT,
            "account_id": COMPANY, "status": "research", "source": "connectsafely",
        })
        with self.assertRaisesRegex(ContractError, "fit evidence"):
            self.enqueue()
        self.assertIsNone(self.claim())

    def test_forged_actor_and_context_cannot_claim(self):
        forged_exact = self.service.request_work({"objective": "Pesquisar conta", "context": {
            "capability": "research", "account_id": COMPANY,
            "connectsafely_account_id": ACCOUNT, "connection_profile": PROFILE,
            "connection_request_version": 1,
        }}, "connection-monitor", "forged-exact")
        self.assertIsNone(self.claim())
        forged = self.service.request_work({"objective": "Pesquisar conta", "context": {
            "capability": "copy", "account_id": COMPANY,
            "connectsafely_account_id": ACCOUNT, "connection_profile": PROFILE,
            "connection_request_version": 1,
        }}, "connection-monitor", "forged-copy")
        self.assertEqual("connection-monitor", forged.actor)
        self.assertIsNone(self.claim())
        wrong_account = self.service.request_work({"objective": "Pesquisar conta", "context": {
            "capability": "research", "account_id": "other.example",
            "connectsafely_account_id": ACCOUNT, "connection_profile": PROFILE,
            "connection_request_version": 1,
        }}, "connection-monitor", "forged-account")
        self.assertIsNone(self.claim())
        self.assertEqual(3, len(self.repo.outbox()))
        self.assertEqual("requested", self.repo.get_work(forged_exact.id).status.value)
        self.assertEqual("requested", self.repo.get_work(wrong_account.id).status.value)
        legitimate = self.enqueue()
        self.assertEqual(legitimate.id, self.claim()["entity_id"])

    def test_paused_gate(self):
        self.enqueue()
        with patch.dict("os.environ", {"THUNDER_EXTERNAL_EFFECTS_ENABLED": "false",
                                     "THUNDER_OS_EXTERNAL_EFFECTS_ENABLED": "false"}):
            consumer = ContinuousConsumer(self.service, evidence_loader=lambda _: None,
                                          transport_factory=lambda *_: None)
            self.repo.put_state("agent_control", "global", {
                "enabled": True, "paused": True,
                "activation_at": (NOW - timedelta(days=1)).isoformat(),
            })
            eligible = EligibleRepository(self.repo, consumer)
            self.assertIsNone(eligible.claim_outbox("paused", NOW, timedelta(seconds=30), kind="agent_job"))
            self.assertIsNone(self.repo.outbox()[0]["lease_owner"])
            self.assertEqual([], self.repo.list_state("connection_research_execution_day"))
            self.repo.put_state("agent_control", "global", {
                "enabled": True, "paused": False,
                "activation_at": (NOW - timedelta(days=1)).isoformat(),
            })
            self.assertIsNotNone(eligible.claim_outbox("active", NOW, timedelta(seconds=30), kind="agent_job"))

    def test_execution_budget_defers_backlog_until_next_sao_paulo_day(self):
        items = self.enqueue_backlog(7)
        before, after = self.local_midnight_boundary()
        leases = [self.eligible.claim_outbox("day-one", before.astimezone(timezone.utc),
                                              timedelta(seconds=30), kind="agent_job")
                  for _ in range(5)]
        self.assertEqual(5, len({lease["entity_id"] for lease in leases}))
        self.assertIsNone(self.eligible.claim_outbox("day-one", before.astimezone(timezone.utc),
                                                   timedelta(seconds=30), kind="agent_job"))
        day_one = before.date().isoformat()
        self.assertEqual(5, len(self.repo.get_state("connection_research_execution_day", day_one)["work_ids"]))
        self.assertEqual(2, len([entry for entry in self.repo.outbox() if entry["lease_owner"] is None]))
        for lease in leases:
            self.repo.finish_outbox(lease["id"], lease["lease_owner"])
        for item in items[5:]:
            self.assertEqual("requested", self.repo.get_work(item.id).status.value)
            self.assertIsNone(self.repo.get_state("agent_usage", item.id))
        resumed = [self.eligible.claim_outbox("day-two", after.astimezone(timezone.utc),
                                               timedelta(seconds=30), kind="agent_job")
                   for _ in range(2)]
        self.assertEqual({item.id for item in items[5:]}, {lease["entity_id"] for lease in resumed})
        self.assertEqual(2, len(self.repo.get_state("connection_research_execution_day", after.date().isoformat())["work_ids"]))

    def test_budget_is_atomic_across_workers_and_does_not_limit_ygor(self):
        self.enqueue_backlog(7)
        before, _ = self.local_midnight_boundary()
        instant = before.astimezone(timezone.utc)
        repositories = [EligibleRepository(Repository(self.path), self.owner) for _ in range(7)]
        def claim_from(index):
            return repositories[index].claim_outbox(f"worker-{index}", instant,
                                                       timedelta(seconds=30), kind="agent_job")
        with ThreadPoolExecutor(max_workers=7) as pool:
            claims = list(pool.map(claim_from, range(7)))
        claimed = [claim for claim in claims if claim is not None]
        self.assertEqual(5, len(claimed))
        self.assertEqual(5, len({claim["entity_id"] for claim in claimed}))
        self.assertEqual(5, len(self.repo.get_state("connection_research_execution_day", before.date().isoformat())["work_ids"]))
        human = self.service.request_work({"objective": "Pesquisar conta", "context": {
            "capability": "research", "account_id": COMPANY,
        }}, "ygor", "human-after-cap")
        self.assertEqual(human.id, self.eligible.claim_outbox(
            "human", instant, timedelta(seconds=30), kind="agent_job")["entity_id"])

    def test_expired_lease_does_not_charge_twice_same_day(self):
        item = self.enqueue()
        before, _ = self.local_midnight_boundary()
        instant = before.astimezone(timezone.utc)
        first = self.eligible.claim_outbox("crashed", instant, timedelta(seconds=30), kind="agent_job")
        second = EligibleRepository(Repository(self.path), self.owner).claim_outbox(
            "restarted", instant + timedelta(seconds=31), timedelta(seconds=30), kind="agent_job")
        self.assertEqual(item.id, first["entity_id"])
        self.assertEqual(item.id, second["entity_id"])
        self.assertEqual([item.id], self.repo.get_state(
            "connection_research_execution_day", before.date().isoformat())["work_ids"])

    def test_budget_write_failure_rolls_back_lease(self):
        self.enqueue()
        with sqlite3.connect(self.path) as con:
            con.execute("""CREATE TRIGGER reject_execution_budget BEFORE INSERT ON thunder_state
                WHEN NEW.kind='connection_research_execution_day'
                BEGIN SELECT RAISE(ABORT, 'injected budget failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected budget failure"):
            self.claim()
        self.assertIsNone(self.repo.outbox()[0]["lease_owner"])
        self.assertEqual([], self.repo.list_state("connection_research_execution_day"))

    def test_request_must_match_profile_and_origin(self):
        with self.assertRaisesRegex(ContractError, "canonical connection"):
            enqueue_connection_research(self.service, connectsafely_account_id="other", profile=PROFILE)
        self.repo.put_state(profile_kind(ACCOUNT), PROFILE, {
            "profile": PROFILE, "connectsafely_account_id": ACCOUNT,
            "account_id": "other.example", "status": "research",
        })
        with self.assertRaisesRegex(ContractError, "canonical connection"):
            self.enqueue()
        self.assertEqual([], self.repo.outbox())

    def test_non_connection_jobs_keep_ygor_only_rule(self):
        other = self.service.request_work({"objective": "Pesquisar conta", "context": {
            "capability": "research", "account_id": COMPANY,
        }}, "hermes", "other-actor")
        self.assertIsNone(self.claim())
        human = self.service.request_work({"objective": "Pesquisar conta", "context": {
            "capability": "copy", "account_id": COMPANY,
        }}, "ygor", "human-request")
        self.assertEqual(human.id, self.claim()["entity_id"])
        self.assertEqual("requested", self.repo.get_work(other.id).status.value)


if __name__ == "__main__":
    unittest.main()
