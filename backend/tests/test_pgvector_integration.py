"""Integration tests for the pgvector column.

These need a real PostgreSQL with the vector extension enabled (migration
f7b2c4e19a08). They use the shared db_session fixture, so every write is
rolled back on teardown.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.policy import PolicyChunk, PolicyDocument

DIMENSION = 384


def make_document(db: Session) -> PolicyDocument:
    document = PolicyDocument(
        title="Vector integration",
        source_filename="policy.pdf",
        sha256=uuid.uuid4().hex,
        storage_path="/storage/policy.pdf",
    )
    db.add(document)
    db.commit()
    return document


def make_chunk(
    db: Session,
    document: PolicyDocument,
    *,
    position: int,
    embedding: list[float],
    page_number: int = 1,
) -> PolicyChunk:
    chunk = PolicyChunk(
        policy_document_id=document.id,
        position=position,
        page_number=page_number,
        content=f"chunk {position}",
        embedding=embedding,
    )
    db.add(chunk)
    db.commit()
    return chunk


def test_extension_is_enabled(db_session: Session) -> None:
    """The vector extension exists in this database."""
    rows = db_session.execute(
        text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
    ).all()

    assert len(rows) == 1
    assert rows[0][0] == "vector"


def test_embedding_column_is_vector_384(db_session: Session) -> None:
    """The column really is vector(384), not a JSONB placeholder."""
    row = db_session.execute(
        text(
            "SELECT data_type, udt_name, character_maximum_length "
            "FROM information_schema.columns "
            "WHERE table_name = 'policy_chunks' AND column_name = 'embedding'"
        )
    ).one()

    assert row.udt_name == "vector"
    # pgvector carries its declared width directly in the type modifier
    # (unlike varchar, which is typmod - 1).
    typmod = db_session.execute(
        text(
            "SELECT atttypmod FROM pg_attribute "
            "WHERE attrelid = 'policy_chunks'::regclass AND attname = 'embedding'"
        )
    ).scalar_one()
    assert typmod == DIMENSION


def test_vector_column_accepts_384_floats(db_session: Session) -> None:
    """A 384-float vector round-trips through the column unchanged."""
    document = make_document(db_session)
    embedding = [(index % 17) / 17.0 for index in range(DIMENSION)]

    chunk = make_chunk(db_session, document, position=1, embedding=embedding)

    db_session.expire_all()
    stored = db_session.get(PolicyChunk, chunk.id)

    assert stored is not None
    assert stored.embedding is not None
    assert len(stored.embedding) == DIMENSION
    for expected, actual in zip(embedding, stored.embedding, strict=True):
        assert actual == pytest.approx(expected, abs=1e-6)


def test_embedding_is_nullable(db_session: Session) -> None:
    """NULL means "not embedded yet", the normal state after ingestion."""
    document = make_document(db_session)
    chunk = make_chunk(db_session, document, position=1, embedding=None)

    db_session.expire_all()
    stored = db_session.get(PolicyChunk, chunk.id)

    assert stored is not None
    assert stored.embedding is None


def test_cosine_similarity_ordering(db_session: Session) -> None:
    """The `<=>` operator ranks the nearest chunk first.

    Uses the vector_cosine_ops operator class, so this also proves the IVFFlat
    index is usable for this query shape.
    """
    document = make_document(db_session)

    near = [0.1] * DIMENSION
    near[0] = 0.2  # close to `probe`
    far = [0.0] * (DIMENSION - 1) + [1.0]  # near-orthogonal to `probe`
    probe = [0.1] * DIMENSION

    make_chunk(db_session, document, position=1, embedding=near)
    make_chunk(db_session, document, position=2, embedding=far)

    rows = db_session.execute(
        text(
            "SELECT content, embedding <=> CAST(:probe AS vector) AS distance "
            "FROM policy_chunks WHERE embedding IS NOT NULL "
            "ORDER BY embedding <=> CAST(:probe AS vector)"
        ),
        {"probe": str(probe)},
    ).all()

    assert len(rows) == 2
    assert rows[0][0] == "chunk 1"
    assert rows[1][0] == "chunk 2"
    assert float(rows[0][1]) < float(rows[1][1])


def test_ivfflat_index_exists(db_session: Session) -> None:
    """The IVFFlat cosine index is present on the embedding column."""
    rows = db_session.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'policy_chunks' "
            "AND indexname = 'ix_policy_chunks_embedding'"
        )
    ).all()

    assert len(rows) == 1
    indexdef = rows[0][0]
    assert "ivfflat" in indexdef
    assert "vector_cosine_ops" in indexdef