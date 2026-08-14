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


@pytest.fixture(autouse=True)
def _reset_provider_cache():
    from app.providers.factory import reset_provider_cache

    reset_provider_cache()
    yield
    reset_provider_cache()


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
        lambda **kwargs: SimpleNamespace(
            aio=SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate))
        ),
    )

    provider = GeminiProvider(api_key="dummy-key-for-construction-only")
    result = await provider.complete("system prompt", "user prompt", Greeting)

    assert result.parsed == Greeting(message="hi")
    assert result.tokens_in == 42
    assert result.tokens_out == 7
    fake_generate.assert_awaited_once()
    _, kwargs = fake_generate.call_args
    assert kwargs["model"] == settings.llm_model


@pytest.mark.asyncio
async def test_provider_returns_none_parsed_on_malformed_json(monkeypatch):
    fake_generate = AsyncMock(return_value=_fake_response("not json at all", 10, 1))
    monkeypatch.setattr(
        "app.providers.google_genai.genai.Client",
        lambda **kwargs: SimpleNamespace(
            aio=SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate))
        ),
    )

    provider = GeminiProvider(api_key="dummy-key-for-construction-only")
    result = await provider.complete("system prompt", "user prompt", Greeting)

    assert result.parsed is None
    assert result.tokens_in == 10
    assert result.tokens_out == 1


@pytest.mark.asyncio
async def test_provider_returns_none_parsed_on_off_schema_json(monkeypatch):
    fake_generate = AsyncMock(
        return_value=_fake_response(json.dumps({"totally": "wrong shape"}), 10, 1)
    )
    monkeypatch.setattr(
        "app.providers.google_genai.genai.Client",
        lambda **kwargs: SimpleNamespace(
            aio=SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate))
        ),
    )

    provider = GeminiProvider(api_key="dummy-key-for-construction-only")
    result = await provider.complete("system prompt", "user prompt", Greeting)

    assert result.parsed is None


def _fake_client_factory(captured: dict, fake_generate):
    def _build(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            aio=SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate))
        )

    return _build


@pytest.mark.asyncio
async def test_vertex_provider_parses_valid_structured_output(monkeypatch):
    """Same call, same parsing, same usage accounting as gemini -- the only
    difference between the two adapters is how the client is authenticated."""
    captured: dict = {}
    fake_generate = AsyncMock(return_value=_fake_response(json.dumps({"message": "hi"}), 42, 7))
    monkeypatch.setattr(
        "app.providers.google_genai.genai.Client",
        _fake_client_factory(captured, fake_generate),
    )

    provider = VertexProvider(project="proj-x", location="us-central1", service_account_info=None)
    result = await provider.complete("system prompt", "user prompt", Greeting)

    assert result.parsed == Greeting(message="hi")
    assert result.tokens_in == 42
    assert result.tokens_out == 7
    assert captured["vertexai"] is True
    assert captured["project"] == "proj-x"
    assert captured["location"] == "us-central1"
    _, kwargs = fake_generate.call_args
    assert kwargs["model"] == settings.llm_model


def test_vertex_provider_passes_no_credentials_for_implicit_adc(monkeypatch):
    """credentials=None is genai.Client's own default, and passing it
    explicitly is identical to omitting it -- which is exactly what makes
    google-auth discover the local ADC file."""
    captured: dict = {}
    monkeypatch.setattr(
        "app.providers.google_genai.genai.Client",
        _fake_client_factory(captured, AsyncMock()),
    )

    VertexProvider(project="proj-x", location="us-central1", service_account_info=None)

    assert captured["credentials"] is None


def test_vertex_provider_builds_credentials_from_the_service_account_info(monkeypatch):
    """from_service_account_info is mocked: a real one needs a real RSA private
    key, and this test is about the wiring, not about google-auth's parsing."""
    captured: dict = {}
    sentinel = object()
    seen: dict = {}
    monkeypatch.setattr(
        "app.providers.google_genai.genai.Client",
        _fake_client_factory(captured, AsyncMock()),
    )
    monkeypatch.setattr(
        "app.providers.google_genai.service_account.Credentials.from_service_account_info",
        lambda info: seen.update(info) or sentinel,
    )

    info = {"type": "service_account", "project_id": "proj-x"}
    VertexProvider(project="proj-x", location="us-central1", service_account_info=info)

    assert captured["credentials"] is sentinel
    assert seen == info


# --------------------------------------------------------------------------
# factory.py
# --------------------------------------------------------------------------


def test_factory_selects_gemini(monkeypatch):
    # google-genai's Client raises immediately on an empty api_key, so a
    # fresh checkout with no real .env (e.g. CI) needs a dummy non-empty
    # value here — this test must not depend on real credentials existing.
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "dummy-key-for-construction-only")
    assert isinstance(get_provider(), GeminiProvider)


