"""Provider selection by ``LLM_PROVIDER``.

Narrow on purpose: this module knows nothing about provider internals beyond
which class to instantiate.

One instance per provider name is cached for the process lifetime -- each
``complete()`` call was previously paying a fresh SDK client construction
(and its underlying HTTP client/connection) on every single specialist call.
Settings are read once at import and provider adapters hold no per-call
mutable state, so caching by name is safe.
"""

from __future__ import annotations

from app.providers.active import active_provider
from app.providers.base import LLMProvider
from app.providers.github_models import GitHubModelsProvider
from app.providers.google_genai import GeminiProvider
from app.providers.groq import GroqProvider

_instances: dict[str, LLMProvider] = {}


def _build(provider: str) -> LLMProvider:
    if provider == "gemini":
        return GeminiProvider()
    if provider == "groq":
        return GroqProvider()
    if provider == "github_models":
        return GitHubModelsProvider()

    raise ValueError(
        f"Unknown provider: {provider!r} "
        "(expected 'gemini', 'groq', or 'github_models')"
    )


def get_provider() -> LLMProvider:
    provider = active_provider()
    if provider not in _instances:
        _instances[provider] = _build(provider)
    return _instances[provider]


def reset_provider_cache() -> None:
    """Clear the cache. Test-only -- production never needs to invalidate it."""
    _instances.clear()
