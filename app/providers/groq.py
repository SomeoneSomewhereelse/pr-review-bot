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

from groq import AsyncGroq
from pydantic import BaseModel, ValidationError

from app.config import settings
from app.providers.base import LLMResponse


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
        self._client = AsyncGroq(api_key=settings.groq_api_key)
        self._model = settings.groq_model

    async def complete(self, system: str, user: str, schema: type[BaseModel]) -> LLMResponse:
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": _schema_system_prompt(system, schema)},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
        )

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
