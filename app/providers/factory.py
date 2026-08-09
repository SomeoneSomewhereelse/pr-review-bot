"""Provider selection by ``LLM_PROVIDER``.

Narrow on purpose: this module knows nothing about provider internals beyond
which class to instantiate.
"""

from __future__ import annotations

from app.providers.active import active_provider
from app.providers.base import LLMProvider
from app.providers.github_models import GitHubModelsProvider
from app.providers.google_genai import GeminiProvider
from app.providers.groq import GroqProvider


def get_provider() -> LLMProvider:
    provider = active_provider()

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
