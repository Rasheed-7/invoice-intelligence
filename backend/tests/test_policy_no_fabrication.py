"""Tests that the policy check never fabricates evidence.

This module encodes one hard rule: the service never produces evidence that
does not correspond to a real row in ``policy_chunks``. A fabricated citation
would let the platform assert a policy requirement that no document contains,
which is worse than saying nothing.

Each test is written so that it fails loudly if the service ever starts
inventing, paraphrasing, or padding results.
"""

import uuid
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.analysis import AnalysisResult
from app.models.document import Document
from app.models.enums import AnalysisStatus, AnalysisType, InvoiceStatus
from app.models.invoice import Invoice
from app.models.policy import PolicyChunk, PolicyDocument
from app.services.policy_analysis import run_policy_analysis

POLICIES_URL = "/api/v1/policies"

POLICY_TEXT = "Approval is required for any invoice amount above 5000 USD."


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


def make_invoice(db: Session, **overrides) -> Invoice:
    document = Document(
        filename="invoice.pdf",
        content_type="application/pdf",
        size_bytes=1024,
        sha256=uuid.uuid4().hex,
        storage_path="/storage/invoice.pdf",
    )
    data = {
        "vendor_name": "Acme Corporation",
        "currency": "USD",
        "total_amount": "7500.00",
        "status": InvoiceStatus.EXTRACTED,
    }
    data.update(overrides)
    invoice = Invoice(document=document, **data)
    db.add(invoice)
    db.commit()
    return invoice


def insert_policy_with_chunk(db: Session, *, is_active: bool = True) -> PolicyDocument:
    """Insert a policy and one embedded chunk directly, bypassing the pipeline."""
    from app.services.embeddings.factory import get_embedding_provider

    document = PolicyDocument(
        title="Approval Policy",
        source_filename="approval.pdf",
        sha256=uuid.uuid4().hex,
        storage_path="/storage/approval.pdf",
        is_active=is_active,
    )
    db.add(document)
    db.flush()

    db.add(
        PolicyChunk(
            policy_document_id=document.id,
            position=1,
            page_number=1,
            content=POLICY_TEXT,
            token_count=len(POLICY_TEXT.split()),
            embedding=get_embedding_provider().embed_one(POLICY_TEXT),
        )
    )
    db.commit()
    return document


def test_analysis_result_evidence_matches_chunks(
    db_session: Session, client: TestClient, storage_root: Path
) -> None:
    """Every cited chunk_id must exist, and cite the policy it belongs to.

    If the service ever invented a chunk id, this fails: the lookup below is
    against the real table, not against anything the service passed in.
    """
    policy = insert_policy_with_chunk(db_session)
    invoice = make_invoice(db_session)

    result = run_policy_analysis(db_session, invoice_id=invoice.id)

    assert result.status == AnalysisStatus.PASSED
    details = result.details
    assert details is not None

    real_chunks = {
        chunk.id: (chunk.policy_document_id, chunk.content, chunk.page_number)
        for chunk in db_session.scalars(select(PolicyChunk)).all()
    }
    assert real_chunks, "fixture should have inserted a chunk"

    evidence = details["evidence"]
    assert evidence, "a matching policy should produce evidence"
    for item in evidence:
        chunk_id = uuid.UUID(item["chunk_id"])
        # The whole point: this id resolves to a stored row.
        assert chunk_id in real_chunks
        stored_policy_id, stored_content, stored_page = real_chunks[chunk_id]
        assert item["text"] == stored_content
        assert item["page_number"] == stored_page
        assert uuid.UUID(item["policy_document_id"]) == stored_policy_id == policy.id


def test_no_evidence_when_policy_inactive(
    db_session: Session, client: TestClient, storage_root: Path
) -> None:
    """An inactive policy contributes nothing; no fallback to it."""
    insert_policy_with_chunk(db_session, is_active=False)
    invoice = make_invoice(db_session)

    result = run_policy_analysis(db_session, invoice_id=invoice.id)

    assert result.status == AnalysisStatus.SKIPPED
    details = result.details
    assert details is not None
    assert details["rule_id"] == "NO_APPLICABLE_POLICY"
    # The chunk exists in the table but is unreachable through an inactive
    # policy, and must not appear.
    assert db_session.scalars(select(PolicyChunk)).all(), "chunk row should exist"
    assert details["evidence"] == []


