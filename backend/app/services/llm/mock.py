"""Deterministic mock LLM provider: fixture lookups plus regex fallback.

Never fabricates: every field is either what the document text literally says
(matched by a documented pattern) or null. There is no default vendor name, no
invented invoice number, no default currency and no zero amounts - if the text
does not contain a value, the field is null and its confidence is 0.0.

Prompt-injection boundary: ``prompt`` and ``document_text`` are read
independently and never concatenated. Document text is data, never
instructions; this module demonstrates the boundary by simply never combining
the two. Any combining logic needed later belongs to a future real provider
and must be audited separately.
"""

import json
import re
import time
from copy import deepcopy
from decimal import Decimal, InvalidOperation

from app.services.llm.fixtures import FIXTURES, FixtureEntry
from app.services.llm.provider import LLMProviderError, LLMResponse

__all__ = ["MockLLMProvider"]

_PROVIDER_NAME = "mock-hybrid"
_MODEL_NAME = "mock-hybrid-v1"

# Confidence bands for regex-derived fields. Rationale: a label in the text
# ("Invoice #: ...") is strong evidence, a distinctive-but-unlabelled pattern
# (GSTIN/EIN) is slightly weaker, pattern-only matches (a bare date, a
# currency symbol) are weaker still, and the vendor heuristic is positional
# guesswork. INVARIANT: every value here must stay strictly below the lowest
# fixture match confidence (0.9) so a fixture hit always outranks pattern
# matching - tests/test_llm_provider.py asserts this.
CONF_EXPLICIT = 0.85  # the line literally labels the value
CONF_PATTERN = 0.7  # distinctive pattern, no label (GSTIN / EIN)
CONF_LINE_ITEM = 0.6  # a row with the right numeric shape was detected
CONF_AMBIGUOUS = 0.55  # matches by pattern but easily confused
CONF_VENDOR_HEURISTIC = 0.5  # positional heuristic only (first-line rule)
CONF_MISSING = 0.0  # no match at all

# Canonical field order for every response this provider produces.
FIELD_ORDER = (
    "vendor_name",
    "invoice_number",
    "invoice_date",
    "po_number",
    "tax_id",
    "currency",
    "line_items",
    "subtotal",
    "tax_amount",
    "total_amount",
)

# --- deterministic patterns -------------------------------------------------

_GSTIN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z]{5}\d{4}[A-Z]\d[Z][A-Z\d]\b")
_EIN_RE = re.compile(r"\b\d{2}-\d{7}\b")
_TAX_ID_LABEL_RE = re.compile(r"^(?:Tax ID|GSTIN):\s*(\S.*?)\s*$", re.IGNORECASE)
# An explicit vendor label beats the positional heuristic below: the text
# literally says who the vendor is, so it scores as an explicit match.
_VENDOR_LABEL_RE = re.compile(r"^(?:Vendor(?: Name)?|Supplier):\s*(\S.*?)\s*$", re.IGNORECASE)
# Invoice/PO rules carry two guards beyond the base pattern so a bare keyword
# can never fabricate a value: \b stops the "Inv"-alternative from eating the
# tail of the keyword itself ("Invoice" -> "oice"), and the digit lookahead
# stops a following word from being captured ("Invoice Date" -> "Date"). An
# invoice number that contains no digit is not invented - see the never-
# fabricate invariant in the module docstring.
_INVOICE_RE = re.compile(
    r"(?:Invoice|Inv)\b[\s#:]*((?=[A-Z0-9\-/]*\d)[A-Z0-9][A-Z0-9\-/]*)",
    re.IGNORECASE,
)
_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_US_DATE_RE = re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b")
_PO_RE = re.compile(
    r"(?:P\.?O\.?|Purchase Order)[\s#:]*((?=[A-Z0-9\-/]*\d)[A-Z0-9][A-Z0-9\-/]*)"
)
# Codes are matched case-sensitively so words like "Europe" never count.
_CURRENCY_CODE_RE = re.compile(r"\b(USD|EUR|GBP|INR|JPY|CAD|AUD)\b")
# Symbol -> ISO code. "$" maps to USD: disambiguating dollar variants would
# require context the mock deliberately does not invent.
_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "¥": "JPY"}
_SUBTOTAL_RE = re.compile(r"(?:Subtotal|Sub[- ]?total)[:\s]+([0-9.,]+)", re.IGNORECASE)
# (?! ID) guard: "Tax ID: 12-3456789" must never be read as a tax amount.
_TAX_AMOUNT_RE = re.compile(r"(?:Tax(?! ID)|VAT|GST)[:\s]+([0-9.,]+)", re.IGNORECASE)
# (?<!Sub) guard: the word "Subtotal" must never satisfy the Total rule.
_TOTAL_RE = re.compile(r"(?<!Sub)(?:Total|Amount Due)[:\s]+([0-9.,]+)", re.IGNORECASE)
_NUMERIC_TOKEN_RE = re.compile(r"^[$€£₹¥]?\d[\d,]*(?:\.\d+)?$")


