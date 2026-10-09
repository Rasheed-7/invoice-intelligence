"""Tests for duplicate invoice detection.

Pure in-memory ORM objects throughout: no database, no LLM, no writes. The
fixtures set explicit ids and document hashes so each test controls exactly
which signals can fire.
"""

import uuid
from decimal import Decimal

import pytest

from app.models.document import Document
from app.models.invoice import Invoice, LineItem
from app.services.duplicate_detection.scorer import (
    DUPLICATE_THRESHOLD,
    SUSPECTED_THRESHOLD,
    find_duplicates,
    score_pair,
)
from app.services.duplicate_detection.signals import (
    fire_line_item_overlap,
    is_identical_file,
    is_same_po_amount,
    is_same_vendor_amount,
    is_same_vendor_invoice_number,
)
from app.services.duplicate_detection.types import (
    DuplicateMatch,
    DuplicateReport,
    DuplicateSignal,
    DuplicateVerdict,
)

_sha_counter = [0]


def next_sha() -> str:
    """Distinct hash per call, so IDENTICAL_FILE does not fire by accident."""
    _sha_counter[0] += 1
    return f"{_sha_counter[0]:064d}"


def make_invoice(**overrides) -> Invoice:
    """Build an in-memory invoice.

    Vendor, invoice number and total default to *distinct* per-call values, so
    a signal only fires when a test deliberately sets the fields it needs.
    Without this, every fixture would share a vendor and amount and
    SAME_VENDOR_AMOUNT would fire in tests that never intended it.
    """
    document = Document(
        filename=overrides.pop("filename", "invoice.pdf"),
        content_type="application/pdf",
        size_bytes=1024,
        sha256=overrides.pop("sha256", next_sha()),
        storage_path="/storage/invoice.pdf",
    )
    seed = _sha_counter[0]
    data = {
        "id": overrides.pop("id", uuid.uuid4()),
        "vendor_name": f"Vendor {seed}",
        "invoice_number": f"INV-{seed}",
        "po_number": None,
        "currency": "USD",
        "total_amount": Decimal(f"{seed}.00"),
    }
    data.update(overrides)
    invoice = Invoice(document=document, **data)
    if "line_items" in data:
        invoice.line_items = data["line_items"]
    return invoice


def line(description: str, quantity: str = "2", unit_price: str = "10.00") -> LineItem:
    return LineItem(
        position=1,
        description=description,
        quantity=Decimal(quantity),
        unit_price=Decimal(unit_price),
        line_total=Decimal("20.00"),
    )


def unrelated() -> Invoice:
    """An invoice that shares no signal with a default invoice."""
    return make_invoice(
        vendor_name="Globex",
        invoice_number="INV-9999",
        total_amount=Decimal("42.00"),
    )


def same_money(**overrides) -> tuple[Invoice, Invoice]:
    """A pair sharing vendor and total, differing in invoice number."""
    a = make_invoice(vendor_name="Acme", total_amount=Decimal("100.00"), **overrides)
    b = make_invoice(
        vendor_name="Acme",
        total_amount=Decimal("100.00"),
        invoice_number="INV-OTHER",
    )
    return a, b


# --- individual signals -----------------------------------------------------


def test_identical_file_signal_fires_on_matching_sha() -> None:
    sha = next_sha()
    assert is_identical_file(
        make_invoice(sha256=sha), make_invoice(sha256=sha)
    )


def test_identical_file_signal_does_not_fire_on_different_sha() -> None:
    assert not is_identical_file(make_invoice(), make_invoice())


def test_same_vendor_invoice_number_signal() -> None:
    assert is_same_vendor_invoice_number(
        make_invoice(vendor_name="Acme", invoice_number="INV-7"),
        make_invoice(vendor_name="Acme", invoice_number="INV-7"),
    )


