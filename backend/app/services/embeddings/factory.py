"""Embedding provider factory.

Returns the configured embedding provider based on settings. Currently supports
only the deterministic hash provider; a real model registers here.
"""

from app.core.config import get_settings
from app.services.embeddings.hash_provider import HashEmbeddingProvider
from app.services.embeddings.provider import EmbeddingProvider

__all__ = ["get_embedding_provider"]


def get_embedding_provider() -> EmbeddingProvider:
    """Return the configured embedding provider.

    Reads ``settings.embedding_provider`` and returns the appropriate
    implementation. Currently supports "hash"; raises ValueError for unknown
    providers.

    A real provider (sentence-transformers, or a commercial embedding API)
    registers here. Nothing else in the codebase needs to change: consumers
    depend on the EmbeddingProvider protocol, not on a concrete class.
    """
    settings = get_settings()
    name = settings.embedding_provider.lower()

    if name == "hash":
        return HashEmbeddingProvider()

    # Future providers register here:
    # if name == "minilm":
    #     return SentenceTransformerEmbeddingProvider()

    raise ValueError(f"unknown EMBEDDING_PROVIDER: {settings.embedding_provider!r}")