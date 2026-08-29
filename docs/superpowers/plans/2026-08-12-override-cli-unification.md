# Override CLI Unification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the real `--sync-env` gap (numbered API-key slots are never pushed, despite
README claiming otherwise), add a fail-fast check for a locally-detectable missing
credential, and build one new script, `scripts/set_override.py`, that fully replaces both
`scripts/set_provider.py` and `scripts/set_api_key.py` — which stay completely untouched and
temporary, since they're wired into an already-rehearsed live demo plan.

**Architecture:** A new shared module, `scripts/_override.py`, holds local-value discovery
(scanning `.env` for numbered credential slots) and a single Render-verification function
that replaces two near-duplicate ones. `scripts/deploy.py`'s sync-env logic and the new
`scripts/set_override.py` both build on this module. `app/providers/factory.py` gets one
small, independent addition: raise clearly and immediately when a resolved credential is
empty, before any network call.

**Tech Stack:** Python 3.12, `python-dotenv` (already an indirect dependency via
`pydantic-settings`, made explicit here), `pytest` + `respx` for HTTP mocking, the existing
Postgres test harness (`tests/conftest.py`'s `db`/`db_url` fixtures).

## Global Constraints

- `scripts/set_provider.py` and `scripts/set_api_key.py` (and their test files) must not be
  modified in this plan — they are temporary and explicitly out of scope (design doc §2, §10).
- No secret value may ever be printed, logged, or returned from any new function except as
  an opaque equality/presence result — matches `CLAUDE.md`'s "no secret is ever logged" rule
  and the existing convention in every function this plan touches or mirrors.
- `allow_abbrev=False` on every new `argparse.ArgumentParser` — this codebase has a
  standing incident precedent (an abbreviated `--sync-en` flag once triggered a real
  production sync) and every existing CLI in `scripts/` carries this guard.
- New/changed store functions are synchronous; async callers use `asyncio.to_thread` (unchanged
  convention, not exercised by this plan since nothing here runs inside the dispatcher).
- Ruff (`line-length = 100`, rules `E4,E7,E9,F,E501`) must pass on every file touched.

---

### Task 1: `scripts/_override.py` — local-value discovery

**Files:**
- Create: `scripts/_override.py`
- Modify: `pyproject.toml` (add `python-dotenv` to `dependencies`)
- Test: `tests/test_override_helpers.py` (new)

**Interfaces:**
- Produces: `local_numbered_slots(base: str, env_path: str = ".env") -> dict[str, str]`,
  `local_value(provider: str, index: int) -> str`

- [ ] **Step 1: Declare the dependency explicitly**

`python-dotenv` is already installed transitively (via `pydantic-settings`), but this plan
imports it directly, so it must be declared. Edit `pyproject.toml`:

```toml
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "pydantic>=2.9",
    "pydantic-settings>=2.6",
    "pygithub>=2.4",
    "google-genai>=0.3",
    "httpx>=0.27",
    "groq>=1.5.0",
    "openai>=2.48.0",
    "psycopg[binary]>=3.2",
    "psycopg-pool>=3.2",
    "python-dotenv>=1.0",
]
```

Run: `uv sync`
Expected: resolves instantly (already installed), no version conflicts.

- [ ] **Step 2: Write the failing test for `local_numbered_slots`**

Create `tests/test_override_helpers.py`:

```python
"""Direct unit coverage for scripts/_override.py -- the shared local-value
discovery and Render-verification logic behind scripts/set_override.py and
scripts/deploy.py's numbered-slot sync-env fix. See
docs/superpowers/specs/2026-08-12-override-cli-unification-design.md."""
from __future__ import annotations

import httpx
import pytest
import respx

from app.config import settings
from scripts import _override

RENDER_SERVICES = "https://api.render.com/v1/services"


def _service_list(service_id="srv-1", name="pr-review-engine"):
    return [{"service": {"id": service_id, "name": name}}]


def _env_var_list(values: dict):
    return [{"envVar": {"key": k, "value": v}} for k, v in values.items()]


def test_local_numbered_slots_finds_matching_keys(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("GROQ_API_KEY_1=gsk_one\nGROQ_API_KEY_2=gsk_two\nOTHER_VAR=x\n")
    slots = _override.local_numbered_slots("GROQ_API_KEY", env_path=str(env_file))
    assert slots == {"GROQ_API_KEY_1": "gsk_one", "GROQ_API_KEY_2": "gsk_two"}


def test_local_numbered_slots_ignores_empty_values(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("GROQ_API_KEY_1=\nGROQ_API_KEY_2=gsk_two\n")
    slots = _override.local_numbered_slots("GROQ_API_KEY", env_path=str(env_file))
    assert slots == {"GROQ_API_KEY_2": "gsk_two"}


def test_local_numbered_slots_returns_empty_for_a_missing_file(tmp_path):
    missing = tmp_path / "does-not-exist.env"
    assert _override.local_numbered_slots("GROQ_API_KEY", env_path=str(missing)) == {}


def test_local_numbered_slots_does_not_match_a_different_base(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_API_KEY_1=gk_one\n")
    assert _override.local_numbered_slots("GROQ_API_KEY", env_path=str(env_file)) == {}
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_override_helpers.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bot.scripts._override'`

- [ ] **Step 4: Write minimal implementation**

Create `scripts/_override.py`:

```python
"""Shared local-value discovery and Render-verification logic for the
provider/key-index override CLI (scripts/set_override.py) and
scripts/deploy.py's numbered-slot sync-env fix.

Extracted from scripts/set_provider.py's _verify_render_credential and
scripts/set_api_key.py's _verify_render_key_slot, which independently
implemented the same "verify against Render, degrade to a warning when it
can't be verified, refuse unless --force" shape. See
docs/superpowers/specs/2026-08-12-override-cli-unification-design.md.

scripts/set_provider.py and scripts/set_api_key.py are deliberately NOT
refactored to use this module -- both are temporary, slated for deletion
once the presentation this was built for is over (see the design doc's
Non-goals section). Only scripts/set_override.py and scripts/deploy.py's
sync-env fix use it.
"""

from __future__ import annotations

import re

from dotenv import dotenv_values

from app.config import settings
from app.providers import registry
from scripts import _render

_SLOT_RE_CACHE: dict[str, re.Pattern[str]] = {}


def local_numbered_slots(base: str, env_path: str = ".env") -> dict[str, str]:
    """Every ``{base}_{N}`` key with a non-empty value in the local env file.

    N >= 1 only -- index 0 is the base var itself, read through Settings by
    local_value() below, never through this scan. Reads the file directly
    (python-dotenv, not os.environ or Settings) because Settings can't
    declare an unbounded family of numbered fields -- mirrors
    app/providers/credentials.py's identical reasoning for the runtime side.
    Returns {} if env_path doesn't exist (dotenv_values degrades gracefully)
    or nothing matches.
    """
    pattern = _SLOT_RE_CACHE.setdefault(base, re.compile(rf"^{re.escape(base)}_(\d+)$"))
    values = dotenv_values(env_path)
    return {key: value for key, value in values.items() if value and pattern.match(key)}


def local_value(provider: str, index: int) -> str:
    """The local value for (provider, index) -- index 0 via Settings (the
    same attribute-name convention scripts/deploy.py's check_provider and
    _verify_render_credential already use), index >= 1 via the scan above."""
    base, _ = registry.PROVIDERS[provider]
    if index == 0:
        return getattr(settings, base.lower(), "")
    env_name = f"{base}_{index}"
    return local_numbered_slots(base).get(env_name, "")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_override_helpers.py -v`
Expected: 4 passed

- [ ] **Step 6: Write tests for `local_value` (already implemented in Step 4)**

`local_value` was written alongside `local_numbered_slots` in Step 4, since both functions
are small enough to implement together — this step adds its test coverage. Append to
`tests/test_override_helpers.py`:

```python
def test_local_value_index_0_reads_through_settings(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "gsk_base")
    assert _override.local_value("groq", 0) == "gsk_base"


def test_local_value_index_n_reads_the_scan(monkeypatch):
    monkeypatch.setattr(
        _override, "local_numbered_slots",
        lambda base, env_path=".env": {"GROQ_API_KEY_2": "gsk_two"},
    )
    assert _override.local_value("groq", 2) == "gsk_two"


def test_local_value_index_n_returns_empty_when_unprovisioned(monkeypatch):
    monkeypatch.setattr(_override, "local_numbered_slots", lambda base, env_path=".env": {})
    assert _override.local_value("groq", 3) == ""
```

`test_local_value_index_n_reads_the_scan` monkeypatches `local_numbered_slots` directly rather
than writing a real file — this keeps the test from depending on or mutating the real
working-directory `.env` file, since `local_value`'s own job is just to call
`local_numbered_slots` and look up one key, not to re-exercise the file-scanning logic already
covered directly by Step 2's tests.

- [ ] **Step 7: Run the full file and commit**

Run: `uv run pytest tests/test_override_helpers.py -v`
Expected: 7 passed (4 from Step 2 + 3 from this step — both functions were already
implemented together in Step 4, so this run should pass immediately with no further
implementation work)

```bash
git add pyproject.toml scripts/_override.py tests/test_override_helpers.py
git commit -m "feat: add local-value discovery for numbered API-key slots

scripts/_override.py::local_numbered_slots scans local .env for BASE_N
credential siblings; local_value resolves either index 0 (via Settings)
or N (via the scan). First half of the shared module behind
scripts/set_override.py and the --sync-env numbered-slot fix."
```

---

### Task 2: `scripts/_override.py` — unified Render verification

**Files:**
- Modify: `scripts/_override.py`
- Test: `tests/test_override_helpers.py`

**Interfaces:**
- Consumes: `local_value` (Task 1)
- Produces: `verify_render_slot(provider: str, index: int) -> tuple[bool, str]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_override_helpers.py`:

```python
def test_verify_render_slot_degrades_without_a_render_api_key():
    ok, message = _override.verify_render_slot("groq", 0)
    assert ok is True
    assert "could not verify against Render" in message


def test_verify_render_slot_degrades_when_no_service_matches(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "no-such-service")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        ok, message = _override.verify_render_slot("groq", 0)
    assert ok is True
    assert "no service named" in message


def test_verify_render_slot_skips_when_database_url_does_not_match(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200, json=_env_var_list({"DATABASE_URL": "postgresql://prod-only/db"})
            )
        )
        ok, message = _override.verify_render_slot("groq", 0)
    assert ok is True
    assert "could not confirm this DATABASE_URL" in message


def test_verify_render_slot_refuses_when_missing_on_render(monkeypatch, db_url):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"DATABASE_URL": db_url}))
        )
        ok, message = _override.verify_render_slot("groq", 2)
    assert ok is False
    assert "GROQ_API_KEY_2" in message


def test_verify_render_slot_refuses_when_local_value_differs(monkeypatch, db_url):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_local")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200, json=_env_var_list({"DATABASE_URL": db_url, "GROQ_API_KEY": "gsk_remote"})
            )
        )
        ok, message = _override.verify_render_slot("groq", 0)
    assert ok is False
    assert "differs" in message


def test_verify_render_slot_passes_when_local_value_matches(monkeypatch, db_url):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_match")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200, json=_env_var_list({"DATABASE_URL": db_url, "GROQ_API_KEY": "gsk_match"})
            )
        )
        ok, message = _override.verify_render_slot("groq", 0)
    assert ok is True
    assert "verified" in message


def test_verify_render_slot_passes_with_no_local_value_to_compare(monkeypatch, db_url):
    """The numbered-slot case set_api_key.py used to always be in -- no local
    counterpart at all, but Render has a real value."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200, json=_env_var_list({"DATABASE_URL": db_url, "GROQ_API_KEY_2": "gsk_remote"})
            )
        )
        ok, message = _override.verify_render_slot("groq", 2)
    assert ok is True
    assert "no local value to compare" in message


def test_verify_render_slot_never_leaks_a_fetched_value(monkeypatch, db_url):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200,
                json=_env_var_list(
                    {"DATABASE_URL": db_url, "GROQ_API_KEY_2": "gsk_SUPER_SECRET_REMOTE"}
                ),
            )
        )
        _, message = _override.verify_render_slot("groq", 2)
    assert "gsk_SUPER_SECRET_REMOTE" not in message
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_override_helpers.py -v`
Expected: 9 FAIL with `AttributeError: module 'bot.scripts._override' has no attribute
'verify_render_slot'`

- [ ] **Step 3: Write minimal implementation**

Append to `scripts/_override.py`:

```python
def verify_render_slot(provider: str, index: int) -> tuple[bool, str]:
    """(ok_to_proceed, message). Replaces both set_provider.py's
    _verify_render_credential and set_api_key.py's _verify_render_key_slot.

    Differs from both predecessors: attempts an equality-against-the-local-
    value check for ANY index, not just 0 -- a numbered slot routinely has a
    real local counterpart now that Task 1's scan exists, so treating index
    >= 1 as "no local value, ever" (set_api_key.py's old assumption) is no
    longer accurate. Never returns, prints, or logs a fetched Render value --
    only presence/absence and in-memory equality results. See
    docs/superpowers/specs/2026-08-10-provider-live-credential-verification-design.md
    section 6 for the invariant this maintains.
    """
    base, _ = registry.PROVIDERS[provider]
    env_name = base if index == 0 else f"{base}_{index}"
    if not settings.render_api_key:
        return True, (
            "could not verify against Render (no RENDER_API_KEY); "
            "setting override without live verification"
        )
    try:
        service_id = _render.find_service_id()
        if service_id is None:
            return True, (
                f"could not verify against Render (no service named "
                f"{settings.render_service_name}); setting override without live verification"
            )
        env_vars = _render.env_vars(service_id)
    # deliberate: inability to verify degrades to a warning, never a refusal
    except Exception as exc:  # noqa: BLE001
        return True, (
            f"could not verify against Render ({type(exc).__name__}); "
            "setting override without live verification"
        )

    if env_vars.get("DATABASE_URL") != settings.database_url:
        return True, (
            "could not confirm this DATABASE_URL is the one the Render service reads "
            "-- skipping live verification"
        )

    live_value = env_vars.get(env_name) or ""
    if not live_value:
        return False, f"{env_name} is missing on the Render service"
    local = local_value(provider, index)
    if not local:
        return True, f"{env_name} present on Render (no local value to compare)"
    if live_value != local:
        return False, f"{env_name} on Render differs from your local .env value"
    return True, f"{env_name} verified on Render (matches local .env)"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_override_helpers.py -v`
Expected: 16 passed

- [ ] **Step 5: Ruff check and commit**

Run: `uv run ruff check scripts/_override.py tests/test_override_helpers.py`
Expected: no errors (fix line lengths/unused imports if any appear)

```bash
git add scripts/_override.py tests/test_override_helpers.py
git commit -m "feat: add unified Render-slot verification to scripts/_override.py

verify_render_slot() replaces both set_provider.py's
_verify_render_credential and set_api_key.py's _verify_render_key_slot --
same degrade-to-warning shape, but now attempts equality-against-local-
value for any index, not just 0."
```

---

### Task 3: Fix `--sync-env` to push numbered slots

**Files:**
- Modify: `scripts/deploy.py:579` (`_wanted_env`)
- Test: `tests/test_deploy_script.py`

**Interfaces:**
- Consumes: `local_numbered_slots` (Task 1)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_deploy_script.py` (near the existing `test_wanted_env_*` tests, using
the existing `gemini_only_config` fixture already defined in that file):

```python
def test_wanted_env_pushes_a_numbered_slot_with_a_local_value(gemini_only_config, monkeypatch):
    monkeypatch.setattr(
        deploy._override, "local_numbered_slots",
        lambda base: {"GEMINI_API_KEY_1": "gk_slot1"} if base == "GEMINI_API_KEY" else {},
    )
    wanted = deploy._wanted_env()
    assert wanted["GEMINI_API_KEY_1"] == "gk_slot1"


def test_wanted_env_omits_a_numbered_slot_with_no_local_value(gemini_only_config, monkeypatch):
    monkeypatch.setattr(deploy._override, "local_numbered_slots", lambda base: {})
    wanted = deploy._wanted_env()
    assert "GEMINI_API_KEY_1" not in wanted
    assert "GROQ_API_KEY_1" not in wanted


def test_wanted_env_pushes_numbered_slots_for_every_provider_not_just_the_selected_one(
    gemini_only_config, monkeypatch
):
    """Mirrors the existing 'other credentials pushed when locally filled'
    policy (test_wanted_env_includes_other_credentials_that_are_set) --
    extended to numbered slots."""
    def _slots(base):
        if base == "GROQ_API_KEY":
            return {"GROQ_API_KEY_2": "gsk_slot2"}
        return {}

    monkeypatch.setattr(deploy._override, "local_numbered_slots", _slots)
    wanted = deploy._wanted_env()
    assert wanted["GROQ_API_KEY_2"] == "gsk_slot2"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_deploy_script.py -k numbered_slot -v`
Expected: 3 FAIL — `AttributeError: module 'bot.scripts.deploy' has no attribute '_override'`
(or the first two assertions fail with a KeyError/missing-key, depending on which runs first)

- [ ] **Step 3: Write minimal implementation**

Modify `scripts/deploy.py`. Add the import (near the existing `from scripts import _render`
at line 36):

```python
from scripts import _override, _render
```

Then modify `_wanted_env()` (currently ending at the "any other provider's credential is
included only when it has a local value" loop, around line 596-601). Find:

```python
    for other_credential, _ in _PROVIDERS.values():
        value = getattr(settings, other_credential.lower(), "")
        if value and other_credential not in wanted:
            wanted[other_credential] = value
    return wanted
```

Replace with:

```python
    for other_credential, _ in _PROVIDERS.values():
        value = getattr(settings, other_credential.lower(), "")
        if value and other_credential not in wanted:
            wanted[other_credential] = value
    for credential, _ in _PROVIDERS.values():
        wanted.update(_override.local_numbered_slots(credential))
    return wanted
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_deploy_script.py -k numbered_slot -v`
Expected: 3 passed

- [ ] **Step 5: Run the full deploy-script test file to check for regressions**

Run: `uv run pytest tests/test_deploy_script.py -v`
Expected: all pass (same count as before this task, plus the 3 new ones)

- [ ] **Step 6: Ruff check and commit**

Run: `uv run ruff check scripts/deploy.py tests/test_deploy_script.py`
Expected: no errors

```bash
git add scripts/deploy.py tests/test_deploy_script.py
git commit -m "fix: --sync-env now pushes numbered API-key slots

_wanted_env() previously only knew about each provider's base credential --
a numbered slot like GROQ_API_KEY_1 was never included no matter what
README's 'Swapping API keys' section claimed. Now scans local .env via the
new scripts/_override.local_numbered_slots for every provider, not just
the selected one, matching the existing base-credential policy."
```

---

### Task 4: Fail-fast on a locally-detectable missing credential

**Files:**
- Modify: `app/providers/factory.py:29` (`_build`)
- Test: `tests/test_providers.py`

**Interfaces:** none (self-contained; independent of Tasks 1-3)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_providers.py`, near the existing `test_factory_raises_for_unknown_provider`:

```python
def test_factory_raises_a_clear_error_for_an_unprovisioned_key_index(monkeypatch):
    """The locally-detectable invalid-state case: activating gemini at index 1
    when only GEMINI_API_KEY (index 0) exists anywhere. Distinct from a
    dead-but-configured provider (e.g. github_models' real retirement),
    which must NOT be affected by this check -- that case has a real,
    non-empty credential and fails at the live call, unchanged."""
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
    monkeypatch.setattr(settings, "llm_provider", "github_models")
    monkeypatch.setattr(settings, "github_models_token", "ghp_real_but_dead")
    from app.providers.github_models import GitHubModelsProvider

    assert isinstance(get_provider(), GitHubModelsProvider)
```

- [ ] **Step 2: Run tests to verify the first one fails**

Run: `uv run pytest tests/test_providers.py -k unprovisioned_key_index -v`
Expected: FAIL — `Failed: DID NOT RAISE <class 'ValueError'>` (today, an empty credential
reaches `GeminiProvider.__init__` and either succeeds construction or fails later, not here)

- [ ] **Step 3: Write minimal implementation**

Modify `app/providers/factory.py`. Current `_build`:

```python
def _build(provider: str, index: int) -> LLMProvider:
    # Check membership BEFORE calling credentials.resolve(): resolve() does
    # registry.PROVIDERS[provider], an unguarded dict lookup that raises a
    # bare KeyError for an unknown name. Two pre-existing tests
    # (test_factory_raises_for_unknown_provider,
    # test_factory_rejects_retired_vertex_provider) expect ValueError with a
    # message naming the accepted providers -- resolving first would raise
    # the wrong exception type before ever reaching the check below.
    if provider not in registry.PROVIDERS:
        raise ValueError(
            f"Unknown provider: {provider!r} "
            "(expected 'gemini', 'groq', or 'github_models')"
        )
    _, api_key = credentials.resolve(provider, index)
    if provider == "gemini":
        return GeminiProvider(api_key=api_key)
    if provider == "groq":
        return GroqProvider(api_key=api_key)
    if provider == "github_models":
        return GitHubModelsProvider(api_key=api_key)
    raise ValueError(f"registry lists {provider!r} but _build cannot construct it")
```

Change the `credentials.resolve(...)` line and add the new check right after it:

```python
def _build(provider: str, index: int) -> LLMProvider:
    # Check membership BEFORE calling credentials.resolve(): resolve() does
    # registry.PROVIDERS[provider], an unguarded dict lookup that raises a
    # bare KeyError for an unknown name. Two pre-existing tests
    # (test_factory_raises_for_unknown_provider,
    # test_factory_rejects_retired_vertex_provider) expect ValueError with a
    # message naming the accepted providers -- resolving first would raise
    # the wrong exception type before ever reaching the check below.
    if provider not in registry.PROVIDERS:
        raise ValueError(
            f"Unknown provider: {provider!r} "
            "(expected 'gemini', 'groq', or 'github_models')"
        )
    env_name, api_key = credentials.resolve(provider, index)
    # Locally-detectable invalid state: no live call needed to know this slot
    # was never provisioned anywhere. Caught by run_specialist's existing
    # broad except -- all three specialists fail with this exact message,
    # with zero network calls, instead of each independently discovering the
    # same problem via a wasted, doomed real call. A DEAD-but-CONFIGURED
    # provider (a real credential, vendor down/retired) is unaffected: resolve()
    # returns a non-empty value here and this check never fires for it.
    if not api_key:
        raise ValueError(
            f"no credential configured for provider={provider!r} index={index} "
            f"({env_name} not set)"
        )
    if provider == "gemini":
        return GeminiProvider(api_key=api_key)
    if provider == "groq":
        return GroqProvider(api_key=api_key)
    if provider == "github_models":
        return GitHubModelsProvider(api_key=api_key)
    raise ValueError(f"registry lists {provider!r} but _build cannot construct it")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_providers.py -v`
Expected: all pass, including both new tests and every pre-existing factory test
(`test_factory_selects_gemini`, `test_factory_rejects_retired_vertex_provider`,
`test_factory_selects_groq`, `test_factory_selects_github_models`,
`test_factory_raises_for_unknown_provider`, `test_factory_returns_the_same_instance_...`,
`test_factory_rebuilds_the_client_when_the_key_index_changes`,
`test_factory_returns_to_the_original_cached_instance_after_switching_back`) — all of these
already pass a non-empty credential, so none of them should be affected by this change.

- [ ] **Step 5: Ruff check and commit**

Run: `uv run ruff check app/providers/factory.py tests/test_providers.py`
Expected: no errors

```bash
git add app/providers/factory.py tests/test_providers.py
git commit -m "fix: fail fast with a clear message on an unprovisioned key index

_build() now raises immediately when credentials.resolve() returns an empty
value, naming the exact missing env var. Caught by run_specialist's
existing broad except -- all three specialists fail with the same specific
message, no network call, instead of each discovering it independently via
a wasted real call. A dead-but-configured provider (real credential, vendor
down) is unaffected -- this only catches an empty resolved value."
```

---

### Task 5: `scripts/set_override.py` — the unified CLI

**Files:**
- Create: `scripts/set_override.py`
- Create: `tests/test_set_override_script.py`

**Interfaces:**
- Consumes: `_override.verify_render_slot` (Task 2), `store.get_provider_override`,
  `store.set_provider_override`, `store.get_key_index_override`,
  `store.set_key_index_override` (all pre-existing, unchanged), `registry.PROVIDERS`
  (pre-existing, unchanged)

- [ ] **Step 1: Write the failing tests for the core grammar**

Create `tests/test_set_override_script.py`:

```python
"""The unified operator CLI replacing both scripts/set_provider.py and
scripts/set_api_key.py. Uses the shared Postgres test harness -- it writes
to the same table the service reads. See
docs/superpowers/specs/2026-08-12-override-cli-unification-design.md
section 5 for the full grammar and its mapping to the two scripts this
replaces (which remain untouched and are not modified by this file)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import respx

from app.config import settings
from app.queue import store
from scripts import set_override

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _temp_db(db):
    yield


def test_activates_a_provider_only():
    assert set_override.main(["groq"]) == 0
    assert store.get_provider_override() == "groq"
    assert store.get_key_index_override("groq") is None


def test_clear_removes_the_provider_override():
    set_override.main(["groq"])
    assert set_override.main(["--clear"]) == 0
    assert store.get_provider_override() is None


def test_sets_index_without_activating():
    assert set_override.main(["groq", "--index", "2", "--no-activate"]) == 0
    assert store.get_key_index_override("groq") == 2
    assert store.get_provider_override() is None


def test_clears_index_without_activating():
    set_override.main(["groq", "--index", "2", "--no-activate"])
    assert set_override.main(["groq", "--clear-index", "--no-activate"]) == 0
    assert store.get_key_index_override("groq") is None


def test_activates_and_sets_index_together():
    assert set_override.main(["groq", "--index", "1"]) == 0
    assert store.get_provider_override() == "groq"
    assert store.get_key_index_override("groq") == 1


def test_activates_and_clears_index_together():
    set_override.main(["groq", "--index", "1", "--no-activate"])
    assert set_override.main(["groq", "--clear-index"]) == 0
    assert store.get_provider_override() == "groq"
    assert store.get_key_index_override("groq") is None


def test_providers_track_independent_index_overrides():
    set_override.main(["groq", "--index", "2", "--no-activate"])
    set_override.main(["gemini", "--index", "1", "--no-activate"])
    assert store.get_key_index_override("groq") == 2
    assert store.get_key_index_override("gemini") == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_set_override_script.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bot.scripts.set_override'`

- [ ] **Step 3: Write the implementation (core grammar + persistence)**

Create `scripts/set_override.py`:

```python
"""Set or clear the DB-backed provider override and/or a provider's
API-key-slot index override -- in one combined write+verification pass
when both are given.

    uv run python -m bot.scripts.set_override groq
    uv run python -m bot.scripts.set_override --clear
    uv run python -m bot.scripts.set_override groq --index 1
    uv run python -m bot.scripts.set_override groq --index 1 --no-activate
    uv run python -m bot.scripts.set_override groq --clear-index
    uv run python -m bot.scripts.set_override groq --clear-index --no-activate

Full replacement for scripts/set_provider.py and scripts/set_api_key.py --
see docs/superpowers/specs/2026-08-12-override-cli-unification-design.md
section 5 for the complete mapping table. Both older scripts are temporary
and are NOT modified by this script's existence; they are deleted
separately, after the presentation this was built for.

Verifies against the EFFECTIVE index -- whatever will actually be active
for this provider after the write, not always index 0 -- via
bot.scripts._override.verify_render_slot. This fixes a latent gap in
scripts/set_provider.py, which always verified index 0 regardless of any
existing key-index override for that provider.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from app.providers import registry
from app.queue import store
from scripts import _override


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="set_override",
        # Without this, argparse treats a truncated flag like --cle as an
        # abbreviation of --clear and runs it -- scripts/deploy.py,
        # scripts/set_provider.py, and scripts/set_api_key.py all carry the
        # same guard after an identical abbreviation match fired a live
        # production sync.
        allow_abbrev=False,
        description=(
            "Set or clear the DB-backed provider override and/or a provider's "
            "API-key-slot index override."
        ),
    )
    parser.add_argument(
        "provider",
        nargs="?",
        help=f"one of: {', '.join(sorted(registry.PROVIDERS))}",
    )
    parser.add_argument(
        "--index", type=int, help="set this provider's key-index override to N"
    )
    parser.add_argument(
        "--clear-index", action="store_true", help="clear this provider's key-index override"
    )
    parser.add_argument(
        "--no-activate",
        action="store_true",
        help="only touch the key-index override; leave the active-provider override untouched",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="clear the provider override (must be used alone)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="write despite a failed live-verification refusal",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)

    if args.clear:
        if args.provider or args.index is not None or args.clear_index or args.no_activate:
            print("--clear must be used alone", file=sys.stderr)
            return 2
        store.init_pool()
        store.set_provider_override(None, datetime.now(timezone.utc).isoformat())
        print("provider override cleared; falling back to LLM_PROVIDER")
        return 0

    if not args.provider:
        print("a provider is required (or --clear)", file=sys.stderr)
        return 2
    if args.provider not in registry.PROVIDERS:
        accepted = ", ".join(sorted(registry.PROVIDERS))
        print(
            f"unsupported provider {args.provider!r} (expected one of: {accepted})",
            file=sys.stderr,
        )
        return 2
    if args.index is not None and args.clear_index:
        print("--index and --clear-index are mutually exclusive", file=sys.stderr)
        return 2
    if args.no_activate and args.index is None and not args.clear_index:
        print("--no-activate requires --index or --clear-index", file=sys.stderr)
        return 2
    if args.index is not None and args.index < 0:
        print(f"index must be >= 0, got {args.index}", file=sys.stderr)
        return 2

    store.init_pool()

    if args.index is not None:
        effective_index = args.index
    elif args.clear_index:
        effective_index = 0
    else:
        effective_index = store.get_key_index_override(args.provider) or 0

    ok, message = _override.verify_render_slot(args.provider, effective_index)
    if ok:
        print(message)
    elif args.force:
        print(f"{message} -- proceeding anyway (--force)", file=sys.stderr)
    else:
        print(f"refusing to set the override: {message}", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc).isoformat()
    if not args.no_activate:
        store.set_provider_override(args.provider, now)
        print(f"provider override set to {args.provider}")
    if args.index is not None:
        store.set_key_index_override(args.provider, args.index, now)
        print(f"{args.provider} key-index override set to {args.index}")
    elif args.clear_index:
        store.set_key_index_override(args.provider, None, now)
        print(f"{args.provider} key-index override cleared")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_set_override_script.py -v`
Expected: 7 passed (each test above runs with no `RENDER_API_KEY` set, so
`verify_render_slot` degrades to "could not verify... proceeding" and every write succeeds)

- [ ] **Step 5: Write the failing tests for validation errors**

Append to `tests/test_set_override_script.py`:

```python
def test_rejects_an_unsupported_provider(capsys):
    assert set_override.main(["vertex"]) == 2
    err = capsys.readouterr().err
    assert "vertex" in err
    assert "groq" in err
    assert store.get_provider_override() is None


def test_rejects_a_negative_index(capsys):
    assert set_override.main(["groq", "--index", "-1"]) == 2
    assert "index" in capsys.readouterr().err


def test_requires_a_provider_or_clear(capsys):
    assert set_override.main([]) == 2
    assert "provider" in capsys.readouterr().err


def test_clear_must_be_used_alone_with_a_provider(capsys):
    assert set_override.main(["groq", "--clear"]) == 2
    assert "alone" in capsys.readouterr().err


def test_clear_must_be_used_alone_with_an_index(capsys):
    assert set_override.main(["--clear", "--index", "1"]) == 2


def test_index_and_clear_index_are_mutually_exclusive(capsys):
    assert set_override.main(["groq", "--index", "1", "--clear-index"]) == 2


def test_no_activate_requires_index_or_clear_index(capsys):
    assert set_override.main(["groq", "--no-activate"]) == 2
    assert "no-activate" in capsys.readouterr().err


def test_rejects_an_abbreviated_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        set_override.main(["groq", "--cle"])
    assert exc.value.code == 2
    assert "--cle" in capsys.readouterr().err


def test_entry_point_runs_as_a_documented_module_invocation():
    """Mirrors the identically-motivated tests in test_set_provider_script.py
    and test_set_api_key_script.py."""
    result = subprocess.run(
        [sys.executable, "-m", "bot.scripts.set_override", "--help"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_set_override_script.py -v`
Expected: all pass — the implementation from Step 3 already covers every validation branch,
including argparse's own built-in `--index -1` rejection... **verify this specifically**:
argparse's `type=int` will parse `"-1"` as `-1` fine (it is not treated as a flag since it's
consumed as `--index`'s value), so the explicit `if args.index is not None and args.index <
0` check in Step 3's implementation is what actually catches it, not argparse itself. Confirm
`test_rejects_a_negative_index` passes for this reason, not by accident.

- [ ] **Step 7: Write the failing tests for the Render-verification integration**

Append to `tests/test_set_override_script.py`:

```python
RENDER_SERVICES = "https://api.render.com/v1/services"


def _service_list(service_id="srv-1", name="pr-review-engine"):
    return [{"service": {"id": service_id, "name": name}}]


def _env_var_list(values: dict):
    return [{"envVar": {"key": k, "value": v}} for k, v in values.items()]


def test_refuses_when_the_effective_slot_is_missing_on_render(monkeypatch, db_url, capsys):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"DATABASE_URL": db_url}))
        )
        code = set_override.main(["groq", "--index", "2"])
    err = capsys.readouterr().err
    assert code == 2
    assert store.get_provider_override() is None
    assert store.get_key_index_override("groq") is None
    assert "GROQ_API_KEY_2" in err


