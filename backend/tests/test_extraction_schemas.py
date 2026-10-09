"""Tests for AI extraction schemas and the LLMResponse adapter."""

import datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.extraction import (
    ExtractionMeta,
    InvoiceExtraction,
    InvoiceHeader,
    InvoiceItem,
    InvoiceTotals,
    Vendor,
    extraction_from_llm_response,
    validate_confidence_keys,
)
from app.services.llm.fixtures import FIXTURES
from app.services.llm.mock import MockLLMProvider
from app.services.llm.provider import LLMResponse


def _make_extraction(**confidence: float) -> InvoiceExtraction:
    return InvoiceExtraction(
        vendor={"name": "Acme Corporation", "tax_id": "12-3456789"},
        invoice={"number": "INV-2024-001", "date": "2024-03-15", "currency": "USD"},
        purchase_order={},
        items=[
            {"description": "Widget", "quantity": "1", "unit_price": "100.00"},
            {"description": "Gadget", "quantity": "2", "unit_price": "5.00"},
        ],
        totals={"subtotal": "110.00", "tax": "8.50", "total": "118.50"},
        meta={
            "source": "mock",
            "provider_name": "mock-hybrid",
            "model_name": "mock-hybrid-v1",
            "confidence": dict(confidence),
            "extracted_at": datetime.datetime.now(datetime.UTC),
        },
    )


def test_vendor_accepts_null_fields() -> None:
    assert Vendor().name is None
    assert Vendor(name=None, tax_id=None).tax_id is None


def test_vendor_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        Vendor(name="X", unknown="Y")  # type: ignore[call-arg]


def test_invoice_header_parses_dates() -> None:
    header = InvoiceHeader(date="2024-03-15")
    assert header.date == datetime.date(2024, 3, 15)
    with pytest.raises(ValidationError):
        InvoiceHeader(date="not-a-date")


def test_invoice_item_quantity_and_price_are_decimal() -> None:
    item = InvoiceItem(quantity="25", unit_price="0.40")
    assert item.quantity == Decimal("25")
    assert item.unit_price == Decimal("0.40")
    assert not isinstance(item.quantity, float)


def test_money_fields_are_decimal_not_float() -> None:
    totals = InvoiceTotals(subtotal="108.50", tax="8.50", total="108.50")
    item = InvoiceItem(unit_price="0.40", line_total="10.00")
    assert isinstance(totals.subtotal, Decimal)
    assert isinstance(item.unit_price, Decimal)
    assert isinstance(item.line_total, Decimal)




def test_confidence_keys_validation_accepts_valid_paths() -> None:
    extraction = _make_extraction(**{"vendor.name": 0.9, "totals.total": 0.85})
    assert validate_confidence_keys(extraction) == []


def test_confidence_keys_validation_rejects_unknown_paths() -> None:
    extraction = _make_extraction(**{"vendor.phone": 0.9})
    assert validate_confidence_keys(extraction) == ["vendor.phone"]


def test_confidence_keys_validation_handles_items() -> None:
    extraction = _make_extraction(
        **{"items[0].description": 0.9, "items[1].unit_price": 0.8, "items[2].description": 0.7}
    )
    assert validate_confidence_keys(extraction) == ["items[2].description"]


def test_extraction_from_llm_response_fixture() -> None:
    entry = next(e for e in FIXTURES if e.fixture_id == "acme-simple")
    response = LLMResponse(
        data=dict(entry.ground_truth),
        confidence=dict(entry.confidence),
        source="fixture",
        provider_name="mock-hybrid",
        model_name="mock-hybrid-v1",
        raw_response="{}",
        duration_ms=1,
    )
    extraction = extraction_from_llm_response(response)
    assert extraction.vendor.name == "Acme Corporation"
    assert extraction.vendor.tax_id == "12-3456789"
    assert extraction.invoice.number == "INV-2024-001"
    assert extraction.invoice.date == datetime.date(2024, 3, 15)
    assert extraction.invoice.currency == "USD"
    assert extraction.purchase_order.number is None
    assert len(extraction.items) == 1
    assert extraction.items[0].description == "Widget"
    assert extraction.items[0].quantity == Decimal("1")
    assert extraction.items[0].unit_price == Decimal("100.00")
    assert extraction.items[0].line_total == Decimal("100.00")
    assert extraction.totals.subtotal == Decimal("100.00")
    assert extraction.totals.tax == Decimal("8.50")
    assert extraction.totals.total == Decimal("108.50")
    assert extraction.meta.source == "fixture"
    assert extraction.meta.provider_name == "mock-hybrid"
    assert validate_confidence_keys(extraction) == []


