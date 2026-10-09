"""Contract test: POST /invoices/{id}/validate returns the full detail shape."""

from pathlib import Path

from fastapi.testclient import TestClient

INVOICES_URL = "/api/v1/invoices"
UPLOAD_URL = "/api/v1/invoices/upload"


def test_validate_endpoint_response_shape(
    client: TestClient, storage_root: Path
) -> None:
    """After a successful validate, the response carries every expected field.

    The response model is InvoiceDetailResponse - the same shape as GET
    /invoices/{id} - so this pins id, status, document_*, extracted fields,
    and line_items in one assertion.
    """
    from app.services.llm.fixture_pdfs import build_fixture_pdf
    from app.services.llm.fixtures import FIXTURES

    upload = client.post(
        UPLOAD_URL,
        files={
            "file": ("shape.pdf", build_fixture_pdf(FIXTURES[0]), "application/pdf")
        },
    )
    assert upload.status_code == 201
    invoice_id = upload.json()["id"]
    assert client.post(f"{INVOICES_URL}/{invoice_id}/analyze").status_code == 200

    response = client.post(f"{INVOICES_URL}/{invoice_id}/validate")

    assert response.status_code == 200
    body = response.json()

    # Identity and lifecycle.
    assert set(body) >= {
        "id",
        "status",
        "created_at",
        "updated_at",
    }
    assert body["id"] == invoice_id
    assert body["status"] == "validated"

    # Flattened document fields.
    assert set(body) >= {
        "document_id",
        "document_filename",
        "document_content_type",
        "document_size_bytes",
        "document_processing_status",
    }
    assert body["document_filename"] == "shape.pdf"
    assert body["document_processing_status"] == "text_extracted"

    # Extracted invoice fields (nullable, but keys must exist).
    assert set(body) >= {
        "vendor_name",
        "invoice_number",
        "invoice_date",
        "due_date",
        "currency",
        "po_number",
        "subtotal",
        "tax_amount",
        "total_amount",
    }
    assert body["vendor_name"] == "Acme Corporation"
    assert body["currency"] == "USD"
    assert body["subtotal"] is not None
    assert body["tax_amount"] is not None
    assert body["total_amount"] is not None

    # Line items with their own full field set.
    assert isinstance(body["line_items"], list)
    assert len(body["line_items"]) == 1
    line_item = body["line_items"][0]
    assert set(line_item) >= {
        "id",
        "position",
        "description",
        "quantity",
        "unit_price",
        "line_total",
        "tax_rate",
    }
    assert line_item["position"] == 1
    assert line_item["quantity"] is not None
    assert line_item["unit_price"] is not None
    assert line_item["line_total"] is not None

    # No unexpected top-level fields beyond the detail contract.
    assert set(body) == {
        "id",
        "status",
        "created_at",
        "updated_at",
        "document_id",
        "document_filename",
        "document_content_type",
        "document_size_bytes",
        "document_processing_status",
        "vendor_name",
        "invoice_number",
        "invoice_date",
        "due_date",
        "currency",
        "po_number",
        "subtotal",
        "tax_amount",
        "total_amount",
        "line_items",
    }
