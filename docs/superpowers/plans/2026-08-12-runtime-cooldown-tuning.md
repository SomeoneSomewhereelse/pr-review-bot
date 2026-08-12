# Runtime-Tunable Cooldown & Escalation Factor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the escalating re-review cooldown's three parameters (base, cap,
escalation factor) tunable at demo time without a redeploy, on top of a
config-layer fix that makes the factor an env var for the first time.

**Architecture:** Two independent layers. (1) `dispatcher_rereview_cooldown_factor`
becomes a validated env var, replacing the hardcoded `2` in
`store.effective_cooldown`; declared in `render.yaml` alongside the two existing
cooldown vars. (2) A DB-backed live override — three new nullable columns on the
existing `runtime_config` singleton row, a cache module mirroring
`app/providers/active.py`, refreshed once per claimed ticket in the dispatcher
(same fail-safe pattern as the provider override), and a `scripts/set_cooldown.py`
CLI mirroring `scripts/set_provider.py` that writes it.

**Tech Stack:** FastAPI/Python 3.12, Postgres via psycopg3 (`app/queue/store.py`),
pydantic-settings (`app/config.py`), pytest + the shared Postgres test harness
(`tests/conftest.py`'s `db`/`db_exec`/`db_query`/`db_url` fixtures), `uv run pytest`.

## Global Constraints

- `dispatcher_rereview_cooldown_factor` must be `>= 1.0` (a factor `< 1` would
  shrink the cooldown, defeating escalation) — enforced by both the Pydantic
  `Field(ge=1.0)` (env var) and `scripts/set_cooldown.py`'s client-side check
  (override).
- A DB-read override is fail-safe: any read failure, or a read value that is
  individually or jointly invalid (`factor < 1.0` or `base > cap`), degrades to
  the env-var defaults for the *whole* triple (never a partial mix of override
  and default fields) and must never raise into the review path.
- The cooldown-override cache is refreshed exactly once per claimed ticket
  (never once per idle dispatcher tick), matching the existing provider-override
  refresh in `app/queue/dispatcher.py::process_next_due`.
- No secret is ever logged (CLAUDE.md) — not applicable to cooldown numbers
  themselves, but `scripts/set_cooldown.py` reuses `scripts/_render.py`, which
  carries this constraint for any Render API response it touches.
- No `--force` flag on `scripts/set_cooldown.py` — the Render-verification
  check here is informational only and never refuses a write (unlike
  `set_provider.py`, there is no credential correctness at stake).

---

## File Structure

| File | Change |
|---|---|
| `app/config.py` | Add `dispatcher_rereview_cooldown_factor` field |
| `app/queue/store.py` | Schema migration (3 columns); `get_cooldown_overrides`/`set_cooldown_override`; `effective_cooldown` reads through the new cache module |
| `app/queue/cooldown_config.py` | New — cache + fail-safe fallback, mirrors `app/providers/active.py` |
| `app/queue/dispatcher.py` | Refresh the cooldown cache once per claimed ticket |
| `scripts/set_cooldown.py` | New CLI |
| `render.yaml` | 3 new `sync: false` entries |
| `SPEC.md` | §12 update |
| `tests/test_queue_store.py` | Cooldown override store tests + `effective_cooldown` factor tests |
| `tests/test_cooldown_config.py` | New — cache module tests |
| `tests/test_dispatcher.py` | Cache-refresh-per-claimed-ticket tests |
| `tests/test_set_cooldown_script.py` | New — CLI tests |

---

### Task 1: `dispatcher_rereview_cooldown_factor` env var + `render.yaml`

**Files:**
- Modify: `app/config.py:40-47`
- Modify: `render.yaml`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `settings.dispatcher_rereview_cooldown_factor: float` (default `2.0`, `ge=1.0`), consumed by Task 3 (`cooldown_config.py`) and Task 2 (`store.effective_cooldown`).

`tests/test_config.py` already exists (validates `dispatcher_notice_sweep_batch_size`
the same way) — add to it rather than creating a new file.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
def test_cooldown_factor_rejects_below_one():
    with pytest.raises(ValidationError):
        Settings(dispatcher_rereview_cooldown_factor=0.5)


def test_cooldown_factor_accepts_exactly_one():
    assert Settings(dispatcher_rereview_cooldown_factor=1.0).dispatcher_rereview_cooldown_factor == 1.0


def test_cooldown_factor_defaults_to_two():
    assert Settings().dispatcher_rereview_cooldown_factor == 2.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `dispatcher_rereview_cooldown_factor` does not exist on `Settings`.

- [ ] **Step 3: Add the field**

In `app/config.py`, immediately after the existing cooldown fields (after
`dispatcher_rereview_cooldown_max_seconds: float = 3600.0`, before
`dispatcher_notice_sweep_batch_size`), add:

```python
    # ge=1.0: a factor < 1 would shrink the cooldown across escalation
    # levels instead of lengthening it, defeating the point of escalation.
    dispatcher_rereview_cooldown_factor: float = Field(default=2.0, ge=1.0)
```

(`Field` is already imported at the top of `app/config.py`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Add the three cooldown env vars to `render.yaml`**

In `render.yaml`, after the existing `envVars` entries (after `GITHUB_MODELS_MODEL`), add:

```yaml
      - key: DISPATCHER_REREVIEW_COOLDOWN_SECONDS
        sync: false
      - key: DISPATCHER_REREVIEW_COOLDOWN_MAX_SECONDS
        sync: false
      - key: DISPATCHER_REREVIEW_COOLDOWN_FACTOR
        sync: false
```

- [ ] **Step 6: Commit**

```bash
git add app/config.py render.yaml tests/test_config.py
git commit -m "feat: add dispatcher_rereview_cooldown_factor env var"
```

---

### Task 2: `runtime_config` schema migration + store functions

**Files:**
- Modify: `app/queue/store.py:47-51` (`_SCHEMA`), and near `get_provider_override`/`set_provider_override` (currently lines 509-533)
- Test: `tests/test_queue_store.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `store.get_cooldown_overrides() -> tuple[float | None, float | None, float | None]` (base, cap, factor); `store.set_cooldown_override(base: float | None, cap: float | None, factor: float | None, now: str) -> None`. Consumed by Task 3 (`cooldown_config.py`) and Task 5 (`scripts/set_cooldown.py`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_queue_store.py` (near the existing `effective_cooldown` tests, after `test_next_cooldown_level_increments_and_guards`):

```python
def test_cooldown_overrides_default_to_none():
    assert store.get_cooldown_overrides() == (None, None, None)


def test_set_then_get_cooldown_overrides():
    store.set_cooldown_override(base=30.0, cap=600.0, factor=1.5, now=T0)
    assert store.get_cooldown_overrides() == (30.0, 600.0, 1.5)


def test_setting_cooldown_override_twice_replaces_rather_than_inserting(db_query):
    store.set_cooldown_override(base=30.0, cap=600.0, factor=1.5, now=T0)
    store.set_cooldown_override(base=60.0, cap=1200.0, factor=2.0, now=T1)
    assert store.get_cooldown_overrides() == (60.0, 1200.0, 2.0)
    assert db_query("SELECT count(*) FROM runtime_config")[0][0] == 1


def test_clearing_cooldown_override_restores_none():
    store.set_cooldown_override(base=30.0, cap=600.0, factor=1.5, now=T0)
    store.set_cooldown_override(base=None, cap=None, factor=None, now=T1)
    assert store.get_cooldown_overrides() == (None, None, None)


def test_cooldown_override_and_provider_override_coexist():
    """Both overrides live on the same singleton row -- setting one must not
    clobber the other."""
    store.set_provider_override("groq", T0)
    store.set_cooldown_override(base=30.0, cap=600.0, factor=1.5, now=T1)
    assert store.get_provider_override() == "groq"
    assert store.get_cooldown_overrides() == (30.0, 600.0, 1.5)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_queue_store.py -k cooldown_override -v`
Expected: FAIL — `AttributeError: module 'app.queue.store' has no attribute 'get_cooldown_overrides'`

- [ ] **Step 3: Add the schema migration**

In `app/queue/store.py`, change the `runtime_config` block inside `_SCHEMA`
(currently):

```python
CREATE TABLE IF NOT EXISTS runtime_config (
    id         INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    provider   TEXT,
    updated_at TEXT NOT NULL
);
```

to:

```python
CREATE TABLE IF NOT EXISTS runtime_config (
    id         INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    provider   TEXT,
    updated_at TEXT NOT NULL
);
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS cooldown_base_seconds DOUBLE PRECISION;
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS cooldown_max_seconds  DOUBLE PRECISION;
ALTER TABLE runtime_config ADD COLUMN IF NOT EXISTS cooldown_factor       DOUBLE PRECISION;
```

- [ ] **Step 4: Add the store functions**

In `app/queue/store.py`, immediately after `set_provider_override` (the function
ending at line 533), add:

```python
def get_cooldown_overrides() -> tuple[float | None, float | None, float | None]:
    """(base, cap, factor) overrides in force, or (None, None, None) when unset.

    Synchronous like every other store function -- async callers use
    asyncio.to_thread.
    """
    with _require_pool().connection() as conn:
        row = conn.execute(
            "SELECT cooldown_base_seconds, cooldown_max_seconds, cooldown_factor "
            "FROM runtime_config WHERE id = 1"
        ).fetchone()
    if row is None:
        return (None, None, None)
    return (row["cooldown_base_seconds"], row["cooldown_max_seconds"], row["cooldown_factor"])


def set_cooldown_override(
    base: float | None, cap: float | None, factor: float | None, now: str
) -> None:
    """Set the (base, cap, factor) override triple, or clear a field with None.

    Upserts the singleton row -- same CHECK (id = 1) guarantee as
    set_provider_override. Writes exactly the three values it's given; a
    caller wanting to change only one field is responsible for reading the
    current triple first (see scripts/set_cooldown.py).
    """
    with _require_pool().connection() as conn:
        conn.execute(
            "INSERT INTO runtime_config "
            "(id, cooldown_base_seconds, cooldown_max_seconds, cooldown_factor, updated_at) "
            "VALUES (1, %s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET "
            "cooldown_base_seconds = EXCLUDED.cooldown_base_seconds, "
            "cooldown_max_seconds = EXCLUDED.cooldown_max_seconds, "
            "cooldown_factor = EXCLUDED.cooldown_factor, "
            "updated_at = EXCLUDED.updated_at",
            (base, cap, factor, now),
        )
```

Note: this `INSERT ... ON CONFLICT` only sets the four columns it names — on a
fresh insert, `provider` takes its column default (`NULL`), and on a conflict
update, `provider` is untouched (matches `set_provider_override`'s own
INSERT, which likewise omits the cooldown columns and leaves them alone on
conflict). This is why Task 2 Step 1's coexistence test matters.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_queue_store.py -k cooldown_override -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/queue/store.py tests/test_queue_store.py
git commit -m "feat: add DB-backed cooldown override storage"
```

---

### Task 3: `cooldown_config.py` cache module + `effective_cooldown` wiring

**Files:**
- Create: `app/queue/cooldown_config.py`
- Modify: `app/queue/store.py` (the `effective_cooldown` function, currently lines 163-172)
- Test: `tests/test_cooldown_config.py` (new)
- Test: `tests/test_queue_store.py` (extend `effective_cooldown` tests for the factor param)

**Interfaces:**
- Consumes: `settings.dispatcher_rereview_cooldown_seconds`, `settings.dispatcher_rereview_cooldown_max_seconds`, `settings.dispatcher_rereview_cooldown_factor` (Task 1).
- Produces: `cooldown_config.effective_config() -> tuple[float, float, float]` (base, cap, factor); `cooldown_config.set_override_cache(base: float | None, cap: float | None, factor: float | None) -> None`; `cooldown_config.reset_override_cache() -> None`. Consumed by Task 2's `store.effective_cooldown` and Task 4 (dispatcher refresh).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cooldown_config.py`:

```python
"""The DB-backed cooldown override cache: base/cap/factor overrides with a
fail-safe fallback to env-var settings. Mirrors tests/test_provider_override.py's
active-provider-cache tests, but for the cooldown triple."""
from __future__ import annotations

from app.config import settings
from app.queue import cooldown_config


def _set_env_defaults(monkeypatch):
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_max_seconds", 3600.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_factor", 2.0)


def setup_function():
    cooldown_config.reset_override_cache()


def teardown_function():
    cooldown_config.reset_override_cache()


def test_no_override_falls_back_to_env_defaults(monkeypatch):
    _set_env_defaults(monkeypatch)
    assert cooldown_config.effective_config() == (300.0, 3600.0, 2.0)


def test_full_override_is_used(monkeypatch):
    _set_env_defaults(monkeypatch)
    cooldown_config.set_override_cache(30.0, 600.0, 1.5)
    assert cooldown_config.effective_config() == (30.0, 600.0, 1.5)


def test_partial_override_mixes_with_env_defaults(monkeypatch):
    _set_env_defaults(monkeypatch)
    cooldown_config.set_override_cache(30.0, None, None)
    assert cooldown_config.effective_config() == (30.0, 3600.0, 2.0)


def test_invalid_factor_falls_back_to_env_defaults_entirely(monkeypatch):
    """A factor < 1 discards the WHOLE override triple, not just the factor --
    a bad factor must not silently pair with a stale overridden base/cap."""
    _set_env_defaults(monkeypatch)
    cooldown_config.set_override_cache(30.0, 600.0, 0.5)
    assert cooldown_config.effective_config() == (300.0, 3600.0, 2.0)


def test_base_above_cap_falls_back_to_env_defaults_entirely(monkeypatch):
    _set_env_defaults(monkeypatch)
    cooldown_config.set_override_cache(700.0, 600.0, 1.5)
    assert cooldown_config.effective_config() == (300.0, 3600.0, 2.0)


def test_clearing_the_cache_returns_to_env_defaults(monkeypatch):
    _set_env_defaults(monkeypatch)
    cooldown_config.set_override_cache(30.0, 600.0, 1.5)
    cooldown_config.reset_override_cache()
    assert cooldown_config.effective_config() == (300.0, 3600.0, 2.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cooldown_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.queue.cooldown_config'`

- [ ] **Step 3: Create `app/queue/cooldown_config.py`**

```python
"""The re-review cooldown parameters actually in force: a DB override
(base/cap/factor) when set and valid, else the env-configured defaults.

Every read of the effective cooldown config goes through effective_config().
Mirrors app/providers/active.py's provider-override cache exactly, including
the reason for the split: the DB read lives in the dispatcher (where the
asyncio.to_thread convention applies) and is pushed in via set_override_cache,
keeping this module import-light and non-blocking.

Fail-safe by construction: the cache starts empty, so before the first refresh
-- and whenever a refresh fails -- the service degrades to its configured
defaults rather than to no cooldown. An override that reads back invalid
(factor < 1, or base > cap) is discarded as a WHOLE triple, never partially
applied, so a bad field can never pair with a stale override in another field.
"""

from __future__ import annotations

from app.config import settings

_base: float | None = None
_cap: float | None = None
_factor: float | None = None


def effective_config() -> tuple[float, float, float]:
    """(base, cap, factor) -- the DB override where fully valid, else the env defaults."""
    base = _base if _base is not None else settings.dispatcher_rereview_cooldown_seconds
    cap = _cap if _cap is not None else settings.dispatcher_rereview_cooldown_max_seconds
    factor = _factor if _factor is not None else settings.dispatcher_rereview_cooldown_factor
    if factor < 1.0 or base > cap:
        return (
            settings.dispatcher_rereview_cooldown_seconds,
            settings.dispatcher_rereview_cooldown_max_seconds,
            settings.dispatcher_rereview_cooldown_factor,
        )
    return base, cap, factor


def set_override_cache(base: float | None, cap: float | None, factor: float | None) -> None:
    global _base, _cap, _factor
    _base, _cap, _factor = base, cap, factor


def reset_override_cache() -> None:
    set_override_cache(None, None, None)
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `uv run pytest tests/test_cooldown_config.py -v`
Expected: PASS

- [ ] **Step 5: Wire `effective_cooldown` through the cache module**

In `app/queue/store.py`, change:

```python
def effective_cooldown(level: int) -> float:
    """Escalated per-PR cooldown: min(base * 2^min(level, _MAX_COOLDOWN_LEVEL), cap).

    level 0 -> base (identical to a non-escalating cooldown, so normal PRs are
    unaffected). Each consecutive rapid re-review raises the level, geometrically
    lengthening the next wait, capped at dispatcher_rereview_cooldown_max_seconds.
    """
    base = settings.dispatcher_rereview_cooldown_seconds
    cap = settings.dispatcher_rereview_cooldown_max_seconds
    return max(base, min(base * 2 ** min(level, _MAX_COOLDOWN_LEVEL), cap))
```

to:

```python
def effective_cooldown(level: int) -> float:
    """Escalated per-PR cooldown: min(base * factor^min(level, _MAX_COOLDOWN_LEVEL), cap).

    level 0 -> base (identical to a non-escalating cooldown, so normal PRs are
    unaffected). Each consecutive rapid re-review raises the level, geometrically
    lengthening the next wait, capped at the effective cap. base/cap/factor come
    from cooldown_config.effective_config() -- a DB override when set and valid,
    else the env-configured defaults.
    """
    base, cap, factor = cooldown_config.effective_config()
    return max(base, min(base * factor ** min(level, _MAX_COOLDOWN_LEVEL), cap))
```

Add the import near the top of `app/queue/store.py` (alongside the existing
`from app.config import settings`):

```python
from app.queue import cooldown_config
```

- [ ] **Step 6: Update the existing `effective_cooldown` tests to also cover the factor**

In `tests/test_queue_store.py`, the two existing tests
(`test_effective_cooldown_escalates_and_caps`,
`test_effective_cooldown_never_drops_below_base_when_cap_misconfigured`) already
monkeypatch `settings.dispatcher_rereview_cooldown_seconds` and
`settings.dispatcher_rereview_cooldown_max_seconds` — they'll keep passing
unchanged since `dispatcher_rereview_cooldown_factor` defaults to `2.0`. Add one
new test right after `test_next_cooldown_level_increments_and_guards`:

```python
def test_effective_cooldown_uses_a_configured_factor(monkeypatch):
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 30.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_max_seconds", 300.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_factor", 3.0)
    assert store.effective_cooldown(0) == 30.0
    assert store.effective_cooldown(1) == 90.0
    assert store.effective_cooldown(2) == 270.0
    assert store.effective_cooldown(3) == 300.0  # 810 -> capped
```

Also add a fixture to reset the cache around every test in this file (the cache
is a module-level global shared across the whole test session), right after the
existing `_temp_db` fixture:

```python
@pytest.fixture(autouse=True)
def _clean_cooldown_cache():
    cooldown_config.reset_override_cache()
    yield
    cooldown_config.reset_override_cache()
```

Add the import at the top: `from app.queue import cooldown_config`.

- [ ] **Step 7: Run the full store test file to verify everything passes**

Run: `uv run pytest tests/test_queue_store.py -v`
Expected: PASS (all tests, including the pre-existing ones)

- [ ] **Step 8: Commit**

```bash
git add app/queue/cooldown_config.py app/queue/store.py tests/test_cooldown_config.py tests/test_queue_store.py
git commit -m "feat: read effective cooldown through a fail-safe DB-override cache"
```

---

### Task 4: Dispatcher refresh + fail-safe test

**Files:**
- Modify: `app/queue/dispatcher.py:125-145` (`process_next_due`)
- Test: `tests/test_dispatcher.py`

**Interfaces:**
- Consumes: `store.get_cooldown_overrides()` (Task 2), `cooldown_config.set_override_cache`/`reset_override_cache` (Task 3).
- Produces: nothing new for later tasks — this closes the loop so `store.effective_cooldown` (already called later in the same function, line ~267) sees a freshly refreshed cache.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_dispatcher.py`, right after
`test_claim_falls_back_to_env_when_the_override_read_fails`:

```python
async def test_claimed_ticket_uses_the_db_cooldown_override(monkeypatch):
    """The behavioral guarantee: a mid-session cooldown override changes the
    next scheduled re-review, with no restart and no redeploy."""
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_max_seconds", 3600.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_factor", 2.0)
    store.set_cooldown_override(base=30.0, cap=600.0, factor=1.5, now=NOW.isoformat())

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)
    _enqueue(1)
    await dispatcher.process_next_due(NOW)
    t = store.get_ticket(1)
    expected = NOW + timedelta(seconds=30.0)  # level 0 -> base override, not env 300s
    assert t.not_before == expected.isoformat()


async def test_claim_falls_back_to_env_cooldown_when_the_override_read_fails(monkeypatch):
    """Fail-safe: an unreachable cooldown override must degrade to the
    configured env defaults, never abort the review, and never keep serving a
    stale cached override from a previous successful refresh."""
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_max_seconds", 3600.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_factor", 2.0)
    # A prior successful refresh cached a DIFFERENT base. If the failure
    # handler merely logged and left the cache alone, effective_cooldown would
    # keep using 30.0 forever -- this is what catches that.
    cooldown_config.set_override_cache(30.0, 600.0, 1.5)

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "get_cooldown_overrides", boom)

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)
    _enqueue(1)
    result = await dispatcher.process_next_due(NOW)
    t = store.get_ticket(1)
    expected = NOW + timedelta(seconds=300.0)  # env default, not the stale 30.0
    assert t.not_before == expected.isoformat()
    assert result.action == "ran"
```

Check the top of `tests/test_dispatcher.py` for the existing `_clean_cache`
autouse fixture (resets `active.reset_override_cache()`) and add the cooldown
equivalent alongside it:

```python
@pytest.fixture(autouse=True)
def _clean_cooldown_cache():
    cooldown_config.reset_override_cache()
    yield
    cooldown_config.reset_override_cache()
```

Add `from app.queue import cooldown_config` to the imports.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_dispatcher.py -k cooldown_override -v`
Expected: FAIL — the override is never refreshed, so both assertions see the
env-default `not_before` (300s) instead of the expected 30s in the first test.

- [ ] **Step 3: Add the refresh to `process_next_due`**

In `app/queue/dispatcher.py`, immediately after the existing provider-override
refresh block (the `try`/`except` ending at `active.set_override_cache(None)`,
around line 144), add:

```python
    # Refresh the cooldown override once per claimed ticket, same cadence and
    # fail-safe shape as the provider-override refresh above: a failure here
    # must never abort a review, and must never leave a stale cached override
    # in place -- degrade all the way to the env defaults.
    try:
        base, cap, factor = await asyncio.to_thread(store.get_cooldown_overrides)
        cooldown_config.set_override_cache(base, cap, factor)
    except Exception:  # noqa: BLE001
        logger.exception("failed to refresh the cooldown override; using env defaults")
        cooldown_config.reset_override_cache()
```

Add the import at the top of `app/queue/dispatcher.py` (alongside
`from app.queue import store`):

```python
from app.queue import cooldown_config
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_dispatcher.py -v`
Expected: PASS (all tests, including pre-existing ones)

- [ ] **Step 5: Commit**

```bash
git add app/queue/dispatcher.py tests/test_dispatcher.py
git commit -m "feat: refresh the cooldown override once per claimed ticket"
```

---

### Task 5: `scripts/set_cooldown.py` CLI

**Files:**
- Create: `scripts/set_cooldown.py`
- Test: `tests/test_set_cooldown_script.py` (new)

**Interfaces:**
- Consumes: `store.get_cooldown_overrides`/`set_cooldown_override` (Task 2), `scripts/_render.py`'s `find_service_id`/`env_vars` (existing, unchanged).
- Produces: `set_cooldown.main(argv: list[str] | None) -> int`, `set_cooldown.build_parser() -> argparse.ArgumentParser` — CLI entry points, not consumed by any other task.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_set_cooldown_script.py`:

```python
"""The operator CLI that sets the DB cooldown override. Uses the shared
Postgres test harness -- it writes to the same table the service reads.
Mirrors tests/test_set_provider_script.py's shape, minus the credential
checks: there is no secret at stake here, only "does this write reach the
database production actually reads" -- which degrades to a warning, never a
refusal, so there is no --force flag."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import respx

from app.config import settings
from app.queue import store
from scripts import set_cooldown

_REPO_ROOT = Path(__file__).resolve().parent.parent

RENDER_SERVICES = "https://api.render.com/v1/services"


def _service_list(service_id="srv-1", name="pr-review-engine"):
    return [{"service": {"id": service_id, "name": name}}]


def _env_var_list(values: dict):
    return [{"envVar": {"key": k, "value": v}} for k, v in values.items()]


@pytest.fixture(autouse=True)
def _temp_db(db):
    yield


def test_sets_base_only_leaves_others_untouched():
    assert set_cooldown.main(["--base", "30"]) == 0
    assert store.get_cooldown_overrides() == (30.0, None, None)


def test_sets_all_three():
    assert set_cooldown.main(["--base", "30", "--cap", "600", "--factor", "1.5"]) == 0
    assert store.get_cooldown_overrides() == (30.0, 600.0, 1.5)


def test_a_second_call_with_one_flag_preserves_the_others_read_modify_write():
    set_cooldown.main(["--base", "30", "--cap", "600", "--factor", "1.5"])
    assert set_cooldown.main(["--factor", "3.0"]) == 0
    assert store.get_cooldown_overrides() == (30.0, 600.0, 3.0)


def test_clear_resets_all_three():
    set_cooldown.main(["--base", "30", "--cap", "600", "--factor", "1.5"])
    assert set_cooldown.main(["--clear"]) == 0
    assert store.get_cooldown_overrides() == (None, None, None)


def test_rejects_a_factor_below_one(capsys):
    assert set_cooldown.main(["--factor", "0.5"]) == 2
    assert "factor" in capsys.readouterr().err.lower()
    assert store.get_cooldown_overrides() == (None, None, None)


def test_requires_at_least_one_flag_or_clear(capsys):
    assert set_cooldown.main([]) == 2
    assert capsys.readouterr().err


def test_rejects_an_abbreviated_flag(capsys):
    """allow_abbrev=False, matching scripts/set_provider.py's guard."""
    with pytest.raises(SystemExit) as exc:
        set_cooldown.main(["--cle"])
    assert exc.value.code == 2
    assert "--cle" in capsys.readouterr().err


def test_entry_point_runs_as_a_documented_module_invocation():
    result = subprocess.run(
        [sys.executable, "-m", "scripts.set_cooldown", "--help"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_sets_the_override_without_a_render_api_key(capsys):
    assert set_cooldown.main(["--base", "30"]) == 0
    assert store.get_cooldown_overrides() == (30.0, None, None)
    assert "could not verify against Render" in capsys.readouterr().out


def test_degrades_to_a_warning_when_no_service_matches_the_configured_name(
    monkeypatch, capsys
):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "no-such-service")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        code = set_cooldown.main(["--base", "30"])
    out = capsys.readouterr().out
    assert code == 0
    assert store.get_cooldown_overrides() == (30.0, None, None)
    assert "no service named" in out


def test_warns_but_proceeds_when_local_database_url_does_not_match_render(
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
        code = set_cooldown.main(["--base", "30"])
    assert code == 0
    assert store.get_cooldown_overrides() == (30.0, None, None)
    assert "could not confirm this DATABASE_URL" in capsys.readouterr().out


def test_confirms_when_database_url_matches_render(monkeypatch, db_url, capsys):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(return_value=httpx.Response(200, json=_service_list()))
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"DATABASE_URL": db_url}))
        )
        code = set_cooldown.main(["--base", "30"])
    assert code == 0
    assert "verified" in capsys.readouterr().out.lower()


def test_clear_never_calls_the_render_verification(monkeypatch):
    def _boom():
        raise AssertionError("must not verify on --clear")

    monkeypatch.setattr(set_cooldown, "_verify_render_reachability", _boom)
    assert set_cooldown.main(["--clear"]) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_set_cooldown_script.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.set_cooldown'`

- [ ] **Step 3: Create `scripts/set_cooldown.py`**

```python
"""Set or clear the DB-backed cooldown override (base/cap/factor).

    uv run python -m scripts.set_cooldown --base 30 --factor 1.5
    uv run python -m scripts.set_cooldown --cap 600
    uv run python -m scripts.set_cooldown --clear

The override takes effect on the next claimed ticket -- no restart, no
redeploy. It writes to whatever DATABASE_URL points at, so against a local
.env this sets a LOCAL override and nothing reaches production.

Unlike scripts/set_provider.py, there is no credential at stake here -- only
numbers. Before writing, this checks (when RENDER_API_KEY is set) whether the
local DATABASE_URL matches the live Render service's, purely as an
informational signal that the write will actually reach production; it never
refuses the write, so there is no --force flag.

A plain tool, not a slash command -- matches scripts/set_provider.py.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from app.config import settings
from app.queue import store
from scripts import _render


def _verify_render_reachability() -> str:
    """A human-readable status line about whether this write reaches the
    Render-hosted production database. Never blocks the write -- see the
    module docstring. Never returns, prints, or logs a fetched Render value,
    only presence/absence and in-memory equality results (matches
    set_provider.py's credential-leak guard, applied here to DATABASE_URL)."""
    if not settings.render_api_key:
        return "could not verify against Render (no RENDER_API_KEY); writing without live verification"
    try:
        service_id = _render.find_service_id()
        if service_id is None:
            return (
                f"could not verify against Render (no service named "
                f"{settings.render_service_name}); writing without live verification"
            )
        env_vars = _render.env_vars(service_id)
    # deliberate: inability to verify degrades to a warning, never a refusal
    except Exception as exc:  # noqa: BLE001
        return f"could not verify against Render ({type(exc).__name__}); writing without live verification"

    if env_vars.get("DATABASE_URL") != settings.database_url:
        return "could not confirm this DATABASE_URL is the one the Render service reads -- writing anyway"
    return "DATABASE_URL verified against the live Render service"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="set_cooldown",
        # Without this, argparse treats a truncated flag like --cle as an
        # abbreviation of --clear and runs it -- scripts/set_provider.py
        # carries the same guard after an identical abbreviation match fired
        # a live production incident on a different script.
        allow_abbrev=False,
        description="Set or clear the DB-backed re-review cooldown override (base/cap/factor).",
    )
    parser.add_argument("--base", type=float, help="base cooldown in seconds")
    parser.add_argument("--cap", type=float, help="cooldown cap in seconds")
    parser.add_argument("--factor", type=float, help="escalation factor (must be >= 1.0)")
    parser.add_argument(
        "--clear", action="store_true", help="remove all three overrides; fall back to env vars"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if not args.clear and args.base is None and args.cap is None and args.factor is None:
        print("at least one of --base/--cap/--factor is required (or --clear)", file=sys.stderr)
        return 2
    if args.factor is not None and args.factor < 1.0:
        print(f"--factor must be >= 1.0 (got {args.factor})", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc).isoformat()
    store.init_pool()

    if args.clear:
        store.set_cooldown_override(base=None, cap=None, factor=None, now=now)
        print("cleared; falling back to the env-configured cooldown defaults")
        return 0

    print(_verify_render_reachability())
    current_base, current_cap, current_factor = store.get_cooldown_overrides()
    new_base = args.base if args.base is not None else current_base
    new_cap = args.cap if args.cap is not None else current_cap
    new_factor = args.factor if args.factor is not None else current_factor
    store.set_cooldown_override(base=new_base, cap=new_cap, factor=new_factor, now=now)
    print(
        f"cooldown override: base {current_base} -> {new_base}, "
        f"cap {current_cap} -> {new_cap}, factor {current_factor} -> {new_factor}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_set_cooldown_script.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/set_cooldown.py tests/test_set_cooldown_script.py
git commit -m "feat: add scripts/set_cooldown.py for live cooldown tuning"
```

---

### Task 6: `SPEC.md` documentation update

**Files:**
- Modify: `SPEC.md:375-390`

**Interfaces:**
- Consumes: nothing (docs only).
- Produces: nothing (docs only).

- [ ] **Step 1: Update the cooldown paragraph**

In `SPEC.md`, replace the paragraph beginning "This cooldown now **escalates**
per PR" (lines 381-390) with:

```markdown
This cooldown now **escalates** per PR — a `cooldown_level` raises the
effective wait geometrically (`effective_cooldown(level) = min(base·factor^level, cap)`)
for a PR that keeps being pushed inside each window, resetting to 0 once the PR
stays quiet for a full window. Level 0 equals the base cooldown, so normal PRs
are unchanged; escalation only lengthens `not_before` (the schedule notice's
ETA reflects it automatically); at the defaults (factor 2, cap 3600s) it bounds
a churning PR from ~288 to ~26 reviews/day without ever abandoning it. The two
escalation sites are: (1) `enqueue_or_update` done/failed re-arm, and
(2) `finalize_review`'s dirty-flag branch.

**Tuning base/cap/factor.** All three are env vars
(`DISPATCHER_REREVIEW_COOLDOWN_SECONDS` default 300s,
`DISPATCHER_REREVIEW_COOLDOWN_MAX_SECONDS` default 3600s,
`DISPATCHER_REREVIEW_COOLDOWN_FACTOR` default 2.0, must be `>= 1.0`), declared
in `render.yaml` and editable in the Render dashboard (redeploys on change).
For live tuning without a redeploy — e.g. shrinking the base to a few seconds
for a demo — `scripts/set_cooldown.py` writes a DB-backed override to the same
`runtime_config` singleton row the LLM-provider override already uses
(`scripts/set_provider.py`); it takes effect on the next claimed ticket. A
read of the override that comes back invalid (`factor < 1.0`, or `base > cap`)
is discarded as a whole triple and falls back to the env defaults —
`app/queue/cooldown_config.py` mirrors `app/providers/active.py`'s fail-safe
cache pattern.
```

- [ ] **Step 2: Commit**

```bash
git add SPEC.md
git commit -m "docs: document the cooldown factor + live-tuning override"
```

---

## Final Verification

- [ ] **Run the full test suite**

Run: `uv run pytest -v`
Expected: PASS, no regressions in any existing test file (particularly
`tests/test_queue_store.py`, `tests/test_dispatcher.py`, `tests/test_provider_override.py`).

- [ ] **Run lint**

Run: `uv run ruff check .`
Expected: no new violations.

- [ ] **Manual smoke test against a local Postgres**

```bash
uv run python -m scripts.set_cooldown --base 5 --cap 20 --factor 1.5
uv run python -m scripts.set_cooldown --clear
```

Expected: both exit 0 and print the override change / clear confirmation with
no traceback (no `RENDER_API_KEY` needed locally — the reachability check
degrades to a printed warning).
