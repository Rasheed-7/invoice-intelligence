"""Deterministic validation checks — pure functions, no DB required.

Invoice and LineItem objects are built in memory (SQLAlchemy allows
constructing mapped instances without a session) and passed directly to the
checks. Every check gets a passing, failing, and skipped case, plus runner
coverage for precedence, summary counts, and JSON-serializable details.
"""

import json
import uuid
from datetime import date
from decimal import Decimal

from app.models.invoice import Invoice, LineItem
from app.services.validation.checks import (
    check_currency_presence,
    check_date_ordering,
    check_line_item_totals,
    check_line_items_sum,
    check_subtotal_plus_tax,
    check_tax_rate_consistency,
)
from app.services.validation.runner import ALL_CHECKS, ALL_DB_CHECKS, run_all_checks
from app.services.validation.types import ValidationOutcome


def make_invoice(**overrides: object) -> Invoice:
    """Return a valid in-memory invoice; tests mutate fields as needed."""
    invoice = Invoice(
        id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        invoice_number="INV-001",
        invoice_date=date(2026, 1, 15),
        due_date=date(2026, 2, 15),
        currency="USD",
        subtotal=Decimal("100.00"),
        tax_amount=Decimal("8.50"),
        total_amount=Decimal("108.50"),
    )
    invoice.line_items = [
        LineItem(
            position=1,
            description="Widget",
            quantity=Decimal("1"),
            unit_price=Decimal("100.00"),
            line_total=Decimal("100.00"),
        ),
    ]
    for key, value in overrides.items():
        setattr(invoice, key, value)
    return invoice


# --- check_line_item_totals -------------------------------------------------


def test_line_item_totals_passed():
    """2 × 10.00 = 20.00 matches line_total exactly."""
    invoice = make_invoice(
        line_items=[
            LineItem(
                position=1,
                quantity=Decimal("2"),
                unit_price=Decimal("10.00"),
                line_total=Decimal("20.00"),
            ),
        ],
    )
    result = check_line_item_totals(invoice)
    assert result.outcome == ValidationOutcome.PASSED
    assert result.severity == "info"
    assert result.details["checked_items"] == 1
    assert result.rule_id == "LINE_ITEM_TOTALS"


def test_line_item_totals_failed():
    """2 × 10.00 = 20.00 but line_total says 21.00 → delta 1.00."""
    invoice = make_invoice(
        line_items=[
            LineItem(
                position=1,
                quantity=Decimal("2"),
                unit_price=Decimal("10.00"),
                line_total=Decimal("21.00"),
            ),
        ],
    )
    result = check_line_item_totals(invoice)
    assert result.outcome == ValidationOutcome.FAILED
    assert result.severity == "error"
    mismatch = result.details["mismatches"][0]
    assert mismatch["position"] == 1
    assert mismatch["expected"] == "20.00"
    assert mismatch["actual"] == "21.00"
    assert mismatch["delta"] == "1.00"


def test_line_item_totals_skipped_no_complete_items():
    """A line with only a description cannot be arithmetically checked."""
    invoice = make_invoice(
        line_items=[LineItem(position=1, description="no numbers here")],
    )
    result = check_line_item_totals(invoice)
    assert result.outcome == ValidationOutcome.SKIPPED
    assert result.severity == "info"
    assert result.details["reason"] == (
        "no line items with quantity, unit_price, and line_total"
    )


def test_line_item_totals_tolerance_accepts_one_cent_drift():
    """Exact product passes; a delta of exactly 0.01 also passes."""
    exact = make_invoice(
        line_items=[
            LineItem(
                position=1,
                quantity=Decimal("3"),
                unit_price=Decimal("3.33"),
                line_total=Decimal("9.99"),
            ),
        ],
    )
    assert check_line_item_totals(exact).outcome == ValidationOutcome.PASSED

    one_cent = make_invoice(
        line_items=[
            LineItem(
                position=1,
                quantity=Decimal("1"),
                unit_price=Decimal("10.00"),
                line_total=Decimal("10.01"),
            ),
        ],
    )
    # delta is exactly MONEY_TOLERANCE; failure requires delta > tolerance.
    assert check_line_item_totals(one_cent).outcome == ValidationOutcome.PASSED


# --- check_line_items_sum ---------------------------------------------------


def test_line_items_sum_passed():
    """60.00 + 40.00 sums to subtotal 100.00 exactly."""
    invoice = make_invoice(
        subtotal=Decimal("100.00"),
        line_items=[
            LineItem(position=1, line_total=Decimal("60.00")),
            LineItem(position=2, line_total=Decimal("40.00")),
        ],
    )
    result = check_line_items_sum(invoice)
    assert result.outcome == ValidationOutcome.PASSED
    assert result.details["expected"] == "100.00"
    assert result.details["actual"] == "100.00"
    assert result.details["delta"] == "0.00"
    assert result.details["item_count"] == 2


