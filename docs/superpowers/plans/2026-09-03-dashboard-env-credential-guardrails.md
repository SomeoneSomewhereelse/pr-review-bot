# Dashboard Environment Credential Guardrails Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add validated, dependency-aware credential management (guided add/replace, cascade-delete, live model/key-slot selection, blur/push validation) to the dashboard's Environment tab, for the LLM provider (gemini/groq/vertex) and GitHub App credential families.

**Architecture:** Two new pure/IO-light bot-side modules (`bot/providers/catalog.py` for live model listing, `bot/config_deps.py` for dependency computation) back five new/extended `dashboard/environment.py` routes, which a set of new `dashboard.html` UI elements (guided modal, model/key-slot selects, blur-validated fields) call. No code is imported from `onboarding/` — logic is reimplemented independently since `bot/` may become its own repo.

**Tech Stack:** FastAPI (dashboard routes), Pydantic (request models), `google-genai`/`groq`/`PyGithub` SDKs (live validation calls), vanilla JS (frontend, no framework), pytest + `unittest.mock` (tests).

**Spec:** `docs/superpowers/specs/2026-09-03-dashboard-env-credential-guardrails-design.md`

## Global Constraints

- No secret value is ever logged, returned in an error message, or asserted against in a test log line — structural errors only (`unauthorized`/`forbidden`/`rate_limited`/`provider_unreachable`/`invalid_service_account_json` for LLM providers; `invalid_key`/`unauthorized`/`github_unreachable`/`installation_not_found`/`multiple_installations` for GitHub App).
- One deliberate live listing/metadata call per validation action — no retries, no burst (root `CLAUDE.md`'s LLM API testing hygiene).
- No code is imported from `onboarding/` anywhere in `bot/`/`dashboard/` — logic is reimplemented from scratch even where structurally similar.
- `GITHUB_APP_INSTALLATION_ID` is never a manually-editable input anywhere in the UI — always a read-only, derived display value.
- Every write still reports partial success/failure per-key (`{"applied": [...], "failed": [...]}`) — this project's standing "partial failure is always visible" convention.
- Vars are never reset to "blank" as an alternative to deletion — Render rejects empty values; cascade always means removing the entry outright.
- Full test suite (`uv run pytest -v`) and `uv run ruff check .` must be green before each commit that isn't purely test-scaffolding, and before the branch is considered done.

---

## Task 1: `bot/providers/catalog.py` — live model listing per provider

**Files:**
- Create: `bot/providers/catalog.py`
- Test: `bot/tests/test_catalog.py`

**Interfaces:**
- Consumes: nothing from other new modules (only stdlib + `google-genai`/`groq` SDKs + `bot.config.settings`).
- Produces: `CatalogResult(ok: bool, models: list[str] | None, error: str | None)` dataclass; `list_gemini_models(api_key: str) -> CatalogResult`; `list_groq_models(api_key: str) -> CatalogResult`; `list_vertex_models(service_account_info: dict | None, project_override: str | None = None, location_override: str | None = None) -> CatalogResult`. All three are synchronous functions (matching `bot/render_client.py`'s and `bot/github_app.py`'s sync style, so dashboard routes can wrap them in `asyncio.to_thread` the same way `dashboard/environment.py` already does everywhere else).

- [ ] **Step 1: Write the failing tests**

```python
# bot/tests/test_catalog.py
"""Mocked-SDK tests for bot/providers/catalog.py -- no live network calls,
ever, per root CLAUDE.md's LLM API testing hygiene section."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from bot.providers import catalog


class _FakeApiError(Exception):
    """Duck-typed stand-in for an SDK error carrying an HTTP-status-shaped
    attribute -- avoids depending on the exact constructor signature of any
    real SDK's exception class."""

    def __init__(self, code: int) -> None:
        super().__init__(f"fake error {code}")
        self.code = code


def _model(name: str, actions=None) -> MagicMock:
    m = MagicMock()
    m.name = name
    m.supported_actions = actions
    return m


class TestListGeminiModels:
    @patch("bot.providers.catalog.genai.Client")
    def test_success_strips_prefix_and_filters_non_generative(self, mock_client_cls):
        client = MagicMock()
        client.models.list.return_value = [
            _model("models/gemini-flash-latest", ["generateContent"]),
            _model("models/embedding-001", ["embedContent"]),
        ]
        mock_client_cls.return_value = client

        result = catalog.list_gemini_models("fake-key")

        assert result.ok is True
        assert result.models == ["gemini-flash-latest"]
        assert result.error is None

    @patch("bot.providers.catalog.genai.Client")
    def test_unauthorized_maps_to_structural_error(self, mock_client_cls):
        client = MagicMock()
        client.models.list.side_effect = _FakeApiError(401)
        mock_client_cls.return_value = client

        result = catalog.list_gemini_models("bad-key")

        assert result.ok is False
        assert result.error == "unauthorized"
        assert result.models is None

    @patch("bot.providers.catalog.genai.Client")
    def test_rate_limited_maps_to_structural_error(self, mock_client_cls):
        client = MagicMock()
        client.models.list.side_effect = _FakeApiError(429)
        mock_client_cls.return_value = client

        result = catalog.list_gemini_models("fake-key")

        assert result.error == "rate_limited"

    @patch("bot.providers.catalog.genai.Client")
    def test_unclassified_error_is_provider_unreachable(self, mock_client_cls):
        client = MagicMock()
        client.models.list.side_effect = RuntimeError("connection reset")
        mock_client_cls.return_value = client

        result = catalog.list_gemini_models("fake-key")

        assert result.error == "provider_unreachable"


class TestListGroqModels:
    @patch("bot.providers.catalog.Groq")
    def test_success_returns_model_ids(self, mock_groq_cls):
        client = MagicMock()
        response = MagicMock()
        response.data = [MagicMock(id="llama-3.3-70b-versatile"), MagicMock(id="llama3-8b-8192")]
        client.models.list.return_value = response
        mock_groq_cls.return_value = client

        result = catalog.list_groq_models("fake-key")

        assert result.ok is True
        assert result.models == ["llama-3.3-70b-versatile", "llama3-8b-8192"]

    @patch("bot.providers.catalog.Groq")
    def test_forbidden_maps_to_structural_error(self, mock_groq_cls):
        client = MagicMock()
        client.models.list.side_effect = _FakeApiError(403)
        mock_groq_cls.return_value = client

        result = catalog.list_groq_models("fake-key")

        assert result.ok is False
        assert result.error == "forbidden"


class TestListVertexModels:
    @patch("bot.providers.catalog.genai.Client")
    @patch("bot.providers.catalog.service_account.Credentials.from_service_account_info")
    def test_success_with_explicit_service_account(self, mock_from_info, mock_client_cls):
        mock_from_info.return_value = MagicMock()
        client = MagicMock()
        client.models.list.return_value = [_model("publishers/google/models/gemini-2.5-flash")]
        mock_client_cls.return_value = client

        result = catalog.list_vertex_models({"project_id": "proj-a", "token_uri": "x"})

        assert result.ok is True
        assert result.models == ["gemini-2.5-flash"]
        mock_client_cls.assert_called_once()
        assert mock_client_cls.call_args.kwargs["project"] == "proj-a"

    def test_no_project_derivable_is_invalid_service_account_json(self):
        result = catalog.list_vertex_models(None)

        assert result.ok is False
        assert result.error == "invalid_service_account_json"

    @patch("bot.providers.catalog.genai.Client")
    def test_project_override_takes_precedence(self, mock_client_cls):
        client = MagicMock()
        client.models.list.return_value = []
        mock_client_cls.return_value = client

        catalog.list_vertex_models(
            {"project_id": "embedded-proj"}, project_override="candidate-proj"
        )

        assert mock_client_cls.call_args.kwargs["project"] == "candidate-proj"

    @patch("bot.providers.catalog.service_account.Credentials.from_service_account_info")
    def test_bad_service_account_info_is_invalid_service_account_json(self, mock_from_info):
        mock_from_info.side_effect = ValueError("malformed")

        result = catalog.list_vertex_models({"project_id": "proj-a"})

        assert result.ok is False
        assert result.error == "invalid_service_account_json"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest bot/tests/test_catalog.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bot.providers.catalog'`

- [ ] **Step 3: Write the implementation**

```python
# bot/providers/catalog.py
"""Live model-catalog listing per LLM provider, for the dashboard's guided
credential setup/replace flow and its per-provider model picker.

Reimplemented independently of onboarding/llm_client.py (structurally
similar, deliberately not imported -- bot/ may become its own repo). Each
function makes exactly one deliberate live listing call, synchronously
(matching bot/render_client.py's and bot/github_app.py's sync style, so
dashboard/environment.py can wrap these in asyncio.to_thread like every
other write path it already has).

See docs/superpowers/specs/2026-09-03-dashboard-env-credential-guardrails-design.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from google import genai
from google.genai import types
from google.oauth2 import service_account
from groq import Groq

from bot.config import settings

_VERTEX_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
_LIST_TIMEOUT_MS = 10_000


@dataclass
class CatalogResult:
    ok: bool
    models: list[str] | None
    error: str | None


def _classify_status(status: int | None) -> str:
    if status == 401:
        return "unauthorized"
    if status == 403:
        return "forbidden"
    if status == 429:
        return "rate_limited"
    return "provider_unreachable"


def _classify_exception(exc: Exception) -> str:
    # Duck-typed on purpose: rather than depend on each SDK's own exception
    # class hierarchy (google-genai's and groq's differ, and either could
    # change shape across versions), read whichever HTTP-status-shaped
    # attribute is present. Every real SDK error we've seen carries one of
    # these two names.
    status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    return _classify_status(status)


def _list_generative_models(client: genai.Client) -> list[str]:
    names: list[str] = []
    for model in client.models.list():
        name = model.name or ""
        if "/" in name:
            name = name.rsplit("/", 1)[-1]
        actions = getattr(model, "supported_actions", None)
        if actions and "generateContent" not in actions:
            continue
        names.append(name)
    return names


def list_gemini_models(api_key: str) -> CatalogResult:
    try:
        client = genai.Client(
            api_key=api_key, http_options=types.HttpOptions(timeout=_LIST_TIMEOUT_MS)
        )
        models = _list_generative_models(client)
    except Exception as exc:  # noqa: BLE001 -- classified into a structural error below
        return CatalogResult(ok=False, models=None, error=_classify_exception(exc))
    return CatalogResult(ok=True, models=models, error=None)


def list_groq_models(api_key: str) -> CatalogResult:
    try:
        client = Groq(api_key=api_key, max_retries=0, timeout=10.0)
        response = client.models.list()
    except Exception as exc:  # noqa: BLE001
        return CatalogResult(ok=False, models=None, error=_classify_exception(exc))
    return CatalogResult(ok=True, models=[m.id for m in response.data], error=None)


def list_vertex_models(
    service_account_info: dict | None,
    project_override: str | None = None,
    location_override: str | None = None,
) -> CatalogResult:
    project = (
        project_override
        or settings.gcp_project
        or (service_account_info or {}).get("project_id", "")
    )
    if not project:
        return CatalogResult(ok=False, models=None, error="invalid_service_account_json")
    location = location_override or settings.gcp_location

    creds = None
    if service_account_info is not None:
        try:
            creds = service_account.Credentials.from_service_account_info(
                service_account_info, scopes=_VERTEX_SCOPES
            )
        except Exception:  # noqa: BLE001 -- malformed key content, not an HTTP failure
            return CatalogResult(ok=False, models=None, error="invalid_service_account_json")

    try:
        client = genai.Client(
            vertexai=True,
            project=project,
            location=location,
            credentials=creds,
            http_options=types.HttpOptions(timeout=_LIST_TIMEOUT_MS),
        )
        models = _list_generative_models(client)
    except Exception as exc:  # noqa: BLE001
        return CatalogResult(ok=False, models=None, error=_classify_exception(exc))
    return CatalogResult(ok=True, models=models, error=None)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest bot/tests/test_catalog.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add bot/providers/catalog.py bot/tests/test_catalog.py
git commit -m "$(cat <<'EOF'
feat: add live model-catalog listing per LLM provider

New bot/providers/catalog.py, reimplemented independently of
onboarding/llm_client.py so bot/ can eventually split into its own repo.
Backs the dashboard's guided credential setup and model picker.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01EDvWri9YivWrM32pVn7BA6
EOF
)"
```

---

## Task 2: `bot/config_deps.py` — credential-family dependency graph

**Files:**
- Create: `bot/config_deps.py`
- Test: `bot/tests/test_config_deps.py`

**Interfaces:**
- Consumes: `bot.providers.registry.slot_env_name(provider: str, index: int) -> str`.
- Produces: `CREDENTIAL_FAMILIES: dict[str, dict]`, `MAX_CREDENTIAL_SLOTS: int = 5`, `credential_slot_vars(family: str) -> list[str]`, `slot_index_for_var(family: str, var: str) -> int | None`, `DeleteDependents` dataclass with `.labels() -> list[str]` and `.any() -> bool`, `dependents_of(var: str, *, key_index_overrides: dict[str, int], provider_override: str | None) -> DeleteDependents | None`, `conflicts_for(family: str, new_project_id: str | None, current_gcp_project: str | None) -> list[dict[str, str]]`.

- [ ] **Step 1: Write the failing tests**

```python
# bot/tests/test_config_deps.py
"""Pure-function tests -- no DB, no mocks, no I/O."""
from __future__ import annotations

from bot.config_deps import (
    conflicts_for,
    credential_slot_vars,
    dependents_of,
    slot_index_for_var,
)


def test_credential_slot_vars_gemini_matches_registry_slots():
    assert credential_slot_vars("gemini") == [
        "GEMINI_API_KEY",
        "GEMINI_API_KEY_1",
        "GEMINI_API_KEY_2",
        "GEMINI_API_KEY_3",
        "GEMINI_API_KEY_4",
    ]


def test_credential_slot_vars_github_app_is_fixed_pair():
    assert credential_slot_vars("github_app") == ["GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY"]


def test_slot_index_for_var_finds_numbered_slot():
    assert slot_index_for_var("groq", "GROQ_API_KEY_2") == 2


def test_slot_index_for_var_finds_base_slot_as_zero():
    assert slot_index_for_var("vertex", "GCP_SERVICE_ACCOUNT_KEY") == 0


def test_slot_index_for_var_returns_none_for_unrelated_var():
    assert slot_index_for_var("groq", "SOME_OTHER_VAR") is None


def test_slot_index_for_var_returns_none_for_github_app():
    assert slot_index_for_var("github_app", "GITHUB_APP_ID") is None


def test_dependents_of_flags_matching_key_index_override():
    dependents = dependents_of(
        "GEMINI_API_KEY_2",
        key_index_overrides={"gemini": 2},
        provider_override="groq",
    )
    assert dependents is not None
    assert dependents.key_index_override is True
    assert dependents.provider_override is False
    assert dependents.labels() == ["key_index override"]


def test_dependents_of_flags_active_provider():
    dependents = dependents_of(
        "GEMINI_API_KEY", key_index_overrides={}, provider_override="gemini"
    )
    assert dependents.provider_override is True
    assert dependents.any() is True


def test_dependents_of_returns_none_when_nothing_points_at_it():
    dependents = dependents_of(
        "GROQ_API_KEY_3", key_index_overrides={"groq": 1}, provider_override="gemini"
    )
    assert dependents is not None
    assert dependents.any() is False


def test_dependents_of_returns_none_for_non_credential_var():
    assert dependents_of("GCP_PROJECT", key_index_overrides={}, provider_override=None) is None


def test_conflicts_for_flags_project_mismatch():
    conflicts = conflicts_for("vertex", "new-project", "old-project")
    assert conflicts == [{"var": "GCP_PROJECT", "current": "old-project", "new": "new-project"}]


def test_conflicts_for_no_conflict_when_projects_match():
    assert conflicts_for("vertex", "same", "same") == []


def test_conflicts_for_empty_when_no_current_value_set():
    assert conflicts_for("vertex", "new-project", None) == []


def test_conflicts_for_empty_for_non_vertex_family():
    assert conflicts_for("gemini", "x", "y") == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest bot/tests/test_config_deps.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bot.config_deps'`

- [ ] **Step 3: Write the implementation**

```python
# bot/config_deps.py
"""Dependency graph for env vars grouped into "credential families".

Lets dashboard/environment.py's guided add/replace/delete flow and its
direct-edit validation stay generic across gemini/groq/vertex/github_app
instead of hardcoding per-provider branches. Pure logic, no I/O -- callers
fetch the current runtime_config/Render state and pass it in.

See docs/superpowers/specs/2026-09-03-dashboard-env-credential-guardrails-design.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from bot.providers.registry import slot_env_name

MAX_CREDENTIAL_SLOTS = 5

# "credential": the var(s) that ARE the identity. "model": the one var this
# family's live model picker writes (LLM providers only). "soft_deps": an
# independent var whose value can become STALE (not absent) when the
# credential changes -- Vertex's GCP_PROJECT/GCP_LOCATION, checked via
# conflicts_for. "derived": a var that is NEVER operator-authored, always
# recomputed from the credential -- GitHub App's installation id.
CREDENTIAL_FAMILIES: dict[str, dict] = {
    "gemini": {"credential": ["GEMINI_API_KEY"], "model": "LLM_MODEL"},
    "groq": {"credential": ["GROQ_API_KEY"], "model": "GROQ_MODEL"},
    "vertex": {
        "credential": ["GCP_SERVICE_ACCOUNT_KEY"],
        "model": "VERTEX_MODEL",
        "soft_deps": ["GCP_PROJECT", "GCP_LOCATION"],
    },
    "github_app": {
        "credential": ["GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY"],
        "derived": ["GITHUB_APP_INSTALLATION_ID"],
    },
}

# The only families whose credential occupies numbered slots (GEMINI_API_KEY_1,
# _2, ...) -- github_app has exactly one App identity, no slots.
_SLOTTED_FAMILIES = ("gemini", "groq", "vertex")


def credential_slot_vars(family: str) -> list[str]:
    """Every env var name `family`'s credential can occupy.

    For an LLM provider: the base var plus every numbered slot
    (`GEMINI_API_KEY`, `GEMINI_API_KEY_1`, ... up to MAX_CREDENTIAL_SLOTS-1).
    For github_app: its fixed two-var pair, unchanged (no slots).
    """
    if family not in _SLOTTED_FAMILIES:
        return list(CREDENTIAL_FAMILIES[family]["credential"])
    return [slot_env_name(family, i) for i in range(MAX_CREDENTIAL_SLOTS)]


def slot_index_for_var(family: str, var: str) -> int | None:
    """Which numbered slot `var` is within `family`'s credential, else None.

    Always None for github_app (not slotted) and for any var that isn't a
    member of `family`'s credential vars at all.
    """
    if family not in _SLOTTED_FAMILIES:
        return None
    for index, candidate in enumerate(credential_slot_vars(family)):
        if candidate == var:
            return index
    return None


@dataclass
class DeleteDependents:
    key_index_override: bool = False
    provider_override: bool = False

    def labels(self) -> list[str]:
        labels = []
        if self.key_index_override:
            labels.append("key_index override")
        if self.provider_override:
            labels.append("active provider override")
        return labels

    def any(self) -> bool:
        return self.key_index_override or self.provider_override


def dependents_of(
    var: str,
    *,
    key_index_overrides: dict[str, int],
    provider_override: str | None,
) -> DeleteDependents | None:
    """What runtime_config state would dangle if `var` were deleted.

    Only LLM-provider credential slots have anything to compute: github_app's
    credential vars are protected (dashboard/environment.py never reaches
    this path for them) and model vars aren't credential-slot-specific, so
    deleting a credential slot never needs to touch a model var. Returns
    None for any var that isn't an LLM-provider credential slot at all.
    """
    for family in _SLOTTED_FAMILIES:
        index = slot_index_for_var(family, var)
        if index is None:
            continue
        return DeleteDependents(
            key_index_override=key_index_overrides.get(family) == index,
            provider_override=provider_override == family,
        )
    return None


def conflicts_for(
    family: str, new_project_id: str | None, current_gcp_project: str | None
) -> list[dict[str, str]]:
    """Soft-dep mismatches a credential replacement should surface.

    Only vertex has a soft_dep whose correct value is derivable from the
    credential itself (GCP_PROJECT, embedded as `project_id` in the
    service-account JSON) -- GCP_LOCATION has no such embedded counterpart to
    compare against, so it's never flagged here, matching the design's
    "left untouched with a non-blocking note" decision.
    """
    if family != "vertex":
        return []
    if not current_gcp_project or not new_project_id:
        return []
    if current_gcp_project == new_project_id:
        return []
    return [{"var": "GCP_PROJECT", "current": current_gcp_project, "new": new_project_id}]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest bot/tests/test_config_deps.py -v`
Expected: PASS (13 tests)

- [ ] **Step 5: Commit**

```bash
git add bot/config_deps.py bot/tests/test_config_deps.py
git commit -m "$(cat <<'EOF'
feat: add credential-family dependency graph for env-var guardrails

Pure-logic module backing the dashboard's cascade-delete and Vertex
project-conflict prompts -- no I/O, callers pass in already-fetched state.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01EDvWri9YivWrM32pVn7BA6
EOF
)"
```

---

## Task 3: `GET /api/environment/render` gains `available_key_slots`; new `GET /api/environment/credential/{family}/models` refresh route

**Files:**
- Modify: `dashboard/environment.py`
- Modify: `bot/providers/credentials.py` (read-only reference, no change expected — confirm `resolve(provider, index) -> tuple[str, str]` signature matches)
- Test: `dashboard/tests/test_environment.py`

**Interfaces:**
- Consumes: `bot.providers.catalog.{list_gemini_models, list_groq_models, list_vertex_models}` (Task 1), `bot.config_deps.{credential_slot_vars, CREDENTIAL_FAMILIES}` (Task 2), `bot.providers.credentials.resolve(provider, index) -> tuple[str, str]`, `bot.providers.vertex_credentials.resolve_service_account_info(index) -> dict | None`, `bot.queue.store.get_all_key_index_overrides() -> dict[str, int]`.
- Produces: `GET /api/environment/render` response gains `"available_key_slots": {provider: [int]}`. New `GET /api/environment/credential/{family}/models?slot=N` route, LLM providers only, response `{"ok": bool, "models": list[str] | None, "error": str | None}`.

- [ ] **Step 1: Write the failing tests**

```python
# Append to dashboard/tests/test_environment.py

class TestAvailableKeySlots:
    def test_render_payload_includes_available_key_slots(
        self, client, session_cookie, monkeypatch
    ):
        monkeypatch.setattr(
            "dashboard.environment.render_client.find_service_id", lambda: "srv-1"
        )
        monkeypatch.setattr(
            "dashboard.environment.render_client.env_vars",
            lambda service_id: {
                "GEMINI_API_KEY": "x",
                "GEMINI_API_KEY_2": "y",
                "GROQ_API_KEY_1": "z",
                "DATABASE_URL": "postgres://...",
            },
        )
        resp = client.get("/api/environment/render", cookies=session_cookie)
        assert resp.status_code == 200
        slots = resp.json()["available_key_slots"]
        assert slots["gemini"] == [0, 2]
        assert slots["groq"] == [1]
        assert slots["vertex"] == []


class TestCredentialModelsRefresh:
    def test_refresh_resolves_current_slot_and_lists_models(
        self, client, session_cookie, monkeypatch
    ):
        monkeypatch.setattr(
            "dashboard.environment.store.get_all_key_index_overrides",
            lambda: {"gemini": 2},
        )
        monkeypatch.setattr(
            "dashboard.environment.credentials.resolve",
            lambda provider, index: ("GEMINI_API_KEY_2", "resolved-key")
            if (provider, index) == ("gemini", 2)
            else (_ for _ in ()).throw(AssertionError("wrong slot resolved")),
        )
        monkeypatch.setattr(
            "dashboard.environment.catalog.list_gemini_models",
            lambda api_key: catalog.CatalogResult(
                ok=True, models=["gemini-flash-latest"], error=None
            )
            if api_key == "resolved-key"
            else (_ for _ in ()).throw(AssertionError("wrong key passed")),
        )
        resp = client.get(
            "/api/environment/credential/gemini/models", cookies=session_cookie
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "models": ["gemini-flash-latest"], "error": None}

    def test_refresh_explicit_slot_overrides_current_override(
        self, client, session_cookie, monkeypatch
    ):
        monkeypatch.setattr(
            "dashboard.environment.store.get_all_key_index_overrides", lambda: {"gemini": 0}
        )
        monkeypatch.setattr(
            "dashboard.environment.credentials.resolve",
            lambda provider, index: ("GEMINI_API_KEY_3", "key-3")
            if index == 3
            else (_ for _ in ()).throw(AssertionError("expected slot 3")),
        )
        monkeypatch.setattr(
            "dashboard.environment.catalog.list_gemini_models",
            lambda api_key: catalog.CatalogResult(ok=True, models=["m"], error=None),
        )
        resp = client.get(
            "/api/environment/credential/gemini/models?slot=3", cookies=session_cookie
        )
        assert resp.status_code == 200

    def test_refresh_no_credential_configured(self, client, session_cookie, monkeypatch):
        monkeypatch.setattr(
            "dashboard.environment.store.get_all_key_index_overrides", lambda: {}
        )
        monkeypatch.setattr(
            "dashboard.environment.credentials.resolve", lambda provider, index: ("X", "")
        )
        resp = client.get(
            "/api/environment/credential/gemini/models", cookies=session_cookie
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "ok": False,
            "models": None,
            "error": "no_credential_configured",
        }

    def test_refresh_rejects_github_app_family(self, client, session_cookie):
        resp = client.get(
            "/api/environment/credential/github_app/models", cookies=session_cookie
        )
        assert resp.status_code == 404

    def test_refresh_vertex_resolves_service_account_info(
        self, client, session_cookie, monkeypatch
    ):
        monkeypatch.setattr(
            "dashboard.environment.store.get_all_key_index_overrides", lambda: {"vertex": 0}
        )
        monkeypatch.setattr(
            "dashboard.environment.vertex_credentials.resolve_service_account_info",
            lambda index: {"project_id": "proj-a"},
        )
        monkeypatch.setattr(
            "dashboard.environment.catalog.list_vertex_models",
            lambda info: catalog.CatalogResult(ok=True, models=["gemini-2.5-flash"], error=None)
            if info == {"project_id": "proj-a"}
            else (_ for _ in ()).throw(AssertionError("wrong info passed")),
        )
        resp = client.get(
            "/api/environment/credential/vertex/models", cookies=session_cookie
        )
        assert resp.status_code == 200
        assert resp.json()["models"] == ["gemini-2.5-flash"]
```

Add the needed import at the top of `dashboard/tests/test_environment.py`:

```python
from bot.providers import catalog
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest dashboard/tests/test_environment.py -v -k "AvailableKeySlots or CredentialModelsRefresh"`
Expected: FAIL — `available_key_slots` missing from response; `/api/environment/credential/gemini/models` 404s.

- [ ] **Step 3: Write the implementation**

Add imports and the `available_key_slots` computation to `dashboard/environment.py`:

```python
# Add to the import block at the top of dashboard/environment.py
from fastapi import APIRouter, HTTPException
from bot.config_deps import CREDENTIAL_FAMILIES, credential_slot_vars
from bot.providers import catalog, credentials, vertex_credentials

_LLM_PROVIDER_FAMILIES = ("gemini", "groq", "vertex")
```

(Note: `APIRouter` is already imported; add `HTTPException` alongside it in the existing `from fastapi import APIRouter` line.)

Modify `_build_render_payload`:

```python
def _build_render_payload() -> dict:
    service_id = render_client.find_service_id()
    if service_id is None:
        return {"vars": [], "available_key_slots": {p: [] for p in _LLM_PROVIDER_FAMILIES}}
    values = render_client.env_vars(service_id)
    available_key_slots = {
        provider: [
            i for i, var in enumerate(credential_slot_vars(provider)) if var in values
        ]
        for provider in _LLM_PROVIDER_FAMILIES
    }
    return {
        "vars": [
            {"key": key, "value": value, "protected": key in render_client.PROTECTED_ENV_KEYS}
            for key, value in values.items()
        ],
        "available_key_slots": available_key_slots,
    }
```

Add the new refresh route (place after the existing `GET /api/environment/config` route, before `EnvironmentConfigPatch`):

```python
def _resolve_current_credential(provider: str, slot: int | None) -> tuple[bool, str]:
    """Resolve the currently-stored credential for `provider`+`slot`.

    Returns (has_credential, value). `value` is a raw API key for
    gemini/groq; unused (empty string) for vertex, which resolves via
    vertex_credentials instead (see _fetch_models_for_provider).
    """
    if slot is None:
        overrides = store.get_all_key_index_overrides()
        slot = overrides.get(provider, 0)
    env_name, value = credentials.resolve(provider, slot)
    return bool(value), value


def _fetch_models_for_provider(provider: str, slot: int | None) -> dict:
    if provider == "vertex":
        if slot is None:
            slot = store.get_all_key_index_overrides().get("vertex", 0)
        info = vertex_credentials.resolve_service_account_info(slot)
        result = catalog.list_vertex_models(info)
    else:
        has_credential, api_key = _resolve_current_credential(provider, slot)
        if not has_credential:
            return {"ok": False, "models": None, "error": "no_credential_configured"}
        if provider == "gemini":
            result = catalog.list_gemini_models(api_key)
        else:
            result = catalog.list_groq_models(api_key)
    return {"ok": result.ok, "models": result.models, "error": result.error}


@router.get("/api/environment/credential/{family}/models")
async def get_credential_models(family: str, slot: int | None = None) -> JSONResponse:
    if family not in _LLM_PROVIDER_FAMILIES:
        raise HTTPException(status_code=404, detail="not an LLM provider family")
    payload = await asyncio.to_thread(_fetch_models_for_provider, family, slot)
    return JSONResponse(payload)
```

Note: `_resolve_current_credential` doesn't need the `env_name` it discards — this matches `credentials.resolve`'s actual `(env_var_name, value)` return shape from `bot/providers/credentials.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest dashboard/tests/test_environment.py -v`
Expected: PASS (full file, including pre-existing tests — confirms no regression)

- [ ] **Step 5: Commit**

```bash
git add dashboard/environment.py dashboard/tests/test_environment.py
git commit -m "$(cat <<'EOF'
feat: surface available key slots and add a live model-refresh route

GET /api/environment/render now reports which numbered credential slots
exist per LLM provider; GET /api/environment/credential/{family}/models
lets the dashboard fetch live models for an already-configured credential
without re-uploading it.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01EDvWri9YivWrM32pVn7BA6
EOF
)"
```

---

## Task 4: `POST /api/environment/validate/{var}` — blur/push validation for the five direct-edit vars

**Files:**
- Modify: `dashboard/environment.py`
- Test: `dashboard/tests/test_environment.py`

**Interfaces:**
- Consumes: `catalog.list_*_models` (Task 1), `credentials.resolve`, `vertex_credentials.resolve_service_account_info`, `store.get_all_key_index_overrides`.
- Produces: `POST /api/environment/validate/{var}` for `var` in `{GCP_PROJECT, GCP_LOCATION, LLM_MODEL, GROQ_MODEL, VERTEX_MODEL}`, body `{"value": str}`, response `{"ok": bool, "error": str | None, "models": list[str] | None}`. Extends `_apply_render_patch` (from Task 3's file) so a `PATCH /api/environment/render` touching one of these five vars re-validates server-side before applying, reporting a failure into `failed` with error `"failed_validation"` rather than writing it.

- [ ] **Step 1: Write the failing tests**

```python
# Append to dashboard/tests/test_environment.py

_MODEL_VAR_TO_PROVIDER = {"LLM_MODEL": "gemini", "GROQ_MODEL": "groq", "VERTEX_MODEL": "vertex"}


class TestValidateVar:
    def test_model_var_valid_when_in_catalog(self, client, session_cookie, monkeypatch):
        monkeypatch.setattr(
            "dashboard.environment.store.get_all_key_index_overrides", lambda: {}
        )
        monkeypatch.setattr(
            "dashboard.environment.credentials.resolve", lambda provider, index: ("X", "key")
        )
        monkeypatch.setattr(
            "dashboard.environment.catalog.list_gemini_models",
            lambda api_key: catalog.CatalogResult(
                ok=True, models=["gemini-flash-latest"], error=None
            ),
        )
        resp = client.post(
            "/api/environment/validate/LLM_MODEL",
            json={"value": "gemini-flash-latest"},
            cookies=session_cookie,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["error"] is None

    def test_model_var_invalid_when_not_in_catalog(self, client, session_cookie, monkeypatch):
        monkeypatch.setattr(
            "dashboard.environment.store.get_all_key_index_overrides", lambda: {}
        )
        monkeypatch.setattr(
            "dashboard.environment.credentials.resolve", lambda provider, index: ("X", "key")
        )
        monkeypatch.setattr(
            "dashboard.environment.catalog.list_gemini_models",
            lambda api_key: catalog.CatalogResult(
                ok=True, models=["gemini-flash-latest"], error=None
            ),
        )
        resp = client.post(
            "/api/environment/validate/LLM_MODEL",
            json={"value": "not-a-real-model"},
            cookies=session_cookie,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["error"] == "not_in_catalog"

    def test_model_var_no_credential_configured(self, client, session_cookie, monkeypatch):
        monkeypatch.setattr(
            "dashboard.environment.store.get_all_key_index_overrides", lambda: {}
        )
        monkeypatch.setattr(
            "dashboard.environment.credentials.resolve", lambda provider, index: ("X", "")
        )
        resp = client.post(
            "/api/environment/validate/GROQ_MODEL",
            json={"value": "llama-3.3-70b-versatile"},
            cookies=session_cookie,
        )
        assert resp.status_code == 200
        assert resp.json()["error"] == "no_credential_configured"

    def test_gcp_project_valid_when_substitution_call_succeeds(
        self, client, session_cookie, monkeypatch
    ):
        monkeypatch.setattr(
            "dashboard.environment.store.get_all_key_index_overrides", lambda: {}
        )
        monkeypatch.setattr(
            "dashboard.environment.vertex_credentials.resolve_service_account_info",
            lambda index: {"project_id": "old-proj"},
        )
        monkeypatch.setattr(
            "dashboard.environment.catalog.list_vertex_models",
            lambda info, project_override=None, location_override=None: catalog.CatalogResult(
                ok=True, models=["gemini-2.5-flash"], error=None
            )
            if project_override == "new-proj"
            else (_ for _ in ()).throw(AssertionError("wrong override applied")),
        )
        resp = client.post(
            "/api/environment/validate/GCP_PROJECT",
            json={"value": "new-proj"},
            cookies=session_cookie,
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_gcp_location_substitutes_location_override(
        self, client, session_cookie, monkeypatch
    ):
        monkeypatch.setattr(
            "dashboard.environment.store.get_all_key_index_overrides", lambda: {}
        )
        monkeypatch.setattr(
            "dashboard.environment.vertex_credentials.resolve_service_account_info",
            lambda index: {"project_id": "proj-a"},
        )
        monkeypatch.setattr(
            "dashboard.environment.catalog.list_vertex_models",
            lambda info, project_override=None, location_override=None: catalog.CatalogResult(
                ok=True, models=[], error=None
            )
            if location_override == "europe-west1"
            else (_ for _ in ()).throw(AssertionError("wrong override applied")),
        )
        resp = client.post(
            "/api/environment/validate/GCP_LOCATION",
            json={"value": "europe-west1"},
            cookies=session_cookie,
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_unknown_var_rejected(self, client, session_cookie):
        resp = client.post(
            "/api/environment/validate/RANDOM_VAR",
            json={"value": "x"},
            cookies=session_cookie,
        )
        assert resp.status_code == 404


class TestPatchRenderValidatesTheseFiveVars:
    def test_patch_rejects_model_not_in_catalog(self, client, session_cookie, monkeypatch):
        monkeypatch.setattr(
            "dashboard.environment.render_client.find_service_id", lambda: "srv-1"
        )
        monkeypatch.setattr(
            "dashboard.environment.render_client.push_env_var", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "dashboard.environment.render_client.trigger_deploy", lambda service_id: "dep-1"
        )
        monkeypatch.setattr(
            "dashboard.environment.store.get_all_key_index_overrides", lambda: {}
        )
        monkeypatch.setattr(
            "dashboard.environment.credentials.resolve", lambda provider, index: ("X", "key")
        )
        monkeypatch.setattr(
            "dashboard.environment.catalog.list_gemini_models",
            lambda api_key: catalog.CatalogResult(ok=True, models=["gemini-flash-latest"], error=None),
        )
        resp = client.patch(
            "/api/environment/render",
            json={"sets": {"LLM_MODEL": "bogus-model", "OTHER_KEY": "fine"}, "deletes": []},
            cookies=session_cookie,
        )
        result = resp.json()
        assert {"key": "LLM_MODEL", "error": "failed_validation"} in result["failed"]
        assert "OTHER_KEY" in result["applied"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest dashboard/tests/test_environment.py -v -k "ValidateVar or ValidatesTheseFiveVars"`
Expected: FAIL — `/api/environment/validate/LLM_MODEL` 404s; `PATCH` doesn't reject the bogus model.

- [ ] **Step 3: Write the implementation**

Add to `dashboard/environment.py` (near the credential-models route from Task 3):

```python
_DIRECT_EDIT_VARS = {
    "LLM_MODEL": "gemini",
    "GROQ_MODEL": "groq",
    "VERTEX_MODEL": "vertex",
    "GCP_PROJECT": "vertex",
    "GCP_LOCATION": "vertex",
}


class ValidateVarRequest(BaseModel):
    value: str


def _validate_model_var(provider: str, candidate: str) -> dict:
    if provider == "vertex":
        slot = store.get_all_key_index_overrides().get("vertex", 0)
        info = vertex_credentials.resolve_service_account_info(slot)
        result = catalog.list_vertex_models(info)
    else:
        slot = store.get_all_key_index_overrides().get(provider, 0)
        _, api_key = credentials.resolve(provider, slot)
        if not api_key:
            return {"ok": False, "error": "no_credential_configured", "models": None}
        result = (
            catalog.list_gemini_models(api_key)
            if provider == "gemini"
            else catalog.list_groq_models(api_key)
        )
    if not result.ok:
        return {"ok": False, "error": result.error, "models": None}
    if candidate not in (result.models or []):
        return {"ok": False, "error": "not_in_catalog", "models": result.models}
    return {"ok": True, "error": None, "models": result.models}


def _validate_gcp_var(var: str, candidate: str) -> dict:
    slot = store.get_all_key_index_overrides().get("vertex", 0)
    info = vertex_credentials.resolve_service_account_info(slot)
    kwargs = {"project_override": candidate} if var == "GCP_PROJECT" else {
        "location_override": candidate
    }
    result = catalog.list_vertex_models(info, **kwargs)
    return {"ok": result.ok, "error": result.error, "models": None}


def _validate_var(var: str, candidate: str) -> dict:
    provider = _DIRECT_EDIT_VARS[var]
    if var in ("GCP_PROJECT", "GCP_LOCATION"):
        return _validate_gcp_var(var, candidate)
    return _validate_model_var(provider, candidate)


@router.post("/api/environment/validate/{var}")
async def validate_var(var: str, payload: ValidateVarRequest) -> JSONResponse:
    if var not in _DIRECT_EDIT_VARS:
        raise HTTPException(status_code=404, detail="not a directly-validatable var")
    result = await asyncio.to_thread(_validate_var, var, payload.value)
    return JSONResponse(result)
```

Now wire the same check into the existing `_apply_render_patch`'s `sets` loop (Task 3's file, function already present from the original 2026-09-02 feature) — replace the existing `for key, value in payload.sets.items():` loop body with a version that validates first for these five vars:

```python
    for key, value in payload.sets.items():
        if stopped:
            break
        if key in _DIRECT_EDIT_VARS:
            check = _validate_var(key, value)
            if not check["ok"]:
                failed.append({"key": key, "error": "failed_validation"})
                continue
        try:
            render_client.push_env_var(service_id, key, value)
        except Exception as exc:  # noqa: BLE001
            failed.append({"key": key, "error": type(exc).__name__})
            stopped = True
            continue
        applied.append(key)
        logger.info("environment: set %s (len %d)", key, len(value))
```

This is a targeted replacement of the existing loop body (same `for` line, same surrounding structure) — the only change is the new `if key in _DIRECT_EDIT_VARS:` block inserted right after `if stopped: break`, and a validation failure uses `continue` (not `stopped = True`) so it doesn't block unrelated keys in the same request, per the Global Constraints' partial-failure rule.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest dashboard/tests/test_environment.py -v`
Expected: PASS (full file)

- [ ] **Step 5: Commit**

```bash
git add dashboard/environment.py dashboard/tests/test_environment.py
git commit -m "$(cat <<'EOF'
feat: validate GCP/model vars server-side on push, add a validate-on-blur route

POST /api/environment/validate/{var} lets the frontend check a candidate
value live before saving; PATCH /api/environment/render now re-runs the
same check authoritatively for these five vars regardless of client state,
rejecting a failing one into `failed` without blocking unrelated keys.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01EDvWri9YivWrM32pVn7BA6
EOF
)"
```

---

## Task 5: guided credential validate/apply routes (gemini/groq/vertex/github_app)

**Files:**
- Modify: `bot/github_app.py` (small additive extension for candidate-credential validation)
- Modify: `dashboard/environment.py`
- Test: `bot/tests/test_github_app.py`
- Test: `dashboard/tests/test_environment.py`

**Interfaces:**
- Consumes: `catalog.list_*_models`, `bot.config_deps.{CREDENTIAL_FAMILIES, conflicts_for}`, a new `bot.github_app._app_jwt_client_for(app_id, private_key_b64) -> Github` and a `client` override param on `discover_installation_id_for_app`.
- Produces: `POST /api/environment/credential/{family}/validate` (multipart) and `POST /api/environment/credential/{family}/apply`, per the spec's API contract (section 5).

- [ ] **Step 1: Write the failing tests (bot/github_app.py extension)**

```python
# Append to bot/tests/test_github_app.py
from unittest.mock import MagicMock, patch

