"""Request/response types for human review of flagged invoices.

Separate from ``app.models.review``, which owns persistence, and from
``app.models.enums.ReviewStatus``, which owns the stored status vocabulary. The
two are deliberately distinct: ``ReviewStatus`` records where a case sits in the
workflow, while :class:`ReviewDecision` records what a human concluded about the
invoice. A case can be ``RESOLVED`` because the invoice was approved, rejected,
or because the case was dismissed with no action needed - the status alone
cannot tell those apart, the decision can.

Kept free of FastAPI imports so the review layer stays testable without HTTP,
matching ``validation_service`` and ``po_matching_service``.
"""

import enum
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "ReviewCaseSummary",
    "ReviewDecision",
    "ReviewResolution",
]


class ReviewDecision(str, enum.Enum):
    """What a reviewer concluded about the invoice behind a case."""

    APPROVE = "approve"
    REJECT = "reject"
    #: Closes the case as "no action needed". Not a decision about the invoice,
    #: so it leaves the invoice's own status alone.
    DISMISS = "dismiss"


class ReviewCaseSummary(BaseModel):
    """A review case as returned by list/read endpoints."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    invoice_id: uuid.UUID
    #: A ReviewStatus value: pending / in_progress / resolved / dismissed.
    status: str
    assigned_to: uuid.UUID | None
    reason: str
    resolution: str | None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None


class ReviewResolution(BaseModel):
    """A reviewer's decision plus the note explaining it."""

    model_config = ConfigDict(extra="forbid")

    decision: ReviewDecision
    reviewer_id: uuid.UUID
    #: The human's note. Required and non-blank: a decision with no stated
    #: reasoning is not auditable, so the service rejects it too - this validator
    #: is the early, friendly check, not the only guard.
    resolution: str = Field(min_length=1)

    @field_validator("resolution")
    @classmethod
    def require_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("resolution must not be blank")
        return value