def test_line_items_sum_failed():
    """49.99 + 49.99 = 99.98 vs subtotal 100.00 → delta 0.02 > tolerance."""
    invoice = make_invoice(
        subtotal=Decimal("100.00"),
        line_items=[
            LineItem(position=1, line_total=Decimal("49.99")),
            LineItem(position=2, line_total=Decimal("49.99")),
        ],
    )
    result = check_line_items_sum(invoice)
    assert result.outcome == ValidationOutcome.FAILED
    assert result.severity == "error"
    assert result.details["expected"] == "99.98"
    assert result.details["actual"] == "100.00"
    assert result.details["delta"] == "0.02"


def test_line_items_sum_skipped_no_subtotal():
    """Line items exist but subtotal is missing → cannot compare."""
    invoice = make_invoice(
        subtotal=None,
        line_items=[LineItem(position=1, line_total=Decimal("50.00"))],
    )
    result = check_line_items_sum(invoice)
    assert result.outcome == ValidationOutcome.SKIPPED
    assert result.details["reason"] == "invoice.subtotal is null"


# --- check_subtotal_plus_tax ------------------------------------------------


def test_subtotal_plus_tax_passed():
    """100.00 + 8.50 = 108.50."""
    invoice = make_invoice(
        subtotal=Decimal("100.00"),
        tax_amount=Decimal("8.50"),
        total_amount=Decimal("108.50"),
    )
    result = check_subtotal_plus_tax(invoice)
    assert result.outcome == ValidationOutcome.PASSED
    assert result.details["expected_total"] == "108.50"
    assert result.details["actual_total"] == "108.50"
    assert result.details["delta"] == "0.00"
    assert result.details["zero_tax"] is False


def test_subtotal_plus_tax_failed():
    """100.00 + 8.50 = 108.50 but total says 109.50 → delta 1.00."""
    invoice = make_invoice(
        subtotal=Decimal("100.00"),
        tax_amount=Decimal("8.50"),
        total_amount=Decimal("109.50"),
    )
    result = check_subtotal_plus_tax(invoice)
    assert result.outcome == ValidationOutcome.FAILED
    assert result.severity == "error"
    assert result.details["expected_total"] == "108.50"
    assert result.details["actual_total"] == "109.50"
    assert result.details["delta"] == "1.00"


def test_subtotal_plus_tax_zero_tax_is_passed():
    """Zero tax is legitimate: 100.00 + 0.00 = 100.00 → passed with note."""
    invoice = make_invoice(
        subtotal=Decimal("100.00"),
        tax_amount=Decimal("0.00"),
        total_amount=Decimal("100.00"),
    )
    result = check_subtotal_plus_tax(invoice)
    assert result.outcome == ValidationOutcome.PASSED
    assert result.details["zero_tax"] is True


def test_subtotal_plus_tax_skipped_missing_fields():
    """total_amount missing → cannot verify the sum."""
    invoice = make_invoice(total_amount=None)
    result = check_subtotal_plus_tax(invoice)
    assert result.outcome == ValidationOutcome.SKIPPED
    assert result.details["reason"] == "missing required fields"
    assert result.details["missing"] == ["total_amount"]


# --- check_tax_rate_consistency ---------------------------------------------


def test_tax_rate_consistency_zero_tax_passed():
    """tax_amount=0 with subtotal=100 → passed, effective_rate '0'."""
    invoice = make_invoice(subtotal=Decimal("100.00"), tax_amount=Decimal("0.00"))
    result = check_tax_rate_consistency(invoice)
    assert result.outcome == ValidationOutcome.PASSED
    assert result.details["effective_rate"] == "0"


def test_tax_rate_consistency_skipped_by_default():
    """Nonzero tax → skipped: per-line tax amounts are not tracked yet."""
    invoice = make_invoice(subtotal=Decimal("100.00"), tax_amount=Decimal("8.50"))
    result = check_tax_rate_consistency(invoice)
    assert result.outcome == ValidationOutcome.SKIPPED
    assert result.details["reason"] == "per-line tax amounts not available"
    assert result.details["effective_rate"] == "0.085000"


# --- check_date_ordering ----------------------------------------------------


def test_date_ordering_passed():
    """invoice_date <= due_date."""
    invoice = make_invoice(invoice_date=date(2026, 1, 15), due_date=date(2026, 2, 14))
    result = check_date_ordering(invoice)
    assert result.outcome == ValidationOutcome.PASSED
    assert result.details["invoice_date"] == "2026-01-15"
    assert result.details["due_date"] == "2026-02-14"
    assert result.details["delta_days"] == 30


