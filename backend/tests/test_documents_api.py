"""Tests for the document upload API."""

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import get_settings

# A tiny but structurally valid PDF - enough for the upload path, which does
# not inspect the file contents.
PDF_BYTES = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"

DOCUMENTS_URL = "/api/v1/documents"

# Valid multipart body whose file part declares an *empty* filename. httpx
# omits the ``filename`` parameter entirely when it is falsy, which Starlette
# treats as a plain form field (-> 422), so the raw body is sent directly.
EMPTY_FILENAME_BODY = (
    b'--b\r\nContent-Disposition: form-data; name="file"; filename=""\r\n'
    b"Content-Type: application/pdf\r\n\r\n"
    + PDF_BYTES
    + b"\r\n--b--\r\n"
)


def test_upload_pdf_creates_document(client: TestClient, storage_root: Path) -> None:
    response = client.post(
        DOCUMENTS_URL,
        files={"file": ("invoice.pdf", PDF_BYTES, "application/pdf")},
    )

    assert response.status_code == 201
    body = response.json()

    assert body["id"]
    assert body["filename"] == "invoice.pdf"
    assert body["content_type"] == "application/pdf"
    assert body["sha256"] == hashlib.sha256(PDF_BYTES).hexdigest()
    assert body["size_bytes"] == len(PDF_BYTES)
    assert body["processing_status"] == "uploaded"
    assert body["uploaded_at"]


def test_upload_rejects_unsupported_content_type(
    client: TestClient, storage_root: Path
) -> None:
    response = client.post(
        DOCUMENTS_URL,
        files={"file": ("notes.txt", b"not a document", "text/plain")},
    )

    assert response.status_code == 415


def test_upload_rejects_oversized_file(
    client: TestClient, storage_root: Path, monkeypatch
) -> None:
    monkeypatch.setattr(get_settings(), "max_upload_bytes", 8)

    payload = b"%PDF-1.4 " + b"x" * 64
    response = client.post(
        DOCUMENTS_URL,
        files={"file": ("big.pdf", payload, "application/pdf")},
    )

    assert response.status_code == 413


def test_upload_requires_filename(client: TestClient, storage_root: Path) -> None:
    response = client.post(
        DOCUMENTS_URL,
        content=EMPTY_FILENAME_BODY,
        headers={"Content-Type": "multipart/form-data; boundary=b"},
    )

    assert response.status_code == 400


def test_upload_is_content_addressed(client: TestClient, storage_root: Path) -> None:
    first = client.post(
        DOCUMENTS_URL,
        files={"file": ("first.pdf", PDF_BYTES, "application/pdf")},
    )
    second = client.post(
        DOCUMENTS_URL,
        files={"file": ("second.pdf", PDF_BYTES, "application/pdf")},
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["sha256"] == second.json()["sha256"]

    # Identical content maps to one sharded path: the second write is a no-op.
    stored = [path for path in storage_root.rglob("*") if path.is_file()]
    assert len(stored) == 1
    assert stored[0] == storage_root / first.json()["sha256"][:2] / first.json()[
        "sha256"
    ][2:4] / f"{first.json()['sha256']}.bin"


def test_get_document_by_id(client: TestClient, storage_root: Path) -> None:
    upload = client.post(
        DOCUMENTS_URL,
        files={"file": ("roundtrip.pdf", PDF_BYTES, "application/pdf")},
    )
    assert upload.status_code == 201
    document_id = upload.json()["id"]

    fetched = client.get(f"{DOCUMENTS_URL}/{document_id}")

    assert fetched.status_code == 200
    assert fetched.json() == upload.json()


def test_get_document_not_found(client: TestClient, storage_root: Path) -> None:
    response = client.get(f"{DOCUMENTS_URL}/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404