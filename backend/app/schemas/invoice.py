"""Invoice response schemas."""

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.enums import DocumentProcessingStatus, InvoiceStatus

if TYPE_CHECKING:
    from app.models.invoice import Invoice


class LineItemResponse(BaseModel):
    id: UUID
    position: int
    description: str | None
    quantity: Decimal | None
    unit_price: Decimal | None
    line_total: Decimal | None
    tax_rate: Decimal | None

    model_config = ConfigDict(from_attributes=True)


class InvoiceResponse(BaseModel):
    """Invoice representation for list views.

    Carries just enough of the document and extracted fields to render a list
    row. The remaining document metadata and money fields live only on
    :class:`InvoiceDetailResponse`.
    """

    id: UUID
    status: InvoiceStatus
    created_at: datetime
    updated_at: datetime

    # Flattened document fields - the list only needs the filename.
    document_id: UUID
    document_filename: str

    # Extracted invoice fields (all nullable until extraction runs)
    vendor_name: str | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    currency: str | None = None
    po_number: str | None = None
    total_amount: Decimal | None = None

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_orm_invoice(cls, invoice: "Invoice") -> "InvoiceResponse":
        """Build a list response by explicitly flattening the document relationship."""

        return cls(
            id=invoice.id,
            status=invoice.status,
            created_at=invoice.created_at,
            updated_at=invoice.updated_at,
            document_id=invoice.document.id,
            document_filename=invoice.document.filename,
            vendor_name=invoice.vendor_name,
            invoice_number=invoice.invoice_number,
            invoice_date=invoice.invoice_date,
            currency=invoice.currency,
            po_number=invoice.po_number,
            total_amount=invoice.total_amount,
        )


class InvoiceDetailResponse(BaseModel):
    """Full invoice representation with document and extracted fields."""

    id: UUID
    status: InvoiceStatus
    created_at: datetime
    updated_at: datetime

    # Flattened document fields
    document_id: UUID
    document_filename: str
    document_content_type: str
    document_size_bytes: int
    document_processing_status: DocumentProcessingStatus

    # Extracted invoice fields (all nullable)
    vendor_name: str | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    due_date: date | None = None
    currency: str | None = None
    po_number: str | None = None
    subtotal: Decimal | None = None
    tax_amount: Decimal | None = None
    total_amount: Decimal | None = None

    # Line items
    line_items: list[LineItemResponse] = []

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_orm_invoice(cls, invoice: "Invoice") -> "InvoiceDetailResponse":
        """Build a detail response by explicitly flattening the document relationship."""
        return cls(
            id=invoice.id,
            status=invoice.status,
            created_at=invoice.created_at,
            updated_at=invoice.updated_at,
            document_id=invoice.document.id,
            document_filename=invoice.document.filename,
            document_content_type=invoice.document.content_type,
            document_size_bytes=invoice.document.size_bytes,
            document_processing_status=invoice.document.processing_status,
            vendor_name=invoice.vendor_name,
            invoice_number=invoice.invoice_number,
            invoice_date=invoice.invoice_date,
            due_date=invoice.due_date,
            currency=invoice.currency,
            po_number=invoice.po_number,
            subtotal=invoice.subtotal,
            tax_amount=invoice.tax_amount,
            total_amount=invoice.total_amount,
            line_items=[LineItemResponse.model_validate(li) for li in invoice.line_items],
        )


class InvoiceListResponse(BaseModel):
    """Paginated list of invoices."""

    items: list[InvoiceResponse]
    total: int
    limit: int
    offset: int