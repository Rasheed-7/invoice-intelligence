"""AI extraction schemas (LLM provider output).

These schemas model the *raw structured output* of an LLM provider
(formalizing the dict shape in ``app.services.llm.fixtures``), whereas
``app.schemas.invoice`` models the *HTTP API responses* served to clients
(flattened, ORM-backed list/detail views). Keep them separate: this module
is a schema concern for AI extraction, not for HTTP serialization.

Confidence map key format (``meta.confidence``):
dot notation with list indexes in ``[...]``, e.g. ``"vendor.name"``,
``"invoice.number"``, ``"totals.total"``, ``"items[0].description"``.

``tax_rate`` is a fraction, not a percentage
(e.g. ``Decimal("0.085")`` for 8.5%).
"""

import datetime
import re
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from app.services.llm.provider import LLMResponse

__all__ = [
    "Vendor",
    "InvoiceHeader",
    "PurchaseOrderReference",
    "InvoiceItem",
    "InvoiceTotals",
    "ExtractionMeta",
    "InvoiceExtraction",
    "VALID_FIELD_PATHS",
    "extraction_from_llm_response",
    "validate_confidence_keys",
]


class Vendor(BaseModel):
    """Vendor/supplier information extracted from an invoice."""

    name: str | None = None
    tax_id: str | None = None

    model_config = ConfigDict(extra="forbid")


class InvoiceHeader(BaseModel):
    """Invoice header fields."""

    number: str | None = None
    date: datetime.date | None = None
    due_date: datetime.date | None = None
    currency: str | None = None  # 3-letter ISO 4217 code when present, else None.

    model_config = ConfigDict(extra="forbid")


class PurchaseOrderReference(BaseModel):
    """Purchase order reference if present on the invoice."""

    number: str | None = None

    model_config = ConfigDict(extra="forbid")


class InvoiceItem(BaseModel):
    """Single line item on an invoice."""

    description: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    line_total: Decimal | None = None
    # As a fraction (e.g. Decimal("0.085") for 8.5%), not a percentage.
    tax_rate: Decimal | None = None

    model_config = ConfigDict(extra="forbid")


class InvoiceTotals(BaseModel):
    """Invoice monetary totals."""

    subtotal: Decimal | None = None
    tax: Decimal | None = None
    total: Decimal | None = None

    model_config = ConfigDict(extra="forbid")


class ExtractionMeta(BaseModel):
    """Metadata about the extraction process."""

    source: Literal["fixture", "regex", "mock", "local", "commercial"]
    provider_name: str
    model_name: str
    confidence: dict[str, float]  # flat map of field-path -> confidence
    extracted_at: datetime.datetime  # timezone-aware UTC

    model_config = ConfigDict(extra="forbid")


class InvoiceExtraction(BaseModel):
    """Complete structured extraction result for an invoice."""

    vendor: Vendor
    invoice: InvoiceHeader
    purchase_order: PurchaseOrderReference
    items: list[InvoiceItem]
    totals: InvoiceTotals
    meta: ExtractionMeta

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Confidence-key validation (schema concern, used by 14-3 to reject
# malformed provider output).
# ---------------------------------------------------------------------------

#: Schema-defined field paths that never carry a list index.
VALID_FIELD_PATHS: frozenset[str] = frozenset(
    {
        "vendor.name",
        "vendor.tax_id",
        "invoice.number",
        "invoice.date",
        "invoice.due_date",
        "invoice.currency",
        "purchase_order.number",
        "totals.subtotal",
        "totals.tax",
        "totals.total",
    }
)

#: Allowed leaf fields for ``items[N].<field>`` paths.
_ITEM_FIELDS: frozenset[str] = frozenset(
    {
        "description",
        "quantity",
        "unit_price",
        "line_total",
        "tax_rate",
    }
)

_ITEM_PATH_RE = re.compile(r"^items\[(\d+)\]\.([A-Za-z_]+)$")


def validate_confidence_keys(extraction: InvoiceExtraction) -> list[str]:
    """Return confidence keys with no corresponding field path in the extraction.

    A key is valid when it is in :data:`VALID_FIELD_PATHS`, or when it matches
    ``items[N].<field>`` with ``<field>`` in the item schema and ``N`` a valid
    index (``0 <= N < len(extraction.items)``). Returns ``[]`` if all keys are
    valid. Used to catch providers emitting confidence for nonexistent fields.
    """
    invalid_keys: list[str] = []
    item_count = len(extraction.items)
    for key in extraction.meta.confidence:
        if key in VALID_FIELD_PATHS:
            continue
        match = _ITEM_PATH_RE.match(key)
        if match is None:
            invalid_keys.append(key)
            continue
        index = int(match.group(1))
        field = match.group(2)
        if field not in _ITEM_FIELDS or not 0 <= index < item_count:
            invalid_keys.append(key)
    return invalid_keys


# ---------------------------------------------------------------------------
# LLMResponse adapter.
# ---------------------------------------------------------------------------

#: This adapter is the ONLY place where the shape of ``LLMResponse.data`` is
#: interpreted. Everything downstream uses :class:`InvoiceExtraction`.
#:
#: The mock provider emits FLAT keys (``vendor_name``, ``invoice_number``,
#: ``line_items`` with ``amount`` leafs, ...). The extraction schema is
#: NESTED (``vendor``/``invoice``/``purchase_order``/``items``/``totals``).
#: Both shapes are accepted: nested keys win when both are present.
_FLAT_TO_VENDOR = {"vendor_name": "name", "tax_id": "tax_id"}
_FLAT_TO_INVOICE = {
    "invoice_number": "number",
    "invoice_date": "date",
    "due_date": "due_date",
    "currency": "currency",
}
_FLAT_TO_PO = {"po_number": "number"}
_FLAT_TO_TOTALS = {
    "subtotal": "subtotal",
    "tax_amount": "tax",
    "total_amount": "total",
}
_FLAT_ITEM_FIELDS = {
    "description": "description",
    "quantity": "quantity",
    "unit_price": "unit_price",
    "amount": "line_total",
    "line_total": "line_total",
    "tax_rate": "tax_rate",
}
_FLAT_ITEM_CONFIDENCE = "line_items"

#: Flat confidence keys emitted by the mock, mapped to schema paths.
_FLAT_CONFIDENCE_MAP = {
    "vendor_name": "vendor.name",
    "tax_id": "vendor.tax_id",
    "invoice_number": "invoice.number",
    "invoice_date": "invoice.date",
    "due_date": "invoice.due_date",
    "currency": "invoice.currency",
    "po_number": "purchase_order.number",
    "subtotal": "totals.subtotal",
    "tax_amount": "totals.tax",
    "total_amount": "totals.total",
}

#: Top-level keys the adapter understands in either shape. Anything else is
#: a provider bug and must raise via ``extra="forbid"``.
_KNOWN_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "vendor",
        "invoice",
        "purchase_order",
        "items",
        "totals",
        "vendor_name",
        "tax_id",
        "invoice_number",
        "invoice_date",
        "due_date",
        "currency",
        "po_number",
        "line_items",
        "subtotal",
        "tax_amount",
        "total_amount",
    }
)


