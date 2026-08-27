# Onboarding Wizard — LLM Provider Credential Frame (Frame 4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the onboarding wizard's frame 4 fully functional — a visitor
picks Gemini, Groq, or Vertex, submits that provider's credential (an API
key paste, or a GCP service-account JSON file for Vertex), the wizard
validates it with one live models-listing call and shows the models that
credential can actually reach, and the visitor must pick one before the
frame completes.

**Architecture:** One new backend module (`onboarding/llm_client.py`)
wrapping the official `google-genai` and `groq` SDKs' model-listing calls
(not raw `httpx` — both SDKs are already project dependencies and already
encode the real request/auth/retry contract), three new relay endpoints in
`router.py`, and frontend JS that drives credential input (text field or
file picker), the one validation call, and a model dropdown gating frame
completion.

**Tech Stack:** FastAPI, `google-genai` (Gemini + Vertex, via
`google.oauth2.service_account.Credentials` for Vertex), `groq`
(`AsyncGroq`), pydantic, vanilla JS (`FileReader` for the Vertex file
picker), pytest + pytest-asyncio + respx (Groq only — Gemini/Vertex are
mocked at the SDK client boundary, see Task 1/2).

**Spec:**
`docs/superpowers/specs/2026-08-27-onboarding-llm-provider-frame-design.md`

## Global Constraints

- `onboarding/` is a **stateless relay** — no database, no session store, no
  server-side persistence of any visitor credential, ever (`onboarding/CLAUDE.md`).
- **Never log a visitor-supplied credential** — an API key or the
  decoded/raw GCP service-account JSON — in full or truncated.
- Every relay endpoint returns a verdict, never a submitted credential —
  **no mint-and-return exception in this sub-project**: all three endpoints
  only ever validate-and-list, nothing is freshly minted server-side.
- **No new `onboarding/config.py` settings, no `onboarding/main.py`
  `lifespan` changes** — every credential here is visitor-supplied per
  request, unlike Supabase's operator-level OAuth secret.
- **The model list is always fetched live from the provider's own
  catalog — never hardcode a default/fallback model id anywhere in this
  sub-project's code.** This is the specific drift (`gemini-flash-latest`
  404s against Vertex's publisher-model catalog — root `CLAUDE.md`'s
  substitutions section) this sub-project exists to stop relying on.
- **The frame's unlock gate requires both a live-validated credential AND
  an explicit model pick** — no fallback to any baked-in default if the
  visitor skips picking a model.
- Gemini/Vertex model lists are filtered to `supported_actions` containing
  `"generateContent"`. **Groq's list is deliberately unfiltered** — no
  name-pattern heuristic filtering (spec section 2).
- Vertex's GCP location is fixed to `us-central1`; `project_id` is always
  derived server-side from the submitted key's own `project_id` field,
  never visitor-typed.
- No bare `except Exception` anywhere — narrow, explicit exception
  handling only, matching `render_client.py`'s/`supabase_client.py`'s
  existing convention.
