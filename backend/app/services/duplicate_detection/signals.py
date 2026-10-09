"""Individual duplicate signals.

Each signal is a pure predicate over two in-memory invoices. Keeping them
independent means a new signal can be added, weighted and tested without
touching the scorer or the report shape.

Amounts are compared with a one-cent tolerance rather than exactly: the same
invoice re-submitted through a different system frequently picks up rounding
differences that are not evidence of duplication being *absent*. Vendor and
invoice number are compared case-insensitively because the same vendor
legitimately writes "ACME CORP" on one document and "Acme Corp" on another.
"""

from decimal import Decimal

from app.models.invoice import Invoice
from app.services.duplicate_detection.types import DuplicateSignal
from app.services.po_matching.normalize import descriptions_match

__all__ = [
    "AMOUNT_TOLERANCE",
    "LINE_ITEM_OVERLAP_THRESHOLD",
    "WEIGHTS",
    "fire_line_item_overlap",
    "is_identical_file",
    "is_same_po_amount",
    "is_same_vendor_amount",
    "is_same_vendor_invoice_number",
    "which_signals",
]

#: Amounts within this absolute difference count as equal.
AMOUNT_TOLERANCE = Decimal("0.01")

#: Fraction of a's line items that must be found in b for the overlap signal.
LINE_ITEM_OVERLAP_THRESHOLD = 0.8

#: Similarity threshold for two line descriptions to be considered the same item.
DESCRIPTION_SIMILARITY_THRESHOLD = 0.8

#: Signal weights. Summed to produce a pair score, clamped to 1.0.
WEIGHTS: dict[DuplicateSignal, float] = {
    # Byte-identical content is definitive on its own.
    DuplicateSignal.IDENTICAL_FILE: 1.0,
    DuplicateSignal.SAME_VENDOR_INVOICE_NUMBER: 0.75,
    DuplicateSignal.SAME_PO_AMOUNT: 0.5,
    DuplicateSignal.SAME_VENDOR_AMOUNT: 0.4,
    DuplicateSignal.LINE_ITEM_OVERLAP: 0.35,
}


def _normalized(value: str | None) -> str | None:
    """Casefold and strip, mapping empty to None so blanks never match."""
    if value is None:
        return None
    cleaned = value.strip().casefold()
    return cleaned or None


def _amounts_agree(a: Decimal | None, b: Decimal | None) -> bool:
    """True when both are present and differ by at most one cent."""
    if a is None or b is None:
        return False
    return abs(a - b) <= AMOUNT_TOLERANCE


def is_identical_file(a: Invoice, b: Invoice) -> bool:
    """The two documents have the same content hash.

    Strongest single signal: the same bytes were uploaded twice.
    """
    if a.document is None or b.document is None:
        return False
    return a.document.sha256 == b.document.sha256


def is_same_vendor_invoice_number(a: Invoice, b: Invoice) -> bool:
    """Same vendor and same invoice number - the canonical duplicate."""
    vendor = _normalized(a.vendor_name)
    number = _normalized(a.invoice_number)
    if vendor is None or number is None:
        return False
    return vendor == _normalized(b.vendor_name) and number == _normalized(b.invoice_number)


def is_same_vendor_amount(a: Invoice, b: Invoice) -> bool:
    """Same vendor billing the same total. A re-send often differs in nothing else."""
    vendor = _normalized(a.vendor_name)
    if vendor is None or vendor != _normalized(b.vendor_name):
        return False
    return _amounts_agree(a.total_amount, b.total_amount)


def is_same_po_amount(a: Invoice, b: Invoice) -> bool:
    """Same PO number and same total: two invoices against one purchase order.

    Weaker than a shared invoice number because one PO legitimately receives
    several invoices (partial billing).
    """
    po = _normalized(a.po_number)
    if po is None or po != _normalized(b.po_number):
        return False
    return _amounts_agree(a.total_amount, b.total_amount)


def _line_items_agree(a_line, b_line) -> bool:
    """Two lines describe the same purchased thing for the same money."""
    if not descriptions_match(
        a_line.description, b_line.description, threshold=DESCRIPTION_SIMILARITY_THRESHOLD
    ):
        return False
    return _amounts_agree(a_line.quantity, b_line.quantity) and _amounts_agree(
        a_line.unit_price, b_line.unit_price
    )


def fire_line_item_overlap(a: Invoice, b: Invoice) -> bool:
    """At least 80% of a's lines also appear in b, by description and price.

    Descriptions are compared through the shared normalizer so "Steel Plate" and
    "steel, plate" match, and a line counts only when quantity *and* unit price
    agree - a re-submission with the same items repriced is not a duplicate.
    """
    lines = list(a.line_items)
    if not lines:
        return False

    # Nothing to overlap against.
    if not b.line_items:
        return False

    matched = sum(
        1
        for line in lines
        if any(_line_items_agree(line, candidate) for candidate in b.line_items)
    )

    return (matched / len(lines)) >= LINE_ITEM_OVERLAP_THRESHOLD


def which_signals(a: Invoice, b: Invoice) -> list[DuplicateSignal]:
    """Return every signal that fires for the pair, in declaration order.

    Ordering is stable so scores and rendered notes are deterministic.
    """
    checks = (
        (DuplicateSignal.IDENTICAL_FILE, is_identical_file),
        (DuplicateSignal.SAME_VENDOR_INVOICE_NUMBER, is_same_vendor_invoice_number),
        (DuplicateSignal.SAME_VENDOR_AMOUNT, is_same_vendor_amount),
        (DuplicateSignal.SAME_PO_AMOUNT, is_same_po_amount),
        (DuplicateSignal.LINE_ITEM_OVERLAP, fire_line_item_overlap),
    )
    return [signal for signal, check in checks if check(a, b)]