"""Tests for the policy document endpoints.

Uses the shared ``client`` fixture (savepoint-scoped session + tmp storage
root), so every row and file written here is discarded on teardown.
"""

import uuid
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.policy import PolicyChunk

POLICIES_URL = "/api/v1/policies"
UNKNOWN_ID = "00000000-0000-0000-0000-000000000000"


def build_pdf(pages: list[str]) -> bytes:
    """Render one page per string in ``pages`` into a real PDF."""
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        # Draw line by line so each newline in the fixture becomes a line break
        # in the extracted text, which is what the chunker splits on.
        y = 100.0
        for line in text.split("\n"):
            if line.strip():
                page.insert_text((72, y), line)
            y += 20
    data = doc.tobytes()
    doc.close()
    return data


SIMPLE_PDF = build_pdf(
    [
        "Travel Policy\n\nExpenses under 50 dollars need no approval."
        "\n\nExpenses above 5000 dollars require CFO sign off.",
        "Travel Policy continued\n\nBook economy class for flights under six hours.",
    ]
)

SCANNED_PDF = build_pdf(["", ""])


def upload(
    client: TestClient,
    *,
    data: bytes = SIMPLE_PDF,
    title: str = "Travel Policy",
    filename: str = "travel.pdf",
    content_type: str = "application/pdf",
):
    return client.post(
        POLICIES_URL,
        files={"file": (filename, data, content_type)},
        data={"title": title},
    )


# --- upload -----------------------------------------------------------------


def test_upload_policy_creates_document(
    client: TestClient, storage_root: Path
) -> None:
    """A valid PDF returns 201 with the document fields and chunk_count 0.

    Processing is a separate call, so a freshly uploaded policy has no chunks.
    """
    response = upload(client)

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Travel Policy"
    assert body["source_filename"] == "travel.pdf"
    assert len(body["sha256"]) == 64
    assert body["is_active"] is True
    assert body["chunk_count"] == 0
    assert uuid.UUID(body["id"])


def test_upload_policy_requires_title(client: TestClient, storage_root: Path) -> None:
    """A blank title is rejected before anything is stored."""
    assert upload(client, title="   ").status_code == 400


def test_upload_policy_rejects_non_pdf_mime(
    client: TestClient, storage_root: Path
) -> None:
    """A PNG upload is a 415."""
    response = upload(
        client,
        data=b"\x89PNG\r\n\x1a\n",
        filename="scan.png",
        content_type="image/png",
    )

    assert response.status_code == 415


def test_upload_policy_rejects_non_pdf_extension(
    client: TestClient, storage_root: Path
) -> None:
    """PDF bytes under a .txt name is a 400 - extension check is independent."""
    response = upload(client, filename="policy.txt", content_type="application/pdf")

    assert response.status_code == 400


