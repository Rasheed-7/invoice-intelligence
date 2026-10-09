"""Policy document upload, processing and listing endpoints.

Processing is a separate, idempotent endpoint rather than something that runs
inside the upload. Extraction, chunking and embedding is the expensive half of
ingestion, and separating it means a client can retry it independently, see
progress per document, and re-run it after a provider change.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.policy import PolicyChunk, PolicyDocument
from app.schemas.policy import (
    PolicyDocumentResponse,
    PolicyListResponse,
    SearchRequest,
)
from app.services.invoices import get_invoice
from app.services.policies import (
    DuplicatePolicy,
    InvalidPolicyExtension,
    PolicyProcessingError,
    PolicyTooLarge,
    UnsupportedPolicyContentType,
    create_policy,
    get_policy,
    process_policy,
)
from app.services.policy_retrieval import (
    RetrievalResult,
    build_invoice_query,
    search_policy,
)
from app.services.storage import get_storage

router = APIRouter(prefix="/policies", tags=["policies"])

#: Subquery counting chunks per document. Used so the list endpoint computes
#: every chunk_count in the same round trip instead of one query per row.
_CHUNK_COUNT_SUBQUERY = (
    select(func.count(PolicyChunk.id))
    .where(PolicyChunk.policy_document_id == PolicyDocument.id)
    .correlate(PolicyDocument)
    .scalar_subquery()
)


def _chunk_count(db: Session, document: PolicyDocument) -> int:
    """Count a single document's chunks. Only for the by-id responses."""
    return (
        db.execute(
            select(func.count(PolicyChunk.id)).where(
                PolicyChunk.policy_document_id == document.id
            )
        ).scalar()
        or 0
    )


def _to_response(db: Session, document: PolicyDocument) -> PolicyDocumentResponse:
    return PolicyDocumentResponse(
        id=document.id,
        title=document.title,
        source_filename=document.source_filename,
        sha256=document.sha256,
        is_active=document.is_active,
        uploaded_at=document.uploaded_at,
        updated_at=document.updated_at,
        chunk_count=_chunk_count(db, document),
    )


@router.post(
    "",
    response_model=PolicyDocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a policy document",
)
def upload_policy(
    file: Annotated[UploadFile, File()],
    title: Annotated[str, Form()],
    db: Annotated[Session, Depends(get_db)],
) -> PolicyDocumentResponse:
    """Store a policy PDF and return the document with ``chunk_count`` 0.

    Extraction and embedding are *not* run here - call
    ``POST /policies/{id}/process`` for that. The document exists and is
    retrievable before any content has been indexed.
    """

    if not title.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="title is required",
        )

    data = file.file.read()

    try:
        document = create_policy(
            db,
            title=title.strip(),
            filename=file.filename or "",
            content_type=file.content_type or "application/octet-stream",
            data=data,
            storage=get_storage(),
        )
    except UnsupportedPolicyContentType as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="policy must be a PDF",
        ) from exc
    except InvalidPolicyExtension as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="extension must be .pdf",
        ) from exc
    except PolicyTooLarge as exc:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="file exceeds maximum size",
        ) from exc
    except DuplicatePolicy as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            # existing_id lets the client deep-link to the document that
            # already holds this content instead of leaving the user stuck.
            detail={
                "detail": "policy with this content already exists",
                "existing_id": str(exc.existing_id),
            },
        ) from exc

    db.commit()
    db.refresh(document)
    return _to_response(db, document)


@router.get(
    "",
    response_model=PolicyListResponse,
    summary="List policy documents with pagination",
)
def list_policies(
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PolicyListResponse:
    """Return policies newest-first, each with its chunk count.

    Includes soft-deleted policies so a UI can show what exists but is inactive;
    filter on ``is_active`` client-side or add a query flag later.
    """

    total = db.execute(select(func.count(PolicyDocument.id))).scalar() or 0

    rows = db.execute(
        select(PolicyDocument, _CHUNK_COUNT_SUBQUERY.label("chunk_count"))
        .order_by(PolicyDocument.uploaded_at.desc(), PolicyDocument.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()

    items = [
        PolicyDocumentResponse(
            id=document.id,
            title=document.title,
            source_filename=document.source_filename,
            sha256=document.sha256,
            is_active=document.is_active,
            uploaded_at=document.uploaded_at,
            updated_at=document.updated_at,
            chunk_count=chunk_count,
        )
        for document, chunk_count in rows
    ]
    return PolicyListResponse(items=items, total=total, limit=limit, offset=offset)


# --- search -----------------------------------------------------------------
# Declared before the "/{policy_id}" routes so the literal paths are matched
# first and are never captured as an id.


@router.post(
    "/search",
    response_model=RetrievalResult,
    summary="Search embedded policy chunks",
)
def search_policies(
    request: SearchRequest,
    db: Annotated[Session, Depends(get_db)],
) -> RetrievalResult:
    """Rank embedded policy chunks against a free-text query.

    ``active_only`` is not exposed: retrieval through the API always filters to
    active policies, so a soft-deleted policy cannot leak into results by
    omission. Service-level callers can opt out explicitly.
    """
    if not request.query.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="query is required",
        )

    return search_policy(
        db,
        query=request.query,
        top_k=request.top_k,
        min_similarity=request.min_similarity,
        active_only=True,
    )


@router.post(
    "/search/for-invoice/{invoice_id}",
    response_model=RetrievalResult,
    summary="Search policy chunks using an invoice's own fields",
)
def search_for_invoice(
    invoice_id: uuid.UUID,
    db: Annotated[Session, Depends(get_db)],
) -> RetrievalResult:
    """Build a structured query from the invoice and search with it.

    The constructed query is echoed back in the result so the caller can see
    what was actually searched - important when the ranking looks wrong.
    """
    invoice = get_invoice(db, invoice_id)
    if invoice is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="invoice not found",
        )

    return search_policy(db, query=build_invoice_query(invoice), active_only=True)


@router.get("/{policy_id}", response_model=PolicyDocumentResponse)
def read_policy(
    policy_id: uuid.UUID,
    db: Annotated[Session, Depends(get_db)],
) -> PolicyDocumentResponse:
    """Return one policy document, or 404 if it does not exist."""
    document = get_policy(db, policy_id)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="policy not found",
        )
    return _to_response(db, document)


@router.post("/{policy_id}/process", response_model=PolicyDocumentResponse)
def process_policy_endpoint(
    policy_id: uuid.UUID,
    db: Annotated[Session, Depends(get_db)],
) -> PolicyDocumentResponse:
    """Extract, chunk, embed and persist chunks for a stored policy.

    Idempotent: re-running replaces the document's chunks rather than appending,
    so chunk_count stays stable across repeats. A PDF with no extractable text
    (a scan) succeeds with ``chunk_count`` 0.
    """
    try:
        document = process_policy(
            db, policy_document_id=policy_id, storage=get_storage()
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="policy not found",
        ) from exc
    except PolicyProcessingError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="policy could not be processed",
        ) from exc

    return _to_response(db, document)


@router.delete("/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
def deactivate_policy(
    policy_id: uuid.UUID,
    db: Annotated[Session, Depends(get_db)],
) -> None:
    """Soft-delete a policy by clearing ``is_active``.

    Deliberately not a hard delete. Embeddings are expensive to regenerate, and
    retrieval filters on active policies, so deactivating keeps the indexed
    content available for a future re-activation while removing it from search
    results immediately. Chunks and their vectors stay in place.
    """
    document = get_policy(db, policy_id)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="policy not found",
        )

    document.is_active = False
    db.commit()