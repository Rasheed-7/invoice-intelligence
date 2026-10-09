"""Rule-based MVP risk engine: combine per-stage findings into one score.

This engine is entirely rule-based. There is no LLM call, no ML model, and
no learned parameter anywhere in this module: every point value is a constant
from :mod:`app.services.risk.weights` combined by integer arithmetic and
comparisons. The same :class:`AnalysisInputs` always produces a byte-identical
:class:`~app.services.risk.types.RiskAssessment`, which is what makes the
output explainable and auditable.

Everything here is a pure function over in-memory inputs. No SQLAlchemy, no
FastAPI, no ``AnalysisResult`` reads, no persistence -- the caller gathers the
highest-severity outcome per stage into an :class:`AnalysisInputs` snapshot and
passes it in.

Design: each finding category contributes a bounded number of points (its
per-contributor cap); the contributions are summed; the total is capped at
``MAX_SCORE`` so the output stays on a fixed 0..100 scale regardless of how
many findings fire. Caps keep any single severe finding from dominating the
score, and the uncapped sum is preserved as ``contributing_total`` so capping
stays auditable.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.services.risk.types import (
    RecommendedAction,
    RiskAssessment,
    RiskContributor,
    RiskLevel,
)
from app.services.risk.weights import (
    AUTO_APPROVE_MAX,
    CAP_DUPLICATE,
    CAP_LOW_EXTRACTION_CONFIDENCE,
    CAP_MISSING_REQUIRED_FIELD,
    CAP_PO_MISMATCH,
    CAP_POLICY_UNCERTAINTY,
    CAP_STRONG_ANOMALY,
    CAP_VALIDATION_FAILURE,
    LOW_CONFIDENCE_THRESHOLD,
    MAX_SCORE,
    REJECT_MIN,
    RISK_LEVEL_HIGH_MIN,
    RISK_LEVEL_MEDIUM_MIN,
)

__all__ = [
    "AnalysisInputs",
    "CALIBRATION_NOTE",
    "assess_risk",
    "contributor_anomaly",
    "contributor_duplicate",
    "contributor_extraction_confidence",
    "contributor_missing_fields",
    "contributor_po_mismatch",
    "contributor_policy",
    "contributor_validation",
]

#: Disclaimer attached to every assessment. The model must never be mistaken
#: for a calibrated scorer.
CALIBRATION_NOTE = (
    "Explainable MVP risk model. "
    "Weights are engineering judgment, not empirically calibrated."
)

#: Rule ids. Stable strings, so downstream consumers can branch on them.
RULE_DUPLICATE = "duplicate_finding"
RULE_PO_MATCH = "po_match_finding"
RULE_VALIDATION = "validation_finding"
RULE_ANOMALY = "anomaly_finding"
RULE_POLICY = "policy_finding"
RULE_LOW_CONFIDENCE = "low_extraction_confidence"
RULE_MISSING_FIELD = "missing_required_field"


class AnalysisInputs(BaseModel):
    """Snapshot of everything the risk engine needs from prior stages.

    Each field is the highest-severity outcome observed, or None if that
    stage never ran for this invoice.
    """

    model_config = ConfigDict(extra="forbid")

    duplicate_verdict: (
        Literal["unique", "suspected_duplicate", "duplicate"] | None
    ) = None
    po_match_verdict: (
        Literal["match", "partial_match", "mismatch", "skipped"] | None
    ) = None
    validation_failed_count: int = Field(default=0, ge=0)
    validation_warning_count: int = Field(default=0, ge=0)
    anomaly_verdict: (
        Literal["normal", "mild_anomaly", "strong_anomaly", "insufficient_history"]
        | None
    ) = None
    #: None = policy check not run.
    policy_evidence_found: bool | None = None
    policy_evidence_count: int = Field(default=0, ge=0)
    #: Field path -> confidence.
    extraction_confidence: dict[str, float] = Field(default_factory=dict)
    #: Subset of REQUIRED_FIELDS.
    missing_required_fields: list[str] = Field(default_factory=list)


def contributor_duplicate(verdict: str | None) -> RiskContributor | None:
    """Confirmed duplicates take the full cap; suspected takes half."""
    if verdict == "duplicate":
        return RiskContributor(
            rule_id=RULE_DUPLICATE,
            category="duplicate",
            points=CAP_DUPLICATE,
            cap=CAP_DUPLICATE,
            severity="error",
            message="Confirmed duplicate of an existing invoice.",
            evidence={"verdict": verdict},
        )
    if verdict == "suspected_duplicate":
        # Half the cap (integer division, rounded down): corroborating signals
        # deserve weight without matching a confirmed re-submission of the
        # same economic claim.
        return RiskContributor(
            rule_id=RULE_DUPLICATE,
            category="duplicate",
            points=CAP_DUPLICATE // 2,
            cap=CAP_DUPLICATE,
            severity="warn",
            message="Possible duplicate — corroborating signals found.",
            evidence={"verdict": verdict},
        )
    # "unique" and "stage never ran" are both non-findings.
    return None


def contributor_po_mismatch(verdict: str | None) -> RiskContributor | None:
    """A mismatch takes the full cap; a partial match takes half."""
    if verdict == "mismatch":
        return RiskContributor(
            rule_id=RULE_PO_MATCH,
            category="po_match",
            points=CAP_PO_MISMATCH,
            cap=CAP_PO_MISMATCH,
            severity="error",
            message="Purchase order mismatch — invoice contradicts the PO.",
            evidence={"verdict": verdict},
        )
    if verdict == "partial_match":
        return RiskContributor(
            rule_id=RULE_PO_MATCH,
            category="po_match",
            points=CAP_PO_MISMATCH // 2,
            cap=CAP_PO_MISMATCH,
            severity="warn",
            message="Partial PO match — some line items disagree.",
            evidence={"verdict": verdict},
        )
    # "match", "skipped" (no PO to match against) and "never ran" are
    # non-findings.
    return None


def contributor_validation(
    failed_count: int, warning_count: int
) -> RiskContributor | None:
    """Failed checks scale 15 + 5 each, capped; warnings scale 8 + 2 each.

    Failures take precedence: a proven arithmetic error outranks warnings, so
    only one contributor is emitted per invoice.
    """
    if failed_count > 0:
        points = min(CAP_VALIDATION_FAILURE, 15 + 5 * (failed_count - 1))
        return RiskContributor(
            rule_id=RULE_VALIDATION,
            category="validation",
            points=points,
            cap=CAP_VALIDATION_FAILURE,
            severity="error",
            message=f"{failed_count} failed validation check(s).",
            evidence={"failed_count": failed_count},
        )
    if warning_count > 0:
        points = min(CAP_VALIDATION_FAILURE // 2, 8 + 2 * (warning_count - 1))
        return RiskContributor(
            rule_id=RULE_VALIDATION,
            category="validation",
            points=points,
            cap=CAP_VALIDATION_FAILURE,
            severity="warn",
            message=f"{warning_count} validation warning(s).",
            evidence={"warning_count": warning_count},
        )
    return None


def contributor_anomaly(verdict: str | None) -> RiskContributor | None:
    """Strong anomalies take the full cap; mild takes half.

    "insufficient_history" contributes nothing: the detector had too little
    data to say anything. Insufficient history is not evidence of risk.
    Absence of evidence is not evidence of absence.
    """
    if verdict == "strong_anomaly":
        return RiskContributor(
            rule_id=RULE_ANOMALY,
            category="anomaly",
            points=CAP_STRONG_ANOMALY,
            cap=CAP_STRONG_ANOMALY,
            severity="error",
            message="Strong anomaly detected against vendor history.",
            evidence={"verdict": verdict},
        )
    if verdict == "mild_anomaly":
        return RiskContributor(
            rule_id=RULE_ANOMALY,
            category="anomaly",
            points=CAP_STRONG_ANOMALY // 2,
            cap=CAP_STRONG_ANOMALY,
            severity="warn",
            message="Mild anomaly detected against vendor history.",
            evidence={"verdict": verdict},
        )
    # "normal", "insufficient_history" and "never ran" are non-findings.
    return None


def contributor_policy(
    policy_evidence_found: bool | None, evidence_count: int
) -> RiskContributor | None:
    """Policy evidence is a soft signal; absence of evidence is stronger.

    The presence of policy evidence is a soft signal — the invoice matched a
    policy, which means someone should look at it. The absence of evidence when
    a check ran is a stronger signal because the platform cannot justify a
    policy-based decision. Neither is a failure by itself.

    ``None`` means the policy check never ran, which is not the same as the
    check running and finding nothing, so it contributes nothing.
    """
    if policy_evidence_found is None:
        return None
    if policy_evidence_found and evidence_count > 0:
        return RiskContributor(
            rule_id=RULE_POLICY,
            category="policy",
            points=CAP_POLICY_UNCERTAINTY // 2,
            cap=CAP_POLICY_UNCERTAINTY,
            severity="info",
            message=(
                "Policy evidence retrieved for review; "
                "a human may need to confirm applicability."
            ),
            evidence={
                "policy_evidence_found": True,
                "evidence_count": evidence_count,
            },
        )
    return RiskContributor(
        rule_id=RULE_POLICY,
        category="policy",
        points=CAP_POLICY_UNCERTAINTY,
        cap=CAP_POLICY_UNCERTAINTY,
        severity="warn",
        message="No applicable policy evidence found.",
        evidence={
            "policy_evidence_found": False,
            "evidence_count": evidence_count,
        },
    )


def contributor_extraction_confidence(
    confidence_map: dict[str, float],
) -> RiskContributor | None:
    """Fields extracted below the low-confidence threshold contribute.

    The evidence payload carries the low-confidence fraction so a reviewer can
    see *how much* of the extraction was doubtful, not just that some of it was.
    """
    low_fields = {
        field: value
        for field, value in sorted(confidence_map.items())
        if value < LOW_CONFIDENCE_THRESHOLD
    }
    if not low_fields:
        return None
    low_count = len(low_fields)
    points = min(CAP_LOW_EXTRACTION_CONFIDENCE, 5 + 3 * low_count)
    return RiskContributor(
        rule_id=RULE_LOW_CONFIDENCE,
        category="extraction",
        points=points,
        cap=CAP_LOW_EXTRACTION_CONFIDENCE,
        severity="warn",
        message=f"{low_count} low-confidence extraction field(s).",
        evidence={
            "threshold": LOW_CONFIDENCE_THRESHOLD,
            "low_confidence_count": low_count,
            "low_confidence_fraction": low_count / len(confidence_map),
            "low_confidence_fields": low_fields,
        },
    )


def contributor_missing_fields(missing: list[str]) -> RiskContributor | None:
    """Each missing required field adds 5 points, up to the category cap."""
    if not missing:
        return None
    points = min(CAP_MISSING_REQUIRED_FIELD, 5 * len(missing))
    return RiskContributor(
        rule_id=RULE_MISSING_FIELD,
        category="completeness",
        points=points,
        cap=CAP_MISSING_REQUIRED_FIELD,
        severity="warn",
        message=f"Missing required field(s): {', '.join(missing)}.",
        evidence={"missing_fields": missing},
    )


def _collect_contributors(inputs: AnalysisInputs) -> list[RiskContributor]:
    """Run every category builder and drop the ones that found nothing.

    Category order is fixed rather than derived from a mapping, so the
    contributor list is stable across runs and diffs read predictably.
    """
    candidates: list[RiskContributor | None] = [
        contributor_duplicate(inputs.duplicate_verdict),
        contributor_po_mismatch(inputs.po_match_verdict),
        contributor_validation(
            inputs.validation_failed_count, inputs.validation_warning_count
        ),
        contributor_anomaly(inputs.anomaly_verdict),
        contributor_policy(
            inputs.policy_evidence_found, inputs.policy_evidence_count
        ),
        contributor_extraction_confidence(inputs.extraction_confidence),
        contributor_missing_fields(inputs.missing_required_fields),
    ]
    return [c for c in candidates if c is not None]


def assess_risk(inputs: AnalysisInputs, invoice_id: UUID) -> RiskAssessment:
    """Combine per-stage findings into one capped, explainable score."""
    contributors = _collect_contributors(inputs)

    contributing_total = sum(c.points for c in contributors)
    caps_applied = contributing_total > MAX_SCORE
    score = min(contributing_total, MAX_SCORE)

    if score >= RISK_LEVEL_HIGH_MIN:
        level = RiskLevel.HIGH
    elif score >= RISK_LEVEL_MEDIUM_MIN:
        level = RiskLevel.MEDIUM
    else:
        level = RiskLevel.LOW

    if score >= REJECT_MIN:
        action = RecommendedAction.REJECT
    elif score <= AUTO_APPROVE_MAX:
        action = RecommendedAction.AUTO_APPROVE
    else:
        action = RecommendedAction.REVIEW

    # Thresholds are ordered so these overrides are dead code today; the guard
    # exists so a future weight change can't silently produce contradictory
    # output (e.g. auto-approving something we just called high risk).
    if level == RiskLevel.HIGH and action == RecommendedAction.AUTO_APPROVE:
        action = RecommendedAction.REVIEW
    if level == RiskLevel.LOW and action == RecommendedAction.REJECT:
        action = RecommendedAction.REVIEW

    notes = [CALIBRATION_NOTE]
    if caps_applied:
        notes.append(
            f"Global cap of {MAX_SCORE} applied; raw total was {contributing_total}."
        )

    return RiskAssessment(
        invoice_id=invoice_id,
        score=score,
        level=level,
        recommended_action=action,
        contributors=contributors,
        contributing_total=contributing_total,
        caps_applied=caps_applied,
        notes=notes,
    )