"""Tests for the extraction service.

Every PDF is built in memory with PyMuPDF; nothing is written to disk.
"""

from collections.abc import Callable

import pymupdf
import pytest

from app.services.extraction import (
    ExtractionError,
    NullOCRProvider,
    PyMuPDFProcessor,
    UnsupportedContentTypeError,
    get_document_processor,
    get_ocr_provider,
)

PdfBuilder = Callable[[list[str]], bytes]


@pytest.fixture
def make_pdf() -> PdfBuilder:
    """Return a builder that renders one PDF page per string, in memory.

    An empty string yields a deliberately blank page.
    """

    def _make(texts: list[str]) -> bytes:
        doc = pymupdf.open()
        try:
            for text in texts:
                page = doc.new_page()
                if text:
                    page.insert_text((72, 72), text)
            return doc.tobytes()
        finally:
            doc.close()

    return _make


def test_pymupdf_extracts_text_from_generated_pdf(make_pdf: PdfBuilder) -> None:
    data = make_pdf(["FIRST_PAGE_MARKER", "SECOND_PAGE_MARKER"])

    result = PyMuPDFProcessor().process(data)

    assert result.page_count == 2
    assert "FIRST_PAGE_MARKER" in result.pages[0].text
    assert "SECOND_PAGE_MARKER" in result.pages[1].text
    assert result.is_empty is False


def test_pymupdf_handles_empty_page(make_pdf: PdfBuilder) -> None:
    data = make_pdf([""])

    result = PyMuPDFProcessor().process(data)

    assert result.page_count == 1
    assert result.pages[0].text.strip() == ""
    assert result.is_empty is True


def test_pymupdf_garbage_bytes_raises() -> None:
    with pytest.raises(ExtractionError):
        PyMuPDFProcessor().process(b"not a pdf")


def test_pymupdf_preserves_page_order(make_pdf: PdfBuilder) -> None:
    data = make_pdf(["ALPHA", "BETA", "GAMMA"])

    result = PyMuPDFProcessor().process(data)

    assert [page.page_number for page in result.pages] == [1, 2, 3]
    assert "ALPHA" in result.pages[0].text
    assert "BETA" in result.pages[1].text
    assert "GAMMA" in result.pages[2].text

    # The markers also appear in page order within the joined full text.
    positions = [result.full_text.index(marker) for marker in ("ALPHA", "BETA", "GAMMA")]
    assert positions == sorted(positions)


def test_get_document_processor_pdf() -> None:
    processor = get_document_processor("application/pdf")

    assert isinstance(processor, PyMuPDFProcessor)


def test_get_document_processor_unsupported() -> None:
    with pytest.raises(UnsupportedContentTypeError):
        get_document_processor("image/png")


def test_null_ocr_provider_unavailable() -> None:
    provider = get_ocr_provider()

    assert isinstance(provider, NullOCRProvider)
    assert provider.is_available() is False
    with pytest.raises(ExtractionError):
        provider.extract(b"", "image/png")


def test_full_text_joins_pages(make_pdf: PdfBuilder) -> None:
    data = make_pdf(["UNIQUE_FIRST_CONTENT", "UNIQUE_SECOND_CONTENT"])

    result = PyMuPDFProcessor().process(data)

    assert "UNIQUE_FIRST_CONTENT" in result.full_text
    assert "UNIQUE_SECOND_CONTENT" in result.full_text
    first = result.full_text.index("UNIQUE_FIRST_CONTENT")
    second = result.full_text.index("UNIQUE_SECOND_CONTENT")
    assert first < second
    assert result.full_text == "\n\n".join(page.text for page in result.pages)