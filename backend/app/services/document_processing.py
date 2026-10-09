"""Document extraction orchestration.

Turns a stored document's raw bytes into per-page ``DocumentPage`` rows plus
the aggregated ``Document.extracted_text``. Deliberately free of FastAPI
imports so the pipeline can be exercised directly from tests.
"""

from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models.document import Document
from app.models.document_page import DocumentPage
from app.models.enums import DocumentProcessingStatus
from app.services.documents import get_document
from app.services.extraction import (
    ExtractionError,
    UnsupportedContentTypeError,
    get_document_processor,
)
from app.services.storage import StorageBackend

__all__ = [
    "DocumentNotFound",
    "DocumentProcessingError",
    "process_document_extraction",
]


class DocumentNotFound(LookupError):
    """Raised when the requested document does not exist."""


# No DocumentProcessingFailed exists elsewhere in the codebase, so this is the
# canonical failure name - nothing to alias.
class DocumentProcessingError(Exception):
    """Raised when extraction could not be completed for a document."""


def process_document_extraction(
    db: Session, *, document_id: UUID, storage: StorageBackend
) -> Document:
    """Extract text from a document's stored file and persist the result.

    Re-processing is allowed and overwrites any pages written by a previous
    run. This function commits on success *and* on failure (so the FAILED
    status survives), then refreshes and returns the document.
    """

    document = get_document(db, document_id)
    if document is None:
        raise DocumentNotFound(f"document {document_id} not found")

    data = storage.read(document.storage_path)

    try:
        processor = get_document_processor(document.content_type)
    except UnsupportedContentTypeError as exc:
        document.processing_status = DocumentProcessingStatus.FAILED
        db.commit()
        raise DocumentProcessingError("unsupported content type for extraction") from exc

    try:
        result = processor.process(data)
    except ExtractionError as exc:
        document.processing_status = DocumentProcessingStatus.FAILED
        db.commit()
        raise DocumentProcessingError(f"extraction failed: {exc}") from exc

    # Re-processing overwrites: drop the pages written by any previous run.
    db.execute(delete(DocumentPage).where(DocumentPage.document_id == document.id))
    for page in result.pages:
        db.add(
            DocumentPage(
                document_id=document.id,
                page_number=page.page_number,
                text=page.text,
                page_metadata=page.page_metadata,
            )
        )

    # Empty text from a scanned PDF is valid input; its disposition is a
    # business decision for a later analysis step, not an extraction decision.
    document.extracted_text = result.full_text
    document.processing_status = DocumentProcessingStatus.TEXT_EXTRACTED
    db.commit()
    db.refresh(document)
    return document