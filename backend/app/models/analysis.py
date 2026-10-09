"""Result of a single analysis run against an invoice.

One row per (invoice, analysis_type) pair. ``score`` is normalized to 0..1 with
four decimals; ``details`` holds the structured evidence for the outcome.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import AnalysisStatus, AnalysisType, pg_enum

if TYPE_CHECKING:
    from app.models.invoice import Invoice


class AnalysisResult(Base):
    __tablename__ = "analysis_results"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    analysis_type: Mapped[AnalysisType] = mapped_column(
        pg_enum(AnalysisType, "analysis_type"),
        nullable=False,
        index=True,
    )
    status: Mapped[AnalysisStatus] = mapped_column(
        pg_enum(AnalysisStatus, "analysis_status"),
        nullable=False,
        index=True,
    )

    # Normalized score in [0, 1]; nullable for analyses that don't score.
    score: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )

    invoice: Mapped["Invoice"] = relationship(back_populates="analysis_results")

    __table_args__ = (
        Index("ix_analysis_results_invoice_type", "invoice_id", "analysis_type"),
    )
