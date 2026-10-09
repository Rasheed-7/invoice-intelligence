"""Review queue endpoints.

Three routes over the review cases the risk engine opens: list a reviewer's
queue, read one case, and resolve it with a decision.

The list endpoint returns a six-field invoice summary per case rather than the
full invoice. A queue is a list, and embedding line items for every invoice on
the page would make the payload grow with the corpus rather than with the page.
Callers needing more follow ``GET /invoices/{id}``.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.review import (
    ReviewCaseResponse,
    ReviewListResponse,
    ReviewResolutionRequest,
)
from app.services.review.types import ReviewDecision, ReviewResolution
from app.services.review_service import (
    ReviewCaseAlreadyResolved,
    ReviewCaseNotFound,
    UnknownReviewer,
    get_review_case,
    list_review_cases,
    resolve_review_case,
)

router = APIRouter(prefix="/reviews", tags=["reviews"])


@router.get("", response_model=ReviewListResponse, summary="List review cases")
def list_reviews(
    db: Annotated[Session, Depends(get_db)],
    status_filter: Annotated[
        str | None,
        Query(alias="status", description="Filter by review status."),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ReviewListResponse:
    """Return a page of review cases, newest first.

    ``total`` counts every case matching the filter, not the page, so a caller can
    compute the number of pages.
    """
    try:
        cases, total = list_review_cases(
            db, status=status_filter, limit=limit, offset=offset
        )
    except ValueError as exc:
        # An unrecognised status string. 422 rather than 500: the request was
        # well-formed HTTP but named a status that does not exist.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"invalid status: {status_filter}",
        ) from exc

    return ReviewListResponse(
        items=[ReviewCaseResponse.from_orm_case(case) for case in cases],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{case_id}",
    response_model=ReviewCaseResponse,
    summary="Fetch a single review case",
)
def get_review(
    case_id: UUID,
    db: Annotated[Session, Depends(get_db)],
) -> ReviewCaseResponse:
    """Return one review case, or 404 if no case has that id."""
    case = get_review_case(db, case_id)
    if case is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="review case not found",
        )
    return ReviewCaseResponse.from_orm_case(case)


@router.post(
    "/{case_id}/resolve",
    response_model=ReviewCaseResponse,
    summary="Resolve a review case with a decision",
)
def resolve_review(
    case_id: UUID,
    payload: ReviewResolutionRequest,
    db: Annotated[Session, Depends(get_db)],
) -> ReviewCaseResponse:
    """Close a review case and apply the decision to its invoice.

    The response reflects the committed case, including the invoice's new status
    via ``invoice_summary``. An invoice already in a terminal status keeps it —
    see the "Terminal statuses are honored" note in docs/architecture.md.
    """
    try:
        case = resolve_review_case(
            db,
            case_id=case_id,
            resolution=ReviewResolution(
                decision=ReviewDecision(payload.decision),
                reviewer_id=payload.reviewer_id,
                resolution=payload.resolution,
            ),
        )
    except ReviewCaseNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="review case not found",
        ) from exc
    except ReviewCaseAlreadyResolved as exc:
        # 409, not 400: the request was valid but conflicts with the case's
        # current state. Retrying after someone else decided is a caller error.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="review case has already been resolved",
        ) from exc
    except UnknownReviewer as exc:
        # 422: the body parsed but names a user that does not exist.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="unknown reviewer_id",
        ) from exc

    # Re-read so the response carries the invoice relationship loaded, without
    # depending on the caller re-querying after the commit.
    refreshed = get_review_case(db, case.id)
    assert refreshed is not None  # just committed; cannot vanish
    return ReviewCaseResponse.from_orm_case(refreshed)