def test_upload_policy_rejects_oversized(
    client: TestClient, storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file over the configured ceiling is a 413."""
    from app.core.config import get_settings

    monkeypatch.setenv("MAX_UPLOAD_BYTES", "1024")
    get_settings.cache_clear()
    try:
        response = upload(client, data=b"%PDF-1.4" + b"0" * 2048)
    finally:
        monkeypatch.delenv("MAX_UPLOAD_BYTES", raising=False)
        get_settings.cache_clear()

    assert response.status_code == 413


def test_upload_policy_duplicate_returns_409_with_existing_id(
    client: TestClient, storage_root: Path
) -> None:
    """Re-uploading identical bytes is a 409 that names the existing document."""
    first = upload(client)
    assert first.status_code == 201
    existing_id = first.json()["id"]

    second = upload(client, title="A different title")

    assert second.status_code == 409
    detail = second.json()["detail"]
    assert detail["detail"] == "policy with this content already exists"
    # The client needs this to deep-link to the document already stored.
    assert detail["existing_id"] == existing_id


# --- processing -------------------------------------------------------------


def test_process_policy_creates_chunks(
    client: TestClient, storage_root: Path, db_session
) -> None:
    """Processing produces chunks, each with a non-null embedding."""
    policy_id = upload(client).json()["id"]

    response = client.post(f"{POLICIES_URL}/{policy_id}/process")

    assert response.status_code == 200
    assert response.json()["chunk_count"] > 0

    chunks = db_session.scalars(
        select(PolicyChunk).order_by(PolicyChunk.position)
    ).all()
    assert chunks
    for chunk in chunks:
        assert chunk.embedding is not None
        assert len(chunk.embedding) == 384
        assert chunk.page_number in (1, 2)
        assert chunk.token_count == len(chunk.content.split())


def test_process_policy_is_idempotent(
    client: TestClient, storage_root: Path, db_session
) -> None:
    """Processing twice replaces chunks rather than appending."""
    policy_id = upload(client).json()["id"]

    first = client.post(f"{POLICIES_URL}/{policy_id}/process").json()
    second = client.post(f"{POLICIES_URL}/{policy_id}/process").json()

    assert first["chunk_count"] == second["chunk_count"]
    assert first["chunk_count"] > 0

    positions = list(
        db_session.scalars(
            select(PolicyChunk.position).order_by(PolicyChunk.position)
        ).all()
    )
    assert len(positions) == second["chunk_count"]
    assert len(set(positions)) == len(positions)


def test_process_policy_unknown_id(client: TestClient, storage_root: Path) -> None:
    """Processing a document that does not exist is a 404."""
    response = client.post(f"{POLICIES_URL}/{UNKNOWN_ID}/process")

    assert response.status_code == 404
    assert response.json()["detail"] == "policy not found"


def test_process_policy_on_scanned_pdf_creates_no_chunks(
    client: TestClient, storage_root: Path
) -> None:
    """A PDF with no extractable text succeeds with zero chunks.

    A scan is a valid upload; there is simply nothing retrievable to index.
    """
    policy_id = upload(client, data=SCANNED_PDF, title="Scanned").json()["id"]

    response = client.post(f"{POLICIES_URL}/{policy_id}/process")

    assert response.status_code == 200
    assert response.json()["chunk_count"] == 0


# --- listing ----------------------------------------------------------------


def test_list_policies_empty(client: TestClient, storage_root: Path) -> None:
    """An empty corpus lists cleanly."""
    response = client.get(POLICIES_URL)

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["limit"] == 50
    assert body["offset"] == 0


def test_list_policies_includes_chunk_count(
    client: TestClient, storage_root: Path
) -> None:
    """chunk_count is computed per item, matching the detail endpoint."""
    processed_id = upload(client, title="Processed").json()["id"]
    assert client.post(f"{POLICIES_URL}/{processed_id}/process").status_code == 200
    upload(
        client,
        title="Not Processed",
        data=build_pdf(["Different content entirely."]),
    )

    body = client.get(POLICIES_URL).json()

    assert body["total"] == 2
    by_title = {item["title"]: item for item in body["items"]}
    assert by_title["Processed"]["chunk_count"] > 0
    assert by_title["Not Processed"]["chunk_count"] == 0

    # The aggregate must agree with the by-id response.
    detail = client.get(f"{POLICIES_URL}/{processed_id}").json()
    assert detail["chunk_count"] == by_title["Processed"]["chunk_count"]


def test_list_policies_pagination(client: TestClient, storage_root: Path) -> None:
    """limit/offset slice the list and report the unpaginated total."""
    for index in range(3):
        upload(client, title=f"Policy {index}", data=build_pdf([f"Body {index}."]))

    page = client.get(f"{POLICIES_URL}?limit=2&offset=0").json()

    assert page["total"] == 3
    assert len(page["items"]) == 2
    assert page["limit"] == 2


# --- single document --------------------------------------------------------


def test_get_policy_by_id(client: TestClient, storage_root: Path) -> None:
    """GET by id returns the document."""
    policy_id = upload(client).json()["id"]

    response = client.get(f"{POLICIES_URL}/{policy_id}")

    assert response.status_code == 200
    assert response.json()["id"] == policy_id
    assert response.json()["chunk_count"] == 0


def test_get_policy_not_found(client: TestClient, storage_root: Path) -> None:
    """An unknown id is a 404."""
    response = client.get(f"{POLICIES_URL}/{UNKNOWN_ID}")

    assert response.status_code == 404
    assert response.json()["detail"] == "policy not found"


def test_delete_policy_sets_is_active_false(
    client: TestClient, storage_root: Path
) -> None:
    """DELETE is a soft delete: 204, still readable, is_active now False.

    Chunks and embeddings are deliberately retained - reactivation should not
    require re-embedding.
    """
    policy_id = upload(client).json()["id"]
    assert client.post(f"{POLICIES_URL}/{policy_id}/process").status_code == 200

    deleted = client.delete(f"{POLICIES_URL}/{policy_id}")

    assert deleted.status_code == 204

    body = client.get(f"{POLICIES_URL}/{policy_id}").json()
    assert body["is_active"] is False
    # Still present, and the indexed content is intact.
    assert body["id"] == policy_id
    assert body["chunk_count"] > 0


def test_delete_policy_unknown_id(client: TestClient, storage_root: Path) -> None:
    """Deleting a document that does not exist is a 404."""
    assert client.delete(f"{POLICIES_URL}/{UNKNOWN_ID}").status_code == 404