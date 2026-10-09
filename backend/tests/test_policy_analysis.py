"""Tests for policy evidence retrieval and persistence.

Policies and their chunks are ingested through the real endpoints so the
embedding pipeline runs end to end; assertions then read the persisted
AnalysisResult rather than the service's return value alone.
"""

import uuid
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.analysis import AnalysisResult
from app.models.audit import AuditEvent
from app.models.document import Document
from app.models.enums import (
    AnalysisStatus,
    AnalysisType,
    AuditActor,
    AuditEventType,
    InvoiceStatus,
)
from app.models.invoice import Invoice
from app.models.policy import PolicyChunk
from app.services.policy_analysis import (
    MIN_POLICY_SIMILARITY,
    PolicyEvidence,
    PolicyFinding,
    get_policy_evidence,
    run_policy_analysis,
)

POLICIES_URL = "/api/v1/policies"

APPROVAL_POLICY_PAGES = [
    "Approval Policy\n\n"
    "Manager approval is required for any invoice over 5000 USD.\n\n"
    "Invoices over 20000 USD require director approval.",
]


def build_pdf(pages: list[str]) -> bytes:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        y = 100.0
        for line in text.split("\n"):
            if line.strip():
                page.insert_text((72, y), line)
            y += 20
    data = doc.tobytes()
    doc.close()
    return data


def ingest_policy(client: TestClient, *, title: str = "Approval Policy") -> str:
    """Upload and process a policy, returning its id."""
    upload = client.post(
        POLICIES_URL,
        files={
            "file": (
                f"{title}.pdf",
                build_pdf(APPROVAL_POLICY_PAGES),
                "application/pdf",
            )
        },
        data={"title": title},
    )
    assert upload.status_code == 201, upload.text
    policy_id = upload.json()["id"]
    assert client.post(f"{POLICIES_URL}/{policy_id}/process").status_code == 200
    return policy_id


def make_invoice(db: Session, **overrides) -> Invoice:
    """Insert an invoice with extraction results already applied."""
    document = Document(
        filename="invoice.pdf",
        content_type="application/pdf",
        size_bytes=1024,
        sha256=uuid.uuid4().hex,
        storage_path="/storage/invoice.pdf",
    )
    data = {
        "vendor_name": "Acme Corporation",
        "invoice_number": "INV-0001",
        "currency": "USD",
        "total_amount": "108.50",
        "status": InvoiceStatus.EXTRACTED,
    }
    data.update(overrides)
    invoice = Invoice(document=document, **data)
    db.add(invoice)
    db.commit()
    return invoice


@pytest.fixture
def approval_policy(client: TestClient, storage_root: Path) -> str:
    """A processed approval policy with embedded chunks."""
    return ingest_policy(client)


# --- finding type invariants ------------------------------------------------


def test_policy_evidence_rejects_unknown_fields() -> None:
    """Evidence is strict: an invented field is an error, not a shrug."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PolicyEvidence(
            chunk_id=uuid.uuid4(),
            policy_document_id=uuid.uuid4(),
            policy_title="t",
            page_number=1,
            similarity=0.5,
            text="x",
            retrieval_query="q",
            fabricated_reason="because",
        )


def test_finding_status_never_allows_failed() -> None:
    """"failed" is not a valid finding status - the risk engine decides."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PolicyFinding(
            rule_id="POLICY_EVIDENCE_FOUND",
            status="failed",
            message="x",
            evidence=[],
        )


# --- evidence found ---------------------------------------------------------


def test_policy_check_with_matching_policy_returns_evidence(
    db_session: Session, client: TestClient, storage_root: Path, approval_policy: str
) -> None:
    """A matching policy yields PASSED with fully populated evidence items."""
    invoice = make_invoice(db_session)

    result = run_policy_analysis(db_session, invoice_id=invoice.id)

    assert result.status == AnalysisStatus.PASSED
    details = result.details
    assert details is not None
    assert details["status"] == "passed"
    assert details["evidence"]

    for item in details["evidence"]:
        # Every citation field is present and real, not blank or synthesized.
        assert uuid.UUID(item["chunk_id"])
        assert item["policy_title"] == "Approval Policy"
        assert item["page_number"] >= 1
        assert item["text"].strip()
        assert item["similarity"] >= MIN_POLICY_SIMILARITY
        assert item["retrieval_query"].strip()


def test_policy_check_no_matching_policy_is_skipped(
    db_session: Session, client: TestClient, storage_root: Path
) -> None:
    """With no policies indexed the finding is SKIPPED with no evidence."""
    invoice = make_invoice(db_session)

    result = run_policy_analysis(db_session, invoice_id=invoice.id)

    assert result.status == AnalysisStatus.SKIPPED
    details = result.details
    assert details is not None
    assert details["rule_id"] == "NO_APPLICABLE_POLICY"
    assert details["evidence"] == []


