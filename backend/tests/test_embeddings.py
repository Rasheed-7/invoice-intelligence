"""Tests for the deterministic hash embedding provider and its factory.

These are pure in-memory tests: no database, no network. The vector column
itself is covered separately in tests/test_pgvector_integration.py.
"""

import math

import pytest

from app.services.embeddings.factory import get_embedding_provider
from app.services.embeddings.hash_provider import HashEmbeddingProvider
from app.services.embeddings.provider import EmbeddingProvider

PROVIDER = HashEmbeddingProvider()
DIMENSION = 384


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors.

    Vectors from this provider are unit-normalized, so this is a plain dot
    product. Written out rather than imported so a failure here points at the
    provider rather than at a helper.
    """
    return sum(x * y for x, y in zip(a, b, strict=True))


# --- shape ------------------------------------------------------------------


def test_dimension_is_384() -> None:
    """The provider reports 384, matching the vector(384) column width."""
    assert PROVIDER.dimension == DIMENSION


def test_embed_returns_list_of_384_floats() -> None:
    """A single input yields exactly one vector of 384 finite floats."""
    vectors = PROVIDER.embed(["invoice approval policy"])

    assert len(vectors) == 1
    vector = vectors[0]
    assert len(vector) == DIMENSION
    assert all(isinstance(value, float) for value in vector)
    assert all(math.isfinite(value) for value in vector)


def test_embed_one_matches_embed() -> None:
    """embed_one is exactly embed([text])[0], including order preservation."""
    texts = ["alpha", "beta", "gamma"]

    batched = PROVIDER.embed(texts)

    assert batched == [PROVIDER.embed_one(text) for text in texts]


# --- determinism ------------------------------------------------------------


def test_embed_is_deterministic() -> None:
    """The same text always produces the identical vector, across calls."""
    text = "manager approval required for invoices over 5000 USD"

    first = PROVIDER.embed_one(text)
    second = PROVIDER.embed_one(text)
    third = PROVIDER.embed([text])[0]

    assert first == second == third


def test_different_texts_produce_different_vectors() -> None:
    """Distinct inputs do not collapse onto the same vector."""
    a = PROVIDER.embed_one("purchase order reconciliation")
    b = PROVIDER.embed_one("duplicate invoice detection")

    assert a != b


# --- normalization ----------------------------------------------------------


def test_embed_is_unit_normalized() -> None:
    """Non-empty input yields L2 length 1.0, so cosine is a plain dot product."""
    for text in ["approval policy", "a", "the quick brown fox jumps over the lazy dog"]:
        vector = PROVIDER.embed_one(text)
        norm = math.sqrt(sum(value * value for value in vector))
        assert norm == pytest.approx(1.0, abs=1e-9), text


def test_embed_empty_text_returns_zero_vector() -> None:
    """Empty and whitespace-only input yield all-zero, never NaN.

    Zero-length has no direction, so there is nothing to normalize. The all-zero
    vector reads as "no signal" rather than a spurious near-match.
    """
    for text in ["", "   ", "\t\n", ".,;!?-"]:
        vector = PROVIDER.embed_one(text)
        assert vector == [0.0] * DIMENSION, repr(text)
        assert all(math.isfinite(value) for value in vector)


# --- retrieval behaviour ----------------------------------------------------


def test_similar_texts_have_higher_cosine_than_unrelated() -> None:
    """Shared vocabulary scores above an unrelated document.

    Asserts ordering only. The absolute numbers depend on hashing internals;
    the property that matters is the ranking.
    """
    query = PROVIDER.embed_one("approval policy invoice")
    related = PROVIDER.embed_one("approval policy for invoices")
    unrelated = PROVIDER.embed_one("the quick brown fox")

    assert cosine(query, related) > cosine(query, unrelated)


def test_bigram_inclusion() -> None:
    """Word order is captured, which only the bigram pass can do.

    Both documents contain the same unigrams ("manager", "approval"), so unigram
    weighting alone cannot separate them. Only the bigram pass makes "manager
    approval" a unit, which is why "manager approval threshold" must outrank
    "threshold manager".
    """
    query = PROVIDER.embed_one("manager approval")
    in_order = PROVIDER.embed_one("manager approval threshold")
    reversed_words = PROVIDER.embed_one("threshold manager")

    assert cosine(query, in_order) > cosine(query, reversed_words)


def test_embedding_normalizes_case_and_punctuation() -> None:
    """Punctuation and casing are normalized away, not encoded as signal."""
    assert PROVIDER.embed_one("Steel,   Plate") == PROVIDER.embed_one("steel plate")


# --- protocol conformance ---------------------------------------------------


def test_provider_satisfies_protocol() -> None:
    """A concrete provider is structurally an EmbeddingProvider."""
    assert isinstance(PROVIDER, EmbeddingProvider)


# --- factory ----------------------------------------------------------------


def test_factory_returns_hash_provider_by_default() -> None:
    """With no override configured, the factory resolves to the hash provider."""
    provider = get_embedding_provider()

    assert isinstance(provider, HashEmbeddingProvider)
    assert isinstance(provider, EmbeddingProvider)
    assert provider.dimension == DIMENSION


def test_factory_rejects_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrecognized name fails loudly rather than silently falling back."""
    from app.core.config import get_settings

    monkeypatch.setenv("EMBEDDING_PROVIDER", "not-a-real-provider")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="EMBEDDING_PROVIDER"):
            get_embedding_provider()
    finally:
        # Settings are lru_cached; leaving the override set would leak into
        # every later test in this session.
        monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
        get_settings.cache_clear()