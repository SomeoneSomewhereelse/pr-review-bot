"""Tests for onboarding/llm_client.py's Gemini and Vertex model listing.
Both share google-genai's SDK. The async listing call itself is httpx-based
for both providers (verified: this environment has no aiohttp installed,
so google-genai's async path falls back to httpx regardless of auth type).
What a single respx mock can't cover is Vertex's separate credential step:
a service-account refreshes its access token via google.auth's synchronous,
requests-based transport (google.auth.transport.requests.AuthorizedSession)
before the httpx listing call ever happens, and respx only intercepts
httpx. Tests mock at the SDK client boundary instead — genai.Client itself
is monkeypatched with a fake that records constructor kwargs and returns a
fake async model pager. See
docs/superpowers/specs/2026-08-27-onboarding-llm-provider-frame-design.md
sections 3-4, 6."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import httpx
import respx
from google.auth import exceptions as google_auth_exceptions
from google.genai import errors as genai_errors

from onboarding import llm_client

# The private key below is a locally-generated throwaway RSA key used only
# for local signing in these tests -- every HTTP call is mocked below, so
# nothing is ever sent anywhere real with it (same shape as
# test_onboarding_github_client.py's _throwaway_key_material() fixture).
_SENTINEL_SERVICE_ACCOUNT = {
    "type": "service_account",
    "project_id": "sentinel-project",
    "private_key": (
        "-----BEGIN PRIVATE KEY-----\n"
        "MIIEvAIBADANBgkqhkiG9w0BAQEFAASCBKYwggSiAgEAAoIBAQDiQlbTqoKFep6U\n"
        "5C6UsJ1B5U/Q8PNBimMiHQCoBqRIspSve5A0MmRmYsnn4UPEivDelDEqBIFpPwUK\n"
        "LxnliVnCKbKpV3RJE/HAxVNGYRj6MfGaPw2rZhe9/W8sBMHIaldkrYj7AOgtkqcw\n"
        "KingtiXdofYQqWNEEeqFIpuL2APoXYotJAGNoDQuNW/86eNHvMapZ3FU6XCPIc40\n"
        "3Y2tPuaYAckm9pQ5wt4MV5/suvfBh3ftEzaITue1Kix5F+qChDQYKLfL5JoQTie9\n"
        "SvvRXDMIYsl+IRKiInG8XzX1TQPmvNs+u2bjBKWW4OwdAACZ9iUCgsG7CkQQzs5R\n"
        "nr3m2ZF9AgMBAAECggEADXWJybSSaBNHvK6oMLMi36ke6txyc/sh84ULJXOjsSli\n"
        "W9/7T4eR3l9RCGinidkEBBGHrSqwcgzMJXNw1G0ruDeXx6gKpFA56NA0KHMdM8Dl\n"
        "0NmgXApKLkSVqOYtitj8kuIZzGic5x0asexIKnRbY0g/pXUWERYJv9qzqwlyDg/p\n"
        "xL2hc/VayovI74NAk4hTr7h++uc0QeIHHuxch1xCh9VywmS9pplF7cDNxWuVuiVr\n"
        "ysaQP7cOIR0E+Iwn3+tx1YqlaKrZykDJ5kd2FOY0MEFLKyDJ7yzuspar4I3CTLYO\n"
        "8/Bzm7D24KtfDCh4vADK0dNE3a/L0hA6/Ai9gr3QxwKBgQD2SvXT41DgYazFgxj3\n"
        "rT/PuDvnZcniUmKzOrJT2YecNECLlSPqXJ8Z7AGKE61kixmOFxE16pZFES3Ntb82\n"
        "SpXap22eKx8X4h86pEwn3DAuIC+I2cPI4o4VzgwMudlMBnfOqVKUUrIV85eE7kzz\n"
        "VJhWntvC/sEY9ED47rJ9P37nFwKBgQDrLT5aNshd4qAkkBCLjD6y18+Xw3FuUVjU\n"
        "dHDNbiTlD3k7N01K2vHkCdSOl+vpUZr/VCmIKnT59zVDtESMrrNMeN/xMJixruJR\n"
        "ASKG14E/jLpue3UFuG4/h2bMKxeZHQU19BtEfEG2kbRkW1nqu27CfvazjO4F67pR\n"
        "vbEVk+2oiwKBgEHK5n5y0/EMxp2AltPa+RfhLEd1Pofx4CHmxSp3Cq3km3VuIskB\n"
        "cxL2o7ah6QjZy7rUWKmhgAD1RNoV+f1j0UI2xaah+E1l/1en+hwPyuMXf/s7yPxJ\n"
        "4RDcGQXxQ6X2eFzBiKjMqnwItWoySmYaLBO/ng8qBKVI4m5dPVsN8jWDAoGAA+o9\n"
        "5nyQ+1cheVpYnCoahRmooAsl4UNDak4B7rmNra6DQyQZikx4yGYNfs4ypDCyltuM\n"
        "0XJ7fgnKfjULCxiBbZ15hOddM2AI7nZJX9tIkIlENUCi4xR96VrUsENrYiYkhxBo\n"
        "P8ydv29PhHgs2AaEwoIgkz6eW8Tf1iqFPym2RB8CgYAeQg8vQ8Ao3nG+k83gobGn\n"
        "qebO0zbGjgNtVDnQuK6bbG/duJx5jTZa8DX8s5EDkUdUEY18504qbMjgOHb2oE5f\n"
        "RrIR0HeDeneKiRy/ssLXT5JZnTZs/t3hrUiCGk540z2KgfUPOIqyhEEaYefHFNfS\n"
        "16Pbqd20zlzDHeCuO/Fr+w==\n"
        "-----END PRIVATE KEY-----\n"
    ),
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
        self.closed = False

    async def aclose(self):
        # Real counterpart: google.genai.client.AsyncClient.aclose(), which
        # llm_client calls in a finally block so the SDK's httpx connection
        # pool isn't leaked once per visitor request.
        self.closed = True


class _FakeClient:
    """Records constructor kwargs so tests can assert genai.Client() was
    built correctly (api_key vs vertexai=True+project+location+credentials)."""

    last_kwargs: dict = {}
    last_instance: "_FakeClient | None" = None
    _next_models: list = []
    _next_exc: Exception | None = None

    def __init__(self, **kwargs):
        _FakeClient.last_kwargs = kwargs
        _FakeClient.last_instance = self
        self.aio = _FakeAio(models=_FakeClient._next_models, exc=_FakeClient._next_exc)


def _install_fake_client(monkeypatch, models=None, exc=None):
    _FakeClient._next_models = models or []
    _FakeClient._next_exc = exc
    monkeypatch.setattr(llm_client.genai, "Client", _FakeClient)
    # list_vertex_models proactively calls the REAL (unmocked)
    # service_account.Credentials.refresh() off-thread before touching the
    # fake genai.Client -- without this, every Vertex test below would
    # attempt an actual network call to Google's token endpoint using the
    # throwaway sentinel key, defeating the SDK-boundary mocking this file's
    # module docstring documents. A no-op default here; individual tests
    # override it when they need to assert on/simulate the refresh itself.
    monkeypatch.setattr(
        llm_client.service_account.Credentials, "refresh", lambda self, request: None
    )


async def test_list_gemini_models_returns_stripped_names(monkeypatch):
    _install_fake_client(
        monkeypatch,
        models=[
            _model("models/gemini-flash-latest"),
            _model("models/gemini-2.5-pro"),
        ],
    )
    result = await llm_client.list_gemini_models("sentinel-api-key")
    assert result == llm_client.LlmModelsListed(models=["gemini-flash-latest", "gemini-2.5-pro"])


async def test_list_gemini_models_constructs_client_with_api_key(monkeypatch):
    _install_fake_client(monkeypatch, models=[])
    await llm_client.list_gemini_models("sentinel-api-key")
    assert _FakeClient.last_kwargs["api_key"] == "sentinel-api-key"
    assert "vertexai" not in _FakeClient.last_kwargs


async def test_list_gemini_models_sets_a_request_timeout(monkeypatch):
    """An unset google-genai timeout is unbounded, not merely long (the SDK
    passes timeout=None straight into httpx), so an unresponsive provider
    would hang this synchronous, visitor-facing validation call forever."""
    _install_fake_client(monkeypatch, models=[])
    await llm_client.list_gemini_models("a")
    assert _FakeClient.last_kwargs["http_options"].timeout == 10_000


async def test_list_vertex_models_sets_a_request_timeout(monkeypatch):
    _install_fake_client(monkeypatch, models=[])
    await llm_client.list_vertex_models(_b64(_SENTINEL_SERVICE_ACCOUNT))
    assert _FakeClient.last_kwargs["http_options"].timeout == 10_000


async def test_list_gemini_models_closes_the_client_on_success(monkeypatch):
    _install_fake_client(monkeypatch, models=[])
    await llm_client.list_gemini_models("a")
    assert _FakeClient.last_instance.aio.closed is True


async def test_list_gemini_models_closes_the_client_on_failure(monkeypatch):
    """The close lives in a finally block precisely so an error path can't
    leak the SDK's connection pool."""
    _install_fake_client(monkeypatch, exc=genai_errors.ClientError(401, {"message": "bad key"}))
    await llm_client.list_gemini_models("bad")
    assert _FakeClient.last_instance.aio.closed is True


