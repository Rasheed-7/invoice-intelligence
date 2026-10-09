"""Tests for soft-deleting invoices via DELETE /invoices/{id}.

Soft delete sets ``invoices.deleted_at`` and nothing else: every attached row
(the Document, LineItems, AnalysisResults, AuditEvents) must survive. A
deleted invoice disappears from the list, reads as 410 Gone, and is treated
as non-existent by the analysis endpoints.

Reuses the shared ``client`` / ``db_session`` / ``storage_root`` fixtures
from ``tests/conftest.py`` (savepoint-rolled-back session, temp storage).
"""

from pathlib import Path
from uuid import uuid4

import pymupdf
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.analysis import AnalysisResult
from app.models.audit import AuditEvent
from app.models.document import Document
from app.models.enums import (
    AnalysisStatus,
    AnalysisType,
    AuditActor,
    AuditEventType,
)
from app.models.invoice import Invoice

INVOICES_URL = "/api/v1/invoices"
UPLOAD_URL = "/api/v1/invoices/upload"


def _pdf_bytes(text: str = "invoice") -> bytes:
    """Build valid PDF bytes in memory; ``text`` varies the content."""
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    data = document.tobytes()
    document.close()
    return data


def _upload(client: TestClient, text: str = "invoice") -> str:
    """Upload one PDF and return the new invoice id."""
    response = client.post(
        UPLOAD_URL,
        files={"file": ("delete-test.pdf", _pdf_bytes(text), "application/pdf")},
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_delete_invoice_returns_204(
    client: TestClient, storage_root: Path
) -> None:
    invoice_id = _upload(client)

    response = client.delete(f"{INVOICES_URL}/{invoice_id}")

    assert response.status_code == 204
    assert response.content == b""


def test_delete_sets_deleted_at(
    client: TestClient, storage_root: Path, db_session: Session
) -> None:
    invoice_id = _upload(client)

    assert client.delete(f"{INVOICES_URL}/{invoice_id}").status_code == 204

    deleted_at = db_session.execute(
        select(Invoice.deleted_at).where(Invoice.id == invoice_id)
    ).scalar_one()
    assert deleted_at is not None


def test_delete_is_idempotent_via_409(
    client: TestClient, storage_root: Path
) -> None:
    invoice_id = _upload(client)

    assert client.delete(f"{INVOICES_URL}/{invoice_id}").status_code == 204

    second = client.delete(f"{INVOICES_URL}/{invoice_id}")
    assert second.status_code == 409
    assert second.json()["detail"] == "invoice is already deleted"


def test_deleted_invoice_not_in_list(
    client: TestClient, storage_root: Path
) -> None:
    keep_id = _upload(client, "keep")
    gone_id = _upload(client, "gone")

    assert client.delete(f"{INVOICES_URL}/{gone_id}").status_code == 204

    body = client.get(INVOICES_URL).json()
    ids = [item["id"] for item in body["items"]]
    assert keep_id in ids
    assert gone_id not in ids
    assert body["total"] == 1


def test_deleted_invoice_get_returns_410(
    client: TestClient, storage_root: Path
) -> None:
    invoice_id = _upload(client)

    assert client.delete(f"{INVOICES_URL}/{invoice_id}").status_code == 204

    response = client.get(f"{INVOICES_URL}/{invoice_id}")
    assert response.status_code == 410


def test_deleted_invoice_cannot_be_analyzed(
    client: TestClient, storage_root: Path
) -> None:
    invoice_id = _upload(client)

    assert client.delete(f"{INVOICES_URL}/{invoice_id}").status_code == 204

    response = client.post(f"{INVOICES_URL}/{invoice_id}/analyze")
    assert response.status_code == 404


def test_deleted_invoice_cannot_be_validated(
    client: TestClient, storage_root: Path
) -> None:
    invoice_id = _upload(client)

    assert client.delete(f"{INVOICES_URL}/{invoice_id}").status_code == 204

    response = client.post(f"{INVOICES_URL}/{invoice_id}/validate")
    assert response.status_code == 404


def test_deleted_invoice_cannot_be_assessed(
    client: TestClient, storage_root: Path
) -> None:
    invoice_id = _upload(client)

    assert client.delete(f"{INVOICES_URL}/{invoice_id}").status_code == 204

    response = client.post(f"{INVOICES_URL}/{invoice_id}/assess-risk")
    assert response.status_code == 404


def test_delete_preserves_analysis_results(
    client: TestClient, storage_root: Path, db_session: Session
) -> None:
    invoice_id = _upload(client)
    db_session.add(
        AnalysisResult(
            invoice_id=invoice_id,
            analysis_type=AnalysisType.CALCULATION_CHECK,
            status=AnalysisStatus.PASSED,
            details={"check_name": "survivor"},
        )
    )
    db_session.flush()

    assert client.delete(f"{INVOICES_URL}/{invoice_id}").status_code == 204

    rows = (
        db_session.execute(
            select(AnalysisResult).where(AnalysisResult.invoice_id == invoice_id)
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].details == {"check_name": "survivor"}


def test_delete_preserves_audit_events(
    client: TestClient, storage_root: Path, db_session: Session
) -> None:
    invoice_id = _upload(client)
    # Seed one pre-existing event, then count everything before deleting.
    db_session.add(
        AuditEvent(
            invoice_id=invoice_id,
            event_type=AuditEventType.NOTE,
            actor=AuditActor.SYSTEM,
            message="pre-delete marker",
        )
    )
    db_session.flush()
    before = len(
        db_session.execute(
            select(AuditEvent).where(AuditEvent.invoice_id == invoice_id)
        )
        .scalars()
        .all()
    )
    assert before >= 1

    assert client.delete(f"{INVOICES_URL}/{invoice_id}").status_code == 204

    after = (
        db_session.execute(
            select(AuditEvent).where(AuditEvent.invoice_id == invoice_id)
        )
        .scalars()
        .all()
    )
    # Every prior event survives, plus the deletion event itself.
    assert len(after) >= before + 1
    assert any(event.message == "pre-delete marker" for event in after)


def test_delete_preserves_document(
    client: TestClient, storage_root: Path, db_session: Session
) -> None:
    invoice_id = _upload(client)
    document_id = db_session.execute(
        select(Invoice.document_id).where(Invoice.id == invoice_id)
    ).scalar_one()

    assert client.delete(f"{INVOICES_URL}/{invoice_id}").status_code == 204

    document = db_session.get(Document, document_id)
    assert document is not None
    assert document.filename == "delete-test.pdf"


def test_delete_writes_audit_event(
    client: TestClient, storage_root: Path, db_session: Session
) -> None:
    invoice_id = _upload(client)

    assert client.delete(f"{INVOICES_URL}/{invoice_id}").status_code == 204

    events = (
        db_session.execute(
            select(AuditEvent).where(
                AuditEvent.invoice_id == invoice_id,
                AuditEvent.message.like("Invoice soft-deleted%"),
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].data is not None
    assert events[0].data["analysis"] == "delete"


def test_delete_unknown_invoice(client: TestClient) -> None:
    response = client.delete(f"{INVOICES_URL}/{uuid4()}")

    assert response.status_code == 404
    assert response.json()["detail"] == "invoice not found"


def test_deleted_invoice_returns_410_not_404(
    client: TestClient, storage_root: Path
) -> None:
    invoice_id = _upload(client)

    assert client.delete(f"{INVOICES_URL}/{invoice_id}").status_code == 204

    response = client.get(f"{INVOICES_URL}/{invoice_id}")
    assert response.status_code == 410
    assert response.status_code != 404
    assert response.json()["detail"] == "invoice deleted"


def test_recreate_after_delete_is_possible(
    client: TestClient, storage_root: Path
) -> None:
    data = _pdf_bytes("same bytes, new invoice")
    first = client.post(
        UPLOAD_URL,
        files={"file": ("recreate.pdf", data, "application/pdf")},
    )
    assert first.status_code == 201
    old_id = first.json()["id"]

    assert client.delete(f"{INVOICES_URL}/{old_id}").status_code == 204

    # Identical bytes: storage dedupes the file on disk, but a fresh upload
    # must create a brand-new invoice, not resurrect the deleted one.
    second = client.post(
        UPLOAD_URL,
        files={"file": ("recreate.pdf", data, "application/pdf")},
    )
    assert second.status_code == 201
    new_id = second.json()["id"]
    assert new_id != old_id
    assert client.get(f"{INVOICES_URL}/{new_id}").status_code == 200
    assert client.get(f"{INVOICES_URL}/{old_id}").status_code == 410

