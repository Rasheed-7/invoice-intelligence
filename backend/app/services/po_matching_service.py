"""Persist deterministic PO matching as an AnalysisResult row.

Runs the pure matcher in ``app.services.po_matching`` against an invoice and a
purchase order, writes one :class:`AnalysisResult`, one audit event, and (when
the invoice is not in a terminal state) a status transition.

This module owns persistence for PO matching only: it never deletes other
analysis types (calculation_check, duplicate_check, anomaly, risk_score),
which are written by their own services.

The PO reference is read from ``invoices.po_number``, an indexed column populated
by the extraction stage.

Known limitation: the acme-simple and initech-missing-fields fixtures carry no
PO number, so ``/match-po`` always reports SKIPPED on them. The
globex-multiline fixture does carry one (PO-EU-4411) and is the fixture to use
when exercising the matching path.
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
from app.services.po_matching.matcher import build_match_report
from app.services.po_matching.types import MatchOutcome, PurchaseOrderMatchReport
from app.services.purchase_orders import get_purchase_order_by_number

__all__ = ["run_po_matching", "get_po_match_result"]


# MatchOutcome -> AnalysisStatus. The two enums have different vocabularies
# ("match"/"partial_match"/... vs "passed"/"warning"/...), so the explicit map
# keeps the coupling visible and fails loudly if either side ever changes.
_STATUS_BY_OUTCOME = {
    MatchOutcome.MATCH: AnalysisStatus.PASSED,
    MatchOutcome.PARTIAL_MATCH: AnalysisStatus.WARNING,
    MatchOutcome.MISMATCH: AnalysisStatus.FAILED,
    MatchOutcome.SKIPPED: AnalysisStatus.SKIPPED,
}

# Terminal statuses: a human or upstream process already made a decision.
# Re-running PO matching must never downgrade these - same rule as validation.
_TERMINAL_STATUSES = frozenset(
    {InvoiceStatus.APPROVED, InvoiceStatus.REJECTED, InvoiceStatus.FAILED}
)


def _match_message(report: PurchaseOrderMatchReport) -> str:
    """One sentence summarizing the outcome, e.g.

    ``PO match MISMATCH: quantity mismatch on line 1``
    ``PO match SKIPPED: no PO reference on invoice``
    """
    outcome = report.overall_outcome
    if outcome == MatchOutcome.SKIPPED:
        reason = report.notes[0] if report.notes else "skipped"
    else:
        reason = None
        # Prefer the first line-level disagreement: it is the most specific
        # explanation (matches the "quantity mismatch on line 1" wording).
        for index, line in enumerate(report.line_matches, start=1):
            if line.mismatches:
                fields = ", ".join(sorted({mismatch.field for mismatch in line.mismatches}))
                reason = f"{fields} mismatch on line {index}"
                break
        if reason is None:
            if report.vendor_matches is False:
                reason = "vendor mismatch"
            elif report.total_matches is False:
                reason = "total mismatch"
            elif report.notes:
                reason = "; ".join(report.notes)
            else:
                reason = "all lines matched"
    return f"PO match {str(outcome.value).upper()}: {reason}"


def run_po_matching(db: Session, *, invoice_id: UUID) -> Invoice:
    """Run deterministic PO matching and persist the result.

    Loads the invoice, derives the PO reference from extraction, looks the PO
    up (when a reference exists), builds the match report, replaces this
    invoice's prior po_match result, records an audit event, and applies the
    status transition. Commits at the end.

    Args:
        db: Database session (this function owns the transaction).
        invoice_id: UUID of the invoice to match against a PO.

    Returns:
        The updated Invoice instance (refreshed after commit).

    Raises:
        LookupError: Invoice not found.
    """
    invoice = get_invoice(db, invoice_id)
    if invoice is None:
        raise LookupError(f"invoice not found: {invoice_id}")

    # Blank/whitespace-only counts as "no PO referenced" rather than a reference
    # that cannot be found - otherwise a provider emitting "" would flag the
    # invoice NEEDS_REVIEW instead of skipping.
    po_reference = (invoice.po_number or "").strip() or None
    po = (
        get_purchase_order_by_number(db, po_reference)
        if po_reference is not None
        else None
    )

    report = build_match_report(invoice, po, po_reference)

    # Re-running matching must replace prior po_match results, not accumulate
    # them. Do NOT delete other analysis types (calculation_check,
    # duplicate_check, anomaly, risk_score) - those are handled by their own
    # services.
    db.execute(
        delete(AnalysisResult).where(
            AnalysisResult.invoice_id == invoice_id,
            AnalysisResult.analysis_type == AnalysisType.PO_MATCH,
        )
    )
    # Flush so the DELETE is issued before the INSERT below; otherwise the new
    # row could conflict with an existing row on (invoice_id, analysis_type).
    db.flush()

    db.add(
        AnalysisResult(
            invoice_id=invoice_id,
            analysis_type=AnalysisType.PO_MATCH,
            status=_STATUS_BY_OUTCOME[report.overall_outcome],
            # Matching is pass/fail/warn, not scored - no normalized score.
            score=None,
            # The report is JSON-serializable as-is; mode="json" stringifies
            # UUIDs and enums for the JSONB column.
            details=report.model_dump(mode="json"),
        )
    )

    # No dedicated AuditEventType for PO matching yet; using NOTE with
    # structured data.
    db.add(
        AuditEvent(
            invoice_id=invoice.id,
            event_type=AuditEventType.NOTE,
            actor=AuditActor.SYSTEM,
            message=_match_message(report),
            data={
                "analysis": "po_match",
                "overall_outcome": str(report.overall_outcome.value),
                "po_reference": report.po_reference,
                "po_found": report.po_found,
                "mismatch_summary": report.mismatch_summary,
            },
        )
    )

    # --- status transition ---------------------------------------------------
    if (
        report.overall_outcome == MatchOutcome.MISMATCH
        and invoice.status not in _TERMINAL_STATUSES
    ):
        invoice.status = InvoiceStatus.NEEDS_REVIEW
    # Otherwise leave the status alone: MATCH/PARTIAL_MATCH/SKIPPED never
    # change it, and terminal statuses (APPROVED/REJECTED/FAILED) are never
    # downgraded - the same never-downgrade rule validation follows.

    db.commit()
    db.refresh(invoice)

    return invoice


def get_po_match_result(db: Session, invoice_id: UUID) -> AnalysisResult | None:
    """Return the latest PO_MATCH AnalysisResult for the invoice, or None.

    Ordered by created_at DESC with an id tiebreaker; normally only one row
    exists because run_po_matching replaces prior results.
    """
    stmt = (
        select(AnalysisResult)
        .where(
            AnalysisResult.invoice_id == invoice_id,
            AnalysisResult.analysis_type == AnalysisType.PO_MATCH,
        )
        .order_by(AnalysisResult.created_at.desc(), AnalysisResult.id.desc())
        .limit(1)
    )
    return db.scalars(stmt).first()
