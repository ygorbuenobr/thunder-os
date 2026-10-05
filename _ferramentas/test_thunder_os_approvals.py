import unittest
from datetime import datetime, timedelta, timezone

from thunder_os.approvals import ApprovalError, ApprovalService


def step(step_id="s1", **changes):
    value = {
        "id": step_id,
        "recipient": "person-1",
        "channel": "linkedin",
        "content": "Mensagem exata",
        "order": 1,
        "interval_hours": 0,
    }
    value.update(changes)
    return value


class ApprovalServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 29, 15, tzinfo=timezone.utc)
        self.service = ApprovalService(lambda: False)

    def test_pack_binds_exact_scope_actor_and_expiry(self):
        pack = self.service.approve_pack(
            "cad-1", 3, [step()], "ygor", self.now + timedelta(hours=2), self.now
        )
        self.assertEqual(pack.actor, "ygor")
        self.assertEqual(pack.cadence_version, 3)
        self.assertEqual(pack.step_ids, ("s1",))
        self.assertEqual(self.service.authorize_step(step(), pack, self.now).step_id, "s1")

    def test_exact_scope_mutation_requires_new_review(self):
        pack = self.service.approve_pack(
            "cad-1", 1, [step()], "ygor", self.now + timedelta(hours=2), self.now
        )
        for field, value in (
            ("recipient", "person-2"),
            ("channel", "email"),
            ("content", "Mudou"),
            ("order", 2),
            ("interval_hours", 24),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(ApprovalError, "scope changed"):
                self.service.authorize_step(step(**{field: value}), pack, self.now)

    def test_selective_invalidation_keeps_unchanged_future_steps(self):
        pack = self.service.approve_pack(
            "cad-1", 1, [step(), step("s2", order=2, interval_hours=24)],
            "ygor", self.now + timedelta(hours=2), self.now,
        )
        changed = self.service.invalidate_affected(pack, ["s2"])
        self.assertEqual(changed.status, "partially_invalidated")
        self.assertEqual(changed.invalidated_step_ids, ("s2",))
        self.assertEqual(self.service.authorize_step(step(), changed, self.now).step_id, "s1")
        with self.assertRaisesRegex(ApprovalError, "invalidated"):
            self.service.authorize_step(step("s2", order=2, interval_hours=24), changed, self.now)

    def test_expired_pack_fails_closed(self):
        pack = self.service.approve_pack(
            "cad-1", 1, [step()], "ygor", self.now + timedelta(seconds=1), self.now
        )
        with self.assertRaisesRegex(ApprovalError, "expired"):
            self.service.authorize_step(step(), pack, self.now + timedelta(seconds=2))

    def test_global_pause_fails_closed_at_approval_and_execution(self):
        paused = ApprovalService(lambda: True)
        with self.assertRaisesRegex(ApprovalError, "global pause"):
            paused.approve_pack("cad-1", 1, [step()], "ygor", self.now + timedelta(hours=1), self.now)
        pack = self.service.approve_pack(
            "cad-1", 1, [step()], "ygor", self.now + timedelta(hours=1), self.now
        )
        with self.assertRaisesRegex(ApprovalError, "global pause"):
            paused.authorize_step(step(), pack, self.now)


if __name__ == "__main__":
    unittest.main()
