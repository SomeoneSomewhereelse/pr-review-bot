"""Tests for onboarding/llm_client.py's Gemini and Vertex model listing.
Both share google-genai's SDK, whose transport mixes httpx and requests
depending on auth type (google.auth.transport.requests.AuthorizedSession
specifically backs the Vertex/ADC-style credential path — verified by
reading google/genai/_api_client.py directly during this sub-project's
brainstorm), so a single respx mock cannot cleanly cover both code paths.
Tests mock at the SDK client boundary instead — genai.Client itself is
monkeypatched with a fake that records constructor kwargs and returns a
fake async model pager. See
docs/superpowers/specs/2026-08-27-onboarding-llm-provider-frame-design.md
sections 3-4, 6."""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace

from google.auth import exceptions as google_auth_exceptions
from google.genai import errors as genai_errors

from onboarding import llm_client

_SENTINEL_SERVICE_ACCOUNT = {
    "type": "service_account",
    "project_id": "sentinel-project",
    "private_key": "-----BEGIN PRIVATE KEY-----\nMIIEvAIBADANBgkqhkiG9w0BAQEFAASCBKYwggSiAgEAAoIBAQDiQlbTqoKFep6U\n5C6UsJ1B5U/Q8PNBimMiHQCoBqRIspSve5A0MmRmYsnn4UPEivDelDEqBIFpPwUK\nLxnliVnCKbKpV3RJE/HAxVNGYRj6MfGaPw2rZhe9/W8sBMHIaldkrYj7AOgtkqcw\nKingtiXdofYQqWNEEeqFIpuL2APoXYotJAGNoDQuNW/86eNHvMapZ3FU6XCPIc40\n3Y2tPuaYAckm9pQ5wt4MV5/suvfBh3ftEzaITue1Kix5F+qChDQYKLfL5JoQTie9\nSvvRXDMIYsl+IRKiInG8XzX1TQPmvNs+u2bjBKWW4OwdAACZ9iUCgsG7CkQQzs5R\nnr3m2ZF9AgMBAAECggEADXWJybSSaBNHvK6oMLMi36ke6txyc/sh84ULJXOjsSli\nW9/7T4eR3l9RCGinidkEBBGHrSqwcgzMJXNw1G0ruDeXx6gKpFA56NA0KHMdM8Dl\n0NmgXApKLkSVqOYtitj8kuIZzGic5x0asexIKnRbY0g/pXUWERYJv9qzqwlyDg/p\nxL2hc/VayovI74NAk4hTr7h++uc0QeIHHuxch1xCh9VywmS9pplF7cDNxWuVuiVr\nysaQP7cOIR0E+Iwn3+tx1YqlaKrZykDJ5kd2FOY0MEFLKyDJ7yzuspar4I3CTLYO\n8/Bzm7D24KtfDCh4vADK0dNE3a/L0hA6/Ai9gr3QxwKBgQD2SvXT41DgYazFgxj3\nrT/PuDvnZcniUmKzOrJT2YecNECLlSPqXJ8Z7AGKE61kixmOFxE16pZFES3Ntb82\nSpXap22eKx8X4h86pEwn3DAuIC+I2cPI4o4VzgwMudlMBnfOqVKUUrIV85eE7kzz\nVJhWntvC/sEY9ED47rJ9P37nFwKBgQDrLT5aNshd4qAkkBCLjD6y18+Xw3FuUVjU\ndHDNbiTlD3k7N01K2vHkCdSOl+vpUZr/VCmIKnT59zVDtESMrrNMeN/xMJixruJR\nASKG14E/jLpue3UFuG4/h2bMKxeZHQU19BtEfEG2kbRkW1nqu27CfvazjO4F67pR\nvbEVk+2oiwKBgEHK5n5y0/EMxp2AltPa+RfhLEd1Pofx4CHmxSp3Cq3km3VuIskB\ncxL2o7ah6QjZy7rUWKmhgAD1RNoV+f1j0UI2xaah+E1l/1en+hwPyuMXf/s7yPxJ\n4RDcGQXxQ6X2eFzBiKjMqnwItWoySmYaLBO/ng8qBKVI4m5dPVsN8jWDAoGAA+o9\n5nyQ+1cheVpYnCoahRmooAsl4UNDak4B7rmNra6DQyQZikx4yGYNfs4ypDCyltuM\n0XJ7fgnKfjULCxiBbZ15hOddM2AI7nZJX9tIkIlENUCi4xR96VrUsENrYiYkhxBo\nP8ydv29PhHgs2AaEwoIgkz6eW8Tf1iqFPym2RB8CgYAeQg8vQ8Ao3nG+k83gobGn\nqebO0zbGjgNtVDnQuK6bbG/duJx5jTZa8DX8s5EDkUdUEY18504qbMjgOHb2oE5f\nRrIR0HeDeneKiRy/ssLXT5JZnTZs/t3hrUiCGk540z2KgfUPOIqyhEEaYefHFNfS\n16Pbqd20zlzDHeCuO/Fr+w==\n-----END PRIVATE KEY-----\n",
    "client_email": "sentinel@sentinel-project.iam.gserviceaccount.com",
    "token_uri": "https://oauth2.googleapis.com/token",
}


