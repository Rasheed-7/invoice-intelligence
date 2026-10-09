"""Foundation model tests.

Every test runs inside a transaction that is rolled back during teardown, so
the database is left exactly as it was found - the only rows ever committed are
those created by Alembic itself.
"""

from collections.abc import Generator
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.init import seed_system_user
from app.db.session import SessionLocal
from app.models import (
    AnalysisResult,
    AnalysisStatus,
    AnalysisType,
    AuditActor,
    AuditEvent,
    AuditEventType,
    Document,
    DocumentPage,
    DocumentProcessingStatus,
    Invoice,
    InvoiceStatus,
    LineItem,
    PolicyChunk,
    PolicyDocument,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
    ReviewCase,
    ReviewStatus,
    User,
)


@pytest.fixture
def db() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def make_document(**overrides) -> Document:
    data = {
        "filename": "invoice.pdf",
        "content_type": "application/pdf",
        "size_bytes": 2048,
        "sha256": "0" * 64,
        "storage_path": "/storage/invoice.pdf",
    }
    data.update(overrides)
    return Document(**data)


def make_invoice(db: Session, **overrides) -> Invoice:
    data = {
        "vendor_name": "Acme Corp",
        "invoice_number": "INV-0001",
        "currency": "USD",
        "subtotal": Decimal("100.0000"),
        "tax_amount": Decimal("8.5000"),
        "total_amount": Decimal("108.5000"),
    }
    data.update(overrides)
    invoice = Invoice(document=make_document(), **data)
    db.add(invoice)
    db.flush()
    return invoice


def test_seed_system_user_is_idempotent(db: Session) -> None:
    seed_system_user(db)
    seed_system_user(db)

    count = db.execute(
        select(func.count()).select_from(User).where(User.email == "system@local")
    ).scalar_one()
    assert count == 1


def test_document_roundtrip(db: Session) -> None:
    document = make_document(sha256="c" * 64)
    db.add(document)
    db.flush()

    fetched = db.execute(select(Document).where(Document.id == document.id)).scalar_one()
    assert fetched.filename == "invoice.pdf"
    assert fetched.content_type == "application/pdf"
    assert fetched.size_bytes == 2048
    assert fetched.sha256 == "c" * 64
    assert fetched.storage_path == "/storage/invoice.pdf"
    assert fetched.processing_status == DocumentProcessingStatus.UPLOADED
    assert fetched.extracted_text is None
    assert fetched.uploaded_at is not None
    assert fetched.updated_at is not None


def test_invoice_requires_document(db: Session) -> None:
    document = make_document()
    invoice = Invoice(
        document=document,
        vendor_name="Acme Corp",
        total_amount=Decimal("10.0000"),
    )
    db.add(invoice)
    db.flush()

    fetched = db.execute(select(Invoice).where(Invoice.id == invoice.id)).scalar_one()
    assert fetched.document.id == document.id
    assert fetched.document.sha256 == document.sha256
    assert fetched.status == InvoiceStatus.INGESTED

    # An invoice without a document must be rejected by the NOT NULL FK.
    db.add(Invoice(vendor_name="No Document Corp"))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_invoice_with_line_items_and_analysis(db: Session) -> None:
    invoice = make_invoice(db, total_amount=Decimal("108.5000"))
    invoice.line_items = [
        LineItem(
            position=1,
            description="Widget",
            quantity=Decimal("10.0000"),
            unit_price=Decimal("10.0000"),
            line_total=Decimal("100.0000"),
        ),
        LineItem(
            position=2,
            description="Gadget",
            quantity=Decimal("1.0000"),
            unit_price=Decimal("8.5000"),
            line_total=Decimal("8.5000"),
        ),
    ]
    invoice.analysis_results = [
        AnalysisResult(
            analysis_type=AnalysisType.CALCULATION_CHECK,
            status=AnalysisStatus.PASSED,
            score=Decimal("0.7500"),
        ),
        AnalysisResult(
            analysis_type=AnalysisType.PO_MATCH,
            status=AnalysisStatus.WARNING,
            score=Decimal("0.7500"),
        ),
        AnalysisResult(
            analysis_type=AnalysisType.DUPLICATE_CHECK,
            status=AnalysisStatus.SKIPPED,
            score=Decimal("0.7500"),
        ),
    ]
    db.flush()

    fetched = db.execute(select(Invoice).where(Invoice.id == invoice.id)).scalar_one()
    assert len(fetched.line_items) == 2
    assert len(fetched.analysis_results) == 3
    assert {result.analysis_type for result in fetched.analysis_results} == {
        AnalysisType.CALCULATION_CHECK,
        AnalysisType.PO_MATCH,
        AnalysisType.DUPLICATE_CHECK,
    }
    assert all(isinstance(result.score, Decimal) for result in fetched.analysis_results)
    assert all(result.score == Decimal("0.7500") for result in fetched.analysis_results)
    assert fetched.total_amount == Decimal("108.5000")
    assert type(fetched.total_amount) is Decimal
    assert type(fetched.line_items[0].line_total) is Decimal
    assert fetched.line_items[0].line_total == Decimal("100.0000")


def test_money_is_not_float(db: Session) -> None:
    subtotal = Decimal("0.1") + Decimal("0.2")
    invoice = make_invoice(db, subtotal=subtotal)

    fetched = db.execute(select(Invoice).where(Invoice.id == invoice.id)).scalar_one()
    assert fetched.subtotal == Decimal("0.3")
    assert type(fetched.subtotal) is Decimal


