"""Integration tests for the risk-engine -> review-case wiring.

``run_risk_assessment`` owns the decision to request human review: a HIGH
assessment or a REJECT recommendation opens a case. These tests assert that
wiring end to end — the risk rows in, a ReviewCase out — rather than testing
``create_review_case`` again, which ``test_review_service.py`` already covers.
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.analysis import AnalysisResult
from app.models.audit import AuditEvent
from app.models.document import Document
from app.models.enums import (
    AnalysisStatus,
    AnalysisType,
    AuditEventType,
    InvoiceStatus,
    ReviewStatus,
)
from app.models.invoice import Invoice, LineItem
from app.models.review import ReviewCase
from app.services.review_service import get_review_case_for_invoice
from app.services.risk_service import run_risk_assessment

BASE = datetime(2026, 1, 1, tzinfo=UTC)

#: 35 (duplicate) + 25 (PO mismatch) + 20 (strong anomaly) = 80: HIGH, but below
#: REJECT_MIN (85), so the invoice routes to NEEDS_REVIEW rather than rejected.
HIGH_INPUTS = (
    (AnalysisType.DUPLICATE_CHECK, AnalysisStatus.FAILED, {"verdict": "duplicate"}),
    (AnalysisType.PO_MATCH, AnalysisStatus.FAILED, {"overall_outcome": "mismatch"}),
    (AnalysisType.ANOMALY, AnalysisStatus.FAILED, {"verdict": "strong_anomaly"}),
)

#: Adding no-applicable-policy (10) reaches 90, which clears REJECT_MIN.
REJECT_INPUTS = HIGH_INPUTS + (
    (
        AnalysisType.POLICY_CHECK,
        AnalysisStatus.SKIPPED,
        {"rule_id": "NO_APPLICABLE_POLICY", "evidence": []},
    ),
)


def make_invoice(db: Session, **overrides) -> Invoice:
    """Insert a Document + Invoice directly in the DB."""
    defaults = {
        "vendor_name": "Acme Corp",
        "invoice_number": "INV-0001",
        "invoice_date": date(2026, 1, 1),
        "total_amount": Decimal("100.00"),
        "currency": "USD",
        "status": InvoiceStatus.EXTRACTED,
        "extraction_confidence": {"vendor_name": 0.95, "total_amount": 0.95},
        "created_at": BASE,
    }
    defaults.update(overrides)

    invoice = Invoice(
        document=Document(
            filename="invoice.pdf",
            content_type="application/pdf",
            size_bytes=2048,
            sha256=uuid.uuid4().hex,
            storage_path="/storage/invoice.pdf",
        ),
        **defaults,
    )
    invoice.line_items = [
        LineItem(
            position=1,
            description="Widget",
            quantity=Decimal("1"),
            unit_price=Decimal("10.00"),
            line_total=Decimal("10.00"),
        )
    ]
    db.add(invoice)
    db.commit()
    return invoice


def seed_results(
    db: Session,
    invoice: Invoice,
    rows: tuple[tuple[AnalysisType, AnalysisStatus, dict], ...],
) -> None:
    """Insert the prior-analysis rows the risk engine reads."""
    for analysis_type, status, details in rows:
        db.add(
            AnalysisResult(
                invoice_id=invoice.id,
                analysis_type=analysis_type,
                status=status,
                details=details,
            )
        )
    db.commit()


def case_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(ReviewCase)) or 0


def review_requested_events(db: Session, invoice: Invoice) -> list[AuditEvent]:
    return list(
        db.scalars(
            select(AuditEvent).where(
                AuditEvent.invoice_id == invoice.id,
                AuditEvent.event_type == AuditEventType.REVIEW_REQUESTED,
            )
        )
    )


def test_assess_risk_high_creates_review_case(db_session: Session) -> None:
    invoice = make_invoice(db_session)
    seed_results(db_session, invoice, HIGH_INPUTS)

    run_risk_assessment(db_session, invoice_id=invoice.id)

    case = get_review_case_for_invoice(db_session, invoice.id)
    assert case is not None
    assert case.status == ReviewStatus.PENDING
    # The reason names the score that triggered it, so a reviewer sees why they
    # were pulled in without opening the risk panel.
    assert case.reason == "Risk score 80 (high); recommended: review"
    assert "80" in case.reason
    assert case.invoice_id == invoice.id

    # 80 is HIGH but below REJECT_MIN, so the invoice is flagged, not rejected.
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.NEEDS_REVIEW

    assert len(review_requested_events(db_session, invoice)) == 1


def test_assess_risk_low_does_not_create_review_case(db_session: Session) -> None:
    # A confirmed-unique duplicate on a complete, high-confidence invoice is a
    # clean bill of health: scoring is advisory and must not manufacture work.
    invoice = make_invoice(db_session)
    seed_results(
        db_session,
        invoice,
        ((AnalysisType.DUPLICATE_CHECK, AnalysisStatus.PASSED, {"verdict": "unique"}),),
    )

    run_risk_assessment(db_session, invoice_id=invoice.id)

    assert get_review_case_for_invoice(db_session, invoice.id) is None
    assert case_count(db_session) == 0
    assert review_requested_events(db_session, invoice) == []
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.EXTRACTED


def test_assess_risk_medium_does_not_create_review_case(db_session: Session) -> None:
    # Only HIGH or REJECT opens a case. MEDIUM is a warning, not a red flag, and
    # a model that interrupted a human for every partially-suspicious invoice
    # would bury the genuinely bad ones.
    #
    # 25 (a full PO mismatch) sits exactly on RISK_LEVEL_MEDIUM_MIN. A
    # partial_match would only reach 12 and stay LOW.
    invoice = make_invoice(db_session)
    seed_results(
        db_session,
        invoice,
        ((AnalysisType.PO_MATCH, AnalysisStatus.FAILED, {"overall_outcome": "mismatch"}),),
    )

    run_risk_assessment(db_session, invoice_id=invoice.id)

    scores = db_session.scalars(
        select(AnalysisResult).where(
            AnalysisResult.invoice_id == invoice.id,
            AnalysisResult.analysis_type == AnalysisType.RISK_SCORE,
        )
    ).one()
    assert scores.details["level"] == "medium"
    assert get_review_case_for_invoice(db_session, invoice.id) is None
    assert case_count(db_session) == 0


def test_assess_risk_reject_creates_review_case(db_session: Session) -> None:
    invoice = make_invoice(db_session)
    seed_results(db_session, invoice, REJECT_INPUTS)

    run_risk_assessment(db_session, invoice_id=invoice.id)

    case = get_review_case_for_invoice(db_session, invoice.id)
    assert case is not None
    assert case.status == ReviewStatus.PENDING
    assert case.reason == "Risk score 90 (high); recommended: reject"
    assert len(review_requested_events(db_session, invoice)) == 1


def test_assess_risk_is_idempotent_for_review_case(db_session: Session) -> None:
    # create_review_case is idempotent and invoice_id is uniquely constrained, so
    # re-running must converge on one case rather than duplicating work items.
    invoice = make_invoice(db_session)
    seed_results(db_session, invoice, HIGH_INPUTS)

    run_risk_assessment(db_session, invoice_id=invoice.id)
    run_risk_assessment(db_session, invoice_id=invoice.id)
    run_risk_assessment(db_session, invoice_id=invoice.id)

    assert case_count(db_session) == 1
    # One case means one review_requested event too, not three.
    assert len(review_requested_events(db_session, invoice)) == 1


def test_assess_risk_case_and_score_commit_together(db_session: Session) -> None:
    # The case is created inside run_risk_assessment's transaction, so the score,
    # the audit events, and the case are visible together or not at all.
    invoice = make_invoice(db_session)
    seed_results(db_session, invoice, HIGH_INPUTS)

    run_risk_assessment(db_session, invoice_id=invoice.id)

    db_session.expunge_all()
    case = get_review_case_for_invoice(db_session, invoice.id)
    scores = list(
        db_session.scalars(
            select(AnalysisResult).where(
                AnalysisResult.invoice_id == invoice.id,
                AnalysisResult.analysis_type == AnalysisType.RISK_SCORE,
            )
        )
    )
    assert case is not None
    assert len(scores) == 1
    assert scores[0].details["score"] == 80
    assert scores[0].status == AnalysisStatus.FAILED