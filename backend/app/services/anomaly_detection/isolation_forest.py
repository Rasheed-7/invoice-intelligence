"""Isolation Forest analysis for vendors with deep enough history.

Provides a stronger multivariate signal than the z-score baseline, at the cost
of explainability: a forest can say an invoice is unusual but not *why*. The
report therefore pairs the forest's verdict with attribution borrowed from the
statistical baseline, and says so explicitly rather than inventing reasons.

No score is produced. ``decision_function`` is a relative, corpus-dependent
ranking value - not a probability - and rescaling it into 0..1 produced a
number that looked meaningful while being arbitrary. Measured on 25 clustered
invoices a 40x amount outlier landed at -0.094 against an inlier's +0.017: the
ordering was reliable, the mapped "score" was not. Thresholds therefore
operate on the raw decision value, and ``score`` stays None.
"""

from app.models.invoice import Invoice
from app.services.anomaly_detection.features import (
    MIN_HISTORY_FOR_ISOLATION_FOREST,
    extract_invoice_features,
)
from app.services.anomaly_detection.statistical import analyze_statistical
from app.services.anomaly_detection.types import (
    AnomalyMethod,
    AnomalyReport,
    AnomalyVerdict,
    FeatureContribution,
)

__all__ = [
    "IF_DECISION_MILD",
    "IF_DECISION_STRONG",
    "NO_SCORE_NOTE",
    "analyze_isolation_forest",
]

#: Raw decision_function value below which an invoice is strongly anomalous.
#: More negative means more isolated from the training corpus.
IF_DECISION_STRONG = -0.15

#: Raw decision_function value below which an invoice is mildly anomalous.
IF_DECISION_MILD = -0.05

#: Seed fixed so the same corpus always produces the same forest. Isolation
#: Forest otherwise samples randomly at every fit.
RANDOM_STATE = 42

#: Note attached to contributions that the forest could not produce itself.
ATTRIBUTION_NOTE = (
    "attribution from statistical baseline "
    "(Isolation Forest does not provide per-feature reasons)"
)

#: Explains the absent score, so a consumer never has to wonder whether it
#: was lost in transit.
NO_SCORE_NOTE = (
    "Isolation Forest does not produce calibrated probabilities; score is not "
    "reported. The decision value is included below."
)


def _complete_matrix(
    invoices: list[Invoice],
) -> list[list[float]] | None:
    """Rows of fully-populated features, or None when too few remain.

    A row with any missing feature cannot be scaled, so it is dropped. Dropping
    is why a corpus can pass the invoice-count gate and still fall back: 25
    invoices where 24 have no ``total_amount`` is not 25 usable samples.
    """
    rows: list[list[float]] = []
    for invoice in invoices:
        features = extract_invoice_features(invoice)
        if any(value is None for value in features.values()):
            continue
        rows.append([float(value) for value in features.values()])
    return rows


def _current_row(current: Invoice) -> list[float] | None:
    """The current invoice as a feature row, or None if a feature is missing."""
    features = extract_invoice_features(current)
    if any(value is None for value in features.values()):
        return None
    return [float(value) for value in features.values()]


def analyze_isolation_forest(
    current: Invoice, prior_invoices: list[Invoice]
) -> AnomalyReport | None:
    """Score an invoice against a vendor's history with an Isolation Forest.

    Args:
        current: The invoice under evaluation.
        prior_invoices: This vendor's prior invoices.

    Returns:
        An :class:`AnomalyReport`, or None when there is not enough usable
        history - the caller is expected to fall back to the statistical
        baseline rather than treat None as "normal".
    """
    if len(prior_invoices) < MIN_HISTORY_FOR_ISOLATION_FOREST:
        return None

    matrix = _complete_matrix(prior_invoices)
    if matrix is None or len(matrix) < MIN_HISTORY_FOR_ISOLATION_FOREST:
        return None

    row = _current_row(current)
    if row is None:
        return None

    # Imported here so the module can be imported (and tested) without sklearn
    # present; it is a hard dependency of the project either way.
    from sklearn.ensemble import IsolationForest

    forest = IsolationForest(
        n_estimators=100, contamination="auto", random_state=RANDOM_STATE
    )
    forest.fit(matrix)

    decision = float(forest.decision_function([row])[0])

    # decision_function is negative for anomalies, 0 at the boundary, positive
    # for inliers. Thresholded directly rather than rescaled: it is a relative
    # ranking value for this corpus, not a probability.
    if decision < IF_DECISION_STRONG:
        verdict = AnomalyVerdict.STRONG_ANOMALY
    elif decision < IF_DECISION_MILD:
        verdict = AnomalyVerdict.MILD_ANOMALY
    else:
        verdict = AnomalyVerdict.NORMAL

    # The forest cannot attribute. Borrow the baseline's reasoning when it has
    # any, clearly labelled, and otherwise say so rather than guessing.
    baseline = analyze_statistical(current, prior_invoices)
    contributions: list[FeatureContribution] = []
    notes: list[str] = [NO_SCORE_NOTE]

    if baseline.contributions:
        for contribution in baseline.contributions:
            contributions.append(
                contribution.model_copy(
                    update={"note": f"{contribution.note} ({ATTRIBUTION_NOTE})"}
                )
            )
        notes.append(ATTRIBUTION_NOTE)
    else:
        notes.append(
            "no per-feature attribution available: Isolation Forest scores a "
            "invoice as a whole and the statistical baseline found no feature "
            "outside its thresholds"
        )

    notes.append(f"isolation forest fitted on {len(matrix)} prior invoices")

    return AnomalyReport(
        invoice_id=current.id,
        vendor_name=current.vendor_name,
        verdict=verdict,
        method=AnomalyMethod.ISOLATION_FOREST,
        # No score: an uncalibrated rescaling of decision_function is not a
        # probability and reporting one would misrepresent the evidence.
        score=None,
        raw_decision=decision,
        contributions=contributions,
        history_size=len(prior_invoices),
        notes=notes,
    )