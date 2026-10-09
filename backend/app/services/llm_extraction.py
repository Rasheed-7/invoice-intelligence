"""LLM extraction service.

Orchestrates the full extraction pipeline: load document text, call LLM provider,
validate and apply structured data to the invoice, write line items and audit log.

Prompt-injection boundary: ``EXTRACTION_PROMPT`` (instructions) and ``document_text``
(untrusted data) are kept separate and never concatenated. The document text is
passed as a separate argument to ``provider.extract_structured``. This module
demonstrates the boundary by construction: the prompt is a module constant, the
document text is read from the database, and they meet only at the provider's
function boundary.
"""

from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.enums import AuditActor, AuditEventType, InvoiceStatus
from app.models.invoice import Invoice, LineItem
from app.schemas.extraction import (
    extraction_from_llm_response,
    validate_confidence_keys,
)
from app.services.invoices import get_invoice
from app.services.llm.factory import get_llm_provider
from app.services.llm.provider import LLMProvider, LLMProviderError

__all__ = [
    "ExtractionSourceEmpty",
    "LLMExtractionFailed",
    "run_llm_extraction",
]

# System-level instruction for the LLM provider. This is the "what to extract"
# guidance. Document content is NEVER concatenated into this string; it is passed
# separately to maintain the instruction/data boundary.
EXTRACTION_PROMPT = """Extract structured invoice data from the provided document text.

Return the following fields when present:
- Vendor name and tax ID
- Invoice number, date, due date, and currency
- Purchase order number (if referenced)
- Line items with description, quantity, unit price, line total, and tax rate
- Totals: subtotal, tax amount, and total amount

Return null for any field that is not present or cannot be determined with confidence.
All monetary amounts must be decimal values, never floats."""


class ExtractionSourceEmpty(Exception):
    """Raised when the document has no extracted text.

    This indicates the document text extraction step has not run or failed.
    The caller should trigger text extraction first before retrying LLM extraction.
    """


class LLMExtractionFailed(Exception):
    """Raised when the LLM provider raises an error or returns invalid data.

    This wraps both provider errors (API failures, timeouts) and validation
    errors (malformed output, unknown confidence keys).
    """


def run_llm_extraction(
    db: Session,
    *,
    invoice_id: UUID,
    provider: LLMProvider | None = None,
) -> Invoice:
    """Run LLM extraction on an invoice and apply the structured result.

    Loads the invoice and its document, calls the LLM provider with the document
    text, validates the structured output, applies extracted fields to the invoice,
    writes line items, and records an audit event.

    Args:
        db: Database session (caller owns the transaction; we commit at the end).
        invoice_id: UUID of the invoice to extract.
        provider: Optional LLM provider instance. If None, uses get_llm_provider().

    Returns:
        The updated Invoice instance (refreshed after commit).

    Raises:
        LookupError: Invoice not found.
        ExtractionSourceEmpty: Document has no extracted text (extraction not run).
        LLMExtractionFailed: Provider error or validation failure.
    """
    # Load invoice and document
    invoice = get_invoice(db, invoice_id)
    if invoice is None:
        raise LookupError(f"invoice not found: {invoice_id}")

    document = invoice.document

    # Validate extracted text is present
    if document.extracted_text is None or document.extracted_text.strip() == "":
        raise ExtractionSourceEmpty(
            f"document {document.id} has no extracted text; "
            "run text extraction first before LLM extraction"
        )

    # Get provider
    if provider is None:
        provider = get_llm_provider()

    # Call LLM provider (prompt and document text are separate parameters)
    try:
        response = provider.extract_structured(
            prompt=EXTRACTION_PROMPT,
            document_text=document.extracted_text,
            document_sha256=document.sha256,
        )
    except LLMProviderError as exc:
        raise LLMExtractionFailed(f"provider error: {exc}") from exc

    # Convert response to extraction schema
    try:
        extraction = extraction_from_llm_response(response)
    except ValidationError as exc:
        raise LLMExtractionFailed(f"invalid provider output: {exc}") from exc

    # Validate confidence keys correspond to real schema paths
    invalid_keys = validate_confidence_keys(extraction)
    if invalid_keys:
        raise LLMExtractionFailed(
            f"provider emitted confidence for unknown fields: {invalid_keys}"
        )

    # Apply extraction to invoice fields
    invoice.vendor_name = extraction.vendor.name
    invoice.invoice_number = extraction.invoice.number
    invoice.invoice_date = extraction.invoice.date
    invoice.due_date = extraction.invoice.due_date
    invoice.currency = extraction.invoice.currency
    # PO reference is promoted to its own column; it is no longer read from extraction_raw.
    invoice.po_number = extraction.purchase_order.number
    invoice.subtotal = extraction.totals.subtotal
    invoice.tax_amount = extraction.totals.tax
    invoice.total_amount = extraction.totals.total
    invoice.extraction_raw = response.model_dump(mode="json")
    invoice.extraction_confidence = extraction.meta.confidence
    invoice.status = InvoiceStatus.EXTRACTED

    # Delete existing line items (re-processing is allowed)
    for existing_item in list(invoice.line_items):
        db.delete(existing_item)
    
    # Flush to ensure deletions are committed before inserts
    db.flush()

    # Write new line items (1-based position)
    for index, item in enumerate(extraction.items, start=1):
        line_item = LineItem(
            invoice_id=invoice.id,
            position=index,
            description=item.description,
            quantity=item.quantity,
            unit_price=item.unit_price,
            line_total=item.line_total,
            tax_rate=item.tax_rate,
        )
        db.add(line_item)

    # Write audit event
    confidence_values = list(extraction.meta.confidence.values())
    audit_event = AuditEvent(
        invoice_id=invoice.id,
        event_type=AuditEventType.EXTRACTED,
        actor=AuditActor.LLM,
        message=f"Extracted {len(extraction.items)} item(s) from {document.filename}",
        data={
            "source": extraction.meta.source,
            "provider": extraction.meta.provider_name,
            "model": extraction.meta.model_name,
            "item_count": len(extraction.items),
            "confidence_min": min(confidence_values) if confidence_values else None,
        },
    )
    db.add(audit_event)

    # Commit and refresh
    db.commit()
    db.refresh(invoice)

    return invoice
