"""Service-level tests for duplicate-check and anomaly-check persistence.

Exercises run_duplicate_check / run_anomaly_check and their getters directly
against the database via the shared savepoint fixture. Invoices are inserted by
hand so each test controls the exact vendor history under analysis.
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
from app.services.anomaly_service import get_anomaly_result, run_anomaly_check
from app.services.duplicate_service import get_duplicate_result, run_duplicate_check

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def make_document(**overrides) -> Document:
    data = {
        "filename": "invoice.pdf",
        "content_type": "application/pdf",
        "size_bytes": 2048,
        "sha256": uuid.uuid4().hex,
        "storage_path": "/storage/invoice.pdf",
    }
    data.update(overrides)
    return Document(**data)


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
    invoice = Invoice(
        document=make_document(**({"sha256": sha256} if sha256 else {})),
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


def line(description: str = "Widget", quantity: str = "2", price: str = "10.00") -> LineItem:
    return LineItem(
        position=1,
        description=description,
        quantity=Decimal(quantity),
        unit_price=Decimal(price),
        line_total=Decimal(quantity) * Decimal(price),
    )


def results_for(db: Session, invoice_id: uuid.UUID, analysis_type: AnalysisType) -> list:
    return list(
        db.scalars(
            select(AnalysisResult).where(
                AnalysisResult.invoice_id == invoice_id,
                AnalysisResult.analysis_type == analysis_type,
            )
        ).all()
    )


def events_for(db: Session, invoice_id: uuid.UUID) -> list[AuditEvent]:
    return list(
        db.scalars(select(AuditEvent).where(AuditEvent.invoice_id == invoice_id)).all()
    )


# --- duplicate check: unique ------------------------------------------------


def test_duplicate_check_unique_when_vendor_is_new(db_session: Session) -> None:
    """No same-vendor history means PASSED with no score."""
    invoice = make_invoice(db_session)

    result = run_duplicate_check(db_session, invoice_id=invoice.id)

    assert result.status == InvoiceStatus.INGESTED
    rows = results_for(db_session, invoice.id, AnalysisType.DUPLICATE_CHECK)
    assert len(rows) == 1
    assert rows[0].status == AnalysisStatus.PASSED
    assert rows[0].score is None
    assert rows[0].details["verdict"] == "unique"
    assert rows[0].details["top_match"] is None


def test_duplicate_check_missing_vendor_note(db_session: Session) -> None:
    """No vendor means nothing to compare against, and the report says so."""
    invoice = make_invoice(db_session, vendor=None)

    run_duplicate_check(db_session, invoice_id=invoice.id)

    row = results_for(db_session, invoice.id, AnalysisType.DUPLICATE_CHECK)[0]
    assert row.status == AnalysisStatus.PASSED
    assert row.details["notes"] == ["vendor name missing"]


def test_duplicate_check_ignores_other_vendors(db_session: Session) -> None:
    """A different vendor with an identical number is not a duplicate."""
    make_invoice(
        db_session,
        vendor="Globex",
        invoice_number="INV-0001",
        total=Decimal("100.00"),
    )
    invoice = make_invoice(db_session, vendor="Acme Corp", invoice_number="INV-0001")

    run_duplicate_check(db_session, invoice_id=invoice.id)

    row = results_for(db_session, invoice.id, AnalysisType.DUPLICATE_CHECK)[0]
    assert row.details["verdict"] == "unique"


# --- duplicate check: positive ----------------------------------------------


def test_duplicate_check_detects_same_vendor_and_number(db_session: Session) -> None:
    """Same vendor + invoice number is a DUPLICATE and routes to review."""
    make_invoice(db_session, vendor="Acme Corp", invoice_number="INV-0001")
    invoice = make_invoice(db_session, vendor="Acme Corp", invoice_number="INV-0001")

    result = run_duplicate_check(db_session, invoice_id=invoice.id)

    assert result.status == InvoiceStatus.NEEDS_REVIEW
    row = results_for(db_session, invoice.id, AnalysisType.DUPLICATE_CHECK)[0]
    assert row.status == AnalysisStatus.FAILED
    assert row.details["verdict"] == "duplicate"
    assert row.score is not None
    assert row.score > 0


def test_duplicate_check_writes_suspected_audit_event(db_session: Session) -> None:
    """A duplicate raises DUPLICATE_SUSPECTED, not a generic NOTE."""
    make_invoice(db_session, vendor="Acme Corp", invoice_number="INV-0001")
    invoice = make_invoice(db_session, vendor="Acme Corp", invoice_number="INV-0001")

    run_duplicate_check(db_session, invoice_id=invoice.id)

    events = events_for(db_session, invoice.id)
    assert len(events) == 1
    event = events[0]
    assert event.event_type == AuditEventType.DUPLICATE_SUSPECTED
    assert event.actor == AuditActor.SYSTEM
    assert "Duplicate verdict: duplicate" in event.message
    assert event.data["analysis"] == "duplicate_check"
    assert event.data["verdict"] == "duplicate"
    assert event.data["matching_invoice_id"] is not None
    assert "same_vendor_invoice_number" in event.data["signals"]


def test_duplicate_check_unique_writes_note_event(db_session: Session) -> None:
    """No match is routine enough not to raise a duplicate alarm."""
    invoice = make_invoice(db_session)

    run_duplicate_check(db_session, invoice_id=invoice.id)

    event = events_for(db_session, invoice.id)[0]
    assert event.event_type == AuditEventType.NOTE
    assert event.message == "No duplicate found"
    assert event.data["matching_invoice_id"] is None
    assert event.data["signals"] == []


def test_duplicate_check_suspected_does_not_change_status(db_session: Session) -> None:
    """SUSPECTED_DUPLICATE is a warning; it must not gate the pipeline.

    Same vendor, same amount, same PO (0.4 + 0.5 = 0.9) but no invoice-number
    match and deliberately dissimilar line items, so LINE_ITEM_OVERLAP stays
    off and the pair lands in SUSPECTED rather than DUPLICATE.
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

    row = results_for(db_session, invoice.id, AnalysisType.DUPLICATE_CHECK)[0]
    assert row.status == AnalysisStatus.WARNING
    assert row.details["verdict"] == "suspected_duplicate"
    # The key assertion: warning did not move the invoice.
    assert result.status == InvoiceStatus.INGESTED


