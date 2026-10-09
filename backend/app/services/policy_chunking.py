"""Deterministic, page-aware chunking for policy documents.

Pure text transformation: no database access, no I/O, no embedding calls. The
caller turns the returned drafts into persisted rows.

The strategy is deliberately simple and explainable. Retrieval quality on a
policy corpus depends far more on chunk boundaries being sensible than on
subtle token packing, and a reader can reason about "one paragraph per chunk,
split at sentences when it is too long" without simulating anything.
"""

import re

from pydantic import BaseModel, ConfigDict

from app.services.extraction import DocumentText

__all__ = [
    "CHUNK_OVERLAP_TOKENS",
    "CHUNK_SIZE_TOKENS",
    "PolicyChunkDraft",
    "chunk_document",
]

#: Target chunk size, in whitespace tokens.
CHUNK_SIZE_TOKENS: int = 200

#: Tokens of the previous chunk repeated at the head of the next one, so a
#: sentence straddling a boundary is still fully retrievable from either side.
CHUNK_OVERLAP_TOKENS: int = 50

#: Paragraph separator. Two or more blank lines.
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n+")

# Sentence heuristic: terminator followed by whitespace or end of string.
# Deliberately crude - it does not understand abbreviations ("e.g.", "Mr."),
# quotes, or decimals. Good enough to keep an oversized paragraph from becoming
# one giant chunk; a real segmenter is the fix if that ever matters.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])(?=\s|$)")


class PolicyChunkDraft(BaseModel):
    """One proposed chunk, before it is embedded and persisted."""

    model_config = ConfigDict(extra="forbid")

    page_number: int
    position: int
    content: str
    token_count: int


def _tokenize(text: str) -> list[str]:
    """Split on whitespace.

    A whitespace-token approximation of length. The provider abstraction means a
    real tokenizer can be plugged in later without changing callers.
    """
    return text.split()


def _split_paragraphs(text: str) -> list[str]:
    """Split on blank-line separation, dropping empty pieces."""
    return [part.strip() for part in _PARAGRAPH_SPLIT.split(text) if part.strip()]


def _split_sentences(text: str) -> list[str]:
    """Split on sentence terminators. Heuristic - see the regex comment."""
    return [part.strip() for part in _SENTENCE_SPLIT.split(text) if part.strip()]


def _pack(tokens: list[str], limit: int) -> list[list[str]]:
    """Greedily pack tokens into groups of at most ``limit``.

    A single token longer than ``limit`` still gets its own group rather than
    being dropped or split mid-word.
    """
    groups: list[list[str]] = []
    current: list[str] = []

    for token in tokens:
        if len(current) == limit:
            groups.append(current)
            current = []
        current.append(token)

    if current:
        groups.append(current)

    return groups


def _overlap_prefix(previous: list[str]) -> list[str]:
    """The trailing overlap window of the previous chunk's tokens."""
    return previous[-CHUNK_OVERLAP_TOKENS:]


def _chunk_page(
    page_text: str,
    *,
    page_number: int,
    position: int,
) -> tuple[list[PolicyChunkDraft], int]:
    """Chunk one page's text.

    ``position`` is the next unused global position. Returns the drafts plus the
    position the counter ended on, so the caller can continue across pages.
    """
    drafts: list[PolicyChunkDraft] = []
    previous_tokens: list[str] = []

    for paragraph in _split_paragraphs(page_text):
        paragraph_tokens = _tokenize(paragraph)

        if not paragraph_tokens:
            continue

        if len(paragraph_tokens) <= CHUNK_SIZE_TOKENS:
            # Whole paragraph becomes one chunk. Overlap applies only when this
            # is not the first chunk of the page, so the prefix is never empty
            # or duplicated from a prior page.
            chunk_tokens = (
                [*_overlap_prefix(previous_tokens), *paragraph_tokens]
                if previous_tokens
                else list(paragraph_tokens)
            )
            drafts.append(
                PolicyChunkDraft(
                    page_number=page_number,
                    position=position,
                    content=" ".join(chunk_tokens),
                    token_count=len(chunk_tokens),
                )
            )
            position += 1
            previous_tokens = chunk_tokens
            continue

        # Oversized paragraph: break it into sentences, then pack the
        # sentences into chunks that fit.
        sentence_groups = _pack(_tokenize(" ".join(_split_sentences(paragraph))), CHUNK_SIZE_TOKENS)

        for group in sentence_groups:
            chunk_tokens = (
                [*_overlap_prefix(previous_tokens), *group]
                if previous_tokens
                else list(group)
            )
            drafts.append(
                PolicyChunkDraft(
                    page_number=page_number,
                    position=position,
                    content=" ".join(chunk_tokens),
                    token_count=len(chunk_tokens),
                )
            )
            position += 1
            previous_tokens = chunk_tokens

    return drafts, position


def chunk_document(document_text: DocumentText) -> list[PolicyChunkDraft]:
    """Split an extracted document into page-scoped chunk drafts.

    A chunk never spans a page boundary: retrieval results can therefore always
    be cited to a specific page. Pages are visited in order and ``position`` is
    a single monotonic counter across all of them, giving chunks a stable,
    page-agnostic ordering for display and stable pagination.

    Pages with no extractable text (a scanned image, a blank page) contribute no
    chunks and do not consume a position.

    Returns:
        Drafts ordered by ``position``, empty when the document has no text.
    """
    drafts: list[PolicyChunkDraft] = []
    position = 1

    for page in document_text.pages:
        if not page.text.strip():
            continue

        page_drafts, position = _chunk_page(
            page.text, page_number=page.page_number, position=position
        )
        drafts.extend(page_drafts)

    return drafts