async def test_list_vertex_models_closes_the_client_on_failure(monkeypatch):
    _install_fake_client(monkeypatch, exc=genai_errors.ClientError(403, {"message": "no role"}))
    await llm_client.list_vertex_models(_b64(_SENTINEL_SERVICE_ACCOUNT))
    assert _FakeClient.last_instance.aio.closed is True


async def test_list_gemini_models_filters_out_non_generate_content_models(monkeypatch):
    _install_fake_client(
        monkeypatch,
        models=[
            _model("models/gemini-flash-latest", supported_actions=["generateContent"]),
            _model("models/embedding-001", supported_actions=["embedContent"]),
            _model("models/no-actions", supported_actions=[]),
        ],
    )
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
    assert result == llm_client.VertexModelsListed(
        project_id="sentinel-project", models=["gemini-2.5-flash"]
    )


async def test_list_vertex_models_lets_through_models_with_no_known_capability(monkeypatch):
    """Vertex's converter (google/genai/models.py's _Model_from_vertex)
    never populates supported_actions at all -- verified directly against
    the installed SDK's source, not assumed. A Vertex Model built through
    the real converter therefore has supported_actions=None, and must
    still be listed (not silently dropped) or Vertex's model catalog is
    always empty regardless of the submitted credential -- this is the
    exact bug a hand-rolled SimpleNamespace fake let through undetected
    across three prior task reviews and one whole-branch review."""
    from google.genai import models as genai_models
    from google.genai import types as genai_types

    converted = genai_models._Model_from_vertex(
        {"name": "publishers/google/models/gemini-2.5-flash"}
    )
    assert "supported_actions" not in converted
    real_model = genai_types.Model(**converted)
    assert real_model.supported_actions is None

    _install_fake_client(monkeypatch, models=[real_model])
    result = await llm_client.list_vertex_models(_b64(_SENTINEL_SERVICE_ACCOUNT))
    assert result == llm_client.VertexModelsListed(
        project_id="sentinel-project", models=["gemini-2.5-flash"]
    )


