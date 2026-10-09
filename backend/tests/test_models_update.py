"""Regression tests for ORM UPDATE behaviour on timestamp columns.

These guard against reintroducing ``func.text("clock_timestamp()")``, which
rendered as ``updated_at = text('clock_timestamp()')`` - a bound string literal
that PostgreSQL rejects with ``DatatypeMismatch``. ``func.clock_timestamp()``
renders a real function call, so the ORM can still UPDATE these tables.

The three tests below cover the tables most likely to be UPDATEd later in the
pipeline: documents (extraction), invoices (status), review_cases (resolution).
"""

from collections.abc import Generator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models import Document, Invoice, InvoiceStatus, ReviewCase, ReviewStatus

PDF = {"content_type": "application/pdf", "size_bytes": 1024, "storage_path": "/storage/{}.pdf"}


@pytest.fixture
def db() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def make_document(sha256: str) -> Document:
    return Document(
        filename="probe.pdf",
        content_type=PDF["content_type"],
        size_bytes=PDF["size_bytes"],
        sha256=sha256,
        storage_path=PDF["storage_path"].format(sha256[:6]),
    )


def test_document_updated_at_advances_on_update(db: Session) -> None:
    """An UPDATE on ``documents`` must raise no error and move ``updated_at``.

    This is the direct regression test for the ``func.text(...)`` bug: the
    UPDATE used to fail outright with ``DatatypeMismatch``.
    """
    document = make_document("a" * 64)
    db.add(document)
    db.flush()

    created_before = document.uploaded_at
    updated_before = document.updated_at

    # Guarantee the clock moves so a changed timestamp is unambiguous.
    db.execute(text("SELECT pg_sleep(0.05)"))

    document.extracted_text = "some extracted text"
    db.flush()
    db.refresh(document)

    assert document.extracted_text == "some extracted text"
    assert document.updated_at > document.uploaded_at
    assert document.updated_at > updated_before
    assert document.uploaded_at == created_before


def test_invoice_status_update_persists(db: Session) -> None:
    """An ORM UPDATE on ``invoices`` round-trips the new status."""
    invoice = Invoice(
        document=make_document("b" * 64),
        status=InvoiceStatus.INGESTED,
    )
    db.add(invoice)
    db.flush()
    assert invoice.status == InvoiceStatus.INGESTED

    invoice.status = InvoiceStatus.EXTRACTED
    db.flush()
    db.refresh(invoice)

    assert invoice.status == InvoiceStatus.EXTRACTED


def test_review_case_resolution_updates_timestamp(db: Session) -> None:
    """Resolving a review case persists status/resolved_at and moves updated_at."""
    invoice = Invoice(document=make_document("c" * 64))
    db.add(invoice)
    db.flush()

    case = ReviewCase(invoice=invoice, status=ReviewStatus.PENDING, reason="subtotal mismatch")
    db.add(case)
    db.flush()

    created_before = case.created_at
    updated_before = case.updated_at

    db.execute(text("SELECT pg_sleep(0.05)"))

    resolved_at = datetime.now(UTC)
    case.status = ReviewStatus.RESOLVED
    case.resolved_at = resolved_at
    db.flush()
    db.refresh(case)

    assert case.status == ReviewStatus.RESOLVED
    assert case.resolved_at is not None
    assert case.updated_at > updated_before
    assert case.updated_at > created_before