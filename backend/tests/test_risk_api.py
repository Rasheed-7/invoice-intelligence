"""HTTP-level tests for the risk-scoring endpoints.

Uses the shared ``client`` fixture (savepoint-scoped session + tmp storage
root), so every row and file written here is discarded on teardown.

Two shapes of test coexist. The pipeline tests drive the real HTTP flow
(upload -> analyze -> validate -> assess-risk) against the acme-simple fixture.
The transition tests seed AnalysisResult rows directly, because provoking a
HIGH or REJECT score through the fixture corpus would mean contriving a document
to trip four unrelated detectors at once; seeding the inputs the engine reads
tests this layer's contract without coupling it to those services' behaviour.
"""

import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.analysis import AnalysisResult
from app.models.document import Document
from app.models.enums import AnalysisStatus, AnalysisType, InvoiceStatus
from app.models.invoice import Invoice, LineItem

INVOICES_URL = "/api/v1/invoices"
UNKNOWN_ID = "00000000-0000-0000-0000-000000000000"
BASE = datetime(2026, 1, 1, tzinfo=UTC)

#: Every key a persisted RiskAssessment carries. Asserted verbatim so a trimmed
#: or renamed field is a test failure rather than a silent API change.
ASSESSMENT_KEYS = {
    "invoice_id",
    "score",
    "level",
    "recommended_action",
    "contributors",
    "contributing_total",
    "caps_applied",
    "notes",
}

#: Per-contributor keys, from RiskContributor.
CONTRIBUTOR_KEYS = {
    "rule_id",
    "category",
    "points",
    "cap",
    "severity",
    "message",
    "evidence",
}

#: A confirmed duplicate (35) plus a PO mismatch (25) plus a strong anomaly
#: (20) = 80: HIGH, but below REJECT_MIN=85, so it routes to NEEDS_REVIEW
#: rather than rejecting.
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


def upload_fixture(client: TestClient, name: str = "acme.pdf") -> str:
    """Upload the acme-simple fixture PDF and return the invoice id."""
    from app.services.llm.fixture_pdfs import build_fixture_pdf
    from app.services.llm.fixtures import FIXTURES

    upload = client.post(
        f"{INVOICES_URL}/upload",
        files={"file": (name, build_fixture_pdf(FIXTURES[0]), "application/pdf")},
    )
    assert upload.status_code == 201
    return upload.json()["id"]