def test_empty_policy_corpus_never_produces_evidence(
    db_session: Session, client: TestClient, storage_root: Path
) -> None:
    """No policies at all: evidence is empty and the message claims nothing.

    The wording matters as much as the empty list - a message like "no policy
    applies" would be a claim the system cannot support when it never looked.
    """
    assert db_session.scalars(select(PolicyDocument)).all() == []
    invoice = make_invoice(db_session)

    result = run_policy_analysis(db_session, invoice_id=invoice.id)

    assert result.status == AnalysisStatus.SKIPPED
    details = result.details
    assert details is not None
    assert details["evidence"] == []
    assert details["rule_id"] == "NO_APPLICABLE_POLICY"
    # It states what it did ("no chunks matched"), not what is true
    # ("no policy applies"). No policy was searched for applicability here.
    assert "no policy chunk" in details["message"].lower()
    assert details["message"] == "No policy chunks matched the invoice query."


def test_evidence_count_never_exceeds_stored_chunks(
    db_session: Session, client: TestClient, storage_root: Path
) -> None:
    """The service cannot report more evidence than the corpus contains."""
    insert_policy_with_chunk(db_session)
    invoice = make_invoice(db_session)

    result = run_policy_analysis(db_session, invoice_id=invoice.id)

    stored = len(db_session.scalars(select(PolicyChunk)).all())
    details = result.details
    assert details is not None
    assert len(details["evidence"]) <= stored


def test_unembedded_chunks_are_never_cited(
    db_session: Session, client: TestClient, storage_root: Path
) -> None:
    """A chunk with no embedding has no vector and cannot be retrieved."""
    document = PolicyDocument(
        title="Unembedded Policy",
        source_filename="unembedded.pdf",
        sha256=uuid.uuid4().hex,
        storage_path="/storage/unembedded.pdf",
        is_active=True,
    )
    db_session.add(document)
    db_session.flush()
    db_session.add(
        PolicyChunk(
            policy_document_id=document.id,
            position=1,
            page_number=1,
            content="This text was never embedded so it cannot be retrieved.",
            token_count=10,
            embedding=None,
        )
    )
    db_session.commit()
    invoice = make_invoice(db_session)

    result = run_policy_analysis(db_session, invoice_id=invoice.id)

    details = result.details
    assert details is not None
    assert details["evidence"] == []


def test_policy_check_persists_only_real_text(
    db_session: Session, client: TestClient, storage_root: Path
) -> None:
    """The persisted blob contains only text that exists in policy_chunks."""
    insert_policy_with_chunk(db_session)
    invoice = make_invoice(db_session)

    run_policy_analysis(db_session, invoice_id=invoice.id)

    stored = db_session.scalars(
        select(AnalysisResult).where(
            AnalysisResult.invoice_id == invoice.id,
            AnalysisResult.analysis_type == AnalysisType.POLICY_CHECK,
        )
    ).one()
    corpus = {c.content for c in db_session.scalars(select(PolicyChunk)).all()}

    details = stored.details
    assert details is not None
    for item in details["evidence"]:
        # Exact membership: no substring, prefix or paraphrase would pass.
        assert item["text"] in corpus


@pytest.fixture(autouse=True)
def _require_storage(client: TestClient, storage_root: Path) -> None:
    """Keep the storage fixture alive for every test in this module."""
    return None


def test_module_uses_the_shared_rollback_fixture(
    db_session: Session, client: TestClient, storage_root: Path
) -> None:
    """Guard: this module must rely on db_session so rows are rolled back."""
    insert_policy_with_chunk(db_session)
    assert db_session.scalars(select(PolicyDocument)).all()