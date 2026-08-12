"""Provider selection by ``LLM_PROVIDER`` (or its DB override), resolved
against the active API-key-slot index (also DB-overridable) per provider.

Narrow on purpose: this module knows which class to instantiate and which
credential to hand it — nothing about provider internals beyond that.

One instance per (provider name, key index) is cached for the process
lifetime — each ``complete()`` call was previously paying a fresh SDK client
construction (and its underlying HTTP client/connection) on every single
specialist call. Settings are read once at import and provider adapters hold
no per-call mutable state, so caching by (provider, index) is safe: a key
swap becomes a cache miss on the new tuple, and the old entry for the
previous index is simply never looked up again — trivial memory cost, no
explicit teardown needed.
"""

from __future__ import annotations

from app.providers import credentials, key_index, registry
from app.providers.active import active_provider
from app.providers.base import LLMProvider
from app.providers.github_models import GitHubModelsProvider
from app.providers.google_genai import GeminiProvider
from app.providers.groq import GroqProvider

_instances: dict[tuple[str, int], LLMProvider] = {}


def _build(provider: str, index: int) -> LLMProvider:
    # Check membership BEFORE calling credentials.resolve(): resolve() does
    # registry.PROVIDERS[provider], an unguarded dict lookup that raises a
    # bare KeyError for an unknown name. Two pre-existing tests
    # (test_factory_raises_for_unknown_provider,
    # test_factory_rejects_retired_vertex_provider) expect ValueError with a
    # message naming the accepted providers -- resolving first would raise
    # the wrong exception type before ever reaching the check below.
    if provider not in registry.PROVIDERS:
        raise ValueError(
            f"Unknown provider: {provider!r} "
            "(expected 'gemini', 'groq', or 'github_models')"
        )
    _, api_key = credentials.resolve(provider, index)
    if provider == "gemini":
        return GeminiProvider(api_key=api_key)
    if provider == "groq":
        return GroqProvider(api_key=api_key)
    return GitHubModelsProvider(api_key=api_key)


def get_provider() -> LLMProvider:
    provider = active_provider()
    index = key_index.active_key_index(provider)
    cache_key = (provider, index)
    if cache_key not in _instances:
        _instances[cache_key] = _build(provider, index)
    return _instances[cache_key]


def reset_provider_cache() -> None:
    """Clear the cache. Test-only -- production never needs to invalidate it."""
    _instances.clear()
