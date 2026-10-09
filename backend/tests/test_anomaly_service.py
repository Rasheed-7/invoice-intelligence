"""Service-level tests for anomaly-check persistence.

Covers the six required scenarios against a real database via the shared
savepoint fixture. Vendor history is inserted by hand so each test controls the
exact baseline the detector builds.
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

BASE = datetime(2026, 1, 1, tzinfo=UTC)
CURRENT_AT = BASE + timedelta(days=365)


def make_invoice(
    db: Session,
    *,
    vendor: str | None = "Acme Corp",
    invoice_number: str = "INV-0001",
    total: Decimal = Decimal("100.00"),
    created_at: datetime = BASE,
    **overrides,
) -> Invoice:
    """Insert a Document + Invoice with one line item directly in the DB."""
    invoice = Invoice(
        document=Document(
            filename="invoice.pdf",
            content_type="application/pdf",
            size_bytes=2048,
            sha256=uuid.uuid4().hex,
            storage_path="/storage/invoice.pdf",
        ),
        vendor_name=vendor,
        invoice_number=invoice_number,
        currency="USD",
        subtotal=total,
        tax_amount=Decimal("8.50"),
        total_amount=total,
        created_at=created_at,
        **overrides,
    )
    invoice.line_items = [
        LineItem(
            position=1,
            description="Widget",
            quantity=Decimal("1"),
            unit_price=Decimal("10.00"),
            line_total=total,
        )
    ]
    db.add(invoice)
    db.commit()
    return invoice


def make_history(db: Session, amounts: list[str], vendor: str = "Acme Corp") -> None:
    """Insert a clustered vendor history spread across the year."""
    for index, amount in enumerate(amounts):
        make_invoice(
            db,
            vendor=vendor,
            invoice_number=f"INV-H{index}",
            total=Decimal(amount),
            created_at=BASE + timedelta(days=index),
        )


def anomaly_rows(db: Session, invoice_id: uuid.UUID) -> list[AnalysisResult]:
    return list(
        db.scalars(
            select(AnalysisResult).where(
                AnalysisResult.invoice_id == invoice_id,
                AnalysisResult.analysis_type == AnalysisType.ANOMALY,
            )
        ).all()
    )


def events(db: Session, invoice_id: uuid.UUID) -> list[AuditEvent]:
    return list(
        db.scalars(select(AuditEvent).where(AuditEvent.invoice_id == invoice_id)).all()
    )


def test_anomaly_check_insufficient_history_is_skipped(db_session: Session) -> None:
    """A brand-new vendor has no baseline, and we say so rather than score."""
    invoice = make_invoice(db_session)

    result = run_anomaly_check(db_session, invoice_id=invoice.id)

    assert result.status == InvoiceStatus.INGESTED
    row = anomaly_rows(db_session, invoice.id)[0]
    assert row.status == AnalysisStatus.SKIPPED
    assert row.score is None
    assert row.details["verdict"] == "insufficient_history"
    assert row.details["method"] == "none"
    assert row.details["history_size"] == 0
    assert row.details["contributions"] == []

    event = events(db_session, invoice.id)[0]
    assert event.event_type == AuditEventType.NOTE
    assert event.actor == AuditActor.SYSTEM
    assert "Anomaly verdict: insufficient_history via none (no score)" in event.message


def test_anomaly_check_normal_invoice_passes(db_session: Session) -> None:
    """An invoice inside the vendor's usual range is unremarkable."""
    make_history(db_session, [str(100 + offset) for offset in range(-4, 6)])
    invoice = make_invoice(
        db_session,
        invoice_number="INV-NEW",
        total=Decimal("100.00"),
        created_at=CURRENT_AT,
    )

    result = run_anomaly_check(db_session, invoice_id=invoice.id)

    assert result.status == InvoiceStatus.INGESTED
    row = anomaly_rows(db_session, invoice.id)[0]
    assert row.status == AnalysisStatus.PASSED
    assert row.details["verdict"] == "normal"
    assert row.details["contributions"] == []


def test_anomaly_check_strong_anomaly_needs_review(db_session: Session) -> None:
    """Ten clustered invoices plus a 4-sigma outlier is FAILED and gates."""
    make_history(db_session, [str(100 + offset) for offset in range(-5, 5)])
    invoice = make_invoice(
        db_session,
        invoice_number="INV-OUTLIER",
        total=Decimal("400.00"),
        created_at=CURRENT_AT,
    )

    result = run_anomaly_check(db_session, invoice_id=invoice.id)

    assert result.status == InvoiceStatus.NEEDS_REVIEW
    row = anomaly_rows(db_session, invoice.id)[0]
    assert row.status == AnalysisStatus.FAILED
    assert row.details["verdict"] == "strong_anomaly"
    assert row.details["contributions"]
    assert row.details["history_size"] == 10


