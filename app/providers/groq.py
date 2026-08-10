"""``groq`` provider adapter — OpenAI-compatible API, Llama model, cross-vendor.

Per SPEC.md section 4: a different vendor + model (Llama, not Gemini)
demonstrates true provider-agnosticism. Implements ``LLMProvider.complete()``.

Structured output: the model actually available and installed
(``llama-3.3-70b-versatile``, chosen per CLAUDE.md task 1) does NOT support
Groq's ``response_format={"type": "json_schema"}`` (constrained decoding) —
verified live, it returns HTTP 400 "This model does not support response
format `json_schema`". Only a smaller set of Groq-hosted models (mostly
``openai/gpt-oss-*``) support that. So this adapter uses the older
``{"type": "json_object"}`` mode (guarantees valid JSON syntax, but not schema
conformance) plus a schema-instructing system prompt — same approach called
out as a fallback in the task brief. As with ``google_genai.py``, raw text is
parsed+validated against ``schema`` locally (never trusting vendor-side
guarantees), so ``validate.py``'s repair-retry logic has one single,
provider-agnostic notion of "validation failed".
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from groq import AsyncGroq
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


def _schema_system_prompt(system: str, schema: type[BaseModel]) -> str:
    """Append the JSON schema to the system prompt.

    ``json_object`` mode only guarantees syntactically-valid JSON, not
    conformance to any particular shape — the model needs the schema spelled
    out in the prompt to have a chance of matching it.
    """
    schema_json = json.dumps(schema.model_json_schema())
    return (
        f"{system}\n\nRespond ONLY with valid JSON matching this JSON schema, "
        f"no prose, no markdown code fences:\n{schema_json}"
    )


class GroqProvider:
    """``groq`` — cross-vendor fallback (Llama via Groq's OpenAI-compatible API)."""

    def __init__(self) -> None:
        # max_retries=0: the SDK's own default (2) silently retries a 429
        # with backoff before this adapter's except clause ever sees it --
        # confirmed live (a 43.1s call, vs. ~5s normal, that never surfaced
        # as RateLimited despite exceeding the account's TPM budget). The
        # durable queue (app/queue/dispatcher.py) already owns retry/backoff
        # for a rate-limited review -- durable across a process restart,
        # visible via a placeholder/schedule-note comment -- so a second,
        # hidden retry layer underneath it is redundant at best and actively
        # hides a real signal at worst.
        self._client = AsyncGroq(api_key=settings.groq_api_key, max_retries=0)
        self._model = settings.groq_model

    async def complete(self, system: str, user: str, schema: type[BaseModel]) -> LLMResponse:
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _schema_system_prompt(system, schema)},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
            )
        # re-raised unless it's a 429
        except Exception as exc:  # noqa: BLE001
            rl = rate_limited_or_none(
                exc, now=datetime.now(timezone.utc), default=settings.default_retry_after_seconds
            )
            if rl is not None:
                raise rl from exc
            raise

        raw_text = response.choices[0].message.content or ""
        usage = response.usage
        tokens_in = (usage.prompt_tokens or 0) if usage else 0
        tokens_out = (usage.completion_tokens or 0) if usage else 0

        return LLMResponse(
            raw_text=raw_text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            parsed=_parse(raw_text, schema),
        )
