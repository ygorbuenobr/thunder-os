"""Bounded runner for research and copy jobs."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import subprocess
from typing import Any, Callable, Mapping


class AgentRunnerError(ValueError):
    pass


@dataclass(frozen=True)
class AgentJob:
    id: str
    type: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class AgentResult:
    status: str
    output: Mapping[str, Any] | None = None
    error_code: str | None = None
    next_action: str | None = None
    attempts: int = 1


FORBIDDEN_FIELDS = {"approved", "sent", "spend", "provider_receipt", "receipt"}


class AgentRunner:
    def __init__(
        self,
        transport: Callable[[AgentJob, float], str],
        *,
        timeout_seconds: float = 120,
        max_attempts: int = 2,
    ):
        if max_attempts not in (1, 2):
            raise ValueError("max_attempts must be 1 or 2")
        if not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 600:
            raise ValueError("timeout_seconds must be between 0 and 600")
        self.transport = transport
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts

    def run(self, job: AgentJob) -> AgentResult:
        if job.type not in {"research", "copy"}:
            raise AgentRunnerError("unsupported job type")
        last_code = "agent_failed"
        for attempt in range(1, self.max_attempts + 1):
            try:
                raw = self.transport(job, self.timeout_seconds)
                output = self._parse(job, raw)
                return AgentResult("succeeded", output=output, attempts=attempt)
            except (subprocess.TimeoutExpired, TimeoutError):
                last_code = "agent_timeout"
            except PermissionError:
                last_code = "forbidden_authority_claim"
                break
            except OSError:
                last_code = "agent_transport_unavailable"
                break
            except (json.JSONDecodeError, AgentRunnerError, TypeError, UnicodeError):
                last_code = "invalid_agent_output"
                break
        return AgentResult(
            "needs_human",
            error_code=last_code,
            next_action="revisar execução do agente",
            attempts=attempt,
        )

    @staticmethod
    def _parse(job: AgentJob, raw: str) -> Mapping[str, Any]:
        if not isinstance(raw, str):
            raise TypeError("agent output must be text")
        if len(raw.encode("utf-8")) > 1_000_000:
            raise AgentRunnerError("agent output too large")
        output = json.loads(raw)
        if not isinstance(output, dict):
            raise AgentRunnerError("agent result must be an object")
        if set(output) & FORBIDDEN_FIELDS:
            raise PermissionError("agent cannot claim external authority")
        if job.type == "research":
            if set(output) != {"artifact", "evidence"} or not isinstance(output["artifact"], dict) or not isinstance(output["evidence"], list):
                raise AgentRunnerError("invalid research result")
        else:
            if set(output) != {"copy", "angle_id", "evidence"} or not isinstance(output["copy"], str) or not isinstance(output["angle_id"], str) or not isinstance(output["evidence"], list):
                raise AgentRunnerError("invalid copy result")
            if not output["copy"].strip() or not output["angle_id"].strip():
                raise AgentRunnerError("copy and angle required")
            research = job.payload.get("validated_research")
            approved_capabilities = set(job.payload.get("approved_capabilities", []))
            approved_proofs = set(job.payload.get("approved_proofs", []))
            if not isinstance(research, Mapping):
                raise AgentRunnerError("validated research required for copy")
            if not set(research.get("capability_ids", [])) <= approved_capabilities or not set(research.get("proof_ids", [])) <= approved_proofs:
                raise AgentRunnerError("copy references unapproved Thunder evidence")
            evidence_refs = {entry.get("id") for entry in output["evidence"] if isinstance(entry, Mapping)}
            required_refs = set(research.get("capability_ids", [])) | set(research.get("proof_ids", []))
            if not required_refs <= evidence_refs:
                raise AgentRunnerError("copy evidence is incomplete")
        return output


def subprocess_transport(command: list[str]) -> Callable[[AgentJob, float], str]:
    """Trusted operator wiring only; never construct this from an ingress payload."""
    if not command or any(not isinstance(part, str) or not part for part in command):
        raise ValueError("configured agent command required")
    command = list(command)
    def execute(job: AgentJob, timeout: float) -> str:
        payload = json.dumps({"id": job.id, "type": job.type, "payload": job.payload}, ensure_ascii=False)
        result = subprocess.run(
            [*command, payload],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
        if result.returncode:
            raise AgentRunnerError("agent command failed")
        return result.stdout.strip()

    return execute
