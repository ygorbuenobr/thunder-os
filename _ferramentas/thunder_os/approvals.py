"""Exact, versioned human approval for cadence effects."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

from .models import ContractError, effect_payload_hash


class ApprovalError(ContractError):
    pass


@dataclass(frozen=True)
class ApprovalPack:
    id: str
    cadence_id: str
    cadence_version: int
    actor: str
    approved_at: datetime
    expires_at: datetime
    step_hashes: tuple[tuple[str, str], ...]
    status: str = "active"
    invalidated_step_ids: tuple[str, ...] = ()

    @property
    def step_ids(self) -> tuple[str, ...]:
        return tuple(step_id for step_id, _ in self.step_hashes)


@dataclass(frozen=True)
class Authorization:
    pack_id: str
    step_id: str
    actor: str
    effect_hash: str
    expires_at: datetime


def _step_hash(step: Mapping[str, Any]) -> str:
    required = {"id", "recipient", "channel", "content", "order", "interval_hours"}
    allowed = required | ({"subject"} if step.get("channel") == "email" else set())
    if set(step) != allowed:
        raise ApprovalError("invalid approval step")
    return effect_payload_hash(
        target=step["recipient"], channel=step["channel"], content=step["content"],
        order=step["order"], interval_hours=step["interval_hours"], subject=step.get("subject"),
    )


class ApprovalService:
    def __init__(self, global_pause: Callable[[], bool]):
        self.global_pause = global_pause

    def approve_pack(
        self, cadence_id: str, cadence_version: int, steps: Sequence[Mapping[str, Any]],
        actor: str, expires_at: datetime, now: datetime,
    ) -> ApprovalPack:
        if self.global_pause():
            raise ApprovalError("global pause active")
        if not actor.strip() or cadence_version < 1 or not steps or expires_at <= now:
            raise ApprovalError("invalid approval pack")
        hashes = tuple((str(item["id"]), _step_hash(item)) for item in steps)
        if len({step_id for step_id, _ in hashes}) != len(hashes):
            raise ApprovalError("duplicate step")
        return ApprovalPack(
            id=f"approval-{uuid4().hex}", cadence_id=cadence_id,
            cadence_version=cadence_version, actor=actor.strip(), approved_at=now,
            expires_at=expires_at, step_hashes=hashes,
        )

    def authorize_step(
        self, step: Mapping[str, Any], pack: ApprovalPack, now: datetime
    ) -> Authorization:
        if self.global_pause():
            raise ApprovalError("global pause active")
        if now >= pack.expires_at:
            raise ApprovalError("approval expired")
        step_id = str(step.get("id", ""))
        if step_id in pack.invalidated_step_ids or pack.status in {"invalidated", "expired"}:
            raise ApprovalError("approval invalidated")
        expected = dict(pack.step_hashes).get(step_id)
        try:
            current = _step_hash(step)
        except ApprovalError as exc:
            raise ApprovalError("approval scope changed") from exc
        if expected is None or expected != current:
            raise ApprovalError("approval scope changed")
        return Authorization(pack.id, step_id, pack.actor, expected, pack.expires_at)

    def invalidate_affected(self, pack: ApprovalPack, step_ids: Sequence[str]) -> ApprovalPack:
        affected = tuple(step_id for step_id in pack.step_ids if step_id in set(step_ids))
        invalidated = tuple(dict.fromkeys((*pack.invalidated_step_ids, *affected)))
        status = "invalidated" if len(invalidated) == len(pack.step_ids) else "partially_invalidated"
        return replace(pack, invalidated_step_ids=invalidated, status=status)
