"""Policy document ingestion.

Deliberately free of FastAPI imports: the upload rules (content-type, size
ceiling, content-addressed storage) and the chunk/embed pipeline are business
logic and must be testable without HTTP.

Split into two entry points so a caller controls when the expensive half runs:

* :func:`create_policy` validates and stores bytes, and does nothing else.
* :func:`process_policy` extracts, chunks, embeds and persists.
"""

import hashlib
from pathlib import Path
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.policy import PolicyChunk, PolicyDocument
from app.services.embeddings.factory import get_embedding_provider
from app.services.extraction import DocumentText, ExtractionError, PyMuPDFProcessor
from app.services.policy_chunking import chunk_document
from app.services.storage import StorageBackend

__all__ = [
    "DuplicatePolicy",
    "InvalidPolicyExtension",
    "PolicyProcessingError",
    "PolicyTooLarge",
    "UnsupportedPolicyContentType",
    "create_policy",
    "get_policy",
    "process_policy",
]


class DuplicatePolicy(Exception):
    """Raised when a policy with the same sha256 already exists.

    Carries ``existing_id`` so the caller can point the user at the document
    that already holds this content, instead of a bare 409 with no way forward.
    """

    def __init__(self, message: str, existing_id: UUID) -> None:
        super().__init__(message)
        self.existing_id = existing_id


class UnsupportedPolicyContentType(Exception):
    """Raised when a policy upload is not a PDF."""


class InvalidPolicyExtension(Exception):
    """Raised when a policy upload's extension is not ``.pdf``."""


class PolicyTooLarge(Exception):
    """Raised when a policy upload exceeds the configured maximum size."""


class PolicyProcessingError(Exception):
    """Raised when extraction fails for a stored policy document."""


#: Policies are PDF-only. Unlike invoice uploads there is no image path, so
#: this is an exact match rather than the settings allow-list.
POLICY_CONTENT_TYPE = "application/pdf"
POLICY_EXTENSION = ".pdf"


def _storage_key(sha256: str) -> str:
    """Content-addressed key for a policy, namespaced away from invoices.

    Two levels of hash sharding keep any single directory from growing
    unbounded. The ``policies/`` prefix is what lets invoices and policies share
    one storage root without colliding.
    """
    return f"policies/{sha256[:2]}/{sha256[2:4]}/{sha256}.pdf"


def create_policy(
    db: Session,
    *,
    title: str,
    filename: str,
    content_type: str,
    data: bytes,
    storage: StorageBackend,
) -> PolicyDocument:
    """Validate an upload, store its bytes, and stage the ``PolicyDocument`` row.

    No extraction, chunking or embedding happens here - :func:`process_policy`
    does that. The row is added to the session but *not* committed: the caller
    owns the transaction.

    Raises:
        UnsupportedPolicyContentType: Not ``application/pdf``.
        InvalidPolicyExtension: Extension is not ``.pdf``.
        PolicyTooLarge: Larger than ``settings.max_upload_bytes``.
        DuplicatePolicy: A policy with the same sha256 is already stored.
    """
    if content_type != POLICY_CONTENT_TYPE:
        raise UnsupportedPolicyContentType(
            f"policies must be {POLICY_CONTENT_TYPE}, got {content_type!r}"
        )

    # Extension check is belt-and-braces with the MIME check, not a replacement:
    # the extension is what a human sees in a download link.
    ext = Path(filename).suffix.lower()
    if ext != POLICY_EXTENSION:
        raise InvalidPolicyExtension(f"extension not allowed: {ext!r}")

    settings = get_settings()
    if len(data) > settings.max_upload_bytes:
        raise PolicyTooLarge(
            f"{len(data)} bytes exceeds limit of {settings.max_upload_bytes}"
        )

    sha256 = hashlib.sha256(data).hexdigest()

    existing = db.execute(
        select(PolicyDocument.id).where(PolicyDocument.sha256 == sha256)
    ).scalar_one_or_none()
    if existing is not None:
        raise DuplicatePolicy(
            f"policy already stored with sha256 {sha256}", existing_id=existing
        )

    # Content-addressed storage makes this idempotent: identical bytes always
    # map to the same key.
    path = storage.save(_storage_key(sha256), data)

    document = PolicyDocument(
        title=title,
        source_filename=filename,
        sha256=sha256,
        storage_path=path,
        is_active=True,
    )
    db.add(document)
    # Flush so the UUID PK is populated before the caller links chunks.
    db.flush()
    return document


def _clear_chunks(db: Session, policy_document_id: UUID) -> None:
    """Delete every chunk for a document. Re-processing replaces, never appends."""
    db.execute(
        delete(PolicyChunk).where(PolicyChunk.policy_document_id == policy_document_id)
    )
    db.flush()


def _embed_and_store(
    db: Session,
    document: PolicyDocument,
    document_text: DocumentText,
) -> int:
    """Chunk, embed and persist. Returns the number of chunks written."""
    drafts = chunk_document(document_text)

    # Empty policy (scanned PDF, no extractable text) is a valid input. No
    # chunks means no retrievable content, which is honest. The document still
    # exists, and the UI shows zero chunks.
    if not drafts:
        return 0

    provider = get_embedding_provider()
    embeddings = provider.embed([draft.content for draft in drafts])

    # A provider that returns the wrong number of vectors would silently
    # misalign embeddings against chunks, so check rather than trust.
    if len(embeddings) != len(drafts):
        raise PolicyProcessingError(
            f"embedding provider returned {len(embeddings)} vectors "
            f"for {len(drafts)} chunks"
        )

    for draft, embedding in zip(drafts, embeddings, strict=True):
        db.add(
            PolicyChunk(
                policy_document_id=document.id,
                position=draft.position,
                page_number=draft.page_number,
                content=draft.content,
                token_count=draft.token_count,
                embedding=embedding,
            )
        )

    return len(drafts)


def process_policy(
    db: Session,
    *,
    policy_document_id: UUID,
    storage: StorageBackend,
) -> PolicyDocument:
    """Extract, chunk, embed and persist chunks for a stored policy.

    Commits at the end. Re-processing replaces the document's existing chunks
    rather than appending, so calling it twice leaves one set of chunks, not
    two.

    Returns:
        The updated PolicyDocument (refreshed after commit).

    Raises:
        LookupError: No such policy document.
        PolicyProcessingError: Text extraction failed.
    """
    document = db.get(PolicyDocument, policy_document_id)
    if document is None:
        raise LookupError(f"policy document not found: {policy_document_id}")

    data = storage.read(document.storage_path)

    try:
        document_text = PyMuPDFProcessor().process(data)
    except ExtractionError as exc:
        raise PolicyProcessingError("extraction failed") from exc

    # Replace first, even on the empty path: a policy whose text disappeared
    # (re-uploaded as a scan) must not keep stale chunks.
    _clear_chunks(db, document.id)

    _embed_and_store(db, document, document_text)

    db.commit()
    db.refresh(document)
    return document


def get_policy(db: Session, policy_document_id: UUID) -> PolicyDocument | None:
    """Return the policy with ``policy_document_id``, or None if absent."""
    return db.get(PolicyDocument, policy_document_id)