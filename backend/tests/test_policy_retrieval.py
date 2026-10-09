"""Tests for policy vector search.

Chunks are inserted directly with controlled embeddings rather than being
produced by the ingestion pipeline, so each test can assert on exact
similarity bands instead of whatever the chunker happened to emit.
"""

import uuid
from decimal import Decimal
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.policy import PolicyChunk, PolicyDocument
from app.services.embeddings.factory import get_embedding_provider
from app.services.policy_retrieval import build_invoice_query, search_policy

POLICIES_URL = "/api/v1/policies"
UNKNOWN_ID = "00000000-0000-0000-0000-000000000000"

PROVIDER = get_embedding_provider()


# --- fixtures / helpers -----------------------------------------------------


def make_policy(db: Session, *, title: str = "Policy", is_active: bool = True):
    """Insert a policy document and return it."""
    document = PolicyDocument(
        title=title,
        source_filename=f"{title}.pdf",
        sha256=uuid.uuid4().hex,
        storage_path=f"/storage/{title}.pdf",
        is_active=is_active,
    )
    db.add(document)
    db.commit()
    return document


def make_chunk(db: Session, document: PolicyDocument, *, content: str, position: int = 1):
    """Insert one chunk, embedding its content with the hash provider."""
    chunk = PolicyChunk(
        policy_document_id=document.id,
        position=position,
        page_number=1,
        content=content,
        token_count=len(content.split()),
        embedding=PROVIDER.embed_one(content),
    )
    db.add(chunk)
    db.commit()
    return chunk


def build_pdf(pages: list[str]) -> bytes:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        y = 100.0
        for line in text.split("\n"):
            if line.strip():
                page.insert_text((72, y), line)
            y += 20
    data = doc.tobytes()
    doc.close()
    return data


# The corpus used by several tests. Similarity to QUERY, measured:
#   QUERY itself                    1.0000
#   same words, reordered           0.9112
#   partial overlap                 0.6886
#   unrelated                       0.1910
QUERY = "manager approval invoice 5000 USD"
EXACT = QUERY
REORDERED = "invoice approval manager 5000 USD"
PARTIAL = "approval required over 5000 USD"
UNRELATED = "office seating chart allocation"


# --- empty / clamping -------------------------------------------------------


def test_empty_query_returns_no_chunks(db_session: Session) -> None:
    """Blank and whitespace-only queries return zero chunks, not an error."""
    for query in ("", " ", "   ", "\t\n"):
        result = search_policy(db_session, query=query)
        assert result.query == ""
        assert result.chunks == []


def test_top_k_is_clamped(db_session: Session) -> None:
    """top_k is clamped into [1, 20] rather than honoured or rejected."""
    over = search_policy(db_session, query=QUERY, top_k=1000)
    assert over.top_k == 20
    assert len(over.chunks) <= 20

    under = search_policy(db_session, query=QUERY, top_k=0)
    assert under.top_k == 1


def test_top_k_caps_returned_rows(db_session: Session) -> None:
    """A clamped-but-valid top_k still limits the rows returned."""
    document = make_policy(db_session)
    for position in range(1, 6):
        make_chunk(
            db_session,
            document,
            content=f"{QUERY} variant {position}",
            position=position,
        )

    assert len(search_policy(db_session, query=QUERY, top_k=2).chunks) == 2
    assert len(search_policy(db_session, query=QUERY, top_k=5).chunks) == 5


def test_search_empty_corpus_returns_empty(db_session: Session) -> None:
    """No policies at all is an empty result, not a failure."""
    result = search_policy(db_session, query=QUERY)

    assert result.chunks == []
    assert result.query == QUERY


# --- ranking ----------------------------------------------------------------


def test_search_returns_higher_similarity_first(db_session: Session) -> None:
    """Results are ordered by descending similarity, matching chunk first."""
    document = make_policy(db_session)
    make_chunk(db_session, document, content=UNRELATED, position=1)
    make_chunk(db_session, document, content=PARTIAL, position=2)
    make_chunk(db_session, document, content=EXACT, position=3)

    result = search_policy(db_session, query=QUERY, top_k=5)

    assert len(result.chunks) == 3
    similarities = [c.similarity for c in result.chunks]
    assert similarities == sorted(similarities, reverse=True)
    # The chunk whose content is the query ranks first and outranks the rest.
    assert result.chunks[0].text == EXACT
    assert result.chunks[0].similarity >= 0.99
    assert result.chunks[0].similarity > result.chunks[-1].similarity
    assert [c.position for c in result.chunks] == [3, 2, 1]


