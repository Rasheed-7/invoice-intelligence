"""Result types for duplicate invoice detection.

No LLM is involved in producing any of these values: every field is derived by
direct comparison in :mod:`app.services.duplicate_detection.scorer`.
"""

import enum
import uuid

from pydantic import BaseModel, ConfigDict


class DuplicateVerdict(str, enum.Enum):
    UNIQUE = "unique"
    SUSPECTED_DUPLICATE = "suspected_duplicate"
    DUPLICATE = "duplicate"


class DuplicateSignal(str, enum.Enum):
    IDENTICAL_FILE = "identical_file"
    SAME_VENDOR_INVOICE_NUMBER = "same_vendor_invoice_number"
    SAME_VENDOR_AMOUNT = "same_vendor_amount"
    SAME_PO_AMOUNT = "same_po_amount"
    LINE_ITEM_OVERLAP = "line_item_overlap"


class DuplicateMatch(BaseModel):
    """One candidate invoice and how strongly it resembles the current one."""

    model_config = ConfigDict(extra="forbid")

    matching_invoice_id: uuid.UUID
    #: Combined signal weight, 0.0..1.0.
    score: float
    signals: list[DuplicateSignal]
    verdict: DuplicateVerdict


class DuplicateReport(BaseModel):
    """Verdict for one invoice against a set of candidates."""

    model_config = ConfigDict(extra="forbid")

    invoice_id: uuid.UUID
    #: Highest-severity verdict across all matches, or UNIQUE when there are none.
    verdict: DuplicateVerdict
    #: Highest-scoring match, or None when the invoice is unique.
    top_match: DuplicateMatch | None
    #: Sorted by score descending, then matching_invoice_id ascending.
    all_matches: list[DuplicateMatch]
    notes: list[str]