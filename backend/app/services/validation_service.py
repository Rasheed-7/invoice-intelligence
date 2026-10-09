"""Persist deterministic validation checks as AnalysisResult rows.

Runs the pure checks in ``app.services.validation`` against an invoice and
writes one :class:`AnalysisResult` per check, one audit event, and (when the
invoice is not in a terminal state) a status transition.

This module owns persistence for validation only: it never deletes other
analysis types (po_match, duplicate_check, anomaly, risk_score), which are
written by their own services.
"""

from uuid import UUID

from sqlalchemy import delete, select
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
from app.services.validation.runner import run_all_checks
from app.services.validation.types import ValidationOutcome

__all__ = ["ValidationFailed", "run_validation", "get_validation_results"]


class ValidationFailed(Exception):
    """Reserved for a genuinely unusable invoice state.

    Note that a blank invoice (no line items, no monetary fields) is *not*
    such a state: every check returns "skipped", the report outcome is
    "skipped", and the invoice keeps its prior status. The route maps this
    exception to 422 should a future check need it.
    """


# ValidationOutcome -> AnalysisStatus. The two enums share the same string
# values ("passed"/"failed"/"warning"/"skipped"); the explicit map keeps the
# coupling visible and fails loudly if either side ever changes.
_STATUS_BY_OUTCOME = {
    ValidationOutcome.PASSED: AnalysisStatus.PASSED,
    ValidationOutcome.FAILED: AnalysisStatus.FAILED,
    ValidationOutcome.WARNING: AnalysisStatus.WARNING,
    ValidationOutcome.SKIPPED: AnalysisStatus.SKIPPED,
}

# Terminal statuses: a human or upstream process already made a decision.
# Re-running validation must never downgrade these back to VALIDATED or
# NEEDS_REVIEW.
_TERMINAL_STATUSES = frozenset(
    {InvoiceStatus.APPROVED, InvoiceStatus.REJECTED, InvoiceStatus.FAILED}
)


def run_validation(db: Session, *, invoice_id: UUID) -> Invoice:
    """Run all validation checks and persist the results.

    Loads the invoice, runs every check, replaces this invoice's prior
    calculation_check results, writes one AnalysisResult per check, records an
    audit event, and applies the status transition. Commits at the end.

    Args:
        db: Database session (this function owns the transaction).
        invoice_id: UUID of the invoice to validate.

    Returns:
        The updated Invoice instance (refreshed after commit).

    Raises:
        LookupError: Invoice not found.
    """
    invoice = get_invoice(db, invoice_id)
    if invoice is None:
        raise LookupError(f"invoice not found: {invoice_id}")

    # A blank invoice (no line items, no monetary fields) is valid input:
    # every check returns "skipped", the report outcome is "skipped", and the
    # status transition below deliberately leaves the invoice where it is.
    report = run_all_checks(invoice, db)

    # Re-validation must replace prior calculation_check results, not
    # accumulate them. Do NOT delete other analysis types (po_match,
    # duplicate_check, anomaly, risk_score) - those are handled by their own
    # services.
    db.execute(
        delete(AnalysisResult).where(
            AnalysisResult.invoice_id == invoice_id,
            AnalysisResult.analysis_type == AnalysisType.CALCULATION_CHECK,
        )
    )
    # Flush so the DELETE is issued before the INSERTs below; otherwise the
    # new rows could conflict with existing rows on the
    # (invoice_id, analysis_type) index.
    db.flush()

    for result in report.checks:
        db.add(
            AnalysisResult(
                invoice_id=invoice_id,
                analysis_type=AnalysisType.CALCULATION_CHECK,
                status=_STATUS_BY_OUTCOME[result.outcome],
                # Validation is pass/fail, not scored - no normalized score.
                score=None,
                # Check name and full details flattened into the JSONB payload
                # for direct queryability.
                details={
                    "check_name": result.check_name,
                    "rule_id": result.rule_id,
                    "message": result.message,
                    "severity": result.severity,
                    **result.details,
                },
            )
        )

    # --- status transition ---------------------------------------------------
    if invoice.status in _TERMINAL_STATUSES:
        # Never overwrite a terminal status: APPROVED, REJECTED and FAILED
        # represent a decision someone already made. Re-validating must not
        # downgrade the invoice back to NEEDS_REVIEW or VALIDATED.
        pass
    elif report.overall_outcome == ValidationOutcome.PASSED:
        invoice.status = InvoiceStatus.VALIDATED
    elif report.overall_outcome in (
        ValidationOutcome.FAILED,
        ValidationOutcome.WARNING,
    ):
        invoice.status = InvoiceStatus.NEEDS_REVIEW
    # ValidationOutcome.SKIPPED: leave the status unchanged - nothing
    # meaningful was validated, so the invoice stays in its prior state.

    # --- audit event ---------------------------------------------------------
    summary = report.summary
    passed = report.overall_outcome == ValidationOutcome.PASSED
    # WARNING and SKIPPED are not "passed", so they route through the
    # VALIDATION_FAILED event type as well; only PASSED gets VALIDATION_PASSED.
    event_type = (
        AuditEventType.VALIDATION_PASSED if passed else AuditEventType.VALIDATION_FAILED
    )
    if passed:
        message = (
            f"Validation passed: {summary['passed']} passed, "
            f"{summary['failed']} failed, {summary['warning']} warnings, "
            f"{summary['skipped']} skipped"
        )
    elif summary["failed"] > 0:
        message = f"Validation failed: {summary['failed']} check(s) failed"
    elif summary["warning"] > 0:
        message = f"Validation failed: {summary['warning']} check(s) warned"
    else:
        message = "Validation failed: no checks could run"

    db.add(
        AuditEvent(
            invoice_id=invoice.id,
            event_type=event_type,
            actor=AuditActor.SYSTEM,
            message=message,
            data={
                "overall_outcome": str(report.overall_outcome.value),
                "summary": report.summary,
                "check_outcomes": {
                    c.check_name: c.outcome.value for c in report.checks
                },
            },
        )
    )

    db.commit()
    db.refresh(invoice)

    return invoice


def get_validation_results(db: Session, invoice_id: UUID) -> list[AnalysisResult]:
    """Return this invoice's calculation_check results in insertion order.

    Ordered by created_at ASC so the row order preserves the check order
    from ALL_CHECKS.
    """
    stmt = (
        select(AnalysisResult)
        .where(
            AnalysisResult.invoice_id == invoice_id,
            AnalysisResult.analysis_type == AnalysisType.CALCULATION_CHECK,
        )
        .order_by(AnalysisResult.created_at.asc())
    )
    return list(db.scalars(stmt).all())

