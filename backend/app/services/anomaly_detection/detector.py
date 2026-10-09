"""Anomaly detection orchestration.

The statistical baseline is the default because it produces per-feature
attribution and works with small samples. Isolation Forest activates at 20+
prior invoices and provides a stronger multivariate signal but no per-feature
reasons - its report combines the forest's decision value with attribution
borrowed from the baseline. Below the baseline threshold, INSUFFICIENT_HISTORY
is returned explicitly - never a fabricated score.

When both run, the verdict is the more severe of the two rather than the
forest's alone. The forest scores an invoice as a single unit and is blind to
signals the baseline can see, such as a burst of recent submissions; letting it
silently override a stronger baseline finding would discard real evidence.
"""

from app.models.invoice import Invoice
from app.services.anomaly_detection.isolation_forest import analyze_isolation_forest
from app.services.anomaly_detection.statistical import analyze_statistical
from app.services.anomaly_detection.types import AnomalyReport, AnomalyVerdict

__all__ = ["detect_anomaly", "max_severity"]

#: Severity ordering. INSUFFICIENT_HISTORY is lowest: it is the *absence* of
#: evidence, so it must never outrank a positive finding.
_SEVERITY_ORDER: tuple[AnomalyVerdict, ...] = (
    AnomalyVerdict.INSUFFICIENT_HISTORY,
    AnomalyVerdict.NORMAL,
    AnomalyVerdict.MILD_ANOMALY,
    AnomalyVerdict.STRONG_ANOMALY,
)


def max_severity(a: AnomalyVerdict, b: AnomalyVerdict) -> AnomalyVerdict:
    """Return whichever verdict carries more evidence behind it."""
    return a if _SEVERITY_ORDER.index(a) >= _SEVERITY_ORDER.index(b) else b


def detect_anomaly(current: Invoice, prior_invoices: list[Invoice]) -> AnomalyReport:
    """Analyse an invoice against its vendor's history.

    The baseline always runs: it is the only method that can explain itself,
    and its verdict participates in the final call even when the forest does.
    A None from the forest never means "normal" - it only ever means "use the
    other method".
    """
    baseline = analyze_statistical(current, prior_invoices)
    forest = analyze_isolation_forest(current, prior_invoices)

    if forest is None:
        return baseline

    combined = max_severity(forest.verdict, baseline.verdict)

    # The forest report already carries the baseline's contributions (it borrows
    # them for attribution) and the forest's raw_decision; only the verdict
    # needs reconciling. When they agree, the copy is a no-op.
    return forest.model_copy(update={"verdict": combined})