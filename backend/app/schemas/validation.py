"""Validation read schemas (persisted check results).

These mirror the rows written by ``run_validation``: one entry per check with
its outcome, message, severity, and the flattened evidence dict.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

__all__ = [
    "ValidationCheckResponse",
    "ValidationSummaryResponse",
]


class ValidationCheckResponse(BaseModel):
    check_name: str
    rule_id: str
    status: Literal["passed", "failed", "warning", "skipped"]
    message: str
    severity: Literal["info", "warn", "error"]
    details: dict

    model_config = ConfigDict(from_attributes=True)


class ValidationSummaryResponse(BaseModel):
    invoice_id: UUID
    overall_status: Literal["passed", "failed", "warning", "skipped"]
    checks: list[ValidationCheckResponse]
    counts: dict[str, int]  # {"passed": N, "failed": N, "warning": N, "skipped": N}
