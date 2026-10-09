"""Tests for page-aware policy chunking.

Pure text transformation - no database, no I/O.
"""

from app.services.extraction import DocumentText, ExtractedPage
from app.services.policy_chunking import (
    CHUNK_OVERLAP_TOKENS,
    CHUNK_SIZE_TOKENS,
    chunk_document,
)


def doc(*pages: tuple[int, str]) -> DocumentText:
    """Build a DocumentText from (page_number, text) pairs."""
    return DocumentText(
        pages=[ExtractedPage(page_number=n, text=t) for n, t in pages]
    )


# --- basics -----------------------------------------------------------------


def test_chunk_document_single_page_short() -> None:
    """One page, one short paragraph, yields exactly one chunk."""
    drafts = chunk_document(doc((1, "Expenses under 50 dollars need no approval.")))

    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.position == 1
    assert draft.page_number == 1
    assert draft.content == "Expenses under 50 dollars need no approval."
    assert draft.token_count == 7


def test_chunk_document_empty_document() -> None:
    """Empty and whitespace-only pages produce no chunks at all."""
    assert chunk_document(doc((1, ""))) == []
    assert chunk_document(doc((1, "   \n\t  "))) == []
    assert chunk_document(DocumentText(pages=[])) == []
    # An empty page between real ones must not consume a position.
    mixed = chunk_document(doc((1, "First page content."), (2, ""), (3, "Third page content.")))
    assert [d.position for d in mixed] == [1, 2]
    assert [d.page_number for d in mixed] == [1, 3]


def test_chunk_token_count_matches_content() -> None:
    """token_count is the whitespace-token count of content, always."""
    drafts = chunk_document(doc((1, "One two three.\n\nFour five.")))

    assert len(drafts) == 2
    for draft in drafts:
        assert draft.token_count == len(draft.content.split())


# --- page boundaries --------------------------------------------------------


def test_chunk_document_page_boundaries_not_crossed() -> None:
    """A chunk belongs to exactly one page; content never spans a boundary."""
    drafts = chunk_document(
        doc(
            (1, "Page one first paragraph.\n\nPage one second paragraph."),
            (2, "Page two first paragraph.\n\nPage two second paragraph."),
        )
    )

    assert drafts
    for draft in drafts:
        # A chunk must not mention the other page's marker.
        other = "Page two" if draft.page_number == 1 else "Page one"
        assert other not in draft.content


def test_chunk_no_overlap_across_pages() -> None:
    """The first chunk of page 2 carries no tail from page 1."""
    drafts = chunk_document(
        doc((1, "Alpha one.\n\nBravo two."), (2, "Charlie three."))
    )

    page_two = [d for d in drafts if d.page_number == 2]
    assert len(page_two) == 1
    # "Alpha one." was the page-1 tail; it must not prefix the page-2 chunk.
    assert page_two[0].content == "Charlie three."


def test_chunk_positions_are_globally_monotonic() -> None:
    """Positions run 1..N across all pages, page-agnostic and gapless."""
    drafts = chunk_document(
        doc(
            (1, "P1 a.\n\nP1 b."),
            (2, "P2 a.\n\nP2 b."),
            (3, "P3 a."),
        )
    )

    assert [d.position for d in drafts] == list(range(1, len(drafts) + 1))
    # Ordering is document order, so page numbers never go backwards.
    assert [d.page_number for d in drafts] == sorted(d.page_number for d in drafts)


# --- splitting and overlap --------------------------------------------------


def test_chunk_long_paragraph_splits() -> None:
    """A paragraph over the size limit produces multiple chunks."""
    paragraph = " ".join(f"Sentence {i} has several words." for i in range(120))
    assert len(paragraph.split()) > CHUNK_SIZE_TOKENS

    drafts = chunk_document(doc((1, paragraph)))

    assert len(drafts) > 1
    for draft in drafts:
        assert draft.page_number == 1
        # Overlap can push a chunk past the target, but never unboundedly.
        assert draft.token_count <= CHUNK_SIZE_TOKENS + CHUNK_OVERLAP_TOKENS


def test_chunk_long_paragraph_without_sentences_still_splits() -> None:
    """Terminator-free text still gets chunked by the token packer."""
    blob = " ".join(f"tok{i}" for i in range(CHUNK_SIZE_TOKENS * 3))

    drafts = chunk_document(doc((1, blob)))

    assert len(drafts) > 1
    assert "".join(d.content for d in drafts).count("tok0") == 1


def test_chunk_overlap_within_page() -> None:
    """The tail of chunk k is repeated at the head of chunk k+1 on the same page.

    Overlap exists so a sentence split across a boundary is still fully
    retrievable from either chunk.
    """
    first = " ".join(f"alpha{i}" for i in range(CHUNK_SIZE_TOKENS))
    second = " ".join(f"beta{i}" for i in range(CHUNK_SIZE_TOKENS))
    drafts = chunk_document(doc((1, f"{first}\n\n{second}")))

    assert len(drafts) == 2
    tail = drafts[0].content.split()[-CHUNK_OVERLAP_TOKENS:]
    assert drafts[1].content.split()[:CHUNK_OVERLAP_TOKENS] == tail
    assert drafts[0].page_number == drafts[1].page_number == 1


def test_first_chunk_of_page_has_no_overlap_prefix() -> None:
    """The very first chunk starts at the content, with no empty prefix."""
    drafts = chunk_document(doc((1, "Only one paragraph here.")))

    assert len(drafts) == 1
    assert drafts[0].content.startswith("Only one paragraph")