"""The embedding provider interface.

Embeddings are behind this protocol so the pipeline does not depend on a
specific model. The MVP ships HashEmbeddingProvider (deterministic, offline,
zero-cost). A local transformer or commercial API provider implements the same
interface and is swapped via config.
"""

from typing import Protocol, runtime_checkable

__all__ = ["EmbeddingProvider"]


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Turns text into fixed-width float vectors.

    ``dimension`` is the vector width the provider emits. It is fixed per
    implementation and must match the ``vector(N)`` width of the column the
    vectors are written to - a mismatch is a database error at insert time, not
    a silent truncation, so callers should check it before a bulk write.
    """

    @property
    def dimension(self) -> int:
        """Width of every vector this provider returns."""
        ...

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts, one vector per input, in the same order."""
        ...

    def embed_one(self, text: str) -> list[float]:
        """Embed a single text. Convenience over ``embed([text])[0]``."""
        ...