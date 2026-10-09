"""Invoice service.

Business logic for creating and querying invoices. Deliberately free of FastAPI
imports so it can be tested without HTTP.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models.audit import AuditEvent
from app.models.enums import AuditActor, AuditEventType, InvoiceStatus
from app.models.invoice import Invoice

if TYPE_CHECKING:
    from app.models.document import Document

__all__ = [
    "InvoiceAlreadyDeleted",
    "create_invoice_from_upload",
    "get_invoice",
    "get_invoice_including_deleted",
    "list_invoices",
    "soft_delete_invoice",
]


class InvoiceAlreadyDeleted(Exception):
    """Raised when soft-deleting an invoice that is already soft-deleted."""


def create_invoice_from_upload(db: Session, *, document: "Document") -> Invoice:
    """Create and stage an Invoice linked to a Document.

    The invoice starts in INGESTED status with all extracted fields as None.
    The row is added to the session but *not* committed - the caller owns the
    transaction.
    """
    invoice = Invoice(document_id=document.id, status=InvoiceStatus.INGESTED)
    db.add(invoice)
    return invoice


def list_invoices(db: Session, *, limit: int = 50, offset: int = 0) -> tuple[list[Invoice], int]:
    """Return a paginated list of live invoices and the total live count.

    Soft-deleted rows (``deleted_at IS NOT NULL``) are excluded from both the
    page and the count. Ordered by created_at DESC. Returns (invoices, total)
    where total is the unfiltered count of all live invoices.
    """
    live = Invoice.deleted_at.is_(None)
    total = db.scalar(select(func.count(Invoice.id)).where(live)) or 0

    stmt = (
        select(Invoice)
        # Eager-load the document: the list serialization reads filename, so
        # without this the response is an N+1 per invoice.
        .options(joinedload(Invoice.document))
        .where(live)
        # Tiebreaker: rows created in the same transaction share a timestamp.
        .order_by(Invoice.created_at.desc(), Invoice.id.desc())
        .limit(limit)
        .offset(offset)
    )
    invoices = db.scalars(stmt).all()

    return invoices, total


def get_invoice(db: Session, invoice_id: UUID) -> Invoice | None:
    """Return the live invoice with joined document and line items, or None.

    Soft-deleted invoices read as "not found" here. Every downstream service
    (validation, PO matching, policy, duplicates, anomalies, risk, review)
    loads through this function, so they all treat a deleted invoice as
    missing with no change to their own code.
    """
    stmt = (
        select(Invoice)
        .options(joinedload(Invoice.document), selectinload(Invoice.line_items))
        .where(Invoice.id == invoice_id, Invoice.deleted_at.is_(None))
    )
    return db.scalars(stmt).first()


def get_invoice_including_deleted(db: Session, invoice_id: UUID) -> Invoice | None:
    """Return the invoice even if soft-deleted. Used by the delete endpoint to
    check whether it's already deleted and to write the audit event."""
    stmt = (
        select(Invoice)
        .options(joinedload(Invoice.document), selectinload(Invoice.line_items))
        .where(Invoice.id == invoice_id)
    )
    return db.scalars(stmt).first()


def soft_delete_invoice(db: Session, *, invoice_id: UUID) -> Invoice:
    """Soft-delete an invoice, preserving its full audit trail.

    Soft delete preserves the audit trail. The invoice row remains, its
    analysis and audit events remain, only deleted_at is set. A reviewer or
    auditor can still see what was deleted and when.

    The Document row, LineItem rows, AnalysisResult rows, AuditEvent rows, and
    ReviewCase row are all left untouched — this is one column change plus one
    audit event. The document may be referenced by other invoices with the same
    sha256, so deleting it would corrupt them.

    Raises:
        LookupError: Invoice not found (including never-existed ids).
        InvoiceAlreadyDeleted: The invoice is already soft-deleted.
    """
    invoice = get_invoice_including_deleted(db, invoice_id)
    if invoice is None:
        raise LookupError(f"invoice not found: {invoice_id}")

    if invoice.deleted_at is not None:
        raise InvoiceAlreadyDeleted(f"invoice already deleted: {invoice_id}")

    invoice.deleted_at = datetime.now(UTC)

    db.add(
        AuditEvent(
            invoice_id=invoice.id,
            event_type=AuditEventType.NOTE,
            actor=AuditActor.SYSTEM,
            message=f"Invoice soft-deleted: {invoice.invoice_number or invoice.id}",
            data={
                "analysis": "delete",
                "invoice_number": invoice.invoice_number,
                "vendor_name": invoice.vendor_name,
            },
        )
    )

    db.commit()
    db.refresh(invoice)

    return invoice