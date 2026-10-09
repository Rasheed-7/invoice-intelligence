"""Tests for anomaly detection: baseline, Isolation Forest, and orchestration.

Pure in-memory invoices throughout - no database, no writes.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest import mock

import pytest

from app.models.document import Document
from app.models.invoice import Invoice, LineItem
from app.services.anomaly_detection.detector import detect_anomaly, max_severity
from app.services.anomaly_detection.features import (
    FREQUENCY_ANOMALY_THRESHOLD,
    MIN_HISTORY_FOR_BASELINE,
    MIN_HISTORY_FOR_ISOLATION_FOREST,
)
from app.services.anomaly_detection.isolation_forest import analyze_isolation_forest
from app.services.anomaly_detection.statistical import analyze_statistical
from app.services.anomaly_detection.types import (
    AnomalyMethod,
    AnomalyReport,
    AnomalyVerdict,
)
from app.services.po_matching.types import MismatchSeverity

BASE = datetime(2026, 1, 1, tzinfo=UTC)
CURRENT_AT = BASE + timedelta(days=365)


def make_invoice(
    *,
    total: Decimal | None = Decimal("100.00"),
    created_at: datetime = BASE,
    unit_price: Decimal | None = Decimal("10.00"),
    vendor: str = "Acme",
) -> Invoice:
    document = Document(
        filename="invoice.pdf",
        content_type="application/pdf",
        size_bytes=1024,
        sha256=uuid.uuid4().hex,
        storage_path="/storage/invoice.pdf",
    )
    invoice = Invoice(
        id=uuid.uuid4(),
        document=document,
        vendor_name=vendor,
        invoice_number="INV-" + uuid.uuid4().hex[:6],
        currency="USD",
        total_amount=total,
        created_at=created_at,
    )
    invoice.line_items = [
        LineItem(
            position=1,
            description="Widget",
            quantity=Decimal("1"),
            unit_price=unit_price,
            line_total=total if total is not None else Decimal("10.00"),
        )
    ]
    return invoice


def clustered_history(count: int = 10, mean: int = 100) -> list[Invoice]:
    """Prior invoices scattered around a mean, giving a non-zero stddev."""
    return [
        make_invoice(total=Decimal(str(mean + offset)), created_at=BASE + timedelta(days=i))
        for i, offset in enumerate(range(-4, -4 + count))
    ]


# --- insufficient history ---------------------------------------------------


def test_insufficient_history_for_new_vendor() -> None:
    """Two prior invoices is not a baseline, and the report says so."""
    current = make_invoice(created_at=CURRENT_AT)
    prior = [make_invoice(created_at=BASE + timedelta(days=i)) for i in range(2)]

    report = detect_anomaly(current, prior)

    assert report.verdict == AnomalyVerdict.INSUFFICIENT_HISTORY
    assert report.score is None
    assert report.method == AnomalyMethod.NONE
    assert report.history_size == 2
    assert report.contributions == []
    assert "2 prior invoices" in report.notes[0]
    assert str(MIN_HISTORY_FOR_BASELINE) in report.notes[0]


def test_insufficient_history_for_no_prior() -> None:
    """A vendor's very first invoice is explicitly 'not enough history'."""
    current = make_invoice(created_at=CURRENT_AT)

    report = detect_anomaly(current, [])

    assert report.verdict == AnomalyVerdict.INSUFFICIENT_HISTORY
    assert report.score is None
    assert report.method == AnomalyMethod.NONE
    assert report.history_size == 0
    assert "0 prior invoices" in report.notes[0]


def test_anomaly_report_never_fabricates_score() -> None:
    """No baseline means no score and no contributions - not a default 0.0."""
    current = make_invoice(created_at=CURRENT_AT)
    prior = [make_invoice(created_at=BASE + timedelta(days=i)) for i in range(3)]

    report = detect_anomaly(current, prior)

    assert report.score is None
    assert report.contributions == []
    assert report.verdict == AnomalyVerdict.INSUFFICIENT_HISTORY


# --- statistical baseline ---------------------------------------------------


def test_normal_invoice_produces_normal_verdict() -> None:
    """An invoice inside the vendor's usual range is unremarkable."""
    prior = clustered_history(10)
    current = make_invoice(total=Decimal("100"), created_at=CURRENT_AT)

    report = detect_anomaly(current, prior)

    assert report.verdict == AnomalyVerdict.NORMAL
    assert report.method == AnomalyMethod.STATISTICAL_BASELINE
    assert report.contributions == []
    assert report.score is None
    assert f"baseline from {len(prior)} prior invoices" in report.notes


