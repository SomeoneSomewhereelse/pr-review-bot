# Onboarding server-side session persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the onboarding wizard's stateless-relay architecture with a
cookie-identified, server-side session (backed by a new dedicated Postgres),
so wizard progress survives a mobile browser destroying `sessionStorage`
mid-flow, and rework the Render env-var push from four incremental calls
into a single bulk push in the final frame.

**Architecture:** A new `onboarding/session_store.py` module (psycopg3 +
`ConnectionPool`, mirroring `bot/queue/store.py`'s pattern) backs one table,
`wizard_sessions`, keyed by a random id delivered via an `HttpOnly`/`Secure`/
`SameSite=Lax` cookie. Each frame's data is Fernet-encrypted as one opaque
blob per frame inside `frame_data` JSONB. Every existing relay endpoint
gains a session read/write on success; the four existing per-frame
`push-render-vars`-style endpoints are deleted and replaced by one
`POST /api/render/bulk-push-env-vars` called from the final frame, which
assembles every var from the session and pushes once.

**Tech Stack:** FastAPI, `psycopg[binary]`, `psycopg_pool`, `cryptography`
(Fernet), pytest, vanilla JS (no new frontend deps).

**Spec:** `docs/superpowers/specs/2026-09-01-onboarding-server-side-session-design.md`

## Global Constraints

- No new background tasks/cron — expiry is swept lazily (spec section 4).
- `onboarding/` still never imports from `bot/`'s credential-handling code
  (onboarding/CLAUDE.md's no-shared-credential-path rule) — `session_store.py`
  mirrors `bot/queue/store.py`'s *pattern*, it does not import it.
- Every relay endpoint still returns a verdict, never a raw credential, to
  the browser (root CLAUDE.md secret-handling + onboarding/CLAUDE.md).
- `sessionStorage` may still hold non-secret UI state (theme, language,
  which frame is expanded) but no credential value, after this plan.
- `update_frame(session_id, frame, data)` is a **shallow merge** into that
  frame's existing dict, not a replace — later tasks depend on this (e.g.
  the "render" frame accumulates `api_key`, then `service_id`/`service_url`,
  then `pending_deploy_id` across three different endpoints).
- Session TTL is 4 hours, a constant in `session_store.py` — not an env var.
- Cookie name: `onboarding_session`.

## Frame-data key reference (used throughout)

| Frame (UI id) | `frame_data` key | Fields written, and by which endpoint |
|---|---|---|
| render-key | `render` | `api_key`, `owner_name` (validate-key) → `+service_id`, `+service_url` (create-service) → `+pending_deploy_id` (trigger-deploy) |
| dashboard-auth | `dashboard_auth` | `username`, `password`, `session_secret` (new `/api/dashboard-auth/confirm`) |
| github-app | `github_app` | `app_id`, `private_key_b64`, `webhook_secret`, `installation_id` (validate-app, only when `all_ok`) |
| supabase | `supabase` | `access_token`, `refresh_token` (OAuth callback) → `+database_url` (connection-info) → `_pending_oauth: {state, verifier}` transiently during the redirect, cleared on completion |
| llm-provider | `llm_provider` | `provider`, `credential_value`, `model` (new `/api/llm/confirm`) |
| uptime-pinger | `uptime_pinger` | `monitor_id` (create-monitor) |

---

### Task 1: Add new dependencies

**Files:**
- Modify: `onboarding/pyproject.toml`

**Interfaces:**
- Produces: `psycopg`, `psycopg_pool`, `cryptography` importable from `onboarding/`.

- [ ] **Step 1: Add dependencies**

Add to `onboarding/pyproject.toml`'s `dependencies` list (matching the
version floors `bot/pyproject.toml` already uses for the first two):
```toml
"psycopg[binary]>=3.2",
"psycopg-pool>=3.2",
"cryptography>=43",
```

- [ ] **Step 2: Sync and verify**

Run: `uv sync --package onboarding`
Expected: resolves cleanly, no version conflicts.

- [ ] **Step 3: Commit**

```bash
git add onboarding/pyproject.toml uv.lock
git commit -m "onboarding: add psycopg3, psycopg-pool, cryptography deps"
```

---

### Task 2: `session_store.py` — the session store module

**Files:**
- Create: `onboarding/session_store.py`
- Test: `onboarding/tests/test_onboarding_session_store.py`

**Interfaces:**
- Consumes: `onboarding.config.settings.database_url`,
  `settings.onboarding_session_encryption_key` (added in Task 3 — write this
  task assuming those attributes exist; Task 3 adds them before this task's
  tests can pass end-to-end, but the module itself only references
  `settings.<attr>`, so write both together and run tests after Task 3 if
  doing them out of order is more convenient. Recommended: do Task 3 first
  in execution even though it's listed second in the plan for narrative
  reasons — the executor should reorder Tasks 2 and 3 if that's simpler;
  neither depends on the other's tests passing, only on the attribute
  existing.)
- Produces (used by every later task):
  - `SESSION_TTL: timedelta` — the TTL constant, for the cookie's `max_age`.
  - `init_pool() -> None`, `close_pool() -> None`
  - `create_session() -> str`
  - `@dataclass(frozen=True) class SessionData: frames: dict[str, dict]`
  - `get_session(session_id: str) -> SessionData | None`
  - `@dataclass(frozen=True) class SessionNotFound: pass`
  - `update_frame(session_id: str, frame: str, data: dict) -> SessionNotFound | None`
  - `read_frame(session_id: str, frame: str) -> dict | None`
  - `delete_session(session_id: str) -> None`

- [ ] **Step 1: Write the module**

```python
"""onboarding/session_store.py — the wizard's server-side session store.

Replaces the stateless-relay invariant onboarding/CLAUDE.md used to state:
mobile browsers were found to destroy sessionStorage (and the browsing
context holding it) mid-flow, so wizard progress now lives here instead,
identified by a cookie rather than anything tab-scoped. See
docs/superpowers/specs/2026-09-01-onboarding-server-side-session-design.md.

Each frame's data is stored as ONE Fernet-encrypted blob per frame (not
field-by-field) inside the `frame_data` JSONB column -- simpler than
classifying which individual fields are secrets per frame, and every value
inside is encrypted either way since the whole container is. Only
get_session()/the router's own display-field allowlist ever decrypts a
frame's contents back out.

Sync functions, run via asyncio.to_thread by callers -- same convention as
bot/queue/store.py, so Postgres network latency never blocks the event
loop. This module never imports from bot/ (onboarding/CLAUDE.md's no-
shared-credential-path rule); it mirrors bot/queue/store.py's pattern, not
its code.
"""
from __future__ import annotations

import dataclasses
import json
import secrets
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet, InvalidToken
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolTimeout

from onboarding.config import settings

SESSION_TTL = timedelta(hours=4)
_POOL_TIMEOUT_SECONDS = 10

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wizard_sessions (
    id TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    frame_data JSONB NOT NULL DEFAULT '{}'::jsonb
);
"""

_pool: ConnectionPool | None = None


def _configure(conn) -> None:
    conn.row_factory = dict_row


def init_pool() -> None:
    """Open the connection pool (if not already) and ensure the schema.
    Idempotent. Fails loudly (RuntimeError, never a bare PoolTimeout) on an
    unreachable database, matching bot/queue/store.py's init_pool()
    convention -- this project's services fail startup loudly rather than
    limping along without their datastore. Never includes
    settings.database_url in the error, which carries the password."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings.database_url,
            min_size=1,
            max_size=4,
            timeout=_POOL_TIMEOUT_SECONDS,
            configure=_configure,
            open=True,
        )
    try:
        with _pool.connection() as conn:
            conn.execute(_SCHEMA)
    except PoolTimeout as exc:
        raise RuntimeError(
            f"Could not reach the onboarding session database within "
            f"{_POOL_TIMEOUT_SECONDS}s. Check DATABASE_URL is set to a "
            "reachable Postgres connection string."
        ) from exc


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def _require_pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("session_store.init_pool() has not been called")
    return _pool


def _fernet() -> Fernet:
    return Fernet(settings.onboarding_session_encryption_key.encode("ascii"))


def _encrypt_frame(data: dict) -> str:
    return _fernet().encrypt(json.dumps(data).encode("utf-8")).decode("ascii")


def _decrypt_frame(token: str) -> dict | None:
    """None (never raises) for a token that fails to decrypt or parse -- a
    rotated encryption key or corrupted row must make that one frame look
    not-yet-complete, not crash the caller."""
    try:
        raw = _fernet().decrypt(token.encode("ascii"))
        return json.loads(raw)
    except (InvalidToken, ValueError, TypeError):
        return None


def create_session() -> str:
    """The ONLY function in this module allowed to mint a new session id --
    every other write path (update_frame) requires an existing session and
    fails closed instead of creating one. This is what prevents a dropped
    cookie, a request race, or a stale cookie pointing at an expired row
    from silently forking or resurrecting a session (spec section 3.2).
    Sweeps expired rows before inserting."""
    session_id = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    with _require_pool().connection() as conn:
        conn.execute("DELETE FROM wizard_sessions WHERE expires_at < now()")
        conn.execute(
            "INSERT INTO wizard_sessions (id, created_at, expires_at, frame_data) "
            "VALUES (%s, %s, %s, %s)",
            (session_id, now, now + SESSION_TTL, Jsonb({})),
        )
    return session_id


@dataclasses.dataclass(frozen=True)
class SessionData:
    frames: dict[str, dict]


def get_session(session_id: str) -> SessionData | None:
    """None for a missing OR expired row -- an expired row is deleted here,
    not just skipped, so callers never distinguish the two cases (both mean
    "treat this visitor as fresh"). A frame whose blob fails to decrypt is
    silently omitted from the returned frames rather than raising -- that
    frame reads as not-yet-complete, same as if it were never set."""
    with _require_pool().connection() as conn:
        row = conn.execute(
            "SELECT frame_data, expires_at FROM wizard_sessions WHERE id = %s",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] < datetime.now(timezone.utc):
            conn.execute("DELETE FROM wizard_sessions WHERE id = %s", (session_id,))
            return None
        frames: dict[str, dict] = {}
        for frame, token in row["frame_data"].items():
            decrypted = _decrypt_frame(token)
            if decrypted is not None:
                frames[frame] = decrypted
        return SessionData(frames=frames)


@dataclasses.dataclass(frozen=True)
class SessionNotFound:
    pass


def update_frame(session_id: str, frame: str, data: dict) -> SessionNotFound | None:
    """Shallow-merges `data` into the session's existing dict for `frame`
    (creating that frame's entry on its first write), then re-encrypts the
    whole frame as one blob. Requires an existing, non-expired session --
    see create_session()'s docstring for why this must never upsert.

    Not fully race-safe against two concurrent updates to the SAME frame
    (read-then-write, not a single atomic statement) -- acceptable given
    this is a single visitor's own sequential wizard flow, not a resource
    under real concurrent access; only cross-frame writes need the atomicity
    the JSONB `||` merge below provides, which is what protects other
    frames' data from being clobbered by this write.
    """
    existing = get_session(session_id)
    if existing is None:
        return SessionNotFound()
    merged = {**existing.frames.get(frame, {}), **data}
    token = _encrypt_frame(merged)
    with _require_pool().connection() as conn:
        conn.execute(
            "UPDATE wizard_sessions SET frame_data = frame_data || %s::jsonb WHERE id = %s",
            (Jsonb({frame: token}), session_id),
        )
    return None


def read_frame(session_id: str, frame: str) -> dict | None:
    """Decrypts and returns one frame's stored data, or None if the session
    or that frame's data doesn't exist."""
    session = get_session(session_id)
    if session is None:
        return None
    return session.frames.get(frame)


def delete_session(session_id: str) -> None:
    """No-op if the id doesn't exist."""
    with _require_pool().connection() as conn:
        conn.execute("DELETE FROM wizard_sessions WHERE id = %s", (session_id,))
```

