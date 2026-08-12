# Runtime-Swappable API Keys Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator switch which numbered API-key slot (`GROQ_API_KEY`, `GROQ_API_KEY_1`, `GROQ_API_KEY_2`, ...) is active per provider, without a redeploy — mirroring the existing provider/cooldown DB-override pattern, but storing only an integer index, never a secret.

**Architecture:** Three new small modules (`app/providers/registry.py`, `credentials.py`, `key_index.py`) plus three nullable `runtime_config` columns give each provider its own DB-backed index override, refreshed once per claimed ticket exactly like the provider/cooldown overrides. The provider factory's client cache is re-keyed by `(provider, index)` so a swap invalidates the right cached SDK client. A new CLI (`scripts/set_api_key.py`) and a new deploy check (`check_api_key_live`) round it out.

**Tech Stack:** FastAPI/Python async backend, `psycopg`/Postgres (Supabase in production, local/CI Postgres in tests via `tests/conftest.py`'s `db` fixture), `pytest`/`pytest-asyncio`/`respx` for tests, `uv run` for all commands.

**Spec:** `docs/superpowers/specs/2026-08-12-api-key-index-override-design.md` (read it first — this plan implements it exactly, with one refinement discovered during planning: `credentials.resolve()`'s index-0 case reads through the `Settings` singleton rather than raw `os.environ`, so it uses the exact same `getattr(settings, base.lower(), "")` convention `scripts/deploy.py::check_provider` already uses, and existing tests that `monkeypatch.setattr(settings, "groq_api_key", ...)` keep working unchanged. Only index ≥ 1 — which has no `Settings` field — reads `os.environ` directly. Behavior for index 0 is identical either way; this only affects testability.)

## Global Constraints

- Secrets only via env vars; **no secret is ever logged** — this feature stores only an integer index in Postgres, never a key value (CLAUDE.md).
- Never make a live LLM API call outside the existing, deliberate `scripts/manual_verify_*.py` scripts — every test added by this plan mocks the SDK/HTTP boundary (CLAUDE.md's testing-hygiene rule).
- Async throughout; narrow, one-purpose modules with clear interfaces (CLAUDE.md conventions) — mirrored from `app/providers/active.py` / `app/queue/cooldown_config.py`.
- A failed override refresh must degrade to a safe default (index 0) and must never abort a review (existing fail-safe convention, extended to the new cache).
- Column names are always looked up through a hardcoded whitelist dict, never built from a caller-supplied string — psycopg parameterizes values, not column identifiers.
- Run `uv run pytest <path>` for tests and `uv run ruff check .` before each commit; CI runs both on push/PR.

---

### Task 1: Shared provider registry (`app/providers/registry.py`)

**Files:**
- Create: `app/providers/registry.py`
- Modify: `scripts/deploy.py:108-116` (delete the local `_PROVIDERS` dict, replace with an import + alias)
- Modify: `scripts/set_provider.py:30` (import path only — behavior unchanged)
- Test: `tests/test_provider_registry.py` (new)

**Interfaces:**
- Produces: `app.providers.registry.PROVIDERS: dict[str, tuple[str, str]]` (provider → `(credential env var, model env var)`), `app.providers.registry.KEY_INDEX_COLUMNS: dict[str, str]` (provider → `runtime_config` column name). Every later task reads one or both of these.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_provider_registry.py
"""app/providers/registry.py -- the single provider -> env-var-name mapping,
shared by app/ (credential resolution) and scripts/ (deploy checks, CLI
overrides). Replaces what was previously scripts/deploy.py's private
_PROVIDERS dict -- app-side code now needs the same mapping, and app/ must
not import from scripts/."""
from __future__ import annotations

from app.providers import registry
from scripts import deploy


def test_registry_lists_all_three_providers():
    assert set(registry.PROVIDERS) == {"gemini", "groq", "github_models"}


def test_registry_maps_each_provider_to_its_credential_and_model_env_vars():
    assert registry.PROVIDERS["gemini"] == ("GEMINI_API_KEY", "LLM_MODEL")
    assert registry.PROVIDERS["groq"] == ("GROQ_API_KEY", "GROQ_MODEL")
    assert registry.PROVIDERS["github_models"] == (
        "GITHUB_MODELS_TOKEN",
        "GITHUB_MODELS_MODEL",
    )


def test_registry_lists_a_key_index_column_per_provider():
    assert set(registry.KEY_INDEX_COLUMNS) == {"gemini", "groq", "github_models"}
    assert registry.KEY_INDEX_COLUMNS["gemini"] == "gemini_key_index"
    assert registry.KEY_INDEX_COLUMNS["groq"] == "groq_key_index"
    assert registry.KEY_INDEX_COLUMNS["github_models"] == "github_models_key_index"


def test_deploy_script_imports_the_shared_registry():
    """scripts/deploy.py must not keep its own copy of this mapping -- a
    provider added to one and not the other is exactly the drift this
    registry exists to prevent (see _PROVIDERS's own prior docstring, which
    already called it 'the single source of truth')."""
    assert deploy._PROVIDERS is registry.PROVIDERS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_provider_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.providers.registry'`

- [ ] **Step 3: Create the registry module**

```python
# app/providers/registry.py
"""Single source of truth for provider -> env-var-name mappings.

Both app/ (runtime credential resolution: credentials.py, factory.py) and
scripts/ (deploy verification, the set_provider/set_api_key CLIs) read this.
Previously duplicated as scripts/deploy.py's private _PROVIDERS dict; moved
here because app/ now needs the same mapping and must not import from
scripts/ (the dependency direction runs the other way everywhere else in
this codebase).
"""

from __future__ import annotations

# provider -> (credential env var, model env var)
PROVIDERS = {
    "gemini": ("GEMINI_API_KEY", "LLM_MODEL"),
    "groq": ("GROQ_API_KEY", "GROQ_MODEL"),
    "github_models": ("GITHUB_MODELS_TOKEN", "GITHUB_MODELS_MODEL"),
}

# provider -> the runtime_config column holding its active API-key-slot
# index override. A hardcoded whitelist, not a naming convention derived at
# call time -- every SQL statement that touches one of these columns looks
# the name up through this dict rather than building it from a caller's
# `provider` string, so this dict IS the injection guard for those callers.
KEY_INDEX_COLUMNS = {
    "gemini": "gemini_key_index",
    "groq": "groq_key_index",
    "github_models": "github_models_key_index",
}
```

- [ ] **Step 4: Update `scripts/deploy.py` to import the shared registry**

Two edits to `scripts/deploy.py`:

1. Add `from app.providers import registry` to the existing import block near the
   top of the file (`scripts/deploy.py:33-34`), so it reads:

   ```python
   from app import github_app
   from app.config import settings
   from app.providers import registry
   from scripts import _render
   ```

2. Replace lines 108-116 (the `_PROVIDERS` dict literal and its comment):

   ```python
   # The credential and model env var each LLM_PROVIDER value requires. This is the
   # single source of truth: check_config, --sync-env and scripts/set_provider.py
   # all read it, so a provider cannot be known to one and unknown to another.
   # provider -> (credential env var, model env var)
   _PROVIDERS = {
       "gemini": ("GEMINI_API_KEY", "LLM_MODEL"),
       "groq": ("GROQ_API_KEY", "GROQ_MODEL"),
       "github_models": ("GITHUB_MODELS_TOKEN", "GITHUB_MODELS_MODEL"),
   }
   ```

   with:

   ```python
   # Single source of truth for provider -> env-var-name mappings, shared with
   # app/ -- see app/providers/registry.py. _PROVIDERS is kept as a
   # module-level alias so every existing call site in this file (and in
   # scripts/set_provider.py) keeps working unchanged.
   _PROVIDERS = registry.PROVIDERS
   ```

- [ ] **Step 5: Update `scripts/set_provider.py`'s import**

`scripts/set_provider.py:30` currently reads:

```python
from scripts.deploy import _PROVIDERS
```

Leave this unchanged — it still works because `deploy._PROVIDERS` still exists (as an alias to `registry.PROVIDERS`). No edit needed here; this step is a verification checkpoint, not a code change.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_provider_registry.py tests/test_deploy_script.py tests/test_set_provider_script.py -v`
Expected: all PASS (this confirms the alias didn't break any existing `deploy._PROVIDERS` usage in `test_deploy_script.py:150-151,1182`)

- [ ] **Step 7: Commit**

```bash
git add app/providers/registry.py scripts/deploy.py tests/test_provider_registry.py
git commit -m "refactor: move provider->env-var registry to app/providers/registry.py"
```

---

### Task 2: Credential resolution (`app/providers/credentials.py`)

**Files:**
- Create: `app/providers/credentials.py`
- Test: `tests/test_credentials.py` (new)

**Interfaces:**
- Consumes: `registry.PROVIDERS` (Task 1).
- Produces: `resolve(provider: str, index: int) -> tuple[str, str]` — `(env_var_name, value)`. Task 6 (factory) is the consumer.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_credentials.py
"""app/providers/credentials.py -- resolving which env var backs a
provider's currently-active API-key slot."""
from __future__ import annotations

from app.config import settings
from app.providers import credentials


def test_index_zero_resolves_to_the_base_env_var_via_settings(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "gsk_from_settings")
    assert credentials.resolve("groq", 0) == ("GROQ_API_KEY", "gsk_from_settings")


def test_index_one_resolves_to_the_suffixed_env_var_via_os_environ(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY_1", "gsk_slot_1")
    assert credentials.resolve("groq", 1) == ("GROQ_API_KEY_1", "gsk_slot_1")


def test_a_missing_numbered_slot_resolves_to_an_empty_string(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY_7", raising=False)
    assert credentials.resolve("groq", 7) == ("GROQ_API_KEY_7", "")


def test_resolves_correctly_for_every_provider(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "gk_x")
    monkeypatch.setattr(settings, "github_models_token", "ghm_x")
    assert credentials.resolve("gemini", 0) == ("GEMINI_API_KEY", "gk_x")
    assert credentials.resolve("github_models", 0) == ("GITHUB_MODELS_TOKEN", "ghm_x")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_credentials.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.providers.credentials'`

- [ ] **Step 3: Implement**

```python
# app/providers/credentials.py
"""Resolves the (env-var-name, value) pair for a provider's currently-active
API-key slot.

Index 0 reads through the Settings singleton, via `base.lower()` -- the same
attribute-name convention scripts/deploy.py's check_provider and
_verify_render_credential already use for the base credential. This keeps
index-0 resolution identical to how every other part of this codebase reads
the base key, and keeps existing tests that
`monkeypatch.setattr(settings, "groq_api_key", ...)` working unchanged.

Index >= 1 has no Settings field -- Settings can't declare an unbounded
family of numbered env vars -- so it reads os.environ directly. A Render env
var never changes within a running process's lifetime (changing one requires
a restart, which re-imports everything), so reading it at resolve-time is
equivalent to reading it at startup; there is no need to enumerate how many
slots exist, only to look up the one that's currently selected.
"""

from __future__ import annotations

import os

from app.config import settings
from app.providers import registry


def resolve(provider: str, index: int) -> tuple[str, str]:
    base, _ = registry.PROVIDERS[provider]
    if index == 0:
        return base, getattr(settings, base.lower(), "")
    env_name = f"{base}_{index}"
    return env_name, os.environ.get(env_name, "")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_credentials.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add app/providers/credentials.py tests/test_credentials.py
git commit -m "feat: resolve a provider's active API-key slot to an env var"
```

---

### Task 3: Key-index override cache (`app/providers/key_index.py`)

**Files:**
- Create: `app/providers/key_index.py`
- Test: `tests/test_key_index_override.py` (new — cache-level tests only in this task; store-level tests are added in Task 4 alongside the store functions, in the same file)

**Interfaces:**
- Produces: `active_key_index(provider: str) -> int`, `set_override_cache(overrides: dict[str, int]) -> None`, `reset_override_cache() -> None`. Task 5 (dispatcher) and Task 6 (factory) both consume `active_key_index`; Task 5 also calls `set_override_cache`/relies on the reset semantics.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_key_index_override.py
"""The DB-backed API-key-index override: a singleton row that lets the
hosted service swap which numbered credential slot is active, per provider,
without a redeploy. Mirrors tests/test_provider_override.py's structure:
store-level tests first, then the process-local cache."""
from __future__ import annotations

import pytest

from app.providers import key_index


@pytest.fixture(autouse=True)
def _clean_cache():
    key_index.reset_override_cache()
    yield
    key_index.reset_override_cache()


def test_active_key_index_falls_back_to_zero():
    assert key_index.active_key_index("groq") == 0


def test_active_key_index_prefers_the_cached_override():
    key_index.set_override_cache({"groq": 2})
    assert key_index.active_key_index("groq") == 2


def test_active_key_index_defensively_floors_a_negative_cached_value():
    """A bad row (hand-edited, or a future bug) must never produce a
    negative index -- there is no such env-var slot."""
    key_index.set_override_cache({"groq": -1})
    assert key_index.active_key_index("groq") == 0


def test_a_provider_missing_from_the_cache_falls_back_to_zero():
    key_index.set_override_cache({"gemini": 3})
    assert key_index.active_key_index("groq") == 0


def test_clearing_the_cache_returns_to_zero():
    key_index.set_override_cache({"groq": 2})
    key_index.reset_override_cache()
    assert key_index.active_key_index("groq") == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_key_index_override.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.providers.key_index'`

- [ ] **Step 3: Implement**

```python
# app/providers/key_index.py
"""The API-key-slot index actually in force per provider: a DB override when
set, else index 0 (the base, unsuffixed env var).

Every read of the active index goes through active_key_index(). Mirrors
app/providers/active.py's provider-override cache exactly, generalized from
one provider to a per-provider dict: each provider tracks its own slot
independently, so switching *which provider* is active never disturbs the
key slot chosen for the other two.

This module deliberately imports nothing DB-related: the DB read lives in
the dispatcher (where the asyncio.to_thread convention applies) and is
pushed in via set_override_cache, keeping this module import-light and
non-blocking.

Fail-safe by construction: the cache starts empty, so before the first
refresh -- and whenever a refresh fails -- every provider degrades to index
0 rather than to a crash or a stale value. A negative cached value (hand-
edited row, or a future bug) is also defensively treated as "no override"
rather than propagated -- there is no such env-var slot.
"""

from __future__ import annotations

_overrides: dict[str, int] = {}


def active_key_index(provider: str) -> int:
    value = _overrides.get(provider)
    return value if value is not None and value >= 0 else 0


def set_override_cache(overrides: dict[str, int]) -> None:
    global _overrides
    _overrides = overrides


def reset_override_cache() -> None:
    set_override_cache({})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_key_index_override.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add app/providers/key_index.py tests/test_key_index_override.py
git commit -m "feat: add the per-provider API-key-index override cache"
```

---

### Task 4: Store functions + schema (`app/queue/store.py`)

**Files:**
- Modify: `app/queue/store.py:20-26` (imports), `:48-55` (`_SCHEMA`), append new functions after `set_cooldown_override` (currently ending at line 578)
- Test: `tests/test_key_index_override.py` (append store-level tests to the file created in Task 3)

**Interfaces:**
- Consumes: `registry.KEY_INDEX_COLUMNS` (Task 1).
- Produces: `get_key_index_override(provider: str) -> int | None`, `set_key_index_override(provider: str, index: int | None, now: str) -> None`, `get_all_key_index_overrides() -> dict[str, int]`. Task 5 (dispatcher) consumes `get_all_key_index_overrides`; Task 7 (CLI) consumes `set_key_index_override`; Task 8 (deploy check) does not use these directly (it reads via a raw connection, mirroring `_resolved_provider`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_key_index_override.py` (below the existing cache tests, but conventionally the store tests come first in the file — insert this block at the **top** of the file, before the `_clean_cache` fixture and cache tests, mirroring `test_provider_override.py`'s store-tests-then-cache-tests ordering):

```python
# Insert near the top of tests/test_key_index_override.py, after the module
# docstring and imports, before the existing `_clean_cache` fixture:

from app.queue import store

T0 = "2026-01-01T12:00:00+00:00"
T1 = "2026-01-01T12:00:01+00:00"


@pytest.fixture(autouse=True)
def _temp_db(db):
    yield


def test_set_then_get_returns_the_override():
    store.set_key_index_override("groq", 2, T0)
    assert store.get_key_index_override("groq") == 2


def test_setting_twice_replaces_rather_than_inserting(db_query):
    store.set_key_index_override("groq", 1, T0)
    store.set_key_index_override("groq", 2, T1)
    assert store.get_key_index_override("groq") == 2
    assert db_query("SELECT count(*) FROM runtime_config")[0][0] == 1


def test_clearing_restores_none():
    store.set_key_index_override("groq", 2, T0)
    store.set_key_index_override("groq", None, T1)
    assert store.get_key_index_override("groq") is None


def test_providers_track_independent_indices():
    store.set_key_index_override("groq", 2, T0)
    store.set_key_index_override("gemini", 1, T0)
    assert store.get_key_index_override("groq") == 2
    assert store.get_key_index_override("gemini") == 1
    assert store.get_key_index_override("github_models") is None


def test_get_all_key_index_overrides_returns_only_the_set_ones():
    store.set_key_index_override("groq", 2, T0)
    assert store.get_all_key_index_overrides() == {"groq": 2}


def test_get_all_key_index_overrides_is_empty_when_no_row_exists():
    assert store.get_all_key_index_overrides() == {}


def test_override_defaults_to_none():
    assert store.get_key_index_override("groq") is None
```

The full new top of the file (docstring + imports + fixtures) should read:

```python
"""The DB-backed API-key-index override: a singleton row that lets the
hosted service swap which numbered credential slot is active, per provider,
without a redeploy. Mirrors tests/test_provider_override.py's structure:
store-level tests first, then the process-local cache."""
from __future__ import annotations

import pytest

from app.providers import key_index
from app.queue import store

T0 = "2026-01-01T12:00:00+00:00"
T1 = "2026-01-01T12:00:01+00:00"


@pytest.fixture(autouse=True)
def _temp_db(db):
    yield


def test_set_then_get_returns_the_override():
    ...  # (as above)

# ... all store tests above ...

@pytest.fixture(autouse=True)
def _clean_cache():
    key_index.reset_override_cache()
    yield
    key_index.reset_override_cache()

# ... all cache tests from Task 3 follow, unchanged ...
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `uv run pytest tests/test_key_index_override.py -v`
Expected: the new store tests FAIL with `AttributeError: module 'app.queue.store' has no attribute 'get_key_index_override'`; the Task 3 cache tests still PASS.

- [ ] **Step 3: Add the schema migration**

In `app/queue/store.py`, add the import (near line 25, alongside the existing `from app.queue import cooldown_config`):

```python
from app.providers import registry
```

Then extend `_SCHEMA` (`app/queue/store.py:53-55`) — insert immediately after the existing three cooldown `ALTER TABLE` lines, before `CREATE TABLE IF NOT EXISTS reviews`:

```python
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS cooldown_base_seconds DOUBLE PRECISION;
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS cooldown_max_seconds  DOUBLE PRECISION;
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS cooldown_factor       DOUBLE PRECISION;
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS gemini_key_index        INTEGER;
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS groq_key_index          INTEGER;
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS github_models_key_index INTEGER;
```

- [ ] **Step 4: Add the store functions**

Append to `app/queue/store.py`, after `set_cooldown_override` (end of file):

```python
def get_key_index_override(provider: str) -> int | None:
    """The API-key-slot index override for `provider`, or None when unset.

    Synchronous like every other store function -- async callers use
    asyncio.to_thread.
    """
    column = registry.KEY_INDEX_COLUMNS[provider]
    with _require_pool().connection() as conn:
        row = conn.execute(f"SELECT {column} FROM runtime_config WHERE id = 1").fetchone()
    return (row or {}).get(column)


def set_key_index_override(provider: str, index: int | None, now: str) -> None:
    """Set the override for `provider`, or clear it with index=None.

    Upserts the singleton row -- same CHECK (id = 1) guarantee as
    set_provider_override. `column` is looked up through
    registry.KEY_INDEX_COLUMNS -- a hardcoded whitelist of exactly three
    names -- and never built from `provider` directly; psycopg parameterizes
    values but not column identifiers, so this lookup IS the injection
    guard, not an optimization.
    """
    column = registry.KEY_INDEX_COLUMNS[provider]
    with _require_pool().connection() as conn:
        conn.execute(
            f"INSERT INTO runtime_config (id, {column}, updated_at) VALUES (1, %s, %s) "
            f"ON CONFLICT (id) DO UPDATE SET {column} = EXCLUDED.{column}, "
            "updated_at = EXCLUDED.updated_at",
            (index, now),
        )


def get_all_key_index_overrides() -> dict[str, int]:
    """{provider: index} for every provider with a non-null override.

    One query reading all three columns -- the dispatcher calls this once
    per claimed ticket, not once per provider.
    """
    columns = registry.KEY_INDEX_COLUMNS
    select = ", ".join(columns.values())
    with _require_pool().connection() as conn:
        row = conn.execute(f"SELECT {select} FROM runtime_config WHERE id = 1").fetchone()
    if row is None:
        return {}
    return {
        provider: row[column] for provider, column in columns.items() if row[column] is not None
    }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_key_index_override.py tests/test_provider_override.py tests/test_cooldown_config.py -v`
Expected: all PASS (the last two confirm the schema addition didn't disturb the existing provider/cooldown override tests, which share the same `runtime_config` table)

- [ ] **Step 6: Commit**

```bash
git add app/queue/store.py tests/test_key_index_override.py
git commit -m "feat: persist the per-provider API-key-index override in runtime_config"
```

---

### Task 5: Dispatcher refresh (`app/queue/dispatcher.py`)

**Files:**
- Modify: `app/queue/dispatcher.py:34` (import), `:146-155` (add a third refresh block right after the existing cooldown refresh)
- Test: `tests/test_dispatcher.py` (append)

**Interfaces:**
- Consumes: `store.get_all_key_index_overrides` (Task 4), `key_index.set_override_cache`/`reset_override_cache` (Task 3).
- Produces: nothing new for later tasks — this closes the loop so `key_index.active_key_index()` reflects the DB within one claimed ticket, which Task 6's factory relies on being kept fresh.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dispatcher.py` (near the existing `test_claimed_ticket_runs_against_the_db_override` / `test_claim_falls_back_to_env_when_the_override_read_fails` pair, ~line 730):

```python
async def test_claimed_ticket_uses_the_db_key_index_override(monkeypatch):
    """The behavioral guarantee: a mid-session key-index override changes
    which credential slot actually resolves, with no restart and no
    redeploy."""
    _stub_comments(monkeypatch)
    store.set_key_index_override("groq", 2, NOW.isoformat())
    seen = []

    async def fake_attempt(repo, pr, comment_id=None):
        from app.providers import key_index
        seen.append(key_index.active_key_index("groq"))
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)
    _enqueue(1)
    await dispatcher.process_next_due(NOW)
    assert seen == [2]


async def test_claim_falls_back_to_index_zero_when_the_key_index_read_fails(monkeypatch):
    """Fail-safe: an unreachable override must degrade to index 0, never
    abort the review, and never keep serving a stale cached override from a
    previous successful refresh."""
    from app.providers import key_index

    _stub_comments(monkeypatch)
    # A prior successful refresh cached a DIFFERENT index. If the failure
    # handler merely logged and left the cache alone, active_key_index()
    # would keep returning 2 forever -- this is what catches that.
    key_index.set_override_cache({"groq": 2})

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "get_all_key_index_overrides", boom)
    seen = []

    async def fake_attempt(repo, pr, comment_id=None):
        seen.append(key_index.active_key_index("groq"))
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)
    _enqueue(1)
    result = await dispatcher.process_next_due(NOW)
    assert seen == [0]
    assert result.action == "ran"
```

Also add a fixture mirroring the existing `_clean_cooldown_cache` (`tests/test_dispatcher.py:31-35`) — insert right after it:

```python
@pytest.fixture(autouse=True)
def _clean_key_index_cache():
    from app.providers import key_index

    key_index.reset_override_cache()
    yield
    key_index.reset_override_cache()
```

And add `from app.queue import store` is already imported (line 15 imports `store` already) — no new top-level import needed in the test file beyond the inline `from app.providers import key_index` used above (matches this test file's existing inline-import style for `active`/`orchestrator` in nearby tests).

- [ ] **Step 2: Run test to verify it fails**

`process_next_due` doesn't refresh `key_index`'s cache yet, so `active_key_index("groq")` will still return 0 even after the override is set — `test_claim_falls_back_to_index_zero_when_the_key_index_read_fails` is trivially green already (it expects 0), so run only the genuinely red one:

Run: `uv run pytest tests/test_dispatcher.py::test_claimed_ticket_uses_the_db_key_index_override -v`
Expected: FAIL — `assert [0] == [2]`

- [ ] **Step 3: Implement the refresh**

In `app/queue/dispatcher.py`, add the import near line 34-35:

```python
from app.providers import active, key_index
from app.providers.active import active_provider
```

Then insert, immediately after the existing cooldown-override refresh block (`app/queue/dispatcher.py:146-155`, right before `if ticket.notice_not_before is not None:`):

```python
    # Refresh the API-key-index overrides once per claimed ticket, same
    # cadence and fail-safe shape as the provider/cooldown refreshes above: a
    # failure here must never abort a review, and must never leave a stale
    # cached override in place -- degrade all the way to index 0 for every
    # provider.
    try:
        key_index_overrides = await asyncio.to_thread(store.get_all_key_index_overrides)
        key_index.set_override_cache(key_index_overrides)
    except Exception:  # noqa: BLE001
        logger.exception("failed to refresh key-index overrides; using index 0")
        key_index.reset_override_cache()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_dispatcher.py -v`
Expected: all PASS, including the two new tests and every pre-existing dispatcher test (confirms the new refresh doesn't disturb provider/cooldown refresh behavior or ticket claiming)

- [ ] **Step 5: Commit**

```bash
git add app/queue/dispatcher.py tests/test_dispatcher.py
git commit -m "feat: refresh the API-key-index override cache once per claimed ticket"
```

---

### Task 6: Provider constructors + factory cache invalidation

This is the one task that needs behavior beyond the provider/cooldown precedent (client cache invalidation on a key swap), so constructor signature changes and the factory rewrite are bundled — a half-done state (constructors changed but factory not yet updated to match) would leave `get_provider()` broken.

**Files:**
- Modify: `app/providers/google_genai.py:55-62`, `app/providers/groq.py:48-63`, `app/providers/github_models.py:64-70` (constructor signatures)
- Modify: `app/providers/factory.py` (full rewrite of `_build`/`get_provider`, cache key becomes `(provider, index)`)
- Modify (call-site fixups, mechanical): `scripts/manual_verify_step4.py:41`, `scripts/manual_verify_groq.py:42`, `scripts/manual_verify_github_models.py:43`, `tests/test_providers.py:63,84,104`, `tests/test_groq_provider.py:43,65,85,101`, `tests/test_github_models_provider.py:57,83,103,133`, `tests/test_provider_rate_limited.py:64,72,79`
- Test: `tests/test_providers.py` (extend the existing factory test block); the mechanical fixups above are themselves test-file edits, not new tests

**Interfaces:**
- Consumes: `credentials.resolve` (Task 2), `key_index.active_key_index` (Task 3), `active.active_provider` (existing).
- Produces: `GeminiProvider(api_key: str)`, `GroqProvider(api_key: str)`, `GitHubModelsProvider(api_key: str)` — required, no default. `factory.get_provider()`'s external signature is unchanged; its cache is now keyed by `(provider, index)` internally.

- [ ] **Step 1: Update the three provider constructors**

`app/providers/google_genai.py:52-63` — replace the class:

```python
class GeminiProvider:
    """``gemini`` (AI-Studio) — the actually-live provider in this environment."""

    def __init__(self, api_key: str) -> None:
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=int(settings.llm_request_timeout_seconds * 1000)
            ),
        )
        self._model = settings.llm_model

    async def complete(self, system: str, user: str, schema: type[BaseModel]) -> LLMResponse:
        return await _complete(self._client, self._model, system, user, schema)
```

`app/providers/groq.py:45-63` — replace the class:

```python
class GroqProvider:
    """``groq`` — cross-vendor fallback (Llama via Groq's OpenAI-compatible API)."""

    def __init__(self, api_key: str) -> None:
        # max_retries=0: the SDK's own default (2) silently retries a 429
        # with backoff before this adapter's except clause ever sees it --
        # confirmed live (a 43.1s call, vs. ~5s normal, that never surfaced
        # as RateLimited despite exceeding the account's TPM budget). The
        # durable queue (app/queue/dispatcher.py) already owns retry/backoff
        # for a rate-limited review -- durable across a process restart,
        # visible via a placeholder/schedule-note comment -- so a second,
        # hidden retry layer underneath it is redundant at best and actively
        # hides a real signal at worst.
        self._client = AsyncGroq(
            api_key=api_key,
            max_retries=0,
            timeout=settings.llm_request_timeout_seconds,
        )
        self._model = settings.groq_model
```

`app/providers/github_models.py:61-70` — replace the class `__init__`:

```python
class GitHubModelsProvider:
    """``github_models`` — cross-vendor option riding the user's GitHub account."""

    def __init__(self, api_key: str) -> None:
        self._client = AsyncOpenAI(
            base_url=_BASE_URL,
            api_key=api_key,
            timeout=settings.llm_request_timeout_seconds,
        )
        self._model = settings.github_models_model
```

- [ ] **Step 2: Fix the mechanical call sites — manual-verify scripts**

`scripts/manual_verify_step4.py:41`: change `provider = GeminiProvider()` to `provider = GeminiProvider(api_key=settings.gemini_api_key)`.

`scripts/manual_verify_groq.py:42`: change `provider = GroqProvider()` to `provider = GroqProvider(api_key=settings.groq_api_key)`.

`scripts/manual_verify_github_models.py:43`: change `provider = GitHubModelsProvider()` to `provider = GitHubModelsProvider(api_key=settings.github_models_token)`.

- [ ] **Step 3: Fix the mechanical call sites — existing tests**

`tests/test_providers.py`: change all three `provider = GeminiProvider()` (lines 63, 84, 104) to `provider = GeminiProvider(api_key="dummy-key-for-construction-only")`.

`tests/test_groq_provider.py`: change all four `provider = GroqProvider()` (lines 43, 65, 85, 101) to `provider = GroqProvider(api_key="dummy-key-for-construction-only")`.

`tests/test_github_models_provider.py`: change all four `provider = GitHubModelsProvider()` (lines 57, 83, 103, 133) to `provider = GitHubModelsProvider(api_key="dummy-key-for-construction-only")`.

`tests/test_provider_rate_limited.py`: change all three `await GroqProvider().complete(...)` (lines 64, 72, 79) to `await GroqProvider(api_key="dummy-key-for-construction-only").complete(...)`.

- [ ] **Step 4: Run the affected tests to confirm they still pass with the new required parameter**

Run: `uv run pytest tests/test_providers.py tests/test_groq_provider.py tests/test_github_models_provider.py tests/test_provider_rate_limited.py -v`
Expected: PASS for every test EXCEPT the factory tests in `tests/test_providers.py` (`test_factory_selects_gemini`, `test_factory_selects_groq`, `test_factory_selects_github_models`, `test_factory_raises_for_unknown_provider`, `test_factory_rejects_retired_vertex_provider`, `test_factory_returns_the_same_instance_on_repeated_calls`), which still call the *old* `factory._build`/`get_provider` and will error until Step 5-6 land — this is the expected red state.

- [ ] **Step 5: Rewrite the factory**

Replace `app/providers/factory.py` in full:

```python
"""Provider selection by ``LLM_PROVIDER`` (or its DB override), resolved
against the active API-key-slot index (also DB-overridable) per provider.

Narrow on purpose: this module knows which class to instantiate and which
credential to hand it — nothing about provider internals beyond that.

One instance per (provider name, key index) is cached for the process
lifetime — each ``complete()`` call was previously paying a fresh SDK client
construction (and its underlying HTTP client/connection) on every single
specialist call. Settings are read once at import and provider adapters hold
no per-call mutable state, so caching by (provider, index) is safe: a key
swap becomes a cache miss on the new tuple, and the old entry for the
previous index is simply never looked up again — trivial memory cost, no
explicit teardown needed.
"""

from __future__ import annotations

from app.providers import credentials, key_index, registry
from app.providers.active import active_provider
from app.providers.base import LLMProvider
from app.providers.github_models import GitHubModelsProvider
from app.providers.google_genai import GeminiProvider
from app.providers.groq import GroqProvider

_instances: dict[tuple[str, int], LLMProvider] = {}


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
    return GitHubModelsProvider(api_key=api_key)


def get_provider() -> LLMProvider:
    provider = active_provider()
    index = key_index.active_key_index(provider)
    cache_key = (provider, index)
    if cache_key not in _instances:
        _instances[cache_key] = _build(provider, index)
    return _instances[cache_key]


def reset_provider_cache() -> None:
    """Clear the cache. Test-only -- production never needs to invalidate it."""
    _instances.clear()
```

- [ ] **Step 6: Add factory tests for the new cache-invalidation behavior**

Append to `tests/test_providers.py`, in the `# factory.py` section (after `test_factory_returns_the_same_instance_on_repeated_calls`, ~line 165):

```python
def test_factory_rebuilds_the_client_when_the_key_index_changes(monkeypatch):
    from app.providers import key_index

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
```

- [ ] **Step 7: Run the full provider test suite**

Run: `uv run pytest tests/test_providers.py tests/test_groq_provider.py tests/test_github_models_provider.py tests/test_provider_rate_limited.py tests/test_credentials.py tests/test_key_index_override.py -v`
Expected: all PASS

- [ ] **Step 8: Run the full test suite and lint**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all PASS — this is the checkpoint that confirms nothing else in the codebase constructs `GeminiProvider`/`GroqProvider`/`GitHubModelsProvider` directly outside the files already fixed in Steps 2-3.

- [ ] **Step 9: Commit**

```bash
git add app/providers/google_genai.py app/providers/groq.py app/providers/github_models.py \
        app/providers/factory.py scripts/manual_verify_step4.py scripts/manual_verify_groq.py \
        scripts/manual_verify_github_models.py tests/test_providers.py tests/test_groq_provider.py \
        tests/test_github_models_provider.py tests/test_provider_rate_limited.py
git commit -m "feat: key the provider client cache by (provider, key index)"
```

---

### Task 7: CLI (`scripts/set_api_key.py`)

**Files:**
- Create: `scripts/set_api_key.py`
- Test: `tests/test_set_api_key_script.py` (new)

**Interfaces:**
- Consumes: `registry.PROVIDERS` (Task 1), `store.set_key_index_override`/`get_key_index_override` (Task 4), `scripts/_render.py`'s `find_service_id`/`env_vars` (existing, unchanged).
- Produces: `main(argv: list[str] | None = None) -> int`, `_verify_render_key_slot(provider: str, index: int) -> tuple[bool, str]` (used directly by one test that monkeypatches it, mirroring `test_set_provider_script.py::test_clear_never_calls_the_render_verification`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_set_api_key_script.py
"""The operator CLI that sets the DB API-key-index override, per provider.
Uses the shared Postgres test harness -- it writes to the same table the
service reads. Mirrors tests/test_set_provider_script.py."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import respx

from app.config import settings
from app.queue import store
from scripts import set_api_key

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _temp_db(db):
    yield


def test_sets_the_override():
    assert set_api_key.main(["groq", "2"]) == 0
    assert store.get_key_index_override("groq") == 2


def test_clear_removes_the_override():
    set_api_key.main(["groq", "2"])
    assert set_api_key.main(["groq", "--clear"]) == 0
    assert store.get_key_index_override("groq") is None


def test_providers_track_independent_overrides():
    set_api_key.main(["groq", "2"])
    set_api_key.main(["gemini", "1"])
    assert store.get_key_index_override("groq") == 2
    assert store.get_key_index_override("gemini") == 1


def test_rejects_an_unsupported_provider(capsys):
    assert set_api_key.main(["vertex", "1"]) == 2
    err = capsys.readouterr().err
    assert "vertex" in err
    assert "groq" in err
    assert store.get_key_index_override("groq") is None


def test_rejects_a_negative_index(capsys):
    assert set_api_key.main(["groq", "-1"]) == 2
    assert "index" in capsys.readouterr().err


def test_requires_an_index_or_clear(capsys):
    assert set_api_key.main(["groq"]) == 2
    assert "index" in capsys.readouterr().err


def test_requires_a_provider(capsys):
    assert set_api_key.main([]) == 2


def test_clear_and_index_are_mutually_exclusive(capsys):
    assert set_api_key.main(["groq", "2", "--clear"]) == 2


def test_entry_point_runs_as_a_documented_module_invocation():
    """Mirrors test_set_provider_script.py's identically-motivated test: a
    subprocess run of the documented invocation form must actually work."""
    result = subprocess.run(
        [sys.executable, "-m", "scripts.set_api_key", "--help"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


RENDER_SERVICES = "https://api.render.com/v1/services"


def _service_list(service_id="srv-1", name="pr-review-engine"):
    return [{"service": {"id": service_id, "name": name}}]


def _env_var_list(values: dict):
    return [{"envVar": {"key": k, "value": v}} for k, v in values.items()]


def test_sets_the_override_without_a_render_api_key(capsys):
    """No RENDER_API_KEY: verification degrades to a warning and the write
    proceeds -- matches set_provider.py's SKIPPED-on-absent-key convention."""
    assert set_api_key.main(["groq", "2"]) == 0
    assert store.get_key_index_override("groq") == 2
    assert "could not verify against Render" in capsys.readouterr().out


def test_refuses_when_the_slot_is_missing_on_render(monkeypatch, db_url, capsys):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"DATABASE_URL": db_url}))
        )
        code = set_api_key.main(["groq", "2"])
    err = capsys.readouterr().err
    assert code == 2
    assert store.get_key_index_override("groq") is None
    assert "GROQ_API_KEY_2" in err


def test_force_writes_the_override_despite_a_missing_slot(monkeypatch, db_url, capsys):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"DATABASE_URL": db_url}))
        )
        code = set_api_key.main(["groq", "2", "--force"])
    err = capsys.readouterr().err
    assert code == 0
    assert store.get_key_index_override("groq") == 2
    assert "GROQ_API_KEY_2" in err
    assert "--force" in err


