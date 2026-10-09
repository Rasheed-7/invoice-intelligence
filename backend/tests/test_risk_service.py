"""Service-level tests for risk-scoring persistence.

Covers build_analysis_inputs (the row -> snapshot mapping), run_risk_assessment
(persist, audit, status transition) and get_risk_assessment, against a real
database via the shared savepoint fixture.

Analysis rows are inserted by hand rather than produced by the upstream
services, so each test controls the exact snapshot the builder must produce.
"""

import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
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
from app.models.invoice import Invoice, LineItem
from app.services.risk_service import (
    build_analysis_inputs,
    get_risk_assessment,
    run_risk_assessment,
)

BASE = datetime(2026, 1, 1, tzinfo=UTC)

#: Worst legal stack, summing to exactly 90 on the 0-100 scale:
#: 35 (confirmed duplicate) + 25 (PO mismatch) + 20 (strong anomaly)
#: + 10 (no applicable policy). 90 >= REJECT_MIN (85), so this is the
#: REJECT band. Drop the policy row to land at 80, the top of the HIGH band.
STACKED_90 = (
    (AnalysisType.DUPLICATE_CHECK, AnalysisStatus.FAILED, {"verdict": "duplicate"}),
    (AnalysisType.PO_MATCH, AnalysisStatus.FAILED, {"overall_outcome": "mismatch"}),
    (AnalysisType.ANOMALY, AnalysisStatus.FAILED, {"verdict": "strong_anomaly"}),
    (
        AnalysisType.POLICY_CHECK,
        AnalysisStatus.SKIPPED,
        {"rule_id": "NO_APPLICABLE_POLICY", "evidence": []},
    ),
)