@pytest.mark.parametrize(
    "terminal", [InvoiceStatus.APPROVED, InvoiceStatus.REJECTED, InvoiceStatus.FAILED]
)
def test_duplicate_check_respects_terminal_status(
    db_session: Session, terminal
) -> None:
    """A duplicate never downgrades a decision someone already made."""
    make_invoice(db_session, vendor="Acme Corp", invoice_number="INV-0001")
    invoice = make_invoice(
        db_session,
        vendor="Acme Corp",
        invoice_number="INV-0001",
        status=terminal,
    )

    result = run_duplicate_check(db_session, invoice_id=invoice.id)

    assert result.status == terminal
    # The finding is still recorded - only the transition is skipped.
    row = results_for(db_session, invoice.id, AnalysisType.DUPLICATE_CHECK)[0]
    assert row.status == AnalysisStatus.FAILED


def test_duplicate_check_is_idempotent(db_session: Session) -> None:
    """A second run replaces the first row rather than appending."""
    make_invoice(db_session, vendor="Acme Corp", invoice_number="INV-0001")
    invoice = make_invoice(db_session, vendor="Acme Corp", invoice_number="INV-0001")

    run_duplicate_check(db_session, invoice_id=invoice.id)
    first = dict(results_for(db_session, invoice.id, AnalysisType.DUPLICATE_CHECK)[0].details)
    run_duplicate_check(db_session, invoice_id=invoice.id)

    rows = results_for(db_session, invoice.id, AnalysisType.DUPLICATE_CHECK)
    assert len(rows) == 1
    assert dict(rows[0].details) == first


def test_duplicate_check_leaves_other_analysis_types(db_session: Session) -> None:
    """Re-running duplicate check must not touch another analysis type."""
    invoice = make_invoice(db_session)
    db_session.add(
        AnalysisResult(
            invoice_id=invoice.id,
            analysis_type=AnalysisType.PO_MATCH,
            status=AnalysisStatus.PASSED,
            score=None,
            details={"po_reference": "PO-1"},
        )
    )
    db_session.commit()

    run_duplicate_check(db_session, invoice_id=invoice.id)

    others = results_for(db_session, invoice.id, AnalysisType.PO_MATCH)
    assert len(others) == 1