def _pick(mapping: dict, nested: dict | None, flat: dict) -> dict:
    """Merge one section from nested dict plus flat aliases.

    Nested keys pass through untouched (nested wins; unknown nested keys
    reach Pydantic and raise via ``extra="forbid"``). Flat aliases only
    fill in keys the nested dict does not already provide.
    """
    result: dict = {}
    if isinstance(nested, dict):
        result.update(nested)
    for old, new in mapping.items():
        if old in flat and new not in result:
            result[new] = flat[old]
    return result


def _normalize_items(data: dict) -> list[dict]:
    raw_items = data.get("items")
    if raw_items is None:
        raw_items = data.get("line_items", [])
    if raw_items is None:
        return []
    normalized: list[dict] = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            normalized.append(entry)
            continue
        merged: dict = dict(entry)
        for old, new in _FLAT_ITEM_FIELDS.items():
            if old in entry and new not in merged:
                merged[new] = entry[old]
        unknown = set(entry) - set(_FLAT_ITEM_FIELDS)
        if unknown:
            raise ValueError(f"unknown item field(s): {sorted(unknown)}")
        kept = {k: v for k, v in merged.items() if k in _FLAT_ITEM_FIELDS.values()}
        normalized.append(kept)
    return normalized


def _normalize_confidence(confidence: dict[str, float], item_count: int) -> dict[str, float]:
    """Map flat mock confidence keys onto schema field paths."""
    normalized: dict[str, float] = {}
    for key, value in confidence.items():
        if key in _FLAT_CONFIDENCE_MAP:
            normalized[_FLAT_CONFIDENCE_MAP[key]] = value
        elif key == _FLAT_ITEM_CONFIDENCE:
            for index in range(item_count):
                for field in sorted(_ITEM_FIELDS):
                    normalized[f"items[{index}].{field}"] = value
        else:
            normalized[key] = value
    return normalized


def extraction_from_llm_response(response: LLMResponse) -> InvoiceExtraction:
    """Build an :class:`InvoiceExtraction` from an :class:`LLMResponse`.

    This adapter is the ONLY place where the shape of ``LLMResponse.data``
    is interpreted. Everything downstream uses :class:`InvoiceExtraction`.

    Unknown top-level or item keys raise ``ValidationError``
    (``extra="forbid"``): a provider emitting unknown fields is a bug.
    Missing keys default to ``None`` (``items`` defaults to ``[]``), so an
    empty ``data`` dict yields an all-null extraction. ``meta`` is rebuilt
    from the response envelope with ``extracted_at`` stamped as now (UTC).
    """
    from pydantic import ValidationError as PydanticValidationError

    data = dict(response.data or {})
    unknown_keys = set(data) - _KNOWN_TOP_LEVEL_KEYS
    if unknown_keys:
        raise PydanticValidationError.from_exception_data(
            title="InvoiceExtraction",
            line_errors=[
                {
                    "type": "extra_forbidden",
                    "loc": ("data", unknown),
                    "msg": "Extra inputs are not permitted",
                    "input": data[unknown],
                }
                for unknown in sorted(unknown_keys)
            ],
        )

    vendor = _pick(_FLAT_TO_VENDOR, data.get("vendor"), data)
    invoice = _pick(_FLAT_TO_INVOICE, data.get("invoice"), data)
    purchase_order = _pick(_FLAT_TO_PO, data.get("purchase_order"), data)
    totals = _pick(_FLAT_TO_TOTALS, data.get("totals"), data)
    try:
        items = _normalize_items(data)
    except ValueError as exc:
        raise PydanticValidationError.from_exception_data(
            title="InvoiceExtraction",
            line_errors=[
                {
                    "type": "extra_forbidden",
                    "loc": ("data",),
                    "msg": str(exc),
                    "input": data,
                }
            ],
        ) from exc

    payload = {
        "vendor": vendor,
        "invoice": invoice,
        "purchase_order": purchase_order,
        "items": items,
        "totals": totals,
        "meta": {
            "source": response.source,
            "provider_name": response.provider_name,
            "model_name": response.model_name,
            "confidence": _normalize_confidence(dict(response.confidence or {}), len(items)),
            "extracted_at": datetime.datetime.now(datetime.UTC),
        },
    }
    return InvoiceExtraction.model_validate(payload)