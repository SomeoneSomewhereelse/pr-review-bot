# Vertex AI Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reinstate `vertex` as a real, live-runnable third LLM provider alongside `gemini` and `groq`, with a GCP service-account credential that resolves through the existing numbered-slot / key-index machinery.

**Architecture:** `vertex` becomes a third entry in the existing provider seam (`registry.PROVIDERS`, `KEY_INDEX_COLUMNS`, `KNOWN_PROVIDERS`) so every generic mechanism — credential resolution, the DB key-index override, the CLI, the dispatcher refresh — picks it up unchanged. The one structural difference is the credential shape: not an API-key string but a GCP service-account JSON key, resolved by a new narrow module `app/providers/vertex_credentials.py` in three layers (`GCP_SERVICE_ACCOUNT_KEY_B64` env → local key file → `None`, meaning implicit ADC). `factory._build` gains a vertex branch that bypasses the generic "empty credential → raise" fast-fail, because for vertex an empty credential legitimately means "fall through to ADC"; instead it fast-fails only when there is no project to call with.

**Tech Stack:** Python 3.12+, `uv`, FastAPI, `google-genai` (`genai.Client(vertexai=True, ...)`), `google-auth` (`google.oauth2.service_account`), Pydantic v2 / pydantic-settings, psycopg3 + Postgres, pytest / pytest-asyncio, ruff.

**Spec:** `docs/superpowers/specs/2026-08-13-vertex-ai-provider-design.md`

## Global Constraints

- **Secrets only via env vars; no secret is ever logged.** No credential value ever goes into Postgres — only the integer slot index. This design introduces **no** exception to that rule (spec §8, §12).
- **One deliberate live call per real verification need.** Never loop or burst live LLM calls across models or keys; if a provider returns 403/429, stop calling it (`CLAUDE.md`, "LLM API testing hygiene"). Only Task 5 makes a real network call; every other task is mocked.
- **Deterministic tests only in `tests/`** — no real network calls, ever. CI runs `ruff check .` + `pytest`.
- **ruff line-length is 100** (`pyproject.toml`). Lint rules: `E4, E7, E9, F, E501`.
- **Async throughout; one-purpose modules with narrow interfaces.** `app/` must never import from `scripts/`.
- **Partial failure stays visible** — a provider that fails to construct flows into the existing `asyncio.gather(..., return_exceptions=True)` → failed-specialist-row path; no new error handling anywhere.
- **DB-touching tests need Postgres** — `tests/conftest.py`'s `db` fixture spins a throwaway container via `testcontainers` (Docker required locally) or uses a local `DATABASE_URL`.
- **Provider naming is exactly `vertex`** (lowercase), env vars exactly `GCP_SERVICE_ACCOUNT_KEY_B64`, `GCP_SERVICE_ACCOUNT_KEY_PATH`, `GCP_PROJECT`, `GCP_LOCATION`; DB column exactly `vertex_key_index`.

## Notes on this plan's scope vs. the spec

Three things the spec's §10 "Surface" list does not mention, found while reading the code, are included here because the change does not compile/pass without them:

1. `tests/test_provider_registry.py` asserts `set(registry.PROVIDERS) == {"gemini", "groq"}` and the same for `KEY_INDEX_COLUMNS` — three assertions to update (Task 1).
2. `app/dashboard.py` builds its backoff panel from `KNOWN_PROVIDERS`, so `tests/test_dashboard_api.py:34` asserts the exact dict `{"gemini": None, "groq": None}` (Task 1).
3. `google.oauth2.service_account` comes from `google-auth`, currently only a *transitive* dependency of `google-genai`. Task 3 promotes it to an explicit direct dependency, since `app/` now imports it by name.

One deliberate non-change worth recording: `scripts/deploy.py`'s `check_config` / `check_provider` will report FAIL for `LLM_PROVIDER=vertex` when `GCP_SERVICE_ACCOUNT_KEY_B64` is unset, even if a local key file or ADC would satisfy the *running app* locally. That is correct — `deploy.py` answers "can this be deployed to Render", and Render has neither a local key file nor a `gcloud` ADC login, so the b64 form genuinely is required there. Task 1 adds a test pinning that behavior rather than leaving it implicit, and Task 6 documents it.

---

### Task 1: Register `vertex` as a known provider name

Makes `vertex` a first-class name everywhere the codebase enumerates providers: settings, the registry, `KNOWN_PROVIDERS`, and the `runtime_config` schema. Nothing can construct a `VertexProvider` yet — that lands in Tasks 3–4. Every test that asserted "vertex is an unsupported/retired name" flips in this task, because the registry entry is what makes those assertions false.

**Files:**
- Modify: `app/config.py:26` (after `groq_model`)
- Modify: `app/providers/registry.py:14-27`
- Modify: `app/providers/base.py:55`
- Modify: `app/queue/store.py:58` (after the `groq_key_index` ALTER)
- Modify: `app/providers/factory.py:29-37` (stale comment + accepted-values message)
- Modify: `.env.example`, `.gitignore`
- Test: `tests/test_provider_registry.py:13,22` (update)
- Test: `tests/test_config.py` (add)
- Test: `tests/test_key_index_override.py` (add)
- Test: `tests/test_dashboard_api.py:34` (update)
- Test: `tests/test_deploy_script.py:30,150,155-174,1506-1509` (update + add)
- Test: `tests/test_set_override_script.py:84-88` (update)
- Test: `tests/test_providers.py:124-131` (delete)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `settings.gcp_project: str`, `settings.gcp_location: str`, `settings.gcp_service_account_key_b64: str`, `settings.gcp_service_account_key_path: str`
  - `registry.PROVIDERS["vertex"] == ("GCP_SERVICE_ACCOUNT_KEY_B64", "LLM_MODEL")`
  - `registry.KEY_INDEX_COLUMNS["vertex"] == "vertex_key_index"`
  - `base.KNOWN_PROVIDERS == ("gemini", "groq", "vertex")`
  - `runtime_config.vertex_key_index` column
  - Therefore: `credentials.resolve("vertex", index) -> tuple[str, str]` works, and `store.get_key_index_override("vertex")` / `set_key_index_override("vertex", n, now)` / `get_all_key_index_overrides()` all cover vertex with no new store functions.

- [ ] **Step 1: Write the failing tests for the registry and settings**

Replace the three assertions in `tests/test_provider_registry.py` (lines 13, 22-24):