def _b64(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


def _model(name, supported_actions=("generateContent",)):
    return SimpleNamespace(name=name, supported_actions=list(supported_actions))


class _FakeModelPager:
    def __init__(self, models):
        self._models = models

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for m in self._models:
            yield m


class _FakeModelsResource:
    def __init__(self, models=None, exc=None):
        self._models = models or []
        self._exc = exc

    async def list(self, **kwargs):
        if self._exc:
            raise self._exc
        return _FakeModelPager(self._models)


class _FakeAio:
    def __init__(self, models=None, exc=None):
        self.models = _FakeModelsResource(models, exc)


class _FakeClient:
    """Records constructor kwargs so tests can assert genai.Client() was
    built correctly (api_key vs vertexai=True+project+location+credentials)."""

    last_kwargs: dict = {}
    _next_models: list = []
    _next_exc: Exception | None = None

    def __init__(self, **kwargs):
        _FakeClient.last_kwargs = kwargs
        self.aio = _FakeAio(models=_FakeClient._next_models, exc=_FakeClient._next_exc)


def _install_fake_client(monkeypatch, models=None, exc=None):
    _FakeClient._next_models = models or []
    _FakeClient._next_exc = exc
    monkeypatch.setattr(llm_client.genai, "Client", _FakeClient)


async def test_list_gemini_models_returns_stripped_names(monkeypatch):
    _install_fake_client(monkeypatch, models=[
        _model("models/gemini-flash-latest"),
        _model("models/gemini-2.5-pro"),
    ])
    result = await llm_client.list_gemini_models("sentinel-api-key")
    assert result == llm_client.LlmModelsListed(models=["gemini-flash-latest", "gemini-2.5-pro"])


async def test_list_gemini_models_constructs_client_with_api_key(monkeypatch):
    _install_fake_client(monkeypatch, models=[])
    await llm_client.list_gemini_models("sentinel-api-key")
    assert _FakeClient.last_kwargs == {"api_key": "sentinel-api-key"}


async def test_list_gemini_models_filters_out_non_generate_content_models(monkeypatch):
    _install_fake_client(monkeypatch, models=[
        _model("models/gemini-flash-latest", supported_actions=["generateContent"]),
        _model("models/embedding-001", supported_actions=["embedContent"]),
        _model("models/no-actions", supported_actions=[]),
    ])
    result = await llm_client.list_gemini_models("a")
    assert result == llm_client.LlmModelsListed(models=["gemini-flash-latest"])


async def test_list_gemini_models_unauthorized(monkeypatch):
    _install_fake_client(monkeypatch, exc=genai_errors.ClientError(401, {"message": "bad key"}))
    result = await llm_client.list_gemini_models("bad")
    assert result == llm_client.LlmApiFailed(reason="unauthorized")


async def test_list_gemini_models_forbidden(monkeypatch):
    _install_fake_client(monkeypatch, exc=genai_errors.ClientError(403, {"message": "forbidden"}))
    result = await llm_client.list_gemini_models("a")
    assert result == llm_client.LlmApiFailed(reason="forbidden")


async def test_list_gemini_models_rate_limited(monkeypatch):
    _install_fake_client(monkeypatch, exc=genai_errors.ClientError(429, {"message": "slow down"}))
    result = await llm_client.list_gemini_models("a")
    assert result == llm_client.LlmApiFailed(reason="rate_limited")


async def test_list_gemini_models_other_client_error_is_unreachable(monkeypatch):
    _install_fake_client(monkeypatch, exc=genai_errors.ClientError(400, {"message": "bad request"}))
    result = await llm_client.list_gemini_models("a")
    assert result == llm_client.LlmApiFailed(reason="provider_unreachable")


async def test_list_gemini_models_server_error_is_unreachable(monkeypatch):
    _install_fake_client(monkeypatch, exc=genai_errors.ServerError(500, {"message": "oops"}))
    result = await llm_client.list_gemini_models("a")
    assert result == llm_client.LlmApiFailed(reason="provider_unreachable")


async def test_list_vertex_models_returns_stripped_names_and_project_id(monkeypatch):
    _install_fake_client(monkeypatch, models=[_model("publishers/google/models/gemini-2.5-flash")])
    result = await llm_client.list_vertex_models(_b64(_SENTINEL_SERVICE_ACCOUNT))
    assert result == llm_client.VertexModelsListed(project_id="sentinel-project", models=["gemini-2.5-flash"])


async def test_list_vertex_models_constructs_client_with_project_and_fixed_location(monkeypatch):
    _install_fake_client(monkeypatch, models=[])
    await llm_client.list_vertex_models(_b64(_SENTINEL_SERVICE_ACCOUNT))
    assert _FakeClient.last_kwargs["vertexai"] is True
    assert _FakeClient.last_kwargs["project"] == "sentinel-project"
    assert _FakeClient.last_kwargs["location"] == "us-central1"


async def test_list_vertex_models_malformed_base64_is_invalid_service_account_json():
    result = await llm_client.list_vertex_models("not-valid-base64!!!")
    assert result == llm_client.LlmApiFailed(reason="invalid_service_account_json")


async def test_list_vertex_models_valid_base64_but_not_json_is_invalid_service_account_json():
    result = await llm_client.list_vertex_models(base64.b64encode(b"not json").decode())
    assert result == llm_client.LlmApiFailed(reason="invalid_service_account_json")


async def test_list_vertex_models_missing_project_id_is_invalid_service_account_json():
    bad = dict(_SENTINEL_SERVICE_ACCOUNT)
    del bad["project_id"]
    result = await llm_client.list_vertex_models(_b64(bad))
    assert result == llm_client.LlmApiFailed(reason="invalid_service_account_json")


async def test_list_vertex_models_auth_error_is_unauthorized(monkeypatch):
    _install_fake_client(monkeypatch, exc=google_auth_exceptions.RefreshError("bad credentials"))
    result = await llm_client.list_vertex_models(_b64(_SENTINEL_SERVICE_ACCOUNT))
    assert result == llm_client.LlmApiFailed(reason="unauthorized")


async def test_list_vertex_models_forbidden(monkeypatch):
    _install_fake_client(monkeypatch, exc=genai_errors.ClientError(403, {"message": "no vertex ai role"}))
    result = await llm_client.list_vertex_models(_b64(_SENTINEL_SERVICE_ACCOUNT))
    assert result == llm_client.LlmApiFailed(reason="forbidden")


async def test_list_vertex_models_server_error_is_unreachable(monkeypatch):
    _install_fake_client(monkeypatch, exc=genai_errors.ServerError(503, {"message": "oops"}))
    result = await llm_client.list_vertex_models(_b64(_SENTINEL_SERVICE_ACCOUNT))
    assert result == llm_client.LlmApiFailed(reason="provider_unreachable")


async def test_list_vertex_models_never_logs_the_decoded_key(caplog):
    with caplog.at_level("DEBUG"):
        await llm_client.list_vertex_models("not-valid-base64!!!")
    assert "sentinel" not in caplog.text.lower()
