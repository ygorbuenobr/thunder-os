import copy
import json
import unittest
from datetime import date, timedelta

from thunder_os.research import NO_EVIDENCE_VALUE_HYPOTHESIS, ResearchError, validate_artifact
from thunder_os.agents import AgentRunnerError
from thunder_os.hermes_text_adapter import finalize_research_output, reconcile_source_limitations, research_instructions, research_response_schema


def base_artifact():
    today = date.today().isoformat()
    facts = [
        {"fact_id": "F1", "statement": "Acme apresenta sua operação no site institucional", "source": "https://acme.example/company", "source_area": "company_site", "published_at": today, "observed_at": today, "freshness": "fresh", "relevance": "identidade e operação confirmadas no domínio oficial", "classification": "context"},
        {"fact_id": "F2", "statement": "Acme anunciou uma nova fábrica", "source": "https://business.example/news/acme-factory", "source_area": "company_news", "published_at": today, "observed_at": today, "freshness": "fresh", "relevance": "indicador operacional recente, sem confirmar intenção de compra", "classification": "indicator"},
        {"fact_id": "F3", "statement": "A página pública descreve a trajetória de Bruno", "source": "https://people.example/bruno", "source_area": "decision_maker", "published_at": today, "observed_at": today, "freshness": "fresh", "relevance": "trajetória observada; tempo no cargo não verificado", "classification": "context"},
        {"fact_id": "F4", "statement": "Acme mantém uma vaga de expansão comercial", "source": "https://jobs.example.net/opening", "source_area": "hiring", "published_at": today, "observed_at": today, "freshness": "fresh", "relevance": "sinal operacional, não intenção explícita", "classification": "indicator"},
        {"fact_id": "F5", "statement": "A empresa declarou congelamento de projetos", "source": "https://social.example/acme-update", "source_area": "company_social", "published_at": today, "observed_at": today, "freshness": "fresh", "relevance": "contrassinal à hipótese de expansão", "classification": "counter_signal"},
    ]
    areas_with_facts = {}
    for fact in facts:
        areas_with_facts.setdefault(fact["source_area"], []).append(fact["fact_id"])
    checked_at = today
    source_coverage = []
    for area in ("company_site", "company_news", "company_social", "google_ads", "meta_ads", "linkedin_ads", "hiring", "sector", "decision_maker", "other_public_source"):
        fact_ids = areas_with_facts.get(area, [])
        inaccessible = area in {"google_ads", "meta_ads"}
        unprovided = area == "linkedin_ads"
        attempted = ([f"https://{area}.example/public-search"] if not fact_ids and not unprovided else [])
        planned = ([] if unprovided else [f"https://{area}.example/public-search"] if not fact_ids else
                   [fact["source"] for fact in facts if fact["fact_id"] in fact_ids])
        source_coverage.append({
            "area": area,
            "status": "not_accessible" if inaccessible else "not_provided" if unprovided else "checked_with_evidence" if fact_ids else "checked_no_result",
            "query": f"consulta sintética de {area}",
            "checked_at": None if unprovided else checked_at,
            "planned_urls": planned,
            "attempted_urls": [fact["source"] for fact in facts if fact["fact_id"] in fact_ids] if fact_ids else attempted,
            "discovered_urls": [],
            "source_urls": [fact["source"] for fact in facts if fact["fact_id"] in fact_ids],
            "fact_ids": fact_ids,
            "details": (f"acesso a {area} indisponível nesta fixture" if inaccessible else
                        f"{area} não dispõe de endpoint público verificado" if unprovided else "resultado sintético de teste"),
        })
    research_gaps = [
        {"gap_id": f"G{index}", "topic": "source_access", "description": f"{area}: fonte pública indisponível ou endpoint não verificado", "severity": "medium", "blocks_hypothesis": False, "refuting_question": "Há outra fonte pública acessível para esta área?"}
        for index, area in enumerate(("google_ads", "meta_ads", "linkedin_ads"), 1)
    ]
    evidence_chains = [{
        "fact_ids": [fact["fact_id"]],
        "implication": f"Implicação sintética de {fact['fact_id']}",
        "alternative": "A operação atual pode absorver a mudança",
        "value_hypothesis": "Operação pode precisar de estrutura comercial",
        "refuting_question": "O que mostraria que esta hipótese não se aplica?",
    } for fact in facts]
    return {
        "account": {"name": "Acme", "domain": "acme.example", "sector": "indústria", "size": "Não verificado", "business_model": "Não verificado", "location": "Não verificado", "digital_presence": "site institucional"},
        "facts": facts,
        "explicit_intent": [],
        "indicators": ["F2", "F4"],
        "counter_signals": ["F5"],
        "counter_signal_assessment": {"status": "signals_found", "details": "Contrassinal público sintético localizado; impacto a qualificar", "fact_ids": ["F5"], "severity": "low", "disposition": "qualify", "qualification_question": "O congelamento ainda afeta a área comercial?"},
        "research_gaps": research_gaps,
        "source_coverage": source_coverage,
        "evidence_chains": evidence_chains,
        "inferences": ["Implicação sintética de F2"],
        "opportunity_hypothesis": "Operação pode precisar de estrutura comercial",
        "alternative_hypothesis": "Expansão pode estar coberta pelo time atual",
        "assessment": {"fit": "high", "moment": "medium", "data_quality": "high", "risk": "medium"},
        "committee": [
            {"person": "Bruno", "role": "decision_maker", "evidence": "https://people.example/bruno", "confidence": "medium", "role_since": None, "career_summary": "Trajetória profissional encontrada; início no cargo não verificado", "tenure_status": "unverified", "fact_ids": ["F3"]},
        ],
        "committee_gaps": [],
        "thunder_fit": {"capability_ids": ["cap-1"], "proof_ids": ["proof-1"], "knowledge_source": "thunder/empresa.md#capacidades"},
        "case_evidence": [{"case_id": "proof-1", "source_ref": "thunder/provas.md#case-1", "statement": "capacidade análoga", "promises_repeat_result": False}],
        "missing": [],
        "limitations": [f"acesso a {area} indisponível nesta fixture" for area in ("google_ads", "meta_ads")],
        "recommendation": "abordar",
        "favorable_facts": ["Acme anunciou uma nova fábrica"],
        "risks": ["Intenção não confirmada"],
        "next_action": "Revisar abordagem",
    }


