"""Pure unit tests for the MVP risk engine.

No DB, no HTTP: ``assess_risk`` takes an ``AnalysisInputs`` snapshot, so every
case here builds inputs in memory and asserts on the returned assessment.
"""

import inspect
import json
import uuid
from decimal import Decimal

from app.services.risk import engine as engine_module
from app.services.risk.engine import (
    CALIBRATION_NOTE,
    AnalysisInputs,
    assess_risk,
    contributor_anomaly,
    contributor_duplicate,
    contributor_extraction_confidence,
    contributor_missing_fields,
    contributor_po_mismatch,
    contributor_policy,
    contributor_validation,
)
from app.services.risk.types import RecommendedAction, RiskLevel
from app.services.risk.weights import (
    CAP_DUPLICATE,
    CAP_LOW_EXTRACTION_CONFIDENCE,
    CAP_MISSING_REQUIRED_FIELD,
    CAP_PO_MISMATCH,
    CAP_POLICY_UNCERTAINTY,
    CAP_STRONG_ANOMALY,
    CAP_VALIDATION_FAILURE,
    MAX_SCORE,
    REJECT_MIN,
    REQUIRED_FIELDS,
    RISK_LEVEL_HIGH_MIN,
    RISK_LEVEL_MEDIUM_MIN,
)

#: A fully-clean baseline. Individual tests override exactly one field so each
#: assertion isolates a single rule.
CLEAN = {
    "duplicate_verdict": "unique",
    "po_match_verdict": "match",
    "validation_failed_count": 0,
    "validation_warning_count": 0,
    "anomaly_verdict": "normal",
    # None = policy check never ran, so the baseline is genuinely zero-risk
    # and each override isolates exactly one rule.
    "policy_evidence_found": None,
    "policy_evidence_count": 0,
    "extraction_confidence": {"vendor_name": 0.95, "invoice_number": 0.95},
    "missing_required_fields": [],
}

#: Every category firing at its worst legal value.
STACKED = {
    "duplicate_verdict": "duplicate",
    "po_match_verdict": "mismatch",
    "validation_failed_count": 5,
    "validation_warning_count": 0,
    "anomaly_verdict": "strong_anomaly",
    "policy_evidence_found": False,
    "policy_evidence_count": 0,
    "extraction_confidence": {f"field_{i}": 0.1 for i in range(5)},
    "missing_required_fields": list(REQUIRED_FIELDS),
}


def _inputs(**overrides: object) -> AnalysisInputs:
    """Build an AnalysisInputs from the clean baseline plus overrides."""
    params = {**CLEAN, **overrides}
    return AnalysisInputs(**params)


def _assess(**overrides: object):
    """Assess the clean baseline with overrides applied."""
    return assess_risk(_inputs(**overrides), uuid.uuid4())


def _one(assessment, category: str):
    """Return the single contributor in a category, asserting there is one."""
    found = [c for c in assessment.contributors if c.category == category]
    assert len(found) == 1, f"expected exactly one {category} contributor: {found}"
    return found[0]


def test_no_inputs_returns_zero_low_auto_approve() -> None:
    a = assess_risk(AnalysisInputs(), uuid.uuid4())
    assert a.score == 0
    assert a.contributing_total == 0
    assert a.level == RiskLevel.LOW
    assert a.recommended_action == RecommendedAction.AUTO_APPROVE
    assert a.contributors == []
    assert a.caps_applied is False
    assert CALIBRATION_NOTE in a.notes


def test_confirmed_duplicate_adds_cap_points() -> None:
    c = contributor_duplicate("duplicate")
    assert c is not None
    assert c.points == CAP_DUPLICATE == 35
    assert c.cap == CAP_DUPLICATE
    assert c.severity == "error"
    assert c.message == "Confirmed duplicate of an existing invoice."
    a = _assess(duplicate_verdict="duplicate")
    assert a.score == CAP_DUPLICATE


def test_suspected_duplicate_adds_half_cap() -> None:
    c = contributor_duplicate("suspected_duplicate")
    assert c is not None
    assert c.points == CAP_DUPLICATE // 2 == 17
    assert c.cap == CAP_DUPLICATE
    assert c.severity == "warn"
    assert c.message == "Possible duplicate — corroborating signals found."


def test_unique_duplicate_is_no_contributor() -> None:
    assert contributor_duplicate("unique") is None
    assert contributor_duplicate(None) is None
    assert [c for c in _assess(duplicate_verdict="unique").contributors
            if c.category == "duplicate"] == []