- **No live network calls to Gemini/Groq/Vertex in tests.** Gemini/Vertex
  are mocked at the SDK client boundary (`google.genai.Client` itself is
  monkeypatched) — its transport mixes `httpx` and `requests` depending on
  auth type, so a single `respx` mock cannot cleanly cover both paths
  (verified directly against the SDK's own source, not assumed). Groq's
  SDK transport is pure `httpx`, so its tests use `respx` normally.
- A visitor's credential lives in `sessionStorage`, never `localStorage`.
- `GET /`'s CSP does **not** need a new entry — this frame has no
  full-page navigation or form POST, only `fetch()` calls.

---

### Task 1: `onboarding/llm_client.py` — Gemini + Vertex model listing

**Files:**
- Create: `onboarding/llm_client.py`
- Test: `tests/test_onboarding_llm_client.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `LlmModelsListed(models: list[str])`,
  `VertexModelsListed(project_id: str, models: list[str])`,
  `LlmApiFailed(reason: str)` (reason ∈ `"unauthorized"` | `"forbidden"` |
  `"rate_limited"` | `"provider_unreachable"` |
  `"invalid_service_account_json"` — Vertex only), `async def
  list_gemini_models(api_key: str) -> LlmModelsListed | LlmApiFailed`,
  `async def list_vertex_models(service_account_key_b64: str) ->
  VertexModelsListed | LlmApiFailed` — `LlmModelsListed`/`LlmApiFailed`
  reused by Task 2's `list_groq_models`; all three functions used by
  Task 3's router.

- [ ] **Step 1: Write the failing tests**

`tests/test_onboarding_llm_client.py`:
```python
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
    "private_key": "-----BEGIN PRIVATE KEY-----\nsentinel\n-----END PRIVATE KEY-----\n",
    "client_email": "sentinel@sentinel-project.iam.gserviceaccount.com",
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_llm_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'onboarding.llm_client'`

- [ ] **Step 3: Write `onboarding/llm_client.py`**

```python
"""Thin async wrapper around Gemini and Vertex AI's model-listing calls —
used to validate a visitor-supplied credential and discover which models it
can actually reach, without persisting anything server-side. Gemini and
Vertex share one internal helper since both go through the same
google-genai SDK, differing only in how genai.Client is constructed. See
docs/superpowers/specs/2026-08-27-onboarding-llm-provider-frame-design.md
sections 3-4."""
from __future__ import annotations

import base64
import binascii
import dataclasses
import json

import httpx
from google import genai
from google.auth import exceptions as google_auth_exceptions
from google.genai import errors as genai_errors
from google.oauth2 import service_account

_VERTEX_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
_VERTEX_LOCATION = "us-central1"


@dataclasses.dataclass(frozen=True)
class LlmModelsListed:
    models: list[str]


@dataclasses.dataclass(frozen=True)
class VertexModelsListed:
    project_id: str
    models: list[str]


@dataclasses.dataclass(frozen=True)
class LlmApiFailed:
    reason: str
    # "unauthorized" | "forbidden" | "rate_limited" | "provider_unreachable"
    # | "invalid_service_account_json" (vertex only)


def _strip_model_prefix(name: str) -> str:
    """"models/gemini-flash-latest" -> "gemini-flash-latest";
    "publishers/google/models/gemini-2.5-flash" -> "gemini-2.5-flash" — both
    are resource-name formats google-genai returns; app/config.py's
    llm_model/vertex_model fields expect the bare id."""
    return name.rsplit("/", 1)[-1]


async def _list_generative_models(client: genai.Client) -> list[str]:
    """Filtered to generateContent-capable models only — the SDK call
    app/providers/google_genai.py actually makes (spec section 2)."""
    names = []
    async for model in await client.aio.models.list():
        if model.name and model.supported_actions and "generateContent" in model.supported_actions:
            names.append(_strip_model_prefix(model.name))
    return names


def _reason_for_client_error_code(code: int) -> str:
    if code == 401:
        return "unauthorized"
    if code == 403:
        return "forbidden"
    if code == 429:
        return "rate_limited"
    return "provider_unreachable"


async def list_gemini_models(api_key: str) -> LlmModelsListed | LlmApiFailed:
    """Live models-listing call against the Gemini Developer API (AI
    Studio) — doubles as credential validation. Never logs api_key."""
    client = genai.Client(api_key=api_key)
    try:
        models = await _list_generative_models(client)
    except genai_errors.ClientError as exc:
        return LlmApiFailed(reason=_reason_for_client_error_code(exc.code))
    except genai_errors.ServerError:
        return LlmApiFailed(reason="provider_unreachable")
    except httpx.HTTPError:
        return LlmApiFailed(reason="provider_unreachable")
    return LlmModelsListed(models=models)


async def list_vertex_models(service_account_key_b64: str) -> VertexModelsListed | LlmApiFailed:
    """Live models-listing call against Vertex AI, authenticated as the
    submitted GCP service account. Location is fixed to us-central1 (spec
    section 2); project is read from the key's own project_id field. Never
    logs the decoded key or its contents."""
    try:
        decoded = base64.b64decode(service_account_key_b64, validate=True)
        info = json.loads(decoded)
        project_id = str(info["project_id"])
    except (binascii.Error, ValueError, KeyError, TypeError):
        return LlmApiFailed(reason="invalid_service_account_json")

    creds = service_account.Credentials.from_service_account_info(info, scopes=_VERTEX_SCOPES)
    client = genai.Client(vertexai=True, project=project_id, location=_VERTEX_LOCATION, credentials=creds)
    try:
        models = await _list_generative_models(client)
    except genai_errors.ClientError as exc:
        return LlmApiFailed(reason=_reason_for_client_error_code(exc.code))
    except genai_errors.ServerError:
        return LlmApiFailed(reason="provider_unreachable")
    except google_auth_exceptions.GoogleAuthError:
        return LlmApiFailed(reason="unauthorized")
    except httpx.HTTPError:
        return LlmApiFailed(reason="provider_unreachable")
    return VertexModelsListed(project_id=project_id, models=models)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_llm_client.py -v`
Expected: PASS (18 tests)

- [ ] **Step 5: Commit**

```bash
git add onboarding/llm_client.py tests/test_onboarding_llm_client.py
git commit -m "feat: add Gemini and Vertex model listing"
```

---

### Task 2: `onboarding/llm_client.py` — Groq model listing

**Files:**
- Modify: `onboarding/llm_client.py`
- Modify: `tests/test_onboarding_llm_client.py`

**Interfaces:**
- Consumes: `LlmModelsListed`, `LlmApiFailed` (Task 1, reused unchanged).
- Produces: `async def list_groq_models(api_key: str) -> LlmModelsListed |
  LlmApiFailed` — used by Task 3's router.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_onboarding_llm_client.py` (add `import httpx` and
`import respx` to the top-of-file imports):
```python
import httpx
import respx

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
                        {"id": "llama-3.3-70b-versatile", "created": 1, "object": "model", "owned_by": "Meta"},
                        {"id": "whisper-large-v3", "created": 1, "object": "model", "owned_by": "OpenAI"},
                    ],
                },
            )
        )
        result = await llm_client.list_groq_models("sentinel-key")
    assert result == llm_client.LlmModelsListed(models=["llama-3.3-70b-versatile", "whisper-large-v3"])


async def test_list_groq_models_sends_bearer_token():
    with respx.mock:
        route = respx.get(MODELS_URL).mock(return_value=httpx.Response(200, json={"object": "list", "data": []}))
        await llm_client.list_groq_models("sentinel-key")
    assert route.calls.last.request.headers["authorization"] == "Bearer sentinel-key"


async def test_list_groq_models_unauthorized():
    with respx.mock:
        respx.get(MODELS_URL).mock(return_value=httpx.Response(401, json={"error": {"message": "invalid key"}}))
        result = await llm_client.list_groq_models("bad")
    assert result == llm_client.LlmApiFailed(reason="unauthorized")


async def test_list_groq_models_forbidden():
    with respx.mock:
        respx.get(MODELS_URL).mock(return_value=httpx.Response(403, json={"error": {"message": "forbidden"}}))
        result = await llm_client.list_groq_models("a")
    assert result == llm_client.LlmApiFailed(reason="forbidden")


async def test_list_groq_models_rate_limited():
    with respx.mock:
        respx.get(MODELS_URL).mock(return_value=httpx.Response(429, json={"error": {"message": "slow down"}}))
        result = await llm_client.list_groq_models("a")
    assert result == llm_client.LlmApiFailed(reason="rate_limited")


async def test_list_groq_models_unreachable_on_5xx():
    with respx.mock:
        respx.get(MODELS_URL).mock(return_value=httpx.Response(500, json={"error": {"message": "oops"}}))
        result = await llm_client.list_groq_models("a")
    assert result == llm_client.LlmApiFailed(reason="provider_unreachable")


async def test_list_groq_models_network_error_is_unreachable():
    with respx.mock:
        respx.get(MODELS_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
        result = await llm_client.list_groq_models("a")
    assert result == llm_client.LlmApiFailed(reason="provider_unreachable")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_llm_client.py -v`
Expected: FAIL — `AttributeError: module 'onboarding.llm_client' has no
attribute 'list_groq_models'`

- [ ] **Step 3: Modify `onboarding/llm_client.py`**

Replace the module docstring:
```python
"""Thin async wrapper around Gemini and Vertex AI's model-listing calls —
used to validate a visitor-supplied credential and discover which models it
can actually reach, without persisting anything server-side. Gemini and
Vertex share one internal helper since both go through the same
google-genai SDK, differing only in how genai.Client is constructed. See
docs/superpowers/specs/2026-08-27-onboarding-llm-provider-frame-design.md
sections 3-4."""
```
with:
```python
"""Thin async wrapper around Gemini, Vertex AI, and Groq's model-listing
calls — used to validate a visitor-supplied credential and discover which
models it can actually reach, without persisting anything server-side.
Gemini and Vertex share one internal helper since both go through the same
google-genai SDK, differing only in how genai.Client is constructed; Groq
uses the official groq SDK directly. See
docs/superpowers/specs/2026-08-27-onboarding-llm-provider-frame-design.md
sections 3-4."""
```

Add `import groq` to the imports (alphabetically among the existing
`google.*` imports):
```python
import httpx
from google import genai
```
becomes
```python
import groq
import httpx
from google import genai
```

Append at the end of the file:
```python


async def list_groq_models(api_key: str) -> LlmModelsListed | LlmApiFailed:
    """Live models-listing call against Groq's OpenAI-compatible API —
    doubles as credential validation. Deliberately unfiltered (spec
    section 2): Groq's Model type carries no capability field to
    distinguish chat-completion models from Whisper/TTS/moderation ones.
    Never logs api_key."""
    client = groq.AsyncGroq(api_key=api_key)
    try:
        response = await client.models.list()
    except groq.AuthenticationError:
        return LlmApiFailed(reason="unauthorized")
    except groq.PermissionDeniedError:
        return LlmApiFailed(reason="forbidden")
    except groq.RateLimitError:
        return LlmApiFailed(reason="rate_limited")
    except (groq.InternalServerError, groq.APIConnectionError, groq.APITimeoutError):
        return LlmApiFailed(reason="provider_unreachable")
    return LlmModelsListed(models=[m.id for m in response.data])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_llm_client.py -v`
Expected: PASS (25 tests total)

- [ ] **Step 5: Commit**

```bash
git add onboarding/llm_client.py tests/test_onboarding_llm_client.py
git commit -m "feat: add Groq model listing"
```

---

### Task 3: `onboarding/router.py` — three relay endpoints

**Files:**
- Modify: `onboarding/router.py`
- Modify: `tests/test_onboarding_router.py`

**Interfaces:**
- Consumes: every `onboarding.llm_client` function and result type
  (Tasks 1-2).
- Produces: `POST /api/llm/gemini/list-models`,
  `POST /api/llm/groq/list-models`, `POST /api/llm/vertex/list-models` —
  JSON contracts per spec section 4.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_onboarding_router.py` (add `llm_client` to the existing
import):
```python
from onboarding import github_client, llm_client, render_client, supabase_client
```

Then add:
```python
async def test_gemini_list_models_returns_models(monkeypatch):
    async def fake_list(api_key):
        assert api_key == "SENTINEL_KEY"
        return llm_client.LlmModelsListed(models=["gemini-flash-latest", "gemini-2.5-pro"])

    monkeypatch.setattr(llm_client, "list_gemini_models", fake_list)
    client = await _client()
    resp = await client.post("/api/llm/gemini/list-models", json={"api_key": "SENTINEL_KEY"})
    assert resp.json() == {"valid": True, "models": ["gemini-flash-latest", "gemini-2.5-pro"]}


async def test_gemini_list_models_reports_failure_reason(monkeypatch):
    async def fake_list(api_key):
        return llm_client.LlmApiFailed(reason="unauthorized")

    monkeypatch.setattr(llm_client, "list_gemini_models", fake_list)
    client = await _client()
    resp = await client.post("/api/llm/gemini/list-models", json={"api_key": "bad"})
    assert resp.json() == {"valid": False, "reason": "unauthorized"}


async def test_gemini_list_models_validation_error_never_echoes_the_key():
    sentinel_key = "SENTINEL_DO_NOT_ECHO_KEY"
    client = await _client()
    resp = await client.post("/api/llm/gemini/list-models", json={"api_key_typo": sentinel_key})
    assert resp.status_code == 422
    assert sentinel_key not in resp.text
    assert "input" not in resp.text


async def test_groq_list_models_returns_models(monkeypatch):
    async def fake_list(api_key):
        assert api_key == "SENTINEL_KEY"
        return llm_client.LlmModelsListed(models=["llama-3.3-70b-versatile"])

    monkeypatch.setattr(llm_client, "list_groq_models", fake_list)
    client = await _client()
    resp = await client.post("/api/llm/groq/list-models", json={"api_key": "SENTINEL_KEY"})
    assert resp.json() == {"valid": True, "models": ["llama-3.3-70b-versatile"]}


async def test_groq_list_models_reports_failure_reason(monkeypatch):
    async def fake_list(api_key):
        return llm_client.LlmApiFailed(reason="rate_limited")

    monkeypatch.setattr(llm_client, "list_groq_models", fake_list)
    client = await _client()
    resp = await client.post("/api/llm/groq/list-models", json={"api_key": "a"})
    assert resp.json() == {"valid": False, "reason": "rate_limited"}


async def test_groq_list_models_validation_error_never_echoes_the_key():
    sentinel_key = "SENTINEL_DO_NOT_ECHO_KEY"
    client = await _client()
    resp = await client.post("/api/llm/groq/list-models", json={"api_key_typo": sentinel_key})
    assert resp.status_code == 422
    assert sentinel_key not in resp.text
    assert "input" not in resp.text


async def test_vertex_list_models_returns_models_and_project_id(monkeypatch):
    async def fake_list(service_account_key_b64):
        assert service_account_key_b64 == "SENTINEL_B64"
        return llm_client.VertexModelsListed(project_id="sentinel-project", models=["gemini-2.5-flash"])

    monkeypatch.setattr(llm_client, "list_vertex_models", fake_list)
    client = await _client()
    resp = await client.post("/api/llm/vertex/list-models", json={"service_account_key_b64": "SENTINEL_B64"})
    assert resp.json() == {"valid": True, "project_id": "sentinel-project", "models": ["gemini-2.5-flash"]}


async def test_vertex_list_models_reports_failure_reason(monkeypatch):
    async def fake_list(service_account_key_b64):
        return llm_client.LlmApiFailed(reason="invalid_service_account_json")

    monkeypatch.setattr(llm_client, "list_vertex_models", fake_list)
    client = await _client()
    resp = await client.post("/api/llm/vertex/list-models", json={"service_account_key_b64": "not-json"})
    assert resp.json() == {"valid": False, "reason": "invalid_service_account_json"}


async def test_vertex_list_models_validation_error_never_echoes_the_key():
    sentinel_key = "SENTINEL_DO_NOT_ECHO_SERVICE_ACCOUNT_KEY"
    client = await _client()
    resp = await client.post("/api/llm/vertex/list-models", json={"key_typo": sentinel_key})
    assert resp.status_code == 422
    assert sentinel_key not in resp.text
    assert "input" not in resp.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_router.py -v`
Expected: FAIL — `404 Not Found` for all three new endpoints (routes don't
exist yet).

- [ ] **Step 3: Modify `onboarding/router.py`**

Replace:
```python
from onboarding import github_client, render_client, supabase_client
```
with:
```python
from onboarding import github_client, llm_client, render_client, supabase_client
```

Add after `SupabaseConnectionInfoRequest`:
```python
class LlmGeminiListModelsRequest(BaseModel):
    api_key: str = Field(max_length=512)


class LlmGroqListModelsRequest(BaseModel):
    api_key: str = Field(max_length=512)


class LlmVertexListModelsRequest(BaseModel):
    service_account_key_b64: str = Field(max_length=16384)
```

Add after `get_supabase_connection_info` (end of file):
```python


@router.post("/api/llm/gemini/list-models")
async def list_gemini_models(payload: LlmGeminiListModelsRequest) -> dict:
    result = await llm_client.list_gemini_models(payload.api_key)
    if isinstance(result, llm_client.LlmModelsListed):
        return {"valid": True, "models": result.models}
    return {"valid": False, "reason": result.reason}


@router.post("/api/llm/groq/list-models")
async def list_groq_models(payload: LlmGroqListModelsRequest) -> dict:
    result = await llm_client.list_groq_models(payload.api_key)
    if isinstance(result, llm_client.LlmModelsListed):
        return {"valid": True, "models": result.models}
    return {"valid": False, "reason": result.reason}


@router.post("/api/llm/vertex/list-models")
async def list_vertex_models(payload: LlmVertexListModelsRequest) -> dict:
    result = await llm_client.list_vertex_models(payload.service_account_key_b64)
    if isinstance(result, llm_client.VertexModelsListed):
        return {"valid": True, "project_id": result.project_id, "models": result.models}
    return {"valid": False, "reason": result.reason}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_router.py -v`
Expected: PASS (all router tests, including the 9 new ones)

- [ ] **Step 5: Commit**

```bash
git add onboarding/router.py tests/test_onboarding_router.py
git commit -m "feat: add LLM provider model-listing relay endpoints"
```

---

### Task 4: `onboarding/static/index.html` — frame 4 markup + Gemini/Groq flow

**Files:**
- Modify: `onboarding/static/index.html`
- Modify: `tests/test_onboarding_page.py`

**Interfaces:**
- Consumes: `POST /api/llm/gemini/list-models`,
  `POST /api/llm/groq/list-models` (Task 3). `POST /api/llm/vertex/list-models`
  exists but this task's JS does not call it yet — Task 5 wires the Vertex
  branch.
- Produces: `STORAGE_KEYS["llm-provider"]`, `readStoredLlmProvider()`,
  `resetLlmProviderSection()`, `getSelectedLlmProvider()`,
  `handleLlmProviderChoice()`, `validateLlmProviderCredential()`,
  `showLlmProviderModels(models, provider, credentialFragment, projectId)`,
  `confirmLlmProviderModel()` — Task 5 extends
  `validateLlmProviderCredential()`'s Vertex branch and reuses
  `showLlmProviderModels`/`resetLlmProviderSection` unchanged.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_onboarding_page.py`:
```python
async def test_frame4_has_a_three_way_provider_selector():
    client = await _client()
    body = (await client.get("/")).text
    assert 'id="llm-provider-choice-gemini"' in body
    assert 'id="llm-provider-choice-groq"' in body
    assert 'id="llm-provider-choice-vertex"' in body


async def test_frame4_has_credential_inputs_and_model_picker():
    client = await _client()
    body = (await client.get("/")).text
    assert 'id="llm-provider-api-key-input"' in body
    assert 'id="llm-provider-file-input"' in body
    assert 'id="llm-provider-model-select"' in body
    assert 'id="llm-provider-continue-submit"' in body


async def test_gemini_llm_endpoint_leaves_the_page_exactly_once():
    client = await _client()
    body = (await client.get("/")).text
    assert body.count('endpoint = "/api/llm/gemini/list-models"') == 1


async def test_groq_llm_endpoint_leaves_the_page_exactly_once():
    client = await _client()
    body = (await client.get("/")).text
    assert body.count('endpoint = "/api/llm/groq/list-models"') == 1


async def test_llm_provider_credential_has_exactly_one_fetch_call_site():
    """All three providers (Gemini/Groq here, Vertex in Task 5) share one
    fetch() call site in validateLlmProviderCredential() rather than one
    fetch() per provider — the per-provider endpoint tests above establish
    each credential still has exactly one path to that shared call site,
    the same one-exit-path invariant onboarding/CLAUDE.md documents for
    every other credential-carrying fetch on this page, adapted for this
    frame's shared-call-site shape."""
    client = await _client()
    body = (await client.get("/")).text
    assert body.count("await fetch(endpoint, {") == 1


async def test_llm_provider_never_persists_to_local_storage():
    client = await _client()
    body = (await client.get("/")).text
    assert 'sessionStorage.setItem(STORAGE_KEYS["llm-provider"]' in body
    assert 'localStorage.setItem(STORAGE_KEYS["llm-provider"]' not in body


async def test_model_confirm_requires_both_credential_and_model():
    """Frame unlock gate: both a live-validated credential AND an explicit
    model pick are required (spec section 2) — no fallback if either is
    missing."""
    client = await _client()
    body = (await client.get("/")).text
    assert "if (!model || !pendingLlmProviderCredential)" in body


async def test_frame4_locked_by_default():
    client = await _client()
    body = (await client.get("/")).text
    assert (
        'id="frame-llm-provider" class="frame" data-status="locked" '
        'data-locked="true"'
    ) in body


async def test_empty_model_list_shows_dedicated_message():
    """A credential that validates but returns zero eligible models is a
    dead end under the "both required" gate (spec section 2) — it gets its
    own message rather than silently showing an empty dropdown."""
    client = await _client()
    body = (await client.get("/")).text
    assert "if (!models.length) {" in body
    assert 'llmProviderError("err_llm_no_models_available");' in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_page.py -v`
Expected: FAIL — the new element ids/strings don't exist yet in the served
page (frame 4 is still the `coming_soon` stub).

- [ ] **Step 3: Modify `onboarding/static/index.html`**

Replace the frame 4 stub:
```html
    <details id="frame-llm-provider" class="frame" data-status="locked" data-locked="true">
      <summary>
        <span class="frame-title" data-i18n="frame4_title"></span>
        <span class="frame-badge"></span>
      </summary>
      <div class="frame-body"><p data-i18n="coming_soon"></p></div>
    </details>
```
with:
```html
    <details id="frame-llm-provider" class="frame" data-status="locked" data-locked="true">
      <summary>
        <span class="frame-title" data-i18n="frame4_title"></span>
        <span class="frame-badge"></span>
        <button class="frame-change" type="button" data-frame="llm-provider" data-i18n="change_button"></button>
      </summary>
      <div class="frame-body">
        <p data-i18n="frame4_instructions"></p>
        <div class="radio-group">
          <label><input type="radio" name="llm-provider-choice" value="gemini" id="llm-provider-choice-gemini"> <span data-i18n="frame4_provider_gemini"></span></label>
          <label><input type="radio" name="llm-provider-choice" value="groq" id="llm-provider-choice-groq"> <span data-i18n="frame4_provider_groq"></span></label>
          <label><input type="radio" name="llm-provider-choice" value="vertex" id="llm-provider-choice-vertex"> <span data-i18n="frame4_provider_vertex"></span></label>
        </div>
        <input id="llm-provider-api-key-input" type="password" style="display: none;">
        <input id="llm-provider-file-input" type="file" accept="application/json" style="display: none;">
        <button id="llm-provider-validate-submit" type="button" data-i18n="validate_button" style="display: none;"></button>
        <div id="llm-provider-model-section" style="display: none;">
          <p data-i18n="frame4_model_instructions"></p>
          <select id="llm-provider-model-select"></select>
          <button id="llm-provider-continue-submit" type="button" data-i18n="continue_button"></button>
        </div>
        <p id="llm-provider-error" class="frame-error"></p>
      </div>
    </details>
```

Add the new STRINGS entries. Replace, in the `en` block:
```javascript
      err_supabase_provisioning_timeout: "Still provisioning — this is taking longer than usual. You can check again, or come back later.",
    },
```
with:
```javascript
      err_supabase_provisioning_timeout: "Still provisioning — this is taking longer than usual. You can check again, or come back later.",
      continue_button: "Continue",
      provider_prefix: "provider: ",
      frame4_instructions: "Pick which LLM provider your deployed service will use, then paste or upload its credential.",
      frame4_provider_gemini: "Gemini (AI Studio API key)",
      frame4_provider_groq: "Groq (API key)",
      frame4_provider_vertex: "Vertex AI (GCP service-account file)",
      frame4_model_instructions: "Choose the model your deployed service will use.",
      err_llm_no_provider: "Pick a provider first.",
      err_llm_empty_key: "Paste your API key first.",
      err_llm_no_file: "Choose your GCP service-account JSON file first.",
      err_llm_invalid_service_account_json: "That file isn't a valid GCP service-account key. Check you picked the right file.",
      err_llm_unauthorized: "That credential was rejected. Double-check it and try again.",
      err_llm_forbidden: "That credential doesn't have access. Double-check its permissions and try again.",
      err_llm_rate_limited: "Too many requests right now. Try again in a moment.",
      err_llm_unreachable: "That provider is unreachable right now. Try again in a moment.",
      err_llm_no_models_available: "This credential is valid, but no usable models are available. Check your account/project has an eligible model enabled.",
      err_llm_no_model_selected: "Pick a model first.",
      err_llm_storage_failed: "Couldn't save this in your browser. Try again.",
    },
```

Replace, in the `he` block:
```javascript
      err_supabase_provisioning_timeout: "עדיין בהקמה — זה לוקח יותר זמן מהרגיל. אפשר לבדוק שוב, או לחזור מאוחר יותר.",
    },
  };
```
with:
```javascript
      err_supabase_provisioning_timeout: "עדיין בהקמה — זה לוקח יותר זמן מהרגיל. אפשר לבדוק שוב, או לחזור מאוחר יותר.",
      continue_button: "המשך",
      provider_prefix: "ספק: ",
      frame4_instructions: "בחרו איזה ספק LLM השירות שלכם ישתמש בו, ואז הדביקו או העלו את פרטי ההתחברות שלו.",
      frame4_provider_gemini: "Gemini (מפתח API של AI Studio)",
      frame4_provider_groq: "Groq (מפתח API)",
      frame4_provider_vertex: "Vertex AI (קובץ חשבון שירות של GCP)",
      frame4_model_instructions: "בחרו את המודל שהשירות שלכם ישתמש בו.",
      err_llm_no_provider: "בחרו ספק קודם.",
      err_llm_empty_key: "הדביקו קודם את מפתח ה-API שלכם.",
      err_llm_no_file: "בחרו קודם את קובץ חשבון השירות של GCP.",
      err_llm_invalid_service_account_json: "הקובץ הזה אינו מפתח חשבון שירות תקין של GCP. ודאו שבחרתם בקובץ הנכון.",
      err_llm_unauthorized: "פרטי ההתחברות נדחו. בדקו אותם שוב ונסו שנית.",
      err_llm_forbidden: "לפרטי ההתחברות האלה אין הרשאה מספקת. בדקו את ההרשאות שלהם ונסו שוב.",
      err_llm_rate_limited: "יותר מדי בקשות כרגע. נסו שוב בעוד רגע.",
      err_llm_unreachable: "הספק אינו זמין כרגע. נסו שוב בעוד רגע.",
      err_llm_no_models_available: "פרטי ההתחברות תקינים, אך אין מודלים זמינים לשימוש. ודאו שיש לחשבון/לפרויקט שלכם מודל מתאים מופעל.",
      err_llm_no_model_selected: "בחרו מודל קודם.",
      err_llm_storage_failed: "לא ניתן היה לשמור זאת בדפדפן זה. נסו שוב.",
    },
  };
```

Add the storage key. Replace:
```javascript
  const STORAGE_KEYS = {
    "render-key": "onboarding.renderApiKey",
    "github-app": "onboarding.githubApp",
    "supabase": "onboarding.supabase",
  };
```
with:
```javascript
  const STORAGE_KEYS = {
    "render-key": "onboarding.renderApiKey",
    "github-app": "onboarding.githubApp",
    "supabase": "onboarding.supabase",
    "llm-provider": "onboarding.llmProvider",
  };
```

Add the reset call to `lockFrame`. Replace:
```javascript
    if (id === "github-app") resetGithubAppCreateSection();
    if (id === "supabase") resetSupabaseConnectSection();
  }
```
with:
```javascript
    if (id === "github-app") resetGithubAppCreateSection();
    if (id === "supabase") resetSupabaseConnectSection();
    if (id === "llm-provider") resetLlmProviderSection();
  }
```

Add the reset+clear to `beginChange`. Replace:
```javascript
    if (id === "supabase") {
      sessionStorage.removeItem(STORAGE_KEYS["supabase"]);
      resetSupabaseConnectSection();
    }
  }
```
with:
```javascript
    if (id === "supabase") {
      sessionStorage.removeItem(STORAGE_KEYS["supabase"]);
      resetSupabaseConnectSection();
    }
    if (id === "llm-provider") {
      sessionStorage.removeItem(STORAGE_KEYS["llm-provider"]);
      resetLlmProviderSection();
    }
  }
```

Add the new JS functions right before `async function validateRenderKey() {`:
```javascript
  let currentLlmProviderErrorKey = null;
  let pendingLlmProviderCredential = null;

  function resetLlmProviderSection() {
    document.querySelectorAll('input[name="llm-provider-choice"]').forEach((r) => { r.checked = false; });
    const apiKeyInput = document.getElementById("llm-provider-api-key-input");
    apiKeyInput.value = "";
    apiKeyInput.style.display = "none";
    const fileInput = document.getElementById("llm-provider-file-input");
    fileInput.value = "";
    fileInput.style.display = "none";
    document.getElementById("llm-provider-validate-submit").style.display = "none";
    document.getElementById("llm-provider-model-section").style.display = "none";
    document.getElementById("llm-provider-model-select").innerHTML = "";
    document.getElementById("llm-provider-error").textContent = "";
    currentLlmProviderErrorKey = null;
    pendingLlmProviderCredential = null;
  }

  function llmProviderError(key) {
    currentLlmProviderErrorKey = key;
    document.getElementById("llm-provider-error").textContent = t(key);
  }

  function llmProviderErrorForReason(reason) {
    const map = {
      unauthorized: "err_llm_unauthorized",
      forbidden: "err_llm_forbidden",
      rate_limited: "err_llm_rate_limited",
      provider_unreachable: "err_llm_unreachable",
      invalid_service_account_json: "err_llm_invalid_service_account_json",
    };
    llmProviderError(map[reason] || "err_llm_unreachable");
  }

  function getSelectedLlmProvider() {
    const checked = document.querySelector('input[name="llm-provider-choice"]:checked');
    return checked ? checked.value : null;
  }

  function handleLlmProviderChoice() {
    const provider = getSelectedLlmProvider();
    document.getElementById("llm-provider-model-section").style.display = "none";
    document.getElementById("llm-provider-model-select").innerHTML = "";
    document.getElementById("llm-provider-error").textContent = "";
    currentLlmProviderErrorKey = null;
    pendingLlmProviderCredential = null;
    const apiKeyInput = document.getElementById("llm-provider-api-key-input");
    const fileInput = document.getElementById("llm-provider-file-input");
    const submitBtn = document.getElementById("llm-provider-validate-submit");
    apiKeyInput.style.display = (provider === "gemini" || provider === "groq") ? "block" : "none";
    fileInput.style.display = (provider === "vertex") ? "block" : "none";
    submitBtn.style.display = provider ? "block" : "none";
  }

  function showLlmProviderModels(models, provider, credentialFragment, projectId) {
    const select = document.getElementById("llm-provider-model-select");
    select.innerHTML = "";
    if (!models.length) {
      llmProviderError("err_llm_no_models_available");
      return;
    }
    models.forEach((m) => {
      const opt = document.createElement("option");
      opt.value = m;
      opt.textContent = m;
      select.appendChild(opt);
    });
    document.getElementById("llm-provider-model-section").style.display = "block";
    pendingLlmProviderCredential = {
      provider,
      ...credentialFragment,
      ...(projectId ? {gcp_project: projectId} : {}),
    };
  }

  async function validateLlmProviderCredential() {
    const provider = getSelectedLlmProvider();
    document.getElementById("llm-provider-error").textContent = "";
    currentLlmProviderErrorKey = null;
    document.getElementById("llm-provider-model-section").style.display = "none";
    document.getElementById("llm-provider-model-select").innerHTML = "";
    pendingLlmProviderCredential = null;
    if (!provider) {
      llmProviderError("err_llm_no_provider");
      return;
    }
    let endpoint, payload, credentialFragment;
    if (provider === "gemini") {
      const apiKey = document.getElementById("llm-provider-api-key-input").value.trim();
      if (!apiKey) {
        llmProviderError("err_llm_empty_key");
        return;
      }
      endpoint = "/api/llm/gemini/list-models";
      payload = {api_key: apiKey};
      credentialFragment = {api_key: apiKey};
    } else if (provider === "groq") {
      const apiKey = document.getElementById("llm-provider-api-key-input").value.trim();
      if (!apiKey) {
        llmProviderError("err_llm_empty_key");
        return;
      }
      endpoint = "/api/llm/groq/list-models";
      payload = {api_key: apiKey};
      credentialFragment = {api_key: apiKey};
    } else {
      llmProviderError("err_llm_no_file");
      return;
    }
    let resp;
    try {
      resp = await fetch(endpoint, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload),
      });
    } catch (err) {
      llmProviderError("err_network");
      return;
    }
    let body;
    try {
      if (!resp.ok) throw new Error("bad status");
      body = await resp.json();
    } catch (err) {
      llmProviderError("err_llm_unreachable");
      return;
    }
    if (!body.valid) {
      llmProviderErrorForReason(body.reason);
      return;
    }
    showLlmProviderModels(body.models, provider, credentialFragment, body.project_id);
  }

  function confirmLlmProviderModel() {
    const select = document.getElementById("llm-provider-model-select");
    const model = select.value;
    if (!model || !pendingLlmProviderCredential) {
      llmProviderError("err_llm_no_model_selected");
      return;
    }
    const stored = {...pendingLlmProviderCredential, model};
    try {
      sessionStorage.setItem(STORAGE_KEYS["llm-provider"], JSON.stringify(stored));
    } catch (err) {
      llmProviderError("err_llm_storage_failed");
      return;
    }
    completeFrame("llm-provider", "provider_prefix", stored.provider);
  }

```

Add the restore-from-session line. Replace:
```javascript
    const supabaseState = readStoredSupabase();
    if (supabaseState && supabaseState.connection_string) {
      completeFrame("supabase", "restored", null);
    } else if (supabaseState && supabaseState.ref) {
      showSupabaseProvisioning();
      pollUntilReady(Date.now());
    }
  }
```
with:
```javascript
    const supabaseState = readStoredSupabase();
    if (supabaseState && supabaseState.connection_string) {
      completeFrame("supabase", "restored", null);
    } else if (supabaseState && supabaseState.ref) {
      showSupabaseProvisioning();
      pollUntilReady(Date.now());
    }
    if (sessionStorage.getItem(STORAGE_KEYS["llm-provider"])) {
      completeFrame("llm-provider", "restored", null);
    }
  }
```

Wire up the new event listeners. Replace:
```javascript
    document.getElementById("supabase-check-status-submit").addEventListener("click", checkSupabaseStatusOnce);
    guardLockedFrames();
```
with:
```javascript
    document.getElementById("supabase-check-status-submit").addEventListener("click", checkSupabaseStatusOnce);
    document.getElementById("llm-provider-validate-submit").addEventListener("click", validateLlmProviderCredential);
    document.getElementById("llm-provider-continue-submit").addEventListener("click", confirmLlmProviderModel);
    document.querySelectorAll('input[name="llm-provider-choice"]').forEach((r) => {
      r.addEventListener("change", handleLlmProviderChoice);
    });
    guardLockedFrames();
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_page.py -v`
Expected: PASS (all page tests, including the 7 new ones)

- [ ] **Step 5: Run the full test suite**

Run: `uv run pytest -q`
Expected: PASS (no regressions in other test files)

- [ ] **Step 6: Commit**

```bash
git add onboarding/static/index.html tests/test_onboarding_page.py
git commit -m "feat: add frame 4 markup and Gemini/Groq credential flow"
```

---

### Task 5: `onboarding/static/index.html` — Vertex file-picker flow + `onboarding/CLAUDE.md`

**Files:**
- Modify: `onboarding/static/index.html`
- Modify: `onboarding/CLAUDE.md`
- Modify: `tests/test_onboarding_page.py`

**Interfaces:**
- Consumes: `POST /api/llm/vertex/list-models` (Task 3);
  `validateLlmProviderCredential()`, `showLlmProviderModels()`,
  `resetLlmProviderSection()` (Task 4).
- Produces: `readFileAsBase64(file)`,
  `base64ToJsonSanityCheck(b64)` — no further tasks in this sub-project
  build on these.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_onboarding_page.py`:
```python
async def test_vertex_llm_endpoint_leaves_the_page_exactly_once():
    client = await _client()
    body = (await client.get("/")).text
    assert body.count('endpoint = "/api/llm/vertex/list-models"') == 1


async def test_vertex_file_is_read_via_filereader_and_base64_encoded():
    client = await _client()
    body = (await client.get("/")).text
    assert "function readFileAsBase64" in body
    assert "new FileReader()" in body
    assert "readAsDataURL(file)" in body


async def test_vertex_credential_gets_a_client_side_json_sanity_check():
    """Catches "wrong file entirely" before any network call — spec
    section 3 step 2."""
    client = await _client()
    body = (await client.get("/")).text
    assert "function base64ToJsonSanityCheck" in body
    assert "JSON.parse(decoded)" in body


async def test_vertex_credential_stored_under_the_spec_field_name():
    """Storage field is gcp_service_account_key_b64 (spec section 5),
    distinct from the wire field service_account_key_b64 (spec section 4)
    the relay endpoint expects — the frame maps between the two."""
    client = await _client()
    body = (await client.get("/")).text
    assert "credentialFragment = {gcp_service_account_key_b64: b64}" in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_page.py -v`
Expected: FAIL — `readFileAsBase64`/`base64ToJsonSanityCheck` don't exist
yet, and the Vertex branch of `validateLlmProviderCredential()` still just
shows `err_llm_no_file` unconditionally.

- [ ] **Step 3: Modify `onboarding/static/index.html`**

Add the two new helper functions right before
`async function validateLlmProviderCredential() {`:
```javascript
  function readFileAsBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const result = reader.result;
        const commaIdx = result.indexOf(",");
        resolve(commaIdx >= 0 ? result.slice(commaIdx + 1) : result);
      };
      reader.onerror = () => reject(reader.error);
      reader.readAsDataURL(file);
    });
  }

  function base64ToJsonSanityCheck(b64) {
    try {
      const decoded = atob(b64);
      JSON.parse(decoded);
      return true;
    } catch (err) {
      return false;
    }
  }

