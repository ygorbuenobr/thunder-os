import unittest
from datetime import datetime, timezone

from thunder_os.cadence import CadenceError, CadenceEngine, calculate_due, changed_future_steps


class CadenceEngineTests(unittest.TestCase):
    def test_due_uses_confirmed_dependency_completion_and_order(self):
        confirmed = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
        due = calculate_due(confirmed, 24)
        self.assertEqual(due.isoformat(), "2026-09-30T12:00:00+00:00")
        result = CadenceEngine().evaluate(
            {"id": "s2", "channel": "email", "due_at": due, "dependency_status": "sent"}, due
        )
        self.assertEqual(result, "due")

    def test_weekend_moves_to_next_business_day_in_sao_paulo(self):
        friday = datetime(2026, 10, 2, 18, tzinfo=timezone.utc)  # 15:00 local
        due = calculate_due(friday, 24)
        self.assertEqual(due.isoformat(), "2026-10-05T18:00:00+00:00")

    def test_timezone_conversion_is_aware_at_historical_dst_boundary(self):
        before_dst_end = datetime(2018, 2, 17, 14, tzinfo=timezone.utc)
        due = calculate_due(before_dst_end, 24)
        self.assertEqual(due.tzinfo, timezone.utc)
        # 11:00 local permanece 11:00 após o fim do DST, portanto passa a 15:00 UTC.
        self.assertEqual(due.isoformat(), "2018-02-19T15:00:00+00:00")

    def test_reply_optout_suppression_failure_and_uncertainty_block(self):
        engine = CadenceEngine()
        now = datetime(2026, 9, 29, tzinfo=timezone.utc)
        base = {"id": "s2", "channel": "linkedin", "due_at": now, "dependency_status": "sent"}
        for flag in ("reply", "opt_out", "suppressed"):
            with self.subTest(flag=flag):
                self.assertEqual(engine.evaluate(base, now, **{flag: True}), "blocked")
        for status in ("failed", "uncertain"):
            with self.subTest(status=status):
                self.assertEqual(engine.evaluate({**base, "dependency_status": status}, now), "blocked")

    def test_unavailable_channel_needs_human_without_substitution(self):
        now = datetime(2026, 9, 29, tzinfo=timezone.utc)
        step = {"id": "s1", "channel": "email", "due_at": now, "dependency_status": "sent"}
        self.assertEqual(CadenceEngine({"linkedin"}).evaluate(step, now), "needs_human")
        self.assertEqual(step["channel"], "email")

    def test_policy_conflict_needs_human(self):
        now = datetime(2026, 9, 29, tzinfo=timezone.utc)
        step = {"id": "s1", "channel": "linkedin", "due_at": now, "dependency_status": "sent"}
        self.assertEqual(CadenceEngine().evaluate(step, now, policy_conflict=True), "needs_human")

    def test_reply_pauses_all_future_steps(self):
        steps = [{"id": "s1", "status": "sent"}, {"id": "s2", "status": "approved"}]
        paused = CadenceEngine().pause_for_reply(steps)
        self.assertEqual(paused, [{"id": "s1", "status": "sent"}, {"id": "s2", "status": "skipped_reply"}])

    def test_scope_change_identifies_only_changed_future_steps(self):
        old = [
            {"id": "s1", "recipient": "p", "channel": "linkedin", "content": "a", "order": 1, "interval_hours": 0},
            {"id": "s2", "recipient": "p", "channel": "email", "subject": "B", "content": "b", "order": 2, "interval_hours": 24},
        ]
        new = [old[0], {**old[1], "content": "c"}]
        self.assertEqual(changed_future_steps(old, new), ("s2",))
        with self.assertRaisesRegex(CadenceError, "step ids"):
            changed_future_steps(old, [old[0]])


if __name__ == "__main__":
    unittest.main()
