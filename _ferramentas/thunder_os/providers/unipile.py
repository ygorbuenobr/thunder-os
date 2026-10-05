"""Unipile boundary with normalized, sanitized one-attempt results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class ProviderResult:
    effect_id: str
    status: str
    receipt_ref: str | None = None
    error_code: str | None = None


class UnipileAdapter:
    def __init__(self, transport: Callable[[Mapping[str, str]], Mapping[str, Any]]):
        self.transport = transport

    def send(self, effect: Mapping[str, Any]) -> ProviderResult:
        allowed = {"target", "content", "effect_id"}
        if set(effect) != allowed or not all(
            isinstance(effect.get(field), str) and effect[field].strip() for field in allowed
        ):
            raise ValueError("invalid linkedin effect")
        request = {field: str(effect[field]).strip() for field in allowed}
        try:
            response = self.transport(request)
        except TimeoutError:
            return ProviderResult(request["effect_id"], "uncertain", error_code="provider_timeout")
        except Exception:
            return ProviderResult(request["effect_id"], "rejected", error_code="provider_error")
        if response.get("ok") is True:
            provider_id = response.get("provider_id")
            if isinstance(provider_id, str) and provider_id:
                return ProviderResult(request["effect_id"], "confirmed", f"unipile:{provider_id}")
            return ProviderResult(request["effect_id"], "uncertain")
        code = response.get("code")
        return ProviderResult(
            request["effect_id"], "rejected",
            error_code=code if isinstance(code, str) and code else "provider_rejected",
        )
