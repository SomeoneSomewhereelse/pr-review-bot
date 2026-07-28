"""Two ``google-genai`` client adapters: Vertex and Gemini (AI-Studio).

Per SPEC.md section 4, these are otherwise identical calls against one SDK —
only client construction differs (``vertexai=True, project=..., location=...``
vs ``api_key=...``). Both implement ``LLMProvider.complete()``: call
``client.aio.models.generate_content(...)`` with a JSON-schema response
config, then parse+validate the raw text against ``schema`` locally (rather
than trusting the SDK's own ``response.parsed`` field) so that
``validate.py``'s repair-retry logic has one single, provider-agnostic
notion of "validation failed" that doesn't depend on SDK-internal behavior.

Deviation from SPEC.md (see CLAUDE.md / task brief): in this environment
``vertex`` is implemented per-spec but not live-runnable (no GCP project, no
billing, no ADC) — it is covered by mocked tests only. ``gemini`` is the
actually-live provider, verified for real in
``scripts/manual_verify_step4.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError

from app.config import settings
from app.providers.base import LLMResponse, rate_limited_or_none


def _parse(raw_text: str, schema: type[BaseModel]) -> BaseModel | None:
    """Best-effort JSON parse + schema validation. Never raises."""
    try:
        data = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        return None
    try:
        return schema.model_validate(data)
    except ValidationError:
        return None


async def _complete(
    client: genai.Client, model: str, system: str, user: str, schema: type[BaseModel]
) -> LLMResponse:
    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_schema=schema,
    )
    try:
        response = await client.aio.models.generate_content(model=model, contents=user, config=config)
    except Exception as exc:  # noqa: BLE001 - re-raised unless it's a 429
        rl = rate_limited_or_none(
            exc, now=datetime.now(timezone.utc), default=settings.default_retry_after_seconds
        )
        if rl is not None:
            raise rl from exc
        raise

    raw_text = response.text or ""
    usage = response.usage_metadata
    tokens_in = (usage.prompt_token_count or 0) if usage else 0
    tokens_out = (usage.candidates_token_count or 0) if usage else 0

    return LLMResponse(
        raw_text=raw_text,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        parsed=_parse(raw_text, schema),
    )


class VertexProvider:
    """``vertex`` (SPEC's default). Not live-verified in this environment —
    no GCP project/billing/ADC configured — but implemented per SPEC/SDK and
    covered by mocked tests.
    """

    def __init__(self) -> None:
        self._client = genai.Client(
            vertexai=True,
            project=settings.google_cloud_project,
            location=settings.google_cloud_location,
        )
        self._model = settings.llm_model

    async def complete(self, system: str, user: str, schema: type[BaseModel]) -> LLMResponse:
        return await _complete(self._client, self._model, system, user, schema)


class GeminiProvider:
    """``gemini`` (AI-Studio) — the actually-live provider in this environment."""

    def __init__(self) -> None:
        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._model = settings.llm_model

    async def complete(self, system: str, user: str, schema: type[BaseModel]) -> LLMResponse:
        return await _complete(self._client, self._model, system, user, schema)