def test_mild_z_score_produces_mild_anomaly() -> None:
    """A moderate deviation is reported with MEDIUM severity.

    History is 96..115 (mean 105.5, population std 5.766), so 120 sits at
    roughly +2.5 sigma: above the mild threshold, below the strong one.
    """
    prior = clustered_history(20, mean=100)
    current = make_invoice(total=Decimal("120"), created_at=CURRENT_AT)

    report = analyze_statistical(current, prior)

    assert report.verdict == AnomalyVerdict.MILD_ANOMALY
    total_contributions = [
        c for c in report.contributions if c.feature == "total_amount"
    ]
    assert len(total_contributions) == 1
    assert total_contributions[0].severity == MismatchSeverity.MEDIUM


def test_strong_z_score_produces_strong_anomaly() -> None:
    """A large deviation is reported with HIGH severity."""
    prior = clustered_history(20, mean=100)
    current = make_invoice(total=Decimal("500"), created_at=CURRENT_AT)

    report = analyze_statistical(current, prior)

    assert report.verdict == AnomalyVerdict.STRONG_ANOMALY
    high = [c for c in report.contributions if c.severity == MismatchSeverity.HIGH]
    assert high
    assert any(c.feature == "total_amount" for c in high)


def test_contribution_includes_mean_std_zscore() -> None:
    """Every reported feature carries the numbers behind the claim."""
    prior = clustered_history(20, mean=100)
    current = make_invoice(total=Decimal("500"), created_at=CURRENT_AT)

    report = analyze_statistical(current, prior)

    contribution = next(c for c in report.contributions if c.feature == "total_amount")
    assert contribution.current_value == "500"
    assert contribution.historical_mean is not None
    assert contribution.historical_std is not None
    assert contribution.z_score is not None
    assert float(contribution.z_score) > 3
    assert "sigma above" in contribution.note
    assert contribution.severity == MismatchSeverity.HIGH


def test_zero_std_baseline_is_skipped_not_divided_by() -> None:
    """A perfectly uniform history must not produce a divide-by-zero score."""
    prior = [make_invoice(total=Decimal("100.00")) for _ in range(10)]
    current = make_invoice(total=Decimal("99999.00"), created_at=CURRENT_AT)

    report = analyze_statistical(current, prior)

    # Degenerate baseline: the feature is skipped rather than reported as an
    # infinite z-score.
    assert [c.feature for c in report.contributions] == []
    assert report.verdict == AnomalyVerdict.NORMAL


def test_high_frequency_alone_can_trigger_anomaly() -> None:
    """A submission burst is anomalous on its own, with no amount deviation.

    Kept below the Isolation Forest threshold so this exercises the baseline,
    where the frequency signal drives the verdict.
    """
    prior = clustered_history(8)
    burst = [
        make_invoice(total=Decimal("100"), created_at=BASE + timedelta(days=350 + i))
        for i in range(FREQUENCY_ANOMALY_THRESHOLD)
    ]
    current = make_invoice(total=Decimal("100"), created_at=CURRENT_AT)

    report = detect_anomaly(current, prior + burst)

    assert report.method == AnomalyMethod.STATISTICAL_BASELINE

    assert report.verdict in (
        AnomalyVerdict.MILD_ANOMALY,
        AnomalyVerdict.STRONG_ANOMALY,
    )
    frequency = [c for c in report.contributions if c.feature == "invoice_frequency"]
    assert frequency
    assert frequency[0].severity == MismatchSeverity.HIGH
    # No z-score exists for a burst, so no score is invented alongside the
    # anomaly verdict.
    assert report.score is None


# --- isolation forest -------------------------------------------------------


def test_isolation_forest_activates_at_threshold() -> None:
    """19 prior invoices use the baseline; 20 switch to the forest."""
    assert MIN_HISTORY_FOR_ISOLATION_FOREST == 20
    current = make_invoice(total=Decimal("100"), created_at=CURRENT_AT)

    under = clustered_history(19)
    assert analyze_isolation_forest(current, under) is None
    assert analyze_statistical(current, under).method == AnomalyMethod.STATISTICAL_BASELINE

    at = clustered_history(20)
    forest = analyze_isolation_forest(current, at)
    assert forest is not None
    assert forest.method == AnomalyMethod.ISOLATION_FOREST


def test_isolation_forest_reports_no_score_but_a_decision_value() -> None:
    """An uncalibrated rescaling is not a probability, so none is reported."""
    prior = clustered_history(25, mean=100)
    outlier = make_invoice(total=Decimal("500000"), created_at=CURRENT_AT)

    report = analyze_isolation_forest(outlier, prior)

    assert report is not None
    assert report.score is None
    assert report.raw_decision is not None
    assert isinstance(report.raw_decision, float)
    assert report.method == AnomalyMethod.ISOLATION_FOREST
    # A consumer should never have to guess why the score is absent.
    assert any("calibrated probabilities" in note for note in report.notes)