def test_search_similarity_is_one_minus_distance(db_session: Session) -> None:
    """An identical text scores ~1.0; similarity is not raw distance."""
    document = make_policy(db_session)
    make_chunk(db_session, document, content=EXACT)

    result = search_policy(db_session, query=QUERY)

    assert len(result.chunks) == 1
    # Identical vectors -> cosine distance 0 -> similarity 1.0. If this ever
    # returned raw distance it would be 0.0 and the assertion would fail.
    assert result.chunks[0].similarity >= 0.99
    assert result.chunks[0].similarity <= 1.0


# --- filtering --------------------------------------------------------------


def test_search_filters_null_embeddings(db_session: Session) -> None:
    """A chunk with no embedding never appears in results."""
    document = make_policy(db_session)
    make_chunk(db_session, document, content=EXACT, position=1)
    db_session.add(
        PolicyChunk(
            policy_document_id=document.id,
            position=2,
            page_number=1,
            content="never embedded",
            token_count=2,
            embedding=None,
        )
    )
    db_session.commit()

    result = search_policy(db_session, query=QUERY)

    assert len(result.chunks) == 1
    assert result.chunks[0].text == EXACT


def test_search_filters_inactive_policies_by_default(db_session: Session) -> None:
    """Soft-deleted policies are excluded unless a caller opts in."""
    active = make_policy(db_session, title="Active")
    inactive = make_policy(db_session, title="Retired", is_active=False)
    make_chunk(db_session, active, content=EXACT, position=1)
    make_chunk(db_session, inactive, content=EXACT, position=1)

    default_result = search_policy(db_session, query=QUERY)
    assert len(default_result.chunks) == 1
    assert default_result.chunks[0].policy_title == "Active"

    # The service supports reaching inactive policies for internal use.
    opted_in = search_policy(db_session, query=QUERY, active_only=False)
    assert len(opted_in.chunks) == 2
    assert {c.policy_title for c in opted_in.chunks} == {"Active", "Retired"}


def test_search_respects_min_similarity(db_session: Session) -> None:
    """A floor drops the weaker matches and keeps the stronger ones."""
    document = make_policy(db_session)
    make_chunk(db_session, document, content=EXACT, position=1)
    make_chunk(db_session, document, content=REORDERED, position=2)
    make_chunk(db_session, document, content=UNRELATED, position=3)

    everything = search_policy(db_session, query=QUERY)
    assert len(everything.chunks) == 3

    # 0.9 keeps exact (1.00) and reordered (0.91), drops unrelated (0.19).
    filtered = search_policy(db_session, query=QUERY, min_similarity=0.9)
    assert {c.text for c in filtered.chunks} == {EXACT, REORDERED}
    assert all(c.similarity >= 0.9 for c in filtered.chunks)

    # A floor above the maximum possible similarity returns nothing rather
    # than raising. (0.999999 would still keep EXACT, which scores exactly 1.0.)
    assert search_policy(db_session, query=QUERY, min_similarity=1.5).chunks == []


# --- provider metadata ------------------------------------------------------


def test_retrieval_result_reports_provider_name(db_session: Session) -> None:
    """Results name the embedding model that produced the ranking.

    Rankings from different providers are not comparable, so a caller has to be
    able to tell which model was used.
    """
    result = search_policy(db_session, query=QUERY)

    assert result.used_provider == "hash"
    assert isinstance(result.used_provider, str)


# --- invoice query construction --------------------------------------------


def test_build_invoice_query_includes_amount_and_currency() -> None:
    """Amount and currency both appear when present."""
    invoice = _invoice(total_amount=Decimal("100.00"), currency="USD")

    query = build_invoice_query(invoice)

    assert "100.00" in query
    assert "USD" in query


def test_build_invoice_query_includes_vendor() -> None:
    """Vendor name appears when present."""
    invoice = _invoice(vendor_name="Acme Corporation")

    assert "Acme Corporation" in build_invoice_query(invoice)


def test_build_invoice_query_handles_missing_fields() -> None:
    """All-null fields fall back to a query that still names the concept."""
    invoice = _invoice()

    assert build_invoice_query(invoice) == "invoice policy approval"


def test_build_invoice_query_omits_absent_fields() -> None:
    """Missing fields are skipped rather than rendered as None."""
    invoice = _invoice(vendor_name="Acme Corporation", total_amount=None, currency=None)

    assert build_invoice_query(invoice) == "approval policy invoice Acme Corporation"


def _invoice(**overrides):
    from app.models.invoice import Invoice

    base = {"id": uuid.uuid4(), "document_id": uuid.uuid4()}
    base.update(overrides)
    return Invoice(**base)


# --- API: search ------------------------------------------------------------