```

Replace the Vertex branch stub:
```javascript
    } else {
      llmProviderError("err_llm_no_file");
      return;
    }
```
with:
```javascript
    } else {
      const fileInput = document.getElementById("llm-provider-file-input");
      const file = fileInput.files[0];
      if (!file) {
        llmProviderError("err_llm_no_file");
        return;
      }
      let b64;
      try {
        b64 = await readFileAsBase64(file);
      } catch (err) {
        llmProviderError("err_llm_invalid_service_account_json");
        return;
      }
      if (!(await base64ToJsonSanityCheck(b64))) {
        llmProviderError("err_llm_invalid_service_account_json");
        return;
      }
      endpoint = "/api/llm/vertex/list-models";
      payload = {service_account_key_b64: b64};
      credentialFragment = {gcp_service_account_key_b64: b64};
    }
```

- [ ] **Step 4: Modify `onboarding/CLAUDE.md`**

Insert a new section after "What sub-project 3 (Supabase provisioning)
adds to these rules" ends and before "## The test suite looks hung on a
fresh worktree — it isn't" begins. Replace:
```markdown
- **The OAuth app is a resource shared across every visitor** — unlike
  every other credential in this service. This is a known, deliberately
  deferred risk; see `ISSUES.md`'s Design Gaps section before changing
  anything about how the OAuth app is used or exposed.

