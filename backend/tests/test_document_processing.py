"""Tests for the document extraction service.

Uses the shared ``db_session`` savepoint fixture: the service commits, which
releases a savepoint inside the outer transaction, and teardown rolls the
outer transaction back so no rows persist between tests. Every PDF is built
in memory; nothing is written to disk except the stored file itself.
"""

import hashlib
import uuid
from pathlib import Path

import pymupdf
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import Document
from app.models.document_page import DocumentPage
from app.models.enums import DocumentProcessingStatus
from app.services.document_processing import (
    DocumentNotFound,
    DocumentProcessingError,
    process_document_extraction,
)
from app.services.storage import LocalStorage


def build_pdf(pages: list[str]) -> bytes:
    """Build a PDF in memory with one page per string (``""`` = blank page)."""

    doc = pymupdf.open()
    try:
        for text in pages:
            page = doc.new_page()
            if text:
                page.insert_text((72, 72), text)
        return doc.tobytes()
    finally:
        doc.close()


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    """A local-disk storage backend rooted at a per-test temp directory."""

    return LocalStorage(tmp_path)


def store_document(
    db: Session,
    storage: LocalStorage,
    data: bytes,
    *,
    content_type: str = "application/pdf",
    filename: str = "test.pdf",
) -> Document:
    """Persist ``data`` through ``storage`` and stage the ``Document`` row."""

    sha256 = hashlib.sha256(data).hexdigest()
    key = storage.save(f"{sha256[:2]}/{sha256[2:4]}/{sha256}.bin", data)
    document = Document(
        filename=filename,
        content_type=content_type,
        size_bytes=len(data),
        sha256=sha256,
        storage_path=key,
    )
    db.add(document)
    db.flush()
    return document


def fetch_pages(db: Session, document_id: uuid.UUID) -> list[DocumentPage]:
    """Return the document's pages ordered by page number."""

    stmt = (
        select(DocumentPage)
        .where(DocumentPage.document_id == document_id)
        .order_by(DocumentPage.page_number)
    )
    return list(db.scalars(stmt).all())


def test_extract_updates_status_and_pages(db_session: Session, storage: LocalStorage) -> None:
    document = store_document(
        db_session, storage, build_pdf(["first page text", "second page text"])
    )

    result = process_document_extraction(
        db_session, document_id=document.id, storage=storage
    )

    assert result.processing_status == DocumentProcessingStatus.TEXT_EXTRACTED
    assert result.extracted_text
    assert "first page text" in result.extracted_text
    assert "second page text" in result.extracted_text

    pages = fetch_pages(db_session, document.id)
    assert [page.page_number for page in pages] == [1, 2]


def test_extract_is_idempotent(db_session: Session, storage: LocalStorage) -> None:
    document = store_document(db_session, storage, build_pdf(["alpha content", "beta content"]))

    first = process_document_extraction(
        db_session, document_id=document.id, storage=storage
    )
    first_text = first.extracted_text

    second = process_document_extraction(
        db_session, document_id=document.id, storage=storage
    )

    assert second.extracted_text == first_text

    # Still two pages, not four: the re-run replaced rather than appended.
    pages = fetch_pages(db_session, document.id)
    assert [page.page_number for page in pages] == [1, 2]


def test_extract_unsupported_content_type_marks_failed(
    db_session: Session, storage: LocalStorage
) -> None:
    document = store_document(
        db_session,
        storage,
        b"\x89PNG\r\n\x1a\n",
        content_type="image/png",
        filename="scan.png",
    )

    with pytest.raises(DocumentProcessingError):
        process_document_extraction(db_session, document_id=document.id, storage=storage)

    # The service committed the FAILED status before re-raising.
    assert document.processing_status == DocumentProcessingStatus.FAILED


def test_extract_empty_pdf_still_marked_extracted(
    db_session: Session, storage: LocalStorage
) -> None:
    document = store_document(db_session, storage, build_pdf([""]))

    result = process_document_extraction(
        db_session, document_id=document.id, storage=storage
    )

    # Empty extraction is not a failure: whether blank text matters is decided
    # by a later analysis step, not by extraction.
    assert result.processing_status == DocumentProcessingStatus.TEXT_EXTRACTED
    assert (result.extracted_text or "").strip() == ""

    pages = fetch_pages(db_session, document.id)
    assert len(pages) == 1
    assert pages[0].page_number == 1
    assert pages[0].text.strip() == ""


def test_extract_missing_document_raises(db_session: Session, storage: LocalStorage) -> None:
    with pytest.raises(DocumentNotFound):
        process_document_extraction(db_session, document_id=uuid.uuid4(), storage=storage)