class ResearchContractTests(unittest.TestCase):
    def test_twelve_fact_chain_omission_and_keyed_model_analysis(self):
        # Reproduce the observed failure shape; raw runtime model output was not retained.
        artifact = base_artifact()
        for number in range(6, 13):
            fact = {**artifact["facts"][0], "fact_id": f"F{number}",
                    "statement": f"Registro público sintético independente {number}"}
            artifact["facts"].append(fact)
            artifact["source_coverage"][0]["fact_ids"].append(fact["fact_id"])
        context = {"research_evidence": copy.deepcopy(artifact["facts"]),
                   "source_coverage": copy.deepcopy(artifact["source_coverage"]),
                   "research_account": copy.deepcopy(artifact["account"])}
        output = {"artifact": artifact, "evidence": [{"source": f["source"]} for f in artifact["facts"]]}
        with self.assertRaisesRegex(ResearchError, "every fact requires an evidence chain"):
            validate_artifact(artifact, {"cap-1"}, {"proof-1"})
        with self.assertRaisesRegex(AgentRunnerError, "exactly cover trusted fact IDs"):
            finalize_research_output(copy.deepcopy(output), context)
        expected = [f"F{i}" for i in range(1, 13)]
        chain_schema = research_response_schema(context)["properties"]["artifact"]["properties"]["evidence_chains"]
        self.assertEqual(expected, chain_schema["required"])
        self.assertFalse(chain_schema["additionalProperties"])
        artifact["evidence_chains"] = {fid: {
            "implication": f"O registro {fid} permite testar a relevância do contexto para a operação.",
            "alternative": "O evento pode refletir rotina institucional já atendida pela equipe atual.",
            "value_hypothesis": "Uma análise comercial só teria valor se houver uma lacuna operacional confirmada.",
            "refuting_question": f"Qual evidência operacional demonstraria que {fid} não exige qualquer mudança?",
        } for fid in expected}
        artifact["inferences"] = [artifact["evidence_chains"]["F1"]["implication"]]
        artifact["opportunity_hypothesis"] = artifact["evidence_chains"]["F1"]["value_hypothesis"]
        original = copy.deepcopy(output)
        result = json.loads(finalize_research_output(copy.deepcopy(output), context))["artifact"]
        self.assertEqual(original["artifact"]["facts"], result["facts"])
        self.assertEqual(original["artifact"]["source_coverage"], result["source_coverage"])
        self.assertEqual(original["artifact"]["recommendation"], result["recommendation"])
        self.assertEqual(expected, [c["fact_ids"][0] for c in result["evidence_chains"]])
        for chain in result["evidence_chains"]:
            self.assertEqual(original["artifact"]["evidence_chains"][chain["fact_ids"][0]],
                             {k:v for k,v in chain.items() if k != "fact_ids"})
        validate_artifact(result, {"cap-1"}, {"proof-1"})
        for change in ("missing", "extra", "placeholder", "repeated", "forged"):
            bad = copy.deepcopy(original)
            chains = bad["artifact"]["evidence_chains"]
            if change == "missing": del chains["F12"]
            if change == "extra": chains["F99"] = chains["F1"]
            if change == "placeholder": chains["F1"]["implication"] = "F1"
            if change == "repeated": chains["F1"]["alternative"] = chains["F1"]["implication"]
            if change == "forged": bad["artifact"]["facts"][0]["statement"] = "Invented"
            snapshot = copy.deepcopy(bad)
            with self.assertRaises(AgentRunnerError): finalize_research_output(bad, context)
            self.assertEqual(snapshot, bad)

    def test_adapter_validates_provenance_before_reconciling_limitations(self):
        artifact = base_artifact()
        facts = artifact["facts"]
        context = {
            "research_evidence": [
                {key: fact[key] for key in ("fact_id", "source", "statement", "observed_at", "published_at", "source_area")}
                for fact in facts
            ],
            "source_coverage": copy.deepcopy(artifact["source_coverage"]),
            "research_account": copy.deepcopy(artifact["account"]),
        }
        artifact["limitations"] = [artifact["limitations"][0]] * 2
        output = {"artifact": artifact, "evidence": [{"source": fact["source"]} for fact in facts]}
        final = json.loads(finalize_research_output(copy.deepcopy(output), context))
        self.assertEqual(2, len(final["artifact"]["limitations"]))
        self.assertEqual(2, len(set(final["artifact"]["limitations"])))

        invented = copy.deepcopy(output)
        invented["artifact"]["source_coverage"][0]["details"] = "Busca verificada sem registro de origem"
        before = copy.deepcopy(invented)
        with self.assertRaisesRegex(AgentRunnerError, "must match its trusted search record"):
            finalize_research_output(invented, context)
        self.assertEqual(before, invented)

    def test_missing_source_gaps_are_derived_after_provenance_without_changing_claims(self):
        artifact = base_artifact()
        artifact["recommendation"] = "pesquisar mais"
        context = {"research_evidence": copy.deepcopy(artifact["facts"]),
                   "source_coverage": copy.deepcopy(artifact["source_coverage"]),
                   "research_account": copy.deepcopy(artifact["account"])}
        artifact["research_gaps"] = []
        with self.assertRaisesRegex(ResearchError, "incomplete source coverage"):
            validate_artifact(artifact, {"cap-1"}, {"proof-1"})
        output = {"artifact": artifact, "evidence": [{"source": f["source"]} for f in artifact["facts"]]}
        before = copy.deepcopy(artifact)
        final = json.loads(finalize_research_output(copy.deepcopy(output), context))["artifact"]
        self.assertEqual({k:v for k,v in before.items() if k != "research_gaps"},
                         {k:v for k,v in final.items() if k != "research_gaps"})
        self.assertEqual(3, len(final["research_gaps"]))
        self.assertTrue(all(g["blocks_hypothesis"] for g in final["research_gaps"]))
        validate_artifact(final, {"cap-1"}, {"proof-1"})
        second = json.loads(finalize_research_output({**output, "artifact": copy.deepcopy(final)}, context))
        self.assertEqual(final, second["artifact"])
        forged = copy.deepcopy(output)
        forged["artifact"]["source_coverage"][0]["details"] = "invented search completion"
        snapshot = copy.deepcopy(forged)
        with self.assertRaises(AgentRunnerError):
            finalize_research_output(forged, context)
        self.assertEqual(snapshot, forged)

    def test_model_instruction_matches_required_gap_join(self):
        prompt = research_instructions({})
        self.assertIn("Every string in missing and committee_gaps MUST exactly equal a research_gaps.description string", prompt)
        self.assertIn("copy its details string verbatim into limitations", prompt)
        artifact = base_artifact()
        artifact["missing"] = ["lacuna parafraseada"]
        with self.assertRaisesRegex(ResearchError, "research gaps must classify every missing"):
            validate_artifact(artifact, {"cap-1"}, {"proof-1"})

    def test_inaccessible_source_limitations_are_unique_and_exact(self):
        artifact = base_artifact()
        details = next(row["details"] for row in artifact["source_coverage"]
                       if row["status"] == "not_accessible")
        artifact["limitations"] = [details, details]
        before = {key: value for key, value in artifact.items() if key != "limitations"}
        output = {"artifact": artifact}
        reconcile_source_limitations(output)
        self.assertEqual(before, {key: value for key, value in artifact.items() if key != "limitations"})
        self.assertEqual(len(artifact["limitations"]), len(set(artifact["limitations"])))
        self.assertEqual({row["details"] for row in artifact["source_coverage"]
                          if row["status"] == "not_accessible"}, set(artifact["limitations"]))
        validate_artifact(artifact, {"cap-1"}, {"proof-1"})

    def test_strong_dossier_preserves_facts_inferences_and_approved_fit(self):
        artifact = validate_artifact(base_artifact(), {"cap-1"}, {"proof-1"})
        self.assertEqual("abordar", artifact.recommendation)
        self.assertEqual("https://acme.example/company", artifact.facts[0]["source"])
        self.assertEqual(("Implicação sintética de F2",), artifact.inferences)
        self.assertEqual(("cap-1",), artifact.capability_ids)

    def test_stale_or_insufficient_dossier_never_defaults_to_approach(self):
        stale = base_artifact()
        stale["facts"][0]["published_at"] = (date.today() - timedelta(days=91)).isoformat()
        stale["facts"][0]["freshness"] = "stale"
        stale["recommendation"] = "pesquisar mais"
        self.assertEqual("pesquisar mais", validate_artifact(stale, {"cap-1"}, {"proof-1"}).recommendation)
        insufficient = base_artifact()
        insufficient["facts"] = []
        insufficient["opportunity_hypothesis"] = NO_EVIDENCE_VALUE_HYPOTHESIS
        insufficient["indicators"] = []
        insufficient["counter_signals"] = []
        insufficient["counter_signal_assessment"] = {"status": "not_assessed", "details": "Sem fatos fornecidos", "fact_ids": [], "severity": "unknown", "disposition": "defer", "qualification_question": "Quais sinais faltam validar?"}
        insufficient["source_coverage"] = [{**item, "status": "not_provided", "checked_at": None, "planned_urls": [], "attempted_urls": [], "source_urls": [], "fact_ids": [], "details": "Sem fonte pública registrada."} for item in insufficient["source_coverage"]]
        insufficient["research_gaps"] = [
            {"gap_id": f"G{index}", "topic": "source_access", "description": f"{item['area']}: sem fonte pública registrada", "severity": "high", "blocks_hypothesis": True, "refuting_question": "Que evidência pública pode preencher esta lacuna?"}
            for index, item in enumerate(insufficient["source_coverage"], 1)
        ]
        insufficient["research_gaps"].append({"gap_id":"G11","topic":"decision_maker","description":"decisor não verificado","severity":"high","blocks_hypothesis":True,"refuting_question":"Quem confirma a responsabilidade pela agenda?"})
        insufficient["evidence_chains"] = []
        insufficient["inferences"] = []
        insufficient["committee"] = []
        insufficient["committee_gaps"] = ["decisor não verificado"]
        insufficient["recommendation"] = "abordar"
        with self.assertRaisesRegex(ResearchError, "approach requires current account-bound evidence"):
            validate_artifact(insufficient, {"cap-1"}, {"proof-1"})

    def test_missing_search_provenance_blocks_complete_looking_fallback(self):
        artifact = base_artifact()
        artifact["source_coverage"] = [
            {**row, "status": "not_provided", "checked_at": None, "planned_urls": [], "attempted_urls": [], "source_urls": [], "fact_ids": [], "details": "Cobertura não fornecida."}
            for row in artifact["source_coverage"]
        ]
        artifact["research_gaps"] = [
            {"gap_id": f"G{index}", "topic": "source_access", "description": f"{row['area']}: cobertura não fornecida", "severity": "high", "blocks_hypothesis": True, "refuting_question": "Que fonte pública pode verificar esta área?"}
            for index, row in enumerate(artifact["source_coverage"], 1)
        ]
        with self.assertRaisesRegex(ResearchError, "every fact must be attached"):
            validate_artifact(artifact, {"cap-1"}, {"proof-1"})

    def test_vacancy_and_expansion_indicators_do_not_become_explicit_intent(self):
        artifact = base_artifact()
        self.assertEqual([], artifact["explicit_intent"])
        self.assertEqual(["F2", "F4"], artifact["indicators"])
        validate_artifact(artifact, {"cap-1"}, {"proof-1"})

    def test_unverified_decision_maker_tenure_is_a_gap_not_a_universal_veto(self):
        artifact = base_artifact()
        decider = artifact["committee"][0]
        decider["role_since"] = None
        decider["tenure_status"] = "publicly_unavailable"
        decider["career_summary"] = "Trajetória identificada; início no cargo não consta nas fontes públicas verificadas"
        validated = validate_artifact(artifact, {"cap-1"}, {"proof-1"})
        self.assertEqual("abordar", validated.recommendation)
        self.assertIsNone(validated.committee[0]["role_since"])

    def test_no_explicit_intent_is_allowed_but_a_recent_indicator_is_still_required(self):
        artifact = base_artifact()
        self.assertEqual([], artifact["explicit_intent"])
        self.assertEqual("abordar", validate_artifact(artifact, {"cap-1"}, {"proof-1"}).recommendation)
        for fact in artifact["facts"]:
            if fact["classification"] == "indicator":
                fact["classification"] = "context"
        artifact["indicators"] = []
        artifact["recommendation"] = "abordar"
        with self.assertRaisesRegex(ResearchError, "approach requires current account-bound evidence"):
            validate_artifact(artifact, {"cap-1"}, {"proof-1"})

    def test_deferred_counter_signal_blocks_approach(self):
        artifact = base_artifact()
        artifact["counter_signal_assessment"].update(severity="medium", disposition="defer")
        with self.assertRaisesRegex(ResearchError, "approach requires current account-bound evidence"):
            validate_artifact(artifact, {"cap-1"}, {"proof-1"})

    def test_undated_publication_is_not_fresh(self):
        artifact = base_artifact()
        artifact["facts"][0]["published_at"] = None
        artifact["facts"][0]["freshness"] = "undated"
        artifact["recommendation"] = "pesquisar mais"
        result = validate_artifact(artifact, {"cap-1"}, {"proof-1"})
        self.assertEqual("undated", result.facts[0]["freshness"])

    def test_freshness_uses_the_decided_ninety_day_boundary(self):
        boundary = base_artifact()
        boundary["facts"][0]["published_at"] = (date.today() - timedelta(days=90)).isoformat()
        boundary["recommendation"] = "pesquisar mais"
        self.assertEqual(validate_artifact(boundary, {"cap-1"}, {"proof-1"}).facts[0]["freshness"], "fresh")
        old = base_artifact()
        old["facts"][0]["published_at"] = (date.today() - timedelta(days=91)).isoformat()
        old["facts"][0]["freshness"] = "stale"
        old["recommendation"] = "pesquisar mais"
        self.assertEqual(validate_artifact(old, {"cap-1"}, {"proof-1"}).facts[0]["freshness"], "stale")
        old["facts"][0]["freshness"] = "fresh"
        with self.assertRaisesRegex(ResearchError, "publication or collection date is inconsistent"):
            validate_artifact(old, {"cap-1"}, {"proof-1"})

    def test_contradictory_fit_is_explicit_and_blocks_unapproved_references(self):
        contradictory = base_artifact()
        contradictory["assessment"]["fit"] = "low"
        contradictory["recommendation"] = "acompanhar"
        artifact = validate_artifact(contradictory, {"cap-1"}, {"proof-1"})
        self.assertIn("fit_low_vs_capability", artifact.contradictions)
        contradictory["thunder_fit"]["proof_ids"] = ["invented-proof"]
        with self.assertRaisesRegex(ResearchError, "unapproved Thunder reference"):
            validate_artifact(contradictory, {"cap-1"}, {"proof-1"})

    def test_recommendation_and_required_account_first_fields_are_strict(self):
        invalid = base_artifact()
        invalid["recommendation"] = "enviar mensagem"
        with self.assertRaisesRegex(ResearchError, "invalid recommendation"):
            validate_artifact(invalid, {"cap-1"}, {"proof-1"})
        missing_alternative = base_artifact()
        missing_alternative["alternative_hypothesis"] = ""
        with self.assertRaisesRegex(ResearchError, "hypotheses required"):
            validate_artifact(missing_alternative, {"cap-1"}, {"proof-1"})

    def test_invalid_fact_url_and_purchase_presuming_alternative_are_rejected(self):
        invalid_url = base_artifact()
        invalid_url["facts"][0]["source"] = "not-a-url"
        with self.assertRaisesRegex(ResearchError, "invalid fact provenance"):
            validate_artifact(invalid_url, {"cap-1"}, {"proof-1"})
        purchase = base_artifact()
        purchase["alternative_hypothesis"] = "A empresa vai contratar a Thunder"
        with self.assertRaisesRegex(ResearchError, "must not presume purchase"):
            validate_artifact(purchase, {"cap-1"}, {"proof-1"})

    def test_complete_dossier_returns_separated_assessment_and_governed_evidence(self):
        artifact = validate_artifact(base_artifact(), {"cap-1"}, {"proof-1"})
        self.assertEqual(artifact.assessment, {"fit": "high", "moment": "medium", "data_quality": "high", "risk": "medium"})
        self.assertEqual(artifact.favorable_facts, ("Acme anunciou uma nova fábrica",))
        self.assertEqual(artifact.risks, ("Intenção não confirmada",))
        self.assertEqual(artifact.next_action, "Revisar abordagem")
        self.assertEqual(artifact.knowledge_source, "thunder/empresa.md#capacidades")
        self.assertEqual(artifact.committee_gaps, ())
        self.assertEqual(artifact.account, {"name": "Acme", "domain": "acme.example", "sector": "indústria", "size": "Não verificado", "business_model": "Não verificado", "location": "Não verificado", "digital_presence": "site institucional"})
        self.assertEqual(artifact.facts[0]["fact_id"], "F1")
        self.assertEqual(artifact.facts[0]["classification"], "context")
        self.assertEqual(artifact.facts[0]["published_at"], date.today().isoformat())
        self.assertEqual(artifact.explicit_intent, ())
        self.assertEqual(artifact.indicators, ("F2", "F4"))
        self.assertEqual(artifact.counter_signals, ("F5",))
        self.assertEqual(len(artifact.source_coverage), 10)
        self.assertEqual(len(artifact.evidence_chains), 5)
        self.assertEqual(artifact.committee[0]["tenure_status"], "unverified")
        self.assertEqual(artifact.committee[0]["fact_ids"], ["F3"])
        self.assertEqual(artifact.case_evidence, ({"case_id": "proof-1", "source_ref": "thunder/provas.md#case-1", "statement": "capacidade análoga", "promises_repeat_result": False},))

    def test_case_promises_and_title_as_authority_are_rejected(self):
        promised = base_artifact()
        promised["case_evidence"][0]["promises_repeat_result"] = True
        with self.assertRaisesRegex(ResearchError, "cannot promise"):
            validate_artifact(promised, {"cap-1"}, {"proof-1"})
        title = base_artifact()
        title["committee"][0]["role"] = "CEO"
        with self.assertRaisesRegex(ResearchError, "committee evidence"):
            validate_artifact(title, {"cap-1"}, {"proof-1"})


if __name__ == "__main__":
    unittest.main()
