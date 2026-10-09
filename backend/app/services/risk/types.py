"""Result types for the MVP risk model.

These model the OUTPUT of risk assessment: a bounded score with per-finding
attribution. They are separate from the persistence layer's AnalysisStatus
enum so the risk layer can evolve without touching it, matching the pattern
used by the validation and anomaly type modules.
"""

import enum
import uuid
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "RiskLevel",
    "RecommendedAction",
    "RiskContributor",
    "RiskAssessment",
]


class RiskLevel(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RecommendedAction(str, enum.Enum):
    AUTO_APPROVE = "auto_approve"
    REVIEW = "review"
    REJECT = "reject"


class RiskContributor(BaseModel):
    """One finding's bounded contribution to the risk score."""

    model_config = ConfigDict(extra="forbid")

    #: Stable machine-readable identifier, e.g. "duplicate_confirmed".
    rule_id: str
    #: One of "duplicate", "po_match", "validation", "anomaly", "policy",
    #: "extraction", "completeness".
    category: str
    #: Points added by this contributor, always 0..cap.
    points: int = Field(ge=0)
    #: The per-contributor ceiling from :mod:`app.services.risk.weights`.
    cap: int = Field(ge=0)
    severity: Literal["info", "warn", "error"]
    #: One-sentence human-readable summary.
    message: str
    #: Structured payload (JSON-safe).
    evidence: dict[str, Any]

    @field_validator("evidence", mode="before")
    @classmethod
    def ensure_json_safe(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Coerce evidence values to JSON-safe equivalents.

        Money arrives as Decimal; it is rendered as a string so precision
        survives the JSON round-trip (mirrors ``validation.types``).
        """
        return {key: _to_json_safe(val) for key, val in value.items()}


class RiskAssessment(BaseModel):
    """The combined risk outcome for one invoice."""

    model_config = ConfigDict(extra="forbid")

    invoice_id: uuid.UUID
    #: Final score after the global cap, always 0..100.
    score: int = Field(ge=0, le=100)
    level: RiskLevel
    recommended_action: RecommendedAction
    contributors: list[RiskContributor]
    #: Sum of contributor points BEFORE the global cap; may exceed 100.
    contributing_total: int = Field(ge=0)
    #: True when the uncapped sum exceeded MAX_SCORE and was clamped.
    caps_applied: bool
    #: Disclaimers, e.g. "MVP model, not empirically calibrated".
    notes: list[str]


def _to_json_safe(value: Any) -> Any:
    """Convert a value to a JSON-serializable form.

    Decimal -> str (preserves precision), date/datetime -> ISO string,
    UUID -> str, dict/list -> recurse, else pass through.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "isoformat"):  # date, datetime
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _to_json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_json_safe(item) for item in value]
    return value
