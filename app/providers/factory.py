"""Provider selection by ``LLM_PROVIDER``.

Narrow on purpose: this module knows nothing about provider internals beyond
which class to instantiate.
"""

from __future__ import annotations

from app.config import settings
from app.providers.base import LLMProvider
from app.providers.google_genai import GeminiProvider, VertexProvider


def get_provider() -> LLMProvider:
    provider = settings.llm_provider

    if provider == "vertex":
        return VertexProvider()
    if provider == "gemini":
        return GeminiProvider()
    if provider == "groq":
        raise NotImplementedError(
            "LLM_PROVIDER=groq is not implemented yet (a later build step, "
            "app/providers/groq.py). Set LLM_PROVIDER to 'vertex' or 'gemini'."
        )

    raise ValueError(f"Unknown LLM_PROVIDER: {provider!r} (expected 'vertex', 'gemini', or 'groq')")