def test_proceeds_when_the_slot_is_present_on_render(monkeypatch, db_url, capsys):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200, json=_env_var_list({"DATABASE_URL": db_url, "GROQ_API_KEY_2": "gsk_x"})
            )
        )
        code = set_api_key.main(["groq", "2"])
    assert code == 0
    assert store.get_key_index_override("groq") == 2
    assert "verified present" in capsys.readouterr().out


def test_never_leaks_a_fetched_credential_value(monkeypatch, db_url, capsys):
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
        set_api_key.main(["groq", "2"])
    captured = capsys.readouterr()
    assert "gsk_SUPER_SECRET_REMOTE" not in captured.out
    assert "gsk_SUPER_SECRET_REMOTE" not in captured.err


def test_proceeds_without_refusal_when_local_database_url_does_not_match_render(
    monkeypatch, capsys
):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200, json=_env_var_list({"DATABASE_URL": "postgresql://prod-only/db"})
            )
        )
        code = set_api_key.main(["groq", "2"])
    assert code == 0
    assert store.get_key_index_override("groq") == 2
    assert "could not confirm this DATABASE_URL" in capsys.readouterr().out


def test_clear_never_calls_the_render_verification(monkeypatch):
    def _boom(provider, index):
        raise AssertionError("must not verify on --clear")

    monkeypatch.setattr(set_api_key, "_verify_render_key_slot", _boom)
    assert set_api_key.main(["groq", "--clear"]) == 0


