"""Policy document API schemas.

``chunk_count`` is a computed aggregate rather than a stored column: it is
derived from the ``policy_chunks`` relationship at response-build time so it can
never drift from the rows.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PolicyDocumentResponse(BaseModel):
    """A stored policy document, with its chunk count."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    title: str
    source_filename: str
    sha256: str
    is_active: bool
    uploaded_at: datetime
    updated_at: datetime
    chunk_count: int = 0


class PolicyListResponse(BaseModel):
    """Paginated policy list."""

    model_config = ConfigDict(extra="forbid")

    items: list[PolicyDocumentResponse]
    total: int
    limit: int
    offset: int


class SearchRequest(BaseModel):
    """Body for POST /policies/search.

    ``top_k`` is bounded here rather than clamped in the service so an
    out-of-range value is a 422 at the edge instead of being silently
    rewritten. The service clamps too, for callers that bypass HTTP.
    """

    model_config = ConfigDict(extra="forbid")

    query: str
    top_k: int = Field(default=5, ge=1, le=20)
    min_similarity: float | None = None