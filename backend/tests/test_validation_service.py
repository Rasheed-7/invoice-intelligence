"""Service-level tests for validation persistence.

These exercise ``run_validation`` and ``get_validation_results`` directly
against the database (via the shared ``db_session`` savepoint fixture) with
no HTTP involved. Invoices are inserted by hand so each test controls the
exact state under validation.
"""

import uuid
from datetime import date
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
from app.services.validation.runner import ALL_CHECKS, ALL_DB_CHECKS
from app.services.validation_service import get_validation_results, run_validation


def make_document(**overrides) -> Document:
    data = {
        "filename": "validation.pdf",
        "content_type": "application/pdf",
        "size_bytes": 2048,
        "sha256": "0" * 64,
        "storage_path": "/storage/validation.pdf",
    }
    data.update(overrides)
    return Document(**data)


def make_invoice(db: Session, *, line_items: list[LineItem] | None = None, **overrides) -> Invoice:
    """Insert a Document + Invoice (+ optional line items) directly in the DB."""
    data = {
        "vendor_name": "Acme Corp",
        "invoice_number": "INV-0001",
        "currency": "USD",
        "invoice_date": date(2026, 1, 15),
        "due_date": date(2026, 2, 15),
        "subtotal": Decimal("100.00"),
        "tax_amount": Decimal("8.50"),
        "total_amount": Decimal("108.50"),
    }
    data.update(overrides)
    invoice = Invoice(document=make_document(), **data)
    if line_items is not None:
        invoice.line_items = line_items
    db.add(invoice)
    # Commit, not just flush: run_validation re-loads the invoice, and an
    # uncommitted object would be read with in-memory Decimal scales while a
    # second run reads DB Numeric(14,4) scales - same value, different strings.
    # Production always validates a persisted invoice.
    db.commit()
    return invoice


def passing_line_item() -> LineItem:
    """1 x 100.00 = 100.00, consistent with make_invoice's monetary defaults."""
    return LineItem(
        position=1,
        description="Widget",
        quantity=Decimal("1"),
        unit_price=Decimal("100.00"),
        line_total=Decimal("100.00"),
    )


def test_validate_passing_invoice_sets_validated_status(db_session: Session) -> None:
    """A consistent invoice validates to VALIDATED with no FAILED rows."""
    invoice = make_invoice(db_session, line_items=[passing_line_item()])

    result = run_validation(db_session, invoice_id=invoice.id)

    assert result.status == InvoiceStatus.VALIDATED
    rows = get_validation_results(db_session, invoice.id)
    assert rows
    assert all(r.status in (AnalysisStatus.PASSED, AnalysisStatus.SKIPPED) for r in rows)
    assert not any(r.status == AnalysisStatus.FAILED for r in rows)


def test_validate_failing_invoice_sets_needs_review(db_session: Session) -> None:
    """A line-item mismatch moves the invoice to NEEDS_REVIEW with a FAILED row."""
    bad_item = LineItem(
        position=1,
        description="Widget",
        quantity=Decimal("2"),
        unit_price=Decimal("10.00"),
        line_total=Decimal("21.00"),  # 2 x 10.00 = 20.00 -> delta 1.00
    )
    invoice = make_invoice(db_session, line_items=[bad_item])

    result = run_validation(db_session, invoice_id=invoice.id)

    assert result.status == InvoiceStatus.NEEDS_REVIEW
    rows = get_validation_results(db_session, invoice.id)
    failed = [r for r in rows if r.status == AnalysisStatus.FAILED]
    assert failed
    assert failed[0].details["check_name"] == "line_item_totals"
    # Per-mismatch evidence lives inside "mismatches"; compare as Decimal so
    # the assertion holds regardless of the Numeric(14,4) string scale.
    mismatch = failed[0].details["mismatches"][0]
    assert Decimal(mismatch["delta"]) == Decimal("1.00")
    assert Decimal(mismatch["expected"]) == Decimal("20.00")
    assert Decimal(mismatch["actual"]) == Decimal("21.00")


def test_validate_creates_one_result_per_check(db_session: Session) -> None:
    """Exactly one calculation_check row per check in ALL_CHECKS - no more."""
    invoice = make_invoice(db_session, line_items=[passing_line_item()])

    run_validation(db_session, invoice_id=invoice.id)

    rows = db_session.scalars(
        select(AnalysisResult).where(
            AnalysisResult.invoice_id == invoice.id,
            AnalysisResult.analysis_type == AnalysisType.CALCULATION_CHECK,
        )
    ).all()
    assert len(rows) == 7
    assert len(rows) == len(ALL_CHECKS) + len(ALL_DB_CHECKS)


def test_validate_is_idempotent(db_session: Session) -> None:
    """A second run replaces the first run's rows rather than appending."""
    invoice = make_invoice(db_session, line_items=[passing_line_item()])

    run_validation(db_session, invoice_id=invoice.id)
    first = [
        (r.details["check_name"], dict(r.details), r.status)
        for r in get_validation_results(db_session, invoice.id)
    ]

    run_validation(db_session, invoice_id=invoice.id)
    second = [
        (r.details["check_name"], dict(r.details), r.status)
        for r in get_validation_results(db_session, invoice.id)
    ]

    # Still 7 rows, not 14.
    assert len(second) == 7
    # The replacement rows carry identical details and statuses.
    assert second == first