def test_same_vendor_invoice_number_needs_both_fields() -> None:
    """Same vendor but a different number is not this signal."""
    assert not is_same_vendor_invoice_number(
        make_invoice(vendor_name="Acme", invoice_number="INV-1"),
        make_invoice(vendor_name="Acme", invoice_number="INV-2"),
    )
    # Same number but a different vendor is not this signal either.
    assert not is_same_vendor_invoice_number(
        make_invoice(vendor_name="Acme", invoice_number="INV-1"),
        make_invoice(vendor_name="Globex", invoice_number="INV-1"),
    )


def test_same_vendor_amount_signal() -> None:
    a, b = same_money()
    assert is_same_vendor_amount(a, b)


def test_same_vendor_amount_needs_matching_vendor() -> None:
    a = make_invoice(vendor_name="Acme", total_amount=Decimal("100.00"))
    b = make_invoice(vendor_name="Globex", total_amount=Decimal("100.00"))

    assert not is_same_vendor_amount(a, b)


def test_same_po_amount_signal() -> None:
    a, b = same_money()
    a.po_number = "PO-9"
    b.po_number = "PO-9"
    assert is_same_po_amount(a, b)


def test_same_po_amount_needs_matching_po() -> None:
    a, b = same_money()
    a.po_number = "PO-9"
    b.po_number = "PO-10"
    assert not is_same_po_amount(a, b)


def test_line_item_overlap_signal_fires_on_matching_lines() -> None:
    a = make_invoice(line_items=[line("Steel Plate")])
    b = make_invoice(line_items=[line("Steel Plate")])

    assert fire_line_item_overlap(a, b)


def test_line_item_overlap_ignores_repriced_lines() -> None:
    """Same description but a different price is a reprice, not a duplicate."""
    a = make_invoice(line_items=[line("Steel Plate", unit_price="10.00")])
    b = make_invoice(line_items=[line("Steel Plate", unit_price="12.00")])

    assert not fire_line_item_overlap(a, b)


def test_line_item_overlap_ignores_different_descriptions() -> None:
    a = make_invoice(line_items=[line("Steel Plate")])
    b = make_invoice(line_items=[line("Copper Wire")])

    assert not fire_line_item_overlap(a, b)


def test_line_item_overlap_requires_eighty_percent() -> None:
    """4 of 5 lines matching (80%) fires; 3 of 5 (60%) does not.

    Descriptions are deliberately dissimilar. Near-identical labels like
    "Item 1" and "Item 3" score above the 0.8 similarity threshold against each
    other, which would let one line match the wrong counterpart and inflate the
    overlap count.
    """
    descriptions = ["Steel Plate", "Copper Wire", "Bolt M10", "Nut M10", "Washer M10"]
    unrelated = ["Office Chair", "Desk Lamp", "Monitor Arm", "Docking Station", "Keyboard Tray"]

    a_items = [
        LineItem(
            position=i,
            description=description,
            quantity=Decimal("1"),
            unit_price=Decimal("1"),
        )
        for i, description in enumerate(descriptions, start=1)
    ]

    def build(labels: list[str]) -> list[LineItem]:
        return [
            LineItem(
                position=i,
                description=label,
                quantity=Decimal("1"),
                unit_price=Decimal("1"),
            )
            for i, label in enumerate(labels, start=1)
        ]

    # Three shared, two unrelated: 3/5 = 0.6, below the threshold.
    three_shared = descriptions[:3] + unrelated[3:5]
    assert not fire_line_item_overlap(
        make_invoice(line_items=a_items), make_invoice(line_items=build(three_shared))
    )

    # Four shared, one unrelated: 4/5 = 0.8, at the threshold.
    four_shared = descriptions[:4] + unrelated[4:5]
    assert fire_line_item_overlap(
        make_invoice(line_items=a_items), make_invoice(line_items=build(four_shared))
    )


def test_line_item_overlap_is_false_without_lines() -> None:
    """Nothing to compare means the signal cannot fire."""
    assert not fire_line_item_overlap(make_invoice(), make_invoice())


# --- scoring ----------------------------------------------------------------


