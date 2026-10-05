"""Validation contract for evidence-backed, account-first ABM research."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
from typing import Any, Mapping
from urllib.parse import urlsplit


RECOMMENDATIONS = {"abordar", "acompanhar", "pesquisar mais", "não priorizar"}
RATINGS = {"low", "medium", "high", "unknown"}
SOURCE_AREAS = {
    "company_site", "company_news", "company_social", "google_ads", "meta_ads",
    "linkedin_ads", "hiring", "sector", "decision_maker", "other_public_source",
}
SOURCE_CLASSES = {"explicit_intent", "indicator", "counter_signal", "context"}
COVERAGE_STATUSES = {"planned", "attempted", "checked_with_evidence", "checked_no_result", "not_accessible", "not_provided"}
NO_EVIDENCE_VALUE_HYPOTHESIS = "No account-specific value hypothesis can be supported from the supplied evidence."
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_ACCOUNT_AREAS = {"company_site", "company_news", "company_social", "google_ads", "meta_ads", "linkedin_ads", "hiring"}


class ResearchError(ValueError):
    pass


@dataclass(frozen=True)
class ResearchArtifact:
    account: Mapping[str, Any]
    facts: tuple[Mapping[str, Any], ...]
    explicit_intent: tuple[str, ...]
    indicators: tuple[str, ...]
    counter_signals: tuple[str, ...]
    counter_signal_assessment: Mapping[str, Any]
    research_gaps: tuple[Mapping[str, Any], ...]
    source_coverage: tuple[Mapping[str, Any], ...]
    evidence_chains: tuple[Mapping[str, Any], ...]
    inferences: tuple[str, ...]
    opportunity_hypothesis: str
    alternative_hypothesis: str
    assessment: Mapping[str, str]
    committee: tuple[Mapping[str, Any], ...]
    capability_ids: tuple[str, ...]
    proof_ids: tuple[str, ...]
    knowledge_source: str
    case_evidence: tuple[Mapping[str, Any], ...]
    committee_gaps: tuple[str, ...]
    missing: tuple[str, ...]
    limitations: tuple[str, ...]
    recommendation: str
    favorable_facts: tuple[str, ...]
    risks: tuple[str, ...]
    next_action: str
    contradictions: tuple[str, ...]


def _nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchError(f"{label} required")
    return value.strip()


def _date_value(value: Any, label: str, *, nullable: bool = False) -> date | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise ResearchError(f"invalid {label} date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ResearchError(f"invalid {label} date") from exc


def _as_text_list(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ResearchError(f"invalid {label}")
    if len(value) != len(set(value)):
        raise ResearchError(f"duplicate {label}")
    return tuple(item.strip() for item in value)


def _registrable_host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    labels = host.split(".")
    # A small conservative set prevents common country-code second-level domains
    # from being mistaken for independent publishers.
    if len(labels) >= 3 and ".".join(labels[-2:]) in {"co.uk", "com.br", "com.au", "co.nz", "co.jp"}:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def _ready_for_approach(research: Mapping[str, Any], *, today: date) -> bool:
    """Require a sound current account hypothesis; buyer tenure is not a universal veto."""
    facts = research.get("facts")
    coverage = research.get("source_coverage")
    fit = research.get("thunder_fit")
    capability_ids = research.get("capability_ids")
    if capability_ids is None and isinstance(fit, Mapping):
        capability_ids = fit.get("capability_ids")
    if (not isinstance(facts, (list, tuple)) or len(facts) < 2
            or not isinstance(coverage, (list, tuple)) or len(coverage) != len(SOURCE_AREAS)
            or not capability_ids or not research.get("counter_signal_assessment")):
        return False
    counter_assessment = research.get("counter_signal_assessment")
    if not isinstance(counter_assessment, Mapping) or counter_assessment.get("status") not in {"signals_found", "no_signals_found", "not_assessed"}:
        return False

    fact_by_id = {fact.get("fact_id"): fact for fact in facts if isinstance(fact, Mapping)}
    if len(fact_by_id) != len(facts) or not fact_by_id:
        return False
    current_facts = []
    current_signals = []
    for fact in facts:
        try:
            collected = _date_value(fact.get("observed_at"), "collection")
            published = _date_value(fact.get("published_at"), "publication", nullable=True)
        except ResearchError:
            return False
        if collected is None or not 0 <= (today - collected).days <= 14:
            return False
        current_facts.append(fact)
        if (fact.get("classification") in {"explicit_intent", "indicator"}
                and published is not None and 0 <= (today - published).days <= 180):
            current_signals.append(fact)
    if len(current_facts) < 2 or not current_signals:
        return False

    current_ids = {fact["fact_id"] for fact in current_facts}
    areas = {fact.get("source_area") for fact in current_facts}
    if not (areas & _ACCOUNT_AREAS):
        return False
    account = research.get("account")
    if not isinstance(account, Mapping) or not isinstance(account.get("domain"), str):
        return False
    domain = account["domain"].lower().strip().split(":", 1)[0]
    if not domain or len({fact.get("source") for fact in current_facts}) < 2:
        return False
    identity_supported = any(
        fact.get("source_area") == "company_site"
        and _registrable_host(str(fact.get("source", ""))) == _registrable_host("https://" + domain)
        for fact in current_facts
    )
    if not identity_supported:
        return False
    references_by_class = {
        "explicit_intent": set(research.get("explicit_intent", ())),
        "indicator": set(research.get("indicators", ())),
        "counter_signal": set(research.get("counter_signals", ())),
    }
    for classification, references in references_by_class.items():
        actual = {fact_id for fact_id, fact in fact_by_id.items() if fact.get("classification") == classification}
        if references != actual:
            return False
    if ((counter_assessment.get("status") == "signals_found") != bool(references_by_class["counter_signal"])
            or set(counter_assessment.get("fact_ids", ())) != references_by_class["counter_signal"]):
        return False
    if counter_assessment.get("disposition") in {"defer", "disqualify"}:
        return False
    gaps = research.get("research_gaps")
    if (not isinstance(gaps, (list, tuple))
            or any(isinstance(gap, Mapping) and gap.get("blocks_hypothesis") is True for gap in gaps)):
        return False

    coverage_by_area = {entry.get("area"): entry for entry in coverage if isinstance(entry, Mapping)}
    if set(coverage_by_area) != SOURCE_AREAS:
        return False
    gaps = research.get("research_gaps", ())
    for area, entry in coverage_by_area.items():
        if entry.get("status") in {"planned", "attempted", "not_accessible", "not_provided"}:
            if not any(isinstance(gap, Mapping) and gap.get("topic") == "source_access"
                       and (area.casefold() in str(gap.get("description", "")).casefold()
                            or str(entry.get("details", "")).casefold() in str(gap.get("description", "")).casefold())
                       for gap in gaps):
                return False
    covered_fact_ids = set()
    for area, entry in coverage_by_area.items():
        if entry.get("status") not in COVERAGE_STATUSES:
            return False
        try:
            checked_at = _date_value(entry.get("checked_at"), "coverage", nullable=True)
        except ResearchError:
            return False
        if entry.get("status") in {"checked_with_evidence", "checked_no_result", "not_accessible", "attempted"}:
            if checked_at is None or not 0 <= (today - checked_at).days <= 14:
                return False
        elif checked_at is not None:
            return False
        refs = entry.get("fact_ids")
        if not isinstance(refs, (list, tuple)) or any(ref not in fact_by_id for ref in refs):
            return False
        if any(fact_by_id[ref].get("source_area") != area for ref in refs):
            return False
        covered_fact_ids.update(refs)
        if entry.get("status") == "checked_with_evidence" and (not refs or not entry.get("source_urls")):
            return False
        if entry.get("status") != "checked_with_evidence" and refs:
            return False
        if entry.get("status") in {"planned", "not_provided"} and entry.get("attempted_urls"):
            return False
        if entry.get("status") in {"attempted", "checked_no_result", "not_accessible"} and not entry.get("attempted_urls"):
            return False
    if covered_fact_ids != set(fact_by_id):
        return False

    people = research.get("committee")
    if not isinstance(people, (list, tuple)):
        return False
    return all(fact.get("classification") in SOURCE_CLASSES for fact in current_facts)


def validate_artifact(
    data: Mapping[str, Any],
    approved_capabilities: set[str],
    approved_proofs: set[str],
) -> ResearchArtifact:
    required = {
        "account", "facts", "explicit_intent", "indicators", "counter_signals",
        "counter_signal_assessment", "research_gaps", "source_coverage", "evidence_chains", "inferences",
        "opportunity_hypothesis", "alternative_hypothesis", "assessment", "committee",
        "committee_gaps", "thunder_fit", "case_evidence", "missing", "limitations",
        "recommendation", "favorable_facts", "risks", "next_action",
    }
    if set(data) != required:
        raise ResearchError("invalid dossier fields")
    account = data["account"]
    account_fields = ("name", "domain", "sector", "size", "business_model", "location", "digital_presence")
    if not isinstance(account, Mapping) or set(account) != set(account_fields) or not all(
        isinstance(account.get(field), str) and account[field].strip() for field in account_fields
    ):
        raise ResearchError("account-first identity required")

    facts = data["facts"]
    if not isinstance(facts, list):
        raise ResearchError("facts must be a list")
    fact_by_id: dict[str, dict[str, Any]] = {}
    for fact in facts:
        fact_fields = {"fact_id", "statement", "source", "source_area", "published_at", "observed_at", "freshness", "relevance", "classification"}
        if (not isinstance(fact, Mapping) or set(fact) != fact_fields
                or not isinstance(fact.get("fact_id"), str) or not re.fullmatch(r"F\d+", fact["fact_id"])
                or not isinstance(fact.get("statement"), str) or not fact["statement"].strip()
                or not isinstance(fact.get("relevance"), str) or not fact["relevance"].strip()
                or re.fullmatch(r"https?://\S+", str(fact.get("source"))) is None
                or fact.get("source_area") not in SOURCE_AREAS
                or fact.get("classification") not in SOURCE_CLASSES
                or fact.get("freshness") not in {"fresh", "stale", "undated"}):
            raise ResearchError("invalid fact provenance")
        if fact["fact_id"] in fact_by_id:
            raise ResearchError("duplicate fact id")
        published = _date_value(fact["published_at"], "publication", nullable=True)
        collected = _date_value(fact["observed_at"], "collection")
        age = (date.today() - published).days if published is not None else None
        expected_freshness = "undated" if published is None else "fresh" if 0 <= age <= 90 else "stale"
        if fact["freshness"] != expected_freshness or collected > date.today():
            raise ResearchError("fact publication or collection date is inconsistent")
        fact_by_id[fact["fact_id"]] = dict(fact)

    explicit_intent = _as_text_list(data["explicit_intent"], "explicit intent references")
    indicators = _as_text_list(data["indicators"], "indicator references")
    counter_signals = _as_text_list(data["counter_signals"], "counter-signal references")
    for label, refs, classification in (
        ("explicit intent", explicit_intent, "explicit_intent"),
        ("indicator", indicators, "indicator"),
        ("counter-signal", counter_signals, "counter_signal"),
    ):
        if any(ref not in fact_by_id or fact_by_id[ref]["classification"] != classification for ref in refs):
            raise ResearchError(f"invalid {label} fact references")
    if set(explicit_intent) & set(indicators) or set(explicit_intent) & set(counter_signals) or set(indicators) & set(counter_signals):
        raise ResearchError("fact classification lists must be disjoint")
    class_references = {
        "explicit_intent": set(explicit_intent), "indicator": set(indicators),
        "counter_signal": set(counter_signals),
    }
    for classification, refs in class_references.items():
        actual = {fact_id for fact_id, fact in fact_by_id.items() if fact["classification"] == classification}
        if actual != refs:
            raise ResearchError("fact classification lists must include every classified fact")
    counter_signal_assessment = data["counter_signal_assessment"]
    if (not isinstance(counter_signal_assessment, Mapping)
            or set(counter_signal_assessment) != {"status", "details", "fact_ids", "severity", "disposition", "qualification_question"}
            or counter_signal_assessment.get("status") not in {"signals_found", "no_signals_found", "not_assessed"}):
        raise ResearchError("invalid counter-signal assessment")
    _nonempty(counter_signal_assessment["details"], "counter-signal assessment details")
    _nonempty(counter_signal_assessment["qualification_question"], "counter-signal qualification question")
    counter_assessment_refs = _as_text_list(counter_signal_assessment["fact_ids"], "counter-signal assessment references")
    if counter_assessment_refs != counter_signals:
        raise ResearchError("counter-signal assessment must cite the counter-signal list")
    if counter_signal_assessment["status"] == "signals_found" and not counter_signals:
        raise ResearchError("signals_found requires counter-signal facts")
    if counter_signal_assessment["status"] == "no_signals_found" and counter_signals:
        raise ResearchError("no_signals_found cannot cite counter-signal facts")
    severity = counter_signal_assessment["severity"]
    disposition = counter_signal_assessment["disposition"]
    if counter_signal_assessment["status"] == "no_signals_found" and (severity != "none" or disposition != "proceed"):
        raise ResearchError("no counter-signals requires none severity and proceed disposition")
    if counter_signal_assessment["status"] == "not_assessed" and (severity != "unknown" or disposition not in {"qualify", "defer"} or counter_signals):
        raise ResearchError("unassessed counter-signals require unknown severity and qualification")
    if counter_signal_assessment["status"] == "signals_found":
        allowed_dispositions = {"low": {"proceed", "qualify"}, "medium": {"qualify", "defer"}, "high": {"defer", "disqualify"}}
        if severity not in allowed_dispositions or disposition not in allowed_dispositions[severity]:
            raise ResearchError("counter-signal severity and disposition are inconsistent")

    research_gaps = data["research_gaps"]
    if not isinstance(research_gaps, list):
        raise ResearchError("invalid research gaps")
    gap_descriptions = set()
    gap_ids = set()
    gap_topics = {"identity", "signal", "decision_maker", "tenure", "budget", "source_access", "fit", "other"}
    for gap in research_gaps:
        if (not isinstance(gap, Mapping) or set(gap) != {"gap_id", "topic", "description", "severity", "blocks_hypothesis", "refuting_question"}
                or not isinstance(gap.get("gap_id"), str) or not re.fullmatch(r"G\d+", gap["gap_id"])
                or gap.get("topic") not in gap_topics or gap.get("severity") not in {"low", "medium", "high"}
                or type(gap.get("blocks_hypothesis")) is not bool):
            raise ResearchError("invalid research gap")
        if gap["gap_id"] in gap_ids:
            raise ResearchError("duplicate research gap id")
        gap_ids.add(gap["gap_id"])
        gap_descriptions.add(_nonempty(gap["description"], "research gap description"))
        _nonempty(gap["refuting_question"], "research gap refuting question")

    source_coverage = data["source_coverage"]
    if not isinstance(source_coverage, list):
        raise ResearchError("invalid source coverage")
    coverage_by_area = {}
    for item in source_coverage:
        fields = {"area", "status", "query", "checked_at", "planned_urls", "attempted_urls", "discovered_urls", "source_urls", "fact_ids", "details"}
        if (not isinstance(item, Mapping) or set(item) != fields
                or item.get("area") not in SOURCE_AREAS or item.get("status") not in COVERAGE_STATUSES
                or item["area"] in coverage_by_area):
            raise ResearchError("invalid source coverage")
        _nonempty(item["query"], "source coverage query")
        details = _nonempty(item["details"], "source coverage details")
        checked_at = _date_value(item["checked_at"], "coverage", nullable=True)
        planned_urls = _as_text_list(item["planned_urls"], "planned source URLs")
        attempted_urls = _as_text_list(item["attempted_urls"], "attempted source URLs")
        discovered_urls = _as_text_list(item["discovered_urls"], "discovered source URLs")
        source_urls = _as_text_list(item["source_urls"], "evidence source URLs")
        if any(re.fullmatch(r"https://\S+", url) is None for url in (*planned_urls, *attempted_urls, *discovered_urls, *source_urls)):
            raise ResearchError("invalid source coverage URL")
        refs = _as_text_list(item["fact_ids"], "coverage fact references")
        if any(ref not in fact_by_id or fact_by_id[ref]["source_area"] != item["area"] for ref in refs):
            raise ResearchError("source coverage references a fact from another area")
        if item["status"] == "checked_with_evidence" and (not refs or not source_urls or checked_at is None or not attempted_urls):
            raise ResearchError("evidence coverage requires dated facts")
        if item["status"] in {"checked_no_result", "not_accessible", "attempted", "planned", "not_provided"} and (refs or source_urls):
            raise ResearchError("negative or inaccessible coverage cannot cite positive facts")
        if item["status"] in {"checked_no_result", "not_accessible", "attempted"} and (checked_at is None or not attempted_urls):
            raise ResearchError("attempted source coverage requires a date and request URL")
        if item["status"] == "planned" and (checked_at is not None or attempted_urls or not planned_urls):
            raise ResearchError("planned source coverage cannot claim an attempted request")
        if item["status"] == "not_provided" and (checked_at is not None or attempted_urls or planned_urls):
            raise ResearchError("unprovided coverage cannot claim a planned or attempted search")
        if item["status"] == "checked_with_evidence" and not set(source_urls) <= set(attempted_urls):
            raise ResearchError("evidence source was not among attempted URLs")
        if item["status"] == "checked_with_evidence" and set(source_urls) != {
            fact_by_id[ref]["source"] for ref in refs
        }:
            raise ResearchError("evidence URLs must match the cited facts exactly")
        if item["status"] == "not_accessible" and not details:
            raise ResearchError("inaccessible source requires a limitation")
        coverage_by_area[item["area"]] = {**dict(item), "details": details}
    if set(coverage_by_area) != SOURCE_AREAS:
        raise ResearchError("source coverage must account for all public research areas")
    for area, entry in coverage_by_area.items():
        if entry["status"] in {"planned", "attempted", "not_accessible", "not_provided"}:
            if not any(gap.get("topic") == "source_access"
                       and (area.casefold() in gap["description"].casefold()
                            or entry["details"].casefold() in gap["description"].casefold())
                       for gap in research_gaps):
                raise ResearchError("incomplete source coverage requires a named research gap")
    if any(fact["fact_id"] not in {ref for item in source_coverage for ref in item["fact_ids"]} for fact in facts):
        raise ResearchError("every fact must be attached to checked source coverage")
    if counter_signal_assessment["status"] == "no_signals_found" and any(
        item["status"] in {"not_provided", "planned", "attempted"} for item in source_coverage
    ):
        raise ResearchError("no_signals_found requires checked or unavailable public source coverage")

    evidence_chains = data["evidence_chains"]
    if not isinstance(evidence_chains, list):
        raise ResearchError("invalid evidence chains")
    chained = set()
    for chain in evidence_chains:
        if (not isinstance(chain, Mapping) or set(chain) != {"fact_ids", "implication", "alternative", "value_hypothesis", "refuting_question"}):
            raise ResearchError("invalid evidence chain")
        refs = _as_text_list(chain["fact_ids"], "evidence-chain fact references")
        if not refs or any(ref not in fact_by_id for ref in refs):
            raise ResearchError("evidence chain must cite dossier facts")
        chained.update(refs)
        for field in ("implication", "alternative", "value_hypothesis", "refuting_question"):
            _nonempty(chain[field], f"evidence chain {field}")
    if set(fact_by_id) != chained:
        raise ResearchError("every fact requires an evidence chain")

    inferences = data["inferences"]
    if not isinstance(inferences, list) or any(not isinstance(value, str) or not value.strip() for value in inferences):
        raise ResearchError("invalid inferences")
    chain_implications = {chain["implication"].strip() for chain in evidence_chains}
    if any(value.strip() not in chain_implications for value in inferences):
        raise ResearchError("each inference must match a cited evidence-chain implication")
    opportunity = _nonempty(data["opportunity_hypothesis"], "hypotheses")
    chain_value_hypotheses = {chain["value_hypothesis"].strip() for chain in evidence_chains}
    if evidence_chains and opportunity not in chain_value_hypotheses:
        raise ResearchError("opportunity hypothesis must match a cited evidence-chain value hypothesis")
    if not facts and opportunity != NO_EVIDENCE_VALUE_HYPOTHESIS:
        raise ResearchError("empty evidence requires the explicit unsupported opportunity statement")
    alternative = _nonempty(data["alternative_hypothesis"], "hypotheses")
    if re.search(r"\b(comprar|contratar|adquirir|vai comprar|quer comprar)\b", alternative, re.I):
        raise ResearchError("alternative hypothesis must not presume purchase")
    assessment = data["assessment"]
    dimensions = {"fit", "moment", "data_quality", "risk"}
    if not isinstance(assessment, Mapping) or set(assessment) != dimensions or any(value not in RATINGS for value in assessment.values()):
        raise ResearchError("invalid separated assessment")

    committee = data["committee"]
    if not isinstance(committee, list):
        raise ResearchError("invalid committee")
    for member in committee:
        member_fields = {"person", "role", "evidence", "confidence", "role_since", "career_summary", "tenure_status", "fact_ids"}
        if (not isinstance(member, Mapping) or set(member) != member_fields
                or not all(isinstance(member.get(field), str) and member[field].strip() for field in ("person", "evidence", "career_summary"))
                or re.fullmatch(r"https?://\S+", member["evidence"]) is None
                or member.get("confidence") not in {"low", "medium", "high"}
                or member.get("role") not in {"decision_maker", "influencer", "champion", "user"}
                or member.get("tenure_status") not in {"verified", "publicly_unavailable", "unverified"}):
            raise ResearchError("invalid committee evidence")
        role_since = _date_value(member["role_since"], "role start", nullable=True)
        refs = _as_text_list(member["fact_ids"], "committee fact references")
        if any(ref not in fact_by_id or fact_by_id[ref]["source_area"] != "decision_maker" for ref in refs):
            raise ResearchError("committee facts must come from decision-maker sources")
        if member["tenure_status"] == "verified" and (role_since is None or not refs):
            raise ResearchError("verified tenure requires a dated role source")
        if role_since is not None and role_since > date.today():
            raise ResearchError("role start date cannot be in the future")
        if member["tenure_status"] != "verified" and role_since is not None:
            raise ResearchError("unverified tenure cannot claim a role start date")

    committee_gaps = _as_text_list(data["committee_gaps"], "committee gaps")
    fit = data["thunder_fit"]
    if not isinstance(fit, Mapping) or set(fit) != {"capability_ids", "proof_ids", "knowledge_source"}:
        raise ResearchError("invalid Thunder fit")
    if not isinstance(fit["capability_ids"], list) or not isinstance(fit["proof_ids"], list):
        raise ResearchError("invalid Thunder fit references")
    capability_ids = tuple(_as_text_list(fit["capability_ids"], "capability references"))
    proof_ids = tuple(_as_text_list(fit["proof_ids"], "proof references"))
    if not set(capability_ids) <= approved_capabilities or not set(proof_ids) <= approved_proofs:
        raise ResearchError("unapproved Thunder reference")
    knowledge_source = _nonempty(fit["knowledge_source"], "knowledge source")
    case_evidence = data["case_evidence"]
    if not isinstance(case_evidence, list):
        raise ResearchError("invalid case evidence")
    for case in case_evidence:
        if (not isinstance(case, Mapping) or set(case) != {"case_id", "source_ref", "statement", "promises_repeat_result"}
                or not all(isinstance(case.get(field), str) and case[field].strip() for field in ("case_id", "source_ref", "statement"))
                or case["case_id"] not in approved_proofs or case["promises_repeat_result"] is not False):
            raise ResearchError("case evidence cannot promise repeated results")

    missing = _as_text_list(data["missing"], "missing evidence")
    committee_gaps = _as_text_list(data["committee_gaps"], "committee gaps")
    if not (set(missing) | set(committee_gaps)) <= gap_descriptions:
        raise ResearchError("research gaps must classify every missing or committee item")
    limitations = _as_text_list(data["limitations"], "research limitations")
    inaccessible_details = {item["details"] for item in source_coverage if item["status"] == "not_accessible"}
    if not inaccessible_details <= set(limitations):
        raise ResearchError("inaccessible sources must be listed as research limitations")
    recommendation = data["recommendation"]
    if recommendation not in RECOMMENDATIONS:
        raise ResearchError("invalid recommendation")
    if recommendation == "abordar" and not _ready_for_approach(data, today=date.today()):
        raise ResearchError("approach requires current account-bound evidence, a recent signal, coverage gaps and non-blocking counter-signals")
    contradictions = []
    if assessment["fit"] == "low" and capability_ids:
        contradictions.append("fit_low_vs_capability")
    return ResearchArtifact(
        account=dict(account), facts=tuple(fact_by_id.values()), explicit_intent=explicit_intent,
        indicators=indicators, counter_signals=counter_signals,
        counter_signal_assessment=dict(counter_signal_assessment), research_gaps=tuple(dict(gap) for gap in research_gaps),
        source_coverage=tuple(coverage_by_area[area] for area in sorted(coverage_by_area)),
        evidence_chains=tuple(dict(chain) for chain in evidence_chains), inferences=tuple(inferences),
        opportunity_hypothesis=opportunity, alternative_hypothesis=alternative,
        assessment=dict(assessment), committee=tuple(dict(member) for member in committee),
        capability_ids=capability_ids, proof_ids=proof_ids, knowledge_source=knowledge_source,
        case_evidence=tuple(dict(case) for case in case_evidence), committee_gaps=committee_gaps,
        missing=missing, limitations=limitations, recommendation=recommendation,
        favorable_facts=tuple(_nonempty(v, "favorable fact") for v in data["favorable_facts"]),
        risks=tuple(_nonempty(v, "risk") for v in data["risks"]),
        next_action=_nonempty(data["next_action"], "next action"), contradictions=tuple(contradictions),
    )


def research_is_current(research: Mapping[str, Any] | None, *, today: date | None = None) -> bool:
    """Recheck quality and source age at use time, never trust a cached status alone."""
    if not research or research.get("status") != "validated" or research.get("recommendation") != "abordar":
        return False
    try:
        return _ready_for_approach(research, today=today or date.today())
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return False
