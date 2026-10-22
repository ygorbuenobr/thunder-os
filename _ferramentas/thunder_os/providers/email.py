"""Provider-neutral email boundary, disabled until explicitly configured."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .result import ProviderResult


class EmailAdapter:
    def __init__(
        self, provider_name: str | None,
        transport: Callable[[Mapping[str, str]], Mapping[str, Any]] | None,
        *, account_id: str | None = None,
    ):
        self.provider_name = provider_name.strip() if isinstance(provider_name, str) else None
        self.transport = transport
        self.account_id = account_id

    def readback(self, receipt_ref: str) -> dict[str, Any]:
        """Read a provider receipt through an explicitly configured read transport."""
        prefix = f"email:{self.provider_name}:"
        if (
            not self.provider_name or not isinstance(receipt_ref, str)
            or not receipt_ref.startswith(prefix)
            or not receipt_ref[len(prefix):].strip()
            or receipt_ref != receipt_ref.strip()
        ):
            raise ValueError("invalid email receipt")
        reader = getattr(self.transport, "readback", None)
        if not callable(reader):
            raise ValueError("email readback not configured")
        try:
            response = reader(receipt_ref)
        except Exception as exc:
            raise ValueError("email readback failed") from exc
        required = {"target", "subject", "content", "effect_id"}
        if (
            not isinstance(response, Mapping)
            or not all(isinstance(response.get(field), str) and response[field].strip()
                       for field in required)
            or "@" not in response["target"]
        ):
            raise ValueError("invalid email readback")
        return dict(response)

    def send(self, effect: Mapping[str, Any]) -> ProviderResult:
        effect_id = str(effect.get("effect_id", ""))
        if not self.provider_name or self.transport is None:
            return ProviderResult(effect_id, "not_configured", error_code="email_not_configured")
        allowed = {"target", "subject", "content", "effect_id"}
        target = effect.get("target")
        if (
            set(effect) != allowed
            or not all(isinstance(effect.get(field), str) and effect[field].strip() for field in allowed)
            or not isinstance(target, str) or "@" not in target
        ):
            raise ValueError("invalid email effect")
        request = {field: str(effect[field]).strip() for field in allowed}
        try:
            response = self.transport(request)
        except TimeoutError:
            return ProviderResult(effect_id, "uncertain", error_code="provider_timeout")
        except Exception:
            return ProviderResult(effect_id, "rejected", error_code="provider_error")
        if response.get("ok") is True:
            provider_id = response.get("provider_id")
            if isinstance(provider_id, str) and provider_id:
                return ProviderResult(effect_id, "confirmed", f"email:{self.provider_name}:{provider_id}")
            return ProviderResult(effect_id, "uncertain")
        code = response.get("code")
        return ProviderResult(
            effect_id, "rejected",
            error_code=code if isinstance(code, str) and code else "provider_rejected",
        )