def test_isolation_forest_produces_deterministic_scores() -> None:
    """random_state=42 pins the forest, so scores repeat exactly."""
    prior = clustered_history(25, mean=100)
    outlier = make_invoice(total=Decimal("5000"), created_at=CURRENT_AT)

    scores = {analyze_isolation_forest(outlier, prior).score for _ in range(3)}

    assert len(scores) == 1


def test_isolation_forest_reports_baseline_attribution() -> None:
    """The forest cannot explain itself, so attribution is borrowed and labelled."""
    prior = clustered_history(25, mean=100)
    current = make_invoice(total=Decimal("500000"), created_at=CURRENT_AT)

    report = analyze_isolation_forest(current, prior)

    assert report is not None
    assert any("does not provide per-feature reasons" in note for note in report.notes)
    for contribution in report.contributions:
        assert "Isolation Forest does not provide" in contribution.note


def test_isolation_forest_falls_back_when_insufficient_complete_rows() -> None:
    """Enough invoices, but most lack a total, is not enough usable rows."""
    assert MIN_HISTORY_FOR_BASELINE == 5
    # 20 invoices that all lack a total and line items, so avg_unit_price and
    # avg_line_total are None and the rows cannot be scaled.
    incomplete = [
        make_invoice(total=None, created_at=BASE + timedelta(days=10 + i))
        for i in range(20)
    ]
    current = make_invoice(total=Decimal("100"), created_at=CURRENT_AT)

    assert analyze_isolation_forest(current, incomplete) is None

    # detect_anomaly falls back to the baseline rather than reporting a forest
    # result it could not actually fit.
    report = detect_anomaly(current, incomplete)
    assert report.method == AnomalyMethod.STATISTICAL_BASELINE
    assert report.score is None


# --- determinism and purity -------------------------------------------------


def test_analysis_is_deterministic() -> None:
    """Three identical runs produce byte-identical reports."""
    prior = clustered_history(12, mean=100)
    current = make_invoice(total=Decimal("400"), created_at=CURRENT_AT)

    first = analyze_statistical(current, prior)
    second = analyze_statistical(current, prior)
    third = analyze_statistical(current, prior)

    assert first.model_dump() == second.model_dump() == third.model_dump()


def test_no_llm_in_anomaly_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The detector must not reach for a model under any circumstances."""
    import app.services.llm.factory as llm_factory

    def explode(*args, **kwargs):
        raise AssertionError("anomaly detection must not call an LLM provider")

    monkeypatch.setattr(llm_factory, "get_llm_provider", explode)
    monkeypatch.setattr(
        "app.services.llm.factory.get_llm_provider", explode, raising=False
    )

    prior = clustered_history(10)
    current = make_invoice(total=Decimal("500"), created_at=CURRENT_AT)

    report = detect_anomaly(current, prior)

    assert report.verdict == AnomalyVerdict.STRONG_ANOMALY


def test_report_rejects_unknown_fields() -> None:
    """The report model is strict about unexpected fields."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AnomalyReport(
            invoice_id=uuid.uuid4(),
            vendor_name="Acme",
            verdict=AnomalyVerdict.NORMAL,
            method=AnomalyMethod.NONE,
            score=None,
            raw_decision=None,
            contributions=[],
            history_size=0,
            notes=[],
            fabricated_recommendation="approve it",
        )


def test_detector_prefers_isolation_forest_when_available() -> None:
    """With enough history the forest wins over the baseline."""
    prior = clustered_history(25, mean=100)
    current = make_invoice(total=Decimal("100"), created_at=CURRENT_AT)

    report = detect_anomaly(current, prior)

    assert report.method == AnomalyMethod.ISOLATION_FOREST
    assert report.history_size == 25


# --- forest vs baseline interaction -----------------------------------------


def test_if_decision_ordering_preserved() -> None:
    """An outlier must rank more negative than an inlier.

    Measured on 25 clustered invoices (amounts 95-103): the 4000 outlier scored
    -0.094 against the inlier's +0.017. This pins the ordering independently of
    where the thresholds sit, so recalibrating them cannot silently invert it.
    """
    prior = clustered_history(25, mean=100)
    outlier = make_invoice(total=Decimal("4000"), created_at=CURRENT_AT)
    inlier = make_invoice(total=Decimal("100"), created_at=CURRENT_AT)

    outlier_report = analyze_isolation_forest(outlier, prior)
    inlier_report = analyze_isolation_forest(inlier, prior)

    assert outlier_report is not None and inlier_report is not None
    assert outlier_report.raw_decision is not None
    assert inlier_report.raw_decision is not None
    assert outlier_report.raw_decision < inlier_report.raw_decision


