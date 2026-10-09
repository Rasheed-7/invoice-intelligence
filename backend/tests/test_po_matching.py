"""Deterministic PO matching tests — in-memory ORM objects only, no DB, no LLM."""

import uuid
from decimal import Decimal

from app.models.invoice import Invoice, LineItem
from app.models.purchase_order import PurchaseOrder, PurchaseOrderItem
from app.services.po_matching.matcher import (
    build_match_report,
    match_line_items,
    match_totals,
    match_vendor,
)
from app.services.po_matching.normalize import descriptions_match, normalize_description
from app.services.po_matching.types import MatchOutcome, MismatchSeverity


def make_line(
    position: int,
    description: str | None,
    quantity: Decimal | None = None,
    unit_price: Decimal | None = None,
    line_total: Decimal | None = None,
) -> LineItem:
    # ids are normally assigned by the DB on flush; set explicitly here so
    # in-memory objects are complete without touching a database.
    return LineItem(
        id=uuid.uuid4(),
        position=position,
        description=description,
        quantity=quantity,
        unit_price=unit_price,
        line_total=line_total,
    )


def make_po_item(
    position: int,
    description: str | None,
    quantity: Decimal | None = None,
    unit_price: Decimal | None = None,
    line_total: Decimal | None = None,
) -> PurchaseOrderItem:
    return PurchaseOrderItem(
        id=uuid.uuid4(),
        position=position,
        description=description,
        quantity=quantity,
        unit_price=unit_price,
        line_total=line_total,
    )


def make_invoice(
    line_items: list[LineItem],
    vendor_name: str | None = None,
    total_amount: Decimal | None = None,
) -> Invoice:
    return Invoice(
        id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        vendor_name=vendor_name,
        total_amount=total_amount,
        line_items=line_items,
    )


def make_po(
    items: list[PurchaseOrderItem],
    vendor_name: str | None = None,
    total_amount: Decimal | None = None,
    po_number: str = "PO-1",
) -> PurchaseOrder:
    return PurchaseOrder(
        id=uuid.uuid4(),
        po_number=po_number,
        vendor_name=vendor_name,
        total_amount=total_amount,
        items=items,
    )


# --- normalize ---------------------------------------------------------------


def test_normalize_description_lowercase_whitespace_punctuation() -> None:
    assert normalize_description("  Steel,   Sheet - Grade A_1; ") == "steel sheet grade a 1"
    assert normalize_description("v2.0") == "v2 0"


def test_normalize_description_removes_noise_tokens_and_handles_none() -> None:
    assert normalize_description("10 pcs Hammer x") == "10 hammer"
    assert normalize_description("Widget unit") == "widget"
    assert normalize_description(None) == ""


# --- descriptions_match ------------------------------------------------------


def test_descriptions_match_identical_and_minor_typo() -> None:
    assert descriptions_match("Stainless Steel Bracket", "stainless   steel, bracket")
    assert descriptions_match("stainless steel bracket", "stainless steeel bracket")


def test_descriptions_match_below_threshold() -> None:
    assert not descriptions_match("Hammer", "Saw Blade")
    # Same prefix, but 0.897 < threshold 0.99 -> rejected.
    assert not descriptions_match("steel bracket", "steel bracket v2", threshold=0.99)


def test_descriptions_match_none_handling() -> None:
    assert descriptions_match(None, None)
    assert not descriptions_match(None, "hammer")
    assert not descriptions_match("hammer", None)


# --- matching ---------------------------------------------------------------


def test_match_line_items_exact_match() -> None:
    invoice = make_invoice(
        [make_line(1, "Hammer", Decimal("2"), Decimal("10.00"), Decimal("20.00"))],
        vendor_name="Acme Corp",
        total_amount=Decimal("20.00"),
    )
    po = make_po(
        [make_po_item(1, "Hammer", Decimal("2"), Decimal("10.00"), Decimal("20.00"))],
        vendor_name="ACME CORP",
        total_amount=Decimal("20.00"),
    )

    report = build_match_report(invoice, po, "PO-1")

    assert report.po_found is True
    assert report.overall_outcome == MatchOutcome.MATCH
    assert len(report.line_matches) == 1
    assert report.line_matches[0].outcome == MatchOutcome.MATCH
    assert report.line_matches[0].mismatches == []
    assert report.line_matches[0].invoice_item_id == invoice.line_items[0].id
    assert report.line_matches[0].po_item_id == po.items[0].id
    assert report.unmatched_invoice_items == []
    assert report.unmatched_po_items == []
    assert report.mismatch_summary == {}
    assert report.vendor_matches is True
    assert report.total_matches is True
    assert report.notes == []


def test_match_line_items_quantity_mismatch_is_high() -> None:
    invoice = make_invoice([make_line(1, "Hammer", quantity=Decimal("10.00"))])
    po = make_po([make_po_item(1, "Hammer", quantity=Decimal("5.00"))])

    matches, unmatched_invoice, unmatched_po = match_line_items(
        invoice.line_items, po.items
    )

    assert len(matches) == 1
    assert matches[0].outcome == MatchOutcome.MISMATCH
    assert len(matches[0].mismatches) == 1
    mismatch = matches[0].mismatches[0]
    assert mismatch.field == "quantity"
    assert mismatch.severity == MismatchSeverity.HIGH
    assert mismatch.invoice_value == "10.00"
    assert mismatch.po_value == "5.00"
    assert mismatch.difference is not None
    assert unmatched_invoice == []
    assert unmatched_po == []


