"""Tests for the LLM extraction service."""

from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models.enums import AuditActor, AuditEventType, InvoiceStatus
from app.models.invoice import LineItem
from app.services.document_processing import process_document_extraction
from app.services.documents import create_document
from app.services.invoices import create_invoice_from_upload
from app.services.llm.fixture_pdfs import build_fixture_pdf
from app.services.llm.fixtures import FIXTURES
from app.services.llm.provider import LLMProviderError, LLMResponse
from app.services.llm_extraction import (
    ExtractionSourceEmpty,
    LLMExtractionFailed,
    run_llm_extraction,
)
from app.services.storage import LocalStorage


def test_run_extraction_populates_invoice_fields(
    db_session: Session, tmp_path: Path
) -> None:
    """Extract from acme-simple fixture PDF; assert invoice fields populated."""
    storage = LocalStorage(str(tmp_path))
    fixture = FIXTURES[0]  # acme-simple
    pdf_bytes = build_fixture_pdf(fixture)

    document = create_document(
        db_session,
        filename="acme-simple.pdf",
        content_type="application/pdf",
        data=pdf_bytes,
        storage=storage,
    )
    invoice = create_invoice_from_upload(db_session, document=document)
    db_session.commit()

    # Run text extraction first
    process_document_extraction(db_session, document_id=document.id, storage=storage)

    # Run LLM extraction
    run_llm_extraction(db_session, invoice_id=invoice.id)

    # Verify invoice fields
    assert invoice.vendor_name == "Acme Corporation"
    assert invoice.invoice_number == "INV-2024-001"
    assert invoice.total_amount == Decimal("108.50")
    assert invoice.status == InvoiceStatus.EXTRACTED

    # Verify extraction artifacts
    assert invoice.extraction_raw is not None
    assert isinstance(invoice.extraction_raw, dict)
    assert invoice.extraction_raw.get("source") == "fixture"

    assert invoice.extraction_confidence is not None
    assert isinstance(invoice.extraction_confidence, dict)
    assert "vendor.name" in invoice.extraction_confidence


def test_extraction_populates_po_number(
    db_session: Session, tmp_path: Path
) -> None:
    """globex-multiline carries a PO number; it must land in the po_number column."""
    storage = LocalStorage(str(tmp_path))
    fixture = FIXTURES[1]  # globex-multiline
    assert fixture.ground_truth["po_number"] == "PO-EU-4411"
    pdf_bytes = build_fixture_pdf(fixture)

    document = create_document(
        db_session,
        filename="globex-multiline.pdf",
        content_type="application/pdf",
        data=pdf_bytes,
        storage=storage,
    )
    invoice = create_invoice_from_upload(db_session, document=document)
    db_session.commit()

    # Before extraction the reference is absent.
    assert invoice.po_number is None

    process_document_extraction(db_session, document_id=document.id, storage=storage)
    run_llm_extraction(db_session, invoice_id=invoice.id)

    # Promoted out of extraction_raw into its own indexed column.
    assert invoice.po_number == "PO-EU-4411"


def test_extraction_leaves_po_number_none_when_absent(
    db_session: Session, tmp_path: Path
) -> None:
    """acme-simple has no PO reference, so po_number stays NULL (not "")."""
    storage = LocalStorage(str(tmp_path))
    fixture = FIXTURES[0]  # acme-simple
    assert fixture.ground_truth["po_number"] is None
    pdf_bytes = build_fixture_pdf(fixture)

    document = create_document(
        db_session,
        filename="acme-simple.pdf",
        content_type="application/pdf",
        data=pdf_bytes,
        storage=storage,
    )
    invoice = create_invoice_from_upload(db_session, document=document)
    db_session.commit()

    process_document_extraction(db_session, document_id=document.id, storage=storage)
    run_llm_extraction(db_session, invoice_id=invoice.id)

    assert invoice.po_number is None


def test_run_extraction_refreshes_po_number_on_reprocess(
    db_session: Session, tmp_path: Path
) -> None:
    """Re-running extraction replaces po_number rather than keeping a stale one.

    This is the specific failure the extraction_raw workaround had: the JSONB
    blob was overwritten wholesale, so any write to it was lost.
    """
    storage = LocalStorage(str(tmp_path))

    # Start with a stale reference from an unrelated invoice.
    document = create_document(
        db_session,
        filename="globex-multiline.pdf",
        content_type="application/pdf",
        data=build_fixture_pdf(FIXTURES[1]),
        storage=storage,
    )
    invoice = create_invoice_from_upload(db_session, document=document)
    invoice.po_number = "PO-STALE-999"
    db_session.commit()

    process_document_extraction(db_session, document_id=document.id, storage=storage)
    run_llm_extraction(db_session, invoice_id=invoice.id)

    assert invoice.po_number == "PO-EU-4411"