from bot.github_app import _app_jwt_client_for, discover_installation_id_for_app


class TestJwtClientFor:
    @patch("bot.github_app.Github")
    @patch("bot.github_app.Auth.AppAuth")
    def test_builds_client_from_explicit_app_id_and_key(self, mock_app_auth, mock_github_cls):
        import base64

        pem_b64 = base64.b64encode(b"fake-pem-content").decode()
        _app_jwt_client_for(99, pem_b64)

        mock_app_auth.assert_called_once_with(99, "fake-pem-content")
        mock_github_cls.assert_called_once()


class TestDiscoverInstallationIdForAppWithExplicitClient:
    def test_uses_passed_client_instead_of_settings(self):
        client = MagicMock()
        client.requester.requestJsonAndCheck.return_value = (
            {},
            [{"id": 555, "account": {"login": "acme"}}],
        )
        result = discover_installation_id_for_app(client=client)
        assert result == 555
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest bot/tests/test_github_app.py -v -k "JwtClientFor or ExplicitClient"`
Expected: FAIL — `_app_jwt_client_for` doesn't exist; `discover_installation_id_for_app` doesn't accept `client=`.

- [ ] **Step 3: Write the `bot/github_app.py` extension**

Replace the existing `_read_private_key` function with a small refactor that preserves its exact behavior for existing callers, plus a new sibling helper and client constructor:

```python
def _decode_private_key(private_key_b64: str) -> str:
    try:
        return base64.b64decode(private_key_b64, validate=True).decode()
    except (binascii.Error, ValueError) as exc:
        raise ValueError(
            "GITHUB_APP_PRIVATE_KEY is not valid base64 -- encode the PEM with: "
            "uv run python -m bot.scripts.encode_credential github-app-private-key.pem"
        ) from exc