def test_get_duplicate_result_round_trip(db_session: Session) -> None:
    """Before a run there is None; after, the persisted row comes back."""
    invoice = make_invoice(db_session)

    assert get_duplicate_result(db_session, invoice.id) is None

    run_duplicate_check(db_session, invoice_id=invoice.id)
    stored = get_duplicate_result(db_session, invoice.id)

    assert stored is not None
    assert stored.analysis_type == AnalysisType.DUPLICATE_CHECK


def test_duplicate_check_missing_invoice_raises(db_session: Session) -> None:
    """A random UUID has no invoice to check -> LookupError."""
    with pytest.raises(LookupError):
        run_duplicate_check(db_session, invoice_id=uuid.uuid4())


# --- anomaly check ----------------------------------------------------------


def test_anomaly_check_insufficient_history(db_session: Session) -> None:
    """A vendor with no history is SKIPPED, never scored."""
    invoice = make_invoice(db_session)

    result = run_anomaly_check(db_session, invoice_id=invoice.id)

    assert result.status == InvoiceStatus.INGESTED
    row = results_for(db_session, invoice.id, AnalysisType.ANOMALY)[0]
    assert row.status == AnalysisStatus.SKIPPED
    assert row.score is None
    assert row.details["verdict"] == "insufficient_history"
    assert row.details["method"] == "none"
    assert row.details["history_size"] == 0


def test_anomaly_check_missing_vendor_is_insufficient(db_session: Session) -> None:
    """Without a vendor there is no baseline, and we say so rather than guess."""
    invoice = make_invoice(db_session, vendor=None)

    run_anomaly_check(db_session, invoice_id=invoice.id)

    row = results_for(db_session, invoice.id, AnalysisType.ANOMALY)[0]
    assert row.status == AnalysisStatus.SKIPPED
    assert row.details["history_size"] == 0


def test_anomaly_check_normal_invoice_passes(db_session: Session) -> None:
    """An invoice inside the vendor's usual range is PASSED."""
    for offset in range(-4, 6):
        make_invoice(
            db_session,
            vendor="Acme Corp",
            invoice_number=f"INV-{offset}",
            total=Decimal(str(100 + offset)),
            created_at=BASE + timedelta(days=abs(offset)),
        )
    invoice = make_invoice(
        db_session, vendor="Acme Corp", invoice_number="INV-NEW", total=Decimal("100")
    )

    result = run_anomaly_check(db_session, invoice_id=invoice.id)

    assert result.status == InvoiceStatus.INGESTED
    row = results_for(db_session, invoice.id, AnalysisType.ANOMALY)[0]
    assert row.status == AnalysisStatus.PASSED
    assert row.details["verdict"] == "normal"


def test_anomaly_check_strong_anomaly_routes_to_review(db_session: Session) -> None:
    """A large deviation is FAILED and moves the invoice to NEEDS_REVIEW."""
    for offset in range(-4, 16):
        make_invoice(
            db_session,
            vendor="Acme Corp",
            invoice_number=f"INV-{offset}",
            total=Decimal(str(100 + offset)),
            created_at=BASE + timedelta(days=offset + 5),
        )
    invoice = make_invoice(
        db_session,
        vendor="Acme Corp",
        invoice_number="INV-HUGE",
        total=Decimal("5000.00"),
    )

    result = run_anomaly_check(db_session, invoice_id=invoice.id)

    assert result.status == InvoiceStatus.NEEDS_REVIEW
    row = results_for(db_session, invoice.id, AnalysisType.ANOMALY)[0]
    assert row.status == AnalysisStatus.FAILED
    assert row.details["verdict"] == "strong_anomaly"
    assert row.details["contributions"]
    assert row.score is not None


def test_anomaly_check_writes_audit_event(db_session: Session) -> None:
    """Each run appends a NOTE event describing the verdict."""
    invoice = make_invoice(db_session)

    run_anomaly_check(db_session, invoice_id=invoice.id)

    events = events_for(db_session, invoice.id)
    assert len(events) == 1
    event = events[0]
    assert event.event_type == AuditEventType.NOTE
    assert event.actor == AuditActor.SYSTEM
    assert "Anomaly verdict: insufficient_history via none" in event.message
    assert "(no score)" in event.message
    assert event.data["analysis"] == "anomaly"
    assert event.data["method"] == "none"
    assert event.data["contribution_count"] == 0