- [ ] **Step 2: Write the test file**

Mock at the `psycopg` connection boundary the same way
`bot/tests/test_store_init.py` / the fixture `bot/tests/conftest.py` uses
for `bot/queue/store.py` — **read that fixture first** and reuse its shape
(a fake `ConnectionPool`/connection context manager backed by an in-memory
dict, or a real throwaway local Postgres if that's what the existing
fixture already sets up for `bot/`'s tests) rather than inventing a new
mocking approach. Set `settings.database_url` and
`settings.onboarding_session_encryption_key` (a real
`Fernet.generate_key()` value) in the test's setup.

```python
"""Tests for onboarding.session_store -- see
docs/superpowers/specs/2026-09-01-onboarding-server-side-session-design.md
section 5 for what this module must guarantee."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

from onboarding import session_store
from onboarding.config import settings


@pytest.fixture(autouse=True)
def _configure_settings(monkeypatch):
    monkeypatch.setattr(settings, "onboarding_session_encryption_key", Fernet.generate_key().decode())
    yield


# NOTE: the executor must adapt the pool setup/teardown below to match
# whatever fixture bot/tests already uses to give bot/queue/store.py's tests
# a real (test) Postgres connection -- init_pool()/close_pool() here need
# the same treatment.
@pytest.fixture(autouse=True)
def _pool():
    session_store.init_pool()
    yield
    with session_store._require_pool().connection() as conn:
        conn.execute("DELETE FROM wizard_sessions")
    session_store.close_pool()


def test_create_session_mints_a_fresh_id_each_call():
    a = session_store.create_session()
    b = session_store.create_session()
    assert a != b


def test_get_session_returns_none_for_an_unknown_id():
    assert session_store.get_session("nonexistent") is None


def test_get_session_returns_empty_frames_for_a_fresh_session():
    session_id = session_store.create_session()
    result = session_store.get_session(session_id)
    assert result.frames == {}


def test_update_frame_then_read_frame_round_trips():
    session_id = session_store.create_session()
    result = session_store.update_frame(session_id, "render", {"api_key": "rnd_abc"})
    assert result is None
    assert session_store.read_frame(session_id, "render") == {"api_key": "rnd_abc"}


def test_update_frame_merges_rather_than_replaces():
    session_id = session_store.create_session()
    session_store.update_frame(session_id, "render", {"api_key": "rnd_abc"})
    session_store.update_frame(session_id, "render", {"service_id": "srv-1"})
    assert session_store.read_frame(session_id, "render") == {
        "api_key": "rnd_abc", "service_id": "srv-1",
    }


def test_update_frame_does_not_clobber_a_different_frame():
    session_id = session_store.create_session()
    session_store.update_frame(session_id, "render", {"api_key": "rnd_abc"})
    session_store.update_frame(session_id, "github_app", {"app_id": 1})
    assert session_store.read_frame(session_id, "render") == {"api_key": "rnd_abc"}
    assert session_store.read_frame(session_id, "github_app") == {"app_id": 1}


def test_update_frame_against_a_missing_session_id_fails_closed():
    result = session_store.update_frame("nonexistent", "render", {"api_key": "x"})
    assert isinstance(result, session_store.SessionNotFound)


def test_update_frame_against_a_deleted_session_does_not_resurrect_it():
    """The fork-risk scenario raised during design review: two successive
    update_frame calls against a session id that was deleted between them
    must not silently recreate a session containing only the second call's
    data."""
    session_id = session_store.create_session()
    session_store.update_frame(session_id, "render", {"api_key": "rnd_abc"})
    session_store.delete_session(session_id)
    result = session_store.update_frame(session_id, "render", {"api_key": "rnd_xyz"})
    assert isinstance(result, session_store.SessionNotFound)
    assert session_store.get_session(session_id) is None


def test_read_frame_returns_none_for_a_frame_never_written():
    session_id = session_store.create_session()
    assert session_store.read_frame(session_id, "render") is None


def test_delete_session_is_a_noop_against_a_missing_id():
    session_store.delete_session("nonexistent")  # must not raise


def test_get_session_treats_an_expired_row_as_missing_and_sweeps_it():
    session_id = session_store.create_session()
    with session_store._require_pool().connection() as conn:
        conn.execute(
            "UPDATE wizard_sessions SET expires_at = %s WHERE id = %s",
            (datetime.now(timezone.utc) - timedelta(seconds=1), session_id),
        )
    assert session_store.get_session(session_id) is None
    with session_store._require_pool().connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM wizard_sessions WHERE id = %s", (session_id,)
        ).fetchone()
    assert row is None


def test_a_frame_that_fails_to_decrypt_is_omitted_not_raised():
    """Simulates a key rotation: a frame written under one Fernet key is
    unreadable under another, and must read as absent, not crash."""
    session_id = session_store.create_session()
    session_store.update_frame(session_id, "render", {"api_key": "rnd_abc"})
    settings.onboarding_session_encryption_key = Fernet.generate_key().decode()
    result = session_store.get_session(session_id)
    assert result.frames == {}


def test_raw_row_content_never_contains_the_plaintext_value():
    session_id = session_store.create_session()
    session_store.update_frame(session_id, "render", {"api_key": "rnd_super_secret_value"})
    with session_store._require_pool().connection() as conn:
        row = conn.execute(
            "SELECT frame_data FROM wizard_sessions WHERE id = %s", (session_id,)
        ).fetchone()
    assert "rnd_super_secret_value" not in str(row["frame_data"])
```

- [ ] **Step 3: Run the tests**

Run: `uv run pytest onboarding/tests/test_onboarding_session_store.py -v`
Expected: all PASS (after Task 3 adds the `database_url` /
`onboarding_session_encryption_key` settings attributes this module reads).

- [ ] **Step 4: Commit**

```bash
git add onboarding/session_store.py onboarding/tests/test_onboarding_session_store.py
git commit -m "onboarding: add session_store.py, the wizard's new session persistence layer"
```

---

### Task 3: `config.py` — new settings

**Files:**
- Modify: `onboarding/config.py`
- Test: `onboarding/tests/test_onboarding_config.py` (create if it doesn't exist; check first)

**Interfaces:**
- Produces: `settings.database_url: str`, `settings.onboarding_session_encryption_key: str`

- [ ] **Step 1: Check for an existing config test file**

Run: `ls onboarding/tests/test_onboarding_config.py 2>/dev/null || echo "none"`
If one exists, add to it instead of creating a new file; follow its
existing style.

- [ ] **Step 2: Add the settings**

**Correction found during execution:** a pydantic `field_validator` that
raises on a malformed value is unsafe for this specific field. Pydantic's
`ValidationError.__str__` embeds the rejected `input_value` verbatim
regardless of the validator's own message — exactly the leak root
`CLAUDE.md`'s secret-handling section warns about. `supabase_oauth_client_id`
above gets away with this because it isn't a secret; this key is. So:
only whitespace-normalize it here, unvalidated; format-validity is checked
in Task 4's lifespan instead, via a plain `RuntimeError` with a hand-written
message.

In `onboarding/config.py`, add near the existing Supabase OAuth settings:

```python
# ... inside class Settings ...

    # The wizard's own dedicated Postgres (never bot/'s queue DB, never a
    # visitor's provisioned project) backing session_store.py. See
    # docs/superpowers/specs/2026-09-01-onboarding-server-side-session-design.md.
    database_url: str = ""

    # A Fernet key encrypting every credential value session_store.py
    # writes. Only whitespace-normalized here, deliberately NOT format-
    # validated via a field_validator -- see the correction note above.
    # Format validity is checked in main.py's lifespan instead.
    onboarding_session_encryption_key: str = ""

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        return value.strip()

    @field_validator("onboarding_session_encryption_key")
    @classmethod
    def _normalize_session_encryption_key(cls, value: str) -> str:
        return value.strip()
```

- [ ] **Step 3: Write/add tests**

```python
def test_database_url_strips_whitespace(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "  postgresql://x  ")
    from onboarding.config import Settings
    assert Settings().database_url == "postgresql://x"


def test_session_encryption_key_reads_from_environment_unvalidated(monkeypatch):
    """Format validity is deliberately not checked here -- see Task 4's
    lifespan test for that."""
    monkeypatch.setenv("ONBOARDING_SESSION_ENCRYPTION_KEY", "not-a-fernet-key")
    from onboarding.config import Settings
    assert Settings().onboarding_session_encryption_key == "not-a-fernet-key"


def test_session_encryption_key_whitespace_only_value_normalizes_to_the_unset_sentinel(monkeypatch):
    monkeypatch.setenv("ONBOARDING_SESSION_ENCRYPTION_KEY", "   ")
    from onboarding.config import Settings
    assert Settings().onboarding_session_encryption_key == ""
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest onboarding/tests/test_onboarding_config.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add onboarding/config.py onboarding/tests/test_onboarding_config.py
git commit -m "onboarding: add DATABASE_URL and ONBOARDING_SESSION_ENCRYPTION_KEY settings"
```

---

### Task 4: `main.py` — lifespan wiring

**Files:**
- Modify: `onboarding/main.py`
- Test: `onboarding/tests/test_onboarding_main.py` (extend existing)

**Interfaces:**
- Consumes: `session_store.init_pool`, `session_store.close_pool`,
  `settings.database_url`, `settings.onboarding_session_encryption_key`.

- [ ] **Step 1: Update the lifespan**

```python
from onboarding import session_store
# ...

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.supabase_oauth_client_id:
        raise RuntimeError(...)  # unchanged
    if not settings.supabase_oauth_client_secret:
        raise RuntimeError(...)  # unchanged
    if not settings.database_url:
        raise RuntimeError(
            "DATABASE_URL is unset — refusing to start. The wizard's "
            "server-side session store needs its own dedicated Postgres "
            "connection string; see onboarding/.env.example."
        )
    if not settings.onboarding_session_encryption_key:
        raise RuntimeError(
            "ONBOARDING_SESSION_ENCRYPTION_KEY is unset — refusing to "
            "start. Needed to encrypt every credential value the session "
            "store writes; see onboarding/.env.example."
        )
    try:
        Fernet(settings.onboarding_session_encryption_key.encode("ascii"))
    except Exception:
        # A plain, hand-written RuntimeError -- NOT a re-raise/chain of the
        # underlying cryptography exception, and never includes the key
        # itself. See config.py's field docstring: a pydantic-level
        # ValidationError was rejected for this exact reason (it echoes
        # input_value regardless of message), and chaining the raw
        # exception here would risk the same class of leak if its own
        # str() ever changes to include more context.
        raise RuntimeError(
            "ONBOARDING_SESSION_ENCRYPTION_KEY is not a valid Fernet key — "
            "refusing to start. Generate one with: python -c \"from "
            "cryptography.fernet import Fernet; print(Fernet.generate_key()"
            '.decode())"'
        ) from None
    session_store.init_pool()
    yield
    session_store.close_pool()
```

`from cryptography.fernet import Fernet` needs importing in `main.py` for
this check.

Also update the module docstring at the top of `main.py` — it currently
says "Stateless relay only — no database, no session store." That's no
longer true; replace with a line pointing at the new spec doc instead.

- [ ] **Step 2: Extend the lifespan tests**

Read `onboarding/tests/test_onboarding_main.py` first for its existing
fixture shape (it already tests the two Supabase-OAuth-unset cases), then
add matching tests for the two new unset cases and the happy path calling
`init_pool`/`close_pool` (mock `session_store.init_pool`/`close_pool` at
the boundary — no real DB needed for this test file).

```python
def test_lifespan_raises_if_database_url_unset(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(settings, "onboarding_session_encryption_key", "somekey")
    # ... same async-context-manager-entry pattern the existing two tests use ...
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        ...


def test_lifespan_raises_if_session_encryption_key_unset(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "postgresql://x")
    monkeypatch.setattr(settings, "onboarding_session_encryption_key", "")
    with pytest.raises(RuntimeError, match="ONBOARDING_SESSION_ENCRYPTION_KEY"):
        ...


def test_lifespan_raises_if_session_encryption_key_malformed(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "postgresql://x")
    monkeypatch.setattr(settings, "onboarding_session_encryption_key", "not-a-fernet-key")
    with pytest.raises(RuntimeError, match="not a valid Fernet key"):
        ...


def test_lifespan_malformed_key_error_never_echoes_the_value(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "postgresql://x")
    monkeypatch.setattr(settings, "onboarding_session_encryption_key", "not-a-fernet-key-xyz-sentinel")
    with pytest.raises(RuntimeError) as exc_info:
        ...
    assert "not-a-fernet-key-xyz-sentinel" not in str(exc_info.value)


def test_lifespan_calls_init_pool_and_close_pool(monkeypatch):
    calls = []
    monkeypatch.setattr(session_store, "init_pool", lambda: calls.append("init"))
    monkeypatch.setattr(session_store, "close_pool", lambda: calls.append("close"))
    # enter and exit the lifespan context manager
    ...
    assert calls == ["init", "close"]
```

- [ ] **Step 3: Run the tests**

Run: `uv run pytest onboarding/tests/test_onboarding_main.py -v`
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add onboarding/main.py onboarding/tests/test_onboarding_main.py
git commit -m "onboarding: wire session_store's pool into the app lifespan"
```

---

### Task 5: Session cookie helpers + `GET /api/session` + `POST /api/session/reset`

**Files:**
- Modify: `onboarding/router.py`
- Test: `onboarding/tests/test_onboarding_router.py`

**Interfaces:**
- Produces: `SESSION_COOKIE_NAME`, `_get_session_id(request) -> str | None`,
  `_set_session_cookie(response, session_id) -> None` — used by every later
  task's endpoints.

- [ ] **Step 1: Add the cookie helpers and session endpoints**

```python
from fastapi import Request, Response
from onboarding import session_store

SESSION_COOKIE_NAME = "onboarding_session"

# frame -> which of that frame's stored keys are safe to echo back to the
# browser for restore-on-load badges (never a credential value).
_DISPLAY_FIELDS = {
    "render": ("owner_name", "service_url"),
    "github_app": (),
    "supabase": ("name",),
    "llm_provider": ("provider", "model"),
    "dashboard_auth": (),
    "uptime_pinger": ("monitor_id",),
}


def _get_session_id(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE_NAME)


def _set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_id,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=int(session_store.SESSION_TTL.total_seconds()),
    )


@router.get("/api/session")
async def get_session_state(request: Request) -> dict:
    session_id = _get_session_id(request)
    if session_id is None:
        return {"frames": {}}
    session = session_store.get_session(session_id)
    if session is None:
        return {"frames": {}}
    frames = {}
    for frame, data in session.frames.items():
        display = {k: data[k] for k in _DISPLAY_FIELDS.get(frame, ()) if k in data}
        frames[frame] = {"complete": True, "display": display}
    return {"frames": frames}


@router.post("/api/session/reset")
async def reset_session(request: Request, response: Response) -> Response:
    session_id = _get_session_id(request)
    if session_id is not None:
        session_store.delete_session(session_id)
    response.delete_cookie(SESSION_COOKIE_NAME)
    response.status_code = 204
    return response
```

- [ ] **Step 2: Write the tests**

Follow the existing `test_onboarding_router.py` fixture shape (a fake
`session_store` module patched in, same pattern as the existing fake
`render_client`/`github_client`).

```python
def test_get_session_with_no_cookie_returns_empty_frames(client):
    resp = client.get("/api/session")
    assert resp.status_code == 200
    assert resp.json() == {"frames": {}}


def test_get_session_with_unknown_cookie_returns_empty_frames(client):
    resp = client.get("/api/session", cookies={"onboarding_session": "bogus"})
    assert resp.json() == {"frames": {}}


def test_get_session_reflects_a_real_session_display_fields(client, fake_session_store):
    session_id = fake_session_store.create_session()
    fake_session_store.update_frame(session_id, "render", {"api_key": "rnd_x", "owner_name": "alice"})
    resp = client.get("/api/session", cookies={"onboarding_session": session_id})
    body = resp.json()
    assert body["frames"]["render"] == {"complete": True, "display": {"owner_name": "alice"}}
    assert "api_key" not in str(body)  # the credential itself must never round-trip


def test_reset_session_deletes_the_row_and_clears_the_cookie(client, fake_session_store):
    session_id = fake_session_store.create_session()
    resp = client.post("/api/session/reset", cookies={"onboarding_session": session_id})
    assert resp.status_code == 204
    assert fake_session_store.get_session(session_id) is None
    assert "onboarding_session=" in resp.headers.get("set-cookie", "") and "Max-Age=0" in resp.headers["set-cookie"] or resp.headers.get("set-cookie", "").endswith("=;")


def test_reset_session_with_no_cookie_is_a_noop_204(client):
    resp = client.post("/api/session/reset")
    assert resp.status_code == 204
```

(Adjust the cookie-clearing assertion to match whatever exact header shape
`Response.delete_cookie` actually produces — check by running the test once
and reading the real header rather than guessing the exact string.)

- [ ] **Step 3: Run the tests**

Run: `uv run pytest onboarding/tests/test_onboarding_router.py -v -k session`
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add onboarding/router.py onboarding/tests/test_onboarding_router.py
git commit -m "onboarding: add GET /api/session and POST /api/session/reset"
```

---

### Task 6: Wire `render-key` frame + `render-service` frame + new `dashboard-auth` confirm endpoint

**Files:**
- Modify: `onboarding/router.py`
- Test: `onboarding/tests/test_onboarding_router.py`

**Interfaces:**
- Consumes: `session_store.create_session`, `session_store.update_frame`,
  `session_store.read_frame`, `_get_session_id`, `_set_session_cookie` (Task 5).
- Produces: `/api/dashboard-auth/confirm` (new).

- [ ] **Step 1: `render-key` — the session's entry point**

```python
@router.post("/api/render/validate-key")
async def validate_render_key(payload: RenderKeyRequest, request: Request, response: Response) -> dict:
    result = await render_client.validate_key(payload.api_key)
    if isinstance(result, render_client.RenderKeyValid):
        session_id = _get_session_id(request)
        if session_id is None or session_store.get_session(session_id) is None:
            session_id = session_store.create_session()
            _set_session_cookie(response, session_id)
        session_store.update_frame(
            session_id, "render", {"api_key": payload.api_key, "owner_name": result.owner_name}
        )
        return {"valid": True, "owner_name": result.owner_name}
    return {"valid": False, "reason": result.reason}
```

Note this is the one endpoint allowed to call `create_session()` — every
other endpoint below requires an existing session and fails closed.

- [ ] **Step 2: `render-service` — drop `api_key` from the request body**

```python
class RenderServiceCreateRequest(BaseModel):
    repo_url: str = Field(min_length=1, max_length=512)
    name: str = Field(min_length=1, max_length=64)


@router.post("/api/render/create-service")
async def create_render_service(payload: RenderServiceCreateRequest, request: Request) -> dict:
    session_id = _get_session_id(request)
    render_frame = session_id and session_store.read_frame(session_id, "render")
    if not render_frame or "api_key" not in render_frame:
        return {"valid": False, "reason": "no_session"}
    result = await render_client.create_service(render_frame["api_key"], payload.repo_url, payload.name)
    if isinstance(result, render_client.RenderServiceCreated):
        session_store.update_frame(
            session_id, "render", {"service_id": result.service_id, "service_url": result.service_url}
        )
        return {"valid": True, "service_id": result.service_id, "service_url": result.service_url}
    if result.message:
        return {"valid": False, "reason": result.reason, "message": result.message}
    return {"valid": False, "reason": result.reason}
```

`"no_session"` is a new reason string every frame-2+ endpoint in this plan
uses for "please complete earlier steps first" — add it to the frontend's
error-reason-to-i18n-key mapping in Task 12/13 as a generic
`err_no_session` message ("Your session isn't ready for this step — please
complete the earlier steps first.").

- [ ] **Step 3: New `dashboard-auth` confirm endpoint, replacing its push endpoint**

Remove `DashboardAuthPushRenderVarsRequest` and
`push_dashboard_auth_render_vars` entirely. Add:

```python
class DashboardAuthConfirmRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=8, max_length=256)
    session_secret: str = Field(min_length=32, max_length=256)


@router.post("/api/dashboard-auth/confirm")
async def confirm_dashboard_auth(payload: DashboardAuthConfirmRequest, request: Request) -> dict:
    session_id = _get_session_id(request)
    if session_id is None or session_store.get_session(session_id) is None:
        return {"valid": False, "reason": "no_session"}
    session_store.update_frame(
        session_id, "dashboard_auth",
        {"username": payload.username, "password": payload.password, "session_secret": payload.session_secret},
    )
    return {"valid": True}
```

There's no external API to validate this against (it's wizard/visitor-
chosen, never checked against anything else) — this endpoint's only job is
persisting it to the session, unlike every other frame's endpoint which
also calls an external API.

- [ ] **Step 4: Write the tests**

```python
def test_validate_render_key_creates_a_session_and_sets_the_cookie(client, fake_render_client):
    fake_render_client.set_valid("owner_name", "alice")
    resp = client.post("/api/render/validate-key", json={"api_key": "rnd_x"})
    assert resp.json() == {"valid": True, "owner_name": "alice"}
    assert "onboarding_session=" in resp.headers.get("set-cookie", "")


def test_validate_render_key_reuses_an_existing_session_cookie(client, fake_render_client, fake_session_store):
    session_id = fake_session_store.create_session()
    fake_render_client.set_valid("owner_name", "alice")
    resp = client.post("/api/render/validate-key", json={"api_key": "rnd_x"}, cookies={"onboarding_session": session_id})
    assert "set-cookie" not in resp.headers  # no NEW cookie needed


def test_create_render_service_reads_api_key_from_session_not_body(client, fake_session_store, fake_render_client):
    session_id = fake_session_store.create_session()
    fake_session_store.update_frame(session_id, "render", {"api_key": "rnd_x"})
    fake_render_client.set_service_created("srv-1", "https://x.onrender.com")
    resp = client.post(
        "/api/render/create-service",
        json={"repo_url": "https://github.com/x/y", "name": "myapp"},
        cookies={"onboarding_session": session_id},
    )
    assert resp.json()["valid"] is True
    assert fake_render_client.last_api_key_used == "rnd_x"


def test_create_render_service_with_no_session_fails_closed(client):
    resp = client.post("/api/render/create-service", json={"repo_url": "https://github.com/x/y", "name": "myapp"})
    assert resp.json() == {"valid": False, "reason": "no_session"}


def test_confirm_dashboard_auth_persists_to_session(client, fake_session_store):
    session_id = fake_session_store.create_session()
    resp = client.post(
        "/api/dashboard-auth/confirm",
        json={"username": "admin", "password": "x" * 12, "session_secret": "y" * 32},
        cookies={"onboarding_session": session_id},
    )
    assert resp.json() == {"valid": True}
    assert fake_session_store.read_frame(session_id, "dashboard_auth")["username"] == "admin"


def test_confirm_dashboard_auth_with_no_session_fails_closed(client):
    resp = client.post(
        "/api/dashboard-auth/confirm",
        json={"username": "admin", "password": "x" * 12, "session_secret": "y" * 32},
    )
    assert resp.json() == {"valid": False, "reason": "no_session"}


def test_dashboard_auth_push_render_vars_endpoint_is_gone(client):
    resp = client.post("/api/dashboard-auth/push-render-vars", json={})
    assert resp.status_code == 404
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest onboarding/tests/test_onboarding_router.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add onboarding/router.py onboarding/tests/test_onboarding_router.py
git commit -m "onboarding: session-back render-key/render-service, add dashboard-auth confirm"
```

---

### Task 7: Wire `github-app` frame; remove its push endpoint

**Files:**
- Modify: `onboarding/router.py`
- Test: `onboarding/tests/test_onboarding_router.py`

- [ ] **Step 1: Add `webhook_secret` to the request and persist on success**

The webhook secret is generated client-side (never sent to GitHub, only
used later as `GITHUB_WEBHOOK_SECRET`), so it must ride along in this
request purely for storage — `validate_app()`'s own logic doesn't use it.

```python
class GithubValidateAppRequest(BaseModel):
    app_id: int = Field(gt=0)
    private_key_b64: str = Field(max_length=16384)
    expected_webhook_url: str = Field(min_length=1, max_length=2048, pattern=r"^https?://[^\s\"'<>\\]+$")
    webhook_secret: str = Field(min_length=1, max_length=512)


@router.post("/api/github/validate-app")
async def validate_github_app(payload: GithubValidateAppRequest, request: Request) -> dict:
    result = await github_client.validate_app(
        payload.app_id, payload.private_key_b64, payload.expected_webhook_url
    )
    if isinstance(result, github_client.AppCredentialsInvalid):
        return {"valid": False, "reason": result.reason}

    if isinstance(result.installation, github_client.InstallationFound):
        installation = {
            "status": "found",
            "installation_id": result.installation.installation_id,
            "account_login": result.installation.account_login,
            "repo_scope": result.installation.repo_scope,
        }
        installation_ok = True
        installation_id = result.installation.installation_id
    elif isinstance(result.installation, github_client.MultipleInstallationsFound):
        installation = {"status": "multiple", "account_logins": result.installation.account_logins}
        installation_ok = False
        installation_id = None
    else:
        installation = {"status": "none"}
        installation_ok = False
        installation_id = None

    all_ok = (
        all(p.ok for p in result.permissions)
        and all(e.ok for e in result.events)
        and installation_ok
        and result.webhook.ok
    )
    if all_ok:
        session_id = _get_session_id(request)
        if session_id is not None and session_store.get_session(session_id) is not None:
            session_store.update_frame(
                session_id, "github_app",
                {
                    "app_id": payload.app_id,
                    "private_key_b64": payload.private_key_b64,
                    "webhook_secret": payload.webhook_secret,
                    "installation_id": installation_id,
                },
            )
    return {
        "valid": True,
        "all_ok": all_ok,
        "permissions": [...],  # unchanged
        "events": [...],  # unchanged
        "installation": installation,
        "webhook": {"ok": result.webhook.ok, "actual_url": result.webhook.actual_url},
    }
```

Note this endpoint does NOT fail closed on a missing session the way
`create-service`/`confirm` do — a visitor re-validating before frame 1 is
even done should still see the live checklist result; it just silently
skips persisting until a session exists (the frontend's own frame-lock
sequencing already prevents reaching this frame without frame 1 done in
the normal flow; this is defense for a directly-called endpoint, not the
primary guard).

- [ ] **Step 2: Remove the push endpoint**

Delete `GithubPushRenderVarsRequest` and `push_github_render_vars` entirely.

- [ ] **Step 3: Write the tests**

```python
def test_validate_github_app_persists_on_all_ok(client, fake_github_client, fake_session_store):
    session_id = fake_session_store.create_session()
    fake_github_client.set_all_ok(installation_id=42)
    resp = client.post(
        "/api/github/validate-app",
        json={"app_id": 1, "private_key_b64": "x", "expected_webhook_url": "https://x/y", "webhook_secret": "s" * 20},
        cookies={"onboarding_session": session_id},
    )
    assert resp.json()["all_ok"] is True
    stored = fake_session_store.read_frame(session_id, "github_app")
    assert stored == {"app_id": 1, "private_key_b64": "x", "webhook_secret": "s" * 20, "installation_id": 42}


def test_validate_github_app_does_not_persist_when_not_all_ok(client, fake_github_client, fake_session_store):
    session_id = fake_session_store.create_session()
    fake_github_client.set_installation_missing()
    client.post(
        "/api/github/validate-app",
        json={"app_id": 1, "private_key_b64": "x", "expected_webhook_url": "https://x/y", "webhook_secret": "s" * 20},
        cookies={"onboarding_session": session_id},
    )
    assert fake_session_store.read_frame(session_id, "github_app") is None


def test_github_push_render_vars_endpoint_is_gone(client):
    resp = client.post("/api/github/push-render-vars", json={})
    assert resp.status_code == 404
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest onboarding/tests/test_onboarding_router.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add onboarding/router.py onboarding/tests/test_onboarding_router.py
git commit -m "onboarding: session-back github-app frame, remove its push endpoint"
```

---

### Task 8: Rewrite Supabase — redirect-and-resume + session-backed calls; remove its push endpoint

**Files:**
- Modify: `onboarding/router.py`, `onboarding/supabase_client.py`
- Test: `onboarding/tests/test_onboarding_router.py`, `onboarding/tests/test_onboarding_supabase_client.py`

This is the task that directly fixes the reported mobile bug. Implements
spec section 3.4 exactly.

**Verified against the live `index.html` flow (confirmed by reading
`connectSupabase()`, `fetchSupabaseOrganizations()`,
`kickOffProjectCreation()` directly):** only the project **name** is chosen
before the redirect. `organization_slug` is chosen *after* the callback
(`fetchSupabaseOrganizations()` lists orgs using the just-obtained access
token; the visitor then picks one, or it's auto-picked if there's only
one). `db_pass` is generated client-side inside `kickOffProjectCreation()`,
also *after* the callback. So `_pending_oauth` only ever needs to carry
`name` — not `organization_slug` or `db_pass`, and `create_project()` is
**not** called from the callback route at all; it moves to its own
session-backed endpoint called after the redirect, same as
`list-organizations`.

- [ ] **Step 1: Add a "start OAuth" endpoint (replacing the frontend's own PKCE generation)**

Currently the browser generates `state`/`code_verifier` itself (see
`connectSupabase()`, ~line 2059, and `generatePkcePair()`). Move this
server-side so the verifier never needs to touch the browser at all:

```python
import secrets as _secrets
import hashlib
import base64


class SupabaseConnectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=256)


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(_secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return verifier, challenge


@router.post("/api/supabase/connect")
async def connect_supabase(payload: SupabaseConnectRequest, request: Request) -> dict:
    session_id = _get_session_id(request)
    if session_id is None or session_store.get_session(session_id) is None:
        return {"valid": False, "reason": "no_session"}
    state = _secrets.token_urlsafe(24)
    verifier, challenge = _pkce_pair()
    session_store.update_frame(
        session_id, "supabase",
        {"_pending_oauth": {"state": state, "verifier": verifier, "name": payload.name}},
    )
    redirect_uri = f"{request.base_url}{SUPABASE_OAUTH_CALLBACK_PATH.lstrip('/')}"
    params = {
        "client_id": settings.supabase_oauth_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    from urllib.parse import urlencode
    return {"valid": True, "authorize_url": f"https://api.supabase.com/v1/oauth/authorize?{urlencode(params)}"}
```

- [ ] **Step 2: Rewrite the OAuth callback route**

Replace the current bare `supabase_oauth_callback()` (which just re-serves
the page) with the real handler. It only exchanges the code for tokens and
carries `name` forward — it does **not** create the project (org isn't
known yet):

```python
@router.get(SUPABASE_OAUTH_CALLBACK_PATH)
async def supabase_oauth_callback(request: Request) -> Response:
    from fastapi.responses import RedirectResponse

    session_id = _get_session_id(request)
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    pending = None
    if session_id is not None:
        supabase_frame = session_store.read_frame(session_id, "supabase")
        pending = (supabase_frame or {}).get("_pending_oauth")
    if session_id is None or pending is None or not code or pending.get("state") != state:
        # No session, no pending OAuth, or a state mismatch (forged/replayed
        # redirect) -- fall back to the ordinary "no session" page load
        # rather than completing anything. See spec section 3.4 step 3.
        return RedirectResponse(url="/", status_code=302)

    redirect_uri = f"{request.base_url}{SUPABASE_OAUTH_CALLBACK_PATH.lstrip('/')}"
    token_result = await supabase_client.exchange_oauth_code(code, pending["verifier"], redirect_uri)
    if not isinstance(token_result, supabase_client.SupabaseTokens):
        return RedirectResponse(url="/", status_code=302)

    session_store.update_frame(
        session_id, "supabase",
        {
            "access_token": token_result.access_token,
            "refresh_token": token_result.refresh_token,
            "name": pending["name"],
            "_pending_oauth": None,
        },
    )
    return RedirectResponse(url="/", status_code=302)
```

`_pending_oauth: None` clears the pending marker on completion (merged in
via `update_frame`, so it overwrites the key rather than leaving stale PKCE
material around).

- [ ] **Step 3: Session-back list-organizations, create-project, project-status, connection-info**

```python
class SupabaseCreateProjectRequest(BaseModel):
    organization_slug: str = Field(min_length=1, max_length=64)


@router.post("/api/supabase/list-organizations")
async def list_supabase_organizations(request: Request) -> dict:
    session_id = _get_session_id(request)
    supabase_frame = session_id and session_store.read_frame(session_id, "supabase")
    if not supabase_frame or "access_token" not in supabase_frame:
        return {"valid": False, "reason": "no_session"}
    result = await supabase_client.list_organizations(supabase_frame["access_token"])
    if isinstance(result, supabase_client.SupabaseOrgsListed):
        return {"valid": True, "orgs": [{"slug": o.slug, "name": o.name} for o in result.orgs]}
    return {"valid": False, "reason": result.reason}


@router.post("/api/supabase/create-project")
async def create_supabase_project(payload: SupabaseCreateProjectRequest, request: Request) -> dict:
    session_id = _get_session_id(request)
    supabase_frame = session_id and session_store.read_frame(session_id, "supabase")
    if not supabase_frame or "access_token" not in supabase_frame or "name" not in supabase_frame:
        return {"valid": False, "reason": "no_session"}
    db_pass = _secrets.token_urlsafe(24)
    result = await supabase_client.create_project(
        supabase_frame["access_token"], payload.organization_slug, supabase_frame["name"], db_pass
    )
    if isinstance(result, supabase_client.SupabaseProjectCreated):
        session_store.update_frame(
            session_id, "supabase",
            {"ref": result.ref, "status": result.status, "db_pass": db_pass, "organization_slug": payload.organization_slug},
        )
        return {"valid": True, "ref": result.ref, "status": result.status}
    if isinstance(result, supabase_client.SupabaseProjectRejected):
        return {"valid": False, "reason": "project_creation_rejected", "message": result.message}
    return {"valid": False, "reason": result.reason}


@router.post("/api/supabase/project-status")
async def get_supabase_project_status(request: Request) -> dict:
    session_id = _get_session_id(request)
    supabase_frame = session_id and session_store.read_frame(session_id, "supabase")
    if not supabase_frame or "access_token" not in supabase_frame or "ref" not in supabase_frame:
        return {"valid": False, "reason": "no_session"}
    result = await supabase_client.get_project_status(supabase_frame["access_token"], supabase_frame["ref"])
    if isinstance(result, supabase_client.SupabaseProjectStatus):
        return {"valid": True, "status": result.status}
    return {"valid": False, "reason": result.reason}


@router.post("/api/supabase/connection-info")
async def get_supabase_connection_info(request: Request) -> dict:
    session_id = _get_session_id(request)
    supabase_frame = session_id and session_store.read_frame(session_id, "supabase")
    if not supabase_frame or not all(k in supabase_frame for k in ("access_token", "ref", "db_pass")):
        return {"valid": False, "reason": "no_session"}
    result = await supabase_client.get_connection_info(
        supabase_frame["access_token"], supabase_frame["ref"], session_id=session_id
    )
    if isinstance(result, supabase_client.SupabaseConnectionInfo):
        database_url = (
            f"postgresql://{result.db_user}:{supabase_frame['db_pass']}"
            f"@{result.db_host}:{result.db_port}/{result.db_name}"
        )
        session_store.update_frame(session_id, "supabase", {"database_url": database_url})
        return {"valid": True}
    return {"valid": False, "reason": result.reason}
```

`db_pass` is now generated **server-side** in `create-project` (moved from
`kickOffProjectCreation()`'s client-side `generateDbPassword()` — delete
that function and its call in Task 14) since the server needs it later for
`connection-info`'s assembly and it should never have to round-trip back to
the browser at all. `connection-info` no longer returns `db_user`/
`db_host`/`db_port`/`db_name` to the browser — the full connection string
is assembled and stored server-side, consistent with "never echo a
credential."

Delete `SupabaseListOrgsRequest`, the old body-taking
`SupabaseCreateProjectRequest` shape (replaced by the org-slug-only version
above), `SupabaseExchangeCodeRequest`/`exchange_supabase_oauth_code`, and
`SupabaseRefreshTokenRequest`/`refresh_supabase_access_token` entirely — the
callback route now owns the code exchange, and there is no client-facing
refresh endpoint anymore since `access_token` never leaves the server.
(`supabase_client.refresh_access_token` itself stays in
`supabase_client.py`, unused by any endpoint yet — leave it in place rather
than deleting a working, previously-tested function; wiring an automatic
refresh-on-401 into the session-backed calls above is worth a follow-up but
is not required to fix the reported bug, and is explicitly out of scope
here per the spec's own scope boundary.)

- [ ] **Step 4: Carry forward the outstanding connection-info diagnostic**

In `onboarding/supabase_client.py`, change `get_connection_info`'s signature
to accept a `session_id: str` parameter purely for log correlation, and tag
both existing `print()` diagnostics with it:

```python
async def get_connection_info(
    access_token: str, ref: str, session_id: str
) -> SupabaseConnectionInfo | SupabaseApiFailed:
    ...
    except ValueError:
        print(f"[DEBUG connection-info session={session_id}] response body did not parse as JSON")
        return SupabaseApiFailed(reason="pooler_config_unavailable")
    ...
        print(f"[DEBUG connection-info session={session_id}] no session/PRIMARY match; entries seen: {shapes}")
        return SupabaseApiFailed(reason="pooler_config_unavailable")
```

Do not touch the diagnostic's actual logic (still only logs `pool_mode`/
`database_type` labels, never a credential) — only add the session id tag.
The mismatch itself stays an open, separately-tracked issue (`ISSUES.md`).

- [ ] **Step 5: Write the tests**

```python
def test_connect_supabase_stores_pending_oauth_and_returns_authorize_url(client, fake_session_store):
    session_id = fake_session_store.create_session()
    resp = client.post(
        "/api/supabase/connect", json={"name": "myproj"}, cookies={"onboarding_session": session_id},
    )
    body = resp.json()
    assert body["valid"] is True
    assert body["authorize_url"].startswith("https://api.supabase.com/v1/oauth/authorize?")
    pending = fake_session_store.read_frame(session_id, "supabase")["_pending_oauth"]
    assert pending["state"] and pending["verifier"] and pending["name"] == "myproj"


def test_connect_supabase_with_no_session_fails_closed(client):
    resp = client.post("/api/supabase/connect", json={"name": "x"})
    assert resp.json() == {"valid": False, "reason": "no_session"}


def test_oauth_callback_completes_on_matching_state(client, fake_session_store, fake_supabase_client):
    session_id = fake_session_store.create_session()
    fake_session_store.update_frame(session_id, "supabase", {
        "_pending_oauth": {"state": "abc", "verifier": "v", "name": "myproj"}
    })
    fake_supabase_client.set_token_exchange_ok(access_token="tok", refresh_token="ref-tok")
    resp = client.get(
        "/oauth/supabase/callback?code=somecode&state=abc",
        cookies={"onboarding_session": session_id},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"
    stored = fake_session_store.read_frame(session_id, "supabase")
    assert stored["access_token"] == "tok"
    assert stored["name"] == "myproj"
    assert stored["_pending_oauth"] is None


def test_oauth_callback_rejects_a_mismatched_state(client, fake_session_store):
    session_id = fake_session_store.create_session()
    fake_session_store.update_frame(session_id, "supabase", {
        "_pending_oauth": {"state": "abc", "verifier": "v", "name": "myproj"}
    })
    resp = client.get(
        "/oauth/supabase/callback?code=somecode&state=WRONG",
        cookies={"onboarding_session": session_id},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert fake_session_store.read_frame(session_id, "supabase").get("access_token") is None


def test_oauth_callback_with_no_pending_state_falls_back_gracefully(client, fake_session_store):
    session_id = fake_session_store.create_session()
    resp = client.get(
        "/oauth/supabase/callback?code=somecode&state=abc",
        cookies={"onboarding_session": session_id},
        follow_redirects=False,
    )
    assert resp.status_code == 302


def test_list_organizations_reads_access_token_from_session(client, fake_session_store, fake_supabase_client):
    session_id = fake_session_store.create_session()
    fake_session_store.update_frame(session_id, "supabase", {"access_token": "tok"})
    fake_supabase_client.set_orgs([("org1", "Org One")])
    resp = client.post("/api/supabase/list-organizations", cookies={"onboarding_session": session_id})
    assert resp.json() == {"valid": True, "orgs": [{"slug": "org1", "name": "Org One"}]}


def test_list_organizations_with_no_session_fails_closed(client):
    resp = client.post("/api/supabase/list-organizations")
    assert resp.json() == {"valid": False, "reason": "no_session"}


def test_create_project_generates_db_pass_server_side_and_stores_it(client, fake_session_store, fake_supabase_client):
    session_id = fake_session_store.create_session()
    fake_session_store.update_frame(session_id, "supabase", {"access_token": "tok", "name": "myproj"})
    fake_supabase_client.set_project_created(ref="abcdefghijklmnopqrst", status="COMING_UP")
    resp = client.post(
        "/api/supabase/create-project", json={"organization_slug": "org1"},
        cookies={"onboarding_session": session_id},
    )
    body = resp.json()
    assert body == {"valid": True, "ref": "abcdefghijklmnopqrst", "status": "COMING_UP"}
    stored = fake_session_store.read_frame(session_id, "supabase")
    assert stored["ref"] == "abcdefghijklmnopqrst"
    assert stored["organization_slug"] == "org1"
    assert stored["db_pass"]  # generated, never supplied by the client
    assert "db_pass" not in body  # never echoed back


def test_create_project_with_no_session_fails_closed(client):
    resp = client.post("/api/supabase/create-project", json={"organization_slug": "org1"})
    assert resp.json() == {"valid": False, "reason": "no_session"}


def test_connection_info_never_returns_db_host_or_user_to_the_browser(client, fake_session_store, fake_supabase_client):
    session_id = fake_session_store.create_session()
    fake_session_store.update_frame(session_id, "supabase", {"access_token": "tok", "ref": "abcdefghijklmnopqrst", "db_pass": "p" * 12})
    fake_supabase_client.set_connection_info(db_user="postgres.x", db_host="aws-0.pooler.supabase.com", db_port=5432, db_name="postgres")
    resp = client.post("/api/supabase/connection-info", cookies={"onboarding_session": session_id})
    body = resp.json()
    assert body == {"valid": True}
    assert fake_session_store.read_frame(session_id, "supabase")["database_url"].startswith("postgresql://postgres.x:")


def test_supabase_push_render_var_endpoint_is_gone(client):
    resp = client.post("/api/supabase/push-render-var", json={})
    assert resp.status_code == 404


def test_supabase_exchange_oauth_code_endpoint_is_gone(client):
    resp = client.post("/api/supabase/exchange-oauth-code", json={})
    assert resp.status_code == 404


def test_supabase_refresh_access_token_endpoint_is_gone(client):
    resp = client.post("/api/supabase/refresh-access-token", json={})
    assert resp.status_code == 404
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest onboarding/tests/test_onboarding_router.py onboarding/tests/test_onboarding_supabase_client.py -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add onboarding/router.py onboarding/supabase_client.py onboarding/tests/test_onboarding_router.py onboarding/tests/test_onboarding_supabase_client.py
git commit -m "onboarding: redirect-and-resume Supabase OAuth via server-side session"
```

---

### Task 9: Wire `llm-provider` frame; remove its push endpoint

**Files:**
- Modify: `onboarding/router.py`
- Test: `onboarding/tests/test_onboarding_router.py`

- [ ] **Step 1: New confirm endpoint**

The three `list-models` endpoints stay exactly as-is (live validation of a
freshly-pasted credential, nothing persisted yet — the visitor may list
models under more than one provider before settling on one). Add:

```python
class LlmConfirmRequest(BaseModel):
    provider: str = Field(pattern=r"^(gemini|groq|vertex)$")
    credential_value: str = Field(min_length=1, max_length=16384)
    model: str = Field(min_length=1, max_length=256)


@router.post("/api/llm/confirm")
async def confirm_llm_provider(payload: LlmConfirmRequest, request: Request) -> dict:
    session_id = _get_session_id(request)
    if session_id is None or session_store.get_session(session_id) is None:
        return {"valid": False, "reason": "no_session"}
    session_store.update_frame(
        session_id, "llm_provider",
        {"provider": payload.provider, "credential_value": payload.credential_value, "model": payload.model},
    )
    return {"valid": True}
```

- [ ] **Step 2: Remove the push endpoint**

Delete `LlmPushRenderVarsRequest` and `push_llm_render_vars` entirely.

- [ ] **Step 3: Write the tests**

```python
def test_confirm_llm_provider_persists_to_session(client, fake_session_store):
    session_id = fake_session_store.create_session()
    resp = client.post(
        "/api/llm/confirm",
        json={"provider": "gemini", "credential_value": "AIza-x", "model": "gemini-flash-latest"},
        cookies={"onboarding_session": session_id},
    )
    assert resp.json() == {"valid": True}
    assert fake_session_store.read_frame(session_id, "llm_provider") == {
        "provider": "gemini", "credential_value": "AIza-x", "model": "gemini-flash-latest",
    }


def test_confirm_llm_provider_with_no_session_fails_closed(client):
    resp = client.post("/api/llm/confirm", json={"provider": "gemini", "credential_value": "x", "model": "m"})
    assert resp.json() == {"valid": False, "reason": "no_session"}


def test_llm_push_render_vars_endpoint_is_gone(client):
    resp = client.post("/api/llm/push-render-vars", json={})
    assert resp.status_code == 404
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest onboarding/tests/test_onboarding_router.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add onboarding/router.py onboarding/tests/test_onboarding_router.py
git commit -m "onboarding: add /api/llm/confirm, remove per-frame llm push endpoint"
```

---

### Task 10: Wire `uptime-pinger` frame

**Files:**
- Modify: `onboarding/router.py`
- Test: `onboarding/tests/test_onboarding_router.py`

- [ ] **Step 1: Persist on success**

```python
@router.post("/api/uptimerobot/create-monitor")
async def create_uptimerobot_monitor(payload: UptimeRobotCreateMonitorRequest, request: Request) -> dict:
    result = await uptimerobot_client.create_or_reuse_monitor(payload.api_key, payload.render_service_url)
    if isinstance(result, uptimerobot_client.UptimeRobotMonitorResult):
        session_id = _get_session_id(request)
        if session_id is not None and session_store.get_session(session_id) is not None:
            session_store.update_frame(session_id, "uptime_pinger", {"monitor_id": result.monitor_id})
        return {"valid": True, "created": result.created, "monitor_id": result.monitor_id}
    return {"valid": False, "reason": result.reason}
```

No endpoint removal here — UptimeRobot never had a push-to-Render endpoint
(it doesn't produce a Render env var).

- [ ] **Step 2: Write the test**

```python
def test_create_uptimerobot_monitor_persists_to_session(client, fake_uptimerobot_client, fake_session_store):
    session_id = fake_session_store.create_session()
    fake_uptimerobot_client.set_created(monitor_id=99)
    client.post(
        "/api/uptimerobot/create-monitor",
        json={"api_key": "u_x", "render_service_url": "https://x.onrender.com"},
        cookies={"onboarding_session": session_id},
    )
    assert fake_session_store.read_frame(session_id, "uptime_pinger") == {"monitor_id": 99}
```

- [ ] **Step 3: Run the tests**

Run: `uv run pytest onboarding/tests/test_onboarding_router.py -v`
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add onboarding/router.py onboarding/tests/test_onboarding_router.py
git commit -m "onboarding: session-back the uptime-pinger frame"
```

---

### Task 11: `render-deploy` — the bulk push endpoint + session-back trigger-deploy/deploy-status

**Files:**
- Modify: `onboarding/router.py`
- Test: `onboarding/tests/test_onboarding_router.py`

**Interfaces:**
- Produces: `POST /api/render/bulk-push-env-vars` (new, replaces the four
  deleted per-frame push endpoints).

- [ ] **Step 1: The bulk push endpoint**

```python
_LLM_ENV_VAR_NAMES = {  # unchanged, already exists
    "gemini": ("GEMINI_API_KEY", "LLM_MODEL"),
    "groq": ("GROQ_API_KEY", "GROQ_MODEL"),
    "vertex": ("GCP_SERVICE_ACCOUNT_KEY", "VERTEX_MODEL"),
}


@router.post("/api/render/bulk-push-env-vars")
async def bulk_push_render_env_vars(request: Request) -> dict:
    session_id = _get_session_id(request)
    render_frame = session_id and session_store.read_frame(session_id, "render")
    if not render_frame or "api_key" not in render_frame or "service_id" not in render_frame:
        return {"valid": False, "reason": "no_session"}

    env_vars: dict[str, str] = {}

    github_app = session_store.read_frame(session_id, "github_app")
    if github_app:
        env_vars["GITHUB_APP_ID"] = str(github_app["app_id"])
        env_vars["GITHUB_APP_PRIVATE_KEY"] = github_app["private_key_b64"]
        env_vars["GITHUB_WEBHOOK_SECRET"] = github_app["webhook_secret"]
        env_vars["GITHUB_APP_INSTALLATION_ID"] = str(github_app["installation_id"])

    supabase = session_store.read_frame(session_id, "supabase")
    if supabase and "database_url" in supabase:
        env_vars["DATABASE_URL"] = supabase["database_url"]

    llm_provider = session_store.read_frame(session_id, "llm_provider")
    if llm_provider:
        credential_var, model_var = _LLM_ENV_VAR_NAMES[llm_provider["provider"]]
        env_vars["LLM_PROVIDER"] = llm_provider["provider"]
        env_vars[credential_var] = llm_provider["credential_value"]
        env_vars[model_var] = llm_provider["model"]

    dashboard_auth = session_store.read_frame(session_id, "dashboard_auth")
    if dashboard_auth:
        env_vars["DASHBOARD_USERNAME"] = dashboard_auth["username"]
        env_vars["DASHBOARD_PASSWORD"] = dashboard_auth["password"]
        env_vars["DASHBOARD_SESSION_SECRET"] = dashboard_auth["session_secret"]

    result = await render_client.push_env_vars(render_frame["api_key"], render_frame["service_id"], env_vars)
    return _push_result(result)
```

- [ ] **Step 2: Session-back trigger-deploy and deploy-status; drop their bodies**

```python
@router.post("/api/render/trigger-deploy")
async def trigger_render_deploy(request: Request) -> dict:
    session_id = _get_session_id(request)
    render_frame = session_id and session_store.read_frame(session_id, "render")
    if not render_frame or "api_key" not in render_frame or "service_id" not in render_frame:
        return {"valid": False, "reason": "no_session"}
    result = await render_client.trigger_deploy(render_frame["api_key"], render_frame["service_id"])
    if isinstance(result, render_client.RenderDeployTriggered):
        session_store.update_frame(session_id, "render", {"pending_deploy_id": result.deploy_id})
        return {"valid": True, "deploy_id": result.deploy_id}
    return {"valid": False, "reason": result.reason}


@router.post("/api/render/deploy-status")
async def get_render_deploy_status(request: Request) -> dict:
    session_id = _get_session_id(request)
    render_frame = session_id and session_store.read_frame(session_id, "render")
    if not render_frame or not all(k in render_frame for k in ("api_key", "service_id", "pending_deploy_id")):
        return {"valid": False, "reason": "no_session"}
    result = await render_client.poll_deploy_status(
        render_frame["api_key"], render_frame["service_id"], render_frame["pending_deploy_id"]
    )
    if isinstance(result, render_client.RenderDeployStatus):
        return {"valid": True, "status": result.status}
    return {"valid": False, "reason": result.reason}
```

Remove `RenderTriggerDeployRequest` and `RenderDeployStatusRequest` (no
longer take a body).

- [ ] **Step 3: Write the tests**

```python
def test_bulk_push_assembles_every_frame_into_one_push_call(client, fake_session_store, fake_render_client):
    session_id = fake_session_store.create_session()
    fake_session_store.update_frame(session_id, "render", {"api_key": "rnd_x", "service_id": "srv-1"})
    fake_session_store.update_frame(session_id, "github_app", {
        "app_id": 1, "private_key_b64": "pk", "webhook_secret": "wh", "installation_id": 42,
    })
    fake_session_store.update_frame(session_id, "supabase", {"database_url": "postgresql://x"})
    fake_session_store.update_frame(session_id, "llm_provider", {"provider": "gemini", "credential_value": "AIza-x", "model": "gemini-flash-latest"})
    fake_session_store.update_frame(session_id, "dashboard_auth", {"username": "admin", "password": "pw123456", "session_secret": "s" * 32})
    resp = client.post("/api/render/bulk-push-env-vars", cookies={"onboarding_session": session_id})
    assert resp.json()["valid"] is True
    pushed = fake_render_client.last_env_vars_pushed
    assert pushed["GITHUB_APP_ID"] == "1"
    assert pushed["DATABASE_URL"] == "postgresql://x"
    assert pushed["GEMINI_API_KEY"] == "AIza-x"
    assert pushed["DASHBOARD_USERNAME"] == "admin"


def test_bulk_push_omits_a_frame_that_was_never_completed(client, fake_session_store, fake_render_client):
    session_id = fake_session_store.create_session()
    fake_session_store.update_frame(session_id, "render", {"api_key": "rnd_x", "service_id": "srv-1"})
    client.post("/api/render/bulk-push-env-vars", cookies={"onboarding_session": session_id})
    pushed = fake_render_client.last_env_vars_pushed
    assert "GITHUB_APP_ID" not in pushed


def test_bulk_push_with_no_session_fails_closed(client):
    resp = client.post("/api/render/bulk-push-env-vars")
    assert resp.json() == {"valid": False, "reason": "no_session"}


def test_trigger_deploy_reads_from_session_and_stores_deploy_id(client, fake_session_store, fake_render_client):
    session_id = fake_session_store.create_session()
    fake_session_store.update_frame(session_id, "render", {"api_key": "rnd_x", "service_id": "srv-1"})
    fake_render_client.set_deploy_triggered(deploy_id="dep-1")
    resp = client.post("/api/render/trigger-deploy", cookies={"onboarding_session": session_id})
    assert resp.json() == {"valid": True, "deploy_id": "dep-1"}
    assert fake_session_store.read_frame(session_id, "render")["pending_deploy_id"] == "dep-1"


def test_deploy_status_reads_from_session(client, fake_session_store, fake_render_client):
    session_id = fake_session_store.create_session()
    fake_session_store.update_frame(session_id, "render", {"api_key": "rnd_x", "service_id": "srv-1", "pending_deploy_id": "dep-1"})
    fake_render_client.set_deploy_status("live")
    resp = client.post("/api/render/deploy-status", cookies={"onboarding_session": session_id})
    assert resp.json() == {"valid": True, "status": "live"}
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest onboarding/tests/test_onboarding_router.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add onboarding/router.py onboarding/tests/test_onboarding_router.py
git commit -m "onboarding: add bulk env-var push for render-deploy, session-back trigger/status"
```

---

### Task 12: Frontend — restore-on-load, Start-over control, stop storing credentials in sessionStorage

**Files:**
- Modify: `onboarding/static/index.html`
- Test: `onboarding/tests/test_onboarding_page.py`

This is the largest single frontend task. **Read the current file in full
around every function named below before editing it** — this plan
describes the required behavior and gives full code for new functions, but
exact line numbers/surrounding context must come from the live file, not
this plan text.

- [ ] **Step 1: Add the "Start over" header control**

Add a button to the page header markup (near the theme/language toggles):
```html
<button id="wizard-reset-button" type="button" data-i18n="reset_wizard_button"></button>
<dialog id="wizard-reset-confirm-dialog">
  <p data-i18n="reset_wizard_confirm_text"></p>
  <button id="wizard-reset-confirm-yes" data-i18n="reset_wizard_confirm_yes"></button>
  <button id="wizard-reset-confirm-no" data-i18n="reset_wizard_confirm_no"></button>
</dialog>
```
Add matching i18n keys (English + Hebrew) for `reset_wizard_button`,
`reset_wizard_confirm_text`, `reset_wizard_confirm_yes`,
`reset_wizard_confirm_no`, and a generic `err_no_session` (used by Task 6's
`"no_session"` reason — "Your session isn't ready for this step; please
complete the earlier steps first.").

```javascript
async function resetWizard() {
  await fetch("/api/session/reset", {method: "POST"});
  location.reload();
}

document.getElementById("wizard-reset-button").addEventListener("click", () => {
  document.getElementById("wizard-reset-confirm-dialog").showModal();
});
document.getElementById("wizard-reset-confirm-yes").addEventListener("click", resetWizard);
document.getElementById("wizard-reset-confirm-no").addEventListener("click", () => {
  document.getElementById("wizard-reset-confirm-dialog").close();
});
```

- [ ] **Step 2: Replace `restoreFromSession()`'s sessionStorage reads with a `GET /api/session` call**

Read the current full `restoreFromSession()` (starts ~line 2622) plus
`STORAGE_KEYS`/`readStoredGithubApp`/`readStoredSupabase`/
`readStoredRenderService` and every other place a frame's *credential* is
read out of `sessionStorage` on load. Replace the whole restore path with:

```javascript
async function restoreFromSession() {
  let body;
  try {
    const resp = await fetch("/api/session");
    body = await resp.json();
  } catch (err) {
    return;  // treat a network failure on load like a fresh visitor
  }
  const frames = body.frames || {};
  if (frames["render"]) completeFrame("render-key", "restored", null);
  if (frames["render"] && frames["render"].display.service_url) {
    completeFrame("render-service", "url_prefix", frames["render"].display.service_url);
  }
  if (frames["dashboard_auth"]) completeFrame("dashboard-auth", "restored", null);
  if (frames["github_app"]) completeFrame("github-app", "restored", null);
  if (frames["supabase"]) completeFrame("supabase", "project_prefix", frames["supabase"].display.name);
  if (frames["llm_provider"]) {
    completeFrame("llm-provider", "provider_prefix", LLM_PROVIDER_LABEL_KEY[frames["llm_provider"].display.provider] || frames["llm_provider"].display.provider);
  }
  if (frames["uptime_pinger"]) completeFrame("uptime-pinger", "restored", null);
}
```

(Match `completeFrame`'s actual current parameter shape/i18n-key
convention exactly — read its definition, ~line 990, before writing this;
the snippet above is illustrative of which frames map to which display
field, not a verbatim final version.)

Delete `STORAGE_KEYS`'s credential-holding entries and
`readStoredGithubApp`/`readStoredSupabase`/`readStoredRenderService` —
replace every remaining read site (e.g. anywhere a submit handler currently
reads a credential back out of `sessionStorage` to send it in a request
body) either by removing it (the credential no longer needs re-sending,
per Tasks 6-11's endpoints reading from the session instead) or, where a
frame's own in-progress form state genuinely needs to survive a same-page
re-render (not a reload), keeping an in-memory JS variable instead of
`sessionStorage`.

- [ ] **Step 3: Write the tests**

Follow `test_onboarding_page.py`'s existing style (function-boundary string
slicing + `body.count(...)` checks). Add:

```python
def test_reset_button_calls_session_reset_endpoint():
    body = index_html()
    fn = _extract_function(body, "async function resetWizard")
    assert 'fetch("/api/session/reset"' in fn
    assert "location.reload()" in fn


def test_restore_from_session_calls_get_api_session():
    body = index_html()
    fn = _extract_function(body, "async function restoreFromSession")
    assert 'fetch("/api/session")' in fn


def test_no_credential_is_ever_written_to_sessionstorage():
    """The invariant this whole plan exists to establish on the frontend
    side: sessionStorage may hold UI state only, never a credential."""
    body = index_html()
    # Adjust this allowlist to whatever non-secret keys legitimately remain
    # (theme, language, which-frame-is-open) after Step 2's deletions.
    for forbidden in ("githubApp", "supabase", "renderService", "llmProvider", "dashboardAuth"):
        assert f'STORAGE_KEYS["{forbidden}' not in body or forbidden not in STORAGE_KEYS_ALLOWED_AFTER_THIS_PLAN
```

(The exact allowlist/assertion shape depends on what Step 2 actually leaves
in `STORAGE_KEYS` — write this test against the real post-edit file rather
than guessing the final key set here.)

- [ ] **Step 4: Run the tests**

Run: `uv run pytest onboarding/tests/test_onboarding_page.py -v`
Expected: all PASS. Expect several existing tests referencing removed
`sessionStorage` keys/functions to need deletion or rewrite — read each
failure and judge whether it's testing removed behavior (delete the test)
or behavior that moved (rewrite it against the new location).

- [ ] **Step 5: Commit**

```bash
git add onboarding/static/index.html onboarding/tests/test_onboarding_page.py
git commit -m "onboarding: restore-on-load via GET /api/session, add Start-over control"
```

---

### Task 13: Frontend — wire the new confirm endpoints, remove the four push functions

**Files:**
- Modify: `onboarding/static/index.html`
- Test: `onboarding/tests/test_onboarding_page.py`

- [ ] **Step 1: Delete the four push functions and their call sites**

Delete `pushLlmProviderToRenderService`, `pushDashboardAuthToRenderService`,
`pushGithubAppToRenderService`, `pushSupabaseToRenderService` entirely, and
every `await push...ToRenderService(...)` call site. Each frame's
completion handler now just calls its frame's own confirm/validate
endpoint (already returning a verdict) and then `completeFrame(...)` — no
separate push step.

- [ ] **Step 2: `confirmLlmProviderModel()` calls `/api/llm/confirm`**

```javascript
async function confirmLlmProviderModel() {
  const select = document.getElementById("llm-provider-model-select");
  const model = select.value;
  if (!model || !pendingLlmProviderCredential) {
    llmProviderError("err_llm_no_model_selected");
    return;
  }
  const continueBtn = document.getElementById("llm-provider-continue-submit");
  continueBtn.disabled = true;
  const credentialValue = pendingLlmProviderCredential.provider === "vertex"
    ? pendingLlmProviderCredential.gcp_service_account_key_b64
    : pendingLlmProviderCredential.api_key;
  let resp, body;
  try {
    resp = await fetch("/api/llm/confirm", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({provider: pendingLlmProviderCredential.provider, credential_value: credentialValue, model}),
    });
    body = await resp.json();
  } catch (err) {
    continueBtn.disabled = false;
    llmProviderError("err_network");
    return;
  }
  if (!body.valid) {
    continueBtn.disabled = false;
    llmProviderErrorForReason(body.reason);
    return;
  }
  completeFrame("llm-provider", "provider_prefix", LLM_PROVIDER_LABEL_KEY[pendingLlmProviderCredential.provider] || pendingLlmProviderCredential.provider);
}
```

- [ ] **Step 3: `submitDashboardAuth()` calls `/api/dashboard-auth/confirm`**

Read the current function (~line 1588) in full; replace its call to
`pushDashboardAuthToRenderService` with a fetch to `/api/dashboard-auth/confirm`
carrying `{username, password, session_secret: sessionSecret}`, gating
`completeFrame("dashboard-auth", ...)` on `body.valid` the same way every
other frame's submit handler already gates completion on its own
endpoint's verdict.

- [ ] **Step 4: `validateGithubApp()` sends `webhook_secret` in its payload**

Read the current function; add `webhook_secret: <the wizard-generated
value already held from `ensureGithubAppWebhookSecret()`>` to the JSON body
sent to `/api/github/validate-app`.

- [ ] **Step 5: `fetchSupabaseConnectionInfo()` drops the client-side connection-string assembly**

Read the current function (~line 2583). Since `/api/supabase/connection-info`
(Task 8) no longer returns `db_user`/`db_host`/`db_port`/`db_name`, delete
the client-side `stored.connection_string = ...` assembly and the
`pushSupabaseToRenderService(stored)` call entirely — the endpoint's
`{"valid": true}` alone is enough to call `completeFrame("supabase", ...)`.

- [ ] **Step 6: Write the tests**

```python
def test_confirm_llm_provider_model_leaves_the_page_exactly_once():
    body = index_html()
    assert body.count('fetch("/api/llm/confirm"') == 1


def test_submit_dashboard_auth_leaves_the_page_exactly_once():
    body = index_html()
    assert body.count('fetch("/api/dashboard-auth/confirm"') == 1


def test_validate_github_app_payload_includes_webhook_secret():
    body = index_html()
    fn = _extract_function(body, "async function validateGithubApp")
    assert "webhook_secret" in fn


def test_push_to_render_functions_are_gone():
    body = index_html()
    for name in (
        "pushLlmProviderToRenderService", "pushDashboardAuthToRenderService",
        "pushGithubAppToRenderService", "pushSupabaseToRenderService",
    ):
        assert f"function {name}" not in body
```

- [ ] **Step 7: Run the tests**

Run: `uv run pytest onboarding/tests/test_onboarding_page.py -v`
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add onboarding/static/index.html onboarding/tests/test_onboarding_page.py
git commit -m "onboarding: wire llm/dashboard-auth confirm endpoints, drop per-frame render push"
```

---

### Task 14: Frontend — Supabase same-tab redirect, remove popup/BroadcastChannel

**Files:**
- Modify: `onboarding/static/index.html`
- Test: `onboarding/tests/test_onboarding_page.py`

- [ ] **Step 1: Read the current Supabase connect flow in full**

Read `connectSupabase()` (~line 2059), `cleanupSupabaseOauthPopup()`,
`handleSupabaseOauthChannelMessage()`, `completeSupabaseOAuth()`,
`handleSupabaseOauthCallback()`, `callSupabaseRelay()`,
`fetchSupabaseOrganizations()`, `kickOffProjectCreation()`, and
`generateDbPassword()` in full before editing — Task 8 was already
corrected against a live read of this flow (only `name` crosses the
redirect; org-picking and `db_pass` generation both happen after), but
confirm nothing drifted since.

- [ ] **Step 2: Replace with a plain same-tab redirect**

Delete `SUPABASE_OAUTH_CHANNEL_NAME`, `supabaseOauthPopup`,
`supabaseOauthPopupWatcher`, `supabaseOauthChannel`,
`cleanupSupabaseOauthPopup`, `handleSupabaseOauthChannelMessage`, and every
PKCE-generation line in `connectSupabase` (verifier/challenge/state are now
generated server-side in `/api/supabase/connect`). Also delete
`generateDbPassword()` — `db_pass` is now generated server-side inside
`/api/supabase/create-project` (Task 8 step 3). Replace `connectSupabase`
with:

```javascript
async function connectSupabase(name) {
  setFrameStatus("supabase", "checking");
  document.getElementById("supabase-connect-submit").disabled = true;
  let resp, body;
  try {
    resp = await fetch("/api/supabase/connect", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name}),
    });
    body = await resp.json();
  } catch (err) {
    setFrameStatus("supabase", "ready");
    document.getElementById("supabase-connect-submit").disabled = false;
    supabaseError("err_network");
    return;
  }
  if (!body.valid) {
    setFrameStatus("supabase", "ready");
    document.getElementById("supabase-connect-submit").disabled = false;
    supabaseErrorForReason(body.reason);
    return;
  }
  location.href = body.authorize_url;
}
```

Delete `handleSupabaseOauthCallback()` and `completeSupabaseOAuth()`
entirely — the callback is now handled purely server-side (Task 8's
rewritten `/oauth/supabase/callback` route), so no client-side code runs
for it at all. Remove whatever call site invoked
`handleSupabaseOauthCallback()` on page load (likely inside
`DOMContentLoaded` — check for a `location.pathname ===
"/oauth/supabase/callback"` branch and delete it, since that route is now
server-rendered-and-redirected before the SPA's own JS ever runs against
that path in a meaningful way).

Delete `SUPABASE_PENDING_KEY` and any remaining
`sessionStorage.setItem/getItem(SUPABASE_PENDING_KEY, ...)` calls.

- [ ] **Step 3: Update `fetchSupabaseOrganizations()`/`kickOffProjectCreation()`, drop `callSupabaseRelay`'s token-refresh retry**

`callSupabaseRelay()` currently wraps `fetch()` with retry-on-401-via-
refresh logic, because the browser used to hold `access_token` and had to
manage its own refresh. That token never reaches the browser anymore
(Task 8), so this retry logic has nothing to refresh client-side — read
`callSupabaseRelay()` in full and simplify it to a plain `fetch()` +
JSON-parse helper (or delete it and inline `fetch()` at each of its two
call sites, whichever reads cleaner given what else is in that helper).
Update its two callers:

```javascript
async function fetchSupabaseOrganizations() {
  const body = await callSupabaseRelay("/api/supabase/list-organizations", {});
  // ... unchanged from here down — list-organizations now reads
  // access_token from the session cookie, no body needed, but the call
  // site's shape (empty {} payload) can stay if callSupabaseRelay still
  // JSON-encodes a body; drop the {} argument if it was simplified away.
}

async function kickOffProjectCreation(organizationSlug) {
  const body = await callSupabaseRelay("/api/supabase/create-project", {
    organization_slug: organizationSlug,
  });
  if (!body.valid) {
    supabaseErrorForReason(body.reason, body.message);
    return false;
  }
  // No more re-read-after-await / db_pass/organization_slug bookkeeping
  // here -- the server already persisted ref/db_pass/organization_slug to
  // the session in the same call. Only local UI state (ref, status, for
  // driving the polling UI) needs to be kept, not written to
  // sessionStorage as a credential.
  ...
}
```

Delete `readStoredSupabase()`'s remaining use for anything beyond local
polling/display state, and any leftover `stored.db_pass`/
`stored.organization_slug` bookkeeping this task's earlier steps didn't
already remove — read the full function bodies live and reconcile, since
this plan's snippets are illustrative of the required end state, not a
verbatim diff.

- [ ] **Step 4: Write the tests**

```python
def test_connect_supabase_does_a_plain_same_tab_redirect():
    body = index_html()
    fn = _extract_function(body, "async function connectSupabase")
    assert "location.href = body.authorize_url" in fn
    assert "window.open" not in fn


def test_popup_and_broadcast_channel_machinery_is_gone():
    body = index_html()
    for name in (
        "supabaseOauthPopup", "supabaseOauthChannel", "BroadcastChannel",
        "cleanupSupabaseOauthPopup", "handleSupabaseOauthChannelMessage",
        "completeSupabaseOAuth", "handleSupabaseOauthCallback",
    ):
        assert name not in body


def test_no_pkce_material_is_generated_client_side_anymore():
    body = index_html()
    fn = _extract_function(body, "async function connectSupabase")
    assert "code_verifier" not in fn and "codeChallenge" not in fn
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest onboarding/tests/test_onboarding_page.py -v`
Expected: all PASS. Delete any now-orphaned tests that asserted the old
popup/BroadcastChannel behavior (they test removed code, not a regression).

- [ ] **Step 6: Commit**

```bash
git add onboarding/static/index.html onboarding/tests/test_onboarding_page.py
git commit -m "onboarding: replace Supabase OAuth popup/BroadcastChannel with a plain redirect"
```

---

### Task 15: Update `onboarding/CLAUDE.md` and `ISSUES.md`

**Files:**
- Modify: `onboarding/CLAUDE.md`, `ISSUES.md`

- [ ] **Step 1: Rewrite the stateless-relay invariant section**

Replace "The invariant this service exists to protect" section (the
"stateless relay, no database, no session store" text) with a description
of the new architecture: server-side session via `wizard_sessions`,
cookie-identified, application-encrypted, TTL-bound. Point at the new spec
doc. Update every rule elsewhere in the file that assumed statelessness
(e.g. the credential-goes-to-sessionStorage-never-localStorage rule still
holds for what little remains in `sessionStorage`, but the framing that a
credential's *only* home is the browser needs correcting).

- [ ] **Step 2: Update sub-project 3's section**

Replace its popup/`BroadcastChannel`/`window.opener` description with the
plain-redirect design from Task 14, and remove the "do not reintroduce a
window.opener check" note (no longer applicable — there's no popup at all).

- [ ] **Step 3: Log this as a completed architecture change in `ISSUES.md`**

Add an entry (following the file's existing incident-entry format)
documenting that the stateless-relay invariant was found fragile against
mobile `sessionStorage` loss and was deliberately replaced, referencing the
new spec and this plan.

- [ ] **Step 4: Commit**

```bash
git add onboarding/CLAUDE.md ISSUES.md
git commit -m "onboarding: document the server-side session architecture in CLAUDE.md/ISSUES.md"
```

---

### Task 16: Full suite, lint, final review

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest -v`
Expected: all PASS. Fix anything red before proceeding — do not skip or
xfail a failure because it "looks small."

- [ ] **Step 2: Run ruff**

Run: `uv run ruff check .`
Expected: no errors. Fix anything found.

- [ ] **Step 3: Manually re-read the new/changed router.py end to end**

Confirm every endpoint that reads a credential from the session also
handles the "session/frame missing" case with the `"no_session"` shape
(no endpoint should raise a raw exception or 500 on a missing/incomplete
session) — this is the kind of cross-cutting consistency check a
per-task review naturally misses, per this project's own plan-execution
hygiene notes about final whole-branch review.

- [ ] **Step 4: Update `onboarding/.env.example`'s already-added entries if anything drifted**

The `DATABASE_URL`/`ONBOARDING_SESSION_ENCRYPTION_KEY` entries were added
before this plan was written — confirm their description still matches
what was actually built (in particular, the schema/TTL described there
should match `session_store.py`'s real constants).

- [ ] **Step 5: Report completion**

Summarize what changed, what's left untested (if anything), and confirm
with the user before considering this done.
