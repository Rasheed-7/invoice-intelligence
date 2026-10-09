"""Service-level tests for duplicate-check persistence.

Covers the six required scenarios against a real database via the shared
savepoint fixture. Invoices and documents are inserted by hand so each test
controls exactly which duplicate signals can fire.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.analysis import AnalysisResult
from app.models.audit import AuditEvent
from app.models.document import Document
from app.models.enums import (
    AnalysisStatus,
    AnalysisType,
    AuditActor,
    AuditEventType,
    InvoiceStatus,
)
from app.models.invoice import Invoice, LineItem
from app.services.duplicate_service import get_duplicate_result, run_duplicate_check

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def make_invoice(
    db: Session,
    *,
    vendor: str | None = "Acme Corp",
    invoice_number: str = "INV-0001",
    total: Decimal = Decimal("100.00"),
    sha256: str | None = None,
    created_at: datetime = BASE,
    lines: list[LineItem] | None = None,
    **overrides,
) -> Invoice:
    """Insert a Document + Invoice (+ optional line items) directly in the DB."""
    document = Document(
        filename="invoice.pdf",
        content_type="application/pdf",
        size_bytes=2048,
        sha256=sha256 or uuid.uuid4().hex,
        storage_path="/storage/invoice.pdf",
    )
    invoice = Invoice(
        document=document,
        vendor_name=vendor,
        invoice_number=invoice_number,
        currency="USD",
        subtotal=total,
        tax_amount=Decimal("8.50"),
        total_amount=total,
        created_at=created_at,
        **overrides,
    )
    if lines is not None:
        invoice.line_items = lines
    db.add(invoice)
    db.commit()
    return invoice


def line(description: str) -> LineItem:
    return LineItem(
        position=1,
        description=description,
        quantity=Decimal("2"),
        unit_price=Decimal("10.00"),
        line_total=Decimal("20.00"),
    )


def duplicate_rows(db: Session, invoice_id: uuid.UUID) -> list[AnalysisResult]:
    return list(
        db.scalars(
            select(AnalysisResult).where(
                AnalysisResult.invoice_id == invoice_id,
                AnalysisResult.analysis_type == AnalysisType.DUPLICATE_CHECK,
            )
        ).all()
    )


def events(db: Session, invoice_id: uuid.UUID) -> list[AuditEvent]:
    return list(
        db.scalars(select(AuditEvent).where(AuditEvent.invoice_id == invoice_id)).all()
    )


def test_duplicate_check_unique_invoice_passes(db_session: Session) -> None:
    """A vendor's only invoice has nothing to duplicate, so it passes."""
    invoice = make_invoice(db_session)

    result = run_duplicate_check(db_session, invoice_id=invoice.id)

    assert result.status == InvoiceStatus.INGESTED
    rows = duplicate_rows(db_session, invoice.id)
    assert len(rows) == 1
    assert rows[0].status == AnalysisStatus.PASSED
    assert rows[0].score is None
    assert rows[0].details["verdict"] == "unique"
    assert rows[0].details["top_match"] is None
    assert rows[0].details["all_matches"] == []

    # A clean result is routine, so it must not raise a duplicate alarm.
    event = events(db_session, invoice.id)[0]
    assert event.event_type == AuditEventType.NOTE
    assert event.actor == AuditActor.SYSTEM
    assert event.message == "No duplicate found"


def test_duplicate_check_identical_file_needs_review(db_session: Session) -> None:
    """The same bytes uploaded twice is a DUPLICATE and routes to review."""
    shared_sha = uuid.uuid4().hex
    make_invoice(
        db_session,
        invoice_number="INV-0001",
        sha256=shared_sha,
        lines=[line("Widget")],
    )
    invoice = make_invoice(
        db_session,
        invoice_number="INV-0002",
        sha256=shared_sha,
        lines=[line("Widget")],
    )

    result = run_duplicate_check(db_session, invoice_id=invoice.id)

    assert result.status == InvoiceStatus.NEEDS_REVIEW
    row = duplicate_rows(db_session, invoice.id)[0]
    assert row.status == AnalysisStatus.FAILED
    assert row.details["verdict"] == "duplicate"
    assert "identical_file" in row.details["top_match"]["signals"]
    assert row.score == 1.0

    event = events(db_session, invoice.id)[0]
    assert event.event_type == AuditEventType.DUPLICATE_SUSPECTED
    assert event.data["matching_invoice_id"] is not None


def test_duplicate_check_same_vendor_invoice_number_needs_review(
    db_session: Session,
) -> None:
    """Same vendor and invoice number is the canonical duplicate."""
    make_invoice(db_session, vendor="Acme Corp", invoice_number="INV-0001")
    invoice = make_invoice(db_session, vendor="Acme Corp", invoice_number="INV-0001")

    result = run_duplicate_check(db_session, invoice_id=invoice.id)

    assert result.status == InvoiceStatus.NEEDS_REVIEW
    row = duplicate_rows(db_session, invoice.id)[0]
    assert row.status == AnalysisStatus.FAILED
    assert row.details["verdict"] == "duplicate"
    assert "same_vendor_invoice_number" in row.details["top_match"]["signals"]


