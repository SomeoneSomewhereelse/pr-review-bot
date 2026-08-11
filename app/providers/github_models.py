"""``github_models`` provider adapter — OpenAI-compatible API, OpenAI models,
riding the user's existing GitHub account (no separate signup/account risk).

Per SPEC.md section 4's cross-vendor intent: a genuinely different vendor
(OpenAI, via GitHub Models) and model family from both Gemini (Google) and
Groq (Llama) — the strongest cross-vendor demonstration among the providers
actually usable in this environment.

Unlike Groq, this API supports native ``json_schema`` structured output
(strict mode) rather than the ``json_object``-plus-prompt-injection
fallback — verified live. Raw output is still parsed+validated locally
against ``schema`` (never trusting vendor-side guarantees), matching every
other provider's never-raise contract.
"""

from __future__ import annotations

from openai import AsyncOpenAI
from pydantic import BaseModel

from app.config import settings
from app.providers.base import LLMResponse, parse_or_none, translate_rate_limit

_BASE_URL = "https://models.github.ai/inference"


def _add_additional_properties_false(node: object) -> None:
    """Recursively set ``additionalProperties: false`` on every object schema.

    OpenAI's strict json_schema mode requires this on EVERY nested object
    schema, not just the top level — Pydantic's model_json_schema() puts
    nested models under "$defs" (referenced via "$ref"), and a live 400
    caught that the top-level-only fix missed those. Walks dicts/lists
    generically rather than special-casing "$defs" so any nesting shape
    Pydantic produces is covered.
    """
    if isinstance(node, dict):
        if node.get("type") == "object" and "additionalProperties" not in node:
            node["additionalProperties"] = False
        for value in node.values():
            _add_additional_properties_false(value)
    elif isinstance(node, list):
        for item in node:
            _add_additional_properties_false(item)


def _response_format(schema: type[BaseModel]) -> dict:
    json_schema = schema.model_json_schema()
    _add_additional_properties_false(json_schema)

    return {
        "type": "json_schema",
        "json_schema": {
            "name": schema.__name__,
            "schema": json_schema,
            "strict": True,
        },
    }


class GitHubModelsProvider:
    """``github_models`` — cross-vendor option riding the user's GitHub account."""

    def __init__(self) -> None:
        self._client = AsyncOpenAI(
            base_url=_BASE_URL,
            api_key=settings.github_models_token,
            timeout=settings.llm_request_timeout_seconds,
        )
        self._model = settings.github_models_model

    async def complete(self, system: str, user: str, schema: type[BaseModel]) -> LLMResponse:
        async with translate_rate_limit(default=settings.default_retry_after_seconds):
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format=_response_format(schema),
            )

        raw_text = response.choices[0].message.content or ""
        usage = response.usage
        tokens_in = (usage.prompt_tokens or 0) if usage else 0
        tokens_out = (usage.completion_tokens or 0) if usage else 0

        return LLMResponse(
            raw_text=raw_text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            parsed=parse_or_none(raw_text, schema),
        )
