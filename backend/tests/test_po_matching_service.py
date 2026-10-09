# Service-level tests for PO matching persistence.
# These exercise run_po_matching directly against the database (via the shared
# db_session savepoint fixture) with no HTTP involved. Invoices and purchase
# orders are inserted by hand so each test controls the exact state under
# matching.

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
from app.models.purchase_order import PurchaseOrder, PurchaseOrderItem
from app.services.po_matching_service import run_po_matching


def make_document(**overrides) -> Document:
    data = {
        "filename": "acme.pdf",
        "content_type": "application/pdf",
        "size_bytes": 2048,
        "sha256": "0" * 64,
        "storage_path": "/storage/acme.pdf",
    }
    data.update(overrides)
    return Document(**data)


def make_invoice(
    db: Session,
    *,
    line_items: list[LineItem] | None = None,
    po_reference: str | None = None,
    **overrides,
) -> Invoice:
    """Insert a Document + Invoice (+ optional line items) directly in the DB.

    ``po_reference`` lands in the invoice's ``po_number`` column, which is
    where the extraction stage puts it and where the service reads it.
    """
    data = {
        "vendor_name": "Acme Corp",
        "invoice_number": "INV-0001",
        "currency": "USD",
        "invoice_date": date(2026, 1, 15),
        "due_date": date(2026, 2, 15),
        "subtotal": Decimal("100.00"),
        "tax_amount": Decimal("8.50"),
        "total_amount": Decimal("108.50"),
        "po_number": po_reference,
    }
    data.update(overrides)
    invoice = Invoice(document=make_document(), **data)
    if line_items is not None:
        invoice.line_items = line_items
    db.add(invoice)
    # Commit, not just flush: run_po_matching re-loads the invoice, and an
    # uncommitted object would be read with in-memory Decimal scales while a
    # second run reads DB Numeric(14,4) scales - same value, different strings.
    db.commit()
    return invoice


def make_po(
    db: Session,
    *,
    items: list[PurchaseOrderItem] | None = None,
    po_number: str = "PO-1",
    **overrides,
) -> PurchaseOrder:
    data = {
        "po_number": po_number,
        "vendor_name": "Acme Corp",
        "total_amount": Decimal("108.50"),
    }
    data.update(overrides)
    po = PurchaseOrder(**data)
    if items is not None:
        po.items = items
    db.add(po)
    db.commit()
    return po


def po_line_item(quantity: str) -> LineItem:
    """An invoice line of the given quantity; line_total stays 100.00."""
    return LineItem(
        position=1,
        description="Widget",
        quantity=Decimal(quantity),
        unit_price=Decimal("100.00"),
        line_total=Decimal("100.00"),
    )


def po_line(quantity: str) -> PurchaseOrderItem:
    """A PO line of the given quantity, comparable to po_line_item."""
    return PurchaseOrderItem(
        position=1,
        description="Widget",
        quantity=Decimal(quantity),
        unit_price=Decimal("100.00"),
        line_total=Decimal("100.00"),
    )


def po_line_at(position: int, description: str, line_total: str) -> PurchaseOrderItem:
    """A PO line at an explicit position/description/total."""
    return PurchaseOrderItem(
        position=position,
        description=description,
        quantity=Decimal("1"),
        unit_price=Decimal("100.00"),
        line_total=Decimal(line_total),
    )


def invoice_line_at(position: int, description: str, line_total: str) -> LineItem:
    """An invoice line at an explicit position/description/total."""
    return LineItem(
        position=position,
        description=description,
        quantity=Decimal("1"),
        unit_price=Decimal("100.00"),
        line_total=Decimal(line_total),
    )


def po_match_rows(db: Session, invoice_id: uuid.UUID) -> list[AnalysisResult]:
    """Every PO_MATCH row currently stored for the invoice."""
    return list(
        db.scalars(
            select(AnalysisResult).where(
                AnalysisResult.invoice_id == invoice_id,
                AnalysisResult.analysis_type == AnalysisType.PO_MATCH,
            )
        ).all()
    )


def test_match_no_po_reference_is_skipped(db_session: Session) -> None:
    """po_number is NULL -> SKIPPED and the invoice status is untouched."""
    invoice = make_invoice(db_session, line_items=[po_line_item("1")])

    result = run_po_matching(db_session, invoice_id=invoice.id)

    assert result.status == InvoiceStatus.INGESTED
    rows = po_match_rows(db_session, invoice.id)
    assert len(rows) == 1
    assert rows[0].status == AnalysisStatus.SKIPPED
    details = rows[0].details
    assert details is not None
    assert details["po_reference"] is None
    assert details["po_found"] is False
    assert details["overall_outcome"] == "skipped"
    assert details["notes"] == ["no PO reference on invoice"]


