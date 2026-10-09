"""Invoice validation checks — pure functions, no I/O.

Each check takes a SQLAlchemy Invoice model (not the Pydantic extraction schema)
and returns a CheckResult. Checks run against persisted domain state.

Design principles:
- Deterministic: same input → same output
- Explicit outcomes: passed, failed, warning, or skipped (never guess)
- No fabrication: if a check can't run, it returns skipped with a reason
- Decimal precision: all money arithmetic uses Decimal with a documented tolerance
"""

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.invoice import Invoice
from app.services.validation.types import (
    MONEY_TOLERANCE,
    CheckResult,
    ValidationOutcome,
)

__all__ = [
    "check_line_item_totals",
    "check_line_items_sum",
    "check_subtotal_plus_tax",
    "check_tax_rate_consistency",
    "check_date_ordering",
    "check_currency_presence",
    "check_duplicate_invoice_number",
]


def check_line_item_totals(invoice: Invoice) -> CheckResult:
    """Verify quantity × unit_price = line_total for each line item.

    Skipped if no line items have all three fields.
    Failed if any mismatch exceeds MONEY_TOLERANCE.
    Passed if all match within tolerance.
    """
    check_name = "line_item_totals"
    rule_id = "LINE_ITEM_TOTALS"
    
    # Collect items with all three fields present
    checkable = [
        li for li in invoice.line_items
        if li.quantity is not None
        and li.unit_price is not None
        and li.line_total is not None
    ]
    
    if not checkable:
        return CheckResult(
            check_name=check_name,
            rule_id=rule_id,
            outcome=ValidationOutcome.SKIPPED,
            message="No line items with quantity, unit_price, and line_total",
            details={"reason": "no line items with quantity, unit_price, and line_total"},
            severity="info",
        )
    
    mismatches = []
    for li in checkable:
        expected = li.quantity * li.unit_price  # type: ignore[operator]
        actual = li.line_total
        delta = abs(expected - actual)  # type: ignore[operator]
        
        if delta > MONEY_TOLERANCE:
            mismatches.append({
                "position": li.position,
                "quantity": str(li.quantity),
                "unit_price": str(li.unit_price),
                "expected": str(expected),
                "actual": str(actual),
                "delta": str(delta),
            })
    
    if mismatches:
        return CheckResult(
            check_name=check_name,
            rule_id=rule_id,
            outcome=ValidationOutcome.FAILED,
            message=f"{len(mismatches)} line item(s) failed quantity × unit_price check",
            details={"mismatches": mismatches, "checked_items": len(checkable)},
            severity="error",
        )
    
    return CheckResult(
            check_name=check_name,
            rule_id=rule_id,
        outcome=ValidationOutcome.PASSED,
        message=f"All {len(checkable)} line items match quantity × unit_price",
        details={"checked_items": len(checkable)},
        severity="info",
    )


def check_line_items_sum(invoice: Invoice) -> CheckResult:
    """Verify sum of line_total fields equals invoice subtotal.

    Skipped if no line items have line_total or if invoice.subtotal is null.
    Failed if the sum differs from subtotal by more than MONEY_TOLERANCE.
    Passed if they match within tolerance.
    """
    check_name = "line_items_sum"
    rule_id = "LINE_ITEMS_SUM"
    
    if invoice.subtotal is None:
        return CheckResult(
            check_name=check_name,
            rule_id=rule_id,
            outcome=ValidationOutcome.SKIPPED,
            message="Invoice subtotal is null",
            details={"reason": "invoice.subtotal is null"},
            severity="info",
        )
    
    items_with_total = [
        li for li in invoice.line_items
        if li.line_total is not None
    ]
    
    if not items_with_total:
        return CheckResult(
            check_name=check_name,
            rule_id=rule_id,
            outcome=ValidationOutcome.SKIPPED,
            message="No line items with line_total",
            details={"reason": "no line items with line_total"},
            severity="info",
        )
    
    expected = sum((li.line_total for li in items_with_total), start=Decimal("0"))
    actual = invoice.subtotal
    delta = abs(expected - actual)
    
    if delta > MONEY_TOLERANCE:
        return CheckResult(
            check_name=check_name,
            rule_id=rule_id,
            outcome=ValidationOutcome.FAILED,
            message="Sum of line items does not match invoice subtotal",
            details={
                "expected": str(expected),
                "actual": str(actual),
                "delta": str(delta),
                "item_count": len(items_with_total),
            },
            severity="error",
        )
    
    return CheckResult(
            check_name=check_name,
            rule_id=rule_id,
        outcome=ValidationOutcome.PASSED,
        message="Sum of line items matches invoice subtotal",
        details={
            "expected": str(expected),
            "actual": str(actual),
            "delta": str(delta),
            "item_count": len(items_with_total),
        },
        severity="info",
    )
    


