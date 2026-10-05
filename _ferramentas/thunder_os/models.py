"""Strict domain contracts shared by Thunder OS adapters and services."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from typing import Any, Mapping, TypeVar


class ContractError(ValueError):
    """Raised when data does not satisfy a domain contract."""


class StaleVersionError(ContractError):
    """Raised when an optimistic transition uses an old version."""


class WorkStatus(str, Enum):
    REQUESTED = "requested"
    IN_PROGRESS = "in_progress"
    NEEDS_HUMAN = "needs_human"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class JobType(str, Enum):
    RESEARCH = "research"
    COPY = "copy"


class Channel(str, Enum):
    LINKEDIN = "linkedin"
    EMAIL = "email"
    WHATSAPP = "whatsapp"
    HUBSPOT = "hubspot"


ALLOWED_WORK_TRANSITIONS = {
    WorkStatus.REQUESTED: {WorkStatus.IN_PROGRESS, WorkStatus.NEEDS_HUMAN},
    WorkStatus.IN_PROGRESS: {
        WorkStatus.NEEDS_HUMAN,
        WorkStatus.SUCCEEDED,
        WorkStatus.FAILED,
    },
    WorkStatus.NEEDS_HUMAN: {WorkStatus.IN_PROGRESS, WorkStatus.FAILED},
    WorkStatus.SUCCEEDED: set(),
    WorkStatus.FAILED: set(),
}


def _strict(data: Mapping[str, Any], required: set[str], optional: set[str] = set()) -> None:
    unknown = set(data) - required - optional
    missing = required - set(data)
    if unknown:
        raise ContractError(f"unknown fields: {', '.join(sorted(unknown))}")
    if missing:
        raise ContractError(f"missing fields: {', '.join(sorted(missing))}")


def _text(value: Any, field: str, limit: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ContractError(f"invalid {field}")
    return value.strip()


def payload_hash(payload: Mapping[str, Any]) -> str:
    """Return a stable hash for a JSON object, independent of key ordering."""
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ContractError("payload must be JSON serializable") from exc
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class WorkItem:
    id: str
    objective: str
    actor: str
    idempotency_key: str
    context: Mapping[str, Any] = field(default_factory=dict)
    logical_owner: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: WorkStatus = WorkStatus.REQUESTED
    version: int = 1
    next_action: str = "aguardar execução"
    result: Mapping[str, Any] | None = None


def parse_work_item(data: Mapping[str, Any]) -> WorkItem:
    required = {"id", "objective", "actor", "idempotency_key"}
    optional = {"context", "logical_owner", "created_at", "updated_at", "status", "version", "next_action", "result"}
    _strict(data, required, optional)
    try:
        status = WorkStatus(data.get("status", WorkStatus.REQUESTED))
    except ValueError as exc:
        raise ContractError("invalid status") from exc
    version = data.get("version", 1)
    if not isinstance(version, int) or version < 1:
        raise ContractError("invalid version")
    result = data.get("result")
    if result is not None and not isinstance(result, Mapping):
        raise ContractError("invalid result")
    context = data.get("context", {})
    if not isinstance(context, Mapping):
        raise ContractError("invalid context")
    actor = _text(data["actor"], "actor", 120)
    return WorkItem(
        id=_text(data["id"], "id", 120),
        objective=_text(data["objective"], "objective"),
        actor=actor,
        idempotency_key=_text(data["idempotency_key"], "idempotency_key", 200),
        context=dict(context),
        logical_owner=_text(data.get("logical_owner", actor), "logical_owner", 120),
        created_at=_text(data.get("created_at", datetime.now(timezone.utc).isoformat()), "created_at", 100),
        updated_at=_text(data.get("updated_at", datetime.now(timezone.utc).isoformat()), "updated_at", 100),
        status=status,
        version=version,
        next_action=_text(data.get("next_action", "aguardar execução"), "next_action"),
        result=dict(result) if result is not None else None,
    )


T = TypeVar("T")


def transition(entity: T, expected_version: int, target: WorkStatus) -> T:
    if not isinstance(entity, WorkItem):
        raise ContractError("unsupported entity")
    if entity.version != expected_version:
        raise StaleVersionError("stale version")
    if target not in ALLOWED_WORK_TRANSITIONS[entity.status]:
        raise ContractError(f"invalid transition: {entity.status.value} -> {target.value}")
    return replace(
        entity, status=target, version=entity.version + 1,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )  # type: ignore[return-value]


def as_payload(entity: WorkItem) -> dict[str, Any]:
    value = asdict(entity)
    value["status"] = entity.status.value
    return value


def effect_payload_hash(
    *, target: str, channel: Channel | str, content: str, order: int, interval_hours: int,
    subject: str | None = None,
) -> str:
    try:
        parsed_channel = Channel(channel).value
    except ValueError as exc:
        raise ContractError("invalid channel") from exc
    if order < 1 or interval_hours < 0:
        raise ContractError("invalid order or interval")
    payload = {
            "target": _text(target, "target", 500),
            "channel": parsed_channel,
            "content": _text(content, "content", 20000),
            "order": order,
            "interval_hours": interval_hours,
    }
    if parsed_channel == Channel.EMAIL.value:
        payload["subject"] = _text(subject, "subject", 998)
    elif subject is not None:
        raise ContractError("subject is email-only")
    return payload_hash(payload)
