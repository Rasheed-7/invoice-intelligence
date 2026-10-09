"""HTTP-level tests for POST /invoices/{id}/match-po and GET /invoices/{id}/po-match.

Invoices and purchase orders are inserted straight into the database so each
test controls the exact state under matching; only the two routes are exercised
over HTTP.
"""

import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.document import Document
from app.models.enums import InvoiceStatus
from app.models.invoice import Invoice, LineItem
from app.models.purchase_order import PurchaseOrder, PurchaseOrderItem
from app.schemas.invoice import InvoiceDetailResponse

INVOICES_URL = "/api/v1/invoices"
UNKNOWN_ID = "00000000-0000-0000-0000-000000000000"


def make_invoice(
    db: Session,
    *,
    line_items: list[LineItem] | None = None,
    po_reference: str | None = None,
    **overrides,
) -> Invoice:
    """Insert a Document + Invoice (+ optional line items) directly in the DB."""
    data = {
        "filename": "acme.pdf",
        "content_type": "application/pdf",
        "size_bytes": 2048,
        "sha256": "0" * 64,
        "storage_path": "/storage/acme.pdf",
        "vendor_name": "Acme Corp",
        "invoice_number": "INV-0001",
        "currency": "USD",
        "subtotal": "100.00",
        "tax_amount": "8.50",
        "total_amount": "108.50",
        "status": InvoiceStatus.INGESTED,
        # The PO reference lives in its own column, written by extraction.
        "po_number": po_reference,
    }
    data.update(overrides)
    document = Document(
        filename=data.pop("filename"),
        content_type=data.pop("content_type"),
        size_bytes=data.pop("size_bytes"),
        sha256=data.pop("sha256"),
        storage_path=data.pop("storage_path"),
    )
    invoice = Invoice(document=document, **data)
    if line_items is not None:
        invoice.line_items = line_items
    db.add(invoice)
    db.commit()
    return invoice


def make_po(
    db: Session,
    *,
    items: list[PurchaseOrderItem] | None = None,
    po_number: str = "PO-1",
) -> PurchaseOrder:
    po = PurchaseOrder(
        po_number=po_number,
        vendor_name="Acme Corp",
        total_amount="108.50",
    )
    if items is not None:
        po.items = items
    db.add(po)
    db.commit()
    return po


def po_line_item(quantity: str) -> LineItem:
    return LineItem(
        position=1,
        description="Widget",
        quantity=quantity,
        unit_price="100.00",
        line_total="100.00",
    )


def po_line(quantity: str) -> PurchaseOrderItem:
    return PurchaseOrderItem(
        position=1,
        description="Widget",
        quantity=quantity,
        unit_price="100.00",
        line_total="100.00",
    )


def test_match_po_endpoint_success(
    client: TestClient, storage_root: Path, db_session: Session
) -> None:
    """POST /match-po runs matching and returns the invoice detail payload."""
    po = make_po(db_session, items=[po_line("10")])
    invoice = make_invoice(
        db_session, line_items=[po_line_item("10")], po_reference=po.po_number
    )

    response = client.post(f"{INVOICES_URL}/{invoice.id}/match-po")

    assert response.status_code == 200


def test_match_po_endpoint_unknown_invoice(client: TestClient) -> None:
    """A missing invoice is a 404, not a 500 leaking the LookupError."""
    response = client.post(f"{INVOICES_URL}/{UNKNOWN_ID}/match-po")

    assert response.status_code == 404
    assert response.json()["detail"] == "invoice not found"


def test_get_po_match_unknown_invoice_returns_404(client: TestClient) -> None:
    """An invoice that does not exist 404s, same as POST /match-po.

    Returning the neutral skipped payload here would make a nonexistent
    invoice indistinguishable from one that exists but was never matched.
    """
    response = client.get(f"{INVOICES_URL}/{UNKNOWN_ID}/po-match")

    assert response.status_code == 404
    assert response.json()["detail"] == "invoice not found"


def test_get_po_match_empty_state(
    client: TestClient, storage_root: Path, db_session: Session
) -> None:
    """Before any match: the neutral skipped-shaped payload, not an error.

    The invoice is real, so the neutral body is correct here - this is the
    case the 404 above must not swallow.
    """
    invoice = make_invoice(db_session, line_items=[po_line_item("10")])

    response = client.get(f"{INVOICES_URL}/{invoice.id}/po-match")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "invoice_id": str(invoice.id),
        "po_reference": None,
        "po_found": False,
        "overall_outcome": "skipped",
        "line_matches": [],
        "unmatched_invoice_items": [],
        "unmatched_po_items": [],
        "mismatch_summary": {},
        "vendor_matches": None,
        "total_matches": None,
        "notes": ["no PO match has been run"],
    }


def test_get_po_match_after_match(
    client: TestClient, storage_root: Path, db_session: Session
) -> None:
    """After POST /match-po the read route returns the same persisted report."""
    po = make_po(db_session, items=[po_line("10")])
    invoice = make_invoice(
        db_session, line_items=[po_line_item("12")], po_reference=po.po_number
    )
    assert client.post(f"{INVOICES_URL}/{invoice.id}/match-po").status_code == 200

    response = client.get(f"{INVOICES_URL}/{invoice.id}/po-match")

    assert response.status_code == 200
    body = response.json()
    assert body["invoice_id"] == str(invoice.id)
    assert body["po_reference"] == po.po_number
    assert body["po_found"] is True
    assert body["overall_outcome"] == "mismatch"
    assert body["mismatch_summary"] == {"quantity": 1}


def test_match_po_response_includes_fields(
    client: TestClient, storage_root: Path, db_session: Session
) -> None:
    """The route still returns the full InvoiceDetailResponse shape."""
    po = make_po(db_session, items=[po_line("10")])
    invoice = make_invoice(
        db_session, line_items=[po_line_item("10")], po_reference=po.po_number
    )

    response = client.post(f"{INVOICES_URL}/{invoice.id}/match-po")

    assert response.status_code == 200
    body = response.json()
    # InvoiceDetailResponse flattens the document relationship into
    # document_* fields rather than nesting a "document" object.
    assert set(InvoiceDetailResponse.model_fields) <= set(body)
    assert body["id"] == str(invoice.id)
    assert body["status"] == "ingested"
    assert body["document_filename"] == "acme.pdf"
    assert body["document_processing_status"] == "uploaded"
    assert body["vendor_name"] == "Acme Corp"
    assert body["currency"] == "USD"
    assert len(body["line_items"]) == 1
    assert body["line_items"][0]["description"] == "Widget"
    assert uuid.UUID(body["document_id"]) == invoice.document_id