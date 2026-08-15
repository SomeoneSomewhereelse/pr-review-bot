"""Provider selection by ``LLM_PROVIDER`` (or its DB override), resolved
against the active API-key-slot index (also DB-overridable) per provider.

Narrow on purpose: this module knows which class to instantiate and which
credential to hand it -- nothing about provider internals beyond that. The
one asymmetry is vertex, whose credential is a service-account identity
rather than an API-key string and whose absence means "use implicit ADC"
rather than "misconfigured"; app/providers/vertex_credentials.py owns that
resolution, this module only branches on it.

One instance per (provider name, key index, model) is cached for the process
lifetime — each ``complete()`` call was previously paying a fresh SDK client
construction (and its underlying HTTP client/connection) on every single
specialist call. Settings are read once at import and provider adapters hold
no per-call mutable state, so caching by (provider, index, model) is safe: a
key swap (or a model override) becomes a cache miss on the new tuple, and the
old entry for the previous index/model is simply never looked up again --
trivial memory cost, no explicit teardown needed.
"""

from __future__ import annotations

from app.config import settings
from app.providers import credentials, key_index, registry, vertex_credentials
from app.providers.active import active_provider
from app.providers.active_model import active_model
from app.providers.base import LLMProvider
from app.providers.google_genai import GeminiProvider, VertexProvider
from app.providers.groq import GroqProvider

_instances: dict[tuple[str, int, str], LLMProvider] = {}


def _build(provider: str, index: int, model: str) -> LLMProvider:
    # Check membership BEFORE resolving any credential: resolve() does
    # registry.PROVIDERS[provider], an unguarded dict lookup that raises a
    # bare KeyError for an unknown name. test_factory_raises_for_unknown_provider
    # expects ValueError with a message naming the accepted providers --
    # resolving first would raise the wrong exception type before ever
    # reaching the check below.
    if provider not in registry.PROVIDERS:
        raise ValueError(
            f"Unknown provider: {provider!r} (expected 'gemini', 'groq', or 'vertex')"
        )
    if provider == "vertex":
        # Deliberately ahead of the generic empty-credential fast-fail below:
        # for vertex an empty resolved credential is legitimate (it means
        # "fall through to implicit ADC"), unlike gemini/groq where an empty
        # string always means misconfigured. The locally-detectable invalid
        # state here is instead "no project to call with at all", which
        # happens only with no GCP_PROJECT override AND no service-account key
        # anywhere to derive one from. Both steps are local -- decoding an env
        # var, reading a file -- so this is still a no-network fast-fail, just
        # performed after credential resolution instead of before it.
        info = vertex_credentials.resolve_service_account_info(index)
        project = settings.gcp_project or (info or {}).get("project_id", "")
        if not project:
            raise ValueError(
                "no credential configured for provider='vertex': GCP_PROJECT not set "
                "and no service-account key found to derive it from"
            )
        return VertexProvider(
            project=project,
            location=settings.gcp_location,
            service_account_info=info,
            model=model,
        )
    env_name, api_key = credentials.resolve(provider, index)
    # Locally-detectable invalid state: no live call needed to know this slot
    # was never provisioned anywhere. Caught by run_specialist's existing
    # broad except -- all three specialists fail with this exact message,
    # with zero network calls, instead of each independently discovering the
    # same problem via a wasted, doomed real call. A DEAD-but-CONFIGURED
    # provider (a real credential, vendor down/retired) is unaffected: resolve()
    # returns a non-empty value here and this check never fires for it.
    if not api_key:
        raise ValueError(
            f"no credential configured for provider={provider!r} index={index} "
            f"({env_name} not set)"
        )
    if provider == "gemini":
        return GeminiProvider(api_key=api_key, model=model)
    if provider == "groq":
        return GroqProvider(api_key=api_key, model=model)
    raise ValueError(f"registry lists {provider!r} but _build cannot construct it")


def get_provider() -> LLMProvider:
    provider = active_provider()
    index = key_index.active_key_index(provider)
    # The model is part of the cache key, not just a constructor argument:
    # adapters bake it in at construction and this cache is process-lifetime,
    # so without it a DB model override would silently no-op on a warm process
    # while the PR comment reported the new model. Same mechanism this cache
    # already relies on for a key swap -- a changed value is simply a miss on a
    # new tuple.
    model = active_model(provider)
    cache_key = (provider, index, model)
    if cache_key not in _instances:
        _instances[cache_key] = _build(provider, index, model)
    return _instances[cache_key]


def reset_provider_cache() -> None:
    """Clear the cache. Test-only -- production never needs to invalidate it."""
    _instances.clear()
