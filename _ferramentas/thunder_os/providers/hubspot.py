"""Durable one-attempt HubSpot writes followed by read-only reconciliation."""
from typing import Any, Mapping
from ..models import payload_hash
from ..db import RepositoryConflict
from .unipile import ProviderResult


class HubSpotAdapter:
    def __init__(self, expected_scope: Mapping[str, str], transport: Any, repository=None):
        self.expected_scope = dict(expected_scope)
        self.transport = transport
        self.repository = repository
        self.capability = "configured" if repository is not None else "disabled"

    def mirror(self, effect: Mapping[str, Any]) -> ProviderResult:
        required = {"workspace", "account_id", "namespace", "object_type", "object_id", "effect_id", "fields"}
        if (set(effect) != required or not isinstance(effect.get("fields"), Mapping) or not effect["fields"]
                or any(not isinstance(effect.get(k), str) or not effect[k].strip() for k in required - {"fields"})) :
            raise ValueError("invalid hubspot effect")
        if any(effect.get(key) != self.expected_scope.get(key) for key in ("workspace", "account_id", "namespace")):
            raise ValueError("target mismatch")
        effect_id = str(effect["effect_id"])
        if self.repository is None:
            return ProviderResult(effect_id, "not_configured", error_code="durable_store_required")
        fingerprint = payload_hash(dict(effect))
        key = payload_hash({**self.expected_scope, "effect_id": effect_id})
        created = False
        try:
            existing = self.repository.append_state("hubspot_effect", key, {"fingerprint": fingerprint, "status": "reserved"})
            created = True
        except RepositoryConflict:
            existing = self.repository.get_state("hubspot_effect", key)
        if existing["fingerprint"] != fingerprint:
            raise ValueError("idempotency payload mismatch")
        if existing["status"] in {"confirmed", "rejected"}:
            return ProviderResult(effect_id, existing["status"], existing.get("receipt_ref"), existing.get("error_code"))
        if created:
            try:
                accepted = self.transport.write(dict(effect))
                if not isinstance(accepted, Mapping) or accepted.get("accepted") not in (True, False):
                    return ProviderResult(effect_id, "pending_reconciliation", error_code="hubspot_write_uncertain")
                if accepted.get("accepted") is False:
                    self.repository.put_state("hubspot_effect", key, {"fingerprint": fingerprint, "status": "rejected", "error_code": "hubspot_rejected"})
                    return ProviderResult(effect_id, "rejected", error_code="hubspot_rejected")
            except Exception:
                # A lost response may still have committed remotely. Never repeat write.
                return ProviderResult(effect_id, "pending_reconciliation", error_code="hubspot_write_uncertain")
        try:
            readback = self.transport.read(str(effect["object_type"]), str(effect["object_id"]))
            matches = isinstance(readback, Mapping) and all(k in readback and readback[k] == v for k, v in effect["fields"].items())
        except Exception:
            matches = False
        if not matches:
            return ProviderResult(effect_id, "pending_reconciliation")
        receipt = f"hubspot:{effect['object_type']}:{effect['object_id']}"
        self.repository.put_state("hubspot_effect", key, {"fingerprint": fingerprint, "status": "confirmed", "receipt_ref": receipt})
        return ProviderResult(effect_id, "confirmed", receipt)