def _elapsed_ms(start: float) -> int:
    """Milliseconds since ``start`` (near-zero for the mock)."""

    return int((time.perf_counter() - start) * 1000)


def _strip_symbol(token: str) -> str:
    return token.strip().lstrip("$€£₹¥")


def _money_value(raw: str) -> str | None:
    """Parse a numeric token as a money string, or None if it is not one.

    Thousands separators are dropped; the digits the text actually contained
    are kept verbatim - no rounding, no padding, no invented precision.
    """

    try:
        return str(Decimal(_strip_symbol(raw).replace(",", "")))
    except InvalidOperation:
        return None


def _quantity_value(raw: str) -> int | float | None:
    """Parse a quantity token as int when integral, else float, else None."""

    try:
        value = Decimal(_strip_symbol(raw).replace(",", ""))
    except InvalidOperation:
        return None
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _extract_vendor(lines: list[str]) -> tuple[str | None, float]:
    """Vendor from an explicit label, else the positional heuristic.

    An explicit "Vendor:"/"Supplier:" line is strong evidence (0.85). The
    fallback is the first line within the first 5 lines that is followed
    within 2 lines by an invoice-number pattern - positional guesswork only,
    hence 0.5.
    """

    for line in lines:
        match = _VENDOR_LABEL_RE.match(line.strip())
        if match:
            return match.group(1), CONF_EXPLICIT
    for i in range(min(5, len(lines))):
        candidate = lines[i].strip()
        if not candidate:
            continue
        for j in range(i + 1, min(i + 3, len(lines))):
            if _INVOICE_RE.search(lines[j]):
                return candidate, CONF_VENDOR_HEURISTIC
    return None, CONF_MISSING


def _extract_tax_id(text: str, lines: list[str]) -> tuple[str | None, float]:
    for line in lines:
        match = _TAX_ID_LABEL_RE.match(line.strip())
        if match:
            return match.group(1), CONF_EXPLICIT
    match = _GSTIN_RE.search(text)
    if match:
        return match.group(0), CONF_PATTERN
    match = _EIN_RE.search(text)
    if match:
        return match.group(0), CONF_PATTERN
    return None, CONF_MISSING


def _date_label_confidence(text: str, raw_match: str) -> float:
    for line in text.splitlines():
        if raw_match in line:
            return CONF_EXPLICIT if "date" in line.lower() else CONF_AMBIGUOUS
    return CONF_AMBIGUOUS


def _extract_line_items(lines: list[str]) -> tuple[list[dict], float]:
    """Rows shaped like ``description qty unit_price amount``.

    A row qualifies only with at least two whitespace-separated numeric
    columns after a description. With exactly two numeric columns the roles
    of unit price and amount are ambiguous, so amount stays null rather than
    guessed (never fabricate).
    """

    items: list[dict] = []
    for line in lines:
        tokens = line.split()
        if len(tokens) < 3:
            continue
        numeric_idx = [i for i, token in enumerate(tokens) if _NUMERIC_TOKEN_RE.match(token)]
        if len(numeric_idx) < 2:
            continue
        description_tokens = tokens[: numeric_idx[0]]
        if not description_tokens:
            continue
        quantity = _quantity_value(tokens[numeric_idx[0]])
        if quantity is None:
            continue
        unit_price = _money_value(tokens[numeric_idx[1]])
        amount = _money_value(tokens[numeric_idx[2]]) if len(numeric_idx) >= 3 else None
        items.append(
            {
                "description": " ".join(description_tokens),
                "quantity": quantity,
                "unit_price": unit_price,
                "amount": amount,
            }
        )
    if items:
        return items, CONF_LINE_ITEM
    return [], CONF_MISSING


