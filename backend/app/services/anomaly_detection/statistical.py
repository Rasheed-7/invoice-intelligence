"""Z-score baseline analysis against a vendor's own history.

This is the default method: it works on small samples and, unlike Isolation
Forest, can say *which* feature deviated and by how much.
"""

from decimal import Decimal

from app.models.invoice import Invoice
from app.services.anomaly_detection.features import (
    MIN_HISTORY_FOR_BASELINE,
    compute_invoice_frequency,
    compute_vendor_history_features,
    extract_invoice_features,
)
from app.services.anomaly_detection.types import (
    AnomalyMethod,
    AnomalyReport,
    AnomalyVerdict,
    FeatureContribution,
)
from app.services.po_matching.types import MismatchSeverity

__all__ = ["Z_SCORE_MILD", "Z_SCORE_STRONG", "analyze_statistical"]

#: Absolute z-score at which a feature is worth reporting.
Z_SCORE_MILD = Decimal("2.0")

#: Absolute z-score at which a feature is alarming.
Z_SCORE_STRONG = Decimal("3.0")

#: Score saturates at twice the strong threshold, so a 6-sigma outlier is as
#: anomalous as the scale can express.
_SCORE_DENOMINATOR = float(Z_SCORE_STRONG * 2)


def _insufficient(current: Invoice, history_size: int) -> AnomalyReport:
    """Report honestly that there is no baseline, rather than scoring anyway."""
    return AnomalyReport(
        invoice_id=current.id,
        vendor_name=current.vendor_name,
        verdict=AnomalyVerdict.INSUFFICIENT_HISTORY,
        method=AnomalyMethod.NONE,
        score=None,
        raw_decision=None,
        contributions=[],
        history_size=history_size,
        notes=[
            f"vendor has {history_size} prior invoices; "
            f"minimum for baseline is {MIN_HISTORY_FOR_BASELINE}"
        ],
    )


def analyze_statistical(
    current: Invoice, prior_invoices: list[Invoice]
) -> AnomalyReport:
    """Compare an invoice to its vendor's history feature by feature.

    Returns:
        An :class:`AnomalyReport`. ``INSUFFICIENT_HISTORY`` with a ``None``
        score when there are too few prior invoices to have an opinion.
    """
    history_size = len(prior_invoices)
    if history_size < MIN_HISTORY_FOR_BASELINE:
        return _insufficient(current, history_size)

    current_features = extract_invoice_features(current)
    history = compute_vendor_history_features(prior_invoices)

    contributions: list[FeatureContribution] = []
    notes: list[str] = []

    for name, value in current_features.items():
        if value is None or name not in history:
            continue
        mean, std = history[name]

        # A zero standard deviation means every prior invoice had the same
        # value. Any deviation is then infinitely many sigma, which is not a
        # measurement - it is an artefact of a degenerate baseline. Skip it.
        if std == 0:
            continue

        z = (value - mean) / std
        magnitude = abs(z)
        if magnitude < Z_SCORE_MILD:
            continue

        severity = (
            MismatchSeverity.HIGH if magnitude >= Z_SCORE_STRONG else MismatchSeverity.MEDIUM
        )
        direction = "above" if z > 0 else "below"
        note = (
            f"{name} is {z.quantize(Decimal('0.1'))} sigma {direction} "
            f"the vendor mean of {mean.quantize(Decimal('0.01'))}"
        )

        contributions.append(
            FeatureContribution(
                feature=name,
                current_value=str(value),
                historical_mean=str(mean),
                historical_std=str(std),
                z_score=str(z.quantize(Decimal("0.001"))),
                severity=severity,
                note=note,
            )
        )
        notes.append(note)

    frequency = compute_invoice_frequency(prior_invoices, current)
    if frequency is not None:
        contributions.append(frequency)
        notes.append(frequency.note)

    if not contributions:
        verdict = AnomalyVerdict.NORMAL
        score: float | None = None
    else:
        z_scores = [
            abs(Decimal(c.z_score)) for c in contributions if c.z_score is not None
        ]
        # Only non-z signals firing (e.g. an invoice-frequency burst, which has no
        # baseline to deviate from) leaves the score as None. A 0.0 would read
        # as "perfectly normal" next to a STRONG_ANOMALY verdict, so we report
        # no score rather than manufacture one - the verdict still stands on
        # the evidence.
        score = min(1.0, float(max(z_scores)) / _SCORE_DENOMINATOR) if z_scores else None
        verdict = (
            AnomalyVerdict.STRONG_ANOMALY
            if any(c.severity == MismatchSeverity.HIGH for c in contributions)
            else AnomalyVerdict.MILD_ANOMALY
        )

    notes.append(f"baseline from {history_size} prior invoices")

    return AnomalyReport(
        invoice_id=current.id,
        vendor_name=current.vendor_name,
        verdict=verdict,
        method=AnomalyMethod.STATISTICAL_BASELINE,
        score=score,
        # The baseline scores with z-scores, so it has no forest decision value.
        raw_decision=None,
        contributions=contributions,
        history_size=history_size,
        notes=notes,
    )