def test_policy_check_evidence_is_verbatim_from_chunk(
    db_session: Session, client: TestClient, storage_root: Path, approval_policy: str
) -> None:
    """Evidence text is the stored chunk content, byte for byte.

    No summarization, no trimming, no rewriting - a citation that does not
    match its source is worse than no citation.
    """
    invoice = make_invoice(db_session)

    result = run_policy_analysis(db_session, invoice_id=invoice.id)

    stored = {
        chunk.id: chunk.content
        for chunk in db_session.scalars(select(PolicyChunk)).all()
    }
    details = result.details
    assert details is not None
    for item in details["evidence"]:
        assert item["text"] == stored[uuid.UUID(item["chunk_id"])]


def test_policy_check_evidence_includes_retrieval_query(
    db_session: Session, client: TestClient, storage_root: Path, approval_policy: str
) -> None:
    """Every evidence item carries the query that produced it."""
    invoice = make_invoice(db_session)

    result = run_policy_analysis(db_session, invoice_id=invoice.id)

    details = result.details
    assert details is not None
    for item in details["evidence"]:
        assert item["retrieval_query"]
        assert "approval" in item["retrieval_query"]


# --- persistence ------------------------------------------------------------


def test_policy_check_is_idempotent(
    db_session: Session, client: TestClient, storage_root: Path, approval_policy: str
) -> None:
    """A second run replaces the first row and reproduces the same details."""
    invoice = make_invoice(db_session)

    first = run_policy_analysis(db_session, invoice_id=invoice.id)
    first_details = dict(first.details)

    second = run_policy_analysis(db_session, invoice_id=invoice.id)

    rows = db_session.scalars(
        select(AnalysisResult).where(
            AnalysisResult.invoice_id == invoice.id,
            AnalysisResult.analysis_type == AnalysisType.POLICY_CHECK,
        )
    ).all()
    assert len(rows) == 1
    assert dict(second.details) == first_details


def test_policy_check_does_not_change_invoice_status(
    db_session: Session, client: TestClient, storage_root: Path, approval_policy: str
) -> None:
    """EXTRACTED stays EXTRACTED - evidence alone never forces review."""
    invoice = make_invoice(db_session, status=InvoiceStatus.EXTRACTED)

    run_policy_analysis(db_session, invoice_id=invoice.id)

    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.EXTRACTED


def test_policy_check_does_not_delete_other_analysis_types(
    db_session: Session, client: TestClient, storage_root: Path, approval_policy: str
) -> None:
    """Re-running policy analysis leaves other analysis rows alone."""
    invoice = make_invoice(db_session)
    db_session.add(
        AnalysisResult(
            invoice_id=invoice.id,
            analysis_type=AnalysisType.PO_MATCH,
            status=AnalysisStatus.PASSED,
            score=None,
            details={"po_reference": "PO-1"},
        )
    )
    db_session.commit()

    run_policy_analysis(db_session, invoice_id=invoice.id)

    others = db_session.scalars(
        select(AnalysisResult).where(
            AnalysisResult.invoice_id == invoice.id,
            AnalysisResult.analysis_type == AnalysisType.PO_MATCH,
        )
    ).all()
    assert len(others) == 1


def test_policy_check_missing_invoice_raises(db_session: Session) -> None:
    """A random UUID has no invoice -> LookupError."""
    with pytest.raises(LookupError):
        run_policy_analysis(db_session, invoice_id=uuid.uuid4())


def test_policy_check_writes_audit_event(
    db_session: Session, client: TestClient, storage_root: Path, approval_policy: str
) -> None:
    """Each run appends a NOTE audit event carrying the query and chunk count."""
    invoice = make_invoice(db_session)

    run_policy_analysis(db_session, invoice_id=invoice.id)

    events = db_session.scalars(
        select(AuditEvent).where(AuditEvent.invoice_id == invoice.id)
    ).all()
    assert len(events) == 1
    event = events[0]
    assert event.event_type == AuditEventType.NOTE
    assert event.actor == AuditActor.SYSTEM
    assert event.data["analysis"] == "policy_check"
    assert event.data["rule_id"] == "POLICY_EVIDENCE_FOUND"
    assert event.data["chunk_count"] > 0
    assert event.data["query"]


def test_get_policy_evidence_round_trip(
    db_session: Session, client: TestClient, storage_root: Path, approval_policy: str
) -> None:
    """Before a run there is None; after, the persisted row comes back."""
    invoice = make_invoice(db_session)

    assert get_policy_evidence(db_session, invoice.id) is None

    run_policy_analysis(db_session, invoice_id=invoice.id)
    stored = get_policy_evidence(db_session, invoice.id)

    assert stored is not None
    assert stored.analysis_type == AnalysisType.POLICY_CHECK
    assert stored.status == AnalysisStatus.PASSED
    assert stored.details["evidence"]