def _regex_extract(text: str) -> tuple[dict, dict]:
    """Deterministically parse ``text``. Returns ``(data, confidence)``.

    Only documented patterns run; anything absent comes back null with a
    0.0 confidence.
    """

    lines = text.splitlines()

    vendor_name, vendor_conf = _extract_vendor(lines)

    invoice_match = _INVOICE_RE.search(text)
    invoice_number = invoice_match.group(1) if invoice_match else None

    iso_match = _ISO_DATE_RE.search(text)
    us_match = None if iso_match else _US_DATE_RE.search(text)
    invoice_date: str | None = None
    date_raw: str | None = None
    if iso_match:
        invoice_date = iso_match.group(1)
        date_raw = iso_match.group(0)
    elif us_match:
        invoice_date = f"{us_match.group(3)}-{us_match.group(1)}-{us_match.group(2)}"
        date_raw = us_match.group(0)
    date_conf = CONF_MISSING if invoice_date is None else _date_label_confidence(text, date_raw)

    po_match = _PO_RE.search(text)
    po_number = po_match.group(1) if po_match else None

    tax_id, tax_id_conf = _extract_tax_id(text, lines)

    currency: str | None = None
    currency_conf = CONF_MISSING
    code_match = _CURRENCY_CODE_RE.search(text)
    if code_match:
        currency = code_match.group(1)
        currency_conf = CONF_EXPLICIT
    else:
        for symbol, iso_code in _CURRENCY_SYMBOLS.items():
            if symbol in text:
                currency = iso_code
                currency_conf = CONF_AMBIGUOUS
                break

    line_items, line_items_conf = _extract_line_items(lines)

    subtotal_match = _SUBTOTAL_RE.search(text)
    subtotal = _money_value(subtotal_match.group(1)) if subtotal_match else None
    tax_amount_match = _TAX_AMOUNT_RE.search(text)
    tax_amount = _money_value(tax_amount_match.group(1)) if tax_amount_match else None
    total_match = _TOTAL_RE.search(text)
    total_amount = _money_value(total_match.group(1)) if total_match else None

    values: dict = {
        "vendor_name": vendor_name,
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "po_number": po_number,
        "tax_id": tax_id,
        "currency": currency,
        "line_items": line_items,
        "subtotal": subtotal,
        "tax_amount": tax_amount,
        "total_amount": total_amount,
    }
    confidence = {
        "vendor_name": vendor_conf,
        "invoice_number": CONF_EXPLICIT if invoice_number else CONF_MISSING,
        "invoice_date": date_conf,
        "po_number": CONF_EXPLICIT if po_number else CONF_MISSING,
        "tax_id": tax_id_conf,
        "currency": currency_conf,
        "line_items": line_items_conf,
        "subtotal": CONF_EXPLICIT if subtotal else CONF_MISSING,
        "tax_amount": CONF_EXPLICIT if tax_amount else CONF_MISSING,
        "total_amount": CONF_EXPLICIT if total_amount else CONF_MISSING,
    }
    # Assemble in the canonical order so raw_response JSON is stable.
    data = {field: values[field] for field in FIELD_ORDER}
    ordered_confidence = {field: confidence[field] for field in FIELD_ORDER}
    return data, ordered_confidence


def _empty_result() -> tuple[dict, dict]:
    """Every field null, every confidence 0.0.

    An empty document text is a valid input (scanned PDF, upstream
    extraction failure); its disposition is a later business decision, so
    the provider answers instead of raising.
    """

    data: dict = {field: [] if field == "line_items" else None for field in FIELD_ORDER}
    confidence: dict = {field: CONF_MISSING for field in FIELD_ORDER}
    return data, confidence


class MockLLMProvider:
    """Hybrid mock: exact fixture hit on SHA-256, otherwise regex parsing.

    Satisfies the :class:`~app.services.llm.provider.LLMProvider` protocol;
    callers cannot tell (and should never need to know) whether a response
    came from a fixture, regex, or a future real model.
    """

    def __init__(self, fixtures: list[FixtureEntry] | None = None) -> None:
        self._fixtures = FIXTURES if fixtures is None else fixtures

    def name(self) -> str:
        """Short identifier for logs and audit events."""

        return _PROVIDER_NAME

    def _find_fixture(self, sha256: str) -> FixtureEntry | None:
        for entry in self._fixtures:
            if entry.sha256 == sha256:
                return entry
        return None

    def extract_structured(
        self, *, prompt: str, document_text: str, document_sha256: str | None = None
    ) -> LLMResponse:
        """Extract structured data; never raises on empty-but-present text.

        ``prompt`` is intentionally unused: the mock never merges
        instructions with document text (prompt-injection boundary). A
        missing ``document_text`` (None) is the one case it cannot handle
        and raises :class:`LLMProviderError`.
        """

        start = time.perf_counter()
        if document_text is None:
            raise LLMProviderError("document_text is required; nothing to extract")

        entry = self._find_fixture(document_sha256) if document_sha256 else None
        if entry is not None:
            data = deepcopy(entry.ground_truth)
            confidence = deepcopy(entry.confidence)
            return LLMResponse(
                data=data,
                confidence=confidence,
                source="fixture",
                provider_name=self.name(),
                model_name=_MODEL_NAME,
                raw_response=json.dumps(data),
                duration_ms=_elapsed_ms(start),
            )

        if document_text.strip() == "":
            data, confidence = _empty_result()
            return LLMResponse(
                data=data,
                confidence=confidence,
                source="regex",
                provider_name=self.name(),
                model_name=_MODEL_NAME,
                raw_response="no document text provided; every field left null",
                duration_ms=_elapsed_ms(start),
            )

        data, confidence = _regex_extract(document_text)
        return LLMResponse(
            data=data,
            confidence=confidence,
            source="regex",
            provider_name=self.name(),
            model_name=_MODEL_NAME,
            raw_response=json.dumps(data),
            duration_ms=_elapsed_ms(start),
        )
