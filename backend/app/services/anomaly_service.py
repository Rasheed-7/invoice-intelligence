"""Persist statistical anomaly detection as an AnalysisResult row.

Runs the pure detector in ``app.services.anomaly_detection`` against an invoice
and its same-vendor history, writes one :class:`AnalysisResult`, one audit
event, and (when the invoice is not in a terminal state) a status transition.

This module owns persistence for anomaly checking only: it never deletes other
analysis types (calculation_check, po_match, duplicate_check, risk_score),
which are written by their own services.

Status policy: only a STRONG_ANOMALY routes an invoice to NEEDS_REVIEW.
MILD_ANOMALY is a warning, not a red flag - a moderate deviation should reach a
human without blocking the pipeline.

``score`` is stored exactly as the detector reports it, which may be None. An
isolated invoice, a frequency burst with no z-score, and any Isolation Forest
verdict all legitimately produce no score, and the column stays null rather than
being backfilled with a manufactured 0.0.
"""

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

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
from app.services.anomaly_detection.detector import detect_anomaly
from app.services.anomaly_detection.types import AnomalyReport, AnomalyVerdict
from app.services.invoices import get_invoice

__all__ = ["run_anomaly_check", "get_anomaly_result"]

#: Upper bound on prior invoices used to build the baseline. Isolation Forest
#: fits 100 trees over this matrix, so the cap keeps worst-case cost bounded
#: while sitting well above the 20-invoice activation threshold.
MAX_ANOMALY_HISTORY = 500

#: AnomalyVerdict -> AnalysisStatus. Explicit map so a future verdict fails
#: loudly here instead of being silently dropped.
_STATUS_BY_VERDICT = {
    AnomalyVerdict.STRONG_ANOMALY: AnalysisStatus.FAILED,
    AnomalyVerdict.MILD_ANOMALY: AnalysisStatus.WARNING,
    AnomalyVerdict.NORMAL: AnalysisStatus.PASSED,
    AnomalyVerdict.INSUFFICIENT_HISTORY: AnalysisStatus.SKIPPED,
}

#: Terminal statuses: a human or upstream process already made a decision.
_TERMINAL_STATUSES = frozenset(
    {InvoiceStatus.APPROVED, InvoiceStatus.REJECTED, InvoiceStatus.FAILED}
)


def _prior_invoices(db: Session, invoice: Invoice) -> list[Invoice]:
    """Same-vendor invoices other than this one, most recent first, bounded.

    Without a vendor there is no baseline to compare against, so the detector
    reports INSUFFICIENT_HISTORY rather than comparing against every invoice.
    """
    if invoice.vendor_name is None:
        return []

    stmt = (
        select(Invoice)
        .options(selectinload(Invoice.line_items))
        .where(
            Invoice.id != invoice.id,
            Invoice.vendor_name == invoice.vendor_name,
            # Baselines are built from invoices that still exist; a
            # soft-deleted invoice must not skew a vendor's history.
            Invoice.deleted_at.is_(None),
        )
        # Most recent first: a vendor's current behaviour is best described by
        # recent history, and the bound then truncates the oldest tail.
        .order_by(Invoice.created_at.desc())
        .limit(MAX_ANOMALY_HISTORY)
    )
    return list(db.scalars(stmt).all())


def _message(report: AnomalyReport) -> str:
    """One sentence for the audit log."""
    # Say "no score" rather than omitting the clause: a reader seeing
    # "strong_anomaly" with no score needs to know it was deliberate.
    scored = " (no score)" if report.score is None else f" (score {report.score})"
    return f"Anomaly verdict: {report.verdict.value} via {report.method.value}{scored}"


def run_anomaly_check(db: Session, *, invoice_id: UUID) -> Invoice:
    """Run anomaly detection and persist the result.

    Replaces this invoice's prior ANOMALY rows rather than appending. Commits
    at the end.

    Args:
        db: Database session (this function owns the transaction).
        invoice_id: UUID of the invoice to analyse.

    Returns:
        The updated Invoice instance (refreshed after commit).

    Raises:
        LookupError: Invoice not found.
    """
    invoice = get_invoice(db, invoice_id)
    if invoice is None:
        raise LookupError(f"invoice not found: {invoice_id}")

    prior_invoices = _prior_invoices(db, invoice)
    report = detect_anomaly(invoice, prior_invoices)

    # Re-running must replace prior anomaly results, not accumulate them. Do NOT
    # delete other analysis types - each has its own service.
    db.execute(
        delete(AnalysisResult).where(
            AnalysisResult.invoice_id == invoice_id,
            AnalysisResult.analysis_type == AnalysisType.ANOMALY,
        )
    )
    # Flush so the DELETE is issued before the INSERT below.
    db.flush()

    db.add(
        AnalysisResult(
            invoice_id=invoice_id,
            analysis_type=AnalysisType.ANOMALY,
            status=_STATUS_BY_VERDICT[report.verdict],
            # May legitimately be None - see the module docstring.
            score=report.score,
            details=report.model_dump(mode="json"),
        )
    )

    # No dedicated anomaly event type yet; NOTE with structured data keeps it
    # queryable.
    db.add(
        AuditEvent(
            invoice_id=invoice.id,
            event_type=AuditEventType.NOTE,
            actor=AuditActor.SYSTEM,
            message=_message(report),
            data={
                "analysis": "anomaly",
                "verdict": report.verdict.value,
                "method": report.method.value,
                "history_size": report.history_size,
                "contribution_count": len(report.contributions),
            },
        )
    )

    # --- status transition ---------------------------------------------------
    if (
        report.verdict == AnomalyVerdict.STRONG_ANOMALY
        and invoice.status not in _TERMINAL_STATUSES
    ):
        invoice.status = InvoiceStatus.NEEDS_REVIEW
    # MILD_ANOMALY deliberately leaves the status alone: a moderate deviation
    # is a warning, not a red flag. NORMAL, INSUFFICIENT_HISTORY and terminal
    # statuses likewise leave it unchanged.

    db.commit()
    db.refresh(invoice)

    return invoice


def get_anomaly_result(db: Session, invoice_id: UUID) -> AnalysisResult | None:
    """Return the latest ANOMALY AnalysisResult for the invoice, or None."""
    stmt = (
        select(AnalysisResult)
        .where(
            AnalysisResult.invoice_id == invoice_id,
            AnalysisResult.analysis_type == AnalysisType.ANOMALY,
        )
        .order_by(AnalysisResult.created_at.desc(), AnalysisResult.id.desc())
        .limit(1)
    )
    return db.scalars(stmt).first()