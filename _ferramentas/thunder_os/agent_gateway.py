"""Idempotent local ingress for agent work envelopes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping, TextIO

from .db import Repository
from .models import ContractError, WorkItem, as_payload
from .service import ThunderService


class EnvelopeError(ValueError):
    pass


ACTOR_CAPABILITIES = {
    "hermes": {"research", "copy"},
    "claude": {"research", "copy"},
    "codex": {"research", "copy"},
}
FORBIDDEN_FIELDS = {"approved", "sent", "spend", "receipt", "external_status"}


class AgentGateway:
    def __init__(self, service: ThunderService, brain_root: str | Path):
        self.service = service
        self.brain_root = Path(brain_root).resolve()

    def ingest(self, envelope: Mapping[str, Any]) -> WorkItem:
        if not isinstance(envelope, Mapping):
            raise EnvelopeError("malformed envelope")
        if set(envelope) & FORBIDDEN_FIELDS:
            raise EnvelopeError("agent cannot claim approval or external completion")
        if not {"id", "actor", "capability", "objective"} <= set(envelope) or set(envelope) - {"id", "actor", "capability", "objective", "account_id"}:
            raise EnvelopeError("invalid envelope fields")
        envelope_id, actor, capability, objective = (
            envelope["id"], envelope["actor"], envelope["capability"], envelope["objective"]
        )
        if not isinstance(envelope_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", envelope_id):
            raise EnvelopeError("invalid envelope id")
        if actor not in ACTOR_CAPABILITIES or capability not in ACTOR_CAPABILITIES[actor]:
            raise EnvelopeError("actor capability denied")
        if not isinstance(objective, str) or not objective.strip():
            raise EnvelopeError("objective required")
        context = {"capability": capability}
        if "account_id" in envelope:
            account_id = envelope["account_id"]
            if not isinstance(account_id, str) or not account_id.strip() or len(account_id) > 253:
                raise EnvelopeError("invalid account_id")
            context["account_id"] = account_id.lower().strip()
        try:
            return self.service.request_work(
                {"objective": objective, "context": context},
                actor,
                f"agent-envelope:{actor}:{envelope_id}",
            )
        except ContractError as exc:
            raise EnvelopeError(str(exc)) from exc

    def import_file(self, actor: str, filename: str) -> WorkItem:
        if actor not in ACTOR_CAPABILITIES or not re.fullmatch(r"[a-zA-Z0-9_.-]+\.json", filename):
            raise EnvelopeError("unsafe envelope path")
        base = (self.brain_root / "_entrada" / actor).resolve()
        path = base / filename
        try:
            resolved = path.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise EnvelopeError("envelope not found") from exc
        if not resolved.is_relative_to(base) or path.is_symlink():
            raise EnvelopeError("unsafe envelope path")
        try:
            value = json.loads(resolved.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeError) as exc:
            raise EnvelopeError("malformed envelope") from exc
        return self.ingest(value)


def main(args: list[str] | None = None, *, stdin: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(prog="thunder-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest")
    ingest.add_argument("--db", required=True)
    ingest.add_argument("--brain", required=True)
    ingest.add_argument("--file")
    ingest.add_argument("--actor")
    parsed = parser.parse_args(args)
    gateway = AgentGateway(ThunderService(Repository(parsed.db)), parsed.brain)
    try:
        if parsed.file:
            if not parsed.actor:
                raise EnvelopeError("actor required for file import")
            item = gateway.import_file(parsed.actor, parsed.file)
        else:
            try:
                value = json.load(stdin or sys.stdin)
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise EnvelopeError("malformed envelope") from exc
            item = gateway.ingest(value)
        print(json.dumps(as_payload(item), ensure_ascii=False))
        return 0
    except EnvelopeError as exc:
        print(json.dumps({"erro": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