```python
def test_registry_lists_all_providers():
    assert set(registry.PROVIDERS) == {"gemini", "groq", "vertex"}


def test_registry_maps_each_provider_to_its_credential_and_model_env_vars():
    assert registry.PROVIDERS["gemini"] == ("GEMINI_API_KEY", "LLM_MODEL")
    assert registry.PROVIDERS["groq"] == ("GROQ_API_KEY", "GROQ_MODEL")
    assert registry.PROVIDERS["vertex"] == ("GCP_SERVICE_ACCOUNT_KEY_B64", "LLM_MODEL")


def test_registry_lists_a_key_index_column_per_provider():
    assert set(registry.KEY_INDEX_COLUMNS) == {"gemini", "groq", "vertex"}
    assert registry.KEY_INDEX_COLUMNS["gemini"] == "gemini_key_index"
    assert registry.KEY_INDEX_COLUMNS["groq"] == "groq_key_index"
    assert registry.KEY_INDEX_COLUMNS["vertex"] == "vertex_key_index"
```

Append to `tests/test_config.py` (`_env_file=None` because this repo's real `.env` may set these — a bare `Settings()` would read it and the assertion would pass in CI and fail locally):

```python
def test_vertex_settings_default_to_derive_everything_from_the_key(monkeypatch):
    """GCP_PROJECT is an OPTIONAL override: unset means "use the project_id
    embedded in the service-account key itself" (design doc §2).

    _env_file=None plus delenv because these defaults must be asserted against
    the code, not against whatever this working copy's .env or the developer's
    exported shell happens to say."""
    for name in (
        "GCP_PROJECT",
        "GCP_LOCATION",
        "GCP_SERVICE_ACCOUNT_KEY_B64",
        "GCP_SERVICE_ACCOUNT_KEY_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    assert settings.gcp_project == ""
    assert settings.gcp_location == "us-central1"
    assert settings.gcp_service_account_key_b64 == ""
    assert settings.gcp_service_account_key_path == "./gcp-service-account-key.json"
```

Append to `tests/test_provider_registry.py`:

```python
def test_known_providers_matches_the_registry():
    """app/dashboard.py builds its per-provider backoff panel from
    KNOWN_PROVIDERS; a provider in one and not the other renders a panel that
    silently omits a real provider."""
    from app.providers.base import KNOWN_PROVIDERS

    assert set(KNOWN_PROVIDERS) == set(registry.PROVIDERS)
```

Append to `tests/test_key_index_override.py`:

```python
def test_vertex_round_trips_through_the_generic_key_index_machinery():
    """vertex needs no new store functions -- it is a third entry under the
    already-generic KEY_INDEX_COLUMNS, including the migration that adds
    vertex_key_index to a runtime_config table that already existed."""
    store.set_key_index_override("vertex", 3, T0)
    assert store.get_key_index_override("vertex") == 3
    assert store.get_all_key_index_overrides() == {"vertex": 3}
    store.set_key_index_override("vertex", None, T1)
    assert store.get_key_index_override("vertex") is None


def test_vertex_index_is_independent_of_the_other_providers():
    store.set_key_index_override("vertex", 2, T0)
    store.set_key_index_override("groq", 1, T0)
    assert store.get_key_index_override("vertex") == 2
    assert store.get_key_index_override("groq") == 1
    assert store.get_key_index_override("gemini") is None
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run:
```bash
uv run pytest tests/test_provider_registry.py tests/test_config.py tests/test_key_index_override.py -v
```
Expected: FAIL — `KeyError: 'vertex'`, `AttributeError: 'Settings' object has no attribute 'gcp_project'`, and `psycopg.errors.UndefinedColumn: column "vertex_key_index" does not exist`.

- [ ] **Step 3: Add the four settings**

In `app/config.py`, immediately after the `groq_model` line (line 26), before the `llm_request_timeout_seconds` comment block:

```python
    # --- Vertex AI (LLM_PROVIDER=vertex). Unlike gemini/groq, the credential
    # is a GCP service-account identity rather than an API-key string:
    # GCP_SERVICE_ACCOUNT_KEY_B64 (hosted) -> a local key file -> implicit ADC.
    # See app/providers/vertex_credentials.py for the resolution order.
    # An OPTIONAL override: unset means "use the project_id embedded in the
    # resolved service-account key", so an operator handed nothing but a JSON
    # key needs no separate project lookup.
    gcp_project: str = ""
    # Which Vertex regional endpoint to call -- not an account property, so the
    # default needs no lookup either.
    gcp_location: str = "us-central1"
    gcp_service_account_key_b64: str = ""
    gcp_service_account_key_path: str = "./gcp-service-account-key.json"
```

- [ ] **Step 4: Add the registry entries**

In `app/providers/registry.py`, replace the `PROVIDERS` and `KEY_INDEX_COLUMNS` dicts:

```python
# provider -> (credential env var, model env var)
PROVIDERS = {
    "gemini": ("GEMINI_API_KEY", "LLM_MODEL"),
    "groq": ("GROQ_API_KEY", "GROQ_MODEL"),
    # vertex's credential is a base64-encoded service-account JSON key, not an
    # API-key string -- but it is resolved through the same numbered-slot
    # mechanism (credentials.resolve), so it belongs in the same table.
    # app/providers/vertex_credentials.py layers the local-file and
    # implicit-ADC fallbacks on top of what this entry resolves.
    "vertex": ("GCP_SERVICE_ACCOUNT_KEY_B64", "LLM_MODEL"),
}

# provider -> the runtime_config column holding its active API-key-slot
# index override. A hardcoded whitelist, not a naming convention derived at
# call time -- every SQL statement that touches one of these columns looks
# the name up through this dict rather than building it from a caller's
# `provider` string, so this dict IS the injection guard for those callers.
KEY_INDEX_COLUMNS = {
    "gemini": "gemini_key_index",
    "groq": "groq_key_index",
    "vertex": "vertex_key_index",
}
```

- [ ] **Step 5: Add vertex to `KNOWN_PROVIDERS` and add the schema column**

In `app/providers/base.py`, line 55:

```python
KNOWN_PROVIDERS = ("gemini", "groq", "vertex")
```

In `app/queue/store.py`, immediately after the `groq_key_index` line (line 58):

```sql
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS vertex_key_index INTEGER;
```

- [ ] **Step 6: Run the new tests to verify they pass**

Run:
```bash
uv run pytest tests/test_provider_registry.py tests/test_config.py tests/test_key_index_override.py -v
```
Expected: PASS.

- [ ] **Step 7: Run the full suite to see exactly what the registry change broke**

Run:
```bash
uv run pytest -q
```
Expected: FAIL in exactly these places (fixed in Steps 8-10) — `tests/test_dashboard_api.py::test_dashboard_payload_shape`-style backoff assertion, `tests/test_deploy_script.py::test_providers_table_covers_every_supported_provider`, the three deploy tests using `"vertex"` as their example of an unsupported provider, `tests/test_set_override_script.py::test_rejects_an_unsupported_provider`, and `tests/test_providers.py::test_factory_rejects_retired_vertex_provider`. If anything *else* fails, stop and report it rather than adapting the test.

- [ ] **Step 8: Update the tests that asserted vertex is an unknown name**

`tests/test_dashboard_api.py`, line 34:

```python
    assert body["queue"]["backoff"] == {"gemini": None, "groq": None, "vertex": None}
```

`tests/test_deploy_script.py`, line 30 — the autouse quarantine must cover vertex's credential too, since `_wanted_env()` reads every provider's credential and a developer's real GCP key would otherwise flow into mocked request bodies:

```python
    for name in ("gemini_api_key", "groq_api_key", "gcp_service_account_key_b64"):
        monkeypatch.setattr(settings, name, "")
```

`tests/test_deploy_script.py`, line 150:

```python
    assert set(deploy._PROVIDERS) == {"gemini", "groq", "vertex"}
```

`tests/test_deploy_script.py`, lines 155-174 — swap the example unsupported value, since `vertex` is now supported:

```python
def test_check_config_fails_on_an_unrecognized_provider(complete_config, monkeypatch):
    """An unrecognized value used to contribute no requirement and pass with
    nothing verified."""
    monkeypatch.setattr(settings, "llm_provider", "unknown")
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "unknown" in result.detail
    assert "gemini" in result.detail


def test_check_config_reports_a_bad_provider_alongside_other_missing_keys(
    complete_config, monkeypatch
):
    """An unsupported provider must not mask problems already collected --
    one run surfaces every problem, per this module's own contract."""
    monkeypatch.setattr(settings, "llm_provider", "unknown")
    monkeypatch.setattr(settings, "github_webhook_secret", "")
    detail = deploy.check_config().detail
    assert "GITHUB_WEBHOOK_SECRET" in detail
    assert "unknown" in detail
```

`tests/test_deploy_script.py`, line 1508:

```python
    monkeypatch.setattr(deploy, "_resolved_provider_or_env", lambda: ("unknown", None))
```

`tests/test_set_override_script.py`, lines 84-88:

```python
def test_rejects_an_unsupported_provider(capsys):
    assert set_override.main(["unknown"]) == 2
    err = capsys.readouterr().err
    assert "unknown" in err
    assert "groq" in err
    assert store.get_provider_override() is None
```

- [ ] **Step 9: Delete the retirement test and pin the deploy-readiness rule**

Delete `tests/test_providers.py::test_factory_rejects_retired_vertex_provider` entirely (lines 124-131). It asserts a retirement this plan is undoing; Task 4 adds the positive construction tests that replace it.

Add to `tests/test_deploy_script.py`, next to `test_check_config_requires_the_gemini_key_when_gemini_selected`:

```python
def test_check_config_requires_the_gcp_key_when_vertex_selected(complete_config, monkeypatch):
    """deploy.py answers "can this be DEPLOYED", and Render has neither a local
    key file nor a `gcloud` ADC login -- so the base64 form is genuinely
    required there even though a local run could resolve either fallback."""
    monkeypatch.setattr(settings, "llm_provider", "vertex")
    monkeypatch.setattr(settings, "gcp_service_account_key_b64", "")
    result = deploy.check_config()
    assert result.status == "FAIL"
    assert "GCP_SERVICE_ACCOUNT_KEY_B64" in result.detail
```

- [ ] **Step 10: Fix the now-stale comment in `factory._build`**

`app/providers/factory.py`, lines 29-37 — the comment names a test that Step 9 deleted, and the message names two providers where there are now three:

```python
    # Check membership BEFORE resolving any credential: resolve() does
    # registry.PROVIDERS[provider], an unguarded dict lookup that raises a
    # bare KeyError for an unknown name. test_factory_raises_for_unknown_provider
    # expects ValueError with a message naming the accepted providers --
    # resolving first would raise the wrong exception type before ever
    # reaching the check below.
    if provider not in registry.PROVIDERS:
        raise ValueError(
            f"Unknown provider: {provider!r} (expected 'gemini', 'groq', or 'vertex')"
        )
```

- [ ] **Step 11: Run the full suite green**

Run:
```bash
uv run ruff check . && uv run pytest -q
```
Expected: PASS, no failures. (`LLM_PROVIDER=vertex` at this point still raises "no credential configured for provider='vertex'" from the generic fast-fail — the vertex branch lands in Task 4. Nothing in the suite exercises that path.)

- [ ] **Step 12: Add the config documentation for the new env vars**

`.gitignore`, under `# Secrets`, after the `*.pem` line — the glob covers both the default filename and the numbered local siblings (`GCP_SERVICE_ACCOUNT_KEY_PATH_1`, `_2`, ...) an operator names by convention:

```gitignore
# GCP service-account key files (default name + any numbered local siblings)
gcp-service-account-key*.json
```

`.env.example` — change the `LLM_PROVIDER` comment line from `# One of: gemini | groq` to:

```
# One of: gemini | groq | vertex
```

and add this stanza after the existing `groq` block (before the `LLM_MODEL` comment), following the same path-for-local / b64-for-hosted style the GitHub App key already uses:

```
# vertex (Vertex AI via google-genai's vertexai=True -- needs GCP billing)
# The credential is a GCP service-account JSON key, not an API-key string.
# Render/hosted: base64 of the whole key file (set in the dashboard, never committed).
GCP_SERVICE_ACCOUNT_KEY_B64=
# GCP_SERVICE_ACCOUNT_KEY_B64_1=
# GCP_SERVICE_ACCOUNT_KEY_B64_2=
# Local development: path to the downloaded key JSON (kept out of git; see .gitignore).
# Selected by the SAME key index as the B64 slots above; B64 wins when both
# resolve. With neither set, google-auth falls back to your own
# `gcloud auth application-default login` credentials.
GCP_SERVICE_ACCOUNT_KEY_PATH=./gcp-service-account-key.json
# GCP_SERVICE_ACCOUNT_KEY_PATH_1=
# GCP_SERVICE_ACCOUNT_KEY_PATH_2=
# Optional: derived from the service-account key's own project_id when unset.
GCP_PROJECT=
# Optional: which Vertex regional endpoint to call.
GCP_LOCATION=us-central1
```

Note `LLM_MODEL` is shared by gemini and vertex (both are the google-genai family); update its existing comment from "for the gemini provider ONLY" to "for the google-genai providers (gemini and vertex) ONLY".

- [ ] **Step 13: Commit**

```bash
git add app/config.py app/providers/registry.py app/providers/base.py \
        app/providers/factory.py app/queue/store.py .env.example .gitignore \
        tests/test_provider_registry.py tests/test_config.py \
        tests/test_key_index_override.py tests/test_dashboard_api.py \
        tests/test_deploy_script.py tests/test_set_override_script.py \
        tests/test_providers.py
git commit -m "feat: register vertex as a known provider name

Settings, registry, KNOWN_PROVIDERS and the vertex_key_index column, so
every generic provider mechanism covers vertex. Construction lands next."
```

---

### Task 2: Vertex credential resolution (`vertex_credentials.py`)

The one module that knows Vertex's credential shape. `credentials.py` keeps its "one env var, one string" contract untouched — this module adds the local-file fallback and the JSON parsing on top, so the two providers that need neither stay simple.

**Files:**
- Create: `app/providers/vertex_credentials.py`
- Test: `tests/test_vertex_credentials.py` (create)

**Interfaces:**
- Consumes: `registry.PROVIDERS["vertex"]` and `settings.gcp_service_account_key_b64` / `gcp_service_account_key_path` from Task 1; the existing, **unmodified** `credentials.resolve(provider: str, index: int) -> tuple[str, str]`.
- Produces: `vertex_credentials.resolve_service_account_info(index: int) -> dict | None` — the parsed service-account JSON, or `None` meaning "no explicit credential; let google-auth discover implicit ADC". Raises `ValueError` (incl. `binascii.Error` / `json.JSONDecodeError`, both subclasses) on a malformed value rather than silently falling through to the next layer.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_vertex_credentials.py`:

```python
"""app/providers/vertex_credentials.py -- Vertex's three-layer credential
chain: GCP_SERVICE_ACCOUNT_KEY_B64 (index-aware) -> a local key file
(index-aware) -> None, meaning "let google-auth discover implicit ADC".

Hermetic by construction: the autouse fixture points every layer at nothing,
because a developer's real .env legitimately sets GCP_SERVICE_ACCOUNT_KEY_PATH
to a key file that exists on their machine -- without it, the "nothing
resolves" tests would pass in CI and fail locally.
"""
from __future__ import annotations

import base64
import json

import pytest

from app.config import settings
from app.providers import vertex_credentials

KEY = {
    "type": "service_account",
    "project_id": "proj-from-key",
    "client_email": "svc@proj-from-key.iam.gserviceaccount.com",
}
OTHER_KEY = {**KEY, "project_id": "proj-from-slot-1"}


def _b64(payload: dict) -> str:
    return base64.b64encode(json.dumps(payload).encode()).decode()


@pytest.fixture(autouse=True)
def _no_real_gcp_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "gcp_service_account_key_b64", "")
    monkeypatch.setattr(
        settings, "gcp_service_account_key_path", str(tmp_path / "absent.json")
    )
    for index in (1, 2):
        monkeypatch.delenv(f"GCP_SERVICE_ACCOUNT_KEY_B64_{index}", raising=False)
        monkeypatch.delenv(f"GCP_SERVICE_ACCOUNT_KEY_PATH_{index}", raising=False)