def test_duplicate_check_suspected_does_not_change_status(db_session: Session) -> None:
    """Weak corroborating evidence warns without gating the pipeline.

    Same vendor and same amount scores 0.4, which is below
    SUSPECTED_THRESHOLD (0.6) on its own - so a shared PO number is added as
    the corroborating signal (0.4 + 0.5 = 0.9) to reach the SUSPECTED tier.
    Line descriptions are deliberately dissimilar so LINE_ITEM_OVERLAP stays off
    and the pair does not cross into DUPLICATE.
    """
    make_invoice(
        db_session,
        vendor="Acme Corp",
        invoice_number="INV-A",
        total=Decimal("100.00"),
        po_number="PO-9",
        lines=[line("Steel Plate")],
    )
    invoice = make_invoice(
        db_session,
        vendor="Acme Corp",
        invoice_number="INV-B",
        total=Decimal("100.00"),
        po_number="PO-9",
        lines=[line("Copper Wire")],
    )
    make_invoice(
        db_session,
        vendor="Acme Corp",
        invoice_number="INV-C",
        total=Decimal("100.00"),
        po_number="PO-9",
        lines=[line("Bolt M10")],
        created_at=BASE + timedelta(days=1),
    )

    result = run_duplicate_check(db_session, invoice_id=invoice.id)

    row = duplicate_rows(db_session, invoice.id)[0]
    assert row.status == AnalysisStatus.WARNING
    assert row.details["verdict"] == "suspected_duplicate"
    # The point of the test: a warning must not move the invoice.
    assert result.status == InvoiceStatus.INGESTED


def test_duplicate_check_vendor_and_amount_alone_stays_unique(db_session: Session) -> None:
    """The honest lower bound: one weak signal is not enough to warn.

    SAME_VENDOR_AMOUNT weights 0.4, under SUSPECTED_THRESHOLD (0.6), so this
    pair is reported UNIQUE. Asserted explicitly so a future weight change that
    silently promotes every same-amount vendor to a warning is caught.
    """
    make_invoice(
        db_session,
        vendor="Acme Corp",
        invoice_number="INV-A",
        total=Decimal("100.00"),
        lines=[line("Steel Plate")],
    )
    invoice = make_invoice(
        db_session,
        vendor="Acme Corp",
        invoice_number="INV-B",
        total=Decimal("100.00"),
        lines=[line("Copper Wire")],
    )

    result = run_duplicate_check(db_session, invoice_id=invoice.id)

    row = duplicate_rows(db_session, invoice.id)[0]
    assert row.details["verdict"] == "unique"
    assert row.status == AnalysisStatus.PASSED
    assert result.status == InvoiceStatus.INGESTED


def test_duplicate_check_is_idempotent(db_session: Session) -> None:
    """A second run replaces the first row rather than appending."""
    shared_sha = uuid.uuid4().hex
    make_invoice(db_session, invoice_number="INV-0001", sha256=shared_sha)
    invoice = make_invoice(db_session, invoice_number="INV-0002", sha256=shared_sha)

    run_duplicate_check(db_session, invoice_id=invoice.id)
    first = dict(duplicate_rows(db_session, invoice.id)[0].details)
    run_duplicate_check(db_session, invoice_id=invoice.id)

    rows = duplicate_rows(db_session, invoice.id)
    assert len(rows) == 1
    assert dict(rows[0].details) == first


def test_duplicate_check_respects_terminal_status(db_session: Session) -> None:
    """APPROVED is a decision already made - a duplicate must not downgrade it."""
    shared_sha = uuid.uuid4().hex
    make_invoice(db_session, invoice_number="INV-0001", sha256=shared_sha)
    invoice = make_invoice(
        db_session,
        invoice_number="INV-0002",
        sha256=shared_sha,
        status=InvoiceStatus.APPROVED,
    )

    result = run_duplicate_check(db_session, invoice_id=invoice.id)

    assert result.status == InvoiceStatus.APPROVED
    # The finding is still recorded - only the transition is skipped.
    row = duplicate_rows(db_session, invoice.id)[0]
    assert row.status == AnalysisStatus.FAILED
    assert row.details["verdict"] == "duplicate"


def test_get_duplicate_result_returns_none_before_run(db_session: Session) -> None:
    """No run yet means no row, so the route can serve its neutral payload."""
    invoice = make_invoice(db_session)

    assert get_duplicate_result(db_session, invoice.id) is None


def test_duplicate_check_missing_invoice_raises(db_session: Session) -> None:
    """A random UUID has no invoice to check -> LookupError."""
    with pytest.raises(LookupError):
        run_duplicate_check(db_session, invoice_id=uuid.uuid4())