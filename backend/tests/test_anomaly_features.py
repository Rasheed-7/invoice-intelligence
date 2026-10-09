"""Tests for anomaly feature extraction and per-vendor baselines.

Pure in-memory invoices: no database, no LLM.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.document import Document
from app.models.invoice import Invoice, LineItem
from app.services.anomaly_detection.features import (
    FREQUENCY_ANOMALY_THRESHOLD,
    FREQUENCY_WINDOW_DAYS,
    compute_invoice_frequency,
    compute_vendor_history_features,
    extract_invoice_features,
)
from app.services.po_matching.types import MismatchSeverity

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def make_invoice(
    *,
    total: Decimal | None = Decimal("100.00"),
    lines: list[LineItem] | None = None,
    created_at: datetime = BASE,
) -> Invoice:
    document = Document(
        filename="invoice.pdf",
        content_type="application/pdf",
        size_bytes=1024,
        sha256=uuid.uuid4().hex,
        storage_path="/storage/invoice.pdf",
    )
    invoice = Invoice(
        id=uuid.uuid4(),
        document=document,
        vendor_name="Acme",
        invoice_number="INV-1",
        currency="USD",
        total_amount=total,
        created_at=created_at,
    )
    invoice.line_items = lines or []
    return invoice


def item(
    position: int = 1,
    description: str = "Widget",
    quantity: Decimal = Decimal("1"),
    unit_price: Decimal | None = Decimal("10.00"),
    line_total: Decimal | None = Decimal("10.00"),
) -> LineItem:
    return LineItem(
        position=position,
        description=description,
        quantity=quantity,
        unit_price=unit_price,
        line_total=line_total,
    )


# --- feature extraction -----------------------------------------------------


def test_extract_invoice_features_computes_averages() -> None:
    """Averages are the arithmetic mean of the present values."""
    invoice = make_invoice(
        lines=[
            item(1, unit_price=Decimal("10.00"), line_total=Decimal("10.00")),
            item(2, unit_price=Decimal("20.00"), line_total=Decimal("40.00")),
        ]
    )

    features = extract_invoice_features(invoice)

    assert features["total_amount"] == Decimal("100.00")
    assert features["item_count"] == Decimal(2)
    assert features["avg_unit_price"] == Decimal("15.00")
    assert features["avg_line_total"] == Decimal("25.00")


def test_extract_invoice_features_handles_missing_values() -> None:
    """Missing unit prices are excluded from the mean, not treated as zero."""
    invoice = make_invoice(
        lines=[
            item(1, unit_price=Decimal("10.00"), line_total=Decimal("10.00")),
            item(2, unit_price=None, line_total=Decimal("40.00")),
        ]
    )

    features = extract_invoice_features(invoice)

    assert features["avg_unit_price"] == Decimal("10.00")
    assert features["avg_line_total"] == Decimal("25.00")


def test_extract_invoice_features_returns_none_for_empty() -> None:
    """No line items means no averages - None, never zero."""
    features = extract_invoice_features(make_invoice(lines=[]))

    assert features["item_count"] == Decimal(0)
    assert features["avg_unit_price"] is None
    assert features["avg_line_total"] is None


def test_extract_invoice_features_preserves_missing_total() -> None:
    """An un-extracted invoice reports no total rather than a zero one."""
    features = extract_invoice_features(make_invoice(total=None))

    assert features["total_amount"] is None


# --- historical baselines ---------------------------------------------------


def test_compute_vendor_history_features_population_stddev() -> None:
    """Exact population stddev for three known values: 98, 100, 102 -> 1.6330."""
    invoices = [
        make_invoice(total=value, lines=[item()])
        for value in (Decimal("98"), Decimal("100"), Decimal("102"))
    ]

    history = compute_vendor_history_features(invoices)

    mean, std = history["total_amount"]
    assert mean == Decimal("100")
    expected = ((Decimal("98") - 100) ** 2 + (100 - 100) ** 2 + (Decimal("102") - 100) ** 2)
    expected = expected / Decimal(3)
    assert std == expected.sqrt()


def test_compute_vendor_history_features_skips_features_with_one_sample() -> None:
    """A single observation has no stddev, so the feature is omitted."""
    only_one_with_lines = make_invoice(total=None, lines=[item()])

    history = compute_vendor_history_features([only_one_with_lines])

    # total_amount is absent on the only invoice, and item_count has one sample.
    assert "total_amount" not in history
    assert "item_count" not in history


def test_compute_vendor_history_features_skips_features_with_zero_std() -> None:
    """A perfectly uniform history yields std 0; the scorer must skip it."""
    invoices = [make_invoice(total=Decimal("100.00"), lines=[item()]) for _ in range(5)]

    history = compute_vendor_history_features(invoices)

    mean, std = history["total_amount"]
    assert mean == Decimal("100.00")
    assert std == Decimal(0)


def test_compute_vendor_history_features_omits_absent_feature() -> None:
    """A feature absent from every invoice never appears in the baseline."""
    invoices = [make_invoice(total=Decimal("100.00"), lines=[]) for _ in range(5)]

    history = compute_vendor_history_features(invoices)

    assert "total_amount" in history
    assert "avg_unit_price" not in history


# --- invoice frequency ------------------------------------------------------


def test_invoice_frequency_contribution_threshold() -> None:
    """Below the threshold is silent; at it, a HIGH contribution appears."""
    current = make_invoice(created_at=BASE + timedelta(days=100))
    # Both sets sit inside the trailing 30-day window before `current`.
    under = [
        make_invoice(created_at=BASE + timedelta(days=80 + i)) for i in range(9)
    ]
    at = [make_invoice(created_at=BASE + timedelta(days=80 + i)) for i in range(10)]

    assert FREQUENCY_ANOMALY_THRESHOLD == 10
    assert compute_invoice_frequency(under, current) is None

    contribution = compute_invoice_frequency(at, current)
    assert contribution is not None
    assert contribution.feature == "invoice_frequency"
    assert contribution.severity == MismatchSeverity.HIGH
    assert contribution.current_value == "10"
    assert "10 invoices" in contribution.note
    assert f"last {FREQUENCY_WINDOW_DAYS} days" in contribution.note


def test_invoice_frequency_only_counts_30_day_window() -> None:
    """Invoices older than the window do not count toward a burst."""
    current = make_invoice(created_at=BASE + timedelta(days=200))
    old = [
        make_invoice(created_at=BASE + timedelta(days=day))
        for day in range(0, 120, 4)
    ]
    recent = [
        make_invoice(created_at=BASE + timedelta(days=180 + i)) for i in range(9)
    ]

    # 30 recent invoices, but only 9 fall inside the trailing 30-day window.
    assert compute_invoice_frequency(old + recent, current) is None

    inside = [
        make_invoice(created_at=BASE + timedelta(days=175 + i)) for i in range(10)
    ]
    assert compute_invoice_frequency(inside, current) is not None


def test_invoice_frequency_ignores_the_current_invoice_window_edge() -> None:
    """The current invoice's own timestamp is an exclusive upper bound."""
    current = make_invoice(created_at=BASE + timedelta(days=50))
    same_moment = [make_invoice(created_at=current.created_at) for _ in range(10)]

    assert compute_invoice_frequency(same_moment, current) is None