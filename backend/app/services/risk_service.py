"""Persist rule-based risk scoring as an AnalysisResult row.

Three-stage split, and the separation is the point:

1. ``build_analysis_inputs`` (this file) reads prior AnalysisResult rows and
   folds them into a single ``AnalysisInputs`` snapshot.
2. ``app.services.risk.engine`` - pure, deterministic, no I/O - turns that
   snapshot into a ``RiskAssessment``. It never touches the database.
3. ``run_risk_assessment`` (this file) writes the assessment back as a
   RISK_SCORE AnalysisResult, an audit event, and a status transition.

This file therefore contains no scoring logic whatsoever: no weights, no
thresholds, no verdicts of its own. Every point in the score comes from the
engine reading a verdict some earlier stage already produced. That keeps the
decision auditable - if a score looks wrong, the contributor that caused it
names the stage that caused it.

A stage that never ran is not a finding. Absence of evidence is not evidence of
absence, so a missing row becomes ``None`` and contributes nothing rather than
being treated as a failure.

``score`` is stored as a fraction (0.0000-1.0000) to match the ``Numeric(5, 4)``
column; the API and frontend present it as 0-100.
"""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models.analysis import AnalysisResult
from app.models.audit import AuditEvent
from app.models.enums import (
    AnalysisStatus,
    AnalysisType,
    AuditActor,
    AuditEventType,
    InvoiceStatus,
)
from app.models.invoice import Invoice
from app.services.invoices import get_invoice
from app.services.review_service import create_review_case
from app.services.risk.engine import AnalysisInputs, RiskAssessment, assess_risk
from app.services.risk.types import RecommendedAction, RiskLevel
from app.services.risk.weights import REQUIRED_FIELDS

__all__ = [
    "build_analysis_inputs",
    "get_risk_assessment",
    "run_risk_assessment",
]

#: Terminal statuses: a human or upstream process already made a decision.
#: Re-scoring must never downgrade these.
_TERMINAL_STATUSES = frozenset(
    {InvoiceStatus.APPROVED, InvoiceStatus.REJECTED, InvoiceStatus.FAILED}
)

#: RiskLevel -> AnalysisStatus. Explicit map so a new level fails loudly here
#: rather than being silently dropped.
_STATUS_BY_LEVEL = {
    RiskLevel.LOW: AnalysisStatus.PASSED,
    RiskLevel.MEDIUM: AnalysisStatus.WARNING,
    RiskLevel.HIGH: AnalysisStatus.FAILED,
}

#: POLICY_CHECK rule ids written by app.services.policy_analysis. Imported by
#: value rather than from that module to keep this file's dependency surface
#: narrow; the literals are part of the persisted contract.
_RULE_POLICY_EVIDENCE_FOUND = "POLICY_EVIDENCE_FOUND"
_RULE_NO_APPLICABLE_POLICY = "NO_APPLICABLE_POLICY"

#: Ceiling on the audit message, matching AuditEvent.message's String(1024).
#: The contributor list is the only unbounded part of the message, so it is
#: truncated rather than letting the INSERT fail on a pathological input.
_AUDIT_MESSAGE_MAX = 1024


def _latest_row(
    db: Session,
    invoice_id: UUID,
    analysis_type: AnalysisType,
) -> AnalysisResult | None:
    """Return the most recent AnalysisResult of one type, or None.

    Ordered by created_at DESC with an id tiebreaker, matching the other
    services' getters: rows written in the same clock tick are otherwise
    ordered arbitrarily.
    """
    stmt = (
        select(AnalysisResult)
        .where(
            AnalysisResult.invoice_id == invoice_id,
            AnalysisResult.analysis_type == analysis_type,
        )
        .order_by(AnalysisResult.created_at.desc(), AnalysisResult.id.desc())
        .limit(1)
    )
    return db.scalars(stmt).first()


def _detail(row: AnalysisResult | None, key: str) -> object:
    """Read one key out of a row's JSONB details, or None.

    ``details`` is nullable JSONB and its shape is owned by whichever service
    wrote the row, so every read goes through here rather than indexing
    directly: a missing row, a null details, or a missing key all mean "this
    stage told us nothing".
    """
    if row is None or not row.details:
        return None
    return row.details.get(key)


def _str_detail(row: AnalysisResult | None, key: str) -> str | None:
    """Read a string detail, ignoring values of any other type."""
    value = _detail(row, key)
    return value if isinstance(value, str) else None


