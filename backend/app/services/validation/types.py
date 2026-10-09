"""Result types and constants for invoice validation checks.

These types model the OUTPUT of validation checks. They are separate from the
persistence layer's AnalysisStatus enum so that the check layer can evolve
independently. Mapping to AnalysisResult happens in the persistence step (15-3).
"""

import enum
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

__all__ = [
    "ValidationOutcome",
    "CheckResult",
    "ValidationReport",
    "MONEY_TOLERANCE",
]


# Absolute tolerance for money equality comparisons.
# Vendors use different rounding rules; one cent is the smallest currency unit
# we care about. Tighter tolerances produce false positives on real invoices.
MONEY_TOLERANCE: Decimal = Decimal("0.01")


class ValidationOutcome(str, enum.Enum):
    """Outcome of a single validation check.

    Separate from AnalysisStatus (the DB enum) so validation logic can evolve
    without touching the persistence layer.
    """

    PASSED = "passed"
    FAILED = "failed"
    WARNING = "warning"
    SKIPPED = "skipped"


class CheckResult(BaseModel):
    """Result of a single validation check.

    Deterministic and self-contained: every field needed to understand the
    outcome is present. No external lookups required.
    """

    check_name: str  # e.g. "subtotal_plus_tax"
    rule_id: str
    outcome: ValidationOutcome
    message: str  # one-sentence human-readable summary
    details: dict[str, Any]  # structured evidence (JSON-serializable)
    severity: Literal["info", "warn", "error"]

    @field_validator("details", mode="before")
    @classmethod
    def ensure_json_safe(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Convert non-serializable types to JSON-safe equivalents."""
        return {k: _to_json_safe(v) for k, v in value.items()}


class ValidationReport(BaseModel):
    """Complete validation report for an invoice.

    Aggregates all check results with a derived overall outcome and summary stats.

    Derivation rule (from the checks list): failed if any check failed,
    else warning if any warning, else skipped if all skipped, else passed.
    """

    # None for invoices built in memory before flush (SQLAlchemy's PK default
    # fires at INSERT); persisted invoices always carry a real id.
    invoice_id: UUID | None
    checks: list[CheckResult]
    overall_outcome: ValidationOutcome = ValidationOutcome.PASSED
    summary: dict[str, int] = Field(
        default_factory=lambda: {"passed": 0, "failed": 0, "warning": 0, "skipped": 0}
    )

    @model_validator(mode="after")
    def _derive_overall_and_summary(self) -> "ValidationReport":
        """Derive overall_outcome and summary counts from the check list."""
        counts = {"passed": 0, "failed": 0, "warning": 0, "skipped": 0}
        for check in self.checks:
            counts[check.outcome.value] += 1

        if counts["failed"] > 0:
            overall = ValidationOutcome.FAILED
        elif counts["warning"] > 0:
            overall = ValidationOutcome.WARNING
        elif counts["passed"] == 0 and counts["skipped"] > 0:
            overall = ValidationOutcome.SKIPPED
        else:
            overall = ValidationOutcome.PASSED

        self.overall_outcome = overall
        self.summary = counts
        return self


def _to_json_safe(value: Any) -> Any:
    """Convert a value to a JSON-serializable form.

    Decimal → str (preserves precision), date/datetime → ISO string,
    UUID → str, dict/list → recurse, else pass through.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (UUID,)):
        return str(value)
    if hasattr(value, "isoformat"):  # date, datetime
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _to_json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_json_safe(item) for item in value]
    return value