def test_match_with_known_po_passes(db_session: Session) -> None:
    """Matching line items -> PASSED and the invoice status is untouched."""
    po = make_po(db_session, items=[po_line("10")])
    invoice = make_invoice(
        db_session, line_items=[po_line_item("10")], po_reference=po.po_number
    )

    result = run_po_matching(db_session, invoice_id=invoice.id)

    assert result.status == InvoiceStatus.INGESTED
    rows = po_match_rows(db_session, invoice.id)
    assert len(rows) == 1
    assert rows[0].status == AnalysisStatus.PASSED
    details = rows[0].details
    assert details is not None
    assert details["po_found"] is True
    assert details["po_reference"] == po.po_number
    assert details["overall_outcome"] == "match"


def test_match_with_qty_mismatch_needs_review(db_session: Session) -> None:
    """PO says 10, invoice says 12 -> FAILED and NEEDS_REVIEW."""
    po = make_po(db_session, items=[po_line("10")])
    invoice = make_invoice(
        db_session, line_items=[po_line_item("12")], po_reference=po.po_number
    )

    result = run_po_matching(db_session, invoice_id=invoice.id)

    assert result.status == InvoiceStatus.NEEDS_REVIEW
    rows = po_match_rows(db_session, invoice.id)
    assert len(rows) == 1
    assert rows[0].status == AnalysisStatus.FAILED
    details = rows[0].details
    assert details is not None
    # Quantity is a HIGH-severity mismatch, so it drives the whole outcome.
    assert details["overall_outcome"] == "mismatch"
    assert details["mismatch_summary"] == {"quantity": 1}


def test_match_with_unknown_po_number_needs_review(db_session: Session) -> None:
    """A reference with no matching PO row -> FAILED and NEEDS_REVIEW."""
    invoice = make_invoice(
        db_session, line_items=[po_line_item("1")], po_reference="PO-999"
    )

    result = run_po_matching(db_session, invoice_id=invoice.id)

    assert result.status == InvoiceStatus.NEEDS_REVIEW
    rows = po_match_rows(db_session, invoice.id)
    assert len(rows) == 1
    assert rows[0].status == AnalysisStatus.FAILED
    details = rows[0].details
    assert details is not None
    assert details["po_found"] is False
    assert details["po_reference"] == "PO-999"
    assert details["overall_outcome"] == "mismatch"


def test_match_is_idempotent(db_session: Session) -> None:
    """A second run replaces the first row rather than appending."""
    po = make_po(db_session, items=[po_line("10")])
    invoice = make_invoice(
        db_session, line_items=[po_line_item("10")], po_reference=po.po_number
    )

    run_po_matching(db_session, invoice_id=invoice.id)
    first = [dict(row.details) for row in po_match_rows(db_session, invoice.id)]

    run_po_matching(db_session, invoice_id=invoice.id)
    second = [dict(row.details) for row in po_match_rows(db_session, invoice.id)]

    assert len(first) == 1
    assert second == first


def test_match_respects_terminal_status(db_session: Session) -> None:
    """APPROVED is a decision already made - matching must not downgrade it."""
    po = make_po(db_session, items=[po_line("10")])
    invoice = make_invoice(
        db_session,
        line_items=[po_line_item("10")],
        po_reference=po.po_number,
        status=InvoiceStatus.APPROVED,
    )

    result = run_po_matching(db_session, invoice_id=invoice.id)

    assert result.status == InvoiceStatus.APPROVED
    # The result is still persisted - only the status transition is skipped.
    assert len(po_match_rows(db_session, invoice.id)) == 1


# --- blank / absent po_number -------------------------------------------------


@pytest.mark.parametrize(
    "po_number",
    [None, "", "   ", "\t\n"],
    ids=["null", "empty", "whitespace", "control-chars"],
)
def test_blank_po_number_is_treated_as_no_reference(
    db_session: Session, po_number
) -> None:
    """A null/blank po_number must degrade to SKIPPED, not look up a PO named ""."""
    invoice = make_invoice(
        db_session, line_items=[po_line_item("1")], po_reference=po_number
    )

    result = run_po_matching(db_session, invoice_id=invoice.id)

    assert result.status == InvoiceStatus.INGESTED
    rows = po_match_rows(db_session, invoice.id)
    assert len(rows) == 1
    assert rows[0].status == AnalysisStatus.SKIPPED
    details = rows[0].details
    assert details is not None
    assert details["po_reference"] is None
    assert details["po_found"] is False
    assert details["overall_outcome"] == "skipped"
    assert details["notes"] == ["no PO reference on invoice"]