def make_invoice(db: Session, **overrides) -> Invoice:
    """Insert a Document + Invoice directly in the DB.

    Defaults describe a complete, high-confidence invoice, so a test only has to
    override the field it cares about.
    """
    defaults = {
        "vendor_name": "Acme Corp",
        "invoice_number": "INV-0001",
        "invoice_date": date(2026, 1, 1),
        "total_amount": Decimal("100.00"),
        "currency": "USD",
        "status": InvoiceStatus.EXTRACTED,
        "extraction_confidence": {"vendor_name": 0.95, "invoice_number": 0.95},
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


def add_result(
    db: Session,
    invoice: Invoice,
    analysis_type: AnalysisType,
    *,
    status: AnalysisStatus = AnalysisStatus.PASSED,
    details: dict | None = None,
) -> AnalysisResult:
    """Insert one AnalysisResult row for an invoice."""
    row = AnalysisResult(
        invoice_id=invoice.id,
        analysis_type=analysis_type,
        status=status,
        details=details,
    )
    db.add(row)
    db.commit()
    return row


def risk_rows(db: Session, invoice: Invoice) -> list[AnalysisResult]:
    """Return every RISK_SCORE row for the invoice."""
    return list(
        db.scalars(
            select(AnalysisResult).where(
                AnalysisResult.invoice_id == invoice.id,
                AnalysisResult.analysis_type == AnalysisType.RISK_SCORE,
            )
        )
    )


# --- build_analysis_inputs: no prior analyses --------------------------------


def test_build_inputs_with_no_prior_analysis(db_session: Session) -> None:
    # No stage ever ran, so every verdict is None and every count is zero.
    # Absence of evidence is not evidence of absence.
    invoice = make_invoice(db_session)
    inputs = build_analysis_inputs(db_session, invoice.id)

    assert inputs.duplicate_verdict is None
    assert inputs.po_match_verdict is None
    assert inputs.anomaly_verdict is None
    assert inputs.policy_evidence_found is None
    assert inputs.policy_evidence_count == 0
    assert inputs.validation_failed_count == 0
    assert inputs.validation_warning_count == 0
    assert inputs.missing_required_fields == []


def test_build_inputs_no_prior_analysis_lists_missing_fields(
    db_session: Session,
) -> None:
    # Genuinely-absent fields are reported even when no stage has run.
    invoice = make_invoice(
        db_session, vendor_name=None, total_amount=None, invoice_date=None
    )
    inputs = build_analysis_inputs(db_session, invoice.id)

    assert inputs.missing_required_fields == [
        "vendor_name",
        "invoice_date",
        "total_amount",
    ]


def test_build_inputs_unknown_invoice_raises(db_session: Session) -> None:
    with pytest.raises(LookupError):
        build_analysis_inputs(db_session, uuid.uuid4())


# --- build_analysis_inputs: per-stage mapping --------------------------------


def test_build_inputs_reads_duplicate_verdict(db_session: Session) -> None:
    invoice = make_invoice(db_session)
    add_result(
        db_session,
        invoice,
        AnalysisType.DUPLICATE_CHECK,
        status=AnalysisStatus.FAILED,
        details={"verdict": "duplicate", "top_match": None},
    )

    inputs = build_analysis_inputs(db_session, invoice.id)
    assert inputs.duplicate_verdict == "duplicate"


def test_build_inputs_reads_latest_duplicate_verdict(db_session: Session) -> None:
    # Latest row wins; ordering is created_at DESC with an id tiebreaker.
    invoice = make_invoice(db_session)
    add_result(
        db_session,
        invoice,
        AnalysisType.DUPLICATE_CHECK,
        details={"verdict": "unique"},
    )
    add_result(
        db_session,
        invoice,
        AnalysisType.DUPLICATE_CHECK,
        status=AnalysisStatus.WARNING,
        details={"verdict": "suspected_duplicate"},
    )

    inputs = build_analysis_inputs(db_session, invoice.id)
    assert inputs.duplicate_verdict == "suspected_duplicate"


def test_build_inputs_reads_po_match_verdict(db_session: Session) -> None:
    invoice = make_invoice(db_session)
    add_result(
        db_session,
        invoice,
        AnalysisType.PO_MATCH,
        status=AnalysisStatus.FAILED,
        details={"overall_outcome": "mismatch", "po_found": True},
    )

    inputs = build_analysis_inputs(db_session, invoice.id)
    assert inputs.po_match_verdict == "mismatch"


def test_build_inputs_reads_anomaly_verdict(db_session: Session) -> None:
    invoice = make_invoice(db_session)
    add_result(
        db_session,
        invoice,
        AnalysisType.ANOMALY,
        details={"verdict": "insufficient_history", "score": None},
    )

    inputs = build_analysis_inputs(db_session, invoice.id)
    assert inputs.anomaly_verdict == "insufficient_history"


def test_build_inputs_counts_validation_failures_and_warnings(
    db_session: Session,
) -> None:
    # One row per check, so this is a status tally, not a latest-row read.
    invoice = make_invoice(db_session)
    for _ in range(2):
        add_result(
            db_session,
            invoice,
            AnalysisType.CALCULATION_CHECK,
            status=AnalysisStatus.FAILED,
        )
    add_result(
        db_session,
        invoice,
        AnalysisType.CALCULATION_CHECK,
        status=AnalysisStatus.WARNING,
    )

    inputs = build_analysis_inputs(db_session, invoice.id)
    assert inputs.validation_failed_count == 2
    assert inputs.validation_warning_count == 1


def test_build_inputs_skipped_checks_count_as_neither(db_session: Session) -> None:
    invoice = make_invoice(db_session)
    add_result(db_session, invoice, AnalysisType.CALCULATION_CHECK)
    add_result(
        db_session,
        invoice,
        AnalysisType.CALCULATION_CHECK,
        status=AnalysisStatus.SKIPPED,
    )

    inputs = build_analysis_inputs(db_session, invoice.id)
    assert inputs.validation_failed_count == 0
    assert inputs.validation_warning_count == 0


def test_build_inputs_policy_evidence_found(db_session: Session) -> None:
    invoice = make_invoice(db_session)
    add_result(
        db_session,
        invoice,
        AnalysisType.POLICY_CHECK,
        details={
            "rule_id": "POLICY_EVIDENCE_FOUND",
            "evidence": [{"chunk_id": "a"}, {"chunk_id": "b"}],
        },
    )

    inputs = build_analysis_inputs(db_session, invoice.id)
    assert inputs.policy_evidence_found is True
    assert inputs.policy_evidence_count == 2


def test_build_inputs_policy_no_evidence(db_session: Session) -> None:
    invoice = make_invoice(db_session)
    add_result(
        db_session,
        invoice,
        AnalysisType.POLICY_CHECK,
        status=AnalysisStatus.SKIPPED,
        details={"rule_id": "NO_APPLICABLE_POLICY", "evidence": []},
    )

    inputs = build_analysis_inputs(db_session, invoice.id)
    assert inputs.policy_evidence_found is False
    assert inputs.policy_evidence_count == 0


def test_build_inputs_policy_not_run(db_session: Session) -> None:
    # The tri-state matters: "never ran" must not become "ran and found
    # nothing", which would manufacture a policy finding the platform has no
    # evidence for.
    invoice = make_invoice(db_session)
    inputs = build_analysis_inputs(db_session, invoice.id)

    assert inputs.policy_evidence_found is None
    assert inputs.policy_evidence_count == 0


def test_build_inputs_reads_extraction_confidence(db_session: Session) -> None:
    invoice = make_invoice(
        db_session,
        extraction_confidence={"vendor_name": 0.5, "total_amount": 0.9},
    )

    inputs = build_analysis_inputs(db_session, invoice.id)
    assert inputs.extraction_confidence == {"vendor_name": 0.5, "total_amount": 0.9}


def test_build_inputs_ignores_invalid_confidence_values(db_session: Session) -> None:
    # Unparseable values are missing data, not confidence 0.0 -- coercing them
    # to zero would sit below LOW_CONFIDENCE_THRESHOLD and inflate the score.
    invoice = make_invoice(
        db_session,
        extraction_confidence={"a": "high", "b": 0.7},
    )

    inputs = build_analysis_inputs(db_session, invoice.id)
    assert inputs.extraction_confidence == {"b": 0.7}


def test_build_inputs_confidence_ignores_bool_and_null(db_session: Session) -> None:
    # bool is an int subclass, so True must not become confidence 1.0.
    invoice = make_invoice(
        db_session,
        extraction_confidence={"flag": True, "empty": None, "vendor_name": 0.9},
    )

    inputs = build_analysis_inputs(db_session, invoice.id)
    assert inputs.extraction_confidence == {"vendor_name": 0.9}


def test_build_inputs_confidence_null_column_is_empty(db_session: Session) -> None:
    invoice = make_invoice(db_session, extraction_confidence=None)

    inputs = build_analysis_inputs(db_session, invoice.id)
    assert inputs.extraction_confidence == {}


def test_build_inputs_confidence_int_is_coerced_to_float(db_session: Session) -> None:
    invoice = make_invoice(db_session, extraction_confidence={"vendor_name": 1})

    inputs = build_analysis_inputs(db_session, invoice.id)
    assert inputs.extraction_confidence == {"vendor_name": 1.0}


def test_build_inputs_missing_required_fields(db_session: Session) -> None:
    invoice = make_invoice(
        db_session,
        vendor_name=None,
        total_amount=None,
        invoice_number="INV-0001",
    )

    inputs = build_analysis_inputs(db_session, invoice.id)
    assert inputs.missing_required_fields == ["vendor_name", "total_amount"]


def test_build_inputs_null_details_yields_no_verdict(db_session: Session) -> None:
    # A row with null details is unreadable, not a finding.
    invoice = make_invoice(db_session)
    add_result(db_session, invoice, AnalysisType.DUPLICATE_CHECK, details=None)

    inputs = build_analysis_inputs(db_session, invoice.id)
    assert inputs.duplicate_verdict is None


def test_build_inputs_non_string_detail_is_ignored(db_session: Session) -> None:
    # A verdict of the wrong JSON type must not be passed through as a verdict.
    invoice = make_invoice(db_session)
    add_result(
        db_session,
        invoice,
        AnalysisType.ANOMALY,
        details={"verdict": 42},
    )

    inputs = build_analysis_inputs(db_session, invoice.id)
    assert inputs.anomaly_verdict is None


# --- run_risk_assessment: persistence ----------------------------------------


def test_run_risk_assessment_no_inputs(db_session: Session) -> None:
    # Nothing else ran and nothing is missing, so the score is 0 / LOW /
    # AUTO_APPROVE and the row is PASSED with a zero fraction.
    invoice = make_invoice(db_session)
    run_risk_assessment(db_session, invoice_id=invoice.id)

    rows = risk_rows(db_session, invoice)
    assert len(rows) == 1
    row = rows[0]
    assert row.score == Decimal("0.0000")
    assert row.status == AnalysisStatus.PASSED
    assert row.details["score"] == 0
    assert row.details["level"] == "low"
    assert row.details["recommended_action"] == "auto_approve"


def test_run_risk_assessment_persists_full_details(db_session: Session) -> None:
    invoice = make_invoice(db_session)
    add_result(
        db_session,
        invoice,
        AnalysisType.DUPLICATE_CHECK,
        status=AnalysisStatus.FAILED,
        details={"verdict": "duplicate"},
    )

    returned = run_risk_assessment(db_session, invoice_id=invoice.id)
    assert returned.id == invoice.id

    rows = risk_rows(db_session, invoice)
    assert len(rows) == 1
    row = rows[0]

    # A confirmed duplicate is CAP_DUPLICATE=35. 35 falls in the MEDIUM band
    # (25 <= score < 60), so the row is a WARNING, not FAILED.
    assert row.score == Decimal("0.3500")
    assert row.status == AnalysisStatus.WARNING
    assert row.details["score"] == 35
    assert row.details["level"] == "medium"
    assert row.details["recommended_action"] == "review"
    assert [c["rule_id"] for c in row.details["contributors"]] == [
        "duplicate_finding"
    ]
    assert row.details["contributors"][0]["points"] == 35
    assert row.details["contributing_total"] == 35


def test_run_risk_assessment_is_idempotent(db_session: Session) -> None:
    # Re-running must converge on one current result, not accumulate.
    invoice = make_invoice(db_session)
    run_risk_assessment(db_session, invoice_id=invoice.id)
    run_risk_assessment(db_session, invoice_id=invoice.id)
    run_risk_assessment(db_session, invoice_id=invoice.id)

    assert len(risk_rows(db_session, invoice)) == 1


def test_run_risk_assessment_replaces_prior_score(db_session: Session) -> None:
    # A stale high score must not survive a later clean run.
    invoice = make_invoice(db_session)
    for analysis_type, status, details in STACKED_90:
        add_result(db_session, invoice, analysis_type, status=status, details=details)
    run_risk_assessment(db_session, invoice_id=invoice.id)
    assert risk_rows(db_session, invoice)[0].details["score"] == 90

    # Clearing the policy row drops the score to 80; the next run must replace
    # the stale 90 rather than accumulate alongside it.
    db_session.execute(
        delete(AnalysisResult).where(
            AnalysisResult.invoice_id == invoice.id,
            AnalysisResult.analysis_type == AnalysisType.POLICY_CHECK,
        )
    )
    db_session.commit()

    run_risk_assessment(db_session, invoice_id=invoice.id)
    rows = risk_rows(db_session, invoice)
    assert len(rows) == 1
    assert rows[0].details["score"] == 80
    assert rows[0].score == Decimal("0.8000")


def test_run_risk_assessment_leaves_other_types_intact(db_session: Session) -> None:
    # Each analysis type has its own service; this one only owns RISK_SCORE.
    invoice = make_invoice(db_session)
    add_result(
        db_session,
        invoice,
        AnalysisType.DUPLICATE_CHECK,
        details={"verdict": "unique"},
    )

    run_risk_assessment(db_session, invoice_id=invoice.id)

    surviving = list(
        db_session.scalars(
            select(AnalysisResult).where(AnalysisResult.invoice_id == invoice.id)
        )
    )
    assert {r.analysis_type for r in surviving} == {
        AnalysisType.DUPLICATE_CHECK,
        AnalysisType.RISK_SCORE,
    }


def test_run_risk_assessment_unknown_invoice(db_session: Session) -> None:
    with pytest.raises(LookupError):
        run_risk_assessment(db_session, invoice_id=uuid.uuid4())


# --- run_risk_assessment: status transitions ---------------------------------


def test_run_risk_assessment_high_score_sets_needs_review(db_session: Session) -> None:
    # 35 (duplicate) + 25 (mismatch) + 20 (strong anomaly) = 80: HIGH, but
    # below REJECT_MIN=85, so a human reviews rather than the invoice closing.
    invoice = make_invoice(db_session)
    for analysis_type, status, details in STACKED_90[:3]:
        add_result(db_session, invoice, analysis_type, status=status, details=details)

    returned = run_risk_assessment(db_session, invoice_id=invoice.id)

    assert returned.status == InvoiceStatus.NEEDS_REVIEW
    row = risk_rows(db_session, invoice)[0]
    assert row.details["score"] == 80
    assert row.details["level"] == "high"
    assert row.details["recommended_action"] == "review"


def test_run_risk_assessment_reject_sets_rejected(db_session: Session) -> None:
    # The stacked worst case sums to 90, which clears REJECT_MIN=85.
    invoice = make_invoice(db_session)
    for analysis_type, status, details in STACKED_90:
        add_result(db_session, invoice, analysis_type, status=status, details=details)

    returned = run_risk_assessment(db_session, invoice_id=invoice.id)

    assert returned.status == InvoiceStatus.REJECTED
    row = risk_rows(db_session, invoice)[0]
    assert row.details["score"] == 90
    assert row.details["recommended_action"] == "reject"


@pytest.mark.parametrize(
    "terminal",
    [InvoiceStatus.APPROVED, InvoiceStatus.REJECTED, InvoiceStatus.FAILED],
)
def test_run_risk_assessment_does_not_downgrade_terminal(
    db_session: Session, terminal: InvoiceStatus
) -> None:
    # A decision someone already made stands, even when a re-score would have
    # rejected the invoice outright. The new score is still persisted.
    invoice = make_invoice(db_session, status=terminal)
    for analysis_type, status, details in STACKED_90:
        add_result(db_session, invoice, analysis_type, status=status, details=details)

    returned = run_risk_assessment(db_session, invoice_id=invoice.id)

    assert returned.status == terminal
    rows = risk_rows(db_session, invoice)
    assert len(rows) == 1
    assert rows[0].details["score"] == 90


def test_run_risk_assessment_low_score_leaves_status_alone(
    db_session: Session,
) -> None:
    # Scoring is advisory: a healthy invoice is neither stalled nor advanced.
    invoice = make_invoice(db_session)
    add_result(
        db_session, invoice, AnalysisType.DUPLICATE_CHECK, details={"verdict": "unique"}
    )

    returned = run_risk_assessment(db_session, invoice_id=invoice.id)

    assert returned.status == InvoiceStatus.EXTRACTED


# --- run_risk_assessment: audit event ----------------------------------------


def test_run_risk_assessment_writes_audit_event(db_session: Session) -> None:
    invoice = make_invoice(db_session)
    add_result(
        db_session,
        invoice,
        AnalysisType.DUPLICATE_CHECK,
        status=AnalysisStatus.FAILED,
        details={"verdict": "duplicate"},
    )

    run_risk_assessment(db_session, invoice_id=invoice.id)

    events = list(
        db_session.scalars(
            select(AuditEvent).where(
                AuditEvent.invoice_id == invoice.id,
                AuditEvent.event_type == AuditEventType.RISK_SCORED,
            )
        )
    )
    assert len(events) == 1
    event = events[0]

    assert event.actor == AuditActor.SYSTEM
    assert event.message == (
        "Risk score 35 (medium); recommended: review. "
        "Contributors: duplicate_finding"
    )
    assert event.data == {
        "analysis": "risk_score",
        "score": 35,
        "level": "medium",
        "recommended_action": "review",
        "contributor_count": 1,
        "contributing_total": 35,
        "caps_applied": False,
    }


def test_audit_event_message_with_no_contributors(db_session: Session) -> None:
    invoice = make_invoice(db_session)
    run_risk_assessment(db_session, invoice_id=invoice.id)

    event = db_session.scalars(
        select(AuditEvent)
        .where(
            AuditEvent.invoice_id == invoice.id,
            AuditEvent.event_type == AuditEventType.RISK_SCORED,
        )
        .limit(1)
    ).first()

    assert event is not None
    assert "Contributors: none" in event.message


def test_audit_message_stays_within_column_width(db_session: Session) -> None:
    # AuditEvent.message is String(1024); the contributor list is the only
    # unbounded part, so it must truncate rather than fail the INSERT.
    invoice = make_invoice(db_session)
    for analysis_type, status, details in STACKED_90:
        add_result(db_session, invoice, analysis_type, status=status, details=details)
    run_risk_assessment(db_session, invoice_id=invoice.id)

    event = db_session.scalars(
        select(AuditEvent).where(
            AuditEvent.invoice_id == invoice.id,
            AuditEvent.event_type == AuditEventType.RISK_SCORED,
        )
    ).first()

    assert event is not None
    assert len(event.message) <= 1024


# --- score + details representation -------------------------------------------


def test_score_is_stored_as_fraction(db_session: Session) -> None:
    # The API and frontend present score as 0-100; Numeric(5,4) holds a
    # fraction, so a 90 is persisted as 0.9000.
    invoice = make_invoice(db_session)
    for analysis_type, status, details in STACKED_90:
        add_result(db_session, invoice, analysis_type, status=status, details=details)

    run_risk_assessment(db_session, invoice_id=invoice.id)
    row = get_risk_assessment(db_session, invoice.id)

    assert row is not None
    assert row.details["score"] == 90
    assert row.score == Decimal("0.9000")


def test_details_json_is_serializable(db_session: Session) -> None:
    # details lands in a JSONB column, so it must round-trip through
    # json.dumps with no Decimal or date leaking through as a non-JSON type.
    invoice = make_invoice(db_session)
    for analysis_type, status, details in STACKED_90:
        add_result(db_session, invoice, analysis_type, status=status, details=details)

    run_risk_assessment(db_session, invoice_id=invoice.id)
    row = get_risk_assessment(db_session, invoice.id)

    assert row is not None and row.details is not None
    encoded = json.dumps(row.details)
    decoded = json.loads(encoded)
    assert decoded["score"] == 90
    assert decoded["level"] == "high"
    assert decoded["invoice_id"] == str(invoice.id)
    assert len(decoded["contributors"]) == 4
    # Evidence values arrive from the engine already JSON-safe.
    for contributor in decoded["contributors"]:
        json.dumps(contributor["evidence"])


# --- get_risk_assessment -----------------------------------------------------


def test_get_risk_assessment_returns_none_before_any_run(db_session: Session) -> None:
    invoice = make_invoice(db_session)
    assert get_risk_assessment(db_session, invoice.id) is None