## The test suite looks hung on a fresh worktree — it isn't
```
with:
```markdown
- **The OAuth app is a resource shared across every visitor** — unlike
  every other credential in this service. This is a known, deliberately
  deferred risk; see `ISSUES.md`'s Design Gaps section before changing
  anything about how the OAuth app is used or exposed.

## What sub-project 4 (LLM provider credential UI) adds to these rules

- **The model this deployment runs is always fetched live from the
  provider's own catalog, never hardcoded.** `onboarding/llm_client.py`
  makes exactly one models-listing call per credential submission, which
  doubles as validation. No provider's default/fallback model string may
  be hardcoded anywhere in this service — that is exactly the drift this
  sub-project exists to avoid (root `CLAUDE.md`'s substitutions section
  documents the real incident this generalizes from: `gemini-flash-latest`
  404s against Vertex's publisher-model catalog).
- **Gemini and Vertex share one internal helper**
  (`_list_generative_models`) since both go through the same `google-genai`
  SDK and differ only in how `genai.Client` is constructed. A change to the
  filtering/prefix-stripping logic belongs in that shared helper, not
  duplicated per provider.
- **Groq's model list is deliberately unfiltered** — its `Model` type
  carries no capability field to distinguish chat-completion models from
  Whisper/TTS/moderation ones, and a name-pattern heuristic was
  deliberately rejected as guessing at API behavior this project's
  testing-hygiene discipline warns against. Do not add one without a new
  brainstorm.
- **The frame's unlock gate requires both a live-validated credential AND
  an explicit model pick** — there is no fallback to any baked-in default
  if the visitor skips picking a model. A credential that validates but
  returns zero eligible models is a genuine dead end under this gate; it
  gets its own distinct error message (`err_llm_no_models_available`)
  rather than folding into a generic validation failure.
- **No operator-level settings were added for this sub-project** — unlike
  Supabase's OAuth app, every credential here is visitor-supplied per
  request. `onboarding/config.py` and `onboarding/main.py`'s `lifespan` are
  untouched by it.
- **Gemini/Vertex tests mock at the SDK client boundary
  (`google.genai.Client` itself is monkeypatched), not `respx`** —
  `google-genai`'s transport mixes `httpx` and `requests` depending on auth
  type, so a single `respx` mock cannot cleanly cover both paths. Groq's
  tests use `respx` as normal, since its SDK transport is pure `httpx`.
- **All three credentials share one `fetch(endpoint, ...)` call site**
  in `validateLlmProviderCredential()`, with `endpoint` set to a literal
  per-provider URL string in each branch — a second adaptation of the
  one-exit-path convention (alongside `callSupabaseRelay`'s): audited by
  checking each `endpoint = "/api/llm/<provider>/list-models"` assignment
  appears exactly once, plus the shared call site itself appears exactly
  once. A new provider added to this frame follows the same shape, not a
  new dedicated `fetch()` call.
- **The Vertex credential's storage field name
  (`gcp_service_account_key_b64`) deliberately differs from its wire field
  name (`service_account_key_b64`)** — the frame maps between them in
  `showLlmProviderModels`'s caller. Keep this distinction if either name
  changes: the wire name matches the relay endpoint's pydantic field, the
  storage name matches this service's `GCP_SERVICE_ACCOUNT_KEY`-adjacent
  naming convention for sub-project 6 to read later.

## The test suite looks hung on a fresh worktree — it isn't
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_page.py -v`
Expected: PASS (all page tests, including the 4 new ones)

- [ ] **Step 6: Run the full test suite**

Run: `uv run pytest -q`
Expected: PASS (no regressions anywhere)

- [ ] **Step 7: Commit**

```bash
git add onboarding/static/index.html onboarding/CLAUDE.md tests/test_onboarding_page.py
git commit -m "feat: add Vertex file-picker credential flow"
```