def test_policy_chunks_cascade(db: Session) -> None:
    policy = PolicyDocument(
        title="Travel Policy",
        source_filename="travel.pdf",
        sha256="d" * 64,
        storage_path="/storage/travel.pdf",
    )
    policy.chunks = [
        PolicyChunk(position=i, content=f"chunk {i}", page_number=1) for i in (1, 2, 3)
    ]
    db.add(policy)
    db.flush()
    policy_id = policy.id

    count = db.execute(
        select(func.count())
        .select_from(PolicyChunk)
        .where(PolicyChunk.policy_document_id == policy_id)
    ).scalar_one()
    assert count == 3

    db.delete(policy)
    db.flush()

    remaining = db.execute(
        select(func.count())
        .select_from(PolicyChunk)
        .where(PolicyChunk.policy_document_id == policy_id)
    ).scalar_one()
    assert remaining == 0


def test_review_case_unique_per_invoice(db: Session) -> None:
    invoice = make_invoice(db)
    db.add(
        ReviewCase(
            invoice=invoice,
            status=ReviewStatus.PENDING,
            reason="duplicate suspected",
        )
    )
    db.flush()

    db.add(
        ReviewCase(
            invoice_id=invoice.id,
            status=ReviewStatus.PENDING,
            reason="second case for the same invoice",
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_audit_event_actor_user(db: Session) -> None:
    user = User(email="reviewer@example.com", display_name="Reviewer")
    invoice = make_invoice(db)
    event = AuditEvent(
        invoice=invoice,
        event_type=AuditEventType.REVIEW_REQUESTED,
        actor=AuditActor.REVIEWER,
        actor_user=user,
        message="Invoice sent for review",
    )
    db.add(event)
    db.flush()

    fetched = db.execute(select(AuditEvent).where(AuditEvent.id == event.id)).scalar_one()
    assert fetched.actor == AuditActor.REVIEWER
    assert fetched.actor_user_id == user.id
    assert fetched.actor_user.id == user.id
    assert fetched.actor_user.email == "reviewer@example.com"


def test_purchase_order_with_items(db: Session) -> None:
    purchase_order = PurchaseOrder(
        po_number="PO-1001",
        vendor_name="Acme Corp",
        currency="USD",
        total_amount=Decimal("250.0000"),
        status=PurchaseOrderStatus.OPEN,
    )
    purchase_order.items = [
        PurchaseOrderItem(
            position=1,
            description="Widget",
            quantity=Decimal("10.0000"),
            unit_price=Decimal("10.0000"),
            line_total=Decimal("100.0000"),
            tax_rate=Decimal("0.1000"),
        ),
        PurchaseOrderItem(
            position=2,
            description="Gadget",
            quantity=Decimal("15.0000"),
            unit_price=Decimal("10.0000"),
            line_total=Decimal("150.0000"),
            tax_rate=Decimal("0.1000"),
        ),
    ]
    db.add(purchase_order)
    db.flush()

    fetched = db.execute(
        select(PurchaseOrder).where(PurchaseOrder.id == purchase_order.id)
    ).scalar_one()
    assert [item.position for item in fetched.items] == [1, 2]
    assert type(fetched.total_amount) is Decimal
    assert fetched.total_amount == Decimal("250.0000")
    assert type(fetched.items[0].line_total) is Decimal
    assert fetched.items[0].line_total == Decimal("100.0000")
    assert fetched.items[1].line_total == Decimal("150.0000")


def test_document_pages_cascade_and_uniqueness(db: Session) -> None:
    document = make_document(sha256="b" * 64)
    db.add(document)
    db.flush()
    document_id = document.id

    db.add_all(
        [
            DocumentPage(document_id=document_id, page_number=n, text=f"page {n}")
            for n in (1, 2, 3)
        ]
    )
    db.flush()

    pages = (
        db.execute(
            select(DocumentPage)
            .where(DocumentPage.document_id == document_id)
            .order_by(DocumentPage.page_number)
        )
        .scalars()
        .all()
    )
    assert len(pages) == 3
    assert [page.page_number for page in pages] == [1, 2, 3]

    # A duplicate (document_id, page_number) must violate
    # uq_document_pages_document_page. The SAVEPOINT keeps the failed insert
    # from poisoning the outer transaction needed for the cascade assertion.
    duplicate = DocumentPage(document_id=document_id, page_number=2, text="duplicate")
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(duplicate)
        db.flush()

    # There is deliberately no ORM relationship yet, so the database-level
    # ON DELETE CASCADE must remove the pages when the document goes away.
    db.delete(document)
    db.flush()

    remaining = (
        db.execute(
            select(func.count())
            .select_from(DocumentPage)
            .where(DocumentPage.document_id == document_id)
        )
        .scalar_one()
    )
    assert remaining == 0

    db.rollback()


def test_all_enums_lowercase_in_db(db: Session) -> None:
    labels = (
        db.execute(
            text(
                "SELECT enumlabel FROM pg_enum "
                "JOIN pg_type ON pg_type.oid = pg_enum.enumtypid "
                "WHERE pg_type.typname IN ("
                "'invoice_status', 'audit_event_type', 'audit_actor', "
                "'document_processing_status', 'purchase_order_status', "
                "'review_status', 'analysis_type', 'analysis_status')"
            )
        )
        .scalars()
        .all()
    )

    assert labels, "expected the foundation enum types to exist"
    assert all(label == label.lower() for label in labels)