def test_index_zero_decodes_the_base_b64_env_var(monkeypatch):
    monkeypatch.setattr(settings, "gcp_service_account_key_b64", _b64(KEY))
    assert vertex_credentials.resolve_service_account_info(0) == KEY


def test_a_numbered_index_decodes_its_own_b64_env_var(monkeypatch):
    monkeypatch.setattr(settings, "gcp_service_account_key_b64", _b64(KEY))
    monkeypatch.setenv("GCP_SERVICE_ACCOUNT_KEY_B64_1", _b64(OTHER_KEY))
    assert vertex_credentials.resolve_service_account_info(1) == OTHER_KEY


def test_falls_back_to_the_local_key_file_when_no_b64_is_set(monkeypatch, tmp_path):
    key_file = tmp_path / "gcp-service-account-key.json"
    key_file.write_text(json.dumps(KEY))
    monkeypatch.setattr(settings, "gcp_service_account_key_path", str(key_file))
    assert vertex_credentials.resolve_service_account_info(0) == KEY


def test_a_numbered_index_falls_back_to_its_own_numbered_local_file(monkeypatch, tmp_path):
    """The local-dev case the design exists for: two real service accounts
    (e.g. a quota-exhausted one and a healthy one) selected by the SAME index
    that selects env-var slots on Render."""
    slot_1 = tmp_path / "key-1.json"
    slot_1.write_text(json.dumps(OTHER_KEY))
    monkeypatch.setenv("GCP_SERVICE_ACCOUNT_KEY_PATH_1", str(slot_1))
    assert vertex_credentials.resolve_service_account_info(1) == OTHER_KEY


