"""Deterministic PO <-> invoice matching.

Pure computation over in-memory ORM objects: no database access and no LLM
calls anywhere in this module. The comparison path is plain Decimal math and
difflib string similarity.
"""

import uuid
from collections.abc import Sequence
from decimal import Decimal

from app.models.invoice import Invoice, LineItem
from app.models.purchase_order import PurchaseOrder, PurchaseOrderItem
from app.services.po_matching.normalize import description_similarity
from app.services.po_matching.types import (
    FieldMismatch,
    LineMatch,
    MatchOutcome,
    MismatchSeverity,
    PurchaseOrderMatchReport,
)

# --- tolerances -------------------------------------------------------------
# Numeric comparisons are exact by default (quantities are contractual);
# money allows one cent of rounding slack (invoiced totals are often
# recomputed by the vendor's system with intermediate rounding).
QUANTITY_TOLERANCE = Decimal("0")
UNIT_PRICE_TOLERANCE = Decimal("0.01")
LINE_TOTAL_TOLERANCE = Decimal("0.01")
TOTAL_TOLERANCE = Decimal("0.01")

# Two lines are candidates for pairing only at this normalized similarity or
# above; below it they are treated as unrelated items.
PAIR_SIMILARITY_THRESHOLD = 0.8
# Once paired, wording differences below this ratio are cosmetic (LOW) noise
# rather than a substantive disagreement about what was ordered.
DESCRIPTION_SIMILARITY_THRESHOLD = 0.95


def _decimal_mismatch(
    field: str,
    invoice_value: Decimal | None,
    po_value: Decimal | None,
    tolerance: Decimal,
    severity: MismatchSeverity,
) -> FieldMismatch | None:
    if invoice_value is None and po_value is None:
        return None
    if invoice_value is None or po_value is None:
        return FieldMismatch(
            field=field,
            po_value=None if po_value is None else str(po_value),
            invoice_value=None if invoice_value is None else str(invoice_value),
            difference="missing value on one side",
            severity=severity,
        )
    difference = abs(invoice_value - po_value)
    if difference <= tolerance:
        return None
    return FieldMismatch(
        field=field,
        po_value=str(po_value),
        invoice_value=str(invoice_value),
        difference=str(difference),
        severity=severity,
    )


def _line_outcome(mismatches: list[FieldMismatch]) -> MatchOutcome:
    if not mismatches:
        return MatchOutcome.MATCH
    if any(mismatch.severity == MismatchSeverity.HIGH for mismatch in mismatches):
        return MatchOutcome.MISMATCH
    return MatchOutcome.PARTIAL_MATCH


def match_line_items(
    invoice_items: Sequence[LineItem],
    po_items: Sequence[PurchaseOrderItem],
) -> tuple[list[LineMatch], list[uuid.UUID], list[uuid.UUID]]:
    """Greedily pair invoice lines to PO lines, one-to-one.

    Pairing requires normalized description similarity >= 0.8; ties are
    broken by the lowest PO position. Each pair is then compared field by
    field:

    * quantity   -> HIGH   severity
    * unit_price -> HIGH   severity
    * line_total -> MEDIUM severity
    * description similarity < 0.95 -> LOW severity

    Outcome is MATCH with no mismatches, MISMATCH if any HIGH mismatch,
    otherwise PARTIAL_MATCH. Returns (line matches, unmatched invoice item
    ids, unmatched PO item ids).
    """
    invoice_sorted = sorted(invoice_items, key=lambda item: item.position)
    po_sorted = sorted(po_items, key=lambda item: item.position)

    pairings: dict[int, int] = {}
    used_po: set[int] = set()

    for invoice_idx, invoice_line in enumerate(invoice_sorted):
        best_po_idx: int | None = None
        best_similarity = 0.0
        for po_idx, po_line in enumerate(po_sorted):
            if po_idx in used_po:
                continue
            similarity = description_similarity(invoice_line.description, po_line.description)
            if similarity < PAIR_SIMILARITY_THRESHOLD:
                continue
            # Strict > keeps the earliest (lowest) position on ties.
            if best_po_idx is None or similarity > best_similarity:
                best_po_idx, best_similarity = po_idx, similarity
        if best_po_idx is not None:
            used_po.add(best_po_idx)
            pairings[invoice_idx] = best_po_idx

    line_matches: list[LineMatch] = []
    for invoice_idx, invoice_line in enumerate(invoice_sorted):
        po_idx = pairings.get(invoice_idx)
        if po_idx is None:
            continue
        po_line = po_sorted[po_idx]

        mismatches: list[FieldMismatch] = []
        for field, value_a, value_b, tolerance, severity in (
            (
                "quantity",
                invoice_line.quantity,
                po_line.quantity,
                QUANTITY_TOLERANCE,
                MismatchSeverity.HIGH,
            ),
            (
                "unit_price",
                invoice_line.unit_price,
                po_line.unit_price,
                UNIT_PRICE_TOLERANCE,
                MismatchSeverity.HIGH,
            ),
            (
                "line_total",
                invoice_line.line_total,
                po_line.line_total,
                LINE_TOTAL_TOLERANCE,
                MismatchSeverity.MEDIUM,
            ),
        ):
            mismatch = _decimal_mismatch(field, value_a, value_b, tolerance, severity)
            if mismatch is not None:
                mismatches.append(mismatch)

        similarity = description_similarity(invoice_line.description, po_line.description)
        if similarity < DESCRIPTION_SIMILARITY_THRESHOLD:
            mismatches.append(
                FieldMismatch(
                    field="description",
                    po_value=po_line.description,
                    invoice_value=invoice_line.description,
                    difference=f"similarity={similarity:.2f}",
                    severity=MismatchSeverity.LOW,
                )
            )

        line_matches.append(
            LineMatch(
                invoice_item_id=invoice_line.id,
                po_item_id=po_line.id,
                outcome=_line_outcome(mismatches),
                mismatches=mismatches,
            )
        )

    unmatched_invoice = [
        line.id for idx, line in enumerate(invoice_sorted) if idx not in pairings
    ]
    unmatched_po = [line.id for idx, line in enumerate(po_sorted) if idx not in used_po]
    return line_matches, unmatched_invoice, unmatched_po