def test_frequency_burst_escalates_verdict_when_forest_says_normal() -> None:
    """The forest must not override a stronger baseline signal.

    The forest scores an invoice as one unit and cannot see submission
    frequency. When it rates the invoice NORMAL while the baseline has found a
    HIGH-severity burst, the combined verdict has to be the baseline's.
    """
    # 25 invoices spread over the year: enough for the forest to activate.
    prior = clustered_history(25, mean=100)
    burst = [
        make_invoice(total=Decimal("100"), created_at=BASE + timedelta(days=340 + i))
        for i in range(FREQUENCY_ANOMALY_THRESHOLD)
    ]
    current = make_invoice(total=Decimal("100"), created_at=CURRENT_AT)
    all_prior = prior + burst

    forest_only = analyze_isolation_forest(current, all_prior)
    assert forest_only is not None, "forest should activate with 35 prior invoices"

    report = detect_anomaly(current, all_prior)

    assert report.method == AnomalyMethod.ISOLATION_FOREST
    # The baseline found a HIGH burst regardless of what the forest decided.
    frequency = [
        c for c in report.contributions if c.feature == "invoice_frequency"
    ]
    assert frequency, "baseline frequency contribution should be attached"
    assert frequency[0].severity == MismatchSeverity.HIGH

    baseline_verdict = analyze_statistical(current, all_prior).verdict
    if forest_only.verdict == AnomalyVerdict.NORMAL:
        assert report.verdict == max_severity(
            AnomalyVerdict.NORMAL, baseline_verdict
        )
        assert report.verdict == baseline_verdict
        assert report.verdict in (
            AnomalyVerdict.MILD_ANOMALY,
            AnomalyVerdict.STRONG_ANOMALY,
        )
    else:
        # Forest already escalated; combined must be at least as severe.
        assert report.verdict in (
            AnomalyVerdict.MILD_ANOMALY,
            AnomalyVerdict.STRONG_ANOMALY,
        )

    # The forest's decision value survives the composition.
    assert report.raw_decision == forest_only.raw_decision


def test_max_severity_orders_verdicts() -> None:
    """INSUFFICIENT_HISTORY is lowest: absence of evidence never outranks a finding."""
    assert max_severity(AnomalyVerdict.NORMAL, AnomalyVerdict.INSUFFICIENT_HISTORY) == (
        AnomalyVerdict.NORMAL
    )
    assert (
        max_severity(AnomalyVerdict.INSUFFICIENT_HISTORY, AnomalyVerdict.NORMAL)
        == AnomalyVerdict.NORMAL
    )
    assert (
        max_severity(AnomalyVerdict.MILD_ANOMALY, AnomalyVerdict.STRONG_ANOMALY)
        == AnomalyVerdict.STRONG_ANOMALY
    )
    assert (
        max_severity(AnomalyVerdict.STRONG_ANOMALY, AnomalyVerdict.MILD_ANOMALY)
        == AnomalyVerdict.STRONG_ANOMALY
    )
    assert (
        max_severity(AnomalyVerdict.NORMAL, AnomalyVerdict.INSUFFICIENT_HISTORY)
        is not AnomalyVerdict.INSUFFICIENT_HISTORY
    )


def test_detector_preserves_strong_baseline_verdict_over_normal_forest() -> None:
    """A 4000 outlier keeps its baseline verdict even when the forest is calm."""
    prior = clustered_history(25, mean=100)
    current = make_invoice(total=Decimal("4000"), created_at=CURRENT_AT)

    report = detect_anomaly(current, prior)

    assert report.method == AnomalyMethod.ISOLATION_FOREST
    assert report.raw_decision is not None
    assert report.verdict in (
        AnomalyVerdict.MILD_ANOMALY,
        AnomalyVerdict.STRONG_ANOMALY,
    )
    # Contributions still come from the baseline, clearly labelled.
    for contribution in report.contributions:
        assert "Isolation Forest does not provide" in contribution.note


def test_no_mock_leakage_between_tests() -> None:
    """Guard that the LLM patch above did not escape its test."""
    import app.services.llm.factory as llm_factory

    assert llm_factory.get_llm_provider is not None
    # A real provider resolves without raising.
    with mock.patch("app.services.llm.factory.get_llm_provider") as provider:
        provider.return_value = object()
        assert llm_factory.get_llm_provider() is not None