def test_b64_wins_over_a_local_file_at_the_same_index(monkeypatch, tmp_path):
    key_file = tmp_path / "gcp-service-account-key.json"
    key_file.write_text(json.dumps(OTHER_KEY))
    monkeypatch.setattr(settings, "gcp_service_account_key_path", str(key_file))
    monkeypatch.setattr(settings, "gcp_service_account_key_b64", _b64(KEY))
    assert vertex_credentials.resolve_service_account_info(0) == KEY


def test_returns_none_when_nothing_resolves():
    """NOT an error for vertex -- None means "pass no explicit credentials to
    the client", which is exactly what triggers google-auth's implicit ADC
    discovery. Contrast gemini/groq, where an empty credential always means
    misconfigured."""
    assert vertex_credentials.resolve_service_account_info(0) is None


def test_a_numbered_index_does_not_fall_back_to_index_zero(monkeypatch):
    """An unprovisioned slot must resolve to "nothing here", not silently to
    the base slot -- a swap to an empty index must be visible, not a no-op."""
    monkeypatch.setattr(settings, "gcp_service_account_key_b64", _b64(KEY))
    assert vertex_credentials.resolve_service_account_info(2) is None


def test_malformed_base64_raises_rather_than_falling_through(monkeypatch, tmp_path):
    """A corrupt env var must surface, not quietly degrade to the next layer --
    that would run against a different account than the operator selected."""
    key_file = tmp_path / "gcp-service-account-key.json"
    key_file.write_text(json.dumps(KEY))
    monkeypatch.setattr(settings, "gcp_service_account_key_path", str(key_file))
    monkeypatch.setattr(settings, "gcp_service_account_key_b64", "!!!not-base64!!!")
    with pytest.raises(ValueError):
        vertex_credentials.resolve_service_account_info(0)


def test_valid_base64_that_is_not_json_raises(monkeypatch):
    monkeypatch.setattr(
        settings, "gcp_service_account_key_b64", base64.b64encode(b"nope").decode()
    )
    with pytest.raises(ValueError):
        vertex_credentials.resolve_service_account_info(0)


def test_malformed_json_in_the_local_file_raises(monkeypatch, tmp_path):
    key_file = tmp_path / "gcp-service-account-key.json"
    key_file.write_text("{ not json")
    monkeypatch.setattr(settings, "gcp_service_account_key_path", str(key_file))
    with pytest.raises(ValueError):
        vertex_credentials.resolve_service_account_info(0)