def test_match_line_items_unit_price_mismatch_is_high() -> None:
    invoice = make_invoice(
        [make_line(1, "Hammer", quantity=Decimal("1"), unit_price=Decimal("12.50"))]
    )
    po = make_po(
        [make_po_item(1, "Hammer", quantity=Decimal("1"), unit_price=Decimal("10.00"))]
    )

    matches, _, _ = match_line_items(invoice.line_items, po.items)

    assert matches[0].outcome == MatchOutcome.MISMATCH
    assert len(matches[0].mismatches) == 1
    assert matches[0].mismatches[0].field == "unit_price"
    assert matches[0].mismatches[0].severity == MismatchSeverity.HIGH


def test_match_multiple_lines_partial() -> None:
    invoice = make_invoice(
        [
            make_line(1, "Steel Plate", Decimal("2"), Decimal("10.00"), Decimal("19.50")),
            make_line(2, "Copper Wire", Decimal("1"), Decimal("5.00"), Decimal("5.00")),
        ]
    )
    po = make_po(
        [
            make_po_item(1, "Steel Plate", Decimal("2"), Decimal("10.00"), Decimal("20.00")),
            make_po_item(2, "Copper Wire", Decimal("1"), Decimal("5.00"), Decimal("5.00")),
        ]
    )

    report = build_match_report(invoice, po, "PO-1")

    assert [m.outcome for m in report.line_matches] == [
        MatchOutcome.PARTIAL_MATCH,
        MatchOutcome.MATCH,
    ]
    assert report.line_matches[0].mismatches[0].field == "line_total"
    assert report.line_matches[0].mismatches[0].severity == MismatchSeverity.MEDIUM
    assert report.overall_outcome == MatchOutcome.PARTIAL_MATCH
    assert report.mismatch_summary == {"line_total": 1}


def test_match_unmatched_invoice_line() -> None:
    hammer = make_line(1, "Hammer", Decimal("1"), Decimal("10.00"), Decimal("10.00"))
    orphan = make_line(2, "Circular Saw", Decimal("1"), Decimal("50.00"), Decimal("50.00"))
    invoice = make_invoice([hammer, orphan])
    po = make_po([make_po_item(1, "Hammer", Decimal("1"), Decimal("10.00"), Decimal("10.00"))])

    report = build_match_report(invoice, po, "PO-1")

    assert report.unmatched_invoice_items == [orphan.id]
    assert report.unmatched_po_items == []
    assert len(report.line_matches) == 1
    assert report.overall_outcome == MatchOutcome.PARTIAL_MATCH


def test_match_unmatched_po_line() -> None:
    invoice = make_invoice(
        [make_line(1, "Hammer", Decimal("1"), Decimal("10.00"), Decimal("10.00"))]
    )
    orphan_po = make_po_item(2, "Circular Saw", Decimal("1"), Decimal("50.00"), Decimal("50.00"))
    po = make_po(
        [make_po_item(1, "Hammer", Decimal("1"), Decimal("10.00"), Decimal("10.00")), orphan_po]
    )

    report = build_match_report(invoice, po, "PO-1")

    assert report.unmatched_po_items == [orphan_po.id]
    assert report.unmatched_invoice_items == []
    assert len(report.line_matches) == 1
    assert report.overall_outcome == MatchOutcome.PARTIAL_MATCH


def test_build_match_report_missing_po_reference_is_skipped() -> None:
    report = build_match_report(make_invoice([]), None, None)

    assert report.overall_outcome == MatchOutcome.SKIPPED
    assert report.po_found is False
    assert report.po_reference is None
    assert report.notes == ["no PO reference on invoice"]
    assert report.line_matches == []
    assert report.vendor_matches is None
    assert report.total_matches is None


def test_build_match_report_po_reference_not_found() -> None:
    report = build_match_report(make_invoice([]), None, "PO-404")

    assert report.overall_outcome == MatchOutcome.MISMATCH
    assert report.po_found is False
    assert report.po_reference == "PO-404"
    assert report.notes == ["PO PO-404 not found"]
    assert report.mismatch_summary == {}


def test_build_match_report_vendor_mismatch() -> None:
    invoice = make_invoice(
        [make_line(1, "Hammer", Decimal("1"), Decimal("10.00"), Decimal("10.00"))],
        vendor_name="Acme Corp",
    )
    po = make_po(
        [make_po_item(1, "Hammer", Decimal("1"), Decimal("10.00"), Decimal("10.00"))],
        vendor_name="ACME INDUSTRIAL",
    )

    report = build_match_report(invoice, po, "PO-1")

    assert report.vendor_matches is False
    assert report.overall_outcome == MatchOutcome.MISMATCH
    assert "vendor mismatch" in report.notes
    assert report.line_matches[0].outcome == MatchOutcome.MATCH

    # Case-insensitive exact equality still counts as a match.
    same_vendor_po = make_po([], vendor_name="  ACME CORP")
    assert match_vendor(invoice, make_po([], vendor_name="acme corp")) is True
    assert match_vendor(invoice, same_vendor_po) is False  # leading/trailing spaces
    assert match_vendor(make_invoice([], vendor_name=None), po) is None


def test_build_match_report_total_mismatch() -> None:
    invoice = make_invoice(
        [make_line(1, "Hammer", Decimal("1"), Decimal("10.00"), Decimal("10.00"))],
        total_amount=Decimal("100.00"),
    )
    po = make_po(
        [make_po_item(1, "Hammer", Decimal("1"), Decimal("10.00"), Decimal("10.00"))],
        total_amount=Decimal("99.00"),
    )

    report = build_match_report(invoice, po, "PO-1")

    assert report.total_matches is False
    assert report.overall_outcome == MatchOutcome.MISMATCH
    assert "total mismatch" in report.notes
    assert report.mismatch_summary == {}

    # Within one cent of tolerance -> still True; missing -> None.
    assert match_totals(invoice, make_po([], total_amount=Decimal("100.005"))) is True
    assert match_totals(make_invoice([], total_amount=None), po) is None
