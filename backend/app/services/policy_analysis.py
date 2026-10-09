"""Retrieve policy evidence for an invoice and persist it as an AnalysisResult.

This service never invents policy text. Every PolicyEvidence item carries the
actual chunk content, page number, and the exact retrieval query that returned
it. If retrieval returns nothing, evidence is empty - never a placeholder or a
synthesized citation. Callers that produce user-facing "policy requires X"
statements must read from evidence and refuse to make claims without it.

Deliberately free of FastAPI imports, matching ``validation_service`` and
``po_matching_service``.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.analysis import AnalysisResult
from app.models.audit import AuditEvent
from app.models.enums import (
    AnalysisStatus,
    AnalysisType,
    AuditActor,
    AuditEventType,
)
from app.models.invoice import Invoice
from app.services.invoices import get_invoice
from app.services.policy_retrieval import build_invoice_query, search_policy

__all__ = [
    "MIN_POLICY_SIMILARITY",
    "PolicyEvidence",
    "PolicyFinding",
    "get_policy_evidence",
    "run_policy_analysis",
]

#: Floor below which a chunk is treated as noise rather than evidence.
#: Threshold chosen empirically for the hash provider. A real semantic provider
#: would want a different value; this is exposed as a constant for that reason.
MIN_POLICY_SIMILARITY = 0.1

#: How many chunks to retrieve. Five is enough to surface a policy without
#: flooding the persisted evidence blob.
RETRIEVAL_TOP_K = 5

#: Rule ids. Stable strings, so downstream consumers can branch on them.
RULE_EVIDENCE_FOUND = "POLICY_EVIDENCE_FOUND"
RULE_NO_APPLICABLE_POLICY = "NO_APPLICABLE_POLICY"

#: Finding status vocabulary. "failed" is deliberately absent: policy evidence
#: either exists or it does not, and the risk engine - not this service -
#: decides whether anything is wrong.
FindingStatus = Literal["passed", "warning", "skipped"]


class PolicyEvidence(BaseModel):
    """One retrieved policy chunk, with the query that found it."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: UUID
    policy_document_id: UUID
    policy_title: str
    page_number: int
    similarity: float
    text: str
    #: The exact query that returned this chunk. Always present, never
    #: fabricated - it is what makes a citation auditable.
    retrieval_query: str


class PolicyFinding(BaseModel):
    """The outcome of policy retrieval for one invoice."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    status: FindingStatus
    message: str
    #: Empty when no policy matched. Never populated with a placeholder.
    evidence: list[PolicyEvidence] = []


#: Finding status -> AnalysisStatus. The vocabularies overlap except that
#: AnalysisStatus also has "failed", which this service never produces.
_STATUS_MAP: dict[FindingStatus, AnalysisStatus] = {
    "passed": AnalysisStatus.PASSED,
    "warning": AnalysisStatus.WARNING,
    "skipped": AnalysisStatus.SKIPPED,
}


def _build_finding(db: Session, invoice: Invoice) -> tuple[PolicyFinding, str]:
    """Retrieve policy chunks for the invoice and turn them into a finding.

    Returns the finding and the query that produced it, which is echoed into
    both the evidence and the audit event.
    """
    query = build_invoice_query(invoice)

    retrieval = search_policy(
        db,
        query=query,
        top_k=RETRIEVAL_TOP_K,
        min_similarity=MIN_POLICY_SIMILARITY,
        active_only=True,
    )

    if not retrieval.chunks:
        return (
            PolicyFinding(
                rule_id=RULE_NO_APPLICABLE_POLICY,
                status="skipped",
                message="No policy chunks matched the invoice query.",
                evidence=[],
            ),
            query,
        )

    evidence = [
        PolicyEvidence(
            chunk_id=chunk.chunk_id,
            policy_document_id=chunk.policy_document_id,
            policy_title=chunk.policy_title,
            page_number=chunk.page_number,
            similarity=chunk.similarity,
            text=chunk.text,
            retrieval_query=query,
        )
        for chunk in retrieval.chunks
    ]

    count = len(evidence)
    return (
        PolicyFinding(
            rule_id=RULE_EVIDENCE_FOUND,
            status="passed",
            message=f"Retrieved {count} policy chunk(s) relevant to this invoice.",
            evidence=evidence,
        ),
        query,
    )


def run_policy_analysis(db: Session, *, invoice_id: UUID) -> AnalysisResult:
    """Retrieve policy evidence for an invoice and persist the finding.

    Replaces any prior POLICY_CHECK rows for this invoice rather than
    appending, so repeated calls converge on one result. The invoice status is
    deliberately left alone: policy evidence does not by itself force review -
    the risk engine decides that from the evidence stored here.

    Commits at the end.

    Returns:
        The persisted AnalysisResult.

    Raises:
        LookupError: Invoice not found.
    """
    invoice = get_invoice(db, invoice_id)
    if invoice is None:
        raise LookupError(f"invoice not found: {invoice_id}")

    finding, query = _build_finding(db, invoice)

    db.execute(
        delete(AnalysisResult).where(
            AnalysisResult.invoice_id == invoice_id,
            AnalysisResult.analysis_type == AnalysisType.POLICY_CHECK,
        )
    )
    db.flush()

    result = AnalysisResult(
        invoice_id=invoice_id,
        analysis_type=AnalysisType.POLICY_CHECK,
        status=_STATUS_MAP[finding.status],
        # Evidence is not scored: there is no meaningful 0..1 "how compliant"
        # number here, and inventing one would be a fabricated metric.
        score=None,
        details=finding.model_dump(mode="json"),
    )
    db.add(result)

    db.add(
        AuditEvent(
            invoice_id=invoice.id,
            # No dedicated event type exists for policy checks yet; NOTE with
            # structured data keeps it queryable.
            event_type=AuditEventType.NOTE,
            actor=AuditActor.SYSTEM,
            message=finding.message,
            data={
                "analysis": "policy_check",
                "rule_id": finding.rule_id,
                "chunk_count": len(finding.evidence),
                "query": query,
            },
        )
    )

    db.commit()
    db.refresh(result)

    # Refresh the invoice so the caller's object reflects committed state.
    db.refresh(invoice)

    return result


def get_policy_evidence(db: Session, invoice_id: UUID) -> AnalysisResult | None:
    """Return the latest POLICY_CHECK result for the invoice, or None.

    Ordered by created_at DESC with an id tiebreaker; normally a single row
    exists because run_policy_analysis replaces prior results.
    """
    stmt = (
        select(AnalysisResult)
        .where(
            AnalysisResult.invoice_id == invoice_id,
            AnalysisResult.analysis_type == AnalysisType.POLICY_CHECK,
        )
        .order_by(AnalysisResult.created_at.desc(), AnalysisResult.id.desc())
        .limit(1)
    )
    return db.scalars(stmt).first()