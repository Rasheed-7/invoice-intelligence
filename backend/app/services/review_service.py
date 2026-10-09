"""Create, list, and resolve human review cases.

A review case is the hand-off between the automatic pipeline and a person: the
risk engine flags an invoice, this creates the case, and a reviewer later closes
it with a decision. At most one case exists per invoice (a unique constraint on
``invoice_id``), so re-running the pipeline can never fan out into duplicate
work items.

Deliberately free of FastAPI imports, matching ``validation_service``,
``po_matching_service``, and ``risk_service``.

Transaction ownership is split on purpose. ``create_review_case`` does **not**
commit, because ``run_risk_assessment`` calls it inside its own transaction and
the score, the audit events, and the case must land or fail together.
``resolve_review_case`` does commit, because it is a standalone human action
with no caller-managed transaction.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.audit import AuditEvent
from app.models.enums import (
    AuditActor,
    AuditEventType,
    InvoiceStatus,
    ReviewStatus,
)
from app.models.invoice import Invoice
from app.models.review import ReviewCase
from app.models.user import User
from app.services.review.types import ReviewDecision, ReviewResolution

__all__ = [
    "ReviewCaseAlreadyResolved",
    "ReviewCaseNotFound",
    "UnknownReviewer",
    "create_review_case",
    "get_review_case",
    "get_review_case_for_invoice",
    "list_review_cases",
    "resolve_review_case",
]

#: Statuses that mean the case is closed. A closed case is never re-decided: the
#: audit trail records one resolution per case, and silently overwriting it would
#: erase the human's original reasoning.
_CLOSED_STATUSES = frozenset({ReviewStatus.RESOLVED, ReviewStatus.DISMISSED})

#: Terminal invoice statuses: a decision already stands, so resolving a case must
#: not overwrite it.
_TERMINAL_STATUSES = frozenset(
    {InvoiceStatus.APPROVED, InvoiceStatus.REJECTED, InvoiceStatus.FAILED}
)

#: Past-tense wording for the audit message. An explicit map rather than
#: appending "d" to the decision value, because "reject" -> "rejectd" and
#: "dismiss" -> "dismissd" are not words.
_DECISION_PAST_TENSE = {
    ReviewDecision.APPROVE: "approved",
    ReviewDecision.REJECT: "rejected",
    ReviewDecision.DISMISS: "dismissed",
}

#: Ceiling on the audit message, matching AuditEvent.message's String(1024).
#: The reviewer's note is free text, so it is truncated rather than allowed to
#: fail the INSERT that records the decision.
_AUDIT_MESSAGE_MAX = 1024


class ReviewCaseNotFound(LookupError):
    """No review case exists with that id."""


class ReviewCaseAlreadyResolved(Exception):
    """The case is already closed and must not be decided twice."""


class UnknownReviewer(LookupError):
    """The referenced reviewer does not exist."""


def _with_relationships(stmt, case_id: uuid.UUID):
    """Eager-load a case's invoice and assigned user for display."""
    return stmt.options(
        selectinload(ReviewCase.invoice),
        selectinload(ReviewCase.assigned_user),
    ).where(ReviewCase.id == case_id)


def create_review_case(db: Session, *, invoice_id: uuid.UUID, reason: str) -> ReviewCase:
    """Create the review case for an invoice, or return the existing one.

    Does not commit: the caller owns the transaction, which is what lets the
    case land atomically with the risk score that triggered it.

    Args:
        db: Database session (caller commits).
        invoice_id: The flagged invoice.
        reason: Human-readable explanation, usually the risk summary.

    Returns:
        The new or pre-existing ReviewCase.

    Raises:
        LookupError: Invoice not found.
        ValueError: ``reason`` is blank.
    """
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        raise LookupError(f"invoice not found: {invoice_id}")

    if not reason.strip():
        # A case whose reason is blank tells a reviewer nothing about why they
        # are being interrupted, so refuse it rather than persist an unusable row.
        raise ValueError("reason must not be blank")

    existing = db.scalars(
        select(ReviewCase)
        .options(selectinload(ReviewCase.invoice), selectinload(ReviewCase.assigned_user))
        .where(ReviewCase.invoice_id == invoice_id)
    ).first()

    # Idempotent: the unique constraint on invoice_id and this check make
    # repeated calls safe. Re-running /assess-risk must not create duplicate
    # cases. An already-resolved case is returned as-is rather than reopened:
    # a human already ruled on this invoice, and re-running a score is not a
    # reason to discard their decision.
    if existing is not None:
        return existing

    case = ReviewCase(
        invoice_id=invoice_id,
        status=ReviewStatus.PENDING,
        reason=reason,
    )
    db.add(case)
    # Flush so the case's id is available to the audit event below without
    # ending the caller's transaction.
    db.flush()

    db.add(
        AuditEvent(
            invoice_id=invoice_id,
            event_type=AuditEventType.REVIEW_REQUESTED,
            actor=AuditActor.SYSTEM,
            message=f"Review case created: {reason}",
            data={"analysis": "review_case", "reason": reason},
        )
    )

    return case