def _read_private_key() -> str:
    """Decode the base64-encoded App private key. Never logged."""
    return _decode_private_key(settings.github_app_private_key)


def _app_jwt_client_for(app_id: int, private_key_b64: str) -> Github:
    """Client authenticated as an ARBITRARY App identity (JWT) -- for
    validating a candidate App ID + private key before it's ever written to
    Settings/Render. Used only by the dashboard's guided credential flow;
    every other caller in this file keeps using _app_jwt_client(), which
    reads the currently-configured identity."""
    return Github(auth=Auth.AppAuth(app_id, _decode_private_key(private_key_b64)))
```

Modify `discover_installation_id_for_app`'s signature to accept an optional client override (the rest of the function body is unchanged — only the first two lines differ):

```python
def discover_installation_id_for_app(client: Github | None = None) -> int:
    """Return the App's single installation id (GET /app/installations, App JWT).

    ... (existing docstring unchanged) ...

    `client`, when given, is used instead of _app_jwt_client() -- lets the
    dashboard's guided credential flow validate a candidate App identity
    that was never written to Settings.
    """
    gh = client if client is not None else _app_jwt_client()
    try:
        _, data = gh.requester.requestJsonAndCheck("GET", "/app/installations")
    except GithubException as exc:
        raise RuntimeError(
            f"GitHub App installations lookup failed with {exc.status} ({exc.data}) -- "
            "likely a bad GITHUB_APP_ID or GITHUB_APP_PRIVATE_KEY."
        ) from exc
    if not data:
        raise AppNotInstalledError(
            "GitHub App has no installations: install it once via the GitHub UI "
            "(repo or org Settings -> GitHub Apps), then redeploy."
        )
    if len(data) > 1:
        accounts = ", ".join(installation["account"]["login"] for installation in data)
        raise RuntimeError(
            f"GitHub App has multiple installations ({accounts}) -- set "
            "GITHUB_APP_INSTALLATION_ID explicitly to pick one."
        )
    return int(data[0]["id"])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest bot/tests/test_github_app.py -v`
Expected: PASS (full file — confirms the `discover_installation_id_for_app()` no-arg call sites elsewhere are unaffected)

- [ ] **Step 5: Commit**

```bash
git add bot/github_app.py bot/tests/test_github_app.py
git commit -m "$(cat <<'EOF'
feat: allow validating a candidate GitHub App identity before it's saved