def test_validate_writes_audit_event(db_session: Session) -> None:
    """A passing run appends a VALIDATION_PASSED event from the system actor."""
    invoice = make_invoice(db_session, line_items=[passing_line_item()])

    run_validation(db_session, invoice_id=invoice.id)

    events = db_session.scalars(select(AuditEvent).where(AuditEvent.invoice_id == invoice.id)).all()
    assert len(events) == 1
    event = events[0]
    assert event.event_type == AuditEventType.VALIDATION_PASSED
    assert event.actor == AuditActor.SYSTEM
    assert event.data["overall_outcome"] == "passed"
    assert event.data["summary"]["failed"] == 0
    assert set(event.data["check_outcomes"]) == {
        check.__name__.removeprefix("check_") for check in ALL_CHECKS + ALL_DB_CHECKS
    }


def test_validate_does_not_overwrite_approved_status(db_session: Session) -> None:
    """APPROVED is terminal - re-validation must not downgrade it."""
    invoice = make_invoice(
        db_session,
        line_items=[passing_line_item()],
        status=InvoiceStatus.APPROVED,
    )

    result = run_validation(db_session, invoice_id=invoice.id)

    assert result.status == InvoiceStatus.APPROVED
    # The results are still persisted - only the status transition is skipped.
    assert len(get_validation_results(db_session, invoice.id)) == 7


def test_validate_skipped_outcome_leaves_status_unchanged(db_session: Session) -> None:
    """No monetary fields and no line items -> all checks skip, status unchanged."""
    invoice = make_invoice(
        db_session,
        subtotal=None,
        tax_amount=None,
        total_amount=None,
        currency=None,
        invoice_date=None,
        due_date=None,
        invoice_number=None,
        status=InvoiceStatus.INGESTED,
    )

    result = run_validation(db_session, invoice_id=invoice.id)

    assert result.status == InvoiceStatus.INGESTED
    rows = get_validation_results(db_session, invoice.id)
    assert len(rows) == 7
    assert all(r.status == AnalysisStatus.SKIPPED for r in rows)


def test_validate_missing_invoice_raises(db_session: Session) -> None:
    """A random UUID has no invoice to validate -> LookupError."""
    with pytest.raises(LookupError):
        run_validation(db_session, invoice_id=uuid.uuid4())


def test_get_validation_results_returns_ordered(db_session: Session) -> None:
    """Rows come back in insertion order, which matches ALL_CHECKS order."""
    invoice = make_invoice(db_session, line_items=[passing_line_item()])

    run_validation(db_session, invoice_id=invoice.id)
    rows = get_validation_results(db_session, invoice.id)

    assert len(rows) == 7
    assert [r.details["check_name"] for r in rows] == [
        check.__name__.removeprefix("check_") for check in ALL_CHECKS + ALL_DB_CHECKS
    ]


def test_duplicate_check_passes_when_unique(db_session: Session) -> None:
    invoice = make_invoice(db_session)
    run_validation(db_session, invoice_id=invoice.id)
    rows = get_validation_results(db_session, invoice.id)
    dup_row = [r for r in rows if r.details["rule_id"] == "DUPLICATE_INVOICE_NUMBER"][0]
    assert dup_row.status == AnalysisStatus.PASSED


def test_duplicate_check_fails_when_same_vendor_invoice_number(db_session: Session) -> None:
    make_invoice(db_session, vendor_name="Vendor A", invoice_number="123")
    i2 = make_invoice(db_session, vendor_name="Vendor A", invoice_number="123")
    run_validation(db_session, invoice_id=i2.id)
    rows = get_validation_results(db_session, i2.id)
    dup_row = [r for r in rows if r.details["rule_id"] == "DUPLICATE_INVOICE_NUMBER"][0]
    assert dup_row.status == AnalysisStatus.FAILED
    assert dup_row.details["duplicate_count"] == 1


def test_duplicate_check_ignores_different_vendor(db_session: Session) -> None:
    make_invoice(db_session, vendor_name="Vendor A", invoice_number="123")
    i2 = make_invoice(db_session, vendor_name="Vendor B", invoice_number="123")
    run_validation(db_session, invoice_id=i2.id)
    rows = get_validation_results(db_session, i2.id)
    dup_row = [r for r in rows if r.details["rule_id"] == "DUPLICATE_INVOICE_NUMBER"][0]
    assert dup_row.status == AnalysisStatus.PASSED


def test_duplicate_check_skipped_without_inputs(db_session: Session) -> None:
    invoice = make_invoice(db_session, invoice_number=None)
    run_validation(db_session, invoice_id=invoice.id)
    rows = get_validation_results(db_session, invoice.id)
    dup_row = [r for r in rows if r.details["rule_id"] == "DUPLICATE_INVOICE_NUMBER"][0]
    assert dup_row.status == AnalysisStatus.SKIPPED


def test_duplicate_check_case_insensitive_vendor(db_session: Session) -> None:
    make_invoice(db_session, vendor_name="ACME", invoice_number="123")
    i2 = make_invoice(db_session, vendor_name="acme", invoice_number="123")
    run_validation(db_session, invoice_id=i2.id)
    rows = get_validation_results(db_session, i2.id)
    dup_row = [r for r in rows if r.details["rule_id"] == "DUPLICATE_INVOICE_NUMBER"][0]
    assert dup_row.status == AnalysisStatus.FAILED