def test_a_configured_path_that_does_not_exist_is_not_an_error(monkeypatch, tmp_path):
    """.env.example ships GCP_SERVICE_ACCOUNT_KEY_PATH pre-filled with the
    default filename, so "configured but absent" is the ordinary state for
    anyone not using vertex -- it must mean "no key here", never a crash."""
    monkeypatch.setattr(
        settings, "gcp_service_account_key_path", str(tmp_path / "never-created.json")
    )
    assert vertex_credentials.resolve_service_account_info(0) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
uv run pytest tests/test_vertex_credentials.py -v
```
Expected: FAIL at collection — `ImportError: cannot import name 'vertex_credentials' from 'app.providers'`.

- [ ] **Step 3: Write the module**

Create `app/providers/vertex_credentials.py`:

```python
"""Resolves the Vertex service-account credential: GCP_SERVICE_ACCOUNT_KEY_B64
(env, index-aware) -> a local key file (index-aware) -> None (implicit ADC).
Never logged -- the same discipline app/github_app.py::_read_private_key
applies to the equivalent GitHub credential.

Separate from credentials.py on purpose: that module knows exactly one
credential shape ("one env var, one string") and stays that way for the two
providers that need nothing more. The local-file fallback and the JSON
parsing live here instead of complicating it for them.

One index, two real meanings depending on environment. On Render, where the
numbered GCP_SERVICE_ACCOUNT_KEY_B64_{n} slots are actually provisioned, the
index selects among env-var blobs. Locally, where those are typically never
exported, the same index falls through to selecting among numbered local
FILES -- which is how a developer tests against several different service
accounts (a quota-exhausted one vs. a healthy one) without touching Render
or Supabase at all.

A malformed value raises rather than falling through to the next layer: a
corrupt env var must surface as a failure, not silently run the review
against a different account than the operator selected.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from app.config import settings
from app.providers import credentials


def _local_path(index: int) -> str:
    """The key-file path for `index` -- Settings at 0, os.environ above it.

    Mirrors credentials.resolve()'s split for exactly the same reason:
    Settings cannot declare an unbounded family of numbered fields.
    """
    if index == 0:
        return settings.gcp_service_account_key_path
    return os.environ.get(f"GCP_SERVICE_ACCOUNT_KEY_PATH_{index}", "")


def resolve_service_account_info(index: int) -> dict | None:
    """The parsed service-account key for `index`, or None for implicit ADC.

    None is NOT an error here (unlike an empty gemini/groq credential): it is
    the signal to pass no explicit credentials to genai.Client, which is what
    makes google-auth discover `gcloud auth application-default login`'s local
    ADC file on its own.
    """
    _, b64 = credentials.resolve("vertex", index)
    if b64:
        return json.loads(base64.b64decode(b64).decode())
    path = _local_path(index)
    if path and Path(path).is_file():
        return json.loads(Path(path).read_text())
    return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:
```bash
uv run ruff check . && uv run pytest tests/test_vertex_credentials.py -v
```
Expected: PASS, all 11 tests.

- [ ] **Step 5: Run the full suite**

Run:
```bash
uv run pytest -q
```
Expected: PASS, no regressions.

- [ ] **Step 6: Commit**

```bash
git add app/providers/vertex_credentials.py tests/test_vertex_credentials.py
git commit -m "feat: resolve the Vertex service-account credential

Three layers -- GCP_SERVICE_ACCOUNT_KEY_B64 (index-aware) -> a local key
file (index-aware) -> None for implicit ADC -- in one narrow module, so
credentials.py keeps its one-env-var-one-string shape for gemini/groq."
```

---

### Task 3: The `VertexProvider` adapter

Adds the second client shape to `google_genai.py`, exactly as `SPEC.md` originally described this file: "Vertex (vertexai=True) + Gemini (api_key) — one SDK, two clients", sharing `_complete()`.

**Files:**
- Modify: `app/providers/google_genai.py:1-24` (docstring + imports), append `VertexProvider`
- Modify: `app/providers/pricing.py:15-18`
- Modify: `pyproject.toml` (dependencies)
- Test: `tests/test_providers.py` (add)

**Interfaces:**
- Consumes: `settings.llm_model`, `settings.llm_request_timeout_seconds`; the existing module-level `_complete(client, model, system, user, schema) -> LLMResponse`.
- Produces:
  - `VertexProvider(project: str, location: str, service_account_info: dict | None)` with `async def complete(self, system: str, user: str, schema: type[BaseModel]) -> LLMResponse` — satisfies the `LLMProvider` protocol.
  - `pricing._RATES[("vertex", "gemini-flash-latest")]`, so `estimate_cost_usd("vertex", ...)` stops raising `KeyError` before the factory can ever select vertex.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_providers.py`, in the `google_genai.py` section (after `test_provider_returns_none_parsed_on_off_schema_json`). Note the import at the top of the file must become `from app.providers.google_genai import GeminiProvider, VertexProvider`:

```python
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
```

Add to `tests/test_providers.py`'s `pricing.py` section:

```python
def test_estimate_cost_usd_vertex_flash():
    """Same model, same published rate as AI-Studio's paid tier -- the two
    providers differ in the auth path, not in what a token costs."""
    cost = pricing.estimate_cost_usd(
        "vertex", "gemini-flash-latest", tokens_in=4_000, tokens_out=500
    )
    assert cost == pytest.approx(0.0012 + 0.00125)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
uv run pytest tests/test_providers.py -v
```
Expected: FAIL at collection — `ImportError: cannot import name 'VertexProvider'`.

- [ ] **Step 3: Promote `google-auth` to a direct dependency**

`app/` is about to import `google.oauth2.service_account` by name, so it must not ride in as a transitive dependency of `google-genai`.

Run:
```bash
uv add "google-auth>=2.35"
git diff --stat pyproject.toml uv.lock
```
Expected: `pyproject.toml` gains one dependency line; `uv.lock` changes only to record google-auth as a direct requirement. If the lockfile shows unrelated version bumps, stop and report before continuing.

- [ ] **Step 4: Write the adapter**

In `app/providers/google_genai.py`, replace the "Deviation from SPEC.md" paragraph in the module docstring (lines 10-14) with:

```
This file holds BOTH google-genai client shapes, as SPEC.md section 4 always
described it: ``vertex`` (``vertexai=True``, a GCP service-account identity)
and ``gemini`` (an AI-Studio ``api_key``) -- one SDK, two clients, one shared
``_complete()``. The vertex adapter was removed once (Vertex AI required an
attached payment card, which this project's no-card constraint ruled out) and
reinstated on 2026-08-14 when GCP billing/ADC access became available; see
CLAUDE.md's "Substitutions from the brief".
```

Add to the imports (after `from google.genai import types`):

```python
from google.oauth2 import service_account
```

Append to the end of the file:

```python
class VertexProvider:
    """``vertex`` -- gemini-flash-latest via Vertex AI (``vertexai=True``).

    Differs from GeminiProvider only in authentication: a GCP service-account
    identity instead of an API key. ``service_account_info=None`` means "pass
    no explicit credentials", which is what makes google-auth discover the
    caller's implicit ADC (``gcloud auth application-default login``).
    ``credentials=None`` is genai.Client's own default, so passing it
    explicitly here is identical to omitting it.

    Reinstated once GCP billing/ADC access became available -- see CLAUDE.md's
    "Substitutions from the brief".
    """

    def __init__(
        self, project: str, location: str, service_account_info: dict | None
    ) -> None:
        creds = None
        if service_account_info is not None:
            creds = service_account.Credentials.from_service_account_info(
                service_account_info
            )
        self._client = genai.Client(
            vertexai=True,
            project=project,
            location=location,
            credentials=creds,
            http_options=types.HttpOptions(
                timeout=int(settings.llm_request_timeout_seconds * 1000)
            ),
        )
        self._model = settings.llm_model

    async def complete(self, system: str, user: str, schema: type[BaseModel]) -> LLMResponse:
        return await _complete(self._client, self._model, system, user, schema)
```

- [ ] **Step 5: Add the pricing entry**

In `app/providers/pricing.py`, add to `_RATES` and extend the comment block above it:

```python
# vertex/gemini-flash-latest: the same model at the same published rate as the
# gemini entry below -- Vertex and AI-Studio differ in the auth path, not in
# what a token costs. Kept as a separate key because estimate_cost_usd is
# called with the ACTIVE provider name, and a missing entry is a hard KeyError.
_RATES: dict[tuple[str, str], tuple[float, float]] = {
    ("gemini", "gemini-flash-latest"): (0.30, 2.50),
    ("vertex", "gemini-flash-latest"): (0.30, 2.50),
    ("groq", "llama-3.3-70b-versatile"): (0.59, 0.79),
}
```

- [ ] **Step 6: Run the tests to verify they pass**

Run:
```bash
uv run ruff check . && uv run pytest tests/test_providers.py -v
```
Expected: PASS.

- [ ] **Step 7: Run the full suite**

Run:
```bash
uv run pytest -q
```
Expected: PASS, no regressions.

- [ ] **Step 8: Commit**

```bash
git add app/providers/google_genai.py app/providers/pricing.py \
        pyproject.toml uv.lock tests/test_providers.py
git commit -m "feat: add the VertexProvider adapter

Second google-genai client shape (vertexai=True) sharing _complete() with
GeminiProvider, plus its pricing entry and an explicit google-auth
dependency now that app/ imports google.oauth2.service_account directly."
```

---

### Task 4: The factory's vertex branch

Wires the resolved credential into the adapter, with the one behavioral difference vertex has from gemini/groq: an empty resolved credential is not an error (it means implicit ADC), so the fast-fail moves from "no credential" to "no project".

**Files:**
- Modify: `app/providers/factory.py:17-55`
- Test: `tests/test_providers.py` (add, in the `factory.py` section)

**Interfaces:**
- Consumes: `vertex_credentials.resolve_service_account_info(index) -> dict | None` (Task 2), `VertexProvider(project, location, service_account_info)` (Task 3), `settings.gcp_project` / `settings.gcp_location` (Task 1).
- Produces: `get_provider()` returns a `VertexProvider` when the active provider is `"vertex"`; no signature changes anywhere.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_providers.py`'s `factory.py` section, where the deleted retirement test used to be:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
uv run pytest tests/test_providers.py -k vertex -v
```
Expected: FAIL — `AttributeError: <module 'app.providers.factory'> has no attribute 'vertex_credentials'`.

- [ ] **Step 3: Write the vertex branch**

In `app/providers/factory.py`, extend the imports:

```python
from app.providers import credentials, key_index, registry, vertex_credentials
from app.providers.active import active_provider
from app.providers.base import LLMProvider
from app.providers.google_genai import GeminiProvider, VertexProvider
from app.providers.groq import GroqProvider
```

and insert the vertex branch in `_build`, immediately after the membership check and **before** `credentials.resolve(...)`:

```python
    if provider == "vertex":
        # Deliberately ahead of the generic empty-credential fast-fail below:
        # for vertex an empty resolved credential is legitimate (it means
        # "fall through to implicit ADC"), unlike gemini/groq where an empty
        # string always means misconfigured. The locally-detectable invalid
        # state here is instead "no project to call with at all", which
        # happens only with no GCP_PROJECT override AND no service-account key
        # anywhere to derive one from. Both steps are local -- decoding an env
        # var, reading a file -- so this is still a no-network fast-fail, just
        # performed after credential resolution instead of before it.
        info = vertex_credentials.resolve_service_account_info(index)
        project = settings.gcp_project or (info or {}).get("project_id", "")
        if not project:
            raise ValueError(
                "no credential configured for provider='vertex': GCP_PROJECT not set "
                "and no service-account key found to derive it from"
            )
        return VertexProvider(
            project=project,
            location=settings.gcp_location,
            service_account_info=info,
        )
```

This needs `settings` in scope — add `from app.config import settings` to the imports if it is not already there.

Also extend the module docstring's first paragraph to note the asymmetry:

```
Narrow on purpose: this module knows which class to instantiate and which
credential to hand it -- nothing about provider internals beyond that. The
one asymmetry is vertex, whose credential is a service-account identity
rather than an API-key string and whose absence means "use implicit ADC"
rather than "misconfigured"; app/providers/vertex_credentials.py owns that
resolution, this module only branches on it.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:
```bash
uv run ruff check . && uv run pytest tests/test_providers.py -v
```
Expected: PASS, including the pre-existing `test_factory_selects_gemini`, `test_factory_selects_groq`, `test_factory_raises_for_unknown_provider`, and the caching tests.

- [ ] **Step 5: Run the full suite**

Run:
```bash
uv run pytest -q
```
Expected: PASS, no regressions.

- [ ] **Step 6: Commit**

```bash
git add app/providers/factory.py tests/test_providers.py
git commit -m "feat: build VertexProvider from the factory

Vertex branch bypasses the generic empty-credential fast-fail -- an empty
credential means implicit ADC here -- and fast-fails on a missing project
instead, deriving it from the key's own project_id when GCP_PROJECT is unset."
```

---

### Task 5: Live verification script

The single deliberate live call that proves vertex actually works end-to-end. Mirrors `scripts/manual_verify_step4.py`.

**Files:**
- Create: `scripts/manual_verify_vertex.py`
- Modify: `README.md` (the "Live verification scripts" table)

**Interfaces:**
- Consumes: `vertex_credentials.resolve_service_account_info`, `VertexProvider`, `estimate_cost_usd`, `validate_and_repair` — all from Tasks 2-4.
- Produces: nothing importable; it is a CLI entry point (`uv run python -m bot.scripts.manual_verify_vertex`). Not collected by pytest (`testpaths = ["tests"]`).

- [ ] **Step 1: Write the script**

Create `scripts/manual_verify_vertex.py`:

```python
"""Manual live verification for the vertex provider (app/providers/*).

Not part of the pytest suite (CI never runs this) -- it depends on a real,
live call to Vertex AI using whatever credential
app/providers/vertex_credentials.py resolves: GCP_SERVICE_ACCOUNT_KEY_B64,
then a local key file, then implicit ADC (`gcloud auth application-default
login`).

Run it directly:

    uv run python -m bot.scripts.manual_verify_vertex

It proves, against real Vertex AI, through the real validate-repair layer:
  1. A structured-output call succeeds and returns a validated instance of a
     tiny test schema (not a bare string).
  2. Real, non-zero token usage (tokens_in/tokens_out) comes back.

ONE deliberate call, run once -- not looped, not repeated across models or
keys (see CLAUDE.md's "LLM API testing hygiene"). If it returns 403 or 429,
stop and investigate via docs rather than retrying.

Resolves key-index slot 0 only: the DB key-index override is a dispatcher-
runtime concern (it is refreshed into a process-local cache per claimed
ticket), and a one-shot CLI has no such cache to read. To verify a different
service account locally, point GCP_SERVICE_ACCOUNT_KEY_PATH at it.

Never prints the credential. The GCP project id IS printed -- an operator
needs to know which project was billed, and it is not a secret -- but no
private-key material ever is.
"""

from __future__ import annotations

import asyncio
import sys

from pydantic import BaseModel

from app.config import settings
from app.providers import vertex_credentials
from app.providers.google_genai import VertexProvider
from app.providers.pricing import estimate_cost_usd
from app.providers.validate import validate_and_repair


class Greeting(BaseModel):
    message: str


def main() -> int:
    info = vertex_credentials.resolve_service_account_info(0)
    project = settings.gcp_project or (info or {}).get("project_id", "")
    source = "service-account key" if info is not None else "implicit ADC (gcloud)"

    print(f"Provider: vertex   Model: {settings.llm_model}")
    print(f"Credential source: {source}")
    print(f"Project: {project or '(none resolved)'}   Location: {settings.gcp_location}")
    print("(never printing the credential)")

    if not project:
        print(
            "\nno project to call with: set GCP_PROJECT, or provide a service-account "
            "key via GCP_SERVICE_ACCOUNT_KEY_B64 / GCP_SERVICE_ACCOUNT_KEY_PATH",
            file=sys.stderr,
        )
        return 2

    provider = VertexProvider(
        project=project,
        location=settings.gcp_location,
        service_account_info=info,
    )

    system = "Respond in the given JSON schema."
    user = "Say hello in one short sentence."

    print("\nMaking a real, live call through validate_and_repair() ...")
    result = asyncio.run(validate_and_repair(provider, system, user, Greeting))

    print(f"\nok: {result.ok}")
    assert result.ok, f"live call failed: {result.error}"
    assert result.parsed is not None
    assert isinstance(result.parsed, Greeting)

    print(f"parsed: {result.parsed!r}")
    print(f"tokens_in: {result.tokens_in}")
    print(f"tokens_out: {result.tokens_out}")

    assert result.tokens_in > 0, "expected non-zero real prompt token usage"
    assert result.tokens_out > 0, "expected non-zero real completion token usage"

    cost = estimate_cost_usd("vertex", settings.llm_model, result.tokens_in, result.tokens_out)
    print(f"estimated cost: ${cost:.6f}")

    print(
        "\nSUCCESS: live Vertex AI structured-output call verified "
        "end-to-end through validate_and_repair()."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Lint and confirm the suite is unaffected**

Run:
```bash
uv run ruff check . && uv run pytest -q
```
Expected: PASS. (`scripts/` is not in `testpaths`, so this script adds no tests.)

- [ ] **Step 3: Provision a real credential**

Either export a base64 key, or drop the JSON key file at the default gitignored path:

```bash
# local file (gitignored via gcp-service-account-key*.json)
cp ~/Downloads/<downloaded-key>.json ./gcp-service-account-key.json
# or, for the hosted shape:
# base64 -w0 ./gcp-service-account-key.json   # value for GCP_SERVICE_ACCOUNT_KEY_B64
```

Confirm the Vertex AI API is enabled on that project. If no credential is available yet, **stop here and report that**; do not mark this task complete or claim verification that did not run.

- [ ] **Step 4: Make the one live call**

Run:
```bash
uv run python -m bot.scripts.manual_verify_vertex
```
Expected: prints `ok: True`, a parsed `Greeting(message=...)`, non-zero `tokens_in`/`tokens_out`, an estimated cost, and `SUCCESS`.

**Run it once.** On a `403`/`429`, stop immediately and investigate via GCP docs/console — do not retry with different models, keys, or projects (`CLAUDE.md`, "LLM API testing hygiene").

- [ ] **Step 5: Add the script to the README's verification table**

In `README.md`'s "Live verification scripts" table, after the `manual_verify_groq.py` row:

```markdown
| `scripts/manual_verify_vertex.py` | Vertex AI provider (service-account or ADC) through the validate-repair layer |
```

- [ ] **Step 6: Commit**

```bash
git add scripts/manual_verify_vertex.py README.md
git commit -m "feat: add the vertex live-verification script

One deliberate live call through validate_and_repair, mirroring
manual_verify_step4.py. Verified live: real structured output, non-zero
token usage."
```

(If Step 4 could not run for lack of a credential, drop the last sentence of that commit message rather than claiming a verification that did not happen.)

---

### Task 6: Documentation

Brings every doc that records vertex's removal into line with its reinstatement, without rewriting the history that explains why it was removed.

**Files:**
- Modify: `CLAUDE.md` (Tech stack bullet; "Substitutions from the brief" bullet)
- Modify: `SETUP.md` §2 (heading + a dated update bullet)
- Modify: `README.md` ("Known limitations", the override section, the test count)
- Modify: `SPEC.md:408-409`
- Modify: `cost.md:46-47`

**Interfaces:**
- Consumes: everything from Tasks 1-5.
- Produces: no code.

- [ ] **Step 1: Update `CLAUDE.md`**

Replace the Tech stack "AI" bullet:

```markdown
- **AI**: `LLMProvider` seam with three adapters — `gemini` and `vertex`
  (both the `google-genai` SDK: an AI-Studio API key vs. a GCP service-account
  identity) and `groq` (OpenAI-compatible, live primary).
  Selected via `LLM_PROVIDER` env var.
```

Replace the "`vertex` adapter removed" bullet under "Substitutions from the brief":

```markdown
- **`vertex` adapter reinstated (2026-08-14)** — it was removed when Vertex AI's
  payment-card requirement collided with this project's no-card constraint (see
  SETUP.md §2), leaving it live-unrunnable and mock-only. GCP billing/ADC access
  later became available, so `vertex` is back as a real, live-runnable third
  provider, matching `SPEC.md`'s stated default. Its credential is a GCP
  service-account identity rather than an API-key string:
  `GCP_SERVICE_ACCOUNT_KEY_B64` (hosted, numbered slots) → a local key file →
  implicit ADC, resolved in `app/providers/vertex_credentials.py`. No secret
  reaches Postgres — only the slot index, exactly as for gemini/groq.
```

Leave "Secrets only via env vars; **no secret is ever logged**" untouched — this design introduces no exception to it.

- [ ] **Step 2: Update `SETUP.md` §2**

Change the §2 heading (the file's existing convention is to append the resolution rather than rewrite the history):

```markdown
## 2. LLM provider — Groq (live); Vertex reinstated 2026-08-14; Gemini blocked, then resolved
```

Append a new bullet at the **end** of §2's bullet list, leaving the removal history above it intact:

```markdown
- **Vertex reinstated, 2026-08-14:** the no-card constraint that ruled Vertex
  out no longer applies — GCP billing/ADC access became available, so
  `LLM_PROVIDER=vertex` is now a real, live-runnable provider verified by
  `scripts/manual_verify_vertex.py`. Its credential is a GCP service-account
  identity, not an API key, resolved in three layers by
  `app/providers/vertex_credentials.py`:
  1. `GCP_SERVICE_ACCOUNT_KEY_B64` (+ numbered `_1`/`_2` siblings) — the
     hosted/Render path, selected by the same `vertex_key_index` override
     gemini/groq use.
  2. `GCP_SERVICE_ACCOUNT_KEY_PATH` (default `./gcp-service-account-key.json`,
     gitignored; + numbered siblings) — local-dev only, for testing several
     service accounts without touching Render or Supabase.
  3. Implicit ADC — with neither of the above, `google-auth` discovers
     `gcloud auth application-default login`'s local credentials on its own.

  `GCP_PROJECT` is an **optional** override: unset, the project is read from
  the service-account key's own `project_id`, so an operator handed nothing
  but a JSON key needs no separate project lookup. `GCP_LOCATION` defaults to
  `us-central1`.

  **Deploying vertex to Render requires the base64 form.** Render has neither
  a local key file nor a `gcloud` login, so `scripts/deploy.py`'s `config` and
  `provider` checks FAIL for `LLM_PROVIDER=vertex` unless
  `GCP_SERVICE_ACCOUNT_KEY_B64` is set locally — that is the value `--sync-env`
  pushes. A file-only local setup is fine for running the app locally, but is
  deliberately not considered deployable.
```

- [ ] **Step 3: Update `README.md`**

Replace the "Vertex AI" bullet under "Known limitations":

```markdown
- **Vertex AI**: live (`LLM_PROVIDER=vertex`), reinstated 2026-08-14 — it had
  been removed while this project's no-card constraint made it unrunnable, and
  came back once GCP billing/ADC access became available. Unlike the other two
  providers its credential is a GCP service-account identity:
  `GCP_SERVICE_ACCOUNT_KEY_B64` (hosted) → a local key file → implicit ADC.
  Verified live via `scripts/manual_verify_vertex.py`. See `SETUP.md` §2.
```

In the "Switching providers and API keys without a redeploy" section, extend the numbered-siblings paragraph so vertex is a third example of the *same* mechanism rather than a new section:

```markdown
Each provider's credential env var can have numbered siblings —
`GROQ_API_KEY`, `GROQ_API_KEY_1`, `GROQ_API_KEY_2`, ... — provisioned ahead
of time exactly like any other env var (one redeploy, via `--sync-env` or
the Render dashboard, to add a new slot). `vertex` rides the identical
mechanism with a differently-shaped credential: `GCP_SERVICE_ACCOUNT_KEY_B64`,
`_1`, `_2`, ... on Render, and locally the same index instead selects among
`GCP_SERVICE_ACCOUNT_KEY_PATH`, `_1`, `_2`, ... key files — so
`uv run python -m bot.scripts.set_override vertex --index 1` swaps service
accounts with no redeploy and no CLI change. Each provider tracks its own
key-index independently, so switching providers never disturbs the slot
chosen for the other two, and no secret value is ever written to, read
from, or logged by the database — only the slot's integer index is.
```

- [ ] **Step 4: Update the README test count**

Run:
```bash
uv run pytest -q | tail -3
```
Then replace `508 deterministic tests` in `README.md`'s "Testing" section with the actual reported number.

- [ ] **Step 5: Update `SPEC.md`**

At `SPEC.md:408-409`, the per-provider override column list still names the retired `github_models` provider. Replace:

```markdown
`runtime_config` override per provider (`gemini_key_index`, `groq_key_index`,
`vertex_key_index`) records which slot is active; `NULL` means index
```

`SPEC.md` §4's description of `vertex` as the default provider needs no correction — it now matches the code again.

- [ ] **Step 6: Update `cost.md`**

Replace the Vertex bullet under "Free-tier headroom":

```markdown
- **Vertex** on the **$300 GCP trial credit** (90 days) covers all LLM calls —
  configured and live as of 2026-08-14 (`LLM_PROVIDER=vertex`, see `SETUP.md`
  §2). Billed at the same per-token rate as the Gemini entry below
  (`app/providers/pricing.py`); the two differ in the auth path, not in price.
```

- [ ] **Step 7: Verify nothing in the docs still calls vertex removed**

Run:
```bash
grep -rn "vertex" --include='*.md' . | grep -vi 'docs/superpowers' | grep -i "remov\|retire\|not configured\|no-card"
```
Expected: matches only inside sentences that describe the *history* of the removal ("was removed … reinstated"), never a present-tense claim that vertex is absent.

- [ ] **Step 8: Final full verification**

Run:
```bash
uv run ruff check . && uv run pytest -q
```
Expected: PASS, zero failures.

- [ ] **Step 9: Commit**

```bash
git add CLAUDE.md SETUP.md README.md SPEC.md cost.md
git commit -m "docs: record the vertex provider's reinstatement

CLAUDE.md, SETUP.md §2, README.md, SPEC.md and cost.md now describe vertex
as live, keeping the removal history that explains why it ever went away.
Also fixes SPEC.md's stale github_models_key_index column reference."
```

---

## Verification checklist (whole plan)

- [ ] `uv run ruff check .` clean
- [ ] `uv run pytest -q` fully green
- [ ] `scripts/manual_verify_vertex.py` run once, live, printing `SUCCESS` (Task 5, Step 4)
- [ ] `grep -rn "GCP_SERVICE_ACCOUNT" app/ scripts/` shows no value ever printed, logged, or written to Postgres
- [ ] `git log --oneline` shows six commits, one per task