def test_po_number_is_stripped_before_lookup(db_session: Session) -> None:
    """A padded reference still finds its PO rather than missing on whitespace."""
    po = make_po(db_session, items=[po_line("10")])
    invoice = make_invoice(
        db_session,
        line_items=[po_line_item("10")],
        po_reference=f"  {po.po_number}  ",
    )

    run_po_matching(db_session, invoice_id=invoice.id)

    rows = po_match_rows(db_session, invoice.id)
    assert rows[0].status == AnalysisStatus.PASSED
    assert rows[0].details["po_found"] is True
    assert rows[0].details["po_reference"] == po.po_number


def test_extraction_raw_is_no_longer_the_source_of_truth(db_session: Session) -> None:
    """A PO number sitting only in extraction_raw is ignored.

    The reference was promoted to the po_number column; extraction_raw is a
    free-form debug artifact that /analyze overwrites, so it must not drive
    matching.
    """
    invoice = make_invoice(
        db_session,
        line_items=[po_line_item("1")],
        po_reference=None,
        extraction_raw={"purchase_order": {"number": "PO-SHOULD-BE-IGNORED"}},
    )

    run_po_matching(db_session, invoice_id=invoice.id)

    rows = po_match_rows(db_session, invoice.id)
    assert rows[0].status == AnalysisStatus.SKIPPED
    assert rows[0].details["po_reference"] is None


def test_extraction_raw_with_usable_reference_is_not_skipped(
    db_session: Session,
) -> None:
    """The one shape that must not degrade: a non-empty po_number."""
    po = make_po(db_session, items=[po_line("10")])
    invoice = make_invoice(
        db_session,
        line_items=[po_line_item("10")],
        po_reference=po.po_number,
    )

    run_po_matching(db_session, invoice_id=invoice.id)

    rows = po_match_rows(db_session, invoice.id)
    assert rows[0].status == AnalysisStatus.PASSED
    assert rows[0].details["po_reference"] == po.po_number


# --- partial / vendor / total -----------------------------------------------


def test_partial_match_two_lines_one_mismatched(db_session: Session) -> None:
    """2 invoice lines vs 2 PO lines, one disagreeing -> PARTIAL_MATCH/WARNING.

    The disagreeing line differs only in line_total (MEDIUM severity), so the
    line itself is a partial match and the overall outcome is a partial match.
    """
    po = make_po(
        db_session,
        items=[
            po_line_at(1, "Widget", "100.00"),
            po_line_at(2, "Gadget", "100.00"),
        ],
    )
    invoice = make_invoice(
        db_session,
        line_items=[
            invoice_line_at(1, "Widget", "100.00"),
            invoice_line_at(2, "Gadget", "99.50"),
        ],
        po_reference=po.po_number,
    )

    result = run_po_matching(db_session, invoice_id=invoice.id)

    # PARTIAL_MATCH is a warning, not a failure - status is left alone.
    assert result.status == InvoiceStatus.INGESTED
    rows = po_match_rows(db_session, invoice.id)
    assert len(rows) == 1
    assert rows[0].status == AnalysisStatus.WARNING
    details = rows[0].details
    assert details is not None
    assert details["overall_outcome"] == "partial_match"
    assert details["mismatch_summary"] == {"line_total": 1}
    assert details["unmatched_invoice_items"] == []
    assert details["unmatched_po_items"] == []
    assert [m["outcome"] for m in details["line_matches"]] == ["match", "partial_match"]


def test_vendor_mismatch_is_mismatch(db_session: Session) -> None:
    """Vendor disagreement alone -> MISMATCH and NEEDS_REVIEW."""
    po = make_po(db_session, items=[po_line("10")], vendor_name="Globex")
    invoice = make_invoice(
        db_session, line_items=[po_line_item("10")], po_reference=po.po_number
    )

    result = run_po_matching(db_session, invoice_id=invoice.id)

    assert result.status == InvoiceStatus.NEEDS_REVIEW
    rows = po_match_rows(db_session, invoice.id)
    assert len(rows) == 1
    assert rows[0].status == AnalysisStatus.FAILED
    details = rows[0].details
    assert details is not None
    assert details["overall_outcome"] == "mismatch"
    assert details["vendor_matches"] is False
    # Lines and total agree, so the vendor is the only reason for the mismatch.
    assert details["mismatch_summary"] == {}
    assert details["total_matches"] is True