def check_subtotal_plus_tax(invoice: Invoice) -> CheckResult:
    """Verify subtotal + tax_amount = total_amount.

    Skipped if any of the three fields is null.
    Failed if subtotal + tax differs from total by more than MONEY_TOLERANCE.
    Passed if they match within tolerance (including zero-tax invoices).
    """
    check_name = "subtotal_plus_tax"
    rule_id = "SUBTOTAL_PLUS_TAX"
    
    if invoice.subtotal is None or invoice.tax_amount is None or invoice.total_amount is None:
        missing = []
        if invoice.subtotal is None:
            missing.append("subtotal")
        if invoice.tax_amount is None:
            missing.append("tax_amount")
        if invoice.total_amount is None:
            missing.append("total_amount")
        
        return CheckResult(
            check_name=check_name,
            rule_id=rule_id,
            outcome=ValidationOutcome.SKIPPED,
            message=f"Missing required fields: {', '.join(missing)}",
            details={"reason": "missing required fields", "missing": missing},
            severity="info",
        )
    
    expected_total = invoice.subtotal + invoice.tax_amount
    actual_total = invoice.total_amount
    delta = abs(expected_total - actual_total)
    
    # Zero tax is legitimate, not a warning
    zero_tax = invoice.tax_amount == Decimal("0")
    
    if delta > MONEY_TOLERANCE:
        return CheckResult(
            check_name=check_name,
            rule_id=rule_id,
            outcome=ValidationOutcome.FAILED,
            message="Subtotal + tax does not match total",
            details={
                "subtotal": str(invoice.subtotal),
                "tax_amount": str(invoice.tax_amount),
                "expected_total": str(expected_total),
                "actual_total": str(actual_total),
                "delta": str(delta),
                "zero_tax": zero_tax,
            },
            severity="error",
        )
    
    return CheckResult(
            check_name=check_name,
            rule_id=rule_id,
        outcome=ValidationOutcome.PASSED,
        message="Subtotal + tax matches total",
        details={
            "subtotal": str(invoice.subtotal),
            "tax_amount": str(invoice.tax_amount),
            "expected_total": str(expected_total),
            "actual_total": str(actual_total),
            "delta": str(delta),
            "zero_tax": zero_tax,
        },
        severity="info",
    )
    


def check_tax_rate_consistency(invoice: Invoice) -> CheckResult:
    """Check consistency of tax rates across line items.

    Currently skipped: we don't have per-line tax amounts to validate against.
    This check exists as a placeholder; it will become meaningful once we track
    line-level tax amounts and can cross-check them against tax_rate fields.

    A zero effective tax rate is valid, not an error.
    """
    check_name = "tax_rate_consistency"
    rule_id = "TAX_RATE_CONSISTENCY"
    
    if invoice.subtotal is None or invoice.tax_amount is None:
        return CheckResult(
            check_name=check_name,
            rule_id=rule_id,
            outcome=ValidationOutcome.SKIPPED,
            message="Missing subtotal or tax_amount",
            details={"reason": "missing subtotal or tax_amount"},
            severity="info",
        )
    
    if invoice.subtotal <= Decimal("0"):
        return CheckResult(
            check_name=check_name,
            rule_id=rule_id,
            outcome=ValidationOutcome.SKIPPED,
            message="Subtotal is zero or negative",
            details={"reason": "subtotal <= 0", "subtotal": str(invoice.subtotal)},
            severity="info",
        )
    
    # Compute the effective rate for context, but we can't validate per-line
    # rates without per-line tax amounts
    effective_rate = (invoice.tax_amount / invoice.subtotal).quantize(Decimal("0.000001"))
    
    # Zero tax is legitimate
    if invoice.tax_amount == Decimal("0"):
        return CheckResult(
            check_name=check_name,
            rule_id=rule_id,
            outcome=ValidationOutcome.PASSED,
            message="Zero tax rate is valid",
            details={"effective_rate": "0"},
            severity="info",
        )
    
    # We can't validate per-line consistency without per-line tax amounts.
    # Placeholder: becomes meaningful once line-level tax is tracked.
    return CheckResult(
            check_name=check_name,
            rule_id=rule_id,
        outcome=ValidationOutcome.SKIPPED,
        message="Per-line tax amounts not available for cross-check",
        details={
            "reason": "per-line tax amounts not available",
            "effective_rate": str(effective_rate),
        },
        severity="info",
    )
    


