import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from thunder_os.db import Repository, RepositoryConflict
from thunder_os.models import WorkStatus, parse_work_item


class RepositoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "painel.db"

    def tearDown(self):
        self.temp.cleanup()

    def item(self):
        return parse_work_item({"id": "w1", "objective": "Pesquisar", "actor": "hermes", "idempotency_key": "key-1"})

    def test_migration_is_additive_idempotent_and_preserves_existing_data(self):
        con = sqlite3.connect(self.path)
        con.execute("CREATE TABLE itens (id TEXT PRIMARY KEY, titulo TEXT)")
        con.execute("INSERT INTO itens VALUES ('old', 'Preservar')")
        con.commit()
        con.close()
        repo = Repository(self.path)
        repo.migrate()
        repo.migrate()
        con = sqlite3.connect(self.path)
        self.assertEqual(("Preservar",), con.execute("SELECT titulo FROM itens WHERE id='old'").fetchone())
        self.assertEqual(1, con.execute("SELECT version FROM thunder_schema").fetchone()[0])
        con.close()

    def test_save_persists_item_event_and_outbox_atomically_across_restart(self):
        repo = Repository(self.path)
        saved = repo.save_with_event(self.item(), expected_version=0, event_type="work_requested", outbox_kind="agent_job")
        reopened = Repository(self.path)
        self.assertEqual(saved, reopened.get_work("w1"))
        self.assertEqual("work_requested", reopened.events("w1")[0]["type"])
        self.assertEqual("agent_job", reopened.outbox()[0]["kind"])

    def test_compare_and_set_rejects_stale_and_concurrent_writer(self):
        repo = Repository(self.path)
        repo.save_with_event(self.item(), 0, "created")
        updated = self.item().__class__(**{**self.item().__dict__, "status": WorkStatus.IN_PROGRESS, "version": 2})
        barrier = threading.Barrier(2)
        results = []

        def write():
            local = Repository(self.path)
            barrier.wait()
            try:
                local.save_with_event(updated, 1, "started")
                results.append("saved")
            except RepositoryConflict:
                results.append("conflict")

        threads = [threading.Thread(target=write) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertCountEqual(["saved", "conflict"], results)
        with self.assertRaises(RepositoryConflict):
            repo.save_with_event(updated, 1, "started")
        history = repo.events("w1")
        self.assertEqual([event["type"] for event in history], ["created", "started"])
        self.assertEqual([event["payload"]["version"] for event in history], [1, 2])

    def manual_fixture(self):
        from thunder_os.models import payload_hash
        repo = Repository(self.path)
        job = repo.enqueue_effect('synthetic-manual', {'cadence_id': 'internal-canary-synthetic',
                                  'step': {'id': 'e1', 'channel': 'email'}})
        repo.finish_effect(job['id'], 'uncertain', None, 'assisted_receipt_pending')
        job = repo.effect(job['id'])
        row = repo.put_state('codex_assisted', job['id'], {'phase': 'dispatch_issued',
                             'ticket': {'synthetic': True}, 'grant': {'synthetic': True}})
        record = {'schema': 'internal_operator_receipt_v1', 'proof': {'job_id': job['id']},
                  'job_payload_hash': payload_hash(job['payload']),
                  'dispatch_authorized': False, 'retry_write': False}
        return repo, job, row, record

    def test_proven_not_sent_cas_has_one_winner_and_one_durable_event(self):
        repo, job, row, record = self.manual_fixture()
        barrier = threading.Barrier(2)
        def reconcile():
            local = Repository(self.path)
            barrier.wait()
            try:
                local.reconcile_proven_not_sent(job, row, record)
                return 'saved'
            except RepositoryConflict:
                return 'conflict'
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: reconcile(), range(2)))
        self.assertCountEqual(results, ['saved', 'conflict'])
        self.assertEqual(repo.effect(job['id'])['state'], 'failed')
        self.assertEqual(repo.effect(job['id'])['error_code'], 'proved_not_sent')
        self.assertEqual(len(repo.events(job['id'])), 1)
        self.assertEqual(repo.events(job['id'])[0]['payload']['previous_effect'], job)
        with self.assertRaises(RepositoryConflict):
            repo.confirm_uncertain_receipt(repo.effect(job['id']), 'gmail:abc123')

    def test_proven_not_sent_audit_failure_rolls_back_job_and_state(self):
        from unittest.mock import patch
        repo, job, row, record = self.manual_fixture()
        with patch.object(repo, 'append_event', side_effect=RuntimeError('synthetic journal failure')):
            with self.assertRaises(RuntimeError):
                repo.reconcile_proven_not_sent(job, row, record)
        self.assertEqual(repo.effect(job['id']), job)
        self.assertEqual(repo.get_state('codex_assisted', job['id']), row)
        self.assertEqual(repo.events(job['id']), [])

    def test_operator_receipt_requires_transaction_exact_snapshot_and_audit(self):
        repo, job, row, record = self.manual_fixture()
        repo.reconcile_proven_not_sent(job, row, record)
        failed = repo.effect(job['id'])
        with self.assertRaises(RepositoryConflict):
            repo.validate_operator_receipt(failed, 'gmail:abc123')
        with repo.transaction():
            self.assertEqual(repo.validate_operator_receipt(failed, 'gmail:abc123'), failed)
            with self.assertRaises(RepositoryConflict):
                repo.validate_operator_receipt(job, 'gmail:abc123')
        sent = repo.confirm_operator_receipt(failed, 'gmail:abc123')
        # Service owns phase + canonical cadence update in the same outer transaction.
        current = repo.get_state('codex_assisted', job['id'])
        repo.put_state('codex_assisted', job['id'], {**current, 'phase': 'confirmed', 'receipt': 'gmail:abc123'})
        self.assertEqual(repo.confirm_operator_receipt(sent, 'gmail:abc123'), sent)
        self.assertEqual(len(repo.events(job['id'])), 2)
        with self.assertRaises(RepositoryConflict):
            repo.confirm_operator_receipt(sent, 'gmail:abc124')
        self.assertEqual(repo.effect(job['id'])['receipt_ref'], 'gmail:abc123')

    def test_ordinary_failed_or_spoofed_manual_record_is_never_operator_receipt(self):
        repo, job, row, record = self.manual_fixture()
        with repo.transaction() as con:
            con.execute("UPDATE thunder_effect_jobs SET state='failed',error_code='proved_not_sent' WHERE id=?", (job['id'],))
            repo.put_state('codex_assisted', job['id'], {**row, 'phase': 'proved_not_sent',
                           'manual_reconciliation': record})
        with self.assertRaisesRegex(RepositoryConflict, 'proof missing'):
            repo.confirm_operator_receipt(repo.effect(job['id']), 'gmail:abc123')
        with self.assertRaises(RepositoryConflict):
            repo.confirm_uncertain_receipt(repo.effect(job['id']), 'gmail:abc123')

    def test_expired_lease_is_recovered_but_active_lease_is_not(self):
        repo = Repository(self.path)
        repo.save_with_event(self.item(), 0, "created", outbox_kind="agent_job")
        now = datetime(2026, 9, 29, tzinfo=timezone.utc)
        first = repo.claim_outbox("worker-a", now, timedelta(seconds=10))
        self.assertTrue(first["lease_owner"].startswith("worker-a:"))
        self.assertIsNone(repo.claim_outbox("worker-b", now + timedelta(seconds=5), timedelta(seconds=10)))
        recovered = repo.claim_outbox("worker-b", now + timedelta(seconds=11), timedelta(seconds=10))
        self.assertEqual(first["id"], recovered["id"])
        self.assertTrue(recovered["lease_owner"].startswith("worker-b:"))
        self.assertNotEqual(first["lease_owner"], recovered["lease_owner"])

    def test_delegated_daily_ceiling_is_atomic_across_concurrent_requests(self):
        repo = Repository(self.path)
        repo.put_state("delegation", "hermes", {
            "enabled": True, "scopes": ["followup"], "daily_limit": 1,
        })
        barrier = threading.Barrier(2)
        results = []

        def reserve():
            local = Repository(self.path)
            barrier.wait()
            try:
                results.append(local.reserve_delegated_action("hermes", "followup", "2026-09-29"))
            except RepositoryConflict:
                results.append("blocked")

        threads = [threading.Thread(target=reserve) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertCountEqual([1, "blocked"], results)
        self.assertEqual(repo.get_state("delegation_usage", "hermes:2026-09-29")["count"], 1)


if __name__ == "__main__":
    unittest.main()
