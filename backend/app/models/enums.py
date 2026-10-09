"""Central home for every enum used by the domain models.

Keeping the enums in a single module has two benefits:

1. There is exactly one place to audit the full set of allowed values.
2. Every native PostgreSQL enum is produced by :func:`pg_enum`, which enforces
   the lowercase ``values_callable`` pattern. Without ``values_callable``
   SQLAlchemy persists the enum *member names* (e.g. ``"UPLOADED"``) while the
   native PostgreSQL labels are lowercase (e.g. ``"uploaded"``), so PostgreSQL
   raises ``InvalidTextRepresentation``.
"""

import enum

from sqlalchemy import Enum as SAEnum

__all__ = [
    "AnalysisStatus",
    "AnalysisType",
    "AuditActor",
    "AuditEventType",
    "DocumentProcessingStatus",
    "InvoiceStatus",
    "PurchaseOrderStatus",
    "ReviewStatus",
    "pg_enum",
]


def pg_enum(enum_cls: type[enum.Enum], name: str) -> SAEnum:
    """Return a native PostgreSQL enum column type bound to ``enum_cls``.

    ``values_callable`` makes SQLAlchemy persist the enum *values* (lowercase)
    instead of the member *names*, matching the native enum labels created by
    Alembic.
    """

    return SAEnum(
        enum_cls,
        name=name,
        native_enum=True,
        values_callable=lambda members: [member.value for member in members],
    )


class InvoiceStatus(enum.StrEnum):
    """Lifecycle of an invoice as it moves through the platform."""

    INGESTED = "ingested"  # document linked, not yet extracted
    EXTRACTED = "extracted"  # structured data pulled out
    VALIDATED = "validated"  # deterministic checks ran
    NEEDS_REVIEW = "needs_review"  # flagged for a human
    APPROVED = "approved"  # human or auto approved
    REJECTED = "rejected"  # rejected
    FAILED = "failed"  # processing error


class AuditEventType(enum.StrEnum):
    """Kinds of decisions/actions we record for compliance."""

    INGESTED = "ingested"
    EXTRACTED = "extracted"
    VALIDATION_FAILED = "validation_failed"
    VALIDATION_PASSED = "validation_passed"
    DUPLICATE_SUSPECTED = "duplicate_suspected"
    RISK_SCORED = "risk_scored"
    REVIEW_REQUESTED = "review_requested"
    REVIEW_DECIDED = "review_decided"
    STATUS_CHANGED = "status_changed"
    NOTE = "note"


class AuditActor(enum.StrEnum):
    """Who or what caused the event."""

    SYSTEM = "system"
    LLM = "llm"
    REVIEWER = "reviewer"


class DocumentProcessingStatus(enum.StrEnum):
    """Where an uploaded document is in the extraction pipeline."""

    UPLOADED = "uploaded"
    TEXT_EXTRACTED = "text_extracted"
    FAILED = "failed"


class PurchaseOrderStatus(enum.StrEnum):
    """Receiving lifecycle of a purchase order."""

    OPEN = "open"
    PARTIALLY_RECEIVED = "partially_received"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class ReviewStatus(enum.StrEnum):
    """State of a human review case."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class AnalysisType(enum.StrEnum):
    """The kind of deterministic/LLM analysis that produced a result."""

    CALCULATION_CHECK = "calculation_check"
    PO_MATCH = "po_match"
    DUPLICATE_CHECK = "duplicate_check"
    ANOMALY = "anomaly"
    RISK_SCORE = "risk_score"
    POLICY_CHECK = "policy_check"


class AnalysisStatus(enum.StrEnum):
    """Outcome of a single analysis run."""

    PASSED = "passed"
    FAILED = "failed"
    WARNING = "warning"
    SKIPPED = "skipped"
