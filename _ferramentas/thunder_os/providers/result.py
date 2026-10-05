"""Provider-neutral immutable outcome; historical receipt strings are preserved."""
from dataclasses import dataclass

@dataclass(frozen=True)
class ProviderResult:
    effect_id: str
    status: str
    receipt_ref: str | None = None
    error_code: str | None = None
