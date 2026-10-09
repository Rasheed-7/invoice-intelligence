"""Document text is untrusted input.

The functions in this module extract raw text from files. They do not interpret
it. Downstream callers must treat extracted text as data, never as instructions.
See docs/architecture.md for the platform's stance on prompt injection.
"""

from typing import Protocol

import pymupdf
from pydantic import BaseModel, model_validator

__all__ = [
    "DocumentProcessor",
    "DocumentText",
    "ExtractionError",
    "ExtractedPage",
    "NullOCRProvider",
    "OCRProvider",
    "PyMuPDFProcessor",
    "UnsupportedContentTypeError",
    "get_document_processor",
    "get_ocr_provider",
]


class ExtractedPage(BaseModel):
    """Text extracted from a single page of a document."""

    page_number: int  # 1-indexed
    text: str
    page_metadata: dict | None = None


class DocumentText(BaseModel):
    """Full extraction result for one document.

    ``full_text``, ``page_count`` and ``is_empty`` are derived from ``pages``
    and recomputed on every construction, so they can never disagree with the
    page list. Page boundaries live structurally in ``pages``; ``full_text``
    joins with blank lines and deliberately carries no page-break markers.
    """

    pages: list[ExtractedPage]
    # Derived fields. The defaults exist only so the model can be built from
    # ``pages`` alone; the validator below always overwrites them.
    full_text: str = ""
    page_count: int = 0
    is_empty: bool = True

    @model_validator(mode="after")
    def _derive_from_pages(self) -> "DocumentText":
        full_text = "\n\n".join(page.text for page in self.pages)
        self.full_text = full_text
        self.page_count = len(self.pages)
        self.is_empty = full_text.strip() == ""
        return self


class ExtractionError(Exception):
    """Base class for every extraction failure."""


class UnsupportedContentTypeError(ExtractionError):
    """Raised when no processor can handle the given content type."""


class DocumentProcessor(Protocol):
    """Contract for turning raw document bytes into a :class:`DocumentText`."""

    def can_process(self, content_type: str) -> bool: ...

    def process(self, data: bytes) -> DocumentText: ...


class OCRProvider(Protocol):
    """Contract for an OCR backend that handles images and scanned files."""

    def is_available(self) -> bool: ...

    def extract(self, data: bytes, content_type: str) -> DocumentText: ...


class PyMuPDFProcessor:
    """PDF text extraction backed by PyMuPDF.

    Operates on bytes only - never a path - so extraction is a pure function
    of its input. Empty pages are not an error: a blank page is a valid page,
    and ``DocumentText.is_empty`` reflects the document as a whole.
    """

    def can_process(self, content_type: str) -> bool:
        """Return True only for ``application/pdf``; images go to OCR later."""

        return content_type == "application/pdf"

    def process(self, data: bytes) -> DocumentText:
        """Extract every page of a PDF into a :class:`DocumentText`.

        Raises :class:`ExtractionError` if PyMuPDF cannot open or read the
        document. The document handle is always closed, even on failure.
        """

        try:
            doc = pymupdf.open(stream=data, filetype="pdf")
        except Exception as exc:
            raise ExtractionError(f"could not open PDF document: {exc}") from exc

        try:
            pages: list[ExtractedPage] = []
            for i, page in enumerate(doc, start=1):
                text = page.get_text()
                pages.append(ExtractedPage(page_number=i, text=text, page_metadata=None))
        except Exception as exc:
            raise ExtractionError(f"could not read PDF document: {exc}") from exc
        finally:
            doc.close()

        return DocumentText(pages=pages)


class NullOCRProvider:
    """Placeholder OCR provider.

    Exists so callers can depend on the :class:`OCRProvider` protocol today
    and swap in a real backend later without touching any call site.
    """

    def is_available(self) -> bool:
        """Always False: no OCR backend is wired up in this build."""

        return False

    def extract(self, data: bytes, content_type: str) -> DocumentText:
        """Raise - OCR is not configured in this build."""

        raise ExtractionError("no OCR provider configured")


def get_document_processor(content_type: str) -> DocumentProcessor:
    """Return the processor registered for ``content_type``.

    Raises :class:`UnsupportedContentTypeError` for anything without a
    processor - images included, since image handling is deferred to the OCR
    step.
    """

    processor = PyMuPDFProcessor()
    if processor.can_process(content_type):
        return processor
    raise UnsupportedContentTypeError(f"no processor for content type: {content_type!r}")


def get_ocr_provider() -> OCRProvider:
    """Return the OCR provider.

    Always the null provider for now; a later step makes this configurable
    via Settings.
    """

    return NullOCRProvider()