def test_run_extraction_creates_line_items(
    db_session: Session, tmp_path: Path
) -> None:
    """Extract from acme-simple; assert line item created correctly."""
    storage = LocalStorage(str(tmp_path))
    fixture = FIXTURES[0]
    pdf_bytes = build_fixture_pdf(fixture)

    document = create_document(
        db_session,
        filename="acme-simple.pdf",
        content_type="application/pdf",
        data=pdf_bytes,
        storage=storage,
    )
    invoice = create_invoice_from_upload(db_session, document=document)
    db_session.commit()

    process_document_extraction(db_session, document_id=document.id, storage=storage)
    run_llm_extraction(db_session, invoice_id=invoice.id)

    # Verify one line item
    assert len(invoice.line_items) == 1
    line_item = invoice.line_items[0]

    assert line_item.position == 1
    assert line_item.description == "Widget"
    assert line_item.quantity == Decimal("1")
    assert line_item.unit_price == Decimal("100.00")
    assert line_item.line_total == Decimal("100.00")


def test_run_extraction_is_idempotent(db_session: Session, tmp_path: Path) -> None:
    """Run extraction twice; assert fields identical and line items not duplicated."""
    storage = LocalStorage(str(tmp_path))
    fixture = FIXTURES[0]
    pdf_bytes = build_fixture_pdf(fixture)

    document = create_document(
        db_session,
        filename="acme-simple.pdf",
        content_type="application/pdf",
        data=pdf_bytes,
        storage=storage,
    )
    invoice = create_invoice_from_upload(db_session, document=document)
    db_session.commit()

    process_document_extraction(db_session, document_id=document.id, storage=storage)

    # First extraction
    run_llm_extraction(db_session, invoice_id=invoice.id)
    first_vendor = invoice.vendor_name
    first_total = invoice.total_amount
    first_item_count = len(invoice.line_items)

    # Second extraction (idempotent)
    run_llm_extraction(db_session, invoice_id=invoice.id)

    # Verify fields unchanged
    assert invoice.vendor_name == first_vendor
    assert invoice.total_amount == first_total
    assert len(invoice.line_items) == first_item_count
    assert len(invoice.line_items) == 1  # Not duplicated


def test_run_extraction_without_text_raises(
    db_session: Session, tmp_path: Path
) -> None:
    """Document with no extracted text raises ExtractionSourceEmpty."""
    storage = LocalStorage(str(tmp_path))

    # Create document without running text extraction
    document = create_document(
        db_session,
        filename="empty.pdf",
        content_type="application/pdf",
        data=b"%PDF-1.7\n%%EOF",
        storage=storage,
    )
    invoice = create_invoice_from_upload(db_session, document=document)
    db_session.commit()

    # extracted_text is None
    assert document.extracted_text is None

    with pytest.raises(ExtractionSourceEmpty):
        run_llm_extraction(db_session, invoice_id=invoice.id)


def test_run_extraction_missing_invoice_raises(db_session: Session) -> None:
    """Random UUID raises LookupError."""
    random_id = uuid4()

    with pytest.raises(LookupError, match="invoice not found"):
        run_llm_extraction(db_session, invoice_id=random_id)


def test_run_extraction_writes_audit_event(
    db_session: Session, tmp_path: Path
) -> None:
    """Extraction writes an AuditEvent with correct metadata."""
    storage = LocalStorage(str(tmp_path))
    fixture = FIXTURES[0]
    pdf_bytes = build_fixture_pdf(fixture)

    document = create_document(
        db_session,
        filename="acme-simple.pdf",
        content_type="application/pdf",
        data=pdf_bytes,
        storage=storage,
    )
    invoice = create_invoice_from_upload(db_session, document=document)
    db_session.commit()

    process_document_extraction(db_session, document_id=document.id, storage=storage)
    run_llm_extraction(db_session, invoice_id=invoice.id)

    # Find the EXTRACTED audit event
    events = [e for e in invoice.audit_events if e.event_type == AuditEventType.EXTRACTED]
    assert len(events) == 1

    event = events[0]
    assert event.actor == AuditActor.LLM
    assert event.data is not None
    assert event.data["source"] == "fixture"
    assert event.data["provider"] == "mock-hybrid"
    assert "model" in event.data
    assert "item_count" in event.data


