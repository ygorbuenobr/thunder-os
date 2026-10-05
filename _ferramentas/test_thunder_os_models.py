import unittest

from thunder_os.models import (
    ALLOWED_WORK_TRANSITIONS,
    Channel,
    ContractError,
    StaleVersionError,
    WorkStatus,
    effect_payload_hash,
    parse_work_item,
    payload_hash,
    transition,
)


class DomainContractTests(unittest.TestCase):
    def item(self, status=WorkStatus.REQUESTED):
        return parse_work_item(
            {
                "id": "work-1",
                "objective": "Pesquisar conta",
                "actor": "hermes",
                "idempotency_key": "req-1",
                "status": status.value,
            }
        )

    def test_rejects_unknown_status_field_and_version(self):
        with self.assertRaisesRegex(ContractError, "unknown fields"):
            parse_work_item({"id": "1", "objective": "x", "actor": "a", "idempotency_key": "k", "extra": 1})
        with self.assertRaisesRegex(ContractError, "invalid status"):
            parse_work_item({"id": "1", "objective": "x", "actor": "a", "idempotency_key": "k", "status": "sent"})
        with self.assertRaisesRegex(ContractError, "invalid version"):
            parse_work_item({"id": "1", "objective": "x", "actor": "a", "idempotency_key": "k", "version": 0})

    def test_payload_hash_is_canonical_and_sensitive_to_effect_fields(self):
        self.assertEqual(payload_hash({"b": 2, "a": 1}), payload_hash({"a": 1, "b": 2}))
        base = dict(target="person-1", channel=Channel.EMAIL, subject="Assunto", content="Olá", order=1, interval_hours=24)
        original = effect_payload_hash(**base)
        for field, value in (("target", "person-2"), ("subject", "Outro"), ("content", "Oi"), ("order", 2), ("interval_hours", 48)):
            changed = {**base, field: value}
            self.assertNotEqual(original, effect_payload_hash(**changed), field)
        linkedin = {**base, "channel": "linkedin"}
        linkedin.pop("subject")
        self.assertNotEqual(original, effect_payload_hash(**linkedin), "channel")

    def test_every_allowed_transition_increments_version(self):
        for source, targets in ALLOWED_WORK_TRANSITIONS.items():
            for target in targets:
                updated = transition(self.item(source), 1, target)
                self.assertEqual(target, updated.status)
                self.assertEqual(2, updated.version)

    def test_traceable_work_contract_keeps_context_owner_state_and_time(self):
        item = parse_work_item({
            "id": "work-1", "objective": "Pesquisar conta", "actor": "hermes",
            "idempotency_key": "key", "context": {"account": "acme.example"},
            "logical_owner": "hermes", "created_at": "2026-09-29T10:00:00+00:00",
            "updated_at": "2026-09-29T10:00:00+00:00",
        })
        self.assertEqual(
            (item.objective, item.context, item.logical_owner, item.status.value, item.created_at),
            ("Pesquisar conta", {"account": "acme.example"}, "hermes", "requested", "2026-09-29T10:00:00+00:00"),
        )

    def test_invalid_and_stale_transitions_are_rejected(self):
        with self.assertRaisesRegex(ContractError, "invalid transition"):
            transition(self.item(WorkStatus.SUCCEEDED), 1, WorkStatus.IN_PROGRESS)
        with self.assertRaisesRegex(StaleVersionError, "stale version"):
            transition(self.item(), 2, WorkStatus.IN_PROGRESS)


if __name__ == "__main__":
    unittest.main()