def test_rejects_an_abbreviated_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        set_api_key.main(["groq", "--cle"])
    assert exc.value.code == 2
    assert "--cle" in capsys.readouterr().err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_set_api_key_script.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.set_api_key'`

- [ ] **Step 3: Implement**

```python
# scripts/set_api_key.py
"""Set or clear the DB-backed API-key-slot index override for a provider.

    uv run python -m scripts.set_api_key groq 2
    uv run python -m scripts.set_api_key groq --clear

The override takes effect on the next claimed ticket -- no restart, no
redeploy. It writes to whatever DATABASE_URL points at, so against a local
.env this sets a LOCAL override and nothing reaches production.

Naming convention: index 0 is the base env var (e.g. GROQ_API_KEY); index
N >= 1 is the base name with an "_N" suffix (GROQ_API_KEY_1, _2, ...). This
script never reads, prints, or stores a credential VALUE -- only the index,
and only a presence check against Render before writing.

Before writing a non-cleared override, this verifies the target env var's
PRESENCE (not its value, and not a live call to the provider) against the
live Render service (when RENDER_API_KEY is set and the local DATABASE_URL
is the one Render actually reads) and refuses by default if it's missing --
pass --force to write anyway. Unlike scripts/set_provider.py's
_verify_render_credential, this does not compare against a local .env value:
a numbered slot typically has no local counterpart at all, so presence on
the live service is the only meaningful check.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from app.config import settings
from app.providers import registry
from app.queue import store
from scripts import _render


def _env_name(provider: str, index: int) -> str:
    base, _ = registry.PROVIDERS[provider]
    return base if index == 0 else f"{base}_{index}"


def _verify_render_key_slot(provider: str, index: int) -> tuple[bool, str]:
    """(ok_to_proceed, message). Never returns, prints, or logs a fetched
    Render value -- only presence/absence -- mirroring
    scripts/set_provider.py's _verify_render_credential.
    """
    env_name = _env_name(provider, index)
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
        return False, (
            f"{env_name} is missing on the Render service; the override would fail "
            "every review immediately. Push it first, or pass --force"
        )
    return True, f"{env_name} verified present on Render"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="set_api_key",
        # Without this, argparse treats a truncated flag like --cle as an
        # abbreviation of --clear and runs it -- scripts/set_provider.py and
        # scripts/deploy.py carry the same guard after an identical
        # abbreviation match fired a live production sync.
        allow_abbrev=False,
        description="Set or clear the DB-backed API-key-slot index override for a provider.",
    )
    parser.add_argument(
        "provider",
        # nargs="?": a required positional with no value at all makes
        # argparse raise SystemExit(2) itself before main() ever runs, which
        # would bypass this script's own "a provider is required" message
        # and break `main([]) == 2` as a return value rather than an
        # uncaught SystemExit. scripts/set_provider.py's `provider` carries
        # the same nargs="?" for the identical reason.
        nargs="?",
        help=f"one of: {', '.join(sorted(registry.PROVIDERS))}",
    )
    parser.add_argument(
        "index",
        nargs="?",
        type=int,
        help="the slot index to activate (0 = the base env var, N = the _N suffix)",
    )
    parser.add_argument(
        "--clear", action="store_true", help="remove the override; fall back to index 0"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="write the override even if live verification against Render finds a problem",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if not args.provider:
        print("a provider is required", file=sys.stderr)
        return 2
    if args.provider not in registry.PROVIDERS:
        accepted = ", ".join(sorted(registry.PROVIDERS))
        print(
            f"unsupported provider {args.provider!r} (expected one of: {accepted})",
            file=sys.stderr,
        )
        return 2
    if args.clear and args.index is not None:
        print("--clear and an index are mutually exclusive", file=sys.stderr)
        return 2
    if not args.clear and args.index is None:
        print("an index is required (or --clear)", file=sys.stderr)
        return 2
    if not args.clear and args.index < 0:
        print(f"index must be >= 0, got {args.index}", file=sys.stderr)
        return 2

    index = None if args.clear else args.index
    if index is not None:
        ok, message = _verify_render_key_slot(args.provider, index)
        if ok:
            print(message)
        elif args.force:
            print(f"{message} -- proceeding anyway (--force)", file=sys.stderr)
        else:
            print(f"refusing to set the override: {message}", file=sys.stderr)
            return 2
    store.init_pool()
    store.set_key_index_override(args.provider, index, datetime.now(timezone.utc).isoformat())
    print("override cleared; falling back to index 0" if index is None
          else f"override set to index {index}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_set_api_key_script.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/set_api_key.py tests/test_set_api_key_script.py
git commit -m "feat: add scripts/set_api_key.py to swap API-key slots without a redeploy"
```

