"""Inbound reply correlation, correction history and cadence stopping."""

from __future__ import annotations

from threading import Lock
from typing import Any, Mapping


class ReplyError(ValueError):
    pass


class ReplyCoordinator:
    CLASSIFICATIONS = {"positive", "objection", "negative", "opt_out", "risk", "unrelated"}

    def __init__(self, store: Any, cadences: Any):
        self.store = store
        self.cadences = cadences
        self._lock = Lock()

    def ingest(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        required = {"id", "channel", "content", "received_at"}
        if not required.issubset(payload) or not all(payload.get(field) for field in required):
            raise ReplyError("invalid reply")
        with self._lock:
            existing = self.store.get(str(payload["id"]))
            if existing is not None:
                return existing
            correlated = all(payload.get(field) for field in ("person_key", "account_slug", "cadence_id"))
            value = dict(payload)
            value["classification_history"] = []
            if correlated:
                self.cadences.pause_for_reply(str(payload["person_key"]), str(payload["id"]))
                value.update(state="needs_classification", next_action="classificar resposta")
            else:
                value.update(state="identity_resolution", next_action="resolver identidade")
            return self.store.save(value)

    def classify(
        self, reply_id: str, classification: str, confidence: float,
        justification: str, actor: str,
    ) -> dict[str, Any]:
        if classification not in self.CLASSIFICATIONS or not 0 <= confidence <= 1:
            raise ReplyError("invalid classification")
        with self._lock:
            reply = self.store.get(reply_id)
            if reply is None:
                raise ReplyError("reply not found")
            if reply.get("state") == "identity_resolution":
                raise ReplyError("identity unresolved")
            history = list(reply["classification_history"])
            history.append({
                "classification": classification, "confidence": confidence,
                "justification": justification, "actor": actor,
            })
            next_actions = {
                "positive": "decidir próximo passo comercial",
                "objection": "decidir resposta à objeção",
                "unrelated": "encerrar como não relacionada",
                "negative": "contato suprimido",
                "opt_out": "contato suprimido",
                "risk": "contato suprimido",
            }
            if classification in {"negative", "opt_out", "risk"}:
                self.cadences.suppress(str(reply["person_key"]), classification)
            updated = {
                **reply, "classification": classification, "confidence": confidence,
                "justification": justification, "classification_history": history,
                "next_action": next_actions[classification],
            }
            return self.store.save(updated)