Additive-only: existing no-arg call sites are unaffected. Backs the
dashboard's guided GitHub App credential validation, which must check an
App ID + private key pair before either is ever written to Settings/Render.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01EDvWri9YivWrM32pVn7BA6
EOF
)"
```

- [ ] **Step 6: Write the failing tests (dashboard guided routes)**

```python
# Append to dashboard/tests/test_environment.py
import base64
import io


class TestGuidedCredentialValidate:
    def test_gemini_validate_success(self, client, session_cookie, monkeypatch):
        monkeypatch.setattr(
            "dashboard.environment.catalog.list_gemini_models",
            lambda api_key: catalog.CatalogResult(ok=True, models=["gemini-flash-latest"], error=None)
            if api_key == "the-key"
            else (_ for _ in ()).throw(AssertionError("wrong key")),
        )
        resp = client.post(
            "/api/environment/credential/gemini/validate",
            data={"api_key": "the-key"},
            cookies=session_cookie,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["models"] == ["gemini-flash-latest"]
        assert body["conflicts"] == []

    def test_gemini_validate_failure_is_structural(self, client, session_cookie, monkeypatch):
        monkeypatch.setattr(
            "dashboard.environment.catalog.list_gemini_models",
            lambda api_key: catalog.CatalogResult(ok=False, models=None, error="unauthorized"),
        )
        resp = client.post(
            "/api/environment/credential/gemini/validate",
            data={"api_key": "bad-key"},
            cookies=session_cookie,
        )
        assert resp.json()["ok"] is False
        assert resp.json()["error"] == "unauthorized"

    def test_vertex_validate_uploads_file_and_flags_project_conflict(
        self, client, session_cookie, monkeypatch
    ):
        key_json = b'{"project_id": "new-proj", "token_uri": "https://oauth2.googleapis.com/token"}'
        b64 = base64.b64encode(key_json).decode()
        monkeypatch.setattr(
            "dashboard.environment.catalog.list_vertex_models",
            lambda info: catalog.CatalogResult(ok=True, models=["gemini-2.5-flash"], error=None)
            if info == {"project_id": "new-proj", "token_uri": "https://oauth2.googleapis.com/token"}
            else (_ for _ in ()).throw(AssertionError("wrong info decoded")),
        )
        monkeypatch.setattr(
            "dashboard.environment.render_client.find_service_id", lambda: "srv-1"
        )
        monkeypatch.setattr(
            "dashboard.environment.render_client.env_vars",
            lambda service_id: {"GCP_PROJECT": "old-proj"},
        )
        resp = client.post(
            "/api/environment/credential/vertex/validate",
            files={"credential_file": ("key.json", io.BytesIO(key_json), "application/json")},
            cookies=session_cookie,
        )
        body = resp.json()
        assert body["ok"] is True
        assert body["project_id"] == "new-proj"
        assert body["conflicts"] == [
            {"var": "GCP_PROJECT", "current": "old-proj", "new": "new-proj"}
        ]

    def test_github_app_validate_success_shows_installation_id(
        self, client, session_cookie, monkeypatch
    ):
        pem_b64 = base64.b64encode(b"fake-pem").decode()
        monkeypatch.setattr(
            "dashboard.environment.github_app._app_jwt_client_for",
            lambda app_id, private_key_b64: "fake-client",
        )
        monkeypatch.setattr(
            "dashboard.environment.github_app.discover_installation_id_for_app",
            lambda client: 4242 if client == "fake-client" else (_ for _ in ()).throw(
                AssertionError("wrong client")
            ),
        )
        resp = client.post(
            "/api/environment/credential/github_app/validate",
            data={"app_id": "123"},
            files={"credential_file": ("app.pem", io.BytesIO(b"fake-pem"), "application/x-pem-file")},
            cookies=session_cookie,
        )
        body = resp.json()
        assert body["ok"] is True
        assert body["installation_id"] == 4242

    def test_github_app_validate_no_installation_is_structural_error(
        self, client, session_cookie, monkeypatch
    ):
        from bot.github_app import AppNotInstalledError

        monkeypatch.setattr(
            "dashboard.environment.github_app._app_jwt_client_for",
            lambda app_id, private_key_b64: "fake-client",
        )
        def _raise(client):
            raise AppNotInstalledError("no installation")
        monkeypatch.setattr(
            "dashboard.environment.github_app.discover_installation_id_for_app", _raise
        )
        resp = client.post(
            "/api/environment/credential/github_app/validate",
            data={"app_id": "123"},
            files={"credential_file": ("app.pem", io.BytesIO(b"fake-pem"), "application/x-pem-file")},
            cookies=session_cookie,
        )
        assert resp.json()["error"] == "installation_not_found"


class TestGuidedCredentialApply:
    def test_gemini_apply_writes_credential_and_model(self, client, session_cookie, monkeypatch):
        applied = {}
        monkeypatch.setattr(
            "dashboard.environment.render_client.find_service_id", lambda: "srv-1"
        )
        def _push(service_id, key, value):
            applied[key] = value
        monkeypatch.setattr("dashboard.environment.render_client.push_env_var", _push)
        monkeypatch.setattr(
            "dashboard.environment.render_client.trigger_deploy", lambda service_id: "dep-1"
        )
        resp = client.post(
            "/api/environment/credential/gemini/apply",
            json={"slot": 0, "credential": {"api_key": "the-key"}, "model": "gemini-flash-latest"},
            cookies=session_cookie,
        )
        assert resp.status_code == 200
        result = resp.json()
        assert "GEMINI_API_KEY" in result["applied"]
        assert "LLM_MODEL" in result["applied"]
        assert applied["GEMINI_API_KEY"] == "the-key"
        assert applied["LLM_MODEL"] == "gemini-flash-latest"

    def test_github_app_apply_writes_id_key_and_installation(
        self, client, session_cookie, monkeypatch
    ):
        applied = {}
        monkeypatch.setattr(
            "dashboard.environment.render_client.find_service_id", lambda: "srv-1"
        )
        monkeypatch.setattr(
            "dashboard.environment.render_client.push_env_var",
            lambda service_id, key, value: applied.__setitem__(key, value),
        )
        monkeypatch.setattr(
            "dashboard.environment.render_client.trigger_deploy", lambda service_id: "dep-1"
        )
        resp = client.post(
            "/api/environment/credential/github_app/apply",
            json={
                "app_id": "123",
                "private_key_b64": "cGVt",
                "installation_id": 4242,
            },
            cookies=session_cookie,
        )
        assert resp.status_code == 200
        result = resp.json()
        assert applied["GITHUB_APP_ID"] == "123"
        assert applied["GITHUB_APP_PRIVATE_KEY"] == "cGVt"
        assert applied["GITHUB_APP_INSTALLATION_ID"] == "4242"
        assert set(result["applied"]) == {
            "GITHUB_APP_ID",
            "GITHUB_APP_PRIVATE_KEY",
            "GITHUB_APP_INSTALLATION_ID",
        }
```

- [ ] **Step 7: Run tests to verify they fail**

Run: `uv run pytest dashboard/tests/test_environment.py -v -k "GuidedCredential"`
Expected: FAIL — routes don't exist yet (404s).

- [ ] **Step 8: Write the implementation**

Add to `dashboard/environment.py`'s import block:

```python
import base64

from fastapi import File, Form, UploadFile

from bot import github_app
```

Add the guided routes (place after Task 4's `validate_var` route):

```python
def _validate_llm_credential(family: str, api_key: str) -> dict:
    result = (
        catalog.list_gemini_models(api_key)
        if family == "gemini"
        else catalog.list_groq_models(api_key)
    )
    return {
        "ok": result.ok,
        "error": result.error,
        "models": result.models,
        "project_id": None,
        "installation_id": None,
        "conflicts": [],
    }


def _validate_vertex_credential(raw_bytes: bytes) -> dict:
    import json

    try:
        info = json.loads(raw_bytes.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {
            "ok": False,
            "error": "invalid_service_account_json",
            "models": None,
            "project_id": None,
            "installation_id": None,
            "conflicts": [],
        }
    result = catalog.list_vertex_models(info)
    project_id = info.get("project_id") if result.ok else None
    conflicts: list[dict] = []
    if result.ok and project_id:
        service_id = render_client.find_service_id()
        current_project = render_client.env_vars(service_id).get("GCP_PROJECT") if service_id else None
        conflicts = config_deps.conflicts_for("vertex", project_id, current_project)
    return {
        "ok": result.ok,
        "error": result.error,
        "models": result.models,
        "project_id": project_id,
        "installation_id": None,
        "conflicts": conflicts,
    }


def _validate_github_app_credential(app_id: int, raw_bytes: bytes) -> dict:
    private_key_b64 = base64.b64encode(raw_bytes).decode()
    try:
        gh_client = github_app._app_jwt_client_for(app_id, private_key_b64)
        installation_id = github_app.discover_installation_id_for_app(client=gh_client)
    except github_app.AppNotInstalledError:
        return {
            "ok": False,
            "error": "installation_not_found",
            "models": None,
            "project_id": None,
            "installation_id": None,
            "conflicts": [],
        }
    except RuntimeError as exc:
        error = "multiple_installations" if "multiple installations" in str(exc) else "unauthorized"
        return {
            "ok": False,
            "error": error,
            "models": None,
            "project_id": None,
            "installation_id": None,
            "conflicts": [],
        }
    except ValueError:
        return {
            "ok": False,
            "error": "invalid_key",
            "models": None,
            "project_id": None,
            "installation_id": None,
            "conflicts": [],
        }
    return {
        "ok": True,
        "error": None,
        "models": None,
        "project_id": None,
        "installation_id": installation_id,
        "conflicts": [],
    }


@router.post("/api/environment/credential/{family}/validate")
async def validate_credential(
    family: str,
    api_key: str | None = Form(None),
    app_id: str | None = Form(None),
    credential_file: UploadFile | None = File(None),
) -> JSONResponse:
    if family not in CREDENTIAL_FAMILIES:
        raise HTTPException(status_code=404, detail="unknown credential family")

    if family in ("gemini", "groq"):
        if not api_key:
            raise HTTPException(status_code=422, detail="api_key is required")
        payload = await asyncio.to_thread(_validate_llm_credential, family, api_key)
    elif family == "vertex":
        if credential_file is None:
            raise HTTPException(status_code=422, detail="credential_file is required")
        raw_bytes = await credential_file.read()
        payload = await asyncio.to_thread(_validate_vertex_credential, raw_bytes)
    else:  # github_app
        if not app_id or credential_file is None:
            raise HTTPException(status_code=422, detail="app_id and credential_file are required")
        raw_bytes = await credential_file.read()
        payload = await asyncio.to_thread(
            _validate_github_app_credential, int(app_id), raw_bytes
        )
    return JSONResponse(payload)


class ApplyLlmCredentialRequest(BaseModel):
    slot: int = 0
    credential: dict[str, str]
    model: str
    clear_gcp_project: bool = False


class ApplyGithubAppRequest(BaseModel):
    app_id: str
    private_key_b64: str
    installation_id: int


def _apply_llm_credential(family: str, payload: ApplyLlmCredentialRequest) -> dict:
    service_id = render_client.find_service_id()
    if service_id is None:
        return {"applied": [], "failed": [{"key": "*", "error": "service_not_found"}]}
    from bot.providers.registry import slot_env_name

    credential_var = slot_env_name(family, payload.slot)
    model_var = CREDENTIAL_FAMILIES[family]["model"]
    credential_value = (
        payload.credential.get("api_key")
        if family != "vertex"
        else payload.credential.get("service_account_b64", "")
    )
    applied: list[str] = []
    failed: list[dict] = []
    for key, value in ((credential_var, credential_value), (model_var, payload.model)):
        try:
            render_client.push_env_var(service_id, key, value)
            applied.append(key)
        except Exception as exc:  # noqa: BLE001
            failed.append({"key": key, "error": type(exc).__name__})
    if family == "vertex" and payload.clear_gcp_project:
        try:
            render_client.delete_env_var(service_id, "GCP_PROJECT")
            applied.append("GCP_PROJECT")
        except Exception as exc:  # noqa: BLE001
            failed.append({"key": "GCP_PROJECT", "error": type(exc).__name__})
    if applied:
        try:
            render_client.trigger_deploy(service_id)
        except Exception:  # noqa: BLE001
            logger.exception("environment: failed to trigger deploy after guided apply")
    return {"applied": applied, "failed": failed}


@router.post("/api/environment/credential/{family}/apply")
async def apply_credential(family: str, payload: dict) -> JSONResponse:
    if family not in ("gemini", "groq", "vertex", "github_app"):
        raise HTTPException(status_code=404, detail="unknown credential family")
    if family == "github_app":
        req = ApplyGithubAppRequest.model_validate(payload)
        result = await asyncio.to_thread(_apply_github_app_credential, req)
    else:
        req = ApplyLlmCredentialRequest.model_validate(payload)
        result = await asyncio.to_thread(_apply_llm_credential, family, req)
    return JSONResponse(result)


def _apply_github_app_credential(payload: ApplyGithubAppRequest) -> dict:
    service_id = render_client.find_service_id()
    if service_id is None:
        return {"applied": [], "failed": [{"key": "*", "error": "service_not_found"}]}
    applied: list[str] = []
    failed: list[dict] = []
    for key, value in (
        ("GITHUB_APP_ID", payload.app_id),
        ("GITHUB_APP_PRIVATE_KEY", payload.private_key_b64),
        ("GITHUB_APP_INSTALLATION_ID", str(payload.installation_id)),
    ):
        try:
            render_client.push_env_var(service_id, key, value)
            applied.append(key)
        except Exception as exc:  # noqa: BLE001
            failed.append({"key": key, "error": type(exc).__name__})
    if applied:
        try:
            render_client.trigger_deploy(service_id)
        except Exception:  # noqa: BLE001
            logger.exception("environment: failed to trigger deploy after guided apply")
    return {"applied": applied, "failed": failed}
```

Also add `from bot import config_deps` to the import block (used by `_validate_vertex_credential`).

- [ ] **Step 9: Run tests to verify they pass**

Run: `uv run pytest dashboard/tests/test_environment.py -v`
Expected: PASS (full file)

- [ ] **Step 10: Commit**

```bash
git add dashboard/environment.py dashboard/tests/test_environment.py
git commit -m "$(cat <<'EOF'
feat: add guided credential validate/apply routes for all four families

POST /api/environment/credential/{family}/validate runs the one live check
per family (list-models for LLM providers, App-identity + installation
discovery for GitHub App) without writing anything; .../apply writes the
credential plus its dependent vars (model, or installation id) together.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01EDvWri9YivWrM32pVn7BA6
EOF
)"
```

---

## Task 6: cascade-delete for LLM provider credential slots

**Files:**
- Modify: `dashboard/environment.py`
- Test: `dashboard/tests/test_environment.py`

**Interfaces:**
- Consumes: `bot.config_deps.dependents_of` (Task 2), `store.get_all_key_index_overrides`, `store.get_provider_override`, `store.set_key_index_override`, `store.set_provider_override`.
- Produces: `DELETE /api/environment/render/{key}?confirm=true` (extends the existing delete path inside `PATCH /api/environment/render`'s `deletes` handling is NOT changed — this is a new dedicated single-key `DELETE` route, since the design's confirmation flow needs a single-key round trip before the operator commits to a full multi-key Save).

- [ ] **Step 1: Write the failing tests**

```python
# Append to dashboard/tests/test_environment.py

class TestCascadeDelete:
    def test_delete_non_dependent_slot_succeeds_immediately(
        self, client, session_cookie, monkeypatch
    ):
        monkeypatch.setattr(
            "dashboard.environment.render_client.find_service_id", lambda: "srv-1"
        )
        monkeypatch.setattr(
            "dashboard.environment.render_client.delete_env_var", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "dashboard.environment.store.get_all_key_index_overrides", lambda: {}
        )
        monkeypatch.setattr(
            "dashboard.environment.store.get_provider_override", lambda: None
        )
        resp = client.delete(
            "/api/environment/render/GEMINI_API_KEY_2", cookies=session_cookie
        )
        assert resp.status_code == 200
        assert resp.json()["applied"] == ["GEMINI_API_KEY_2"]

    def test_delete_dependent_slot_without_confirm_returns_409(
        self, client, session_cookie, monkeypatch
    ):
        monkeypatch.setattr(
            "dashboard.environment.store.get_all_key_index_overrides", lambda: {"gemini": 2}
        )
        monkeypatch.setattr(
            "dashboard.environment.store.get_provider_override", lambda: None
        )
        resp = client.delete(
            "/api/environment/render/GEMINI_API_KEY_2", cookies=session_cookie
        )
        assert resp.status_code == 409
        assert resp.json()["dependents"] == ["key_index override"]

    def test_delete_active_provider_credential_without_confirm_returns_409(
        self, client, session_cookie, monkeypatch
    ):
        monkeypatch.setattr(
            "dashboard.environment.store.get_all_key_index_overrides", lambda: {}
        )
        monkeypatch.setattr(
            "dashboard.environment.store.get_provider_override", lambda: "gemini"
        )
        resp = client.delete(
            "/api/environment/render/GEMINI_API_KEY", cookies=session_cookie
        )
        assert resp.status_code == 409
        assert resp.json()["dependents"] == ["active provider override"]

    def test_confirmed_delete_cascades_runtime_config(self, client, session_cookie, monkeypatch):
        cleared = {}
        monkeypatch.setattr(
            "dashboard.environment.render_client.find_service_id", lambda: "srv-1"
        )
        monkeypatch.setattr(
            "dashboard.environment.render_client.delete_env_var", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "dashboard.environment.store.get_all_key_index_overrides", lambda: {"gemini": 2}
        )
        monkeypatch.setattr(
            "dashboard.environment.store.get_provider_override", lambda: "gemini"
        )
        monkeypatch.setattr(
            "dashboard.environment.store.set_key_index_override",
            lambda provider, index, now: cleared.__setitem__("key_index", (provider, index)),
        )
        monkeypatch.setattr(
            "dashboard.environment.store.set_provider_override",
            lambda provider, now: cleared.__setitem__("provider", provider),
        )
        resp = client.delete(
            "/api/environment/render/GEMINI_API_KEY_2?confirm=true", cookies=session_cookie
        )
        assert resp.status_code == 200
        assert resp.json()["applied"] == ["GEMINI_API_KEY_2"]
        assert cleared["key_index"] == ("gemini", None)

    def test_protected_key_delete_still_refused(self, client, session_cookie):
        resp = client.delete(
            "/api/environment/render/DATABASE_URL?confirm=true", cookies=session_cookie
        )
        assert resp.status_code == 200
        assert resp.json()["failed"] == [{"key": "DATABASE_URL", "error": "protected"}]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest dashboard/tests/test_environment.py -v -k CascadeDelete`
Expected: FAIL — no such route exists yet.

- [ ] **Step 3: Write the implementation**

Add to `dashboard/environment.py` (this task assumes Task 5's `from bot import config_deps` import already exists in the file; add it now if Task 5 hasn't run yet):

```python
def _cascade_delete(key: str, confirm: bool) -> tuple[int, dict]:
    if key in render_client.PROTECTED_ENV_KEYS:
        return 200, {"applied": [], "failed": [{"key": key, "error": "protected"}], "deploy_id": None}

    key_index_overrides = store.get_all_key_index_overrides()
    provider_override = store.get_provider_override()
    dependents = config_deps.dependents_of(
        key, key_index_overrides=key_index_overrides, provider_override=provider_override
    )

    if dependents is not None and dependents.any() and not confirm:
        return 409, {"dependents": dependents.labels()}

    service_id = render_client.find_service_id()
    if service_id is None:
        return 200, {
            "applied": [],
            "failed": [{"key": key, "error": "service_not_found"}],
            "deploy_id": None,
        }
    try:
        render_client.delete_env_var(service_id, key)
    except Exception as exc:  # noqa: BLE001
        return 200, {
            "applied": [],
            "failed": [{"key": key, "error": type(exc).__name__}],
            "deploy_id": None,
        }

    now = datetime.now(timezone.utc).isoformat()
    if dependents is not None:
        for family in ("gemini", "groq", "vertex"):
            index = config_deps.slot_index_for_var(family, key)
            if index is None:
                continue
            if dependents.key_index_override:
                store.set_key_index_override(family, None, now)
            if dependents.provider_override:
                store.set_provider_override(None, now)

    deploy_id = None
    try:
        deploy_id = render_client.trigger_deploy(service_id)
    except Exception:  # noqa: BLE001
        logger.exception("environment: failed to trigger deploy after cascade delete of %s", key)
    logger.info("environment: deleted %s (cascade)", key)
    return 200, {"applied": [key], "failed": [], "deploy_id": deploy_id}


@router.delete("/api/environment/render/{key}")
async def delete_render_env_var(key: str, confirm: bool = False) -> JSONResponse:
    status_code, payload = await asyncio.to_thread(_cascade_delete, key, confirm)
    return JSONResponse(payload, status_code=status_code)
```

`config_deps` is already imported (`from bot import config_deps`) from Task 5's step — no new import statement needed here if Task 5 ran first, per this task's execution order.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest dashboard/tests/test_environment.py -v`
Expected: PASS (full file)

- [ ] **Step 5: Commit**

```bash
git add dashboard/environment.py dashboard/tests/test_environment.py
git commit -m "$(cat <<'EOF'
feat: cascade-delete dependent runtime_config on LLM credential slot delete

DELETE /api/environment/render/{key} now returns 409 with the list of
dependent runtime_config entries (key_index override, active provider)
before deleting a non-protected LLM credential slot, and clears them on
confirm rather than leaving them dangling.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01EDvWri9YivWrM32pVn7BA6
EOF
)"
```

---

## Task 7: frontend — model/key-slot selects, refresh button, blur validation, Save guard

**Files:**
- Modify: `dashboard/static/dashboard.html`

**Interfaces:**
- Consumes: `GET /api/environment/render` (now returns `available_key_slots`), `GET /api/environment/config` (already returns `model`/`key_index`), `GET /api/environment/credential/{family}/models`, `POST /api/environment/validate/{var}`, existing `PATCH /api/environment/config`.
- Produces: updated `configForm` markup with per-provider model/key-slot selects; `saveConfig()` extended to send `model`/`key_index`; blur-validated inline indicators for `GCP_PROJECT`/`GCP_LOCATION` (in the render vars table) and the three model selects; a `saveDisabledByInvalidField` guard on both Save buttons.

- [ ] **Step 1: Add the new i18n strings**

In `dashboard/static/dashboard.html`'s `STRINGS.en` block, add after `env_delete: "Delete",`:

```javascript
        env_config_model: "Model",
        env_config_key_slot: "Key slot",
        env_refresh_models: "Refresh models",
        env_validating: "Checking…",
        env_valid: "✓ valid",
        env_invalid: "✗ invalid",
        env_fix_before_save: "Fix invalid fields before saving.",
```

In `STRINGS.he`, add after `env_delete: "מחק",`:

```javascript
        env_config_model: "מודל",
        env_config_key_slot: "חריץ מפתח",
        env_refresh_models: "רענן מודלים",
        env_validating: "בודק…",
        env_valid: "✓ תקין",
        env_invalid: "✗ לא תקין",
        env_fix_before_save: "תקן שדות לא תקינים לפני השמירה.",
```

- [ ] **Step 2: Add model/key-slot selects to `configForm`**

Replace the existing `configForm` block (lines 255–277) with:

```html
      <form id="configForm">
        <label data-i18n="env_config_provider"></label>
        <select id="cfgProvider">
          <option value="">—</option>
          <option value="gemini">gemini</option>
          <option value="groq">groq</option>
          <option value="vertex">vertex</option>
        </select>
        <div id="providerModelRows"></div>
        <label data-i18n="env_config_cooldown_base"></label>
        <input id="cfgCooldownBase" type="number" step="any">
        <label data-i18n="env_config_cooldown_max"></label>
        <input id="cfgCooldownMax" type="number" step="any">
        <label data-i18n="env_config_cooldown_factor"></label>
        <input id="cfgCooldownFactor" type="number" step="any">
        <label data-i18n="env_config_usage_cap_tokens"></label>
        <input id="cfgUsageCapTokens" type="number">
        <label data-i18n="env_config_usage_cap_reset"></label>
        <input id="cfgUsageCapReset" type="text" placeholder="HH:MM">
        <label>
          <input id="cfgReviewDraftPrs" type="checkbox">
          <span data-i18n="env_config_review_draft_prs"></span>
        </label>
      </form>
```

(Only the added `<div id="providerModelRows"></div>` line is new; everything else is unchanged from the current file.)

- [ ] **Step 3: Write the provider model/key-slot row rendering + wiring**

Add this JS after the existing `populateConfigForm` function (after line 775's closing brace, before `fetchEnvironmentConfig`):

```javascript
    const LLM_PROVIDERS = ["gemini", "groq", "vertex"];
    let currentConfig = { model: {}, key_index: {} };
    let currentAvailableKeySlots = { gemini: [], groq: [], vertex: [] };
    const invalidFields = new Set();

    function updateSaveGuards() {
      const disabled = invalidFields.size > 0;
      document.getElementById("saveRenderVarsBtn").disabled = disabled;
      document.getElementById("saveConfigBtn").disabled = disabled;
    }

    function renderProviderModelRows() {
      const container = document.getElementById("providerModelRows");
      container.innerHTML = LLM_PROVIDERS.map((provider) => {
        const slots = currentAvailableKeySlots[provider] || [];
        const currentSlot = currentConfig.key_index[provider] ?? "";
        const currentModel = currentConfig.model[provider] || "";
        const slotOptions = slots
          .map((s) => `<option value="${s}" ${String(s) === String(currentSlot) ? "selected" : ""}>${s}</option>`)
          .join("");
        return `
          <label>${esc(provider)} ${t("env_config_key_slot")}</label>
          <select data-provider="${provider}" class="cfg-key-slot">
            <option value="">—</option>
            ${slotOptions}
          </select>
          <label>${esc(provider)} ${t("env_config_model")}</label>
          <span>
            <input type="text" class="cfg-model" data-provider="${provider}" value="${esc(currentModel)}">
            <button type="button" class="control cfg-refresh-models" data-provider="${provider}">${t("env_refresh_models")}</button>
            <span class="cfg-model-status" data-provider="${provider}"></span>
          </span>
        `;
      }).join("");

      container.querySelectorAll(".cfg-refresh-models").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const provider = btn.getAttribute("data-provider");
          const statusEl = container.querySelector(`.cfg-model-status[data-provider="${provider}"]`);
          statusEl.textContent = t("env_validating");
          const resp = await fetch(`/api/environment/credential/${provider}/models`);
          const data = await resp.json();
          if (!data.ok) {
            statusEl.textContent = `${t("env_invalid")} (${data.error})`;
            return;
          }
          statusEl.textContent = "";
          const input = container.querySelector(`.cfg-model[data-provider="${provider}"]`);
          const datalistId = `models-${provider}`;
          let datalist = document.getElementById(datalistId);
          if (!datalist) {
            datalist = document.createElement("datalist");
            datalist.id = datalistId;
            document.body.appendChild(datalist);
            input.setAttribute("list", datalistId);
          }
          datalist.innerHTML = (data.models || []).map((m) => `<option value="${esc(m)}">`).join("");
        });
      });

      container.querySelectorAll(".cfg-model").forEach((input) => {
        input.addEventListener("blur", async () => {
          const provider = input.getAttribute("data-provider");
          const varName = { gemini: "LLM_MODEL", groq: "GROQ_MODEL", vertex: "VERTEX_MODEL" }[provider];
          const statusEl = container.querySelector(`.cfg-model-status[data-provider="${provider}"]`);
          if (!input.value) { statusEl.textContent = ""; invalidFields.delete(varName); updateSaveGuards(); return; }
          statusEl.textContent = t("env_validating");
          const resp = await fetch(`/api/environment/validate/${varName}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ value: input.value }),
          });
          const result = await resp.json();
          if (result.ok) {
            statusEl.textContent = t("env_valid");
            invalidFields.delete(varName);
          } else {
            statusEl.textContent = `${t("env_invalid")} (${result.error})`;
            invalidFields.add(varName);
          }
          updateSaveGuards();
        });
      });
    }