---

### Task 8: Deploy-time check (`scripts/deploy.py::check_api_key_live`)

**Files:**
- Modify: `scripts/deploy.py` — add `_resolved_key_index`/`_resolved_key_index_or_env` (mirroring `_resolved_provider`/`_resolved_provider_or_env`, `scripts/deploy.py:272-330`), add `check_api_key_live` (mirroring `check_provider_live`, `scripts/deploy.py:332-375`), register it in `run_checks` (`:752-764`), update `build_parser`'s description (`:767-790`)
- Test: `tests/test_deploy_script.py` (append)

**Interfaces:**
- Consumes: `registry.KEY_INDEX_COLUMNS`, `registry.PROVIDERS` (as `_PROVIDERS`, Task 1), `_resolved_provider_or_env` (existing).
- Produces: `check_api_key_live() -> CheckResult`, registered as the `"api-key-live"` row.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_deploy_script.py`, near the existing `_resolved_provider_or_env`/`check_provider_live` tests (~line 1340-1425):

```python
def test_resolved_key_index_or_env_falls_back_without_a_database_url(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "")
    assert deploy._resolved_key_index_or_env("groq") == (0, None)


def test_resolved_key_index_or_env_resolves_the_override_when_database_url_is_set(
    override_seam,
):
    override_seam((2,))
    assert deploy._resolved_key_index_or_env("groq") == (2, 2)


