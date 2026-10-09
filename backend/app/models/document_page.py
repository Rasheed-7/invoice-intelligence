"""Document page model.

A ``DocumentPage`` holds the text of a single page of an uploaded document.
Pages are the unit of extraction: ``Document.extracted_text`` stores the full
concatenated text while this table keeps the per-page breakdown.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DocumentPage(Base):
    __tablename__ = "document_pages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # 1-indexed page position within the document.
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)

    text: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Reserved for future layout/bbox data. Named ``page_metadata`` because
    # ``metadata`` is a reserved attribute on SQLAlchemy declarative classes.
    page_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )

    __table_args__ = (
        Index(
            "uq_document_pages_document_page",
            "document_id",
            "page_number",
            unique=True,
        ),
    )