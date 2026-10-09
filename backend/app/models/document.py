"""Document model.

A ``Document`` represents an uploaded file (typically a PDF). It is decoupled
from invoices: the file-level metadata that used to live on ``invoices`` now
lives here. There is intentionally no foreign key back to invoices so that a
document can exist (and be reprocessed) before or without an invoice.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import DocumentProcessingStatus, pg_enum


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    # Content hash, used later for duplicate detection. Deliberately indexed
    # but NOT unique: a legitimate re-send must still be storable.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)

    processing_status: Mapped[DocumentProcessingStatus] = mapped_column(
        pg_enum(DocumentProcessingStatus, "document_processing_status"),
        nullable=False,
        default=DocumentProcessingStatus.UPLOADED,
        index=True,
    )

    # Free text extracted from the PDF (post-OCR). Nullable until extraction runs.
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.clock_timestamp(),
        onupdate=func.clock_timestamp(),
    )