def test_extraction_from_llm_response_empty_data() -> None:
    response = LLMResponse(
        data={},
        confidence={},
        source="regex",
        provider_name="mock-hybrid",
        model_name="mock-hybrid-v1",
        raw_response="",
        duration_ms=0,
    )
    extraction = extraction_from_llm_response(response)
    assert extraction.vendor.name is None
    assert extraction.invoice.number is None
    assert extraction.purchase_order.number is None
    assert extraction.items == []
    assert extraction.totals.total is None


def test_extraction_from_llm_response_rejects_unknown_keys() -> None:
    response = LLMResponse(
        data={"vendor": {"name": "X", "phone": "123"}},
        confidence={},
        source="regex",
        provider_name="mock-hybrid",
        model_name="mock-hybrid-v1",
        raw_response="",
        duration_ms=0,
    )
    with pytest.raises(ValidationError):
        extraction_from_llm_response(response)


def test_extraction_round_trips_through_json() -> None:
    extraction = _make_extraction(**{"vendor.name": 0.9})
    dumped = extraction.model_dump(mode="json")
    roundtrip = InvoiceExtraction.model_validate(dumped)
    assert roundtrip == extraction
    assert isinstance(roundtrip.totals.total, Decimal)
    assert isinstance(roundtrip.invoice.date, datetime.date)


def test_confidence_and_source_strict_types() -> None:
    with pytest.raises(ValidationError):
        ExtractionMeta(
            source="unknown",  # type: ignore[arg-type]
            provider_name="p",
            model_name="m",
            confidence={},
            extracted_at=datetime.datetime.now(datetime.UTC),
        )
    with pytest.raises(ValidationError):
        ExtractionMeta(
            source="mock",
            provider_name="p",
            model_name="m",
            confidence={"vendor.name": "high"},  # type: ignore[dict-item]
            extracted_at=datetime.datetime.now(datetime.UTC),
        )
    # int coerces to float: accepted.
    meta = ExtractionMeta(
        source="mock",
        provider_name="p",
        model_name="m",
        confidence={"vendor.name": 1},  # type: ignore[dict-item]
        extracted_at=datetime.datetime.now(datetime.UTC),
    )
    assert meta.confidence["vendor.name"] == 1.0


def test_extraction_from_llm_response_matches_mock_fixture_roundtrip() -> None:
    provider = MockLLMProvider()
    entry = FIXTURES[0]
    response = provider.extract_structured(
        prompt="x", document_text="", document_sha256=entry.sha256
    )
    extraction = extraction_from_llm_response(response)
    assert validate_confidence_keys(extraction) == []
    assert extraction.vendor.name == "Acme Corporation"
    assert extraction.totals.total == Decimal("108.50")


def test_mock_and_schema_shapes_are_reconcilable() -> None:
    """Pins the contract between MockLLMProvider's output shape and
    InvoiceExtraction's expected shape, via the adapter.

    If the mock starts emitting different keys, or the schema adds required
    fields the mock can't fill, this test fails and points at the exact
    mismatch — rather than failing later inside the /analyze pipeline.
    """
    from decimal import Decimal

    from app.services.llm.fixtures import FIXTURES
    from app.services.llm.mock import MockLLMProvider

    provider = MockLLMProvider()

    for entry in FIXTURES:
        response = provider.extract_structured(
            prompt="x",
            document_text="",
            document_sha256=entry.sha256,
        )
        # Must not raise
        extraction = extraction_from_llm_response(response)

        # Must produce a valid extraction carrying the fixture's identity
        assert extraction.meta.source == "fixture", (
            f"{entry.fixture_id}: expected source=fixture, got {extraction.meta.source}"
        )
        assert extraction.meta.provider_name == "mock-hybrid"
        assert extraction.vendor.name == entry.ground_truth.get("vendor_name"), (
            f"{entry.fixture_id}: vendor name mismatch"
        )

        # Total must round-trip through Decimal, matching the fixture's ground truth
        expected_total = entry.ground_truth.get("total_amount")
        if expected_total is None:
            assert extraction.totals.total is None
        else:
            assert extraction.totals.total == Decimal(expected_total), (
                f"{entry.fixture_id}: total mismatch"
            )

        # Every confidence key must correspond to a real schema path
        assert validate_confidence_keys(extraction) == [], (
            f"{entry.fixture_id}: orphan confidence keys: {validate_confidence_keys(extraction)}"
        )