def test_po_mismatch_adds_cap_points() -> None:
    c = contributor_po_mismatch("mismatch")
    assert c is not None
    assert c.points == CAP_PO_MISMATCH == 25
    assert c.cap == CAP_PO_MISMATCH
    assert c.severity == "error"


def test_po_partial_match_adds_half_cap() -> None:
    c = contributor_po_mismatch("partial_match")
    assert c is not None
    assert c.points == CAP_PO_MISMATCH // 2 == 12
    assert c.severity == "warn"


def test_po_skipped_is_no_contributor() -> None:
    # "skipped" means there was no PO to match against -- not a finding.
    assert contributor_po_mismatch("skipped") is None
    assert contributor_po_mismatch("match") is None
    assert contributor_po_mismatch(None) is None


def test_validation_first_failure_is_15_points() -> None:
    c = contributor_validation(1, 0)
    assert c is not None
    assert c.points == 15
    assert c.severity == "error"


def test_validation_three_failures_hits_cap() -> None:
    c = contributor_validation(3, 0)
    assert c is not None
    # 15 + 5 * (3 - 1) = 25 == CAP_VALIDATION_FAILURE
    assert c.points == CAP_VALIDATION_FAILURE == 25


def test_validation_warnings_alone_are_half_cap_ish() -> None:
    single = contributor_validation(0, 1)
    many = contributor_validation(0, 10)
    assert single is not None and many is not None
    assert single.points == 8
    assert single.severity == "warn"
    assert many.points == CAP_VALIDATION_FAILURE // 2 == 12


def test_validation_failures_outrank_warnings() -> None:
    # Only one validation contributor is emitted, and it reports the failure.
    a = _assess(validation_failed_count=1, validation_warning_count=4)
    c = _one(a, "validation")
    assert c.points == 15
    assert c.severity == "error"


def test_strong_anomaly_adds_cap_points() -> None:
    c = contributor_anomaly("strong_anomaly")
    assert c is not None
    assert c.points == CAP_STRONG_ANOMALY == 20
    assert c.severity == "error"


def test_mild_anomaly_adds_half_cap() -> None:
    c = contributor_anomaly("mild_anomaly")
    assert c is not None
    assert c.points == CAP_STRONG_ANOMALY // 2 == 10
    assert c.severity == "warn"


def test_insufficient_history_is_no_contributor() -> None:
    # Critical: insufficient history must not contribute to risk. Absence of
    # evidence is not evidence of absence.
    assert contributor_anomaly("insufficient_history") is None
    assert contributor_anomaly("normal") is None
    a = _assess(anomaly_verdict="insufficient_history")
    assert a.contributors == []
    assert a.score == 0


def test_policy_evidence_found_is_soft_signal() -> None:
    # The baseline leaves the policy check unrun (contributing nothing);
    # running it and finding evidence adds a small "someone should look" note.
    baseline = _assess()
    assert [c for c in baseline.contributors if c.category == "policy"] == []

    c = contributor_policy(True, 3)
    assert c is not None
    assert c.points == CAP_POLICY_UNCERTAINTY // 2 == 5
    assert c.severity == "info"
    assert c.message == (
        "Policy evidence retrieved for review; "
        "a human may need to confirm applicability."
    )


def test_policy_evidence_absent_is_higher_signal() -> None:
    absent = contributor_policy(False, 0)
    found = contributor_policy(True, 3)
    assert absent is not None and found is not None
    assert absent.points == CAP_POLICY_UNCERTAINTY == 10
    assert absent.severity == "warn"
    assert absent.message == "No applicable policy evidence found."
    # Absence when a check ran outweighs presence of evidence.
    assert absent.points > found.points


def test_policy_check_not_run_is_no_contributor() -> None:
    assert contributor_policy(None, 0) is None
    assert contributor_policy(None, 5) is None
    assert [c for c in _assess(policy_evidence_found=None).contributors
            if c.category == "policy"] == []


def test_low_extraction_confidence_contributes() -> None:
    c = contributor_extraction_confidence(
        {"vendor_name": 0.5, "invoice_number": 0.4, "total_amount": 0.9}
    )
    assert c is not None
    # 2 low fields -> min(20, 5 + 3 * 2) = 11
    assert c.points == 11
    assert c.cap == CAP_LOW_EXTRACTION_CONFIDENCE
    assert c.severity == "warn"
    assert c.evidence["low_confidence_count"] == 2
    assert c.evidence["low_confidence_fields"] == {
        "invoice_number": 0.4,
        "vendor_name": 0.5,
    }