async def test_list_vertex_models_still_filters_when_capability_is_known(monkeypatch):
    """If a future SDK version DOES populate supported_actions for Vertex,
    the filter must still apply -- the fix isn't "let everything through
    unconditionally", it's "don't drop what we can't classify"."""
    _install_fake_client(
        monkeypatch,
        models=[
            _model(
                "publishers/google/models/gemini-2.5-flash", supported_actions=["generateContent"]
            ),
            _model("publishers/google/models/embedding-001", supported_actions=["embedContent"]),
        ],
    )
    result = await llm_client.list_vertex_models(_b64(_SENTINEL_SERVICE_ACCOUNT))
    assert result == llm_client.VertexModelsListed(
        project_id="sentinel-project", models=["gemini-2.5-flash"]
    )


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


async def test_list_vertex_models_rejects_non_google_token_uri(monkeypatch):
    """SSRF guard: from_service_account_info() reads token_uri straight out
    of the visitor-supplied dict and google-auth uses it as the destination
    of the token-refresh request it later issues -- an unpinned value lets a
    visitor (who also controls the matching private key) redirect that
    server-side outbound request to an arbitrary host. Credentials must
    never even be constructed from a mismatched value, so the test fails
    loudly if from_service_account_info is reached at all."""

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("credentials must never be built from an unpinned token_uri")

    monkeypatch.setattr(
        llm_client.service_account.Credentials, "from_service_account_info", _fail_if_called
    )
    malicious = dict(
        _SENTINEL_SERVICE_ACCOUNT, token_uri="http://169.254.169.254/latest/meta-data/"
    )
    result = await llm_client.list_vertex_models(_b64(malicious))
    assert result == llm_client.LlmApiFailed(reason="invalid_service_account_json")


