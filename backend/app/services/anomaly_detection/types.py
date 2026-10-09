"""Result types for invoice anomaly detection.

Every field is produced by arithmetic over :class:`~app.models.invoice.Invoice`
objects. Nothing here is inferred by a model and nothing is a placeholder.
"""

import enum
import uuid

from pydantic import BaseModel, ConfigDict

from app.services.po_matching.types import MismatchSeverity


class AnomalyVerdict(str, enum.Enum):
    NORMAL = "normal"
    MILD_ANOMALY = "mild_anomaly"
    STRONG_ANOMALY = "strong_anomaly"
    INSUFFICIENT_HISTORY = "insufficient_history"


class AnomalyMethod(str, enum.Enum):
    STATISTICAL_BASELINE = "statistical_baseline"
    ISOLATION_FOREST = "isolation_forest"
    NONE = "none"


class FeatureContribution(BaseModel):
    """Why one feature looked anomalous, with the numbers behind it."""

    model_config = ConfigDict(extra="forbid")

    #: "total_amount", "avg_unit_price", "avg_line_total", "item_count",
    #: "invoice_frequency".
    feature: str
    #: Decimal rendered as a string, so precision survives JSON.
    current_value: str
    historical_mean: str | None
    historical_std: str | None
    z_score: str | None
    severity: MismatchSeverity
    #: Human-readable explanation, e.g. "3.2 sigma above the vendor mean".
    note: str


class AnomalyReport(BaseModel):
    """The outcome of anomaly analysis for one invoice."""

    model_config = ConfigDict(extra="forbid")

    invoice_id: uuid.UUID
    vendor_name: str | None
    verdict: AnomalyVerdict
    method: AnomalyMethod
    #: 0.0-1.0. None when there is not enough history to score honestly, and
    #: always None for Isolation Forest, which has no calibrated probability.
    score: float | None
    #: Isolation Forest's raw decision_function value. Negative means more
    # isolated from the training corpus; it is a relative ranking for this
    #: corpus, not a probability. None when the forest did not run.
    raw_decision: float | None
    contributions: list[FeatureContribution]
    #: Prior invoices for this vendor used to build the baseline.
    history_size: int
    notes: list[str]