def test_run_extraction_replaces_line_items_on_reprocess(
    db_session: Session, tmp_path: Path
) -> None:
    """Manually insert fake line items, then extract; assert fakes replaced."""
    storage = LocalStorage(str(tmp_path))
    fixture = FIXTURES[0]
    pdf_bytes = build_fixture_pdf(fixture)

    document = create_document(
        db_session,
        filename="acme-simple.pdf",
        content_type="application/pdf",
        data=pdf_bytes,
        storage=storage,
    )
    invoice = create_invoice_from_upload(db_session, document=document)
    db_session.commit()

    # Insert two fake line items
    fake1 = LineItem(
        invoice_id=invoice.id,
        position=1,
        description="Fake Item 1",
        quantity=Decimal("99"),
        unit_price=Decimal("99.99"),
    )
    fake2 = LineItem(
        invoice_id=invoice.id,
        position=2,
        description="Fake Item 2",
        quantity=Decimal("88"),
        unit_price=Decimal("88.88"),
    )
    db_session.add(fake1)
    db_session.add(fake2)
    db_session.commit()

    assert len(invoice.line_items) == 2

    # Run extraction
    process_document_extraction(db_session, document_id=document.id, storage=storage)
    run_llm_extraction(db_session, invoice_id=invoice.id)

    # Verify fakes replaced with extracted item
    assert len(invoice.line_items) == 1
    assert invoice.line_items[0].description == "Widget"
    assert invoice.line_items[0].quantity == Decimal("1")


def test_run_extraction_provider_failure_raises(
    db_session: Session, tmp_path: Path
) -> None:
    """Provider that raises LLMProviderError results in LLMExtractionFailed."""
    storage = LocalStorage(str(tmp_path))
    fixture = FIXTURES[0]
    pdf_bytes = build_fixture_pdf(fixture)

    document = create_document(
        db_session,
        filename="acme-simple.pdf",
        content_type="application/pdf",
        data=pdf_bytes,
        storage=storage,
    )
    invoice = create_invoice_from_upload(db_session, document=document)
    db_session.commit()

    process_document_extraction(db_session, document_id=document.id, storage=storage)

    # Store original fields
    original_vendor = invoice.vendor_name
    original_total = invoice.total_amount

    # Provider that always fails
    class FailingProvider:
        def extract_structured(
            self, *, prompt: str, document_text: str, document_sha256: str | None = None
        ) -> LLMResponse:
            raise LLMProviderError("simulated provider failure")

        def name(self) -> str:
            return "failing-mock"

    with pytest.raises(LLMExtractionFailed, match="provider error"):
        run_llm_extraction(db_session, invoice_id=invoice.id, provider=FailingProvider())

    # Verify invoice fields were not modified
    db_session.refresh(invoice)
    assert invoice.vendor_name == original_vendor
    assert invoice.total_amount == original_total


def test_run_extraction_invalid_provider_output_raises(
    db_session: Session, tmp_path: Path
) -> None:
    """Provider returning invalid data raises LLMExtractionFailed."""
    storage = LocalStorage(str(tmp_path))
    fixture = FIXTURES[0]
    pdf_bytes = build_fixture_pdf(fixture)

    document = create_document(
        db_session,
        filename="acme-simple.pdf",
        content_type="application/pdf",
        data=pdf_bytes,
        storage=storage,
    )
    invoice = create_invoice_from_upload(db_session, document=document)
    db_session.commit()

    process_document_extraction(db_session, document_id=document.id, storage=storage)

    # Store original fields
    original_vendor = invoice.vendor_name
    original_total = invoice.total_amount

    # Provider that returns invalid data (unknown field)
    class InvalidProvider:
        def extract_structured(
            self, *, prompt: str, document_text: str, document_sha256: str | None = None
        ) -> LLMResponse:
            return LLMResponse(
                data={"vendor": {"name": "X", "unknown_field": "Y"}},
                confidence={},
                source="mock",
                provider_name="invalid-mock",
                model_name="invalid-v1",
                raw_response="{}",
                duration_ms=0,
            )

        def name(self) -> str:
            return "invalid-mock"

    with pytest.raises(LLMExtractionFailed, match="invalid provider output"):
        run_llm_extraction(db_session, invoice_id=invoice.id, provider=InvalidProvider())

    # Verify invoice unchanged
    db_session.refresh(invoice)
    assert invoice.vendor_name == original_vendor
    assert invoice.total_amount == original_total