def test_score_pair_returns_none_when_no_signal_fires() -> None:
    assert score_pair(make_invoice(), unrelated()) is None


def test_identical_file_alone_is_duplicate() -> None:
    """A single definitive signal is enough."""
    sha = next_sha()
    match = score_pair(make_invoice(sha256=sha), make_invoice(sha256=sha))

    assert match is not None
    assert match.signals == [DuplicateSignal.IDENTICAL_FILE]
    assert match.score == 1.0
    assert match.verdict == DuplicateVerdict.DUPLICATE


def test_same_vendor_and_number_is_duplicate_via_boost() -> None:
    """The canonical duplicate reaches DUPLICATE on that signal alone."""
    match = score_pair(
        make_invoice(vendor_name="Acme", invoice_number="INV-1"),
        make_invoice(vendor_name="Acme", invoice_number="INV-1"),
    )

    assert match is not None
    assert match.signals == [DuplicateSignal.SAME_VENDOR_INVOICE_NUMBER]
    assert match.score == pytest.approx(0.75)
    # The score alone would be SUSPECTED; the verdict override promotes it.
    assert match.score < DUPLICATE_THRESHOLD
    assert match.verdict == DuplicateVerdict.DUPLICATE


def test_vendor_amount_and_po_amount_is_suspected() -> None:
    """Two mid-weight signals corroborate without reaching DUPLICATE."""
    a, b = same_money()
    a.po_number = "PO-9"
    b.po_number = "PO-9"

    match = score_pair(a, b)

    assert match is not None
    assert set(match.signals) == {
        DuplicateSignal.SAME_VENDOR_AMOUNT,
        DuplicateSignal.SAME_PO_AMOUNT,
    }
    assert match.score == pytest.approx(0.9)
    assert match.score >= SUSPECTED_THRESHOLD
    assert match.score < DUPLICATE_THRESHOLD
    assert match.verdict == DuplicateVerdict.SUSPECTED_DUPLICATE


def test_line_item_overlap_alone_is_unique() -> None:
    """One weak signal stays below the suspect threshold."""
    a = make_invoice(line_items=[line("Steel Plate")])
    b = make_invoice(line_items=[line("Steel Plate")])

    match = score_pair(a, b)

    assert match is not None
    assert match.signals == [DuplicateSignal.LINE_ITEM_OVERLAP]
    assert match.score == pytest.approx(0.35)
    assert match.verdict == DuplicateVerdict.UNIQUE


def test_two_weak_signals_combine_to_suspected() -> None:
    """Weak evidence adds up when it corroborates."""
    a, b = same_money()
    a.line_items = [line("Steel Plate")]
    b.line_items = [line("Steel Plate")]

    match = score_pair(a, b)

    assert match is not None
    assert set(match.signals) == {
        DuplicateSignal.SAME_VENDOR_AMOUNT,
        DuplicateSignal.LINE_ITEM_OVERLAP,
    }
    assert match.score == pytest.approx(0.75)
    assert match.verdict == DuplicateVerdict.SUSPECTED_DUPLICATE


def test_vendor_and_invoice_number_is_case_insensitive() -> None:
    """The same vendor writes its name inconsistently."""
    match = score_pair(
        make_invoice(vendor_name="ACME CORP", invoice_number="inv-1"),
        make_invoice(vendor_name="  acme corp  ", invoice_number="INV-1"),
    )

    assert match is not None
    assert DuplicateSignal.SAME_VENDOR_INVOICE_NUMBER in match.signals


def test_amount_comparison_tolerates_one_cent() -> None:
    a, b = same_money()
    b.total_amount = Decimal("100.01")

    assert is_same_vendor_amount(a, b)


def test_amount_comparison_rejects_two_cent_drift() -> None:
    a, b = same_money()
    b.total_amount = Decimal("100.02")

    assert not is_same_vendor_amount(a, b)