def test_anomaly_check_message_includes_score_when_present(db_session: Session) -> None:
    """A scored verdict names its score; an unscored one says so explicitly."""
    for offset in range(-4, 6):
        make_invoice(
            db_session,
            vendor="Acme Corp",
            invoice_number=f"INV-{offset}",
            total=Decimal(str(100 + offset)),
            created_at=BASE + timedelta(days=offset + 5),
        )
    invoice = make_invoice(
        db_session,
        vendor="Acme Corp",
        invoice_number="INV-BIG",
        total=Decimal("4000.00"),
    )

    run_anomaly_check(db_session, invoice_id=invoice.id)

    message = events_for(db_session, invoice.id)[0].message
    assert "Anomaly verdict: strong_anomaly" in message
    assert "(score " in message


@pytest.mark.parametrize(
    "terminal", [InvoiceStatus.APPROVED, InvoiceStatus.REJECTED, InvoiceStatus.FAILED]
)
def test_anomaly_check_respects_terminal_status(db_session: Session, terminal) -> None:
    """A strong anomaly never downgrades a terminal decision."""
    for offset in range(-4, 16):
        make_invoice(
            db_session,
            vendor="Acme Corp",
            invoice_number=f"INV-{offset}",
            total=Decimal(str(100 + offset)),
            created_at=BASE + timedelta(days=offset + 5),
        )
    invoice = make_invoice(
        db_session,
        vendor="Acme Corp",
        invoice_number="INV-BIG",
        total=Decimal("5000.00"),
        status=terminal,
    )

    result = run_anomaly_check(db_session, invoice_id=invoice.id)

    assert result.status == terminal
    row = results_for(db_session, invoice.id, AnalysisType.ANOMALY)[0]
    assert row.status == AnalysisStatus.FAILED


def test_anomaly_check_is_idempotent(db_session: Session) -> None:
    """A second run replaces the first row rather than appending."""
    invoice = make_invoice(db_session)

    run_anomaly_check(db_session, invoice_id=invoice.id)
    first = dict(results_for(db_session, invoice.id, AnalysisType.ANOMALY)[0].details)
    run_anomaly_check(db_session, invoice_id=invoice.id)

    rows = results_for(db_session, invoice.id, AnalysisType.ANOMALY)
    assert len(rows) == 1
    assert dict(rows[0].details) == first


def test_anomaly_check_leaves_other_analysis_types(db_session: Session) -> None:
    """Re-running anomaly check must not touch another analysis type."""
    invoice = make_invoice(db_session)
    db_session.add(
        AnalysisResult(
            invoice_id=invoice.id,
            analysis_type=AnalysisType.DUPLICATE_CHECK,
            status=AnalysisStatus.PASSED,
            score=None,
            details={"verdict": "unique"},
        )
    )
    db_session.commit()

    run_anomaly_check(db_session, invoice_id=invoice.id)

    assert len(results_for(db_session, invoice.id, AnalysisType.DUPLICATE_CHECK)) == 1


def test_get_anomaly_result_round_trip(db_session: Session) -> None:
    """Before a run there is None; after, the persisted row comes back."""
    invoice = make_invoice(db_session)

    assert get_anomaly_result(db_session, invoice.id) is None

    run_anomaly_check(db_session, invoice_id=invoice.id)
    stored = get_anomaly_result(db_session, invoice.id)

    assert stored is not None
    assert stored.analysis_type == AnalysisType.ANOMALY


def test_anomaly_check_missing_invoice_raises(db_session: Session) -> None:
    """A random UUID has no invoice to check -> LookupError."""
    with pytest.raises(LookupError):
        run_anomaly_check(db_session, invoice_id=uuid.uuid4())


def test_both_checks_coexist_on_one_invoice(db_session: Session) -> None:
    """Running both analyses leaves two independent rows."""
    invoice = make_invoice(db_session)

    run_duplicate_check(db_session, invoice_id=invoice.id)
    run_anomaly_check(db_session, invoice_id=invoice.id)

    assert len(results_for(db_session, invoice.id, AnalysisType.DUPLICATE_CHECK)) == 1
    assert len(results_for(db_session, invoice.id, AnalysisType.ANOMALY)) == 1
    # And two audit events, one per analysis.
    assert len(events_for(db_session, invoice.id)) == 2