def test_resolved_key_index_or_env_defaults_to_zero_when_no_override(override_seam):
    override_seam(None)
    assert deploy._resolved_key_index_or_env("groq") == (0, None)


def test_resolved_key_index_or_env_propagates_a_db_error(override_seam):
    override_seam(RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        deploy._resolved_key_index_or_env("groq")


def test_api_key_live_skips_without_a_render_api_key(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "")
    assert deploy.check_api_key_live().status == "SKIPPED"


def test_api_key_live_skips_when_the_provider_resolution_raises(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(deploy, "_resolved_provider_or_env", boom)
    assert deploy.check_api_key_live().status == "SKIPPED"


def test_api_key_live_skips_when_the_index_resolution_raises(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(deploy, "_resolved_provider_or_env", lambda: ("groq", None))

    def boom(provider):
        raise RuntimeError("db down")

    monkeypatch.setattr(deploy, "_resolved_key_index_or_env", boom)
    assert deploy.check_api_key_live().status == "SKIPPED"


def test_api_key_live_skips_for_an_unsupported_provider(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(deploy, "_resolved_provider_or_env", lambda: ("vertex", None))
    assert deploy.check_api_key_live().status == "SKIPPED"


def test_api_key_live_passes_for_index_zero_present_on_render(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    monkeypatch.setattr(deploy, "_resolved_provider_or_env", lambda: ("groq", None))
    monkeypatch.setattr(deploy, "_resolved_key_index_or_env", lambda provider: (0, None))
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"GROQ_API_KEY": "gsk_x"}))
        )
        result = deploy.check_api_key_live()
    assert result.status == "PASS"
    assert "GROQ_API_KEY" in result.detail
    assert "GROQ_API_KEY_" not in result.detail  # index 0 -> unsuffixed name


def test_api_key_live_fails_when_the_overrides_slot_is_missing_on_render(monkeypatch):
    """The exact failure mode this check exists to catch: the DB override
    names index 2 but nobody ever pushed GROQ_API_KEY_2 to Render."""
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    monkeypatch.setattr(deploy, "_resolved_provider_or_env", lambda: ("groq", None))
    monkeypatch.setattr(deploy, "_resolved_key_index_or_env", lambda provider: (2, 2))
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"GROQ_API_KEY": "gsk_x"}))
        )
        result = deploy.check_api_key_live()
    assert result.status == "FAIL"
    assert "GROQ_API_KEY_2" in result.detail
    assert "not present" in result.detail