def test_total_mismatch_is_mismatch(db_session: Session) -> None:
    """A total disagreement -> MISMATCH and NEEDS_REVIEW."""
    po = make_po(db_session, items=[po_line("10")], total_amount=Decimal("200.00"))
    invoice = make_invoice(
        db_session, line_items=[po_line_item("10")], po_reference=po.po_number
    )

    result = run_po_matching(db_session, invoice_id=invoice.id)

    assert result.status == InvoiceStatus.NEEDS_REVIEW
    rows = po_match_rows(db_session, invoice.id)
    assert len(rows) == 1
    assert rows[0].status == AnalysisStatus.FAILED
    details = rows[0].details
    assert details is not None
    assert details["overall_outcome"] == "mismatch"
    assert details["total_matches"] is False
    # Lines and vendor agree, so the total is the only reason.
    assert details["mismatch_summary"] == {}


@pytest.mark.parametrize(
    "terminal", [InvoiceStatus.APPROVED, InvoiceStatus.REJECTED, InvoiceStatus.FAILED]
)
def test_match_never_downgrades_any_terminal_status(
    db_session: Session, terminal
) -> None:
    """APPROVED/REJECTED/FAILED are decisions already made - never downgraded.

    Matching here produces a MISMATCH, which is exactly the case that would
    otherwise move a non-terminal invoice to NEEDS_REVIEW.
    """
    po = make_po(db_session, items=[po_line("10")])
    invoice = make_invoice(
        db_session,
        line_items=[po_line_item("12")],
        po_reference=po.po_number,
        status=terminal,
    )

    result = run_po_matching(db_session, invoice_id=invoice.id)

    assert result.status == terminal
    # The mismatch is still recorded - only the transition is skipped.
    rows = po_match_rows(db_session, invoice.id)
    assert len(rows) == 1
    assert rows[0].status == AnalysisStatus.FAILED
    assert rows[0].details["overall_outcome"] == "mismatch"


def test_match_does_not_delete_other_analysis_types(db_session: Session) -> None:
    """Re-running PO matching leaves calculation_check rows alone."""
    invoice = make_invoice(db_session, line_items=[po_line_item("1")])
    db_session.add(
        AnalysisResult(
            invoice_id=invoice.id,
            analysis_type=AnalysisType.CALCULATION_CHECK,
            status=AnalysisStatus.PASSED,
            score=None,
            details={"check_name": "subtotal_plus_tax"},
        )
    )
    db_session.commit()

    run_po_matching(db_session, invoice_id=invoice.id)

    others = list(
        db_session.scalars(
            select(AnalysisResult).where(
                AnalysisResult.invoice_id == invoice.id,
                AnalysisResult.analysis_type == AnalysisType.CALCULATION_CHECK,
            )
        ).all()
    )
    assert len(others) == 1
    assert len(po_match_rows(db_session, invoice.id)) == 1


def test_match_writes_note_audit_event_per_run(db_session: Session) -> None:
    """Each run appends one NOTE event from the system actor with the payload."""
    po = make_po(db_session, items=[po_line("10")])
    invoice = make_invoice(
        db_session, line_items=[po_line_item("10")], po_reference=po.po_number
    )

    run_po_matching(db_session, invoice_id=invoice.id)
    run_po_matching(db_session, invoice_id=invoice.id)

    events = list(
        db_session.scalars(
            select(AuditEvent).where(AuditEvent.invoice_id == invoice.id)
        ).all()
    )
    assert len(events) == 2
    for event in events:
        # NOTE is reused deliberately: there is no PO_MATCH event type yet.
        assert event.event_type == AuditEventType.NOTE
        assert event.actor == AuditActor.SYSTEM
        assert event.data["analysis"] == "po_match"
        assert event.data["overall_outcome"] == "match"
        assert event.data["po_reference"] == po.po_number
        assert event.data["po_found"] is True
        assert event.data["mismatch_summary"] == {}


def test_match_missing_invoice_raises(db_session: Session) -> None:
    """A random UUID has no invoice to match -> LookupError.

    Not in the requested list, but it is the error path the POST route maps to
    404, so leaving it uncovered would let that mapping regress silently.
    """
    with pytest.raises(LookupError):
        run_po_matching(db_session, invoice_id=uuid.uuid4())