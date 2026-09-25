import json

import pytest

from offer_finder.core.evaluator import (
    SYSTEM_PROMPT,
    EvaluationError,
    build_user_prompt,
    evaluate,
    is_recommended,
    verify_evidence,
)
from offer_finder.core.models import FieldValue
from tests.helpers.factories import make_checks, make_facts, make_judgment, make_listing, make_spec
from tests.helpers.fakes import FakeProvider


def _llm_response(**overrides):
    values = {
        "product_assessment": {"verdict": "good", "requirements_match": "match", "summary": "65 inch OLED"},
        "deal_assessment": {"verdict": "poor", "summary": "At the top of the budget, no remote"},
        "highlights": [{"claim": "It is an OLED", "evidence": "LG OLED 65 Zoll"}],
        "concerns": [{"claim": "Price at budget limit", "evidence": "fact:price_eur", "severity": "low"}],
        "unknowns": ["age of the TV"],
        "score": 75,
        "confidence": "medium",
    }
    values.update(overrides)
    return values


def _evaluate(provider, threshold=70, checks=None, spec=None):
    return evaluate(
        make_listing(),
        spec if spec is not None else make_spec(),
        search_spec_id=1,
        facts=make_facts(),
        checks=checks or make_checks(),
        provider=provider,
        model_name="gemini-test",
        threshold=threshold,
    )


# --- verify_evidence ---------------------------------------------------------------


def _claims(*evidence):
    return make_judgment(
        highlights=[{"claim": f"c{i}", "evidence": e} for i, e in enumerate(evidence)], concerns=[]
    )


def test_verbatim_quote_is_verified_ignoring_case_and_whitespace():
    judgment = verify_evidence(_claims("lg  OLED 65\nzoll", '"sehr guter Zustand"'), make_facts(), make_checks())
    assert [c.verified for c in judgment.highlights] == [True, True]


def test_paraphrased_or_invented_quote_is_not_verified():
    judgment = verify_evidence(_claims("excellent condition", "inkl. Wandhalterung"), make_facts(), make_checks())
    assert [c.verified for c in judgment.highlights] == [False, False]


def test_fact_citation_is_verified_only_for_known_values():
    judgment = verify_evidence(
        _claims("fact:price_eur", "fact:distance_km", "fact:posted_days_ago", "fact:made_up"),
        make_facts(),
        make_checks(),
    )
    # posted_days_ago is unknown in make_facts() — citing an unknown can't support a claim
    assert [c.verified for c in judgment.highlights] == [True, True, False, False]


def test_verified_flag_from_the_model_is_never_trusted():
    judgment = make_judgment(highlights=[{"claim": "x", "evidence": "nowhere in text", "verified": True}])
    assert verify_evidence(judgment, make_facts(), make_checks()).highlights[0].verified is False


def test_concerns_are_verified_too():
    judgment = make_judgment(
        concerns=[{"claim": "x", "evidence": "mit Fernbedienung", "severity": "low"}], highlights=[]
    )
    assert verify_evidence(judgment, make_facts(), make_checks()).concerns[0].verified is True


# --- is_recommended ----------------------------------------------------------------


def test_recommended_needs_threshold_score():
    assert is_recommended(make_judgment(score=70), make_checks(), threshold=70) is True
    assert is_recommended(make_judgment(score=69), make_checks(), threshold=70) is False


def test_requirements_mismatch_is_never_recommended():
    judgment = make_judgment(
        score=95,
        product_assessment={"verdict": "good", "requirements_match": "mismatch", "summary": "55 inch"},
    )
    assert is_recommended(judgment, make_checks(), threshold=70) is False


def test_hard_fail_is_never_recommended():
    checks = make_checks(hard_fail_reasons=["€750 is €50 over the €700 budget"])
    assert is_recommended(make_judgment(score=95), checks, threshold=70) is False


# --- prompt grounding -------------------------------------------------------------


def test_prompt_contains_spec_text_facts_and_checks():
    facts = make_facts()
    prompt = build_user_prompt(facts, make_checks(distance_km=2.1), make_spec())
    assert "OLED 65 Zoll" in prompt  # spec query
    assert facts.description in prompt
    assert '"posted_days_ago": null' in prompt  # unknown stays explicitly null
    assert '"distance_km": 2.1' in prompt


def test_prompt_without_spec_says_so():
    prompt = build_user_prompt(make_facts(), make_checks(), None)
    assert "No specific search spec" in prompt


def test_system_prompt_forbids_market_value_claims_and_requires_evidence():
    assert "market" in SYSTEM_PROMPT
    assert "fact:<name>" in SYSTEM_PROMPT
    assert "unknowns" in SYSTEM_PROMPT


# --- evaluate ----------------------------------------------------------------------


def test_evaluate_keeps_product_and_deal_quality_separate():
    evaluation = _evaluate(FakeProvider(_llm_response()))
    assert evaluation.judgment.product_assessment.verdict == "good"
    assert evaluation.judgment.deal_assessment.verdict == "poor"
    assert evaluation.judgment.product_assessment.summary != evaluation.judgment.deal_assessment.summary


def test_evaluate_records_everything_needed_for_replay():
    provider = FakeProvider(_llm_response())
    evaluation = _evaluate(provider)
    assert evaluation.user_prompt == provider.prompts[0]
    assert evaluation.raw_response == _llm_response()
    assert evaluation.model == "gemini-test"
    assert evaluation.prompt_version
    assert evaluation.facts == make_facts()


def test_evaluate_verifies_evidence_and_applies_threshold():
    evaluation = _evaluate(FakeProvider(_llm_response(score=75)), threshold=70)
    assert evaluation.judgment.highlights[0].verified is True
    assert evaluation.recommended is True
    assert _evaluate(FakeProvider(_llm_response(score=75)), threshold=80).recommended is False


def test_evaluate_with_missing_price_keeps_it_null_in_prompt():
    facts = make_facts()
    facts.price_eur = FieldValue(value=None, source="none", confidence=0.0)
    provider = FakeProvider(_llm_response())
    evaluate(make_listing(), make_spec(), 1, facts, make_checks(), provider, "m", 70)
    assert '"price_eur": null' in provider.prompts[0]


def test_malformed_response_twice_is_an_evaluation_error():
    with pytest.raises(EvaluationError):
        _evaluate(FakeProvider(json.JSONDecodeError("x", "y", 0), {"score": 500}))


def test_provider_failure_is_an_evaluation_error():
    with pytest.raises(EvaluationError):
        _evaluate(FakeProvider(RuntimeError("429 Too Many Requests")))


def test_prompt_includes_structured_attributes_and_they_are_citeable():
    facts = make_facts().model_copy(update={"attributes": {"Versand": "Nur Abholung"}})
    assert '"Versand": "Nur Abholung"' in build_user_prompt(facts, make_checks(), make_spec())
    judgment = verify_evidence(_claims("fact:attributes"), facts, make_checks())
    assert judgment.highlights[0].verified is True
