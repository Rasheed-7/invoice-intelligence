"""Policy document models.

A policy document is split into ordered chunks that the retrieval layer
embeds. ``embedding`` is a real pgvector column (``vector(384)``), enabled by
migration f7b2c4e19a08, with an IVFFlat cosine index for approximate
nearest-neighbour search. The width is fixed: changing it requires a new
migration and a re-embed of every chunk.
"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class PolicyDocument(Base):
    __tablename__ = "policy_documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    source_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, index=True
    )

    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.clock_timestamp(),
        onupdate=func.clock_timestamp(),
    )

    chunks: Mapped[list["PolicyChunk"]] = relationship(
        back_populates="policy_document",
        cascade="all, delete-orphan",
        order_by="PolicyChunk.position",
    )


class PolicyChunk(Base):
    __tablename__ = "policy_chunks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    policy_document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("policy_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    position: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 1-indexed source page. Chunks never span pages, so this is exact and
    # lets a retrieval hit be cited back to a page.
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # Real pgvector column, not a JSONB placeholder. Values are 384-dimension
    # float vectors written by an EmbeddingProvider; NULL means "not embedded
    # yet", which is the normal state for a freshly ingested document.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(384), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )

    policy_document: Mapped[PolicyDocument] = relationship(back_populates="chunks")

    __table_args__ = (
        Index(
            "ix_policy_chunks_document_position",
            "policy_document_id",
            "position",
            unique=True,
        ),
    )
