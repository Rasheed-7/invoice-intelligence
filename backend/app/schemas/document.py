"""Schemas for the document upload API."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

__all__ = ["DocumentResponse"]


class DocumentResponse(BaseModel):
    """Public representation of a stored document.

    There is deliberately no ``DocumentCreate``: documents are created from a
    multipart upload, never from a JSON request body.
    """

    # from_attributes allows building this model straight from a Document ORM
    # instance (e.g. DocumentResponse.model_validate(document)).
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    processing_status: str
    uploaded_at: datetime