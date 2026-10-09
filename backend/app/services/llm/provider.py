"""LLM provider contract.

``prompt`` is the instruction block - what the caller wants extracted.
``document_text`` is the untrusted document content. They are deliberately
separate parameters: implementations must never concatenate ``document_text``
into the instruction portion of the prompt. Document text is data, never
instructions (see docs/architecture.md). If a future provider needs to build
a combined message, that logic lives in that provider and must be audited
separately - the protocol keeps the boundary structural.
"""

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel

__all__ = ["LLMProvider", "LLMProviderError", "LLMResponse"]


class LLMResponse(BaseModel):
    """Structured result returned by any LLM provider.

    ``data`` is the structured extraction result (a serialized
    InvoiceExtraction in practice, typed as dict because the provider does
    not know about our schema). ``confidence`` carries per-field values: the
    fixture path copies them from the fixture, the regex path reflects actual
    pattern-matching certainty, and missing fields sit at 0.0.
    """

    data: dict
    confidence: dict[str, float]
    source: Literal["fixture", "regex", "mock", "local", "commercial"]
    provider_name: str  # e.g. "mock-hybrid", future "openai-gpt-4o-mini"
    model_name: str  # e.g. "mock-hybrid-v1"
    # Raw textual output for traceability: a JSON dump for the mock, the
    # LLM's raw text for future real providers.
    raw_response: str
    # How long the provider took. Mock is near-zero; kept for parity with
    # real providers.
    duration_ms: int


class LLMProviderError(Exception):
    """Base for provider failures.

    Future real providers raise this on API errors, timeouts and rate limits;
    the mock raises it only when it genuinely cannot produce anything (for
    example when no document text was supplied at all). An empty - but
    present - document text is NOT an error; see the mock's empty-input path.
    """


@runtime_checkable
class LLMProvider(Protocol):
    """Contract every LLM provider must satisfy.

    Application code depends only on this protocol, so the provider can be
    swapped (fixture, regex, local model, commercial API) without the caller
    ever knowing where a response came from.
    """

    def extract_structured(
        self, *, prompt: str, document_text: str, document_sha256: str | None = None
    ) -> LLMResponse:
        """Extract structured data from ``document_text`` under ``prompt``.

        ``document_sha256`` is an optional hint of the source document's
        SHA-256, used by fixture-backed providers for exact lookups. Real
        providers may ignore it.
        """
        ...

    def name(self) -> str:
        """Short identifier for logs and audit events."""
        ...