def _validation_counts(db: Session, invoice_id: UUID) -> tuple[int, int]:
    """Return (failed_count, warning_count) across CALCULATION_CHECK rows.

    One row exists per check, so this is a straight status tally rather than a
    latest-row read. A check whose status is SKIPPED counts as neither.
    """
    stmt = (
        select(AnalysisResult.status, func.count())
        .where(
            AnalysisResult.invoice_id == invoice_id,
            AnalysisResult.analysis_type == AnalysisType.CALCULATION_CHECK,
        )
        .group_by(AnalysisResult.status)
    )
    counts = dict(db.execute(stmt).all())
    return (
        int(counts.get(AnalysisStatus.FAILED, 0)),
        int(counts.get(AnalysisStatus.WARNING, 0)),
    )


def _policy_signal(
    row: AnalysisResult | None,
) -> tuple[bool | None, int]:
    """Return (evidence_found, evidence_count) from the latest POLICY_CHECK.

    Tri-state on the first element because the three cases mean different
    things to the engine: True is "a policy applies, someone should look", False
    is "the check ran and found nothing to cite", and None is "the check never
    ran". Collapsing None into False would manufacture a finding the platform
    has no evidence for.
    """
    if row is None:
        return None, 0

    rule_id = _str_detail(row, "rule_id")
    if rule_id == _RULE_POLICY_EVIDENCE_FOUND:
        evidence = _detail(row, "evidence")
        return True, len(evidence) if isinstance(evidence, list) else 0
    if rule_id == _RULE_NO_APPLICABLE_POLICY:
        return False, 0
    # Unrecognised rule_id: treat as "ran, told us nothing actionable" rather
    # than asserting a verdict we cannot justify.
    return False, 0


def _extraction_confidence(invoice: Invoice) -> dict[str, float]:
    """Coerce the invoice's extraction confidence map to floats.

    Non-numeric entries are dropped rather than coerced to 0.0: an unreadable
    confidence value is missing data, not a confidence of zero, and inventing
    the latter would inflate the risk score on a parse quirk.
    """
    raw = invoice.extraction_confidence
    if not isinstance(raw, dict):
        return {}

    confidence: dict[str, float] = {}
    for field, value in raw.items():
        if isinstance(value, bool):
            # bool is an int subclass; True must not become 1.0 confidence.
            continue
        if isinstance(value, (int, float)):
            confidence[field] = float(value)
    return confidence


def _missing_required_fields(invoice: Invoice) -> list[str]:
    """Required fields that are absent on the invoice, in REQUIRED_FIELDS order.

    Only genuine absences count. invoice_date and total_amount are the two that
    can realistically be missing after extraction.
    """
    return [name for name in REQUIRED_FIELDS if getattr(invoice, name, None) is None]


def build_analysis_inputs(db: Session, invoice_id: UUID) -> AnalysisInputs:
    """Fold prior analysis rows into an AnalysisInputs snapshot.

    Reads, never writes. Every field takes the highest-severity (most recent)
    outcome for its stage, or None where that stage never ran.

    Args:
        db: Database session (borrowed, not committed).
        invoice_id: UUID of the invoice to summarize.

    Returns:
        AnalysisInputs ready to hand to ``assess_risk``.

    Raises:
        LookupError: Invoice not found.
    """
    invoice = get_invoice(db, invoice_id)
    if invoice is None:
        raise LookupError(f"invoice not found: {invoice_id}")

    failed_count, warning_count = _validation_counts(db, invoice_id)
    policy_found, policy_count = _policy_signal(
        _latest_row(db, invoice_id, AnalysisType.POLICY_CHECK)
    )

    return AnalysisInputs(
        duplicate_verdict=_str_detail(
            _latest_row(db, invoice_id, AnalysisType.DUPLICATE_CHECK), "verdict"
        ),
        po_match_verdict=_str_detail(
            _latest_row(db, invoice_id, AnalysisType.PO_MATCH), "overall_outcome"
        ),
        validation_failed_count=failed_count,
        validation_warning_count=warning_count,
        anomaly_verdict=_str_detail(
            _latest_row(db, invoice_id, AnalysisType.ANOMALY), "verdict"
        ),
        policy_evidence_found=policy_found,
        policy_evidence_count=policy_count,
        extraction_confidence=_extraction_confidence(invoice),
        missing_required_fields=_missing_required_fields(invoice),
    )


def _audit_message(assessment: RiskAssessment) -> str:
    """One sentence for the audit timeline, truncated to the column width.

    Lists the rule ids rather than the messages: the ids are short and stable,
    and the per-finding detail already lives in the RISK_SCORE row's details.
    """
    rule_ids = ", ".join(c.rule_id for c in assessment.contributors) or "none"
    message = (
        f"Risk score {assessment.score} ({assessment.level.value}); "
        f"recommended: {assessment.recommended_action.value}. "
        f"Contributors: {rule_ids}"
    )
    if len(message) <= _AUDIT_MESSAGE_MAX:
        return message
    # Truncate the contributor list, not the score/level/action prefix: those
    # are the parts a reader scanning the timeline actually needs.
    return message[: _AUDIT_MESSAGE_MAX - 3] + "..."


