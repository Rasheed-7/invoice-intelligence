"""Result types for deterministic PO <-> invoice matching.

No LLM is involved in producing any of these values: every field is derived
by direct field comparison in :mod:`app.services.po_matching.matcher`.
"""

import enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class MatchOutcome(str, enum.Enum):
    MATCH = "match"
    PARTIAL_MATCH = "partial_match"
    MISMATCH = "mismatch"
    SKIPPED = "skipped"


class MismatchSeverity(str, enum.Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class FieldMismatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    po_value: str | None
    invoice_value: str | None
    difference: str | None
    severity: MismatchSeverity


class LineMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_item_id: UUID | None
    po_item_id: UUID | None
    outcome: MatchOutcome
    mismatches: list[FieldMismatch] = []


class PurchaseOrderMatchReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_id: UUID
    po_reference: str | None
    po_found: bool
    overall_outcome: MatchOutcome
    line_matches: list[LineMatch]
    unmatched_invoice_items: list[UUID]
    unmatched_po_items: list[UUID]
    mismatch_summary: dict[str, int]
    vendor_matches: bool | None
    total_matches: bool | None
    notes: list[str]