def test_factory_selects_groq(monkeypatch):
    # _build()'s new empty-credential check (added by this task) now raises
    # before construction, so this test needs a non-empty value — previously an
    # empty string reached GroqProvider.__init__ successfully, since Groq's SDK
    # only rejects api_key=None, not an empty string.
    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "dummy-key-for-construction-only")
    from app.providers.groq import GroqProvider

    assert isinstance(get_provider(), GroqProvider)


def test_factory_raises_for_unknown_provider(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "bogus")
    with pytest.raises(ValueError):
        get_provider()


def test_factory_raises_a_clear_error_for_an_unprovisioned_key_index(monkeypatch):
    """The locally-detectable invalid-state case: activating gemini at index 1
    when only GEMINI_API_KEY (index 0) exists anywhere. Distinct from a
    dead-but-configured provider (a real credential for a vendor that's down
    or retired), which must NOT be affected by this check -- that case has a
    real, non-empty credential and fails at the live call, unchanged."""
    from app.providers import key_index
    from app.providers.factory import reset_provider_cache

    monkeypatch.setattr(settings, "llm_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "gk_index_0")
    monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
    reset_provider_cache()
    key_index.set_override_cache({"gemini": 1})

    with pytest.raises(ValueError) as exc:
        get_provider()
    assert "GEMINI_API_KEY_1" in str(exc.value)
    assert "gemini" in str(exc.value)
    assert "1" in str(exc.value)

    key_index.reset_override_cache()
    reset_provider_cache()


def test_factory_unaffected_by_a_dead_but_configured_provider(monkeypatch):
    """A real, non-empty credential must still reach client construction --
    this check only catches an EMPTY resolved value, nothing else."""
    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_real_but_dead")
    from app.providers.groq import GroqProvider

    assert isinstance(get_provider(), GroqProvider)


def test_factory_returns_the_same_instance_on_repeated_calls(monkeypatch):
    from app.providers.factory import reset_provider_cache

    monkeypatch.setattr(settings, "llm_provider", "groq")
    # _build()'s empty-credential check requires a non-empty api_key.
    monkeypatch.setattr(settings, "groq_api_key", "dummy-key-for-construction-only")
    reset_provider_cache()
    first = get_provider()
    second = get_provider()
    assert first is second
    reset_provider_cache()


def test_factory_rebuilds_the_client_when_the_key_index_changes(monkeypatch):
    from app.providers import key_index
    from app.providers.factory import reset_provider_cache

    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_index_0")
    monkeypatch.setenv("GROQ_API_KEY_1", "gsk_index_1")
    reset_provider_cache()
    key_index.reset_override_cache()

    at_index_0 = get_provider()
    key_index.set_override_cache({"groq": 1})
    at_index_1 = get_provider()

    assert at_index_0 is not at_index_1
    key_index.reset_override_cache()
    reset_provider_cache()


def test_factory_returns_to_the_original_cached_instance_after_switching_back(monkeypatch):
    from app.providers import key_index
    from app.providers.factory import reset_provider_cache

    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_index_0")
    monkeypatch.setenv("GROQ_API_KEY_1", "gsk_index_1")
    reset_provider_cache()
    key_index.reset_override_cache()

    at_index_0 = get_provider()
    key_index.set_override_cache({"groq": 1})
    get_provider()
    key_index.reset_override_cache()
    back_at_index_0 = get_provider()

    assert back_at_index_0 is at_index_0
    key_index.reset_override_cache()
    reset_provider_cache()


def _mock_vertex_client(monkeypatch, captured: dict | None = None):
    """genai.Client would otherwise try to authenticate for real at
    construction; these tests are about _build's own branching. Pass a dict to
    capture the kwargs it was constructed with."""

    def _build(**kwargs):
        if captured is not None:
            captured.update(kwargs)
        return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace()))

    monkeypatch.setattr("app.providers.google_genai.genai.Client", _build)


def test_factory_selects_vertex_and_derives_the_project_from_the_key(monkeypatch):
    """GCP_PROJECT unset is the COMMON case: an operator handed nothing but a
    service-account JSON key gets the project from the key's own project_id."""
    from app.providers.google_genai import VertexProvider

    captured: dict = {}
    _mock_vertex_client(monkeypatch, captured)
    monkeypatch.setattr(settings, "llm_provider", "vertex")
    monkeypatch.setattr(settings, "gcp_project", "")
    monkeypatch.setattr(settings, "gcp_location", "us-central1")
    monkeypatch.setattr(
        "app.providers.factory.vertex_credentials.resolve_service_account_info",
        lambda index: {"type": "service_account", "project_id": "proj-from-key"},
    )
    monkeypatch.setattr(
        "app.providers.google_genai.service_account.Credentials.from_service_account_info",
        lambda info: object(),
    )

    assert isinstance(get_provider(), VertexProvider)
    assert captured["project"] == "proj-from-key"
    assert captured["location"] == "us-central1"


