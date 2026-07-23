"""Deterministic tests for the provider layer (app/providers/*).

Mocks the actual `google.genai.Client` boundary (`client.aio.models.generate_content`)
so nothing here ever makes a real network call. Live verification against the
real Gemini API happens separately in scripts/manual_verify_step4.py.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from app.config import settings
from app.providers import pricing
from app.providers.factory import get_provider
from app.providers.google_genai import GeminiProvider, VertexProvider
from app.providers.validate import validate_and_repair


class Greeting(BaseModel):
    message: str


def _fake_response(text: str, tokens_in: int = 10, tokens_out: int = 5):
    """Build a stand-in for google.genai.types.GenerateContentResponse."""
    return SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(
            prompt_token_count=tokens_in,
            candidates_token_count=tokens_out,
        ),
    )


# --------------------------------------------------------------------------
# google_genai.py — both client shapes
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gemini_provider_parses_valid_structured_output(monkeypatch):
    fake_generate = AsyncMock(return_value=_fake_response(json.dumps({"message": "hi"}), 42, 7))
    monkeypatch.setattr(
        "app.providers.google_genai.genai.Client",
        lambda **kwargs: SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate))),
    )

    provider = GeminiProvider()
    result = await provider.complete("system prompt", "user prompt", Greeting)

    assert result.parsed == Greeting(message="hi")
    assert result.tokens_in == 42
    assert result.tokens_out == 7
    fake_generate.assert_awaited_once()
    _, kwargs = fake_generate.call_args
    assert kwargs["model"] == settings.llm_model


@pytest.mark.asyncio
async def test_vertex_provider_parses_valid_structured_output(monkeypatch):
    captured_client_kwargs = {}

    def fake_client_ctor(**kwargs):
        captured_client_kwargs.update(kwargs)
        return SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(
                    generate_content=AsyncMock(return_value=_fake_response(json.dumps({"message": "hi"}), 3, 2))
                )
            )
        )

    monkeypatch.setattr("app.providers.google_genai.genai.Client", fake_client_ctor)
    monkeypatch.setattr(settings, "google_cloud_project", "my-project")
    monkeypatch.setattr(settings, "google_cloud_location", "us-central1")

    provider = VertexProvider()
    result = await provider.complete("system prompt", "user prompt", Greeting)

    assert result.parsed == Greeting(message="hi")
    assert captured_client_kwargs["vertexai"] is True
    assert captured_client_kwargs["project"] == "my-project"
    assert captured_client_kwargs["location"] == "us-central1"


@pytest.mark.asyncio
async def test_provider_returns_none_parsed_on_malformed_json(monkeypatch):
    fake_generate = AsyncMock(return_value=_fake_response("not json at all", 10, 1))
    monkeypatch.setattr(
        "app.providers.google_genai.genai.Client",
        lambda **kwargs: SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate))),
    )

    provider = GeminiProvider()
    result = await provider.complete("system prompt", "user prompt", Greeting)

    assert result.parsed is None
    assert result.tokens_in == 10
    assert result.tokens_out == 1


@pytest.mark.asyncio
async def test_provider_returns_none_parsed_on_off_schema_json(monkeypatch):
    fake_generate = AsyncMock(return_value=_fake_response(json.dumps({"totally": "wrong shape"}), 10, 1))
    monkeypatch.setattr(
        "app.providers.google_genai.genai.Client",
        lambda **kwargs: SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate))),
    )

    provider = GeminiProvider()
    result = await provider.complete("system prompt", "user prompt", Greeting)

    assert result.parsed is None


# --------------------------------------------------------------------------
# factory.py
# --------------------------------------------------------------------------


def test_factory_selects_gemini(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    assert isinstance(get_provider(), GeminiProvider)


def test_factory_selects_vertex(monkeypatch):
    # Vertex client construction resolves ADC when project/location are
    # unset, so give it explicit values (as a real deployment would via env
    # vars) rather than exercising ADC discovery in a unit test.
    monkeypatch.setattr(settings, "llm_provider", "vertex")
    monkeypatch.setattr(settings, "google_cloud_project", "my-project")
    monkeypatch.setattr(settings, "google_cloud_location", "us-central1")
    assert isinstance(get_provider(), VertexProvider)


def test_factory_raises_not_implemented_for_groq(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "groq")
    with pytest.raises(NotImplementedError):
        get_provider()


def test_factory_raises_for_unknown_provider(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "bogus")
    with pytest.raises(ValueError):
        get_provider()


# --------------------------------------------------------------------------
# validate.py — validate-and-repair
# --------------------------------------------------------------------------


class FakeProvider:
    """A scripted LLMProvider: returns each entry in `responses`, in order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system, user, schema):
        self.calls.append((system, user))
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_validate_and_repair_succeeds_on_first_try():
    from app.providers.base import LLMResponse

    provider = FakeProvider([LLMResponse(raw_text='{"message": "hi"}', tokens_in=5, tokens_out=2, parsed=Greeting(message="hi"))])

    result = await validate_and_repair(provider, "sys", "usr", Greeting)

    assert result.ok is True
    assert result.parsed == Greeting(message="hi")
    assert result.tokens_in == 5
    assert result.tokens_out == 2
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_validate_and_repair_retries_once_then_succeeds():
    from app.providers.base import LLMResponse

    provider = FakeProvider(
        [
            LLMResponse(raw_text="garbage", tokens_in=10, tokens_out=1, parsed=None),
            LLMResponse(raw_text='{"message": "fixed"}', tokens_in=8, tokens_out=3, parsed=Greeting(message="fixed")),
        ]
    )

    result = await validate_and_repair(provider, "sys", "usr", Greeting)

    assert result.ok is True
    assert result.parsed == Greeting(message="fixed")
    assert result.tokens_in == 18  # accumulated across both attempts
    assert result.tokens_out == 4
    assert len(provider.calls) == 2
    # repair prompt should differ from the original system prompt
    assert provider.calls[1][0] != provider.calls[0][0]
    assert "sys" in provider.calls[1][0]


@pytest.mark.asyncio
async def test_validate_and_repair_fails_after_repair_also_fails():
    from app.providers.base import LLMResponse

    provider = FakeProvider(
        [
            LLMResponse(raw_text="garbage", tokens_in=10, tokens_out=1, parsed=None),
            LLMResponse(raw_text="still garbage", tokens_in=8, tokens_out=1, parsed=None),
        ]
    )

    result = await validate_and_repair(provider, "sys", "usr", Greeting)

    assert result.ok is False
    assert result.parsed is None
    assert result.error is not None
    assert result.tokens_in == 18
    assert result.tokens_out == 2
    assert len(provider.calls) == 2


# --------------------------------------------------------------------------
# pricing.py
# --------------------------------------------------------------------------


def test_estimate_cost_usd_gemini_flash():
    cost = pricing.estimate_cost_usd("gemini", "gemini-flash-latest", tokens_in=4_000, tokens_out=500)
    # 4000/1e6 * 0.30 + 500/1e6 * 2.50
    assert cost == pytest.approx(0.0012 + 0.00125)


def test_estimate_cost_usd_unknown_model_raises():
    with pytest.raises(KeyError):
        pricing.estimate_cost_usd("gemini", "no-such-model", tokens_in=1, tokens_out=1)


def test_estimate_cost_usd_groq_placeholder_raises():
    with pytest.raises(NotImplementedError):
        pricing.estimate_cost_usd("groq", "llama-whatever", tokens_in=1, tokens_out=1)
