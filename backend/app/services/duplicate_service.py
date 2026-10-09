"""Persist duplicate-invoice detection as an AnalysisResult row.

Runs the pure scorer in ``app.services.duplicate_detection`` against an invoice
and its same-vendor candidates, writes one :class:`AnalysisResult`, one audit
event, and (when the invoice is not in a terminal state) a status transition.

This module owns persistence for duplicate checking only: it never deletes other
analysis types (calculation_check, po_match, anomaly, risk_score), which are
written by their own services.

Status policy: only a confirmed DUPLICATE routes an invoice to NEEDS_REVIEW.
SUSPECTED_DUPLICATE is a warning, not a red flag - corroborating-but-weak
evidence should reach a human without blocking the pipeline.
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
from app.services.duplicate_detection.scorer import find_duplicates
from app.services.duplicate_detection.types import DuplicateReport, DuplicateVerdict
from app.services.invoices import get_invoice

__all__ = ["run_duplicate_check", "get_duplicate_result"]

#: Upper bound on candidates compared per run. Scoring is O(n) in candidates
#: and each one eager-loads its line items, so an unbounded scan would grow
#: both queries and per-row memory without bound. 1000 is far above any
#: realistic MVP corpus and keeps a single run comfortably fast.
MAX_DUPLICATE_CANDIDATES = 1000

#: DuplicateVerdict -> AnalysisStatus. Explicit map so a future verdict is a
#: loud failure here rather than a silently-skipped row.
_STATUS_BY_VERDICT = {
    DuplicateVerdict.DUPLICATE: AnalysisStatus.FAILED,
    DuplicateVerdict.SUSPECTED_DUPLICATE: AnalysisStatus.WARNING,
    DuplicateVerdict.UNIQUE: AnalysisStatus.PASSED,
}

#: Terminal statuses: a human or upstream process already made a decision.
#: Re-running this check must never downgrade these.
_TERMINAL_STATUSES = frozenset(
    {InvoiceStatus.APPROVED, InvoiceStatus.REJECTED, InvoiceStatus.FAILED}
)


def _candidates(db: Session, invoice: Invoice) -> list[Invoice]:
    """Same-vendor invoices, most recent first, bounded.

    Without a vendor there is nothing to compare against: every invoice would
    be a candidate for every other, and the "same vendor" signals could not
    mean anything. The scorer reports UNIQUE with an explanatory note instead.
    """
    if invoice.vendor_name is None:
        return []

    stmt = (
        select(Invoice)
        .options(selectinload(Invoice.line_items))
        .where(
            Invoice.id != invoice.id,
            Invoice.vendor_name.is_not(None),
            Invoice.vendor_name == invoice.vendor_name,
            # Soft-deleted invoices are gone from every read path: they must
            # not keep flagging live invoices as duplicates of a void.
            Invoice.deleted_at.is_(None),
        )
        # Most recent first: the nearest prior submissions are the likeliest
        # duplicates, and the bound then truncates the least relevant tail.
        .order_by(Invoice.created_at.desc())
        .limit(MAX_DUPLICATE_CANDIDATES)
    )
    return list(db.scalars(stmt).all())


def _message(report: DuplicateReport) -> str:
    """One sentence for the audit log."""
    if report.top_match is None:
        return "No duplicate found"

    score = report.top_match.score
    return (
        f"Duplicate verdict: {report.verdict.value} "
        f"(score {score}, matched {report.top_match.matching_invoice_id})"
    )


def run_duplicate_check(db: Session, *, invoice_id: UUID) -> Invoice:
    """Run duplicate detection and persist the result.

    Replaces this invoice's prior DUPLICATE_CHECK rows rather than appending,
    so repeated calls converge on one result. Commits at the end.

    Args:
        db: Database session (this function owns the transaction).
        invoice_id: UUID of the invoice to check.

    Returns:
        The updated Invoice instance (refreshed after commit).

    Raises:
        LookupError: Invoice not found.
    """
    invoice = get_invoice(db, invoice_id)
    if invoice is None:
        raise LookupError(f"invoice not found: {invoice_id}")

    candidates = _candidates(db, invoice)
    report = find_duplicates(invoice, candidates)

    if not candidates and invoice.vendor_name is None:
        # No candidates has two distinct causes: the invoice genuinely has no
        # vendor to match on, or it has one but no *other* invoice shares it.
        # The scorer already reports "no duplicate signals found" for the
        # second case — only overwrite with "vendor name missing" when the
        # vendor is actually absent.
        report = report.model_copy(update={"notes": ["vendor name missing"]})

    # Re-running must replace prior duplicate_check results, not accumulate
    # them. Do NOT delete other analysis types - each has its own service.
    db.execute(
        delete(AnalysisResult).where(
            AnalysisResult.invoice_id == invoice_id,
            AnalysisResult.analysis_type == AnalysisType.DUPLICATE_CHECK,
        )
    )
    # Flush so the DELETE is issued before the INSERT below.
    db.flush()

    top = report.top_match
    db.add(
        AnalysisResult(
            invoice_id=invoice_id,
            analysis_type=AnalysisType.DUPLICATE_CHECK,
            status=_STATUS_BY_VERDICT[report.verdict],
            # The strongest matching pair's score, or None when unique. This is
            # a real measured confidence, not a normalized composite.
            score=top.score if top is not None else None,
            details=report.model_dump(mode="json"),
        )
    )

    suspected = report.verdict in (
        DuplicateVerdict.DUPLICATE,
        DuplicateVerdict.SUSPECTED_DUPLICATE,
    )
    db.add(
        AuditEvent(
            invoice_id=invoice.id,
            event_type=(
                AuditEventType.DUPLICATE_SUSPECTED if suspected else AuditEventType.NOTE
            ),
            actor=AuditActor.SYSTEM,
            message=_message(report),
            data={
                "analysis": "duplicate_check",
                "verdict": report.verdict.value,
                "matching_invoice_id": (
                    str(top.matching_invoice_id) if top is not None else None
                ),
                "signals": (
                    [signal.value for signal in top.signals] if top is not None else []
                ),
            },
        )
    )

    # --- status transition ---------------------------------------------------
    if report.verdict == DuplicateVerdict.DUPLICATE and invoice.status not in _TERMINAL_STATUSES:
        invoice.status = InvoiceStatus.NEEDS_REVIEW
    # SUSPECTED_DUPLICATE deliberately leaves the status alone: it is a warning,
    # not a red flag, and gating the pipeline on weak corroborating evidence
    # would bury the real duplicates. UNIQUE and terminal statuses likewise
    # leave it unchanged - the never-downgrade rule still applies.

    db.commit()
    db.refresh(invoice)

    return invoice


def get_duplicate_result(db: Session, invoice_id: UUID) -> AnalysisResult | None:
    """Return the latest DUPLICATE_CHECK AnalysisResult, or None."""
    stmt = (
        select(AnalysisResult)
        .where(
            AnalysisResult.invoice_id == invoice_id,
            AnalysisResult.analysis_type == AnalysisType.DUPLICATE_CHECK,
        )
        .order_by(AnalysisResult.created_at.desc(), AnalysisResult.id.desc())
        .limit(1)
    )
    return db.scalars(stmt).first()