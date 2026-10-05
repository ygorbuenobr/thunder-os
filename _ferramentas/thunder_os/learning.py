"""Evidence-bounded learning proposals with explicit human promotion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


class LearningError(ValueError):
    pass


@dataclass(frozen=True)
class LearningProposal:
    criterion_version: str
    hypothesis: str
    population: str
    period: str
    comparison: str
    evidence: Mapping[str, Any]
    counterevidence: Mapping[str, Any]
    owner: str
    review_at: str
    limitation: str
    proposed_action: str = "testar hipótese em experimento delimitado"


@dataclass(frozen=True)
class LearningResult:
    recommendation: LearningProposal | None
    observation: Mapping[str, Any]


class LearningEngine:
    def __init__(self, minimum_sample: int, minimum_lift: float):
        self.minimum_sample = minimum_sample
        self.minimum_lift = minimum_lift

    def analyze(
        self, records: Sequence[Mapping[str, Any]], hypothesis: str, population: str,
        owner: str, review_at: str,
    ) -> LearningResult:
        positive = sum(record.get("outcome") == "positive" for record in records)
        negative = sum(record.get("outcome") == "negative" for record in records)
        sample = positive + negative
        if sample < self.minimum_sample:
            return LearningResult(None, {"reason": "insufficient_sample", "sample": sample})
        rate = positive / sample
        if abs(rate - 0.5) < self.minimum_lift:
            return LearningResult(None, {"reason": "conflicting_evidence", "sample": sample})
        proposal = LearningProposal(
            criterion_version="positive-rate-v1", hypothesis=hypothesis, population=population,
            period=f"amostra encerrada para revisão em {review_at}",
            comparison="taxa positiva versus baseline 0.5",
            evidence={"positive": positive, "sample": sample, "rate": rate},
            counterevidence={"negative": negative}, owner=owner, review_at=review_at,
            limitation="associação observada; causalidade não comprovada",
        )
        return LearningResult(proposal, {"reason": "sufficient_evidence", "sample": sample})

    def start_experiment(
        self, proposal: LearningProposal | None, metric: str, population: str, owner: str,
        stop_condition: str, evaluate_at: str,
    ) -> dict[str, str]:
        if proposal is None:
            raise LearningError("actionable proposal required")
        return {
            "hypothesis": proposal.hypothesis, "metric": metric, "population": population,
            "owner": owner, "stop_condition": stop_condition, "evaluate_at": evaluate_at,
            "status": "planned",
        }

    def promotion_command(
        self, proposal: LearningProposal | None, actor: str, *, approved: bool
    ) -> dict[str, str]:
        if proposal is None or not approved or actor != "ygor":
            raise LearningError("human promotion required")
        return {
            "status": "human_approved", "actor": actor,
            "action": "review_policy_change", "hypothesis": proposal.hypothesis,
        }

    def complete_experiment(
        self, experiment: Mapping[str, str], result: Mapping[str, Any], actor: str,
        *, approved: bool,
    ) -> dict[str, Any]:
        if experiment.get("status") != "planned" or not result:
            raise LearningError("completed experiment evidence required")
        if not approved or actor != "ygor":
            return {
                **dict(experiment), "status": "awaiting_human_decision",
                "result": dict(result), "next_action": "Ygor decide promover, ajustar ou encerrar",
            }
        return {
            **dict(experiment), "status": "human_approved", "result": dict(result),
            "actor": actor, "next_action": "preparar mudança de regra separadamente",
        }
