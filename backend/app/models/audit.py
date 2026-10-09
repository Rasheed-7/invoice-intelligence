import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import AuditActor, AuditEventType, pg_enum

if TYPE_CHECKING:
    from app.models.invoice import Invoice
    from app.models.user import User


class AuditEvent(Base):
    """Append-only log of everything that happened to an invoice.

    Events are never updated or deleted. If a decision changes,
    a new event is appended.
    """

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    event_type: Mapped[AuditEventType] = mapped_column(
        pg_enum(AuditEventType, "audit_event_type"),
        nullable=False,
    )
    actor: Mapped[AuditActor] = mapped_column(
        pg_enum(AuditActor, "audit_actor"),
        nullable=False,
    )

    # Who acted, when it was a human (system/llm events leave this NULL).
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Human-readable summary shown in the UI timeline.
    message: Mapped[str] = mapped_column(String(1024), nullable=False)

    # Structured payload: the "evidence" for this event.
    # Examples: {"failed_checks": ["subtotal_mismatch"], "delta": "12.34"}
    #           {"risk_score": 0.72, "reasons": [...]}
    data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )

    invoice: Mapped["Invoice"] = relationship(back_populates="audit_events")
    actor_user: Mapped["User | None"] = relationship()

    __table_args__ = (
        Index("ix_audit_events_invoice_created", "invoice_id", "created_at"),
    )