def test_api_key_live_never_leaks_a_fetched_value(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    monkeypatch.setattr(deploy, "_resolved_provider_or_env", lambda: ("groq", None))
    monkeypatch.setattr(deploy, "_resolved_key_index_or_env", lambda provider: (0, None))
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(
                200, json=_env_var_list({"GROQ_API_KEY": "gsk_SUPER_SECRET"})
            )
        )
        result = deploy.check_api_key_live()
    assert "gsk_SUPER_SECRET" not in result.detail


def test_run_checks_includes_the_api_key_live_row(monkeypatch):
    monkeypatch.setattr(deploy, "check_api_key_live",
                        lambda: deploy.CheckResult("api-key-live", "PASS", ""))
    for fn, row in (
        ("check_config", "config"),
        ("check_installation_and_webhook", "github-app"),
        ("check_health_endpoint", "health"),
        ("check_database", "database"),
        ("check_provider", "provider"),
        ("check_provider_live", "provider-live"),
        ("check_render_service", "render-service"),
        ("check_uptime_pinger", "uptime-pinger"),
    ):
        monkeypatch.setattr(deploy, fn, lambda *a, _n=row: deploy.CheckResult(_n, "PASS", ""))
    names = [r.name for r in deploy.run_checks("owner/repo", BASE)]
    assert "api-key-live" in names
    assert names.index("api-key-live") > names.index("provider-live")
