"""Document upload and retrieval endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.document import DocumentResponse
from app.services.documents import (
    UnsupportedContentType,
    UploadTooLarge,
    create_document,
    get_document,
)
from app.services.storage import get_storage

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post(
    "",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a document",
)
async def upload_document(
    file: Annotated[UploadFile, File()],
    db: Annotated[Session, Depends(get_db)],
) -> DocumentResponse:
    """Store an uploaded file and return its metadata.

    The file is content-addressed by SHA-256, so re-uploading identical bytes
    reuses the already-stored object.
    """

    filename = (file.filename or "").strip()
    if not filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="filename is required",
        )

    # MVP: the whole payload is read into memory. Streaming directly to storage
    # in chunks is a future improvement for very large files.
    data = await file.read()

    try:
        document = create_document(
            db,
            filename=filename,
            content_type=file.content_type or "",
            data=data,
            storage=get_storage(),
        )
    except UploadTooLarge as exc:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="file exceeds maximum size",
        ) from exc
    except UnsupportedContentType as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="unsupported content type",
        ) from exc

    db.add(document)
    db.commit()
    db.refresh(document)

    return DocumentResponse.model_validate(document)


@router.get(
    "/{document_id}",
    response_model=DocumentResponse,
    summary="Fetch a document by id",
)
def read_document(
    document_id: UUID,
    db: Annotated[Session, Depends(get_db)],
) -> DocumentResponse:
    """Return a single document's metadata, or 404 if it does not exist."""

    document = get_document(db, document_id)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="document not found",
        )
    return DocumentResponse.model_validate(document)