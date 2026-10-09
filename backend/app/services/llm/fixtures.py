"""Ground-truth fixture registry for the mock LLM provider.

Each entry pins the SHA-256 of a generated PDF to its expected extraction
(``ground_truth``) and per-field confidence. Confidence convention: fields
present in the ground truth carry >= 0.9 (the fixture is authoritative);
fields that are null in the ground truth carry 0.0 - "no match" markers, not
match confidences. See tests/test_llm_mock.py for the invariant that
every regex-derived confidence stays strictly below every fixture match
confidence.
"""

from pydantic import BaseModel

__all__ = ["FIXTURES", "FixtureEntry", "find_fixture_by_sha256"]


class FixtureEntry(BaseModel):
    """One known PDF fixture and its expected extraction."""

    sha256: str  # SHA-256 of the fixture PDF bytes
    fixture_id: str  # short human name
    description: str  # one line
    scenario: str  # "clean" | "missing_fields" | "multi_line" | ...
    ground_truth: dict  # expected extraction (serialized InvoiceExtraction)
    confidence: dict[str, float]  # per-field confidence


FIXTURES: list[FixtureEntry] = [
    FixtureEntry(
        sha256="f1edd89c32a8c240d730a918f30eb60e15f47765a77000aed25255bd30683368",
        fixture_id="acme-simple",
        description="Simple invoice with one line item, USD, no PO",
        scenario="clean",
        ground_truth={
            "vendor_name": "Acme Corporation",
            "invoice_number": "INV-2024-001",
            "invoice_date": "2024-03-15",
            "po_number": None,
            "tax_id": "12-3456789",
            "currency": "USD",
            "line_items": [
                {
                    "description": "Widget",
                    "quantity": 1,
                    "unit_price": "100.00",
                    "amount": "100.00",
                },
            ],
            "subtotal": "100.00",
            "tax_amount": "8.50",
            "total_amount": "108.50",
        },
        confidence={
            "vendor_name": 0.95,
            "invoice_number": 0.98,
            "invoice_date": 0.95,
            "po_number": 0.0,  # absent from this invoice
            "tax_id": 0.95,
            "currency": 0.99,
            "line_items": 0.92,
            "subtotal": 0.97,
            "tax_amount": 0.97,
            "total_amount": 0.99,
        },
    ),
    FixtureEntry(
        sha256="edb65e6ff77725f062ab3c59f6017247eb4439567ef1afffaeac984266a41a22",
        fixture_id="globex-multiline",
        description="Three line items, EUR, one quantity above one, everything present",
        scenario="multi_line",
        ground_truth={
            "vendor_name": "Globex Europe BV",
            "invoice_number": "INV-EU-7788",
            "invoice_date": "2024-03-15",
            "po_number": "PO-EU-4411",
            "tax_id": "DE123456789",
            "currency": "EUR",
            "line_items": [
                {
                    "description": "Bolt M10",
                    "quantity": 25,
                    "unit_price": "0.40",
                    "amount": "10.00",
                },
                {
                    "description": "Nut M10",
                    "quantity": 25,
                    "unit_price": "0.20",
                    "amount": "5.00",
                },
                {
                    "description": "Washer M10",
                    "quantity": 50,
                    "unit_price": "0.05",
                    "amount": "2.50",
                },
            ],
            "subtotal": "17.50",
            "tax_amount": "3.50",
            "total_amount": "21.00",
        },
        confidence={
            "vendor_name": 0.95,
            "invoice_number": 0.97,
            "invoice_date": 0.95,
            "po_number": 0.95,
            "tax_id": 0.95,
            "currency": 0.99,
            "line_items": 0.94,
            "subtotal": 0.97,
            "tax_amount": 0.97,
            "total_amount": 0.98,
        },
    ),
    FixtureEntry(
        sha256="8a4c3e73c3a72653c4643a7d372bc3f3d78b1d964d9b1e8b800a1d80960733b2",
        fixture_id="initech-missing-fields",
        description="No PO number, no tax_id, one line item, USD",
        scenario="missing_fields",
        ground_truth={
            "vendor_name": "Initech Inc",
            "invoice_number": "INV-900",
            "invoice_date": "2024-06-30",
            "po_number": None,
            "tax_id": None,
            "currency": "USD",
            "line_items": [
                {
                    "description": "Consulting Services",
                    "quantity": 1,
                    "unit_price": "2500.00",
                    "amount": "2500.00",
                },
            ],
            "subtotal": "2500.00",
            "tax_amount": "200.00",
            "total_amount": "2700.00",
        },
        confidence={
            "vendor_name": 0.95,
            "invoice_number": 0.97,
            "invoice_date": 0.95,
            "po_number": 0.0,  # missing field: null in ground truth
            "tax_id": 0.0,  # missing field: null in ground truth
            "currency": 0.99,
            "line_items": 0.93,
            "subtotal": 0.97,
            "tax_amount": 0.97,
            "total_amount": 0.98,
        },
    ),
]


def find_fixture_by_sha256(sha256: str) -> FixtureEntry | None:
    """Return the fixture whose PDF hash is exactly ``sha256``, or None.

    Exact match only: no prefix or fuzzy matching, so a hash hint can never
    resolve to the wrong ground truth.
    """

    for entry in FIXTURES:
        if entry.sha256 == sha256:
            return entry
    return None
