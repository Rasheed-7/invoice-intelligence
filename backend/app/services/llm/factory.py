"""LLM provider factory.

Returns the configured LLM provider based on settings. Currently supports only
the mock provider; future providers (local models, commercial APIs) register
here.
"""

from app.core.config import get_settings
from app.services.llm.mock import MockLLMProvider
from app.services.llm.provider import LLMProvider

__all__ = ["get_llm_provider"]


def get_llm_provider() -> LLMProvider:
    """Return the configured LLM provider.

    Reads ``settings.llm_provider`` and returns the appropriate implementation.
    Currently supports "mock"; raises ValueError for unknown providers.

    Future providers (local models, commercial APIs like OpenAI) register here.
    """
    settings = get_settings()
    provider_name = settings.llm_provider.lower()

    if provider_name == "mock":
        return MockLLMProvider()

    # Future providers register here:
    # if provider_name == "local":
    #     return LocalLLMProvider(...)
    # if provider_name == "openai":
    #     return OpenAIProvider(...)

    raise ValueError(f"unknown LLM_PROVIDER: {settings.llm_provider!r}")