def test_force_writes_despite_a_missing_slot(monkeypatch, db_url, capsys):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"DATABASE_URL": db_url}))
        )
        code = set_override.main(["groq", "--index", "2", "--force"])
    err = capsys.readouterr().err
    assert code == 0
    assert store.get_provider_override() == "groq"
    assert store.get_key_index_override("groq") == 2
    assert "--force" in err


def test_verifies_against_the_currently_configured_index_when_neither_flag_given(
    monkeypatch, db_url, capsys
):
    """The gap-fix: activating a provider that already has a non-zero index
    override must verify THAT index, not always index 0."""
    set_override.main(["groq", "--index", "2", "--no-activate"])
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200, json=_env_var_list({"DATABASE_URL": db_url, "GROQ_API_KEY_2": "gsk_x"})
            )
        )
        code = set_override.main(["groq"])
    out = capsys.readouterr().out
    assert code == 0
    assert store.get_provider_override() == "groq"
    assert "GROQ_API_KEY_2" in out


def test_clear_never_calls_the_render_verification(monkeypatch):
    def _boom(provider, index):
        raise AssertionError("must not verify on --clear")

    monkeypatch.setattr(_override, "verify_render_slot", _boom)
    assert set_override.main(["--clear"]) == 0


def test_never_leaks_a_fetched_credential_value(monkeypatch, db_url, capsys):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200,
                json=_env_var_list(
                    {"DATABASE_URL": db_url, "GROQ_API_KEY": "gsk_SUPER_SECRET_REMOTE"}
                ),
            )
        )
        set_override.main(["groq"])
    captured = capsys.readouterr()
    assert "gsk_SUPER_SECRET_REMOTE" not in captured.out
    assert "gsk_SUPER_SECRET_REMOTE" not in captured.err