def _apply_status(invoice: Invoice, assessment: RiskAssessment) -> None:
    """Move the invoice status per the assessment, respecting terminal states."""
    if invoice.status in _TERMINAL_STATUSES:
        # A human or upstream process already decided. Re-scoring records the
        # new number but must not reopen a closed decision.
        return

    if assessment.recommended_action == RecommendedAction.REJECT:
        # The risk engine's REJECT recommendation is the only analysis stage
        # that can set terminal REJECTED status. A reviewer can still override
        # it, but nothing downstream of this service will second-guess it.
        invoice.status = InvoiceStatus.REJECTED
    elif assessment.level == RiskLevel.HIGH:
        # High risk without a REJECT recommendation: route to a human rather
        # than closing the invoice out.
        invoice.status = InvoiceStatus.NEEDS_REVIEW
    # LOW and MEDIUM levels, and REVIEW/AUTO_APPROVE actions, leave the status
    # alone: scoring is advisory and must not stall a healthy pipeline.


def run_risk_assessment(db: Session, *, invoice_id: UUID) -> Invoice:
    """Score the invoice's prior analyses and persist the result.

    Replaces this invoice's prior RISK_SCORE rows rather than appending, so
    repeated calls converge on one current result. Commits at the end.

    Args:
        db: Database session (this function owns the transaction).
        invoice_id: UUID of the invoice to score.

    Returns:
        The updated Invoice instance (refreshed after commit).

    Raises:
        LookupError: Invoice not found.
    """
    invoice = get_invoice(db, invoice_id)
    if invoice is None:
        raise LookupError(f"invoice not found: {invoice_id}")

    inputs = build_analysis_inputs(db, invoice_id)
    assessment = assess_risk(inputs, invoice_id)

    # Re-running must replace prior risk_score results, not accumulate them.
    # Do NOT delete other analysis types - each has its own service.
    db.execute(
        delete(AnalysisResult).where(
            AnalysisResult.invoice_id == invoice_id,
            AnalysisResult.analysis_type == AnalysisType.RISK_SCORE,
        )
    )
    # Flush so the DELETE is issued before the INSERT below.
    db.flush()

    db.add(
        AnalysisResult(
            invoice_id=invoice_id,
            analysis_type=AnalysisType.RISK_SCORE,
            status=_STATUS_BY_LEVEL[assessment.level],
            # The API and frontend present score as 0-100; the database stores
            # it as a normalized fraction to match the column type and to keep
            # a single canonical representation.
            score=Decimal(assessment.score) / Decimal(100),
            # The full assessment, contributors included: this row is the
            # explanation of the score, so it must be self-contained.
            details=assessment.model_dump(mode="json"),
        )
    )

    db.add(
        AuditEvent(
            invoice_id=invoice.id,
            event_type=AuditEventType.RISK_SCORED,
            actor=AuditActor.SYSTEM,
            message=_audit_message(assessment),
            data={
                "analysis": "risk_score",
                "score": assessment.score,
                "level": assessment.level.value,
                "recommended_action": assessment.recommended_action.value,
                "contributor_count": len(assessment.contributors),
                "contributing_total": assessment.contributing_total,
                "caps_applied": assessment.caps_applied,
            },
        )
    )

    _apply_status(invoice, assessment)

    # The risk engine owns the review-request decision. HIGH or REJECT creates
    # the case here, in the same transaction as the score, so the audit trail is
    # atomic. create_review_case is idempotent - re-running /assess-risk will not
    # duplicate the case.
    if assessment.level == RiskLevel.HIGH or (
        assessment.recommended_action == RecommendedAction.REJECT
    ):
        create_review_case(
            db,
            invoice_id=invoice_id,
            reason=(
                f"Risk score {assessment.score} ({assessment.level.value}); "
                f"recommended: {assessment.recommended_action.value}"
            ),
        )

    db.commit()
    db.refresh(invoice)

    return invoice


def get_risk_assessment(db: Session, invoice_id: UUID) -> AnalysisResult | None:
    """Return the latest RISK_SCORE AnalysisResult for the invoice, or None.

    Ordered by created_at DESC with an id tiebreaker; normally only one row
    exists because run_risk_assessment replaces prior results.
    """
    return _latest_row(db, invoice_id, AnalysisType.RISK_SCORE)