def test_factory_prefers_an_explicit_gcp_project_over_the_keys_own(monkeypatch):
    """GCP_PROJECT still exists as an override -- for pointing a key at a
    different project than the one it was minted in."""
    captured: dict = {}
    _mock_vertex_client(monkeypatch, captured)
    monkeypatch.setattr(settings, "llm_provider", "vertex")
    monkeypatch.setattr(settings, "gcp_project", "proj-explicit")
    monkeypatch.setattr(
        "app.providers.factory.vertex_credentials.resolve_service_account_info",
        lambda index: {"type": "service_account", "project_id": "proj-from-key"},
    )
    monkeypatch.setattr(
        "app.providers.google_genai.service_account.Credentials.from_service_account_info",
        lambda info: object(),
    )

    get_provider()
    assert captured["project"] == "proj-explicit"


def test_factory_builds_vertex_from_implicit_adc_when_a_project_is_set(monkeypatch):
    """The one behavioral difference from gemini/groq worth its own test: an
    EMPTY resolved credential is not an error for vertex. _build must not
    raise -- any failure then comes from the SDK/google-auth relying on
    implicit ADC, which is a live-call concern, not a config one."""
    from app.providers.google_genai import VertexProvider

    _mock_vertex_client(monkeypatch)
    monkeypatch.setattr(settings, "llm_provider", "vertex")
    monkeypatch.setattr(settings, "gcp_project", "proj-explicit")
    monkeypatch.setattr(
        "app.providers.factory.vertex_credentials.resolve_service_account_info",
        lambda index: None,
    )

    assert isinstance(get_provider(), VertexProvider)


def test_factory_raises_when_vertex_has_neither_a_project_nor_a_credential(monkeypatch):
    """Pure implicit-ADC with no key to derive a project from: locally
    detectable, so it must fast-fail before any network call rather than let
    three specialists each discover the same problem the expensive way."""
    _mock_vertex_client(monkeypatch)
    monkeypatch.setattr(settings, "llm_provider", "vertex")
    monkeypatch.setattr(settings, "gcp_project", "")
    monkeypatch.setattr(
        "app.providers.factory.vertex_credentials.resolve_service_account_info",
        lambda index: None,
    )

    with pytest.raises(ValueError) as exc:
        get_provider()
    assert "vertex" in str(exc.value)
    assert "GCP_PROJECT" in str(exc.value)


def test_factory_passes_the_active_key_index_to_vertex_credentials(monkeypatch):
    """vertex rides the same key-index override as gemini/groq -- the index
    must reach the credential resolver, or a slot swap would be a silent
    no-op for this provider alone."""
    from app.providers import key_index
    from app.providers.factory import reset_provider_cache

    _mock_vertex_client(monkeypatch)
    seen: list[int] = []
    monkeypatch.setattr(settings, "llm_provider", "vertex")
    monkeypatch.setattr(settings, "gcp_project", "proj-explicit")
    monkeypatch.setattr(
        "app.providers.factory.vertex_credentials.resolve_service_account_info",
        lambda index: seen.append(index) or None,
    )
    reset_provider_cache()
    key_index.set_override_cache({"vertex": 2})

    get_provider()
    assert seen == [2]

    key_index.reset_override_cache()
    reset_provider_cache()


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

    provider = FakeProvider(
        [
            LLMResponse(
                raw_text='{"message": "hi"}',
                tokens_in=5,
                tokens_out=2,
                parsed=Greeting(message="hi"),
            )
        ]
    )

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
            LLMResponse(
                raw_text='{"message": "fixed"}',
                tokens_in=8,
                tokens_out=3,
                parsed=Greeting(message="fixed"),
            ),
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
    cost = pricing.estimate_cost_usd(
        "gemini", "gemini-flash-latest", tokens_in=4_000, tokens_out=500
    )
    # 4000/1e6 * 0.30 + 500/1e6 * 2.50
    assert cost == pytest.approx(0.0012 + 0.00125)


def test_estimate_cost_usd_unknown_model_raises():
    with pytest.raises(KeyError):
        pricing.estimate_cost_usd("gemini", "no-such-model", tokens_in=1, tokens_out=1)


def test_estimate_cost_usd_groq_llama():
    cost = pricing.estimate_cost_usd(
        "groq", "llama-3.3-70b-versatile", tokens_in=4_000, tokens_out=500
    )
    # 4000/1e6 * 0.59 + 500/1e6 * 0.79
    assert cost == pytest.approx(0.00236 + 0.000395)


def test_estimate_cost_usd_groq_unknown_model_raises():
    with pytest.raises(KeyError):
        pricing.estimate_cost_usd("groq", "no-such-model", tokens_in=1, tokens_out=1)


def test_estimate_cost_usd_vertex_flash():
    """Same model, same published rate as AI-Studio's paid tier -- the two
    providers differ in the auth path, not in what a token costs."""
    cost = pricing.estimate_cost_usd(
        "vertex", "gemini-flash-latest", tokens_in=4_000, tokens_out=500
    )
    assert cost == pytest.approx(0.0012 + 0.00125)
