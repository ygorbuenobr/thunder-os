import unittest

from thunder_os.learning import LearningEngine, LearningError


def outcomes(positive, negative):
    return ([{"outcome": "positive"}] * positive) + ([{"outcome": "negative"}] * negative)


class LearningEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = LearningEngine(minimum_sample=10, minimum_lift=0.2)

    def test_low_sample_remains_observation_not_recommendation(self):
        result = self.engine.analyze(outcomes(7, 2), "usar sinal X", "contas industriais", "ygor", "2026-10-15")
        self.assertIsNone(result.recommendation)
        self.assertEqual(result.observation["reason"], "insufficient_sample")

    def test_conflicting_evidence_remains_outside_actionable_recommendations(self):
        result = self.engine.analyze(outcomes(5, 5), "usar sinal X", "contas industriais", "ygor", "2026-10-15")
        self.assertIsNone(result.recommendation)
        self.assertEqual(result.observation["reason"], "conflicting_evidence")

    def test_sufficient_evidence_produces_one_bounded_proposal(self):
        result = self.engine.analyze(outcomes(9, 1), "usar sinal X", "contas industriais", "ygor", "2026-10-15")
        proposal = result.recommendation
        self.assertEqual(proposal.hypothesis, "usar sinal X")
        self.assertEqual(proposal.population, "contas industriais")
        self.assertEqual(proposal.evidence, {"positive": 9, "sample": 10, "rate": 0.9})
        self.assertEqual(proposal.counterevidence, {"negative": 1})
        self.assertEqual(
            (proposal.criterion_version, proposal.period, proposal.comparison),
            ("positive-rate-v1", "amostra encerrada para revisão em 2026-10-15", "taxa positiva versus baseline 0.5"),
        )
        self.assertEqual(proposal.owner, "ygor")
        self.assertEqual(proposal.review_at, "2026-10-15")
        self.assertEqual(proposal.limitation, "associação observada; causalidade não comprovada")

    def test_test_decision_creates_delimited_experiment(self):
        proposal = self.engine.analyze(outcomes(9, 1), "usar sinal X", "contas industriais", "ygor", "2026-10-15").recommendation
        experiment = self.engine.start_experiment(
            proposal, "taxa de resposta", "20 contas industriais", "ygor",
            "parar com 3 descadastros", "2026-10-15",
        )
        self.assertEqual(
            (experiment["hypothesis"], experiment["metric"], experiment["population"],
             experiment["owner"], experiment["stop_condition"], experiment["evaluate_at"],
             experiment["status"]),
            ("usar sinal X", "taxa de resposta", "20 contas industriais", "ygor",
             "parar com 3 descadastros", "2026-10-15", "planned"),
        )

    def test_policy_changes_only_after_explicit_human_promotion(self):
        proposal = self.engine.analyze(outcomes(9, 1), "usar sinal X", "contas industriais", "ygor", "2026-10-15").recommendation
        with self.assertRaisesRegex(LearningError, "human promotion required"):
            self.engine.promotion_command(proposal, "agent", approved=False)
        command = self.engine.promotion_command(proposal, "ygor", approved=True)
        self.assertEqual(command["status"], "human_approved")
        self.assertEqual(command["actor"], "ygor")
        self.assertEqual(command["action"], "review_policy_change")

    def test_finished_experiment_waits_for_human_before_any_rule_change(self):
        proposal = self.engine.analyze(outcomes(9, 1), "usar sinal X", "contas industriais", "ygor", "2026-10-15").recommendation
        experiment = self.engine.start_experiment(
            proposal, "taxa de resposta", "20 contas industriais", "ygor",
            "parar com 3 descadastros", "2026-10-30",
        )
        completed = self.engine.complete_experiment(
            experiment, {"rate": 0.7, "sample": 20}, "agent", approved=False,
        )
        self.assertEqual(completed["status"], "awaiting_human_decision")
        self.assertEqual(completed["result"], {"rate": 0.7, "sample": 20})


if __name__ == "__main__":
    unittest.main()