def match_vendor(invoice: Invoice, po: PurchaseOrder) -> bool | None:
    """Case-insensitive exact vendor comparison; None when either side is missing."""
    if invoice.vendor_name is None or po.vendor_name is None:
        return None
    return invoice.vendor_name.casefold() == po.vendor_name.casefold()


def match_totals(invoice: Invoice, po: PurchaseOrder) -> bool | None:
    """True when totals agree within TOTAL_TOLERANCE; None when either is missing."""
    if invoice.total_amount is None or po.total_amount is None:
        return None
    return abs(invoice.total_amount - po.total_amount) <= TOTAL_TOLERANCE


def _derive_overall(
    line_matches: list[LineMatch],
    unmatched_invoice: list[uuid.UUID],
    unmatched_po: list[uuid.UUID],
    vendor_matches: bool | None,
    total_matches: bool | None,
) -> MatchOutcome:
    if any(match.outcome == MatchOutcome.MISMATCH for match in line_matches):
        return MatchOutcome.MISMATCH
    if vendor_matches is False or total_matches is False:
        return MatchOutcome.MISMATCH
    if any(match.outcome == MatchOutcome.PARTIAL_MATCH for match in line_matches):
        return MatchOutcome.PARTIAL_MATCH
    if unmatched_invoice or unmatched_po:
        return MatchOutcome.PARTIAL_MATCH
    return MatchOutcome.MATCH


def build_match_report(
    invoice: Invoice,
    po: PurchaseOrder | None,
    po_reference: str | None,
) -> PurchaseOrderMatchReport:
    """Run every sub-matcher and derive the overall outcome.

    * no PO and no reference  -> SKIPPED ("no PO reference on invoice")
    * reference but no PO     -> MISMATCH ("PO {ref} not found", po_found=False)
    * PO found                -> line/vendor/total matching, outcome derived
    """
    if po is None and po_reference is None:
        return PurchaseOrderMatchReport(
            invoice_id=invoice.id,
            po_reference=None,
            po_found=False,
            overall_outcome=MatchOutcome.SKIPPED,
            line_matches=[],
            unmatched_invoice_items=[],
            unmatched_po_items=[],
            mismatch_summary={},
            vendor_matches=None,
            total_matches=None,
            notes=["no PO reference on invoice"],
        )

    if po is None:
        return PurchaseOrderMatchReport(
            invoice_id=invoice.id,
            po_reference=po_reference,
            po_found=False,
            overall_outcome=MatchOutcome.MISMATCH,
            line_matches=[],
            unmatched_invoice_items=[],
            unmatched_po_items=[],
            mismatch_summary={},
            vendor_matches=None,
            total_matches=None,
            notes=[f"PO {po_reference} not found"],
        )

    line_matches, unmatched_invoice, unmatched_po = match_line_items(
        invoice.line_items, po.items
    )
    vendor_matches = match_vendor(invoice, po)
    total_matches = match_totals(invoice, po)

    mismatch_summary: dict[str, int] = {}
    for match in line_matches:
        for mismatch in match.mismatches:
            mismatch_summary[mismatch.field] = mismatch_summary.get(mismatch.field, 0) + 1

    notes: list[str] = []
    if vendor_matches is False:
        notes.append("vendor mismatch")
    if total_matches is False:
        notes.append("total mismatch")
    if unmatched_invoice:
        notes.append(f"{len(unmatched_invoice)} invoice line(s) not matched")
    if unmatched_po:
        notes.append(f"{len(unmatched_po)} PO line(s) not matched")

    overall = _derive_overall(
        line_matches, unmatched_invoice, unmatched_po, vendor_matches, total_matches
    )

    return PurchaseOrderMatchReport(
        invoice_id=invoice.id,
        po_reference=po_reference,
        po_found=True,
        overall_outcome=overall,
        line_matches=line_matches,
        unmatched_invoice_items=unmatched_invoice,
        unmatched_po_items=unmatched_po,
        mismatch_summary=mismatch_summary,
        vendor_matches=vendor_matches,
        total_matches=total_matches,
        notes=notes,
    )