```

Note: `test_clear_never_calls_the_render_verification` monkeypatches `_override.verify_render_slot`
directly (the shared module), not a copy on `set_override` — `scripts/set_override.py` calls
`_override.verify_render_slot(...)` as a qualified reference, so patching the attribute on the
`_override` module itself is what actually takes effect (patching `set_override.verify_render_slot`
would do nothing, since no such name exists in that module).

- [ ] **Step 8: Run tests to verify they pass**

Run: `uv run pytest tests/test_set_override_script.py -v`
Expected: all pass — no implementation changes needed for this step; Step 3's implementation
already wires in `_override.verify_render_slot` correctly.

- [ ] **Step 9: Run the full test file once more, then ruff check**

Run: `uv run pytest tests/test_set_override_script.py -v`
Expected: 20 passed (7 + 9 + 4 = 20; recount against the actual file before treating this as
a hard assertion — the point is zero failures, not an exact count)

Run: `uv run ruff check scripts/set_override.py tests/test_set_override_script.py`
Expected: no errors

- [ ] **Step 10: Commit**

```bash
git add scripts/set_override.py tests/test_set_override_script.py
git commit -m "feat: add scripts/set_override.py, unifying provider+key-index overrides

Full replacement for scripts/set_provider.py and scripts/set_api_key.py
(both untouched, temporary -- see the design doc's Non-goals). Adds the
new capability of activating a provider and setting its key-index
override together, in one write+verification pass, while preserving every
operation both older scripts support via --no-activate/--clear-index.
Verifies against the EFFECTIVE index (fixing set_provider.py's latent
always-checks-index-0 gap) rather than always index 0."
```

---

### Task 6: README documentation

**Files:**
- Modify: `README.md`

**Interfaces:** none (docs only)

- [ ] **Step 1: Add the new section**

In `README.md`, insert a new section immediately after the existing "Swapping API keys
without a redeploy" section (which ends at line 254, right before "#### Deploying an image,
when the Render service has no connected repo" at line 256):

```markdown

#### Setting the provider and key-index override together

```bash
uv run python -m bot.scripts.set_override groq --index 1        # activate groq AND its index-1 slot, together
uv run python -m bot.scripts.set_override groq --index 1 --no-activate   # index only, same as set_api_key.py below
uv run python -m bot.scripts.set_override groq --clear-index --no-activate  # clear index only, same as set_api_key.py below
uv run python -m bot.scripts.set_override groq                  # activate only, same as set_provider.py below
uv run python -m bot.scripts.set_override --clear                # clear the provider override, same as set_provider.py below
```

`scripts/set_override.py` is a full, standalone replacement for both `set_provider.py` and
`set_api_key.py` below — every operation either of them supports is reachable through this
one script (see `docs/superpowers/specs/2026-08-12-override-cli-unification-design.md`
section 5 for the complete mapping), plus the new capability of setting both overrides in one
write and one Render-verification pass instead of two round trips. It verifies against the
**effective** index — whichever index will actually be active for that provider after the
write, not always index 0 — so activating a provider that already has a non-default key-index
override verifies the correct slot, not the base credential.

`scripts/set_provider.py` and `scripts/set_api_key.py` (documented in the two sections above)
are **superseded by this script** and will be removed in a follow-up cleanup; nothing below
needs to change before then.
```

- [ ] **Step 2: Add a superseded-note to the two older sections**

At the top of the "Switching providers without a redeploy" section (line 182, right after the
`#### Switching providers without a redeploy` heading, before its code block), add:

```markdown
> Superseded by `scripts/set_override.py` below, which can do everything this script does
> plus set a key-index override in the same write. Kept working and documented here until a
> follow-up cleanup removes it.

```

Do the same at the top of "Swapping API keys without a redeploy" (line 230), right after its
heading:

```markdown
> Superseded by `scripts/set_override.py` below, which can do everything this script does
> plus activate a provider in the same write. Kept working and documented here until a
> follow-up cleanup removes it.

```

- [ ] **Step 3: Proofread the rendered section order**

Run: `grep -n "^#### " README.md` and confirm the order reads: "Switching providers..."
(superseded note) → "Tuning the re-review cooldown..." (unaffected) → "Swapping API keys..."
(superseded note) → "Setting the provider and key-index override together" (new) →
"Deploying an image...". Reorder if the new section landed anywhere else.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: document scripts/set_override.py; mark the two old scripts superseded"
```

---

### Task 7: Full-suite verification

**Files:** none created or modified (verification only; a fix-up commit only if something
surfaces)

**Interfaces:** none

- [ ] **Step 1: Run the complete test suite**

Run: `uv run pytest -v`
Expected: all tests pass, including every file touched by Tasks 1-6 and every pre-existing
test file this plan did not intend to change (`tests/test_set_provider_script.py`,
`tests/test_set_api_key_script.py`, `tests/test_set_cooldown_script.py`,
`tests/test_provider_registry.py`, `tests/test_provider_override.py`, and the rest) — these
must show **zero changes in pass/fail count** versus before this plan started, confirming
Task 1-5's additions caused no regression to the two scripts this plan deliberately left
untouched.

- [ ] **Step 2: Run ruff across the whole repo**

Run: `uv run ruff check .`
Expected: no errors

- [ ] **Step 3: Confirm `scripts/set_provider.py` and `scripts/set_api_key.py` are byte-for-byte unchanged**

Run: `git diff --stat HEAD~6..HEAD -- scripts/set_provider.py scripts/set_api_key.py`
(adjust `HEAD~6` to however many commits this plan actually produced, so the range covers
every commit from Task 1 through Task 6)
Expected: no output — zero lines changed in either file, confirming the Global Constraint
held throughout.

- [ ] **Step 4: If everything passes, this plan is complete — no further commit needed**

If Steps 1-3 all pass cleanly, there is nothing left to fix or commit; the six commits from
Tasks 1-6 are the complete, verified deliverable. If any regression surfaced, fix it now,
re-run Steps 1-3, and commit the fix with a message describing exactly what broke and why.