```

- [ ] **Step 4: Wire blur validation for `GCP_PROJECT`/`GCP_LOCATION` in the render vars table**

Modify `renderRenderVarsTable()`'s input-wiring loop (the existing `tbody.querySelectorAll(".env-value-input:not([readonly])")` block) to also validate on blur for these two keys:

```javascript
      tbody.querySelectorAll(".env-value-input:not([readonly])").forEach((input) => {
        input.addEventListener("input", () => {
          stagedEdits.set(input.getAttribute("data-key"), input.value);
        });
        const key = input.getAttribute("data-key");
        if (key === "GCP_PROJECT" || key === "GCP_LOCATION") {
          input.addEventListener("blur", async () => {
            const resp = await fetch(`/api/environment/validate/${key}`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ value: input.value }),
            });
            const result = await resp.json();
            if (result.ok) { invalidFields.delete(key); } else { invalidFields.add(key); }
            updateSaveGuards();
          });
        }
      });
```

This replaces the existing three-line block (lines 700–704) with the version above — the original `input` listener is preserved verbatim as the first statement inside the `forEach`.

- [ ] **Step 5: Extend `populateConfigForm`, `fetchEnvironmentConfig`, `fetchEnvironmentRender`, and `saveConfig`**

```javascript
    function populateConfigForm(cfg) {
      document.getElementById("cfgProvider").value = cfg.provider || "";
      document.getElementById("cfgCooldownBase").value = cfg.cooldown_base_seconds ?? "";
      document.getElementById("cfgCooldownMax").value = cfg.cooldown_max_seconds ?? "";
      document.getElementById("cfgCooldownFactor").value = cfg.cooldown_factor ?? "";
      document.getElementById("cfgUsageCapTokens").value = cfg.usage_cap_tokens ?? "";
      document.getElementById("cfgUsageCapReset").value = cfg.usage_cap_reset ?? "";
      document.getElementById("cfgReviewDraftPrs").checked = Boolean(cfg.review_draft_prs);
      currentConfig = { model: cfg.model || {}, key_index: cfg.key_index || {} };
      renderProviderModelRows();
    }