```

Also add `("check_api_key_live", "api-key-live")` to the stub loops of the two pre-existing composite tests, `test_run_checks_includes_the_provider_live_row` (~line 1428) and `test_run_checks_includes_the_provider_row` (~line 1446), so both still fully stub `run_checks`'s (now nine) checks:

```python
def test_run_checks_includes_the_provider_live_row(monkeypatch):
    monkeypatch.setattr(deploy, "check_provider_live",
                        lambda: deploy.CheckResult("provider-live", "PASS", ""))
    for fn, row in (
        ("check_config", "config"),
        ("check_installation_and_webhook", "github-app"),
        ("check_health_endpoint", "health"),
        ("check_database", "database"),
        ("check_provider", "provider"),
        ("check_api_key_live", "api-key-live"),
        ("check_render_service", "render-service"),
        ("check_uptime_pinger", "uptime-pinger"),
    ):
        monkeypatch.setattr(deploy, fn, lambda *a, _n=row: deploy.CheckResult(_n, "PASS", ""))
    names = [r.name for r in deploy.run_checks("owner/repo", BASE)]
    assert "provider-live" in names
    assert names.index("provider-live") > names.index("provider")


def test_run_checks_includes_the_provider_row(monkeypatch):
    monkeypatch.setattr(deploy, "check_provider",
                        lambda: deploy.CheckResult("provider", "PASS", ""))
    for fn, row in (
        ("check_config", "config"),
        ("check_installation_and_webhook", "github-app"),
        ("check_health_endpoint", "health"),
        ("check_database", "database"),
        ("check_provider_live", "provider-live"),
        ("check_api_key_live", "api-key-live"),
        ("check_render_service", "render-service"),
        ("check_uptime_pinger", "uptime-pinger"),
    ):
        monkeypatch.setattr(deploy, fn,
                            lambda *a, _n=row: deploy.CheckResult(_n, "PASS", ""))
    names = [r.name for r in deploy.run_checks("owner/repo", BASE)]
    assert "provider" in names
    assert names.index("provider") > names.index("database")
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `uv run pytest tests/test_deploy_script.py -k "key_index or api_key_live" -v`
Expected: FAIL with `AttributeError: module 'scripts.deploy' has no attribute '_resolved_key_index_or_env'`

- [ ] **Step 3: Implement `_resolved_key_index`/`_resolved_key_index_or_env`**

In `scripts/deploy.py`, insert immediately after `_resolved_provider_or_env` (currently ending at line 329, right before `def check_provider_live`):

```python
def _resolved_key_index(provider: str) -> tuple[int, int | None]:
    """(active index, override or None) for `provider`. Reads via a raw
    short-timeout connection, mirroring _resolved_provider() for the same
    reason: a one-shot CLI must not pay store.init_pool()'s 30s timeout.
    """
    column = registry.KEY_INDEX_COLUMNS[provider]
    with psycopg.connect(settings.database_url, connect_timeout=_DB_CONNECT_TIMEOUT) as conn:
        row = conn.execute(f"SELECT {column} FROM runtime_config WHERE id = 1").fetchone()
    override = row[0] if row else None
    return (override if override is not None else 0), override


def _resolved_key_index_or_env(provider: str) -> tuple[int, int | None]:
    """Like _resolved_key_index(), but usable without DATABASE_URL: without a
    database there is no override to check, so this falls back to index 0.
    """
    if not settings.database_url:
        return 0, None
    return _resolved_key_index(provider)
```

