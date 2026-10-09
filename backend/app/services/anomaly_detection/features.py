"""Feature extraction and per-vendor historical baselines.

Money is :class:`~decimal.Decimal` throughout. Float arithmetic on currency
accumulates error, and a z-score computed from a float total is quietly wrong
in the third decimal place - which is exactly the magnitude that matters when
deciding whether an invoice is unusual.
"""

from datetime import timedelta
from decimal import Decimal

from app.models.invoice import Invoice
from app.services.anomaly_detection.types import FeatureContribution
from app.services.po_matching.types import MismatchSeverity

__all__ = [
    "FREQUENCY_ANOMALY_THRESHOLD",
    "FREQUENCY_WINDOW_DAYS",
    "MIN_HISTORY_FOR_BASELINE",
    "MIN_HISTORY_FOR_ISOLATION_FOREST",
    "compute_invoice_frequency",
    "compute_vendor_history_features",
    "extract_invoice_features",
]

#: Below this many prior invoices there is no baseline to deviate from, and
#: the detector says so instead of inventing a score.
MIN_HISTORY_FOR_BASELINE = 5

#: Isolation Forest needs a real sample before its multivariate model means
#: anything.
MIN_HISTORY_FOR_ISOLATION_FOREST = 20

#: Prior invoices inside this window that count as "recent submission activity".
FREQUENCY_WINDOW_DAYS = 30

#: Invoice count inside the window that counts as a burst.
FREQUENCY_ANOMALY_THRESHOLD = 10


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values) / Decimal(len(values))


def extract_invoice_features(invoice: Invoice) -> dict[str, Decimal | None]:
    """Derive comparable numeric features from a single invoice.

    Features are None rather than zero when the source field is missing. Zero is
    a meaningful amount; "absent" is not, and conflating them would make an
    un-extracted invoice look like a free one.
    """
    items = list(invoice.line_items)

    unit_prices = [i.unit_price for i in items if i.unit_price is not None]
    line_totals = [i.line_total for i in items if i.line_total is not None]

    return {
        "total_amount": invoice.total_amount,
        "item_count": Decimal(len(items)),
        "avg_unit_price": _mean(unit_prices) if unit_prices else None,
        "avg_line_total": _mean(line_totals) if line_totals else None,
    }


def _population_stddev(values: list[Decimal], mean: Decimal) -> Decimal:
    """Population standard deviation.

    Written out rather than using ``statistics.pstdev``, which rejects
    Decimal. Population (not sample) deviation is correct here: the prior
    invoices *are* the full set we are comparing against, not a sample drawn
    from a larger population.
    """
    variance = sum((value - mean) ** 2 for value in values) / Decimal(len(values))
    return variance.sqrt()


def compute_vendor_history_features(
    prior_invoices: list[Invoice],
) -> dict[str, tuple[Decimal, Decimal]]:
    """Mean and population stddev per feature across a vendor's prior invoices.

    Features with fewer than two data points are skipped entirely: a single
    observation has a stddev of zero, which would make every later z-score a
    division by zero rather than a real measurement.
    """
    collected: dict[str, list[Decimal]] = {}
    for invoice in prior_invoices:
        for name, value in extract_invoice_features(invoice).items():
            if value is not None:
                collected.setdefault(name, []).append(value)

    history: dict[str, tuple[Decimal, Decimal]] = {}
    for name, values in collected.items():
        if len(values) < 2:
            continue
        mean = _mean(values)
        history[name] = (mean, _population_stddev(values, mean))

    return history


def compute_invoice_frequency(
    prior_invoices: list[Invoice],
    current: Invoice,
) -> FeatureContribution | None:
    """Flag a vendor that has submitted an unusual number of invoices recently.

    Counting is restricted to a trailing window so an old burst does not make a
    currently-quiet vendor look busy forever.
    """
    if current.created_at is None:
        return None

    window_start = current.created_at - timedelta(days=FREQUENCY_WINDOW_DAYS)
    recent = [
        invoice
        for invoice in prior_invoices
        if invoice.created_at is not None
        and window_start <= invoice.created_at < current.created_at
    ]
    count = len(recent)

    if count < FREQUENCY_ANOMALY_THRESHOLD:
        return None

    return FeatureContribution(
        feature="invoice_frequency",
        current_value=str(count),
        historical_mean=None,
        historical_std=None,
        z_score=None,
        severity=MismatchSeverity.HIGH,
        note=f"vendor submitted {count} invoices in the last {FREQUENCY_WINDOW_DAYS} days",
    )