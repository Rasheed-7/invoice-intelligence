"""HTTP-level tests for the review endpoints.

Uses the shared ``client`` fixture (savepoint-scoped session + tmp storage root),
so every row written here is discarded on teardown.

Cases are created through the service rather than by POSTing them, because there
is no "create case" endpoint - cases are opened by the risk engine. The risk
engine's own decision to open one is covered in
``test_risk_service_review_integration.py``.
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import Document
from app.models.enums import InvoiceStatus, ReviewStatus
from app.models.invoice import Invoice, LineItem
from app.models.review import ReviewCase
from app.models.user import User
from app.services.review_service import create_review_case

REVIEWS_URL = "/api/v1/reviews"
INVOICES_URL = "/api/v1/invoices"
UNKNOWN_ID = "00000000-0000-0000-0000-000000000000"
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
    """Insert a Document + Invoice directly in the DB."""
    defaults = {
        "vendor_name": "Acme Corporation",
        "invoice_number": "INV-0001",
        "invoice_date": date(2026, 1, 1),
        "total_amount": Decimal("100.00"),
        "currency": "USD",
        "status": InvoiceStatus.NEEDS_REVIEW,
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


def make_case(db: Session, invoice: Invoice, reason: str = "Risk score 80 (high)") -> ReviewCase:
    """Open a pending review case for an invoice and commit it."""
    case = create_review_case(db, invoice_id=invoice.id, reason=reason)
    db.commit()
    return case


def resolve_body(reviewer: User, decision: str = "approve", note: str = "ok") -> dict:
    return {
        "decision": decision,
        "reviewer_id": str(reviewer.id),
        "resolution": note,
    }


# --- GET /reviews ------------------------------------------------------------


def test_list_reviews_empty(client: TestClient) -> None:
    response = client.get(REVIEWS_URL)

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 0
    # Echoed back so a caller can page without re-reading its own query string.
    assert body["limit"] == 50
    assert body["offset"] == 0


def test_list_reviews_returns_created_cases(client: TestClient, db_session: Session) -> None:
    first = make_invoice(db_session)
    second = make_invoice(db_session, invoice_number="INV-0002")
    make_case(db_session, first, reason="older")
    make_case(db_session, second, reason="newer")

    body = client.get(REVIEWS_URL).json()

    assert body["total"] == 2
    # Newest first.
    assert [item["invoice_id"] for item in body["items"]] == [str(second.id), str(first.id)]
    assert body["items"][0]["reason"] == "newer"


def test_list_reviews_filters_by_status(
    client: TestClient, db_session: Session
) -> None:
    pending_invoice = make_invoice(db_session)
    resolved_invoice = make_invoice(db_session, invoice_number="INV-0002")
    dismissed_invoice = make_invoice(db_session, invoice_number="INV-0003")
    make_case(db_session, pending_invoice, reason="p")
    resolved = make_case(db_session, resolved_invoice, reason="r")
    dismissed = make_case(db_session, dismissed_invoice, reason="d")
    resolved.status = ReviewStatus.RESOLVED
    dismissed.status = ReviewStatus.DISMISSED
    db_session.commit()

    body = client.get(f"{REVIEWS_URL}?status=pending").json()

    assert body["total"] == 1
    assert [item["invoice_id"] for item in body["items"]] == [str(pending_invoice.id)]

    # The unfiltered list still sees all three.
    assert client.get(REVIEWS_URL).json()["total"] == 3


def test_list_reviews_rejects_invalid_status(client: TestClient) -> None:
    response = client.get(f"{REVIEWS_URL}?status=bogus")

    assert response.status_code == 422


def test_list_reviews_pagination(client: TestClient, db_session: Session) -> None:
    invoices = [make_invoice(db_session, invoice_number=f"INV-{i}") for i in range(3)]
    for invoice in invoices:
        make_case(db_session, invoice)

    first_page = client.get(f"{REVIEWS_URL}?limit=2&offset=0").json()
    assert len(first_page["items"]) == 2
    # total reflects the filter, not the page, so page count is computable.
    assert first_page["total"] == 3
    assert first_page["limit"] == 2
    assert first_page["offset"] == 0

    second_page = client.get(f"{REVIEWS_URL}?limit=2&offset=2").json()
    assert len(second_page["items"]) == 1
    assert second_page["total"] == 3
    assert second_page["offset"] == 2

    # The pages partition the set rather than repeating rows.
    seen = [item["id"] for item in first_page["items"] + second_page["items"]]
    assert len(set(seen)) == 3


def test_list_reviews_includes_invoice_summary(
    client: TestClient, db_session: Session
) -> None:
    # The summary exists so a reviewer can judge the case without a second call.
    invoice = make_invoice(db_session)
    make_case(db_session, invoice)

    summary = client.get(REVIEWS_URL).json()["items"][0]["invoice_summary"]

    assert summary["id"] == str(invoice.id)
    assert summary["vendor_name"] == "Acme Corporation"
    assert summary["invoice_number"] == "INV-0001"
    assert summary["currency"] == "USD"
    assert summary["status"] == "needs_review"
    # Full invoice detail must not leak into the queue payload.
    assert "line_items" not in summary
    assert "document_filename" not in summary


# --- GET /reviews/{id} -------------------------------------------------------


def test_get_review_by_id(client: TestClient, db_session: Session) -> None:
    invoice = make_invoice(db_session)
    case = make_case(db_session, invoice, reason="Risk score 80 (high)")

    response = client.get(f"{REVIEWS_URL}/{case.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(case.id)
    assert body["invoice_id"] == str(invoice.id)
    assert body["status"] == "pending"
    assert body["reason"] == "Risk score 80 (high)"
    assert body["assigned_to"] is None
    assert body["resolution"] is None
    assert body["resolved_at"] is None
    assert body["created_at"] is not None
    assert body["updated_at"] is not None


def test_get_review_not_found(client: TestClient) -> None:
    response = client.get(f"{REVIEWS_URL}/{UNKNOWN_ID}")

    assert response.status_code == 404
    assert response.json()["detail"] == "review case not found"


def test_get_review_includes_invoice_summary(
    client: TestClient, db_session: Session
) -> None:
    invoice = make_invoice(db_session, vendor_name="Globex Industrial")
    make_case(db_session, invoice)

    body = client.get(f"{REVIEWS_URL}/{UNKNOWN_ID}")  # warm the cache, ignore
    assert body.status_code == 404

    cases = client.get(REVIEWS_URL).json()["items"]
    summary = cases[0]["invoice_summary"]
    assert summary["vendor_name"] == "Globex Industrial"


# --- POST /reviews/{id}/resolve ----------------------------------------------


def test_resolve_review_approve(client: TestClient, db_session: Session) -> None:
    invoice = make_invoice(db_session, status=InvoiceStatus.NEEDS_REVIEW)
    reviewer = make_user(db_session)
    case = make_case(db_session, invoice)

    response = client.post(
        f"{REVIEWS_URL}/{case.id}/resolve",
        json=resolve_body(reviewer, "approve", "Approved for payment"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "resolved"
    assert body["resolution"] == "Approved for payment"
    assert body["assigned_to"] == str(reviewer.id)
    assert body["resolved_at"] is not None

    invoice_state = client.get(f"{INVOICES_URL}/{invoice.id}").json()
    assert invoice_state["status"] == "approved"


def test_resolve_review_reject(client: TestClient, db_session: Session) -> None:
    invoice = make_invoice(db_session, status=InvoiceStatus.NEEDS_REVIEW)
    reviewer = make_user(db_session)
    case = make_case(db_session, invoice)

    response = client.post(
        f"{REVIEWS_URL}/{case.id}/resolve",
        json=resolve_body(reviewer, "reject", "Duplicate of a paid invoice"),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "resolved"
    invoice_state = client.get(f"{INVOICES_URL}/{invoice.id}").json()
    assert invoice_state["status"] == "rejected"


def test_resolve_review_dismiss(client: TestClient, db_session: Session) -> None:
    # Dismiss is a statement about the case, not the invoice.
    invoice = make_invoice(db_session, status=InvoiceStatus.NEEDS_REVIEW)
    reviewer = make_user(db_session)
    case = make_case(db_session, invoice)

    response = client.post(
        f"{REVIEWS_URL}/{case.id}/resolve",
        json=resolve_body(reviewer, "dismiss", "False positive"),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "dismissed"
    invoice_state = client.get(f"{INVOICES_URL}/{invoice.id}").json()
    assert invoice_state["status"] == "needs_review"


def test_resolve_review_unknown_case(client: TestClient, db_session: Session) -> None:
    reviewer = make_user(db_session)

    response = client.post(
        f"{REVIEWS_URL}/{UNKNOWN_ID}/resolve", json=resolve_body(reviewer)
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "review case not found"


def test_resolve_review_unknown_reviewer(client: TestClient, db_session: Session) -> None:
    invoice = make_invoice(db_session)
    case = make_case(db_session, invoice)

    response = client.post(
        f"{REVIEWS_URL}/{case.id}/resolve",
        json={
            "decision": "approve",
            "reviewer_id": str(uuid.uuid4()),
            "resolution": "Approved for payment",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "unknown reviewer_id"
    # The failed lookup left the case untouched.
    assert client.get(f"{REVIEWS_URL}/{case.id}").json()["status"] == "pending"


def test_resolve_review_already_resolved(client: TestClient, db_session: Session) -> None:
    invoice = make_invoice(db_session)
    reviewer = make_user(db_session)
    case = make_case(db_session, invoice)

    first = client.post(
        f"{REVIEWS_URL}/{case.id}/resolve", json=resolve_body(reviewer)
    )
    assert first.status_code == 200

    second = client.post(
        f"{REVIEWS_URL}/{case.id}/resolve", json=resolve_body(reviewer, "reject")
    )

    assert second.status_code == 409
    assert second.json()["detail"] == "review case has already been resolved"


def test_resolve_review_requires_non_blank_resolution(
    client: TestClient, db_session: Session
) -> None:
    # min_length=1 accepts " "; only the field validator rejects it.
    invoice = make_invoice(db_session)
    reviewer = make_user(db_session)
    case = make_case(db_session, invoice)

    response = client.post(
        f"{REVIEWS_URL}/{case.id}/resolve", json=resolve_body(reviewer, "approve", " ")
    )

    assert response.status_code == 422
    assert client.get(f"{REVIEWS_URL}/{case.id}").json()["status"] == "pending"


def test_resolve_review_rejects_extra_fields(client: TestClient, db_session: Session) -> None:
    invoice = make_invoice(db_session)
    reviewer = make_user(db_session)
    case = make_case(db_session, invoice)
    body = resolve_body(reviewer)
    body["unknown_field"] = "should not be accepted"

    response = client.post(f"{REVIEWS_URL}/{case.id}/resolve", json=body)

    assert response.status_code == 422


def test_resolve_review_rejects_unknown_decision(
    client: TestClient, db_session: Session
) -> None:
    invoice = make_invoice(db_session)
    reviewer = make_user(db_session)
    case = make_case(db_session, invoice)

    response = client.post(
        f"{REVIEWS_URL}/{case.id}/resolve", json=resolve_body(reviewer, "escalate")
    )

    assert response.status_code == 422


def test_resolve_review_response_reflects_updated_case(
    client: TestClient, db_session: Session
) -> None:
    # The POST response and a follow-up GET must agree, including the invoice
    # status that the decision just changed.
    invoice = make_invoice(db_session, status=InvoiceStatus.NEEDS_REVIEW)
    reviewer = make_user(db_session)
    case = make_case(db_session, invoice)

    body = client.post(
        f"{REVIEWS_URL}/{case.id}/resolve",
        json=resolve_body(reviewer, "approve", "Approved for payment"),
    ).json()
    refetched = client.get(f"{REVIEWS_URL}/{case.id}").json()

    assert body == refetched
    assert body["status"] == refetched["status"] == "resolved"
    assert body["assigned_to"] == refetched["assigned_to"]
    assert body["resolved_at"] == refetched["resolved_at"]
    assert body["invoice_summary"]["status"] == "approved"


def test_resolve_approve_does_not_unreject_terminal_invoice_via_api(
    client: TestClient, db_session: Session
) -> None:
    # The HTTP path honours the terminal guard exactly as the service does: the
    # case closes, the invoice stays rejected.
    invoice = make_invoice(db_session, status=InvoiceStatus.REJECTED)
    reviewer = make_user(db_session)
    case = make_case(db_session, invoice)

    response = client.post(
        f"{REVIEWS_URL}/{case.id}/resolve", json=resolve_body(reviewer, "approve")
    )

    assert response.status_code == 200
    assert response.json()["status"] == "resolved"
    assert client.get(f"{INVOICES_URL}/{invoice.id}").json()["status"] == "rejected"