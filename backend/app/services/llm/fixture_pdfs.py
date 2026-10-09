"""Deterministic in-memory PDF generation for the LLM fixtures.

The PDF's text is derived from the fixture's ``ground_truth``, so regenerating
a fixture after changing its ground truth yields a new hash - the test suite
asserts ``sha256(build_fixture_pdf(entry)) == entry.sha256``.

Byte-for-byte reproducibility requires three things: fixed metadata (including
creation/mod dates), and no random trailer /ID. The /ID suppression is why
``save()`` passes ``no_new_id=True`` on top of the garbage/deflate/clean flags;
without it MuPDF writes a fresh random ID on every save.
"""

import io

import pymupdf

from app.services.llm.fixtures import FixtureEntry

__all__ = ["build_fixture_pdf"]

# Fixed page geometry and font keep the content stream identical across runs.
_PAGE_WIDTH = 612
_PAGE_HEIGHT = 792
_LEFT = 72
_TOP = 72
_LEADING = 16
_FONT_SIZE = 11


def _render_lines(entry: FixtureEntry) -> list[str]:
    """Derive the invoice text lines from ``entry.ground_truth``.

    Null fields are simply omitted from the page, which is exactly what the
    regex round-trip test expects to find (or not find) afterwards.
    """

    gt = entry.ground_truth
    lines = [
        str(gt["vendor_name"]),
        f"Invoice #: {gt['invoice_number']}",
        f"Date: {gt['invoice_date']}",
    ]
    if gt["po_number"]:
        lines.append(f"PO: {gt['po_number']}")
    if gt["tax_id"]:
        lines.append(f"Tax ID: {gt['tax_id']}")
    lines.append(f"Currency: {gt['currency']}")
    for item in gt["line_items"]:
        quantity = item["quantity"]
        lines.append(
            f"{item['description']} {quantity} {item['unit_price']} {item['amount']}"
        )
    lines.append(f"Subtotal: {gt['subtotal']}")
    lines.append(f"Tax: {gt['tax_amount']}")
    lines.append(f"Total: {gt['total_amount']}")
    return lines


def build_fixture_pdf(entry: FixtureEntry) -> bytes:
    """Generate ``entry``'s PDF as bytes, reproducible byte-for-byte."""

    doc = pymupdf.open()
    try:
        page = doc.new_page(width=_PAGE_WIDTH, height=_PAGE_HEIGHT)
        y = _TOP
        for line in _render_lines(entry):
            page.insert_text((_LEFT, y), line, fontsize=_FONT_SIZE, fontname="helv")
            y += _LEADING
        doc.set_metadata(
            {
                "format": "PDF 1.7",
                "title": entry.fixture_id,
                "author": "",
                "subject": entry.scenario,
                "keywords": "",
                "creator": "fixture-generator",
                "producer": "fixture-generator",
                # Fixed epoch dates: MuPDF would otherwise stamp "now".
                "creationDate": "D:19700101000000+00'00'",
                "modDate": "D:19700101000000+00'00'",
            }
        )
        buffer = io.BytesIO()
        doc.save(buffer, garbage=0, deflate=True, clean=False, no_new_id=True)
        return buffer.getvalue()
    finally:
        doc.close()