async def test_list_vertex_models_rejects_non_google_universe_domain(monkeypatch):
    """Same SSRF class as the token_uri guard above -- universe_domain is
    the other field google-auth reads off the submitted dict that can steer
    where an outbound request lands."""

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("credentials must never be built from an unpinned universe_domain")

    monkeypatch.setattr(
        llm_client.service_account.Credentials, "from_service_account_info", _fail_if_called
    )
    malicious = dict(_SENTINEL_SERVICE_ACCOUNT, universe_domain="attacker-controlled.example")
    result = await llm_client.list_vertex_models(_b64(malicious))
    assert result == llm_client.LlmApiFailed(reason="invalid_service_account_json")


async def test_list_vertex_models_allows_the_real_google_token_uri(monkeypatch):
    """The guard must not reject a legitimate credential using Google's own
    token endpoint -- every other Vertex test already exercises this
    implicitly via _SENTINEL_SERVICE_ACCOUNT; this test makes the allow-path
    explicit rather than relying on that as an accident of other tests."""
    _install_fake_client(monkeypatch, models=[])
    result = await llm_client.list_vertex_models(_b64(_SENTINEL_SERVICE_ACCOUNT))
    assert result == llm_client.VertexModelsListed(project_id="sentinel-project", models=[])


async def test_list_vertex_models_allows_missing_universe_domain(monkeypatch):
    """universe_domain is optional in a real service-account JSON (absent
    from _SENTINEL_SERVICE_ACCOUNT); absence must not be treated as a
    rejection."""
    assert "universe_domain" not in _SENTINEL_SERVICE_ACCOUNT
    _install_fake_client(monkeypatch, models=[])
    result = await llm_client.list_vertex_models(_b64(_SENTINEL_SERVICE_ACCOUNT))
    assert not (
        isinstance(result, llm_client.LlmApiFailed)
        and result.reason == "invalid_service_account_json"
    )


async def test_list_vertex_models_refreshes_credentials_off_the_event_loop(monkeypatch):
    """Confirms the proactive refresh actually happens (via
    asyncio.to_thread, off the event loop) rather than trusting the SDK to
    have refreshed internally -- which would still block the loop. See the
    module-level comment above the await asyncio.to_thread(creds.refresh,
    ...) call in list_vertex_models."""
    _install_fake_client(monkeypatch, models=[])
    calls = []
    monkeypatch.setattr(
        llm_client.service_account.Credentials,
        "refresh",
        lambda self, request: calls.append(request),
    )
    await llm_client.list_vertex_models(_b64(_SENTINEL_SERVICE_ACCOUNT))
    assert len(calls) == 1


async def test_list_vertex_models_refresh_error_is_unauthorized(monkeypatch):
    """A RefreshError raised by the proactive refresh step itself (not just
    by the later models.list() call) must map the same way."""
    _install_fake_client(monkeypatch, models=[])

    def _raise(self, request):
        raise google_auth_exceptions.RefreshError("bad credentials")

    monkeypatch.setattr(llm_client.service_account.Credentials, "refresh", _raise)
    result = await llm_client.list_vertex_models(_b64(_SENTINEL_SERVICE_ACCOUNT))
    assert result == llm_client.LlmApiFailed(reason="unauthorized")


async def test_list_vertex_models_auth_error_is_unauthorized(monkeypatch):
    _install_fake_client(monkeypatch, exc=google_auth_exceptions.RefreshError("bad credentials"))
    result = await llm_client.list_vertex_models(_b64(_SENTINEL_SERVICE_ACCOUNT))
    assert result == llm_client.LlmApiFailed(reason="unauthorized")


async def test_list_vertex_models_forbidden(monkeypatch):
    _install_fake_client(
        monkeypatch, exc=genai_errors.ClientError(403, {"message": "no vertex ai role"})
    )
    result = await llm_client.list_vertex_models(_b64(_SENTINEL_SERVICE_ACCOUNT))
    assert result == llm_client.LlmApiFailed(reason="forbidden")


async def test_list_vertex_models_server_error_is_unreachable(monkeypatch):
    _install_fake_client(monkeypatch, exc=genai_errors.ServerError(503, {"message": "oops"}))
    result = await llm_client.list_vertex_models(_b64(_SENTINEL_SERVICE_ACCOUNT))
    assert result == llm_client.LlmApiFailed(reason="provider_unreachable")


