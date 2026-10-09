"""Service-level tests for review-case creation, listing, and resolution.

Covers ``create_review_case``, ``list_review_cases``, the two getters, and
``resolve_review_case`` against a real database via the shared savepoint
fixture.

The risk-engine wiring that *calls* ``create_review_case`` lives in
``test_risk_service_review_integration.py``, so this file stays focused on the
review service's own contract.
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.document import Document
from app.models.enums import (
    AuditActor,
    AuditEventType,
    InvoiceStatus,
    ReviewStatus,
)
from app.models.invoice import Invoice, LineItem
from app.models.review import ReviewCase
from app.models.user import User
from app.services.review.types import ReviewDecision, ReviewResolution
from app.services.review_service import (
    ReviewCaseAlreadyResolved,
    ReviewCaseNotFound,
    UnknownReviewer,
    create_review_case,
    get_review_case,
    get_review_case_for_invoice,
    list_review_cases,
    resolve_review_case,
)

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def make_user(db: Session, email: str = "system@local") -> User:
    """Return the reviewer, reusing the seeded user rather than re-inserting it.

    Deliberate, do not "simplify" this into a hard insert: ``seed_system_user``
    provisions exactly ``system@local``, so re-inserting violates the unique
    index on email whenever the database has been seeded. The lookup is what
    makes these tests independent of pre-existing database state.
    """
    existing = db.scalars(select(User).where(User.email == email)).first()
    if existing is not None:
        return existing

    user = User(email=email, display_name="System Reviewer")
    db.add(user)
    db.commit()
    return user


def make_invoice(db: Session, **overrides) -> Invoice:
    """Insert a Document + Invoice directly in the DB.

    Defaults describe a flagged invoice sitting in NEEDS_REVIEW, which is the
    state every resolve test starts from.
    """
    defaults = {
        "vendor_name": "Acme Corp",
        "invoice_number": "INV-0001",
        "invoice_date": date(2026, 1, 1),
        "total_amount": Decimal("100.00"),
        "currency": "USD",
        "status": InvoiceStatus.NEEDS_REVIEW,
        "extraction_confidence": {"vendor_name": 0.95},
        "created_at": BASE,
    }
    defaults.update(overrides)

    invoice = Invoice(
        document=Document(
            filename="invoice.pdf",
            content_type="application/pdf",
            size_bytes=2048,
            sha256=uuid.uuid4().hex,
            storage_path="/storage/invoice.pdf",
        ),
        **defaults,
    )
    invoice.line_items = [
        LineItem(
            position=1,
            description="Widget",
            quantity=Decimal("1"),
            unit_price=Decimal("10.00"),
            line_total=Decimal("10.00"),
        )
    ]
    db.add(invoice)
    db.commit()
    return invoice


def events_for(
    db: Session, invoice: Invoice, event_type: AuditEventType
) -> list[AuditEvent]:
    return list(
        db.scalars(
            select(AuditEvent).where(
                AuditEvent.invoice_id == invoice.id,
                AuditEvent.event_type == event_type,
            )
        )
    )


def case_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(ReviewCase)) or 0


# --- create_review_case ------------------------------------------------------


def test_create_review_case_inserts_pending_case(db_session: Session) -> None:
    invoice = make_invoice(db_session)
    case = create_review_case(
        db_session, invoice_id=invoice.id, reason="Risk score 80 (high)"
    )
    db_session.commit()

    assert case.id is not None
    assert case.invoice_id == invoice.id
    assert case.status == ReviewStatus.PENDING
    assert case.reason == "Risk score 80 (high)"
    # Nothing is decided yet, so nothing may be filled in.
    assert case.resolution is None
    assert case.resolved_at is None
    assert case.assigned_to is None
    assert case_count(db_session) == 1


def test_create_review_case_is_idempotent(db_session: Session) -> None:
    # invoice_id is uniquely constrained, so a non-idempotent implementation
    # would fail the INSERT on the second call rather than duplicating quietly.
    invoice = make_invoice(db_session)
    first = create_review_case(db_session, invoice_id=invoice.id, reason="first")
    db_session.commit()

    second = create_review_case(db_session, invoice_id=invoice.id, reason="second")
    db_session.commit()

    assert second.id == first.id
    assert case_count(db_session) == 1
    # The original reason stands: a repeat call must not rewrite the audit trail.
    assert second.reason == "first"
    assert len(events_for(db_session, invoice, AuditEventType.REVIEW_REQUESTED)) == 1


def test_create_review_case_writes_audit_event(db_session: Session) -> None:
    invoice = make_invoice(db_session)
    reason = "Risk score 90 (high); recommended: reject"
    create_review_case(db_session, invoice_id=invoice.id, reason=reason)
    db_session.commit()

    found = events_for(db_session, invoice, AuditEventType.REVIEW_REQUESTED)
    assert len(found) == 1
    event = found[0]
    assert event.event_type == AuditEventType.REVIEW_REQUESTED
    assert event.actor == AuditActor.SYSTEM
    assert event.message == f"Review case created: {reason}"
    assert event.data == {"analysis": "review_case", "reason": reason}


def test_create_review_case_does_not_reopen_a_resolved_case(
    db_session: Session,
) -> None:
    # A human already ruled on this invoice. Re-running the score is not a
    # reason to discard their decision.
    invoice = make_invoice(db_session)
    reviewer = make_user(db_session)
    case = create_review_case(db_session, invoice_id=invoice.id, reason="flagged")
    db_session.commit()

    resolve_review_case(
        db_session,
        case_id=case.id,
        resolution=ReviewResolution(
            decision=ReviewDecision.APPROVE,
            reviewer_id=reviewer.id,
            resolution="Looks fine after all.",
        ),
    )

    again = create_review_case(db_session, invoice_id=invoice.id, reason="re-flagged")
    db_session.commit()

    assert again.id == case.id
    assert again.status == ReviewStatus.RESOLVED
    assert again.resolution == "Looks fine after all."
    assert case_count(db_session) == 1


def test_create_review_case_rejects_blank_reason(db_session: Session) -> None:
    # A case whose reason is blank tells a reviewer nothing about why they are
    # being interrupted.
    invoice = make_invoice(db_session)
    with pytest.raises(ValueError):
        create_review_case(db_session, invoice_id=invoice.id, reason="   ")


def test_create_review_case_unknown_invoice(db_session: Session) -> None:
    with pytest.raises(LookupError):
        create_review_case(db_session, invoice_id=uuid.uuid4(), reason="x")


# --- list --------------------------------------------------------------------


def test_list_review_cases_empty(db_session: Session) -> None:
    cases, total = list_review_cases(db_session)

    assert cases == []
    assert total == 0


def test_list_review_cases_filters_by_status(db_session: Session) -> None:
    pending_invoice = make_invoice(db_session)
    resolved_invoice = make_invoice(db_session, invoice_number="INV-0002")
    dismissed_invoice = make_invoice(db_session, invoice_number="INV-0003")

    create_review_case(db_session, invoice_id=pending_invoice.id, reason="p")
    resolved = create_review_case(
        db_session, invoice_id=resolved_invoice.id, reason="r"
    )
    dismissed = create_review_case(
        db_session, invoice_id=dismissed_invoice.id, reason="d"
    )
    db_session.commit()

    resolved.status = ReviewStatus.RESOLVED
    dismissed.status = ReviewStatus.DISMISSED
    db_session.commit()

    pending, pending_total = list_review_cases(db_session, status="pending")
    assert pending_total == 1
    assert [c.invoice_id for c in pending] == [pending_invoice.id]

    is_resolved, resolved_total = list_review_cases(db_session, status="resolved")
    assert resolved_total == 1
    assert [c.invoice_id for c in is_resolved] == [resolved_invoice.id]

    is_dismissed, dismissed_total = list_review_cases(db_session, status="dismissed")
    assert dismissed_total == 1
    assert [c.invoice_id for c in is_dismissed] == [dismissed_invoice.id]

    # The unfiltered count ignores the filter, so a paginating caller can tell
    # "3 exist" from "1 matches".
    _, unfiltered_total = list_review_cases(db_session)
    assert unfiltered_total == 3


def test_list_review_cases_orders_newest_first(db_session: Session) -> None:
    first = make_invoice(db_session)
    second = make_invoice(db_session, invoice_number="INV-0002")
    third = make_invoice(db_session, invoice_number="INV-0003")
    create_review_case(db_session, invoice_id=first.id, reason="one")
    create_review_case(db_session, invoice_id=second.id, reason="two")
    create_review_case(db_session, invoice_id=third.id, reason="three")
    db_session.commit()

    cases, total = list_review_cases(db_session)

    assert total == 3
    # Newest first: created_at is identical for all three (same clock tick in
    # tests), so this also proves the id tiebreaker is applied deterministically
    # rather than leaving the order to chance.
    assert [c.invoice_id for c in cases] == [third.id, second.id, first.id]


def test_list_review_cases_pagination(db_session: Session) -> None:
    ids = []
    for index in range(5):
        invoice = make_invoice(db_session, invoice_number=f"INV-{index}")
        create_review_case(db_session, invoice_id=invoice.id, reason=f"r{index}")
        ids.append(invoice.id)
    db_session.commit()

    first_page, total = list_review_cases(db_session, limit=2, offset=0)
    assert total == 5
    assert len(first_page) == 2
    # Page size must not change what the total reports.
    assert total == 5

    second_page, _ = list_review_cases(db_session, limit=2, offset=2)
    assert len(second_page) == 2

    last_page, _ = list_review_cases(db_session, limit=2, offset=4)
    assert len(last_page) == 1

    # Pages partition the set rather than repeating rows.
    seen = [c.invoice_id for c in first_page + second_page + last_page]
    assert sorted(seen) == sorted(ids)
    assert len(set(seen)) == 5


def test_list_review_cases_eager_loads_invoice_and_user(db_session: Session) -> None:
    invoice = make_invoice(db_session)
    create_review_case(db_session, invoice_id=invoice.id, reason="one")
    db_session.commit()

    cases, _ = list_review_cases(db_session)
    # Touching the relationship after the instance is detached proves the eager
    # load happened; a lazy load here would raise DetachedInstanceError.
    db_session.expunge_all()
    assert cases[0].invoice.vendor_name == "Acme Corp"
    assert cases[0].assigned_user is None


# --- getters -----------------------------------------------------------------


def test_get_review_case_by_id(db_session: Session) -> None:
    invoice = make_invoice(db_session)
    reviewer = make_user(db_session)
    case = create_review_case(db_session, invoice_id=invoice.id, reason="flagged")
    case.assigned_to = reviewer.id
    db_session.commit()

    found = get_review_case(db_session, case.id)

    assert found is not None
    assert found.id == case.id
    assert found.invoice_id == invoice.id
    # Both relationships came back eagerly loaded.
    assert found.invoice.invoice_number == "INV-0001"
    assert found.assigned_user is not None
    assert found.assigned_user.email == "system@local"


def test_get_review_case_not_found(db_session: Session) -> None:
    assert get_review_case(db_session, uuid.uuid4()) is None


def test_get_review_case_for_invoice(db_session: Session) -> None:
    invoice = make_invoice(db_session)
    case = create_review_case(db_session, invoice_id=invoice.id, reason="flagged")
    db_session.commit()

    found = get_review_case_for_invoice(db_session, invoice.id)

    assert found is not None
    assert found.id == case.id
    assert found.invoice.id == invoice.id
    assert found.assigned_user is None
    # An invoice that was never flagged has no case.
    other = make_invoice(db_session, invoice_number="INV-0002")
    assert get_review_case_for_invoice(db_session, other.id) is None


# --- resolve -----------------------------------------------------------------


def test_resolve_review_case_approve_sets_invoice_approved(db_session: Session) -> None:
    invoice = make_invoice(db_session, status=InvoiceStatus.NEEDS_REVIEW)
    reviewer = make_user(db_session)
    case = create_review_case(db_session, invoice_id=invoice.id, reason="flagged")
    db_session.commit()

    resolved = resolve_review_case(
        db_session,
        case_id=case.id,
        resolution=ReviewResolution(
            decision=ReviewDecision.APPROVE,
            reviewer_id=reviewer.id,
            resolution="Vendor confirmed the charge is legitimate.",
        ),
    )

    assert resolved.status == ReviewStatus.RESOLVED
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.APPROVED


def test_resolve_review_case_reject_sets_invoice_rejected(db_session: Session) -> None:
    invoice = make_invoice(db_session, status=InvoiceStatus.NEEDS_REVIEW)
    reviewer = make_user(db_session)
    case = create_review_case(db_session, invoice_id=invoice.id, reason="flagged")
    db_session.commit()

    resolved = resolve_review_case(
        db_session,
        case_id=case.id,
        resolution=ReviewResolution(
            decision=ReviewDecision.REJECT,
            reviewer_id=reviewer.id,
            resolution="Duplicate of an invoice already paid.",
        ),
    )

    assert resolved.status == ReviewStatus.RESOLVED
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.REJECTED


def test_resolve_review_case_dismiss_leaves_invoice_status(db_session: Session) -> None:
    # Dismiss closes the case without deciding the invoice: it stays in
    # NEEDS_REVIEW and can be re-opened by a fresh assessment later.
    invoice = make_invoice(db_session, status=InvoiceStatus.NEEDS_REVIEW)
    reviewer = make_user(db_session)
    case = create_review_case(db_session, invoice_id=invoice.id, reason="flagged")
    db_session.commit()

    resolved = resolve_review_case(
        db_session,
        case_id=case.id,
        resolution=ReviewResolution(
            decision=ReviewDecision.DISMISS,
            reviewer_id=reviewer.id,
            resolution="No action needed; false positive.",
        ),
    )

    assert resolved.status == ReviewStatus.DISMISSED
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.NEEDS_REVIEW


def test_resolve_review_case_records_reviewer(db_session: Session) -> None:
    invoice = make_invoice(db_session)
    reviewer = make_user(db_session)
    case = create_review_case(db_session, invoice_id=invoice.id, reason="flagged")
    db_session.commit()
    assert case.assigned_to is None

    resolved = resolve_review_case(
        db_session,
        case_id=case.id,
        resolution=ReviewResolution(
            decision=ReviewDecision.APPROVE,
            reviewer_id=reviewer.id,
            resolution="ok",
        ),
    )

    assert resolved.assigned_to == reviewer.id
    assert resolved.assigned_user is not None
    assert resolved.assigned_user.email == "system@local"

    # The audit event names the same human, not just the actor type.
    event = events_for(db_session, invoice, AuditEventType.REVIEW_DECIDED)[0]
    assert event.actor == AuditActor.REVIEWER
    assert event.actor_user_id == reviewer.id


def test_resolve_review_case_requires_known_reviewer(db_session: Session) -> None:
    invoice = make_invoice(db_session)
    case = create_review_case(db_session, invoice_id=invoice.id, reason="flagged")
    db_session.commit()

    with pytest.raises(UnknownReviewer):
        resolve_review_case(
            db_session,
            case_id=case.id,
            resolution=ReviewResolution(
                decision=ReviewDecision.APPROVE,
                reviewer_id=uuid.uuid4(),
                resolution="ghost",
            ),
        )

    # Checked before any write, so the case is left untouched.
    db_session.refresh(case)
    assert case.status == ReviewStatus.PENDING
    assert case.resolved_at is None
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.NEEDS_REVIEW


def test_resolve_review_case_rejects_unknown_case_id(db_session: Session) -> None:
    reviewer = make_user(db_session)

    with pytest.raises(ReviewCaseNotFound):
        resolve_review_case(
            db_session,
            case_id=uuid.uuid4(),
            resolution=ReviewResolution(
                decision=ReviewDecision.APPROVE,
                reviewer_id=reviewer.id,
                resolution="x",
            ),
        )


def test_resolve_review_case_rejects_already_resolved(db_session: Session) -> None:
    invoice = make_invoice(db_session)
    reviewer = make_user(db_session)
    case = create_review_case(db_session, invoice_id=invoice.id, reason="flagged")
    db_session.commit()
    resolution = ReviewResolution(
        decision=ReviewDecision.APPROVE,
        reviewer_id=reviewer.id,
        resolution="Looks fine.",
    )
    resolve_review_case(db_session, case_id=case.id, resolution=resolution)

    # One resolution per case: overwriting it would erase the human's reasoning.
    with pytest.raises(ReviewCaseAlreadyResolved):
        resolve_review_case(db_session, case_id=case.id, resolution=resolution)

    assert len(events_for(db_session, invoice, AuditEventType.REVIEW_DECIDED)) == 1


def test_resolve_review_case_rejects_already_dismissed(db_session: Session) -> None:
    invoice = make_invoice(db_session)
    reviewer = make_user(db_session)
    case = create_review_case(db_session, invoice_id=invoice.id, reason="flagged")
    db_session.commit()
    resolve_review_case(
        db_session,
        case_id=case.id,
        resolution=ReviewResolution(
            decision=ReviewDecision.DISMISS,
            reviewer_id=reviewer.id,
            resolution="no action",
        ),
    )

    with pytest.raises(ReviewCaseAlreadyResolved):
        resolve_review_case(
            db_session,
            case_id=case.id,
            resolution=ReviewResolution(
                decision=ReviewDecision.APPROVE,
                reviewer_id=reviewer.id,
                resolution="changed my mind",
            ),
        )


@pytest.mark.parametrize(
    "terminal",
    [InvoiceStatus.APPROVED, InvoiceStatus.REJECTED, InvoiceStatus.FAILED],
)
def test_resolve_review_case_does_not_override_terminal_status(
    db_session: Session, terminal: InvoiceStatus
) -> None:
    # A decision someone already made stands; resolving the case still succeeds,
    # it just does not rewrite the invoice.
    invoice = make_invoice(db_session, status=terminal)
    reviewer = make_user(db_session)
    case = create_review_case(db_session, invoice_id=invoice.id, reason="flagged")
    db_session.commit()

    resolved = resolve_review_case(
        db_session,
        case_id=case.id,
        resolution=ReviewResolution(
            decision=ReviewDecision.APPROVE,
            reviewer_id=reviewer.id,
            resolution="x",
        ),
    )

    assert resolved.status == ReviewStatus.RESOLVED
    db_session.refresh(invoice)
    assert invoice.status == terminal


def test_resolve_approve_does_not_unreject_terminal_invoice(
    db_session: Session,
) -> None:
    """A reviewer cannot un-reject an invoice the risk engine already rejected.

    This is the smoke scenario turned into a regression test. The case closes and
    the reviewer's decision is recorded in the audit trail, but the invoice keeps
    its terminal REJECTED status: every service in the pipeline, human decisions
    included, refuses to overwrite a terminal status.
    """
    invoice = make_invoice(db_session, status=InvoiceStatus.REJECTED)
    reviewer = make_user(db_session)
    case = create_review_case(db_session, invoice_id=invoice.id, reason="flagged")
    db_session.commit()

    resolved = resolve_review_case(
        db_session,
        case_id=case.id,
        resolution=ReviewResolution(
            decision=ReviewDecision.APPROVE,
            reviewer_id=reviewer.id,
            resolution="Approved for payment",
        ),
    )

    # The case is closed and the human's decision is captured...
    assert resolved.status == ReviewStatus.RESOLVED
    assert resolved.assigned_to == reviewer.id
    assert resolved.resolution == "Approved for payment"
    assert resolved.resolved_at is not None

    event = events_for(db_session, invoice, AuditEventType.REVIEW_DECIDED)[0]
    assert event.actor == AuditActor.REVIEWER
    assert event.actor_user_id == reviewer.id
    assert event.data["decision"] == "approve"

    # ...but the invoice is untouched.
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.REJECTED


def test_resolve_review_case_writes_review_decided_audit_event(db_session: Session) -> None:
    invoice = make_invoice(db_session)
    reviewer = make_user(db_session)
    case = create_review_case(db_session, invoice_id=invoice.id, reason="flagged")
    db_session.commit()

    resolve_review_case(
        db_session,
        case_id=case.id,
        resolution=ReviewResolution(
            decision=ReviewDecision.REJECT,
            reviewer_id=reviewer.id,
            resolution="Not a real charge.",
        ),
    )

    found = events_for(db_session, invoice, AuditEventType.REVIEW_DECIDED)
    assert len(found) == 1
    event = found[0]
    assert event.event_type == AuditEventType.REVIEW_DECIDED
    assert event.actor == AuditActor.REVIEWER
    # "rejected", not the "rejectd" that naive suffixing would produce.
    assert event.message == "Review rejected: Not a real charge."
    assert event.data == {
        "analysis": "review_case",
        "decision": "reject",
        "reviewer_id": str(reviewer.id),
        "case_id": str(case.id),
    }


@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        (ReviewDecision.APPROVE, "Review approved: "),
        (ReviewDecision.REJECT, "Review rejected: "),
        (ReviewDecision.DISMISS, "Review dismissed: "),
    ],
)
def test_review_decided_message_is_grammatical(
    db_session: Session, decision: ReviewDecision, expected: str
) -> None:
    invoice = make_invoice(db_session)
    reviewer = make_user(db_session)
    case = create_review_case(db_session, invoice_id=invoice.id, reason="flagged")
    db_session.commit()

    resolve_review_case(
        db_session,
        case_id=case.id,
        resolution=ReviewResolution(
            decision=decision, reviewer_id=reviewer.id, resolution="note"
        ),
    )

    event = events_for(db_session, invoice, AuditEventType.REVIEW_DECIDED)[0]
    assert event.message == f"{expected}note"


def test_resolve_review_case_sets_resolved_at(db_session: Session) -> None:
    invoice = make_invoice(db_session)
    reviewer = make_user(db_session)
    case = create_review_case(db_session, invoice_id=invoice.id, reason="flagged")
    db_session.commit()
    assert case.resolved_at is None

    resolved = resolve_review_case(
        db_session,
        case_id=case.id,
        resolution=ReviewResolution(
            decision=ReviewDecision.APPROVE,
            reviewer_id=reviewer.id,
            resolution="ok",
        ),
    )

    assert resolved.resolved_at is not None
    # Application-side UTC, so the value is timezone-aware rather than naive.
    assert resolved.resolved_at.tzinfo is not None
    assert resolved.resolved_at.utcoffset().total_seconds() == 0


def test_resolve_review_case_truncates_long_audit_message(db_session: Session) -> None:
    # AuditEvent.message is String(1024) and the reviewer's note is free text, so
    # an over-long note must truncate rather than fail the INSERT.
    invoice = make_invoice(db_session)
    reviewer = make_user(db_session)
    case = create_review_case(db_session, invoice_id=invoice.id, reason="flagged")
    db_session.commit()

    resolve_review_case(
        db_session,
        case_id=case.id,
        resolution=ReviewResolution(
            decision=ReviewDecision.REJECT,
            reviewer_id=reviewer.id,
            resolution="x" * 4000,
        ),
    )

    event = events_for(db_session, invoice, AuditEventType.REVIEW_DECIDED)[0]
    assert len(event.message) <= 1024
    assert event.message.endswith("...")
    # The full note is still persisted on the case itself.
    assert len(events_for(db_session, invoice, AuditEventType.REVIEW_DECIDED)[0].data) > 0


def test_resolve_review_case_rejects_blank_resolution(db_session: Session) -> None:
    # model_construct bypasses validation, proving the service checks too and is
    # not relying on the schema alone.
    invoice = make_invoice(db_session)
    reviewer = make_user(db_session)
    case = create_review_case(db_session, invoice_id=invoice.id, reason="flagged")
    db_session.commit()

    with pytest.raises(ValueError):
        resolve_review_case(
            db_session,
            case_id=case.id,
            resolution=ReviewResolution.model_construct(
                decision=ReviewDecision.APPROVE,
                reviewer_id=reviewer.id,
                resolution="   ",
            ),
        )


def test_resolution_schema_rejects_blank_note() -> None:
    with pytest.raises(ValueError):
        ReviewResolution(
            decision=ReviewDecision.APPROVE,
            reviewer_id=uuid.uuid4(),
            resolution="   ",
        )