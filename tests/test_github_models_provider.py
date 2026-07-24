"""Deterministic tests for app/providers/github_models.py.

Mocks the actual `openai.AsyncOpenAI` client boundary
(`client.chat.completions.create`) so nothing here ever makes a real network
call. Live verification happens separately in
scripts/manual_verify_github_models.py.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from app.config import settings
from app.providers.github_models import GitHubModelsProvider, _response_format


class Greeting(BaseModel):
    message: str


class Item(BaseModel):
    name: str
    count: int


class Container(BaseModel):
    """A schema with a nested $defs model, like our real specialist
    container schemas (e.g. SecurityFindings wrapping SecurityFinding).
    """

    items: list[Item]


def _fake_response(content: str, prompt_tokens: int = 10, completion_tokens: int = 5):
    """Build a stand-in for an OpenAI-compatible ChatCompletion response."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


@pytest.mark.asyncio
async def test_github_models_provider_parses_valid_structured_output(monkeypatch):
    fake_create = AsyncMock(return_value=_fake_response(json.dumps({"message": "hi"}), 42, 7))
    monkeypatch.setattr(
        "app.providers.github_models.AsyncOpenAI",
        lambda **kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
        ),
    )

    provider = GitHubModelsProvider()
    result = await provider.complete("system prompt", "user prompt", Greeting)

    assert result.parsed == Greeting(message="hi")
    assert result.tokens_in == 42
    assert result.tokens_out == 7
    fake_create.assert_awaited_once()
    _, kwargs = fake_create.call_args
    assert kwargs["model"] == settings.github_models_model
    assert kwargs["response_format"]["type"] == "json_schema"
    assert kwargs["response_format"]["json_schema"]["strict"] is True
    # OpenAI's strict json_schema mode requires this explicitly — Pydantic's
    # model_json_schema() doesn't set it by default (caught via a live 400).
    assert kwargs["response_format"]["json_schema"]["schema"]["additionalProperties"] is False


@pytest.mark.asyncio
async def test_github_models_provider_returns_none_parsed_on_malformed_json(monkeypatch):
    fake_create = AsyncMock(return_value=_fake_response("not json at all", 10, 1))
    monkeypatch.setattr(
        "app.providers.github_models.AsyncOpenAI",
        lambda **kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
        ),
    )

    provider = GitHubModelsProvider()
    result = await provider.complete("system prompt", "user prompt", Greeting)

    assert result.parsed is None
    assert result.tokens_in == 10
    assert result.tokens_out == 1


@pytest.mark.asyncio
async def test_github_models_provider_returns_none_parsed_on_off_schema_json(monkeypatch):
    fake_create = AsyncMock(return_value=_fake_response(json.dumps({"totally": "wrong shape"}), 10, 1))
    monkeypatch.setattr(
        "app.providers.github_models.AsyncOpenAI",
        lambda **kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
        ),
    )

    provider = GitHubModelsProvider()
    result = await provider.complete("system prompt", "user prompt", Greeting)

    assert result.parsed is None


def test_response_format_sets_additional_properties_false_on_nested_defs():
    """OpenAI's strict json_schema mode requires additionalProperties: false
    on EVERY object schema, including nested $defs entries — a real live 400
    caught this when a container schema (list[SomeModel]) was used, not just
    the top-level flat-schema case.
    """
    fmt = _response_format(Container)
    schema = fmt["json_schema"]["schema"]

    assert schema["additionalProperties"] is False
    nested = schema["$defs"]["Item"]
    assert nested["additionalProperties"] is False


@pytest.mark.asyncio
async def test_github_models_provider_sends_system_and_user_messages(monkeypatch):
    fake_create = AsyncMock(return_value=_fake_response(json.dumps({"message": "hi"})))
    monkeypatch.setattr(
        "app.providers.github_models.AsyncOpenAI",
        lambda **kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
        ),
    )

    provider = GitHubModelsProvider()
    await provider.complete("Be a helpful reviewer.", "user prompt", Greeting)

    _, kwargs = fake_create.call_args
    assert kwargs["messages"] == [
        {"role": "system", "content": "Be a helpful reviewer."},
        {"role": "user", "content": "user prompt"},
    ]
