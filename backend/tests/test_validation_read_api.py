"""Tests for GET /invoices/{id}/validation (validation read endpoint)."""

from pathlib import Path

from fastapi.testclient import TestClient

INVOICES_URL = "/api/v1/invoices"
UPLOAD_URL = "/api/v1/invoices/upload"


def upload_fixture(client: TestClient, name: str = "acme.pdf") -> str:
    """Upload the acme-simple fixture PDF and return the invoice id."""
    from app.services.llm.fixture_pdfs import build_fixture_pdf
    from app.services.llm.fixtures import FIXTURES

    upload = client.post(
        UPLOAD_URL,
        files={"file": (name, build_fixture_pdf(FIXTURES[0]), "application/pdf")},
    )
    assert upload.status_code == 201
    return upload.json()["id"]


def test_get_validation_returns_checks_after_validate(
    client: TestClient, storage_root: Path
) -> None:
    """Full pipeline: upload -> analyze -> validate -> read back 6 checks."""
    invoice_id = upload_fixture(client)
    assert client.post(f"{INVOICES_URL}/{invoice_id}/analyze").status_code == 200
    assert client.post(f"{INVOICES_URL}/{invoice_id}/validate").status_code == 200

    response = client.get(f"{INVOICES_URL}/{invoice_id}/validation")

    assert response.status_code == 200
    body = response.json()
    assert body["invoice_id"] == invoice_id
    assert len(body["checks"]) == 7
    assert body["overall_status"] == "passed"
    counts = body["counts"]
    assert counts["passed"] + counts["failed"] + counts["warning"] + counts["skipped"] == 7
    assert set(body["checks"][0]) >= {
        "check_name",
        "rule_id",
        "status",
        "message",
        "severity",
        "details",
    }


def test_get_validation_empty_when_never_run(
    client: TestClient, storage_root: Path
) -> None:
    """No validation rows yet: empty checks, skipped overall, zero counts."""
    invoice_id = upload_fixture(client)

    response = client.get(f"{INVOICES_URL}/{invoice_id}/validation")

    assert response.status_code == 200
    body = response.json()
    assert body["invoice_id"] == invoice_id
    assert body["checks"] == []
    assert body["overall_status"] == "skipped"
    assert body["counts"] == {"passed": 0, "failed": 0, "warning": 0, "skipped": 0}


def test_get_validation_unknown_invoice(
    client: TestClient, storage_root: Path
) -> None:
    """Unknown id returns 404 with the standard detail."""
    response = client.get(
        f"{INVOICES_URL}/00000000-0000-0000-0000-000000000000/validation"
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "invoice not found"


def test_get_validation_check_details_present(
    client: TestClient, storage_root: Path
) -> None:
    """Persisted evidence survives the JSONB round-trip with exact values."""
    invoice_id = upload_fixture(client)
    assert client.post(f"{INVOICES_URL}/{invoice_id}/analyze").status_code == 200
    assert client.post(f"{INVOICES_URL}/{invoice_id}/validate").status_code == 200

    response = client.get(f"{INVOICES_URL}/{invoice_id}/validation")

    assert response.status_code == 200
    by_name = {check["check_name"]: check for check in response.json()["checks"]}
    tax_check = by_name["subtotal_plus_tax"]
    assert tax_check["status"] == "passed"
    assert tax_check["rule_id"] == "SUBTOTAL_PLUS_TAX"
    assert float(tax_check["details"]["expected_total"]) == 108.50
    assert float(tax_check["details"]["actual_total"]) == 108.50
    assert float(tax_check["details"]["delta"]) == 0.00
