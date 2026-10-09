"""Vector search over embedded policy chunks.

Uses cosine distance (``<=>``) against the IVFFlat index. Every vector written
by the configured provider is unit-normalized, so cosine distance and inner
product rank identically - cosine is used because it stays correct if a future
provider emits unnormalized vectors.

What callers get back is *similarity* (``1 - distance``), not raw distance.
Similarity is the quantity people reason about: 1.0 is identical, 0.0 is
orthogonal. Returning distance would push that subtraction into every caller.
"""

import uuid

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.invoice import Invoice
from app.services.embeddings.factory import get_embedding_provider

__all__ = [
    "RetrievedChunk",
    "RetrievalResult",
    "build_invoice_query",
    "search_policy",
]

#: Retrieval should never be able to return thousands of chunks and blow up the
#: downstream context that consumes them. Clamped rather than rejected so a
#: caller asking for 500 still gets a useful page of results.
MIN_TOP_K = 1
MAX_TOP_K = 20
DEFAULT_TOP_K = 5

#: Fallback when an invoice has no fields worth searching on.
_FALLBACK_QUERY = "invoice policy approval"

#: IVFFlat probe count used per query.
#:
#: IVFFlat is approximate: it probes `ivfflat.probes` of the index's `lists`
#: clusters and only ranks rows inside those clusters. The index is built with
#: lists=100, but the corpus is tiny (a handful of chunks), so the default
#: probes=1 lands on a cluster that does not contain the nearest row and the
#: query returns *zero* results. That is indistinguishable from "no matching
#: policy" and silently returns wrong answers.
#:
#: Probing all clusters makes recall exact. At MVP scale that costs nothing
#: measurable, and correctness beats a speedup nobody can observe. Re-tune this
#: together with the index's `lists` once the corpus is large enough for the
#: index to matter.
IVFFLAT_PROBES = 100


class RetrievedChunk(BaseModel):
    """One chunk matched by the search, with the evidence that matched."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: uuid.UUID
    policy_document_id: uuid.UUID
    policy_title: str
    page_number: int
    position: int
    text: str
    #: 1 - cosine_distance. 1.0 is identical, 0.0 is orthogonal.
    similarity: float


class RetrievalResult(BaseModel):
    """A ranked set of chunks for one query."""

    model_config = ConfigDict(extra="forbid")

    query: str
    top_k: int
    chunks: list[RetrievedChunk]
    #: Which embedding model produced the ranking. Callers should surface this:
    #: results from different providers are not comparable.
    used_provider: str


def _clamp_top_k(top_k: int) -> int:
    """Clamp ``top_k`` into the supported range."""
    return max(MIN_TOP_K, min(MAX_TOP_K, top_k))


def _used_provider_name() -> str:
    """Report the configured provider name from settings, not the class name."""
    from app.core.config import get_settings

    return get_settings().embedding_provider.lower()


# Cosine distance against the IVFFlat index. `similarity` is computed in SQL so
# it comes back on the same row; the Python-side filter below then reuses it
# instead of re-deriving anything.
_SEARCH_SQL = text(
    """
    SELECT
        pc.id                AS chunk_id,
        pc.policy_document_id AS policy_document_id,
        pd.title             AS policy_title,
        pc.page_number       AS page_number,
        pc.position          AS position,
        pc.content           AS text,
        1 - (pc.embedding <=> CAST(:qvec AS vector)) AS similarity
    FROM policy_chunks pc
    JOIN policy_documents pd ON pd.id = pc.policy_document_id
    WHERE pc.embedding IS NOT NULL
      AND (:active_only = false OR pd.is_active = true)
    ORDER BY pc.embedding <=> CAST(:qvec AS vector)
    LIMIT :top_k
    """
)


def search_policy(
    db: Session,
    *,
    query: str,
    top_k: int = DEFAULT_TOP_K,
    min_similarity: float | None = None,
    active_only: bool = True,
) -> RetrievalResult:
    """Return the chunks most similar to ``query``.

    Args:
        db: Database session. Caller owns the transaction.
        query: Natural language or structured query text.
        top_k: Maximum chunks to return, clamped to [1, 20].
        min_similarity: Optional floor. Chunks scoring below it are dropped.
        active_only: Exclude soft-deleted policies. Defaults to True.

    Returns:
        A :class:`RetrievalResult`. Empty for an empty query, a corpus with no
        embedded chunks, or a ``min_similarity`` floor nothing clears.
    """
    provider_name = _used_provider_name()
    clamped_top_k = _clamp_top_k(top_k)
    stripped = query.strip()

    # An empty query has no meaningful embedding. Returning zero chunks is
    # honest; a caller should treat an empty result as "no evidence," not as a
    # system error.
    if not stripped:
        return RetrievalResult(
            query="", top_k=clamped_top_k, chunks=[], used_provider=provider_name
        )

    embedding = get_embedding_provider().embed_one(stripped)

    # Raise IVFFlat recall before querying. SET LOCAL is scoped to the
    # caller's transaction, so this does not leak into pooled connections.
    db.execute(text(f"SET LOCAL ivfflat.probes = {IVFFLAT_PROBES}"))

    rows = db.execute(
        _SEARCH_SQL,
        {
            "qvec": str(embedding),
            "top_k": clamped_top_k,
            "active_only": active_only,
        },
    ).all()

    chunks = [
        RetrievedChunk(
            chunk_id=row.chunk_id,
            policy_document_id=row.policy_document_id,
            policy_title=row.policy_title,
            page_number=row.page_number,
            position=row.position,
            text=row.text,
            similarity=float(row.similarity),
        )
        for row in rows
    ]

    # Filtered in Python rather than SQL: the LIMIT already bounds the row
    # count, so filtering here costs nothing extra. Pushing it into a WHERE
    # clause would change which rows the LIMIT selects and would need a
    # nested subquery to preserve "top-k that also clear the floor".
    if min_similarity is not None:
        chunks = [c for c in chunks if c.similarity >= min_similarity]

    return RetrievalResult(
        query=stripped,
        top_k=clamped_top_k,
        chunks=chunks,
        used_provider=provider_name,
    )


def build_invoice_query(invoice: Invoice) -> str:
    """Build a deterministic retrieval query from an invoice's fields.

    Retrieval uses structured field-derived queries so hash-based embeddings can
    find matching policy text. A real semantic provider could accept free-text;
    this query shape is deliberately conservative so the MVP demo retrieves
    something sensible.

    Deliberately not free-text: the hash provider matches on shared tokens, so
    "108.50 USD vendor Acme Corporation approval policy" retrieves on the terms
    a policy actually uses (amount, currency, vendor, approval) rather than on
    prose that a bag-of-tokens provider cannot interpret.
    """
    parts: list[str] = ["approval", "policy", "invoice"]

    if invoice.total_amount is not None:
        parts.append(str(invoice.total_amount))
    if invoice.currency is not None:
        parts.append(invoice.currency)
    if invoice.vendor_name is not None:
        parts.append(invoice.vendor_name)

    if len(parts) == 3:
        # The invoice carried no searchable field at all (not yet extracted).
        # Fall back to a query that still names the concept, so the caller gets
        # generic policy text rather than a guaranteed-empty result.
        return _FALLBACK_QUERY

    return " ".join(parts)