def make_invoice(db: Session, **overrides) -> Invoice:
    """Insert a Document + Invoice directly in the DB.

    Defaults are a complete, high-confidence invoice, so a test only overrides
    what it cares about.
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


# --- POST /assess-risk --------------------------------------------------------


def test_assess_risk_endpoint_success(client: TestClient, storage_root: Path) -> None:
    """Full pipeline: upload -> analyze -> validate -> assess-risk."""
    invoice_id = upload_fixture(client)
    assert client.post(f"{INVOICES_URL}/{invoice_id}/analyze").status_code == 200
    assert client.post(f"{INVOICES_URL}/{invoice_id}/validate").status_code == 200

    response = client.post(f"{INVOICES_URL}/{invoice_id}/assess-risk")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == invoice_id
    # Scoring is advisory: a low score must not stall a healthy pipeline, so
    # the status stays wherever /validate left it.
    assert body["status"] == "validated"

    read = client.get(f"{INVOICES_URL}/{invoice_id}/risk")
    assert read.status_code == 200
    # The acme fixture genuinely has no PO, so extraction reported 0.0
    # confidence for purchase_order.number -- a real low-confidence finding,
    # not a placeholder.
    risk = read.json()
    assert risk["level"] == "low"
    assert risk["recommended_action"] == "auto_approve"
    assert [c["rule_id"] for c in risk["contributors"]] == [
        "low_extraction_confidence"
    ]


def test_assess_risk_endpoint_unknown_id(client: TestClient) -> None:
    response = client.post(f"{INVOICES_URL}/{UNKNOWN_ID}/assess-risk")

    assert response.status_code == 404
    assert response.json()["detail"] == "invoice not found"


def test_assess_risk_endpoint_is_idempotent(
    client: TestClient, db_session: Session
) -> None:
    invoice_id = upload_fixture(client)
    assert client.post(f"{INVOICES_URL}/{invoice_id}/analyze").status_code == 200

    assert client.post(f"{INVOICES_URL}/{invoice_id}/assess-risk").status_code == 200
    first = client.get(f"{INVOICES_URL}/{invoice_id}/risk").json()

    # Re-running must replace the prior row, not append a second one.
    assert client.post(f"{INVOICES_URL}/{invoice_id}/assess-risk").status_code == 200
    after = client.get(f"{INVOICES_URL}/{invoice_id}/risk").json()

    invoice = db_session.get(Invoice, invoice_id)
    assert invoice is not None
    assert len(risk_rows(db_session, invoice)) == 1
    assert first == after


def test_assess_risk_response_status_reflects_transition(
    client: TestClient, db_session: Session
) -> None:
    invoice = make_invoice(db_session)
    seed_results(db_session, invoice, HIGH_INPUTS)

    response = client.post(f"{INVOICES_URL}/{invoice.id}/assess-risk")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "needs_review"
    assert body["id"] == str(invoice.id)


def test_assess_risk_reject_response_status_rejected(
    client: TestClient, db_session: Session
) -> None:
    invoice = make_invoice(db_session)
    seed_results(db_session, invoice, REJECT_INPUTS)

    response = client.post(f"{INVOICES_URL}/{invoice.id}/assess-risk")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "rejected"

    risk = client.get(f"{INVOICES_URL}/{invoice.id}/risk").json()
    assert risk["recommended_action"] == "reject"
    assert risk["level"] == "high"


# --- GET /risk ----------------------------------------------------------------


def test_get_risk_empty_state(client: TestClient) -> None:
    """A real invoice that was never scored gets a neutral payload, not a 404."""
    invoice_id = upload_fixture(client)

    response = client.get(f"{INVOICES_URL}/{invoice_id}/risk")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "invoice_id": invoice_id,
        "score": None,
        "level": "unknown",
        "recommended_action": "unknown",
        "contributors": [],
        "contributing_total": 0,
        "caps_applied": False,
        "notes": ["risk assessment has not been run"],
    }


def test_get_risk_after_assessment(
    client: TestClient, db_session: Session
) -> None:
    """The GET payload is exactly what run_risk_assessment persisted."""
    invoice = make_invoice(db_session)
    seed_results(db_session, invoice, HIGH_INPUTS)

    assert client.post(f"{INVOICES_URL}/{invoice.id}/assess-risk").status_code == 200

    response = client.get(f"{INVOICES_URL}/{invoice.id}/risk")
    assert response.status_code == 200
    persisted = risk_rows(db_session, invoice)[0]

    # Returned unchanged: no trimming, no renormalization of the score.
    assert response.json() == persisted.details
    assert response.json()["score"] == 80
    assert response.json()["level"] == "high"


def test_get_risk_unknown_invoice(client: TestClient) -> None:
    response = client.get(f"{INVOICES_URL}/{UNKNOWN_ID}/risk")

    assert response.status_code == 404
    assert response.json()["detail"] == "invoice not found"


def test_get_risk_payload_shape(
    client: TestClient, db_session: Session
) -> None:
    invoice = make_invoice(db_session)
    seed_results(db_session, invoice, HIGH_INPUTS)
    assert client.post(f"{INVOICES_URL}/{invoice.id}/assess-risk").status_code == 200

    body = client.get(f"{INVOICES_URL}/{invoice.id}/risk").json()

    assert set(body) == ASSESSMENT_KEYS
    assert body["invoice_id"] == str(invoice.id)
    assert isinstance(body["score"], int)
    assert body["level"] in {"low", "medium", "high"}
    assert body["recommended_action"] in {"auto_approve", "review", "reject"}
    assert isinstance(body["contributors"], list)
    assert isinstance(body["contributing_total"], int)
    assert isinstance(body["caps_applied"], bool)
    assert isinstance(body["notes"], list)


def test_get_risk_empty_state_payload_shape(client: TestClient) -> None:
    """The empty payload carries the same keys, so a client can parse one shape."""
    invoice_id = upload_fixture(client)

    body = client.get(f"{INVOICES_URL}/{invoice_id}/risk").json()

    assert set(body) == ASSESSMENT_KEYS


def test_get_risk_contributors_shape(
    client: TestClient, db_session: Session
) -> None:
    invoice = make_invoice(db_session)
    seed_results(db_session, invoice, HIGH_INPUTS)
    assert client.post(f"{INVOICES_URL}/{invoice.id}/assess-risk").status_code == 200

    contributors = client.get(f"{INVOICES_URL}/{invoice.id}/risk").json()["contributors"]

    assert len(contributors) == 3
    for contributor in contributors:
        assert set(contributor) == CONTRIBUTOR_KEYS
        assert isinstance(contributor["rule_id"], str)
        assert isinstance(contributor["category"], str)
        assert isinstance(contributor["points"], int)
        assert isinstance(contributor["cap"], int)
        assert contributor["severity"] in {"info", "warn", "error"}
        assert isinstance(contributor["message"], str)
        assert isinstance(contributor["evidence"], dict)
        # The per-contributor cap is an invariant the API exposes verbatim.
        assert 0 < contributor["points"] <= contributor["cap"]


def test_get_risk_details_are_json_safe(
    client: TestClient, db_session: Session
) -> None:
    """The payload must round-trip through json.dumps with no Decimal/date leak."""
    invoice = make_invoice(db_session)
    seed_results(db_session, invoice, REJECT_INPUTS)
    assert client.post(f"{INVOICES_URL}/{invoice.id}/assess-risk").status_code == 200

    response = client.get(f"{INVOICES_URL}/{invoice.id}/risk")

    decoded = json.loads(response.content.decode())
    assert decoded["score"] == 90
    assert decoded["invoice_id"] == str(invoice.id)
    for contributor in decoded["contributors"]:
        json.dumps(contributor["evidence"])


# --- isolation ----------------------------------------------------------------


def test_assess_risk_does_not_modify_other_analysis_rows(
    client: TestClient, db_session: Session
) -> None:
    """Only RISK_SCORE rows belong to this endpoint; the rest must be untouched."""
    invoice_id = upload_fixture(client)
    assert client.post(f"{INVOICES_URL}/{invoice_id}/analyze").status_code == 200
    assert client.post(f"{INVOICES_URL}/{invoice_id}/validate").status_code == 200
    assert client.post(f"{INVOICES_URL}/{invoice_id}/check-duplicates").status_code == 200
    assert client.post(f"{INVOICES_URL}/{invoice_id}/check-anomalies").status_code == 200

    invoice = db_session.get(Invoice, invoice_id)
    assert invoice is not None

    def snapshot() -> dict:
        """Map row id -> (analysis_type, status, details-json).

        Keyed by id because details is a dict and therefore unhashable; the
        JSON encoding keeps the comparison exact.
        """
        return {
            row.id: (
                row.analysis_type,
                row.status,
                json.dumps(row.details, sort_keys=True, default=str),
            )
            for row in db_session.scalars(
                select(AnalysisResult).where(AnalysisResult.invoice_id == invoice_id)
            )
        }

    before = snapshot()
    before_types = {state[0] for state in before.values()}
    assert AnalysisType.RISK_SCORE not in before_types
    # Three prior stages ran: 7 calculation checks, one duplicate check, one
    # anomaly check. PO matching was not invoked, so it contributes no row.
    assert len(before) == 9
    assert before_types == {
        AnalysisType.CALCULATION_CHECK,
        AnalysisType.DUPLICATE_CHECK,
        AnalysisType.ANOMALY,
    }

    assert client.post(f"{INVOICES_URL}/{invoice_id}/assess-risk").status_code == 200

    db_session.expire_all()
    after = snapshot()

    # Every pre-existing row survives with identical type, status and details.
    assert before.items() <= after.items()
    # Exactly one row was added, and it is of this endpoint's own type.
    assert len(after) == len(before) + 1
    new_ids = set(after) - set(before)
    assert len(new_ids) == 1
    assert after[new_ids.pop()][0] == AnalysisType.RISK_SCORE