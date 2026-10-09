"""HTTP-level tests for POST /invoices/{id}/policy-check and GET /{id}/policy-evidence."""

import uuid
from pathlib import Path

import pymupdf
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.enums import AnalysisType
from app.models.policy import PolicyChunk

INVOICES_URL = "/api/v1/invoices"
POLICIES_URL = "/api/v1/policies"
UNKNOWN_ID = "00000000-0000-0000-0000-000000000000"

APPROVAL_POLICY_PAGES = [
    "Approval Policy\n\n"
    "Manager approval is required for any invoice over 5000 USD.\n\n"
    "Invoices over 20000 USD require director approval.",
]


def build_pdf(pages: list[str]) -> bytes:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        y = 100.0
        for line in text.split("\n"):
            if line.strip():
                page.insert_text((72, y), line)
            y += 20
    data = doc.tobytes()
    doc.close()
    return data


def ingest_policy(client: TestClient) -> str:
    upload = client.post(
        POLICIES_URL,
        files={
            "file": (
                "approval.pdf",
                build_pdf(APPROVAL_POLICY_PAGES),
                "application/pdf",
            )
        },
        data={"title": "Approval Policy"},
    )
    assert upload.status_code == 201, upload.text
    policy_id = upload.json()["id"]
    assert client.post(f"{POLICIES_URL}/{policy_id}/process").status_code == 200
    return policy_id


def upload_invoice(client: TestClient) -> str:
    from app.services.llm.fixture_pdfs import build_fixture_pdf
    from app.services.llm.fixtures import FIXTURES

    upload = client.post(
        f"{INVOICES_URL}/upload",
        files={"file": ("acme.pdf", build_fixture_pdf(FIXTURES[0]), "application/pdf")},
    )
    assert upload.status_code == 201
    invoice_id = upload.json()["id"]
    assert client.post(f"{INVOICES_URL}/{invoice_id}/analyze").status_code == 200
    return invoice_id


# --- POST /policy-check -----------------------------------------------------


def test_policy_check_endpoint_success(
    client: TestClient, storage_root: Path, db_session: Session
) -> None:
    """POST returns the invoice detail payload after running the check."""
    ingest_policy(client)
    invoice_id = upload_invoice(client)

    response = client.post(f"{INVOICES_URL}/{invoice_id}/policy-check")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == invoice_id
    assert "document_filename" in body
    assert "line_items" in body
    # The invoice detail response carries no evidence; that is GET's job.
    assert "evidence" not in body

    stored = db_session.execute(
        select(func.count()).select_from(PolicyChunk)
    ).scalar_one()
    assert stored  # the policy really was indexed


def test_policy_check_endpoint_unknown_id(client: TestClient, storage_root: Path) -> None:
    """An unknown invoice is a 404."""
    response = client.post(f"{INVOICES_URL}/{UNKNOWN_ID}/policy-check")

    assert response.status_code == 404
    assert response.json()["detail"] == "invoice not found"


def test_policy_check_endpoint_leaves_status_alone(
    client: TestClient, storage_root: Path
) -> None:
    """Running the check does not move the invoice out of EXTRACTED."""
    ingest_policy(client)
    invoice_id = upload_invoice(client)

    response = client.post(f"{INVOICES_URL}/{invoice_id}/policy-check")

    assert response.status_code == 200
    assert response.json()["status"] == "extracted"


# --- GET /policy-evidence ---------------------------------------------------


def test_policy_evidence_empty_state(
    client: TestClient, storage_root: Path
) -> None:
    """Before any run the invoice exists but has no finding."""
    invoice_id = upload_invoice(client)

    response = client.get(f"{INVOICES_URL}/{invoice_id}/policy-evidence")

    assert response.status_code == 200
    assert response.json() == {
        "rule_id": "NOT_RUN",
        "status": "skipped",
        "message": "policy analysis has not been run",
        "evidence": [],
    }


def test_policy_evidence_after_run(
    client: TestClient, storage_root: Path
) -> None:
    """After POST, GET returns the persisted finding with evidence."""
    ingest_policy(client)
    invoice_id = upload_invoice(client)
    assert client.post(f"{INVOICES_URL}/{invoice_id}/policy-check").status_code == 200

    response = client.get(f"{INVOICES_URL}/{invoice_id}/policy-evidence")

    assert response.status_code == 200
    body = response.json()
    assert body["rule_id"] == "POLICY_EVIDENCE_FOUND"
    assert body["status"] == "passed"
    assert body["evidence"]
    for item in body["evidence"]:
        assert set(item) == {
            "chunk_id",
            "policy_document_id",
            "policy_title",
            "page_number",
            "similarity",
            "text",
            "retrieval_query",
        }
        uuid.UUID(item["chunk_id"])
        assert item["policy_title"] == "Approval Policy"
        assert item["text"].strip()
        assert item["retrieval_query"].strip()


def test_policy_evidence_unknown_id(client: TestClient, storage_root: Path) -> None:
    """A missing invoice 404s rather than returning the empty state."""
    response = client.get(f"{INVOICES_URL}/{UNKNOWN_ID}/policy-evidence")

    assert response.status_code == 404
    assert response.json()["detail"] == "invoice not found"


def test_policy_evidence_no_corpus_is_skipped_not_unrun(
    client: TestClient, storage_root: Path
) -> None:
    """A real "no policy applies" finding is distinguishable from NOT_RUN."""
    invoice_id = upload_invoice(client)
    assert client.post(f"{INVOICES_URL}/{invoice_id}/policy-check").status_code == 200

    body = client.get(f"{INVOICES_URL}/{invoice_id}/policy-evidence").json()

    assert body["rule_id"] == "NO_APPLICABLE_POLICY"
    assert body["status"] == "skipped"
    assert body["evidence"] == []
    # This is the key distinction: NOT_RUN means "not looked", this means
    # "looked, found nothing". Collapsing them would be a lie to the caller.
    assert body["rule_id"] != "NOT_RUN"


def test_policy_check_endpoint_is_idempotent(
    client: TestClient, storage_root: Path, db_session: Session
) -> None:
    """Calling the endpoint twice leaves exactly one POLICY_CHECK row."""
    from app.models.analysis import AnalysisResult

    ingest_policy(client)
    invoice_id = upload_invoice(client)

    assert client.post(f"{INVOICES_URL}/{invoice_id}/policy-check").status_code == 200
    first = client.get(f"{INVOICES_URL}/{invoice_id}/policy-evidence").json()

    assert client.post(f"{INVOICES_URL}/{invoice_id}/policy-check").status_code == 200
    second = client.get(f"{INVOICES_URL}/{invoice_id}/policy-evidence").json()

    rows = db_session.scalars(
        select(AnalysisResult).where(
            AnalysisResult.invoice_id == uuid.UUID(invoice_id),
            AnalysisResult.analysis_type == AnalysisType.POLICY_CHECK,
        )
    ).all()
    assert len(rows) == 1
    assert second == first