"""Deterministic multichannel cadence scheduling and stop rules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .models import effect_payload_hash


SAO_PAULO = ZoneInfo("America/Sao_Paulo")


class CadenceError(ValueError):
    pass


def calculate_due(dependency_completed_at: datetime, interval_hours: int) -> datetime:
    if dependency_completed_at.tzinfo is None or interval_hours < 0:
        raise CadenceError("invalid due calculation")
    local = dependency_completed_at.astimezone(SAO_PAULO) + timedelta(hours=interval_hours)
    while local.weekday() >= 5:
        local += timedelta(days=1)
    return local.astimezone(timezone.utc)


def _scope_hash(step: Mapping[str, Any]) -> str:
    return effect_payload_hash(
        target=step["recipient"], channel=step["channel"], content=step["content"],
        order=step["order"], interval_hours=step["interval_hours"], subject=step.get("subject"),
    )


def changed_future_steps(
    original: Sequence[Mapping[str, Any]], updated: Sequence[Mapping[str, Any]]
) -> tuple[str, ...]:
    old = {str(step["id"]): _scope_hash(step) for step in original}
    new = {str(step["id"]): _scope_hash(step) for step in updated}
    if old.keys() != new.keys():
        raise CadenceError("step ids changed")
    return tuple(step_id for step_id in old if old[step_id] != new[step_id])


class CadenceEngine:
    def __init__(self, available_channels: set[str] | None = None):
        self.available_channels = available_channels or {"linkedin", "email"}

    def evaluate(
        self, step: Mapping[str, Any], now: datetime, *, reply: bool = False,
        opt_out: bool = False, suppressed: bool = False, policy_conflict: bool = False,
    ) -> str:
        if reply or opt_out or suppressed:
            return "blocked"
        if step.get("dependency_status") in {"failed", "uncertain", "blocked"}:
            return "blocked"
        if policy_conflict or step.get("channel") not in self.available_channels:
            return "needs_human"
        due_at = step.get("due_at")
        if not isinstance(due_at, datetime) or due_at.tzinfo is None:
            raise CadenceError("invalid due time")
        return "due" if now >= due_at else "approved"

    def pause_for_reply(self, steps: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [
            {**step, "status": "skipped_reply"}
            if step.get("status") in {"approved", "due", "draft"}
            else dict(step)
            for step in steps
        ]


def effective_due(step: Mapping[str, Any], effects: Sequence[Mapping[str, Any]], cadence_id: str) -> datetime | None:
    """A follow-up interval starts at the confirmed predecessor's send time."""
    try:
        interval = step["interval_hours"]
        if type(interval) is not int or interval < 0:
            return None
        if not step.get("predecessor_id"):
            due = datetime.fromisoformat(str(step["due_at"]))
        else:
            predecessor = next((effect for effect in effects
                if effect.get("payload", {}).get("cadence_id") == cadence_id
                and effect.get("payload", {}).get("step", {}).get("id") == step["predecessor_id"]
                and effect.get("state") == "sent"), None)
            if not predecessor or not predecessor.get("sent_at"):
                return None
            due = datetime.fromisoformat(str(predecessor["sent_at"])) + timedelta(hours=interval)
        return due if due.tzinfo is not None else None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
