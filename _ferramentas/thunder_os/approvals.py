"""Exact, versioned human approval for cadence effects."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

from .models import ContractError, effect_payload_hash, payload_hash
import re


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


# Payload approval is deliberately insufficient to execute a campaign. These
# helpers belong to the existing approval boundary; no second authority store.
_EXECUTION_FIELDS = {
    "platform", "campaign_id", "campaign_version", "rules_version", "rules_hash",
    "configuration_hash", "run_id", "providers",
}


def execution_context(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _EXECUTION_FIELDS:
        raise ApprovalError("campaign_context_required")
    if not isinstance(value["platform"], str) or value["platform"] not in {"thunder_os", "chattie", "instantly"}:
        raise ApprovalError("campaign_platform_invalid")
    for field in _EXECUTION_FIELDS - {"providers"}:
        if not isinstance(value[field], str) or not value[field].strip() or len(value[field]) > 200:
            raise ApprovalError("campaign_context_invalid")
    for field in ("rules_hash", "configuration_hash"):
        if not re.fullmatch(r"[a-f0-9]{64}", value[field]):
            raise ApprovalError("campaign_hash_invalid")
    providers = value["providers"]
    if (not isinstance(providers, Mapping) or not providers
            or not set(providers) <= {"linkedin", "email"}
            or any(not isinstance(v, str) or not re.fullmatch(r"[a-z0-9_-]{1,64}", v)
                   for v in providers.values())):
        raise ApprovalError("campaign_providers_invalid")
    return {**value, "providers": dict(providers)}


def execution_request(context, batch_id, batch_version, step_hashes, control_version):
    context = execution_context(context)
    if (not isinstance(batch_id, str) or not batch_id.strip()
            or type(batch_version) is not int or batch_version < 1
            or type(control_version) is not int or control_version < 0
            or not isinstance(step_hashes, Mapping) or not step_hashes
            or any(not isinstance(k, str) or not k or not isinstance(v, str)
                   or not re.fullmatch(r"[a-f0-9]{64}", v) for k, v in step_hashes.items())):
        raise ApprovalError("campaign_batch_invalid")
    return {"confirmed": True, "context": context, "batch_id": batch_id,
            "batch_version": batch_version, "scope_hash": payload_hash(dict(step_hashes)),
            "control_version": control_version}


def confirm_execution(request, expected, actor, expires_at, now):
    # Explicit human endpoint only. A bool, natural-language 'approved', agent
    # context or a confirmation for another batch cannot authorize anything.
    if actor != "ygor" or not isinstance(request, Mapping) or dict(request) != expected:
        raise ApprovalError("campaign_confirmation_required")
    if (request.get("confirmed") is not True or type(request.get("batch_version")) is not int
            or type(request.get("control_version")) is not int):
        raise ApprovalError("campaign_confirmation_required")
    if expires_at <= now:
        raise ApprovalError("campaign_confirmation_expired")
    return {"request": expected, "actor": actor, "confirmed_at": now.isoformat(),
            "expires_at": expires_at.isoformat()}


def validate_execution(record, expected, now, *, platform="thunder_os"):
    if not isinstance(record, Mapping) or record.get("actor") != "ygor":
        raise ApprovalError("campaign_confirmation_required")
    if (record.get("request") != expected or expected["context"]["platform"] != platform
            or type(record.get("request", {}).get("batch_version")) is not int
            or type(record.get("request", {}).get("control_version")) is not int
            or record.get("request", {}).get("confirmed") is not True):
        raise ApprovalError("campaign_confirmation_scope_changed")
    try:
        confirmed = datetime.fromisoformat(record["confirmed_at"])
        expiry = datetime.fromisoformat(record["expires_at"])
        if not confirmed <= now < expiry:
            raise ApprovalError("campaign_confirmation_expired")
    except (KeyError, TypeError, ValueError) as exc:
        raise ApprovalError("campaign_confirmation_expired") from exc