def check_date_ordering(invoice: Invoice) -> CheckResult:
    """Verify due_date >= invoice_date.

    Skipped if either date is null.
    Failed if due_date is before invoice_date.
    Passed if due_date >= invoice_date.
    """
    check_name = "date_ordering"
    rule_id = "DATE_ORDERING"
    
    if invoice.invoice_date is None or invoice.due_date is None:
        missing = []
        if invoice.invoice_date is None:
            missing.append("invoice_date")
        if invoice.due_date is None:
            missing.append("due_date")
        
        return CheckResult(
            check_name=check_name,
            rule_id=rule_id,
            outcome=ValidationOutcome.SKIPPED,
            message=f"Missing required fields: {', '.join(missing)}",
            details={"reason": "missing required fields", "missing": missing},
            severity="info",
        )
    
    if invoice.due_date < invoice.invoice_date:
        delta_days = (invoice.invoice_date - invoice.due_date).days
        return CheckResult(
            check_name=check_name,
            rule_id=rule_id,
            outcome=ValidationOutcome.FAILED,
            message="Due date is before invoice date",
            details={
                "invoice_date": invoice.invoice_date.isoformat(),
                "due_date": invoice.due_date.isoformat(),
                "delta_days": -delta_days,  # negative to show how far back
            },
            severity="error",
        )
    
    delta_days = (invoice.due_date - invoice.invoice_date).days
    return CheckResult(
            check_name=check_name,
            rule_id=rule_id,
        outcome=ValidationOutcome.PASSED,
        message="Due date is on or after invoice date",
        details={
            "invoice_date": invoice.invoice_date.isoformat(),
            "due_date": invoice.due_date.isoformat(),
            "delta_days": delta_days,
        },
        severity="info",
    )


def check_currency_presence(invoice: Invoice) -> CheckResult:
    """Check if currency is present when monetary fields exist.

    Skipped if no monetary fields are present.
    Warning if currency is null but monetary fields exist (usable but ambiguous).
    Passed if currency is present.
    """
    check_name = "currency_presence"
    rule_id = "CURRENCY_PRESENCE"
    
    has_money = any([
        invoice.subtotal is not None,
        invoice.tax_amount is not None,
        invoice.total_amount is not None,
    ])
    
    if not has_money:
        return CheckResult(
            check_name=check_name,
            rule_id=rule_id,
            outcome=ValidationOutcome.SKIPPED,
            message="No monetary fields present",
            details={"reason": "no monetary fields"},
            severity="info",
        )
    
    if invoice.currency is None:
        return CheckResult(
            check_name=check_name,
            rule_id=rule_id,
            outcome=ValidationOutcome.WARNING,
            message="Currency is missing while monetary fields exist",
            details={
                "has_subtotal": invoice.subtotal is not None,
                "has_tax_amount": invoice.tax_amount is not None,
                "has_total_amount": invoice.total_amount is not None,
            },
            severity="warn",
        )
    
    return CheckResult(
            check_name=check_name,
            rule_id=rule_id,
        outcome=ValidationOutcome.PASSED,
        message=f"Currency is present: {invoice.currency}",
        details={"currency": invoice.currency},
        severity="info",
    )


def check_duplicate_invoice_number(invoice: Invoice, db: Session) -> CheckResult:
    """Check if an invoice with the same invoice_number and vendor_name already exists.

    Requires DB access.
    Skipped if invoice_number, vendor_name, or id are missing.
    Passed if no duplicates found.
    Failed if one or more matches found.
    """
    check_name = "duplicate_invoice_number"
    rule_id = "DUPLICATE_INVOICE_NUMBER"

    if invoice.invoice_number is None or invoice.vendor_name is None or invoice.id is None:
        return CheckResult(
            check_name=check_name,
            rule_id=rule_id,
            outcome=ValidationOutcome.SKIPPED,
            message="Missing required inputs (invoice_number, vendor_name, or id)",
            details={"reason": "missing inputs"},
            severity="info",
        )

    stmt = (
        select(Invoice.id)
        .where(
            func.lower(Invoice.vendor_name) == invoice.vendor_name.lower(),
            Invoice.invoice_number == invoice.invoice_number,
            Invoice.id != invoice.id,
            # A soft-deleted invoice no longer exists as far as live invoices
            # are concerned: it must not fail another invoice's validation.
            Invoice.deleted_at.is_(None),
        )
    )
    duplicate_ids_qs = db.scalars(stmt).all()
    duplicate_ids = [str(vid) for vid in duplicate_ids_qs]
    duplicate_count = len(duplicate_ids)

    if duplicate_count == 0:
        return CheckResult(
            check_name=check_name,
            rule_id=rule_id,
            outcome=ValidationOutcome.PASSED,
            message="Unique invoice number for vendor",
            details={"duplicate_count": 0},
            severity="info",
        )

    return CheckResult(
        check_name=check_name,
        rule_id=rule_id,
        outcome=ValidationOutcome.FAILED,
        message=(
            f"Invoice number {invoice.invoice_number} already exists "
            f"for vendor {invoice.vendor_name}"
        ),
        details={"duplicate_count": duplicate_count, "duplicate_ids": duplicate_ids},
        severity="error",
    )