```

(Replaces the existing `populateConfigForm` body — every line before the new two are unchanged, verbatim.)

```javascript
    async function fetchEnvironmentRender() {
      const resp = await fetch("/api/environment/render");
      if (resp.status === 401) { window.location.href = "/login"; return; }
      const data = await resp.json();
      renderVars = data.vars;
      currentAvailableKeySlots = data.available_key_slots || currentAvailableKeySlots;
      stagedEdits.clear();
      stagedDeletes.clear();
      renderRenderVarsTable();
      renderProviderModelRows();
    }
```

(Adds the `currentAvailableKeySlots` line and the trailing `renderProviderModelRows()` call; everything else unchanged.)

```javascript
    async function saveConfig() {
      const body = {
        provider: document.getElementById("cfgProvider").value || null,
        cooldown_base_seconds: parseFloat(document.getElementById("cfgCooldownBase").value) || null,
        cooldown_max_seconds: parseFloat(document.getElementById("cfgCooldownMax").value) || null,
        cooldown_factor: parseFloat(document.getElementById("cfgCooldownFactor").value) || null,
        usage_cap_tokens: parseInt(document.getElementById("cfgUsageCapTokens").value, 10) || null,
        usage_cap_reset: document.getElementById("cfgUsageCapReset").value || null,
        review_draft_prs: document.getElementById("cfgReviewDraftPrs").checked,
        model: {},
        key_index: {},
      };
      document.querySelectorAll(".cfg-model").forEach((input) => {
        const provider = input.getAttribute("data-provider");
        if (input.value) body.model[provider] = input.value;
      });
      document.querySelectorAll(".cfg-key-slot").forEach((select) => {
        const provider = select.getAttribute("data-provider");
        if (select.value !== "") body.key_index[provider] = parseInt(select.value, 10);
      });
      if (invalidFields.size > 0) {
        document.getElementById("configSaveResult").textContent = t("env_fix_before_save");
        return;
      }
      const resp = await fetch("/api/environment/config", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const result = await resp.json();
      document.getElementById("configSaveResult").textContent =
        `applied: ${result.applied.join(LIST_SEPARATOR) || "-"}; failed: ${result.failed.map((f) => `${f.key} (${f.error})`).join(LIST_SEPARATOR) || "-"}`;
    }
```

Also add the same client-side guard to `saveRenderVars` — insert right after its opening line (`async function saveRenderVars() {`):

```javascript
      if (invalidFields.size > 0) {
        document.getElementById("renderSaveResult").textContent = t("env_fix_before_save");
        return;
      }
```

- [ ] **Step 6: Manual check**

Run: `uv run uvicorn bot.main:app --reload` (or this project's existing `run`-skill launch command), log into the dashboard, open the Environment tab, confirm: the provider model/key-slot rows render, "Refresh models" populates a datalist, typing an invalid model into a model field and blurring shows `✗ invalid` and disables both Save buttons, fixing it re-enables them.

- [ ] **Step 7: Commit**

```bash
git add dashboard/static/dashboard.html
git commit -m "$(cat <<'EOF'
feat: add model/key-slot selects and blur validation to the Environment tab

Runtime_config panel gains a model input + key-slot select + "refresh
models" per LLM provider; GCP_PROJECT/GCP_LOCATION and the model fields
validate on blur and block Save while any is flagged invalid.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01EDvWri9YivWrM32pVn7BA6
EOF
)"
```

---

## Task 8: frontend — guided credential modal (Add dropdown, cascade-delete confirmation)

**Files:**
- Modify: `dashboard/static/dashboard.html`

**Interfaces:**
- Consumes: `POST /api/environment/credential/{family}/validate`, `POST /api/environment/credential/{family}/apply`, `DELETE /api/environment/render/{key}?confirm=`.
- Produces: a guided-setup dropdown next to `addVarForm`, a modal (`<dialog>`) for credential entry per family, and a confirmation dialog on a 409 cascade-delete response.

- [ ] **Step 1: Add i18n strings**

Append to `STRINGS.en` (after Task 7's additions):

```javascript
        env_guided_setup: "Guided setup…",
        env_guided_gemini: "Gemini key",
        env_guided_groq: "Groq key",
        env_guided_vertex: "Vertex service account",
        env_guided_github_app: "GitHub App",
        env_guided_validate: "Validate",
        env_guided_apply: "Apply",
        env_guided_cancel: "Cancel",
        env_guided_project_conflict: "GCP_PROJECT is set to {current}, this key belongs to {new}.",
        env_guided_keep_project: "Keep GCP_PROJECT",
        env_guided_clear_project: "Clear GCP_PROJECT (use key's project)",
        env_guided_installation_label: "Installation ID (auto-discovered)",
        env_delete_confirm_title: "This will also clear:",
        env_delete_confirm_ok: "Delete anyway",
```

Append to `STRINGS.he`:

```javascript
        env_guided_setup: "התקנה מודרכת…",
        env_guided_gemini: "מפתח Gemini",
        env_guided_groq: "מפתח Groq",
        env_guided_vertex: "חשבון שירות Vertex",
        env_guided_github_app: "אפליקציית GitHub",
        env_guided_validate: "אמת",
        env_guided_apply: "החל",
        env_guided_cancel: "ביטול",
        env_guided_project_conflict: "GCP_PROJECT מוגדר ל-{current}, המפתח הזה שייך ל-{new}.",
        env_guided_keep_project: "השאר GCP_PROJECT",
        env_guided_clear_project: "נקה GCP_PROJECT (השתמש בפרויקט של המפתח)",
        env_guided_installation_label: "מזהה התקנה (זוהה אוטומטית)",
        env_delete_confirm_title: "פעולה זו תנקה גם:",
        env_delete_confirm_ok: "מחק בכל זאת",
```

- [ ] **Step 2: Add the modal markup and guided-setup dropdown**

Insert right after the `addVarForm` element (after line 250's closing `</form>`, before `saveRenderVarsBtn`):

```html
      <select id="guidedSetupSelect">
        <option value="" data-i18n="env_guided_setup"></option>
        <option value="gemini" data-i18n="env_guided_gemini"></option>
        <option value="groq" data-i18n="env_guided_groq"></option>
        <option value="vertex" data-i18n="env_guided_vertex"></option>
        <option value="github_app" data-i18n="env_guided_github_app"></option>
      </select>
      <dialog id="guidedModal">
        <div id="guidedModalBody"></div>
        <button type="button" id="guidedValidateBtn" data-i18n="env_guided_validate"></button>
        <button type="button" id="guidedApplyBtn" data-i18n="env_guided_apply" disabled></button>
        <button type="button" id="guidedCancelBtn" data-i18n="env_guided_cancel"></button>
        <div id="guidedModalResult"></div>
      </dialog>
      <dialog id="deleteConfirmModal">
        <div data-i18n="env_delete_confirm_title"></div>
        <ul id="deleteConfirmList"></ul>
        <button type="button" id="deleteConfirmOkBtn" data-i18n="env_delete_confirm_ok"></button>
        <button type="button" id="deleteConfirmCancelBtn" data-i18n="env_guided_cancel"></button>
      </dialog>
```

- [ ] **Step 3: Write the guided modal JS**

Add before the `document.getElementById("addVarForm")` listener (so it's defined before first use):

```javascript
    let guidedValidatedPayload = null;

    function guidedModalBodyFor(family) {
      if (family === "gemini" || family === "groq") {
        return `<input type="password" id="guidedApiKey" placeholder="API key">`;
      }
      if (family === "vertex") {
        return `<input type="file" id="guidedCredentialFile" accept=".json">`;
      }
      return `
        <input type="text" id="guidedAppId" placeholder="App ID">
        <input type="file" id="guidedCredentialFile" accept=".pem">
      `;
    }

    document.getElementById("guidedSetupSelect").addEventListener("change", (event) => {
      const family = event.target.value;
      if (!family) return;
      guidedValidatedPayload = null;
      document.getElementById("guidedApplyBtn").disabled = true;
      document.getElementById("guidedModalResult").textContent = "";
      document.getElementById("guidedModalBody").innerHTML = guidedModalBodyFor(family);
      document.getElementById("guidedModal").dataset.family = family;
      document.getElementById("guidedModal").showModal();
    });

    document.getElementById("guidedValidateBtn").addEventListener("click", async () => {
      const family = document.getElementById("guidedModal").dataset.family;
      const formData = new FormData();
      if (family === "gemini" || family === "groq") {
        formData.append("api_key", document.getElementById("guidedApiKey").value);
      } else if (family === "vertex") {
        formData.append("credential_file", document.getElementById("guidedCredentialFile").files[0]);
      } else {
        formData.append("app_id", document.getElementById("guidedAppId").value);
        formData.append("credential_file", document.getElementById("guidedCredentialFile").files[0]);
      }
      const resp = await fetch(`/api/environment/credential/${family}/validate`, {
        method: "POST",
        body: formData,
      });
      const result = await resp.json();
      const resultEl = document.getElementById("guidedModalResult");
      if (!result.ok) {
        resultEl.textContent = `${t("env_invalid")}: ${result.error}`;
        document.getElementById("guidedApplyBtn").disabled = true;
        return;
      }
      guidedValidatedPayload = { family, result, raw: formData };
      let extra = "";
      if (family !== "github_app") {
        const options = (result.models || []).map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join("");
        extra += `<label>${t("env_config_model")}</label><select id="guidedModelSelect">${options}</select>`;
      }
      if (result.conflicts && result.conflicts.length > 0) {
        const c = result.conflicts[0];
        extra += `<p>${t("env_guided_project_conflict").replace("{current}", esc(c.current)).replace("{new}", esc(c.new))}</p>`;
        extra += `
          <label><input type="radio" name="projectChoice" value="keep" checked> ${t("env_guided_keep_project")}</label>
          <label><input type="radio" name="projectChoice" value="clear"> ${t("env_guided_clear_project")}</label>
        `;
      }
      if (family === "github_app") {
        extra += `<label>${t("env_guided_installation_label")}</label><span>${result.installation_id}</span>`;
      }
      document.getElementById("guidedModalBody").insertAdjacentHTML("beforeend", extra);
      resultEl.textContent = t("env_valid");
      document.getElementById("guidedApplyBtn").disabled = false;
    });

    document.getElementById("guidedApplyBtn").addEventListener("click", async () => {
      if (!guidedValidatedPayload) return;
      const { family, result } = guidedValidatedPayload;
      let body;
      if (family === "github_app") {
        body = {
          app_id: document.getElementById("guidedAppId").value,
          private_key_b64: btoa(await guidedValidatedPayload.raw.get("credential_file").text()),
          installation_id: result.installation_id,
        };
      } else {
        const model = document.getElementById("guidedModelSelect").value;
        const credential = family === "vertex"
          ? { service_account_b64: btoa(await guidedValidatedPayload.raw.get("credential_file").text()) }
          : { api_key: guidedValidatedPayload.raw.get("api_key") };
        const clearChoice = document.querySelector('input[name="projectChoice"]:checked');
        body = {
          slot: 0,
          credential,
          model,
          clear_gcp_project: Boolean(clearChoice && clearChoice.value === "clear"),
        };
      }
      const resp = await fetch(`/api/environment/credential/${family}/apply`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const result2 = await resp.json();
      document.getElementById("guidedModalResult").textContent =
        `applied: ${result2.applied.join(LIST_SEPARATOR) || "-"}; failed: ${(result2.failed || []).map((f) => `${f.key} (${f.error})`).join(LIST_SEPARATOR) || "-"}`;
      document.getElementById("guidedModal").close();
      document.getElementById("guidedSetupSelect").value = "";
      await fetchEnvironmentRender();
      await fetchEnvironmentConfig();
    });

    document.getElementById("guidedCancelBtn").addEventListener("click", () => {
      document.getElementById("guidedModal").close();
      document.getElementById("guidedSetupSelect").value = "";
    });
```

- [ ] **Step 4: Wire the cascade-delete confirmation into the existing delete button**

Replace the existing delete-button wiring inside `renderRenderVarsTable()`:

```javascript
      tbody.querySelectorAll('[data-action="delete"]').forEach((btn) => {
        btn.addEventListener("click", async () => {
          const key = btn.getAttribute("data-key");
          const resp = await fetch(`/api/environment/render/${key}`, { method: "DELETE" });
          if (resp.status === 409) {
            const data = await resp.json();
            document.getElementById("deleteConfirmList").innerHTML =
              data.dependents.map((d) => `<li>${esc(d)}</li>`).join("");
            const modal = document.getElementById("deleteConfirmModal");
            modal.dataset.key = key;
            modal.showModal();
            return;
          }
          await fetchEnvironmentRender();
          await fetchEnvironmentConfig();
        });
      });
```

(This replaces the previous simple `stagedDeletes.add(...)` behavior with an immediate single-key `DELETE` call, matching Task 6's dedicated route — the bulk `stagedDeletes`/`saveRenderVars` path stays for edits, but per-row delete now goes through the confirmation-aware endpoint directly.)

Add the confirmation dialog's button handlers (near the guided-modal JS):

```javascript
    document.getElementById("deleteConfirmOkBtn").addEventListener("click", async () => {
      const modal = document.getElementById("deleteConfirmModal");
      const key = modal.dataset.key;
      await fetch(`/api/environment/render/${key}?confirm=true`, { method: "DELETE" });
      modal.close();
      await fetchEnvironmentRender();
      await fetchEnvironmentConfig();
    });

    document.getElementById("deleteConfirmCancelBtn").addEventListener("click", () => {
      document.getElementById("deleteConfirmModal").close();
    });
```

- [ ] **Step 5: Manual check**

Run the app, open Environment tab: pick "Guided setup…" → Gemini key → paste a key → Validate → confirm a model dropdown appears → Apply → confirm the row shows up. Delete a slot referenced by an active override → confirm the dependents list appears → confirm → confirm cascade cleared the config panel's key-slot select too.

- [ ] **Step 6: Commit**

```bash
git add dashboard/static/dashboard.html
git commit -m "$(cat <<'EOF'
feat: add guided credential setup modal and cascade-delete confirmation UI

Add button gains a per-family guided-setup option (file/text credential
input, live validate, model picker or installation-id label, project
conflict prompt); per-row delete now surfaces the cascade-delete
confirmation dialog before removing a credential slot with dependents.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01EDvWri9YivWrM32pVn7BA6
EOF
)"
```

---

## Task 9: full-suite verification and wrap-up

**Files:**
- None created; verification only.

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest -v`
Expected: all tests PASS, including every test added in Tasks 1–6.

- [ ] **Step 2: Run ruff**

Run: `uv run ruff check .`
Expected: no errors. Fix any that appear (import ordering, unused imports left over from the incremental edits above) before proceeding.

- [ ] **Step 3: Manual pass through the actual dashboard UI**

Using this project's `run` skill (or `uv run uvicorn bot.main:app --reload` directly), log into the dashboard and walk through, in order: (a) guided add for at least one LLM provider, confirming the model dropdown is populated from a real/mocked live call and the row appears after Apply; (b) guided add for GitHub App, confirming the installation ID shows as a read-only label, never an editable input; (c) one cascade-delete confirmation end to end; (d) the blur-validate → Save-blocked-while-invalid → re-enabled-once-fixed sequence for at least one of the five directly-editable vars. Note any UI issues found and fix them before calling this done — per this project's standing rule, UI flows aren't caught by type-checking or route tests alone.

- [ ] **Step 4: Update `dashboard/CLAUDE.md` if scope changed**

Check whether `dashboard/CLAUDE.md`'s description of `dashboard/environment.py` still accurately describes its write surface (it currently says "Render env vars ... and runtime_config overrides ... via bot.queue.store's existing get_*/set_* functions" — still accurate, since the new routes are additive to the same two categories). No edit expected, but re-read it once to confirm it isn't stale after these changes.

- [ ] **Step 5: Final commit (only if Steps 1–4 produced changes)**

```bash
git add -A
git commit -m "$(cat <<'EOF'
fix: address ruff/test findings from the final verification pass

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01EDvWri9YivWrM32pVn7BA6
EOF
)"
```
