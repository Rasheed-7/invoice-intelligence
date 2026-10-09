"""Invoice model and its line items.

File-level metadata (filename, content type, hash, extracted text) now lives on
:class:`~app.models.document.Document`. An invoice references exactly one
document via a unique, cascading foreign key.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import InvoiceStatus, pg_enum

if TYPE_CHECKING:
    from app.models.analysis import AnalysisResult
    from app.models.audit import AuditEvent
    from app.models.document import Document
    from app.models.review import ReviewCase


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # One-to-one with the uploaded file. Unique so a document cannot back
    # more than one invoice; cascades so deleting the document removes it.
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    # --- normalized extracted fields (nullable until extraction runs) ---
    vendor_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    invoice_number: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    po_number: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    invoice_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)

    subtotal: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    tax_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    total_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)

    # --- raw extraction artifacts ---
    # Full LLM output, kept for debugging and re-processing.
    extraction_raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Per-field confidence scores, e.g. {"vendor_name": 0.94, "total_amount": 0.88}
    extraction_confidence: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # --- lifecycle ---
    status: Mapped[InvoiceStatus] = mapped_column(
        pg_enum(InvoiceStatus, "invoice_status"),
        nullable=False,
        default=InvoiceStatus.INGESTED,
        index=True,
    )

    # Soft-delete marker. A set value means the invoice is gone from reads but
    # its row — and every analysis, audit event, line item, and review attached
    # to it — is preserved for compliance. NULL means live.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    # --- timestamps ---
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.clock_timestamp(),
        onupdate=func.clock_timestamp(),
    )

    # --- relationships ---
    document: Mapped["Document"] = relationship()
    line_items: Mapped[list["LineItem"]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
        order_by="LineItem.position",
    )
    audit_events: Mapped[list["AuditEvent"]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
        order_by="AuditEvent.created_at",
    )
    analysis_results: Mapped[list["AnalysisResult"]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
        order_by="AnalysisResult.created_at",
    )
    review_case: Mapped["ReviewCase | None"] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
        uselist=False,
    )

    __table_args__ = (
        # Common query: "have we seen this vendor's invoice number before?"
        Index("ix_invoices_vendor_invoice_number", "vendor_name", "invoice_number"),
    )


class LineItem(Base):
    __tablename__ = "line_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Ordering within the invoice. Kept explicit so we don't rely on insert order.
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    line_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    tax_rate: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)

    # Per-line confidence, same idea as invoice-level.
    extraction_confidence: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    invoice: Mapped[Invoice] = relationship(back_populates="line_items")

    __table_args__ = (
        Index("ix_line_items_invoice_position", "invoice_id", "position", unique=True),
    )