def test_date_ordering_failed():
    """due_date before invoice_date → failed with negative delta_days."""
    invoice = make_invoice(invoice_date=date(2026, 2, 14), due_date=date(2026, 1, 15))
    result = check_date_ordering(invoice)
    assert result.outcome == ValidationOutcome.FAILED
    assert result.severity == "error"
    assert result.details["delta_days"] == -30


def test_date_ordering_skipped():
    """One date is None → ordering cannot be checked."""
    invoice = make_invoice(invoice_date=date(2026, 1, 15), due_date=None)
    result = check_date_ordering(invoice)
    assert result.outcome == ValidationOutcome.SKIPPED
    assert result.details["missing"] == ["due_date"]


# --- check_currency_presence -------------------------------------------------


def test_currency_presence_warning():
    """Money fields present but currency missing → warning, not failure."""
    invoice = make_invoice(currency=None)
    result = check_currency_presence(invoice)
    assert result.outcome == ValidationOutcome.WARNING
    assert result.severity == "warn"
    assert result.details["has_subtotal"] is True


def test_currency_presence_passed():
    """Currency present."""
    invoice = make_invoice(currency="EUR")
    result = check_currency_presence(invoice)
    assert result.outcome == ValidationOutcome.PASSED
    assert result.details["currency"] == "EUR"
    assert result.rule_id == "CURRENCY_PRESENCE"


def test_currency_presence_skipped():
    """No monetary fields at all → nothing to disambiguate."""
    invoice = make_invoice(subtotal=None, tax_amount=None, total_amount=None)
    result = check_currency_presence(invoice)
    assert result.outcome == ValidationOutcome.SKIPPED
    assert result.details["reason"] == "no monetary fields"


# --- runner ------------------------------------------------------------------


def test_run_all_checks_overall_failed_when_any_failed():
    """A single failed check makes the whole report failed."""
    invoice = make_invoice(
        line_items=[
            LineItem(
                position=1,
                quantity=Decimal("2"),
                unit_price=Decimal("10.00"),
                line_total=Decimal("21.00"),
            ),
        ],
    )
    report = run_all_checks(invoice)
    assert report.overall_outcome == ValidationOutcome.FAILED
    failed = [c for c in report.checks if c.outcome == ValidationOutcome.FAILED]
    assert failed
    assert failed[0].check_name == "line_item_totals"
    assert report.summary["failed"] == len(failed)


def test_run_all_checks_overall_warning_precedence():
    """All pass except currency (warning) → overall warning, not passed."""
    invoice = make_invoice(currency=None)
    report = run_all_checks(invoice)
    assert report.overall_outcome == ValidationOutcome.WARNING
    assert report.summary["failed"] == 0
    assert report.summary["warning"] == 1
    # tax_rate_consistency is skipped for nonzero tax; warning outranks skipped.
    assert report.summary["skipped"] >= 1


def test_run_all_checks_summary_counts():
    """summary counts must match the actual outcomes, and sum to check count."""
    invoice = make_invoice(currency=None)
    report = run_all_checks(invoice)
    from app.services.validation.runner import ALL_DB_CHECKS
    assert len(report.checks) == len(ALL_CHECKS) + len(ALL_DB_CHECKS)
    assert sum(report.summary.values()) == len(report.checks)
    actual = {"passed": 0, "failed": 0, "warning": 0, "skipped": 0}
    for check in report.checks:
        actual[check.outcome.value] += 1
    assert report.summary == actual


def test_run_all_checks_details_are_json_serializable():
    """json.dumps must succeed for every check's details — pins _to_json_safe."""
    invoice = make_invoice(
        currency=None,
        line_items=[
            LineItem(
                position=1,
                quantity=Decimal("2"),
                unit_price=Decimal("10.00"),
                line_total=Decimal("21.00"),
            ),
        ],
    )
    report = run_all_checks(invoice)
    from app.services.validation.runner import ALL_DB_CHECKS
    assert len(report.checks) == len(ALL_CHECKS) + len(ALL_DB_CHECKS)
    for check in report.checks:
        json.dumps(check.details)
    # The full report must also serialize (dates and Decimals included).
    json.dumps(report.model_dump(mode="json"))

def test_rule_ids_are_unique():
    """Rule IDs must be unique across all checks."""
    inv = make_invoice()
    rule_ids = [check(inv).rule_id for check in ALL_CHECKS] + [
        check(inv, None).rule_id for check in ALL_DB_CHECKS
    ]
    assert len(set(rule_ids)) == len(ALL_CHECKS) + len(ALL_DB_CHECKS)