@pytest.fixture
def indexed_policy(client: TestClient, storage_root: Path) -> str:
    """A policy ingested and processed through the real endpoints."""
    upload = client.post(
        POLICIES_URL,
        files={
            "file": (
                "travel.pdf",
                build_pdf(
                    [
                        "Travel Policy\n\n"
                        "Manager approval is required for any invoice over 5000 USD.",
                        "Travel Policy continued\n\n"
                        "Invoices over 20000 USD require director approval.",
                    ]
                ),
                "application/pdf",
            )
        },
        data={"title": "Travel Policy"},
    )
    assert upload.status_code == 201
    policy_id = upload.json()["id"]
    assert client.post(f"{POLICIES_URL}/{policy_id}/process").status_code == 200
    return policy_id


def test_search_endpoint_returns_chunks(
    client: TestClient, storage_root: Path, indexed_policy: str
) -> None:
    """POST /search returns ranked chunks with the expected shape."""
    import json

    response = client.post(
        f"{POLICIES_URL}/search",
        content=json.dumps({"query": "manager approval invoice 5000 USD", "top_k": 3}),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "manager approval invoice 5000 USD"
    assert body["top_k"] == 3
    assert body["used_provider"] == "hash"
    assert body["chunks"]
    for chunk in body["chunks"]:
        assert set(chunk) == {
            "chunk_id",
            "policy_document_id",
            "policy_title",
            "page_number",
            "position",
            "text",
            "similarity",
        }
        assert chunk["policy_title"] == "Travel Policy"
        assert 0.0 < chunk["similarity"] <= 1.0
        uuid.UUID(chunk["chunk_id"])


def test_search_endpoint_rejects_empty_query(
    client: TestClient, storage_root: Path, indexed_policy: str
) -> None:
    """A whitespace-only query is a 400."""
    import json

    response = client.post(
        f"{POLICIES_URL}/search",
        content=json.dumps({"query": "   "}),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "query is required"


def test_search_endpoint_rejects_bad_top_k(
    client: TestClient, storage_root: Path, indexed_policy: str
) -> None:
    """top_k outside [1, 20] is a 422 at the schema boundary."""
    import json

    for bad in (0, 100):
        response = client.post(
            f"{POLICIES_URL}/search",
            content=json.dumps({"query": "policy", "top_k": bad}),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422, bad


def test_search_endpoint_hides_inactive_policies(
    client: TestClient, storage_root: Path, indexed_policy: str
) -> None:
    """The API always filters to active policies; there is no opt-out."""
    import json

    before = client.post(
        f"{POLICIES_URL}/search",
        content=json.dumps({"query": "approval policy"}),
        headers={"Content-Type": "application/json"},
    ).json()
    assert before["chunks"]

    assert client.delete(f"{POLICIES_URL}/{indexed_policy}").status_code == 204

    after = client.post(
        f"{POLICIES_URL}/search",
        content=json.dumps({"query": "approval policy"}),
        headers={"Content-Type": "application/json"},
    ).json()
    assert after["chunks"] == []


def test_search_for_invoice_endpoint(
    client: TestClient, storage_root: Path, indexed_policy: str
) -> None:
    """Upload + analyze an invoice, then search using its own fields."""
    invoice_id = _upload_and_analyze(client)

    response = client.post(f"{POLICIES_URL}/search/for-invoice/{invoice_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["used_provider"] == "hash"
    # The constructed query is echoed so the caller can see what was searched.
    # Amount carries the Numeric(14,4) scale the database returns.
    assert body["query"] == "approval policy invoice 108.5000 USD Acme Corporation"
    assert body["chunks"]


def test_search_for_invoice_unknown_id(client: TestClient, storage_root: Path) -> None:
    """An unknown invoice is a 404."""
    response = client.post(f"{POLICIES_URL}/search/for-invoice/{UNKNOWN_ID}")

    assert response.status_code == 404
    assert response.json()["detail"] == "invoice not found"


def _upload_and_analyze(client: TestClient) -> str:
    """Upload the acme fixture and run /analyze, returning the invoice id."""
    from app.services.llm.fixture_pdfs import build_fixture_pdf
    from app.services.llm.fixtures import FIXTURES

    upload = client.post(
        f"{POLICIES_URL.rsplit('/', 1)[0]}/invoices/upload",
        files={"file": ("acme.pdf", build_fixture_pdf(FIXTURES[0]), "application/pdf")},
    )
    assert upload.status_code == 201
    invoice_id = upload.json()["id"]
    assert client.post(f"/api/v1/invoices/{invoice_id}/analyze").status_code == 200
    return invoice_id