def test_amount_signals_ignore_missing_amounts() -> None:
    """A missing amount is absence of evidence, not evidence of duplication."""
    a = make_invoice(total_amount=None)
    b = make_invoice(total_amount=None)

    assert not is_same_vendor_amount(a, b)
    assert not is_same_po_amount(a, b)


# --- report -----------------------------------------------------------------


def test_find_duplicates_with_empty_candidates_is_unique() -> None:
    current = make_invoice()

    report = find_duplicates(current, [])

    assert report.invoice_id == current.id
    assert report.verdict == DuplicateVerdict.UNIQUE
    assert report.top_match is None
    assert report.all_matches == []
    assert report.notes == ["no duplicate signals found"]


def test_find_duplicates_excludes_self() -> None:
    """An invoice is trivially identical to itself and must be excluded."""
    current = make_invoice()

    report = find_duplicates(current, [current])

    assert report.all_matches == []
    assert report.verdict == DuplicateVerdict.UNIQUE
    assert all(m.matching_invoice_id != current.id for m in report.all_matches)


def test_find_duplicates_orders_by_score_then_id() -> None:
    """Deterministic ordering: score DESC, then matching_invoice_id ASC."""
    sha = next_sha()
    current = make_invoice(sha256=sha)
    identical = make_invoice(sha256=sha)
    # Two equally-scored matches, handed to find_duplicates out of id order.
    low_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    high_id = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
    first = make_invoice(id=low_id, vendor_name="Acme", total_amount=Decimal("100.00"))
    second = make_invoice(id=high_id, vendor_name="Acme", total_amount=Decimal("100.00"))

    report = find_duplicates(current, [second, identical, first])

    assert report.verdict == DuplicateVerdict.DUPLICATE
    assert report.top_match is not None
    assert report.top_match.matching_invoice_id == identical.id
    scores = [m.score for m in report.all_matches]
    assert scores == sorted(scores, reverse=True)
    # The two 0.4-scored ties come back in id order.
    tied = [m.matching_invoice_id for m in report.all_matches[1:]]
    assert tied == sorted(tied)


def test_report_verdict_is_highest_severity_across_matches() -> None:
    """One duplicate outranks several suspects."""
    current = make_invoice(
        vendor_name="Acme", invoice_number="INV-1", total_amount=Decimal("100.00")
    )
    duplicate = make_invoice(
        vendor_name="Acme", invoice_number="INV-1", total_amount=Decimal("999.00")
    )
    suspected = make_invoice(
        vendor_name="Acme",
        invoice_number="INV-B",
        total_amount=Decimal("100.00"),
        line_items=[line("Widget")],
    )

    report = find_duplicates(current, [suspected, duplicate])

    assert report.verdict == DuplicateVerdict.DUPLICATE
    assert report.top_match is not None
    assert report.top_match.verdict == DuplicateVerdict.DUPLICATE


def test_report_notes_describe_top_signals() -> None:
    sha = next_sha()
    current = make_invoice(sha256=sha)
    identical = make_invoice(sha256=sha)

    report = find_duplicates(current, [identical])

    assert report.notes == ["matched identical file sha256"]


def test_report_note_for_vendor_invoice_number() -> None:
    current = make_invoice(vendor_name="Acme", invoice_number="INV-1")
    other = make_invoice(vendor_name="Acme", invoice_number="INV-1")

    report = find_duplicates(current, [other])

    assert report.notes == ["same vendor and invoice number"]


def test_report_types_reject_unknown_fields() -> None:
    """Both result models are strict about unexpected fields."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DuplicateMatch(
            matching_invoice_id=uuid.uuid4(),
            score=0.5,
            signals=[],
            verdict=DuplicateVerdict.UNIQUE,
            fabricated="nope",
        )

    with pytest.raises(ValidationError):
        DuplicateReport(
            invoice_id=uuid.uuid4(),
            verdict=DuplicateVerdict.UNIQUE,
            top_match=None,
            all_matches=[],
            notes=[],
            extra_field=1,
        )