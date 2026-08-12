"""The ``google-genai`` client adapter for Gemini (AI-Studio).

Per SPEC.md section 4 this calls ``client.aio.models.generate_content(...)``
with a JSON-schema response config, then parses+validates the raw text against
``schema`` locally (rather than trusting the SDK's own ``response.parsed``
field) so that ``validate.py``'s repair-retry logic has one single,
provider-agnostic notion of "validation failed" that doesn't depend on
SDK-internal behavior.

Deviation from SPEC.md (see CLAUDE.md's "Substitutions from the brief"): the
``vertex`` adapter that once lived here was removed. Vertex AI requires an
attached payment card, which this project's no-card constraint rules out, so it
was never live-runnable here and could only ever be covered by mocked tests.
"""

from __future__ import annotations

from google import genai
from google.genai import types
from pydantic import BaseModel

from app.config import settings
from app.providers.base import LLMResponse, parse_or_none, translate_rate_limit


async def _complete(
    client: genai.Client, model: str, system: str, user: str, schema: type[BaseModel]
) -> LLMResponse:
    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_schema=schema,
    )
    async with translate_rate_limit(default=settings.default_retry_after_seconds):
        response = await client.aio.models.generate_content(
            model=model, contents=user, config=config
        )

    raw_text = response.text or ""
    usage = response.usage_metadata
    tokens_in = (usage.prompt_token_count or 0) if usage else 0
    tokens_out = (usage.candidates_token_count or 0) if usage else 0

    return LLMResponse(
        raw_text=raw_text,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        parsed=parse_or_none(raw_text, schema),
    )


class GeminiProvider:
    """``gemini`` (AI-Studio) — the actually-live provider in this environment."""

    def __init__(self, api_key: str) -> None:
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=int(settings.llm_request_timeout_seconds * 1000)
            ),
        )
        self._model = settings.llm_model

    async def complete(self, system: str, user: str, schema: type[BaseModel]) -> LLMResponse:
        return await _complete(self._client, self._model, system, user, schema)