def test_anomaly_check_mild_does_not_change_status(db_session: Session) -> None:
    """A moderate deviation warns but must not gate the pipeline.

    History 96..115 gives mean 105.5 and population std 5.766, so 120 sits at
    roughly +2.5 sigma: past the mild threshold, short of the strong one.
    """
    make_history(db_session, [str(100 + offset) for offset in range(-4, 16)])
    invoice = make_invoice(
        db_session,
        invoice_number="INV-MILD",
        total=Decimal("120.00"),
        created_at=CURRENT_AT,
    )

    result = run_anomaly_check(db_session, invoice_id=invoice.id)

    row = anomaly_rows(db_session, invoice.id)[0]
    assert row.status == AnalysisStatus.WARNING
    assert row.details["verdict"] == "mild_anomaly"
    assert result.status == InvoiceStatus.INGESTED


def test_anomaly_check_is_idempotent(db_session: Session) -> None:
    """A second run replaces the first row rather than appending."""
    make_history(db_session, [str(100 + offset) for offset in range(-5, 5)])
    invoice = make_invoice(
        db_session,
        invoice_number="INV-OUTLIER",
        total=Decimal("400.00"),
        created_at=CURRENT_AT,
    )

    run_anomaly_check(db_session, invoice_id=invoice.id)
    first = dict(anomaly_rows(db_session, invoice.id)[0].details)
    run_anomaly_check(db_session, invoice_id=invoice.id)

    rows = anomaly_rows(db_session, invoice.id)
    assert len(rows) == 1
    assert dict(rows[0].details) == first


def test_anomaly_check_no_score_when_only_frequency(db_session: Session) -> None:
    """A submission burst has no baseline to deviate from, so no score.

    The invoice's amount sits inside the vendor's normal range, so only the
    frequency signal fires. That contribution carries severity but no z-score,
    so the report must carry score=None rather than a manufactured 0.0.
    """
    make_history(db_session, [str(100 + offset) for offset in range(-5, 5)])
    # 10 recent invoices inside the trailing 30-day window before `invoice`.
    for index in range(10):
        make_invoice(
            db_session,
            invoice_number=f"INV-R{index}",
            total=Decimal("100.00"),
            created_at=CURRENT_AT - timedelta(days=20 - index),
        )
    invoice = make_invoice(
        db_session,
        invoice_number="INV-BURST",
        total=Decimal("100.00"),
        created_at=CURRENT_AT,
    )

    result = run_anomaly_check(db_session, invoice_id=invoice.id)

    row = anomaly_rows(db_session, invoice.id)[0]
    assert row.status in (AnalysisStatus.WARNING, AnalysisStatus.FAILED)
    assert row.details["score"] is None
    frequency = [
        c for c in row.details["contributions"] if c["feature"] == "invoice_frequency"
    ]
    assert frequency, "the burst should produce a frequency contribution"
    # A burst is a red flag on its own.
    assert result.status == InvoiceStatus.NEEDS_REVIEW


def test_get_anomaly_result_returns_none_before_run(db_session: Session) -> None:
    """No run yet means no row, so the route can serve its neutral payload."""
    invoice = make_invoice(db_session)

    assert get_anomaly_result(db_session, invoice.id) is None


def test_anomaly_check_respects_terminal_status(db_session: Session) -> None:
    """APPROVED is a decision already made - a strong anomaly cannot undo it."""
    make_history(db_session, [str(100 + offset) for offset in range(-5, 5)])
    invoice = make_invoice(
        db_session,
        invoice_number="INV-OUTLIER",
        total=Decimal("400.00"),
        created_at=CURRENT_AT,
        status=InvoiceStatus.APPROVED,
    )

    result = run_anomaly_check(db_session, invoice_id=invoice.id)

    assert result.status == InvoiceStatus.APPROVED
    # The finding is still recorded - only the transition is skipped.
    assert anomaly_rows(db_session, invoice.id)[0].status == AnalysisStatus.FAILED


def test_anomaly_check_missing_invoice_raises(db_session: Session) -> None:
    """A random UUID has no invoice to check -> LookupError."""
    with pytest.raises(LookupError):
        run_anomaly_check(db_session, invoice_id=uuid.uuid4())