async def test_list_vertex_models_never_logs_the_decoded_key(monkeypatch, caplog):
    """Drives a real sentinel key all the way through decode -> parse ->
    from_service_account_info -> a failing live call, so the assertion has
    something to actually catch. The earlier version passed malformed
    base64, which returns before any key material exists in the process --
    it could never have failed for the reason its name claims."""
    _install_fake_client(monkeypatch, exc=genai_errors.ClientError(401, {"message": "bad key"}))
    with caplog.at_level("DEBUG"):
        await llm_client.list_vertex_models(_b64(_SENTINEL_SERVICE_ACCOUNT))
    assert "sentinel" not in caplog.text.lower()
    assert "BEGIN PRIVATE KEY" not in caplog.text


MODELS_URL = "https://api.groq.com/openai/v1/models"


async def test_list_groq_models_returns_ids_unfiltered():
    """Deliberately unfiltered (spec section 2): whisper-large-v3 is a
    non-chat model Groq's API doesn't distinguish from a chat one."""
    with respx.mock:
        respx.get(MODELS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "id": "llama-3.3-70b-versatile",
                            "created": 1,
                            "object": "model",
                            "owned_by": "Meta",
                        },
                        {
                            "id": "whisper-large-v3",
                            "created": 1,
                            "object": "model",
                            "owned_by": "OpenAI",
                        },
                    ],
                },
            )
        )
        result = await llm_client.list_groq_models("sentinel-key")
    assert result == llm_client.LlmModelsListed(
        models=["llama-3.3-70b-versatile", "whisper-large-v3"]
    )


async def test_list_groq_models_sends_bearer_token():
    with respx.mock:
        route = respx.get(MODELS_URL).mock(
            return_value=httpx.Response(200, json={"object": "list", "data": []})
        )
        await llm_client.list_groq_models("sentinel-key")
    assert route.calls.last.request.headers["authorization"] == "Bearer sentinel-key"


async def test_list_groq_models_unauthorized():
    with respx.mock:
        respx.get(MODELS_URL).mock(
            return_value=httpx.Response(401, json={"error": {"message": "invalid key"}})
        )
        result = await llm_client.list_groq_models("bad")
    assert result == llm_client.LlmApiFailed(reason="unauthorized")


async def test_list_groq_models_forbidden():
    with respx.mock:
        respx.get(MODELS_URL).mock(
            return_value=httpx.Response(403, json={"error": {"message": "forbidden"}})
        )
        result = await llm_client.list_groq_models("a")
    assert result == llm_client.LlmApiFailed(reason="forbidden")


async def test_list_groq_models_rate_limited():
    with respx.mock:
        respx.get(MODELS_URL).mock(
            return_value=httpx.Response(429, json={"error": {"message": "slow down"}})
        )
        result = await llm_client.list_groq_models("a")
    assert result == llm_client.LlmApiFailed(reason="rate_limited")


async def test_list_groq_models_unreachable_on_5xx():
    with respx.mock:
        respx.get(MODELS_URL).mock(
            return_value=httpx.Response(500, json={"error": {"message": "oops"}})
        )
        result = await llm_client.list_groq_models("a")
    assert result == llm_client.LlmApiFailed(reason="provider_unreachable")


async def test_list_groq_models_network_error_is_unreachable():
    with respx.mock:
        respx.get(MODELS_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
        result = await llm_client.list_groq_models("a")
    assert result == llm_client.LlmApiFailed(reason="provider_unreachable")


async def test_list_groq_models_never_logs_the_api_key(caplog):
    """Groq's counterpart to the Vertex logging guard above. DEBUG is where
    the groq SDK dumps its request options, so that is the level worth
    pinning."""
    with respx.mock:
        respx.get(MODELS_URL).mock(
            return_value=httpx.Response(401, json={"error": {"message": "bad key"}})
        )
        with caplog.at_level("DEBUG"):
            await llm_client.list_groq_models("sentinel-super-secret-groq-key")
    assert "sentinel-super-secret-groq-key" not in caplog.text


async def test_list_groq_models_does_not_retry_behind_our_back():
    """The groq SDK defaults to max_retries=2 and retries 429/5xx, which
    would turn one visitor-facing validation into three live calls --
    counter to bot/providers/groq.py's documented max_retries=0 decision
    and to root CLAUDE.md's "stop calling on a 403/429" discipline."""
    with respx.mock:
        route = respx.get(MODELS_URL).mock(
            return_value=httpx.Response(429, json={"error": {"message": "slow down"}})
        )
        result = await llm_client.list_groq_models("a")
    assert result == llm_client.LlmApiFailed(reason="rate_limited")
    assert route.call_count == 1
