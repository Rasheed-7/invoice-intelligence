"""Deterministic hash-based embeddings.

NOT semantically meaningful. Two texts with similar *meaning* but different
*words* will not score as similar. Retrieval quality is closer to a TF-IDF-style
lexical match than to real embeddings.

What it does provide:

* deterministic output (same text -> same vector, always)
* unit-normalized vectors so cosine similarity behaves correctly
* offline operation, zero dependencies, zero cost, fast tests
* the same interface as any real embedding model

A real provider (sentence-transformers all-MiniLM-L6-v2, or a commercial
embedding API) implements :class:`~app.services.embeddings.provider.EmbeddingProvider`
identically and is swapped via ``settings.embedding_provider`` without touching
the retrieval service or UI.
"""

import hashlib
import math
import re

__all__ = ["HashEmbeddingProvider"]

#: Vector width. Must match the ``vector(384)`` width of
#: ``policy_chunks.embedding``.
DIMENSION = 384

#: Dimensions touched per token. Each token spreads its weight over this many
#: coordinates so that unrelated tokens do not all collide on one axis.
DIMENSIONS_PER_TOKEN = 8

#: Bytes of the SHA-256 digest consumed per touched dimension.
_BYTES_PER_DIMENSION = 4

#: Weight spread: each touched dimension gets 1.0 + this fraction, so a token
#: contributes a small deterministic bonus on top of a unit of mass.
_WEIGHT_FRACTION_SCALE = 1000

#: Bigrams count half as much as unigrams, so phrase evidence helps without
#: swamping exact-token matches.
_BIGRAM_WEIGHT = 0.5

#: Word characters: letters/digits, everything else splits. Keeps "PO-4411",
#: "invoice_date" and "don't" as single tokens instead of fragments.
_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: Digest length for SHA-256, used to validate the slicing constants.
_DIGEST_BYTES = hashlib.sha256(b"").digest_size

# Runtime guard rather than a bare `assert`: asserts vanish under `python -O`,
# and an out-of-range dimension would corrupt data silently.
assert DIMENSIONS_PER_TOKEN * _BYTES_PER_DIMENSION <= _DIGEST_BYTES, (
    f"{DIMENSIONS_PER_TOKEN} dimensions x {_BYTES_PER_DIMENSION} bytes "
    f"must fit in a {_DIGEST_BYTES}-byte SHA-256 digest"
)


def _tokenize(text: str) -> list[str]:
    """Lowercase and split on any non-alphanumeric run.

    Normalizes case and collapses punctuation, so "Steel,   Plate" and
    "steel plate" produce identical tokens and therefore identical vectors.
    """
    return _TOKEN_RE.findall(text.lower())


def _accumulate(vector: list[float], token: str, weight: float) -> None:
    """Add ``weight`` spread deterministically across DIMENSIONS_PER_TOKEN axes.

    The digest is consumed in fixed-width slices: one slice selects which
    dimension is touched, the next supplies the weight bonus. Both are pure
    functions of the token, so the mapping never drifts.
    """
    digest = hashlib.sha256(token.encode("utf-8")).digest()

    for index in range(DIMENSIONS_PER_TOKEN):
        start = index * _BYTES_PER_DIMENSION
        dim_slice = digest[start : start + _BYTES_PER_DIMENSION]
        weight_slice = digest[start + _BYTES_PER_DIMENSION : start + 2 * _BYTES_PER_DIMENSION]
        if len(weight_slice) < _BYTES_PER_DIMENSION:
            # Only possible if the constants above were retuned past the
            # digest length; fall back to a zero bonus rather than crashing.
            weight_slice = b"\x00" * _BYTES_PER_DIMENSION

        dimension = int.from_bytes(dim_slice, "big") % DIMENSION
        bonus = int.from_bytes(weight_slice, "big") % _WEIGHT_FRACTION_SCALE
        if dimension >= DIMENSION:  # pragma: no cover - guarded by modulo
            raise ValueError(f"dimension index {dimension} exceeds {DIMENSION}")

        vector[dimension] += weight * (1.0 + bonus / _WEIGHT_FRACTION_SCALE)


def _l2_normalize(vector: list[float]) -> list[float]:
    """Scale to unit length. An all-zero vector is returned unchanged.

    Zero-length input has no direction, so there is nothing to normalize; any
    division would produce NaN, which Postgres rejects rather than stores.
    """
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return vector
    return [value / norm for value in vector]


class HashEmbeddingProvider:
    """Deterministic, offline, dependency-free embeddings.

    Implements :class:`~app.services.embeddings.provider.EmbeddingProvider`.
    Stateless and safe to share across threads.
    """

    @property
    def dimension(self) -> int:
        return DIMENSION

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts, one vector per input, in the same order.

        Unigrams contribute full weight; adjacent token pairs ("manager
        approval") contribute half weight so phrase evidence is present but
        does not outweigh exact-token matches. The result is L2-normalized, so
        cosine similarity is a plain dot product and lies in [-1, 1].

        Empty or punctuation-only input yields an all-zero vector. Cosine
        similarity against it is undefined, and in practice reads as "no
        signal" rather than a spurious near-match.
        """
        vectors: list[list[float]] = []

        for text in texts:
            vector = [0.0] * DIMENSION
            tokens = _tokenize(text)

            for token in tokens:
                _accumulate(vector, token, 1.0)

            for first, second in zip(tokens[:-1], tokens[1:], strict=True):
                _accumulate(vector, f"{first} {second}", _BIGRAM_WEIGHT)

            vectors.append(_l2_normalize(vector))

        return vectors

    def embed_one(self, text: str) -> list[float]:
        """Embed a single text."""
        return self.embed([text])[0]