- [ ] **Step 4: Implement `check_api_key_live`**

Insert immediately after `check_provider_live` (currently ending at line 375):

```python
def check_api_key_live() -> CheckResult:
    """Whether the actively-resolved provider's actively-resolved key SLOT is
    genuinely present on the live Render service -- catches "the DB says
    index 2 but nobody ever pushed GROQ_API_KEY_2 to Render", the same class
    of gap check_provider_live catches for the provider name itself.
    """
    name = "api-key-live"
    if not settings.render_api_key:
        return CheckResult(
            name, "SKIPPED", "set RENDER_API_KEY to verify credentials against the live service"
        )
    try:
        provider, _provider_override = _resolved_provider_or_env()
    # deliberate: a DB problem is provider's/database's row to report, not ours
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name, "SKIPPED", f"could not resolve the active provider ({type(exc).__name__})"
        )
    entry = _PROVIDERS.get(provider)
    if entry is None:
        return CheckResult(name, "SKIPPED", f"{provider} is not a supported provider")
    try:
        index, index_override = _resolved_key_index_or_env(provider)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name, "SKIPPED", f"could not resolve the active key index ({type(exc).__name__})"
        )
    credential, _ = entry
    env_name = credential if index == 0 else f"{credential}_{index}"
    source = f"index {index}" + (" (DB override)" if index_override is not None else "")
    try:
        service_id = _render.find_service_id()
        if service_id is None:
            return CheckResult(name, "FAIL", f"no service named {settings.render_service_name}")
        live_value = _render.env_vars(service_id).get(env_name) or ""
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "FAIL", f"Render API error ({type(exc).__name__})")
    if not live_value:
        return CheckResult(
            name, "FAIL", f"{provider} ({source}) -- {env_name} not present on Render"
        )
    return CheckResult(name, "PASS", f"{provider} ({source}) -- {env_name} present on Render")
```

- [ ] **Step 5: Register the check and update the CLI description**

In `run_checks` (`scripts/deploy.py:752-764`), add the new row right after `provider-live`:

```python
def run_checks(repo: str, base: str) -> list[CheckResult]:
    """All nine, cheapest and most foundational first, so a misconfiguration
    is reported before the checks that would fail as a consequence of it."""
    return [
        _safe("config", check_config),
        _safe("github-app", check_installation_and_webhook, repo, base),
        _safe("health", check_health_endpoint, base),
        _safe("database", check_database),
        _safe("provider", check_provider),
        _safe("provider-live", check_provider_live),
        _safe("api-key-live", check_api_key_live),
        _safe("render-service", check_render_service),
        _safe("uptime-pinger", check_uptime_pinger, base),
    ]
```

In `build_parser` (`scripts/deploy.py:774-779`), update the description to mention the new check:

```python
        description=(
            "Verify the hosted deployment: configuration, GitHub App installation "
            "and webhook, health endpoint, database, active provider, whether that "
            "provider's credential is actually live on Render, whether its active "
            "API-key slot is actually live on Render, Render service, and keep-warm "
            "pinger. Exit 0 all passed, 1 a check failed, 2 could not run."
        ),
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_deploy_script.py -v`
Expected: all PASS (including every pre-existing test in the file — confirms the new check and the two updated composite tests didn't disturb anything else)

- [ ] **Step 7: Run the full suite and lint**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all PASS

- [ ] **Step 8: Commit**

```bash
git add scripts/deploy.py tests/test_deploy_script.py
git commit -m "feat: add the api-key-live deploy check for the active key slot"
```

---

### Task 9: Documentation (`SPEC.md`, `README.md`)

**Files:**
- Modify: `SPEC.md` (extend the `runtime_config` / provider-override section, ~lines 356-403)
- Modify: `README.md` (new subsection after "Tuning the re-review cooldown without a redeploy", before "Deploying an image, when the Render service has no connected repo", ~line 228)

**Interfaces:** None — this task produces no code other tasks depend on.

- [ ] **Step 1: Extend `SPEC.md`**

Insert a new paragraph in `SPEC.md` immediately after the "Tuning base/cap/factor." paragraph (which currently ends at line 403, right before "**Re-review scheduled notice.**"):

```markdown
**Swapping API-key slots.** Each provider's credential env var can have
numbered siblings (`GROQ_API_KEY`, `GROQ_API_KEY_1`, `GROQ_API_KEY_2`, ...),
provisioned like any other env var (one redeploy to add a slot). A separate
`runtime_config` override per provider (`gemini_key_index`, `groq_key_index`,
`github_models_key_index`) records which slot is active; `NULL` means index
0, the base env var. `scripts/set_api_key.py` writes it — the same
no-redeploy, next-claimed-ticket mechanics as the provider/cooldown
overrides — and no secret ever reaches Postgres: only the integer index
does. `app/providers/factory.py` keys its client cache by `(provider,
index)`, so a swap invalidates exactly the right cached SDK client rather
than the whole cache. `scripts/deploy.py`'s `api-key-live` check is the
read-only counterpart, mirroring `provider-live`: it confirms the actively-
resolved index's env var is genuinely present on the live Render service.
```

- [ ] **Step 2: Extend `README.md`**

Insert a new subsection in `README.md` immediately after the "Tuning the re-review cooldown without a redeploy" subsection (which ends at line 227) and before "#### Deploying an image, when the Render service has no connected repo" (line 229):

```markdown
#### Swapping API keys without a redeploy

```bash
uv run python -m scripts.set_api_key groq 2       # activate GROQ_API_KEY_2
uv run python -m scripts.set_api_key groq --clear  # back to GROQ_API_KEY (index 0)
```

Each provider's credential env var can have numbered siblings —
`GROQ_API_KEY`, `GROQ_API_KEY_1`, `GROQ_API_KEY_2`, ... — provisioned ahead
of time exactly like any other env var (one redeploy, via `--sync-env` or
the Render dashboard, to add a new slot). This writes which slot is
**active** to the `runtime_config` table and takes effect on the **next
ticket the dispatcher claims** — no restart, no redeploy, and no secret
value is ever written to, read from, or logged by the database: only the
slot's integer index is. Each provider tracks its own index independently,
so switching providers never disturbs the slot chosen for the other two.
Same presence-only Render verification as `set_provider.py` — refuses by
default (pass `--force` to override) if the target slot's env var is
missing on the live service, but never compares against a local `.env`
value, since a numbered slot typically has no local counterpart at all.
`scripts/deploy.py`'s `api-key-live` check is the read-only counterpart: it
confirms the actively-resolved provider's actively-resolved slot is
genuinely present on Render, catching the exact gap a redeploy-free index
flip can introduce — the DB says index 2, but nobody ever pushed
`GROQ_API_KEY_2` to Render.
```

- [ ] **Step 3: Verify the doc changes render sensibly**

Run: `uv run ruff check .` (confirms no Python files were accidentally broken by this task) and manually re-read the two edited sections in `README.md`/`SPEC.md` for consistency with the surrounding prose (no code to run — this is a documentation-only task).

- [ ] **Step 4: Commit**

```bash
git add SPEC.md README.md
git commit -m "docs: document swapping API-key slots without a redeploy"
```

---

## Final verification

After Task 9, run the entire suite once more end-to-end to confirm the whole feature is coherent:

```bash
uv run ruff check .
uv run pytest -q
```

Expected: both commands exit 0. This is also the point to (optionally, manually, outside this plan) try the CLI against a local `.env`-pointed database:

```bash
uv run python -m scripts.set_api_key groq 1
uv run python -m scripts.deploy   # api-key-live should now report against index 1 (or SKIPPED without RENDER_API_KEY)
uv run python -m scripts.set_api_key groq --clear
```
