import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models import (
    AuditActor,
    AuditEvent,
    AuditEventType,
    Document,
    Invoice,
    InvoiceStatus,
    LineItem,
)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def make_document(**overrides) -> Document:
    data = {
        "filename": "acme-2024-001.pdf",
        "content_type": "application/pdf",
        "size_bytes": 1024,
        "sha256": "a" * 64,
        "storage_path": "/storage/acme-2024-001.pdf",
    }
    data.update(overrides)
    return Document(**data)


def test_invoice_with_line_items_and_audit_event_roundtrip(db):
    """Insert an invoice with children, read it back, assert it round-trips.

    This test uses a transaction that is rolled back, so it does not
    leave rows behind in the database. The file-level metadata that used to
    live on the invoice now lives on the linked ``Document``.
    """
    invoice = Invoice(
        document=make_document(),
        vendor_name="Acme Corp",
        invoice_number="INV-2024-001",
        currency="USD",
        subtotal=Decimal("100.0000"),
        tax_amount=Decimal("8.5000"),
        total_amount=Decimal("108.5000"),
        status=InvoiceStatus.EXTRACTED,
        line_items=[
            LineItem(
                position=1,
                description="Widget",
                quantity=Decimal("10.0000"),
                unit_price=Decimal("10.0000"),
                line_total=Decimal("100.0000"),
            )
        ],
        audit_events=[
            AuditEvent(
                event_type=AuditEventType.INGESTED,
                actor=AuditActor.SYSTEM,
                message="Invoice ingested from acme-2024-001.pdf",
            )
        ],
    )

    db.add(invoice)
    db.flush()  # sends SQL but does not commit

    fetched = db.execute(select(Invoice).where(Invoice.id == invoice.id)).scalar_one()

    assert fetched.vendor_name == "Acme Corp"
    assert fetched.total_amount == Decimal("108.5000")
    assert fetched.document.filename == "acme-2024-001.pdf"
    assert len(fetched.line_items) == 1
    assert fetched.line_items[0].description == "Widget"
    assert len(fetched.audit_events) == 1
    assert fetched.audit_events[0].event_type == AuditEventType.INGESTED


def test_duplicate_sha256_can_be_inserted(db):
    """We deliberately do NOT enforce uniqueness on sha256 at the DB level.

    Duplicate detection is a *business decision* (maybe it's a legitimate
    re-send), not a hard constraint. Both documents should insert cleanly.
    """
    for i in range(2):
        db.add(make_document(filename=f"dup-{i}.pdf", sha256="b" * 64))
    db.flush()

    rows = (
        db.execute(select(Document).where(Document.sha256 == "b" * 64)).scalars().all()
    )
    assert len(rows) == 2


def test_uuid_pk_is_generated_client_side(db):
    document = make_document(sha256="c" * 64)
    db.add(document)
    db.flush()
    assert isinstance(document.id, uuid.UUID)