def test_extraction_confidence_contribution_is_capped() -> None:
    c = contributor_extraction_confidence({f"f{i}": 0.1 for i in range(20)})
    assert c is not None
    assert c.points == CAP_LOW_EXTRACTION_CONFIDENCE == 20


def test_high_extraction_confidence_is_no_contributor() -> None:
    assert contributor_extraction_confidence({}) is None
    assert contributor_extraction_confidence({"vendor_name": 0.95}) is None
    assert [c for c in _assess().contributors if c.category == "extraction"] == []


def test_missing_required_fields_contribute() -> None:
    c = contributor_missing_fields(["total_amount"])
    assert c is not None
    # 5 * 1 missing field
    assert c.points == 5
    assert c.cap == CAP_MISSING_REQUIRED_FIELD == 10
    assert c.evidence["missing_fields"] == ["total_amount"]


def test_missing_required_fields_contribution_is_capped() -> None:
    c = contributor_missing_fields(list(REQUIRED_FIELDS) * 5)
    assert c is not None
    assert c.points == CAP_MISSING_REQUIRED_FIELD == 10


def test_contributors_sum_below_cap() -> None:
    a = _assess(duplicate_verdict="duplicate", po_match_verdict="mismatch")
    assert a.contributing_total == CAP_DUPLICATE + CAP_PO_MISMATCH == 60
    assert a.score == a.contributing_total
    assert a.caps_applied is False


def test_global_cap_applies_when_sum_exceeds_100() -> None:
    a = _assess(**STACKED)
    assert a.contributing_total > MAX_SCORE
    assert a.score == MAX_SCORE == 100
    assert a.caps_applied is True
    assert any(f"raw total was {a.contributing_total}" in n for n in a.notes)


def test_contributor_caps_are_respected() -> None:
    a = _assess(**STACKED)
    expected = {
        "duplicate": CAP_DUPLICATE,
        "po_match": CAP_PO_MISMATCH,
        "validation": CAP_VALIDATION_FAILURE,
        "anomaly": CAP_STRONG_ANOMALY,
        "policy": CAP_POLICY_UNCERTAINTY,
        "extraction": CAP_LOW_EXTRACTION_CONFIDENCE,
        "completeness": CAP_MISSING_REQUIRED_FIELD,
    }
    assert {c.category for c in a.contributors} == set(expected)
    for c in a.contributors:
        assert 0 < c.points <= c.cap
        assert c.cap == expected[c.category]


def test_high_score_recommends_reject() -> None:
    a = _assess(**STACKED)
    assert a.score >= REJECT_MIN
    assert a.level == RiskLevel.HIGH
    assert a.recommended_action == RecommendedAction.REJECT


def test_medium_band_recommends_review() -> None:
    a = _assess(po_match_verdict="mismatch")
    assert RISK_LEVEL_MEDIUM_MIN <= a.score < RISK_LEVEL_HIGH_MIN
    assert a.level == RiskLevel.MEDIUM
    assert a.recommended_action == RecommendedAction.REVIEW


def test_notes_include_mvp_disclaimer() -> None:
    a = _assess()
    assert CALIBRATION_NOTE in a.notes
    assert a.notes[0] == CALIBRATION_NOTE


def test_assessment_is_deterministic() -> None:
    invoice_id = uuid.uuid4()
    inputs = _inputs(**STACKED)
    dumps = [assess_risk(inputs, invoice_id).model_dump() for _ in range(3)]
    assert dumps[0] == dumps[1] == dumps[2]


def test_evidence_with_decimal_serializes_cleanly() -> None:
    # Money-typed evidence must survive a JSON round-trip as a string, with
    # precision intact -- never as a lossy float.
    c = contributor_missing_fields(["total_amount"])
    assert c is not None
    # Re-validate so the field_validator coerces Decimal -> str.
    from app.services.risk.types import RiskContributor

    c = RiskContributor.model_validate(
        {**c.model_dump(), "evidence": {"reference_amount": Decimal("1234.56")}}
    )
    payload = json.loads(c.model_dump_json())
    assert payload["evidence"]["reference_amount"] == "1234.56"


def test_no_llm_or_ml_in_engine() -> None:
    src = inspect.getsource(engine_module)
    for forbidden in (
        "sklearn",
        "scikit",
        "openai",
        "anthropic",
        "transformers",
        "LLMProvider",
        "torch",
        "tensorflow",
    ):
        assert forbidden not in src, f"forbidden reference found: {forbidden}"


def test_rule_ids_are_unique_across_contributors() -> None:
    a = _assess(**STACKED)
    ids = [c.rule_id for c in a.contributors]
    assert len(ids) == len(set(ids))
    assert len(ids) == 7