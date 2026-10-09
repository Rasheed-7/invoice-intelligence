"""Scoring and reporting for duplicate invoice detection.

Signals are summed into a score, then mapped to a verdict by threshold. The
threshold boundaries live here rather than in the signal module so the policy
question ("how similar is too similar?") is visible in one place.
"""

from app.models.invoice import Invoice
from app.services.duplicate_detection.signals import WEIGHTS, which_signals
from app.services.duplicate_detection.types import (
    DuplicateMatch,
    DuplicateReport,
    DuplicateSignal,
    DuplicateVerdict,
)

__all__ = [
    "DUPLICATE_THRESHOLD",
    "SAME_VENDOR_INVOICE_NUMBER_THRESHOLD",
    "SUSPECTED_THRESHOLD",
    "find_duplicates",
    "score_pair",
]

#: Score floor applied when SAME_VENDOR_INVOICE_NUMBER fires, so the combined
#: score reflects that signal even when nothing else matches.
SAME_VENDOR_INVOICE_NUMBER_THRESHOLD = 0.75

#: Score at or above which a pair is a suspected duplicate.
SUSPECTED_THRESHOLD = 0.6

#: Score at or above which a pair is a confirmed duplicate.
#:
#: Set above the sum of the two mid-weight signals (SAME_PO_AMOUNT 0.5 +
#: SAME_VENDOR_AMOUNT 0.4 = 0.9) so that corroborating weak evidence lands in
#: SUSPECTED rather than DUPLICATE. Only an identical file (1.0) clears it by
#: score alone; SAME_VENDOR_INVOICE_NUMBER clears it via the verdict override
#: below, because that signal is definitive on its own and cannot be reached by
#: summing weaker ones.
DUPLICATE_THRESHOLD = 0.95


def _verdict_for(score: float) -> DuplicateVerdict:
    """Map a score to a verdict, highest tier first."""
    if score >= DUPLICATE_THRESHOLD:
        return DuplicateVerdict.DUPLICATE
    if score >= SUSPECTED_THRESHOLD:
        return DuplicateVerdict.SUSPECTED_DUPLICATE
    return DuplicateVerdict.UNIQUE


def score_pair(a: Invoice, b: Invoice) -> DuplicateMatch | None:
    """Score two invoices against each other, or None if no signal fires.

    ``a`` is the invoice under evaluation and ``b`` the candidate; signals are
    directional where it matters (line-item overlap is measured against a).

    Args:
        a: The invoice being checked.
        b: A candidate that might be its duplicate.

    Returns:
        A populated :class:`DuplicateMatch`, or None when the pair shares no
        signal at all.
    """
    signals = which_signals(a, b)
    if not signals:
        return None

    # Summing weights lets weak corroborating signals add up, but the total is
    # capped: beyond "certainly a duplicate" a higher number carries no more
    # information and would make cross-invoice comparisons meaningless.
    raw_score = min(1.0, sum(WEIGHTS[signal] for signal in signals))

    if DuplicateSignal.SAME_VENDOR_INVOICE_NUMBER in signals:
        # Same vendor + same invoice number is the canonical duplicate; the
        # combined score must reflect that even if no other signals fire.
        raw_score = max(raw_score, SAME_VENDOR_INVOICE_NUMBER_THRESHOLD)

    # No invoice is legitimately issued twice by one vendor under one number,
    # so this signal settles the verdict on its own rather than being summed
    # with weaker evidence. Scored alone it reaches only 0.75, which would
    # otherwise land in SUSPECTED.
    if DuplicateSignal.SAME_VENDOR_INVOICE_NUMBER in signals:
        verdict = DuplicateVerdict.DUPLICATE
    else:
        verdict = _verdict_for(raw_score)

    return DuplicateMatch(
        matching_invoice_id=b.id,
        score=raw_score,
        signals=signals,
        verdict=verdict,
    )


def _note_for(signal: DuplicateSignal) -> str:
    """A short human-readable description of why a signal fired."""
    return {
        DuplicateSignal.IDENTICAL_FILE: "matched identical file sha256",
        DuplicateSignal.SAME_VENDOR_INVOICE_NUMBER: "same vendor and invoice number",
        DuplicateSignal.SAME_VENDOR_AMOUNT: "same vendor and invoice amount",
        DuplicateSignal.SAME_PO_AMOUNT: "same PO number and amount",
        DuplicateSignal.LINE_ITEM_OVERLAP: "matching line items",
    }.get(signal, f"unknown signal {signal.value}")


def _highest_verdict(verdicts: list[DuplicateVerdict]) -> DuplicateVerdict:
    """Pick the most severe verdict, UNIQUE when the list is empty."""
    if not verdicts:
        return DuplicateVerdict.UNIQUE
    for verdict in (
        DuplicateVerdict.DUPLICATE,
        DuplicateVerdict.SUSPECTED_DUPLICATE,
    ):
        if verdict in verdicts:
            return verdict
    return DuplicateVerdict.UNIQUE


def find_duplicates(current: Invoice, candidates: list[Invoice]) -> DuplicateReport:
    """Compare one invoice against many candidates and summarise the outcome.

    The current invoice is excluded from its own candidate list: an invoice is
    trivially identical to itself, and leaving it in would make every invoice a
    confirmed duplicate of itself.

    Ordering is score descending with the matching id ascending as a
    tiebreaker, so equal-scoring candidates always come back in the same order
    and the report is reproducible across runs.

    Args:
        current: The invoice under evaluation.
        candidates: Invoices to compare against, including possibly ``current``.

    Returns:
        A :class:`DuplicateReport`. Verdict UNIQUE with no matches when nothing
        fires.
    """
    matches: list[DuplicateMatch] = []

    for candidate in candidates:
        if candidate.id == current.id:
            continue
        match = score_pair(current, candidate)
        if match is not None:
            matches.append(match)

    matches.sort(key=lambda m: (-m.score, m.matching_invoice_id))

    verdict = _highest_verdict([m.verdict for m in matches])
    top_match = matches[0] if matches else None

    notes: list[str] = []
    if top_match is None:
        notes.append("no duplicate signals found")
    else:
        notes.extend(_note_for(signal) for signal in top_match.signals)

    return DuplicateReport(
        invoice_id=current.id,
        verdict=verdict,
        top_match=top_match,
        all_matches=matches,
        notes=notes,
    )