def list_review_cases(
    db: Session,
    *,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ReviewCase], int]:
    """List review cases newest-first, with the total count for the filter.

    The count reflects the status filter (not the page), so a paginating caller
    can compute a page count. ``id`` breaks ties in ``created_at`` so two cases
    created in the same clock tick still order deterministically.
    """
    conditions = []
    if status is not None:
        conditions.append(ReviewCase.status == ReviewStatus(status))

    base = select(ReviewCase)
    count_stmt = select(func.count()).select_from(ReviewCase)
    if conditions:
        base = base.where(*conditions)
        count_stmt = count_stmt.where(*conditions)

    total = db.scalar(count_stmt) or 0

    stmt = (
        base.options(
            selectinload(ReviewCase.invoice),
            selectinload(ReviewCase.assigned_user),
        )
        .order_by(ReviewCase.created_at.desc(), ReviewCase.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(db.scalars(stmt).all()), total


def get_review_case(db: Session, case_id: uuid.UUID) -> ReviewCase | None:
    """Return one review case with its invoice and assignee eager-loaded."""
    return db.scalars(_with_relationships(select(ReviewCase), case_id)).first()


def get_review_case_for_invoice(db: Session, invoice_id: uuid.UUID) -> ReviewCase | None:
    """Return the review case for an invoice, or None if it has never been flagged."""
    stmt = (
        select(ReviewCase)
        .options(
            selectinload(ReviewCase.invoice),
            selectinload(ReviewCase.assigned_user),
        )
        .where(ReviewCase.invoice_id == invoice_id)
    )
    return db.scalars(stmt).first()


def _decision_message(decision: ReviewDecision, note: str) -> str:
    """One sentence for the audit timeline, truncated to the column width."""
    message = f"Review {_DECISION_PAST_TENSE[decision]}: {note}"
    if len(message) <= _AUDIT_MESSAGE_MAX:
        return message
    return message[: _AUDIT_MESSAGE_MAX - 3] + "..."


def resolve_review_case(
    db: Session, *, case_id: uuid.UUID, resolution: ReviewResolution
) -> ReviewCase:
    """Close a review case with a decision and apply it to the invoice.

    Commits at the end.

    Args:
        db: Database session (this function owns the transaction).
        case_id: The case to resolve.
        resolution: The decision, the reviewing user, and the note.

    Returns:
        The resolved ReviewCase (refreshed after commit).

    Raises:
        ReviewCaseNotFound: No case with that id.
        ReviewCaseAlreadyResolved: The case is already closed.
        UnknownReviewer: ``resolution.reviewer_id`` does not match a user.
        ValueError: The resolution note is blank.
    """
    case = get_review_case(db, case_id)
    if case is None:
        raise ReviewCaseNotFound(f"review case not found: {case_id}")

    if case.status in _CLOSED_STATUSES:
        raise ReviewCaseAlreadyResolved(
            f"review case {case_id} is already {case.status.value}"
        )

    # The schema already rejects a blank note; re-checked here because the
    # service is also callable directly, and a decision with no stated reasoning
    # is not auditable.
    if not resolution.resolution.strip():
        raise ValueError("resolution must not be blank")

    if db.get(User, resolution.reviewer_id) is None:
        # Checked before any write so a bad reviewer id cannot leave a half-
        # applied case behind.
        raise UnknownReviewer(f"unknown reviewer: {resolution.reviewer_id}")

    case.status = (
        ReviewStatus.DISMISSED
        if resolution.decision == ReviewDecision.DISMISS
        else ReviewStatus.RESOLVED
    )
    case.assigned_to = resolution.reviewer_id
    case.resolution = resolution.resolution
    # Application-side UTC timestamp, not a DB default, because resolved_at is
    # only set on this code path and we want it in the same statement as the
    # status change.
    case.resolved_at = datetime.now(UTC)

    invoice = case.invoice
    if invoice is not None and invoice.status not in _TERMINAL_STATUSES:
        if resolution.decision == ReviewDecision.APPROVE:
            invoice.status = InvoiceStatus.APPROVED
        elif resolution.decision == ReviewDecision.REJECT:
            invoice.status = InvoiceStatus.REJECTED
        # Dismiss closes the case without deciding the invoice. The invoice stays
        # in NEEDS_REVIEW and a fresh case can be created if it's re-opened.

    db.add(
        AuditEvent(
            invoice_id=case.invoice_id,
            event_type=AuditEventType.REVIEW_DECIDED,
            actor=AuditActor.REVIEWER,
            actor_user_id=resolution.reviewer_id,
            message=_decision_message(resolution.decision, resolution.resolution),
            data={
                "analysis": "review_case",
                "decision": resolution.decision.value,
                "reviewer_id": str(resolution.reviewer_id),
                "case_id": str(case.id),
            },
        )
    )

    db.commit()
    db.refresh(case)

    return case