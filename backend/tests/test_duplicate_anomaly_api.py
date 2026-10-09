"""HTTP-level tests for the duplicate-check and anomaly-check endpoints.

Uses the shared ``client`` fixture (savepoint-scoped session + tmp storage
root), so every row and file written here is discarded on teardown.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.document import Document
from app.models.enums import InvoiceStatus
from app.models.invoice import Invoice, LineItem

INVOICES_URL = "/api/v1/invoices"
UNKNOWN_ID = "00000000-0000-0000-0000-000000000000"
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def make_invoice(
    db: Session,
    *,
    vendor: str | None = "Acme Corp",
    invoice_number: str = "INV-0001",
    total: Decimal = Decimal("100.00"),
    sha256: str | None = None,
    created_at: datetime = BASE,
    **overrides,
) -> Invoice:
    """Insert a Document + Invoice with one line item directly in the DB."""
    invoice = Invoice(
        document=Document(
            filename="invoice.pdf",
            content_type="application/pdf",
            size_bytes=2048,
            sha256=sha256 or uuid.uuid4().hex,
            storage_path="/storage/invoice.pdf",
        ),
        vendor_name=vendor,
        invoice_number=invoice_number,
        currency="USD",
        subtotal=total,
        tax_amount=Decimal("8.50"),
        total_amount=total,
        created_at=created_at,
        **overrides,
    )
    invoice.line_items = [
        LineItem(
            position=1,
            description="Widget",
            quantity=Decimal("1"),
            unit_price=Decimal("10.00"),
            line_total=total,
        )
    ]
    db.add(invoice)
    db.commit()
    return invoice


def make_duplicate_pair(db: Session) -> Invoice:
    """Two invoices sharing one document hash - a guaranteed duplicate."""
    shared = uuid.uuid4().hex
    make_invoice(db, invoice_number="INV-0001", sha256=shared)
    return make_invoice(db, invoice_number="INV-0002", sha256=shared)


# --- duplicates: POST -------------------------------------------------------


def test_check_duplicates_endpoint_success(
    client: TestClient, storage_root: Path, db_session: Session
) -> None:
    """POST returns the invoice detail payload after running the check."""
    invoice = make_duplicate_pair(db_session)

    response = client.post(f"{INVOICES_URL}/{invoice.id}/check-duplicates")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(invoice.id)
    assert "document_filename" in body
    # The detail response carries no verdict; that is GET's job.
    assert "verdict" not in body


def test_check_duplicates_endpoint_moves_duplicate_to_needs_review(
    client: TestClient, storage_root: Path, db_session: Session
) -> None:
    """A confirmed duplicate is reflected in the returned invoice status."""
    invoice = make_duplicate_pair(db_session)

    response = client.post(f"{INVOICES_URL}/{invoice.id}/check-duplicates")

    assert response.status_code == 200
    assert response.json()["status"] == "needs_review"


def test_check_duplicates_endpoint_unknown_id(client: TestClient) -> None:
    """An unknown invoice is a 404, not a 500 leaking the LookupError."""
    response = client.post(f"{INVOICES_URL}/{UNKNOWN_ID}/check-duplicates")

    assert response.status_code == 404
    assert response.json()["detail"] == "invoice not found"


# --- duplicates: GET --------------------------------------------------------


def test_get_duplicates_empty_state(
    client: TestClient, storage_root: Path, db_session: Session
) -> None:
    """Before any run: a neutral payload, not a 404."""
    invoice = make_invoice(db_session)

    response = client.get(f"{INVOICES_URL}/{invoice.id}/duplicates")

    assert response.status_code == 200
    assert response.json() == {
        "invoice_id": str(invoice.id),
        "verdict": "unique",
        "top_match": None,
        "all_matches": [],
        "notes": ["duplicate check has not been run"],
    }


def test_get_duplicates_after_check(
    client: TestClient, storage_root: Path, db_session: Session
) -> None:
    """After POST the read route returns the persisted report."""
    invoice = make_duplicate_pair(db_session)
    assert client.post(f"{INVOICES_URL}/{invoice.id}/check-duplicates").status_code == 200

    response = client.get(f"{INVOICES_URL}/{invoice.id}/duplicates")

    assert response.status_code == 200
    body = response.json()
    assert body["invoice_id"] == str(invoice.id)
    assert body["verdict"] == "duplicate"
    assert body["top_match"] is not None
    assert body["all_matches"]
    assert "identical_file" in body["top_match"]["signals"]
    # The empty-state note is gone, replaced by real findings.
    assert "duplicate check has not been run" not in body["notes"]


def test_get_duplicates_unknown_id(client: TestClient) -> None:
    """A missing invoice 404s rather than returning the empty state."""
    response = client.get(f"{INVOICES_URL}/{UNKNOWN_ID}/duplicates")

    assert response.status_code == 404
    assert response.json()["detail"] == "invoice not found"


# --- anomalies: POST --------------------------------------------------------


def test_check_anomalies_endpoint_success(
    client: TestClient, storage_root: Path, db_session: Session
) -> None:
    """POST returns the invoice detail payload after running the check."""
    invoice = make_invoice(db_session)

    response = client.post(f"{INVOICES_URL}/{invoice.id}/check-anomalies")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(invoice.id)
    assert "line_items" in body
    assert "verdict" not in body


def test_check_anomalies_endpoint_leaves_status_alone(
    client: TestClient, storage_root: Path, db_session: Session
) -> None:
    """Insufficient history must not move the invoice."""
    invoice = make_invoice(db_session, status=InvoiceStatus.EXTRACTED)

    response = client.post(f"{INVOICES_URL}/{invoice.id}/check-anomalies")

    assert response.status_code == 200
    assert response.json()["status"] == "extracted"


def test_check_anomalies_endpoint_unknown_id(client: TestClient) -> None:
    """An unknown invoice is a 404."""
    response = client.post(f"{INVOICES_URL}/{UNKNOWN_ID}/check-anomalies")

    assert response.status_code == 404
    assert response.json()["detail"] == "invoice not found"


# --- anomalies: GET ---------------------------------------------------------


def test_get_anomalies_empty_state(
    client: TestClient, storage_root: Path, db_session: Session
) -> None:
    """Before any run: a neutral payload, not a 404."""
    invoice = make_invoice(db_session)

    response = client.get(f"{INVOICES_URL}/{invoice.id}/anomalies")

    assert response.status_code == 200
    assert response.json() == {
        "invoice_id": str(invoice.id),
        "verdict": "insufficient_history",
        "method": "none",
        "score": None,
        "contributions": [],
        "history_size": 0,
        "notes": ["anomaly check has not been run"],
    }


def test_get_anomalies_after_check(
    client: TestClient, storage_root: Path, db_session: Session
) -> None:
    """After POST the read route returns the persisted report."""
    invoice = make_invoice(db_session)
    assert client.post(f"{INVOICES_URL}/{invoice.id}/check-anomalies").status_code == 200

    response = client.get(f"{INVOICES_URL}/{invoice.id}/anomalies")

    assert response.status_code == 200
    body = response.json()
    assert body["invoice_id"] == str(invoice.id)
    assert body["verdict"] == "insufficient_history"
    assert body["method"] == "none"
    assert body["score"] is None
    assert "anomaly check has not been run" not in body["notes"]


def test_get_anomalies_unknown_id(client: TestClient) -> None:
    """A missing invoice 404s."""
    response = client.get(f"{INVOICES_URL}/{UNKNOWN_ID}/anomalies")

    assert response.status_code == 404
    assert response.json()["detail"] == "invoice not found"