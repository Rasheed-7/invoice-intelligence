"""Review case API schemas.

These sit between the persisted ``ReviewCase`` row and the HTTP layer. They
mirror ``app.services.review.types.ReviewCaseSummary`` and add the invoice
summary a reviewer needs to make a decision without a second request.

Deliberate choice: a case carries only a six-field invoice summary, never the
full invoice. A reviewer's queue is a list, and embedding line items and the
whole document per case would make the payload grow with every line of every
invoice on the page. Callers that need the full invoice follow
``GET /invoices/{id}``, which already exists.
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.review import ReviewCase

__all__ = [
    "InvoiceSummary",
    "ReviewCaseResponse",
    "ReviewListResponse",
    "ReviewResolutionRequest",
]

#: The decision vocabulary. Spelled as a Literal rather than reusing the
#: ``ReviewDecision`` enum so the API contract is fixed here: adding a decision
#: to the enum later must not silently widen what the endpoint accepts.
ReviewDecisionLiteral = Literal["approve", "reject", "dismiss"]


class InvoiceSummary(BaseModel):
    """The minimum invoice context needed to judge a review case."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    vendor_name: str | None
    invoice_number: str | None
    total_amount: Decimal | None
    currency: str | None
    status: str


class ReviewCaseResponse(BaseModel):
    """A review case plus a summary of the invoice it belongs to."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    invoice_id: UUID
    #: A ReviewStatus value: pending / in_progress / resolved / dismissed.
    status: str
    assigned_to: UUID | None
    reason: str
    resolution: str | None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    invoice_summary: InvoiceSummary | None

    @classmethod
    def from_orm_case(cls, case: ReviewCase) -> "ReviewCaseResponse":
        """Build a response from a ReviewCase row.

        Written by hand rather than via ``from_attributes`` because the nested
        summary is computed here, not a column. ``invoice_summary`` is Optional
        because the relationship is only populated when the caller eager-loaded
        it; returning ``None`` beats raising a lazy-load error from a list
        endpoint.
        """
        invoice = case.invoice
        summary = (
            InvoiceSummary(
                id=invoice.id,
                vendor_name=invoice.vendor_name,
                invoice_number=invoice.invoice_number,
                total_amount=invoice.total_amount,
                currency=invoice.currency,
                status=invoice.status.value,
            )
            if invoice is not None
            else None
        )
        return cls(
            id=case.id,
            invoice_id=case.invoice_id,
            status=case.status.value,
            assigned_to=case.assigned_to,
            reason=case.reason,
            resolution=case.resolution,
            created_at=case.created_at,
            updated_at=case.updated_at,
            resolved_at=case.resolved_at,
            invoice_summary=summary,
        )


class ReviewResolutionRequest(BaseModel):
    """Body for resolving a review case."""

    model_config = ConfigDict(extra="forbid")

    decision: ReviewDecisionLiteral
    reviewer_id: UUID
    resolution: str = Field(min_length=1)

    @field_validator("resolution")
    @classmethod
    def require_non_blank(cls, value: str) -> str:
        """Reject whitespace-only notes.

        ``min_length=1`` alone accepts ``" "``, which would let a decision be
        recorded with no stated reasoning. Rejecting it here means the caller gets
        a 422 with a field-level message rather than a 500 from the service.
        """
        if not value.strip():
            raise ValueError("resolution must not be blank")
        return value


class ReviewListResponse(BaseModel):
    """A page of review cases."""

    model_config = ConfigDict(extra="forbid")

    items: list[ReviewCaseResponse]
    #: Total matching the filter, not the page size, so a caller can compute the
    #: number of pages.
    total: int
    limit: int
    offset: int