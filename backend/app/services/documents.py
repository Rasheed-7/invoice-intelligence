"""Document upload service.

Deliberately free of FastAPI imports: the upload rules (size ceiling,
content-type allow-list, content-addressed storage) are business logic and
must be testable without HTTP.
"""

import hashlib
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.document import Document
from app.models.enums import DocumentProcessingStatus
from app.services.storage import StorageBackend

__all__ = [
    "UnsupportedContentType",
    "UploadTooLarge",
    "InvalidExtension",
    "create_document",
    "get_document",
]


class UploadTooLarge(Exception):
    """Raised when an upload exceeds the configured maximum size."""


class UnsupportedContentType(Exception):
    """Raised when an upload's content type is not on the allow-list."""


class InvalidExtension(Exception):
    """Raised when an upload's file extension is not on the allow-list."""


def _storage_key(sha256: str) -> str:
    """Return the content-addressed storage key for a hash.

    Sharding the hash two levels deep (``xx/yy/<sha256>.bin``) keeps any single
    directory from growing unbounded.
    """

    return f"{sha256[:2]}/{sha256[2:4]}/{sha256}.bin"


def create_document(
    db: Session,
    *,
    filename: str,
    content_type: str,
    data: bytes,
    storage: StorageBackend,
) -> Document:
    """Validate an upload, store its bytes, and stage the ``Document`` row.

    The row is added to the session but *not* committed - the caller owns the
    transaction. Raises :class:`UploadTooLarge` or
    :class:`UnsupportedContentType` before anything is persisted.
    """

    settings = get_settings()

    if len(data) > settings.max_upload_bytes:
        raise UploadTooLarge(
            f"{len(data)} bytes exceeds limit of {settings.max_upload_bytes}"
        )

    if content_type not in settings.allowed_content_types_list:
        raise UnsupportedContentType(f"content type not allowed: {content_type!r}")

    # Extension validation is belt-and-braces with the MIME check, not a replacement.
    ext = Path(filename).suffix.lower()
    if ext == "" or ext not in settings.allowed_extensions_list:
        raise InvalidExtension(f"extension not allowed: {ext!r}")

    sha256 = hashlib.sha256(data).hexdigest()

    # Content-addressed storage makes writes idempotent: identical bytes always
    # map to the same key, so the backend will not rewrite an existing file.
    path = storage.save(_storage_key(sha256), data)

    document = Document(
        filename=filename,
        content_type=content_type,
        size_bytes=len(data),
        sha256=sha256,
        storage_path=path,
        processing_status=DocumentProcessingStatus.UPLOADED,
    )
    db.add(document)
    # Flush so the UUID PK is populated before the caller links related rows.
    db.flush()
    return document


def get_document(db: Session, document_id: UUID) -> Document | None:
    """Return the document with ``document_id``, or ``None`` if it is absent."""

    return db.get(Document, document_id)