# Supabase First-Deploy Hardening + First Hosted Run Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a first-time Supabase deploy legible and repeatable, then actually
run the app on Render + Supabase end-to-end for the first time.

**Architecture:** Phase 1 is a repo change: a diagnostic rewrite of the one
failure `init_pool()` reports badly, plus documentation that stops naming one
person's GitHub account and stops presenting the retired Cloudflare Tunnel as a
live path. Phase 2 is an operational run in pure observation mode — provision
Supabase and Render, verify the app bootstraps its own schema, then exercise the
happy path and the two demo resilience beats.

**Tech Stack:** FastAPI, psycopg3 + psycopg_pool, pytest, `gh` CLI, Render
(Docker runtime), Supabase Postgres (Supavisor session pooler), UptimeRobot.

**Spec:** `docs/superpowers/specs/2026-08-05-supabase-first-deploy-hardening-and-first-hosted-run-design.md`

## Global Constraints

- **No secret is ever logged.** `settings.database_url` carries a password;
  error messages describe failure shapes instead of interpolating it. Applies to
  `RENDER_API_KEY` too.
- **Startup fails loudly** if Postgres is unreachable (design spec §11). The
  Phase 1 change improves the *message*, never the semantics.
- **Phase 2 makes no repo changes** between Task 6 and the end of Task 12.
  Correcting operator-side configuration is executing the run; changing code is
  not. A code-level blocker stops the run, gets recorded, and resumes after a
  separate fix.
- **Segment C (Task 12) runs exactly once.** If Groq returns 403 rather than
  429, stop calling it immediately and record it (CLAUDE.md's LLM hygiene rules).
- **Render env vars: only the single-key add-or-update endpoint.**
  `PUT /v1/services/{id}/env-vars` replaces the entire list and would delete
  `DATABASE_URL` and the App key.
- **No tool ever runs SQL or DDL against the database.** `tickets` must be
  created by the app's own boot or the run has proven nothing. Verification
  queries are read-only `SELECT`s.
- **`RENDER_API_KEY` is optional.** Every step has a documented fallback; no
  step's pass criteria change when it is absent.
- Async throughout; one-purpose modules; existing conventions followed.

**Note on commit grouping:** the spec (§8) groups Phase 1 into three concerns.
This plan lands them as five commits — hardening splits into code+tests,
regression guards, and docs — because each carries its own test cycle and is
independently rejectable. The concerns and their contents are unchanged.

---

## Phase 1 — Repo changes

### Task 1: Diagnostic rewrite of `init_pool()`'s first-connection failure

**Files:**
- Modify: `app/queue/store.py:16` (import), `app/queue/store.py:41` (new
  constant), `app/queue/store.py:67-75` (`init_pool`)
- Create: `tests/test_store_init.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `store._POOL_TIMEOUT_SECONDS: int` (module constant, monkeypatchable
  by tests) and `init_pool() -> None` raising `RuntimeError` (with
  `__cause__` a `psycopg_pool.PoolTimeout`) when the first connection never
  succeeds. Task 2's lifespan test relies on both.

**Background the implementer needs:**

Measured behavior, so you do not rediscover it: `ConnectionPool(..., open=True)`
does **not** wait for a connection at construction, and does **not** validate the
conninfo eagerly. Construction always succeeds. The failure surfaces at the first
`_pool.connection()` as `psycopg_pool.PoolTimeout` after the pool's `timeout`
(default 30s). The driver's real error is logged separately by psycopg_pool as
`error connecting in 'pool-1': …` at WARNING. `PoolTimeout` subclasses
`psycopg.OperationalError`, so catch it by name, never by `psycopg.Error`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_store_init.py`. This file deliberately does **not** use the
shared `db` fixture — it points the store at a dead port and never needs a real
database. `127.0.0.1:1` refuses immediately, so these run in about a second.

```python
"""init_pool()'s failure path: an unreachable Postgres must fail loudly with an
actionable message, must never leak the connection string, and must not rewrite
errors that are not connection timeouts.

Deliberately does NOT use the shared ``db`` fixture (tests/conftest.py) — these
tests point the store at a dead port (127.0.0.1:1 refuses immediately) and never
need a real database.
"""
from __future__ import annotations

import contextlib

import psycopg
import pytest
from psycopg_pool import PoolTimeout

from app.config import settings
from app.queue import store

SENTINEL_PASSWORD = "sentinel-pw-must-not-appear"
DEAD_URL = (
    f"postgresql://someuser:{SENTINEL_PASSWORD}@127.0.0.1:1/postgres?connect_timeout=1"
)


@pytest.fixture(autouse=True)
def _closed_pool():
    """No module-level pool before or after; these tests own it entirely."""
    store.close_pool()
    yield
    store.close_pool()


def test_init_pool_unreachable_db_raises_actionable_runtime_error(monkeypatch):
    monkeypatch.setattr(settings, "database_url", DEAD_URL)
    monkeypatch.setattr(store, "_POOL_TIMEOUT_SECONDS", 1)

    with pytest.raises(RuntimeError) as excinfo:
        store.init_pool()

    message = str(excinfo.value)
    assert "provisioning" in message
    assert "postgres.<project-ref>" in message
    assert "percent-encode" in message.lower()
    # The driver's own error is preserved as the cause, not discarded.
    assert isinstance(excinfo.value.__cause__, PoolTimeout)


def test_init_pool_error_never_leaks_the_connection_string(monkeypatch):
    """CLAUDE.md: no secret is ever logged. database_url carries the password, so
    the actionable message must describe failure shapes, never interpolate it."""
    monkeypatch.setattr(settings, "database_url", DEAD_URL)
    monkeypatch.setattr(store, "_POOL_TIMEOUT_SECONDS", 1)

    with pytest.raises(RuntimeError) as excinfo:
        store.init_pool()

    rendered = str(excinfo.value) + repr(excinfo.value.args)
    assert SENTINEL_PASSWORD not in rendered
    assert DEAD_URL not in rendered


def test_init_pool_does_not_mask_a_non_timeout_failure(monkeypatch):
    """Only PoolTimeout gets the friendly rewrite. A privilege error on
    CREATE TABLE must surface as itself -- reporting it as "still provisioning"
    would send the operator chasing the wrong problem.

    A malformed conninfo cannot be used to test this: ConnectionPool constructs
    fine and the failure still arrives as PoolTimeout (and PoolTimeout subclasses
    psycopg.OperationalError). So inject the failure at the DDL step instead.
    """

    class _FakeConn:
        def execute(self, *args, **kwargs):
            raise psycopg.errors.InsufficientPrivilege(
                "permission denied for schema public"
            )

    class _FakePool:
        def __init__(self, *args, **kwargs):
            pass

        @contextlib.contextmanager
        def connection(self):
            yield _FakeConn()

        def close(self):
            pass

    monkeypatch.setattr(store, "ConnectionPool", _FakePool)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        store.init_pool()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_store_init.py -v`

Expected: the first two FAIL — `AttributeError` on
`store._POOL_TIMEOUT_SECONDS` from `monkeypatch.setattr` (the constant does not
exist yet). The third FAILs too: without the wrapper, `init_pool` currently
passes `InsufficientPrivilege` through, so it may already pass — if it does,
that is fine and expected; it is a guard against the *change* you are about to
make, so it must still pass in Step 4.

- [ ] **Step 3: Implement the change**

In `app/queue/store.py`, extend the import on line 16:

```python
from psycopg_pool import ConnectionPool, PoolTimeout
```

Add below `_pool: ConnectionPool | None = None` (line 41):

```python
# Explicit rather than relying on psycopg_pool's default (same value), so a test
# can shrink it without waiting 30s for a connection that will never open.
_POOL_TIMEOUT_SECONDS = 30

_FIRST_CONNECT_HELP = (
    "could not open a Postgres connection at startup within {timeout:.0f}s. "
    "On a first deploy this is nearly always one of:\n"
    "  1. the database is still provisioning -- wait until it reports ready, "
    "then deploy again (a failed deploy is not retried automatically);\n"
    "  2. a pooler connection string whose username is missing its project "
    "suffix -- it must look like postgres.<project-ref>, not plain postgres;\n"
    "  3. a password containing characters that must be percent-encoded "
    "(@ # / ?).\n"
    "The driver's own error is logged above as \"error connecting in 'pool-1'\"."
)
```

Replace `init_pool()` (lines 67-75) with:

```python
def init_pool() -> None:
    """Open the connection pool (if not already) and ensure the schema. Idempotent.

    A PoolTimeout here means the very first connection never succeeded, which on a
    hosted first deploy is nearly always a provisioning or connection-string
    problem rather than a transient blip. Re-raise it as a RuntimeError carrying
    the likely causes: the bare PoolTimeout reads like a hang, and the driver's
    real error is tens of lines further up the log. Startup still fails loudly
    (design spec section 11) -- RuntimeError matches _require_pool()'s convention
    and app/main.py's lifespan already documents it as the fail-loudly path. The
    message never includes settings.database_url, which carries the password.
    """
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
            _FIRST_CONNECT_HELP.format(timeout=_POOL_TIMEOUT_SECONDS)
        ) from exc
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_store_init.py -v`
Expected: 3 passed, in roughly 2-3 seconds.

- [ ] **Step 5: Run the full suite and the linter**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all green. Docker must be running for the testcontainers-backed tests
(see SETUP.md §2c).

- [ ] **Step 6: Commit**

```bash
git add app/queue/store.py tests/test_store_init.py
git commit -m "fix(queue): report a first-boot connection failure actionably

A PoolTimeout from init_pool()'s first connection reads like a hang -- the
driver's real cause is logged tens of lines earlier as \"error connecting in
'pool-1'\". Re-raise it as a RuntimeError naming the three real first-deploy
causes (project still provisioning, pooler username missing its project suffix,
password not percent-encoded), preserving the PoolTimeout as __cause__.

Semantics are unchanged: startup still fails loudly per the hosting-migration
design section 11, and the message never interpolates database_url."
```

---

### Task 2: Regression guards — schema shape and loud startup

**Files:**
- Modify: `tests/test_queue_store.py` (append one test)
- Modify: `tests/test_main_lifespan.py` (append one test)

**Interfaces:**
- Consumes: `store._POOL_TIMEOUT_SECONDS` and `init_pool()`'s `RuntimeError`
  from Task 1; `store.Ticket` (existing dataclass, `app/queue/store.py:44-60`);
  the `db_query` fixture (`tests/conftest.py:77`) which returns a list of tuples.
- Produces: nothing consumed downstream.

**Why these two:** `_row_to_ticket` does `Ticket(**row)`, so any drift between
`_SCHEMA` and the `Ticket` dataclass is a runtime `TypeError` in production with
no test catching it. And Task 1 touched the startup failure path, which the
design deliberately wants loud — worth pinning so a future "make startup more
forgiving" change has to argue with a test.

- [ ] **Step 1: Write the failing schema test**

Append to `tests/test_queue_store.py`:

```python
def test_schema_columns_match_the_ticket_dataclass(db_query):
    """_row_to_ticket does Ticket(**row), so _SCHEMA and the dataclass must agree
    exactly -- drift is a production TypeError. This also makes the hosted run's
    column check (plan Task 9) a machine-checked invariant rather than a number
    written down in a doc."""
    rows = db_query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'tickets'"
    )
    assert {row[0] for row in rows} == set(store.Ticket.__dataclass_fields__)
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_queue_store.py::test_schema_columns_match_the_ticket_dataclass -v`
Expected: PASS immediately. This is a characterization test — it documents an
invariant that already holds. If it FAILS, stop: `_SCHEMA` and `Ticket` have
already drifted and that is a real bug to report, not something to paper over.

- [ ] **Step 3: Write the failing lifespan test**

Append to `tests/test_main_lifespan.py`. Note the module's autouse `_env(db)`
fixture (line 20) has already opened a pool against the test Postgres, so close
it and repoint before driving the lifespan:

```python
async def test_lifespan_fails_loudly_when_postgres_is_unreachable(monkeypatch):
    """Design spec section 11: "If Postgres is unreachable at boot, startup fails
    loudly (correct)". Guards that init_pool()'s diagnostic rewrite did not soften
    that into a warning, and that no dispatcher task is left running."""
    monkeypatch.setattr(dispatcher, "run_forever", _hang_forever)
    monkeypatch.setattr(settings, "github_app_installation_id", 12345)

    # The autouse _env(db) fixture already opened a pool on the test Postgres.
    store.close_pool()
    monkeypatch.setattr(
        settings, "database_url", "postgresql://u:p@127.0.0.1:1/postgres?connect_timeout=1"
    )
    monkeypatch.setattr(store, "_POOL_TIMEOUT_SECONDS", 1)

    created_tasks = []
    real_create_task = asyncio.create_task

    def _spy_create_task(coro, *args, **kwargs):
        task = real_create_task(coro, *args, **kwargs)
        created_tasks.append(task)
        return task

    monkeypatch.setattr(main.asyncio, "create_task", _spy_create_task)

    with pytest.raises(RuntimeError):
        async with main.lifespan(main.app):
            pass

    # init_pool() raised before create_task was reached: no leaked dispatcher.
    assert created_tasks == []
```

- [ ] **Step 4: Run it**

Run: `uv run pytest tests/test_main_lifespan.py -v`
Expected: 4 passed. The new test takes ~1s (the shrunk pool timeout).

- [ ] **Step 5: Run the full suite and the linter**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add tests/test_queue_store.py tests/test_main_lifespan.py
git commit -m "test: pin the schema/dataclass invariant and loud startup

_row_to_ticket does Ticket(**row), so drift between _SCHEMA and the Ticket
dataclass is a production TypeError that nothing caught. And now that
init_pool() rewrites its first-connection failure, pin that startup still
raises and leaves no dispatcher task behind."
```

---

### Task 3: SETUP.md hardening — pooler string, App ID, verification

**Files:**
- Modify: `SETUP.md:6-28` (§1, App ID guidance), `SETUP.md:171-179` (§3.1),
  `SETUP.md:180-199` (§3.2)
- Modify: `.env.example:11-20`

**Interfaces:** none — documentation only.

**Background:** the current §3.1 example (`SETUP.md:177`) is the wrong *shape*
for a pooler connection string. It shows user `postgres` and
`[host].pooler.supabase.com`; the real session-pooler string uses
`postgres.<project-ref>` as the username and a region-varying
`aws-<n>-<region>.pooler.supabase.com` host. Reconstructing from the current
example produces `FATAL: Tenant or user not found`.

- [ ] **Step 1: Rewrite §3.1 (`SETUP.md:171-179`)**

Replace the four numbered steps with:

```markdown
### 3.1 Supabase setup

1. Create a Supabase project at https://supabase.com
2. **Wait until the dashboard reports the project ready** (~2 minutes). A
   connection attempt against a still-provisioning project fails, and Render
   does not retry a failed deploy — see §3.2's troubleshooting note.
3. Open **Connect** (or Project Settings → Database) and copy the
   **Session-mode pooler** connection string — port **5432**, not 6543.
   - Shape: `postgresql://postgres.<project-ref>:<password>@aws-<region>.pooler.supabase.com:5432/postgres`
   - **Copy it verbatim; do not retype or reconstruct it.** Both the
     `postgres.<project-ref>` username and the region-varying subdomain are
     project-specific, and either one wrong yields
     `FATAL: Tenant or user not found`.
   - If the password contains `@ # / ?`, percent-encode it — those characters
     terminate fields in a URI.
4. Set it as the `DATABASE_URL` env var (locally in `.env`, and in the Render
   dashboard per §3.2).
5. Optional hardening: libpq's default `sslmode=prefer` gets an encrypted
   connection but performs no certificate verification. For MITM protection use
   `sslmode=verify-full` together with Supabase's CA certificate. The app does
   not enforce this.
```

- [ ] **Step 2: Amend §3.2 (`SETUP.md:180-199`)**

Two changes to the existing env-var list, then two additions after it.

Change `GITHUB_APP_ID`'s entry from `(from .env)` to:

```markdown
   - `GITHUB_APP_ID`: the numeric App ID — see §1 for where to find it
```

Add to the same list:

```markdown
   - Do **not** set `GITHUB_APP_INSTALLATION_ID`. Leaving it unset is
     deliberate: the app auto-discovers it at boot from the App JWT.
   - `RENDER_API_KEY` is **not** a service env var. It is optional
     operator-local tooling (Account Settings → API Keys) that lets deploy
     scripts set env vars and read logs from your machine. Never add it to
     `render.yaml` and never give it to the service.
```

Add after step 5 (`Click **Deploy**`):

```markdown
6. **Verify before considering this step done:**
   - The deploy's logs end with uvicorn's `Application startup complete.`
   - `curl https://<your-service>.onrender.com/healthz` returns `{"status":"ok"}`.

**Troubleshooting the first deploy.** If it fails with
`error connecting in 'pool-1'` or a `RuntimeError` about the connection not
opening, the usual cause is a Supabase project that was not ready yet, or a
mistyped pooler string (§3.1). Render does **not** retry failed deploys
automatically, and a first deploy leaves no previous instance running — fix the
value and click **Manual Deploy**.
```

- [ ] **Step 3: Add App ID guidance to §1 (`SETUP.md:6-28`)**

Append to the §1 bullet list:

```markdown
- **Obtaining the App ID** (needed as `GITHUB_APP_ID`): open the App's settings
  at `https://github.com/settings/apps/<your-app-slug>` → **General** → **App
  ID**, near the top. The manifest-conversion flow above also returns it
  directly. Three IDs sit close together and only two are used here:
  - **App ID** → `GITHUB_APP_ID`. A short integer. `app/config.py` types it as
    `int`, so a non-numeric paste fails config validation at startup, and
    `app/github_app.py` reports "likely a bad `GITHUB_APP_ID`" on a 401.
  - **Installation ID** → `GITHUB_APP_INSTALLATION_ID`. **Optional** — the app
    auto-discovers it at boot when unset. Capture it manually via
    `GET /app/installations` (signed with a short-lived JWT) if you want it
    pinned.
  - **Client ID** — not used by this project at all. Easy to grab by mistake,
    since it sits on the same page.
```

- [ ] **Step 4: Annotate `.env.example`**

Change line 12 and add the optional Render entry. Replace lines 11-20 with:

```
# --- GitHub App ---
# Numeric App ID: App settings -> General -> "App ID". See SETUP.md §1, which
# also explains why this is not the Client ID or the Installation ID.
GITHUB_APP_ID=
# Optional: the app auto-discovers this at boot when left empty.
GITHUB_APP_INSTALLATION_ID=
# Local development: path to the downloaded private-key PEM (kept out of git; see .gitignore).
GITHUB_APP_PRIVATE_KEY_PATH=./github-app-private-key.pem
# Render/hosted: base64-encoded private key (set in dashboard, never committed).
GITHUB_APP_PRIVATE_KEY_B64=
GITHUB_WEBHOOK_SECRET=
# Target repository for pull requests (owner/repo format)
GITHUB_TARGET_REPO=
```

Append at the end of the file:

```
# --- Optional operator tooling (NOT used by the deployed service) ---
# Render API key (Account Settings -> API Keys). Optional: lets local scripts set
# service env vars and read deploy logs instead of doing it in the dashboard.
# Never add this to render.yaml. Unknown vars are ignored by app/config.py.
RENDER_API_KEY=
```

- [ ] **Step 5: Verify nothing else contradicts the new text**

Run: `grep -n "6543\|from .env\|sslmode" SETUP.md`
Expected: the only `6543` mention is §3.1's "not 6543"; no remaining
`(from .env)` for `GITHUB_APP_ID`.

Run: `uv run pytest tests/test_config.py -q`
Expected: PASS — `.env.example` is not read by tests, but this confirms the
config surface is untouched.

- [ ] **Step 6: Commit**

```bash
git add SETUP.md .env.example
git commit -m "docs: fix the Supabase pooler string and document the App ID

SETUP.md §3.1's example connection string was the wrong shape for a pooler --
plain 'postgres' as the username and a placeholder host -- which yields FATAL:
Tenant or user not found for anyone reconstructing it instead of copying the
dashboard value. Give the real session-pooler shape, tell readers to copy it
verbatim, note percent-encoding, and add the wait-for-ready step.

§3.2 gains a verification step and a troubleshooting note (Render does not
retry failed deploys), plus the deliberate omission of
GITHUB_APP_INSTALLATION_ID and the optional RENDER_API_KEY.

§1 was a past-tense record that never told a reader how to obtain their own App
ID; it now does, and distinguishes it from the Installation and Client IDs."
```

---

### Task 4: Repeatability — stop naming one account

**Files:**
- Modify: `scripts/seed_demo_pr.py:9`, `scripts/manual_verify_step3.py:4-5`
- Modify: `tests/test_github_app.py:21`
- Modify: `SETUP.md:16`, `SETUP.md:194`, `SETUP.md:217`, `SETUP.md:296` and the
  hardcoded `settings/apps/tov-pr-review-bot-testbed` URLs (lines ~216, ~237,
  ~295)
- Modify: `docs/superpowers/specs/2026-08-03-demo-plan-design.md:40`,
  `docs/superpowers/specs/2026-08-03-demo-plan-design.md:170`

**Interfaces:** none — docstrings, one test constant, and docs.

**Background:** the *code* is already correct. `scripts/seed_demo_pr.py:38`,
`scripts/manual_verify_step3.py:27`, and `scripts/demo_provider_swap.py:32` all
read `settings.github_target_repo`. Only prose still hardcodes an account, which
makes the procedure read as single-owner when it is not.

**Do not touch** `docs/2026-08-03-supabase-hosting-migration-handoff.md`,
`docs/superpowers/plans/2026-08-03-supabase-hosting-migration.md`, or
`docs/superpowers/specs/2026-08-03-supabase-hosting-migration-design.md` — those
are records of what was done at the time.

- [ ] **Step 1: Fix the script docstrings**

In `scripts/seed_demo_pr.py`, change line 9 from naming the repo to:

```
  1. Clones the configured test repo (GITHUB_TARGET_REPO) into a fresh temp
     directory via `gh repo clone`.
```

In `scripts/manual_verify_step3.py`, change lines 4-5 to:

```
external GitHub state: a real installation of the configured GitHub App, and a
real PR on the configured test repo (GITHUB_TARGET_REPO).
```

- [ ] **Step 2: Neutralize the test constant**

In `tests/test_github_app.py:21`:

```python
REPO_FULL_NAME = "test-owner/pr-review-bot-testbed"
```

This is a fake value in a `respx`-mocked test; every URL in the file is derived
from it, so nothing else needs changing.

- [ ] **Step 3: Run the affected tests**

Run: `uv run pytest tests/test_github_app.py -q`
Expected: PASS. If anything fails, a URL was hardcoded rather than derived from
`REPO_FULL_NAME` — fix it to derive.

- [ ] **Step 4: Genericize the docs**

In `SETUP.md`, replace the owner in lines 16, 194, 217, and 296 with
`<your-user>` (e.g. `<your-user>/pr-review-bot-testbed`), and replace hardcoded
`https://github.com/settings/apps/tov-pr-review-bot-testbed` URLs with
`https://github.com/settings/apps/<your-app-slug>`.

In `docs/superpowers/specs/2026-08-03-demo-plan-design.md`, do the same at line
40 and change line 170's checklist item to:

```markdown
8. Confirm `gh auth status` shows you logged in as the account that owns the
   `GITHUB_TARGET_REPO` testbed.
```

- [ ] **Step 5: Verify only records still name the account**

Run: `grep -rn "TovTechOrg\|tov-pr-review-bot-testbed" --include="*.py" --include="*.md" . | grep -v "^./.venv"`
Expected: hits only in `docs/2026-08-03-supabase-hosting-migration-handoff.md`,
`docs/superpowers/plans/2026-08-03-supabase-hosting-migration.md`, and
`docs/superpowers/specs/2026-08-03-supabase-hosting-migration-design.md`.

Run: `uv run pytest -q && uv run ruff check .`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add scripts/seed_demo_pr.py scripts/manual_verify_step3.py tests/test_github_app.py SETUP.md docs/superpowers/specs/2026-08-03-demo-plan-design.md
git commit -m "docs: stop hardcoding one GitHub account in the setup procedure

The scripts already read settings.github_target_repo, but docstrings, a test
fixture, SETUP.md and the demo plan still named a specific account and App slug,
which makes a repeatable procedure read as single-owner. Historical records under
docs/2026-08-03-* are left as-is."
```

---

### Task 5: Retire the remaining Cloudflare Tunnel references

**Files:**
- Modify: `CLAUDE.md:9`, `CLAUDE.md:25`
- Modify: `README.md:88-101`, `README.md:138-141`, `README.md:179-181`,
  `README.md:207`
- Modify: `SETUP.md:20-24`, delete `SETUP.md:251-261` (§3.6), fix
  `SETUP.md:298-300`
- Modify: `SPEC.md:292-293`, `SPEC.md:319-320`
- Modify: `cost.md:15-21`
- Modify: `docs/superpowers/specs/2026-08-03-demo-plan-design.md:27-32`,
  `:55-59`, `:147`, `:155-169`

**Interfaces:** none — documentation only.

**Background:** there is **no dependency to remove.** `cloudflared` appears in no
`pyproject.toml`, `Dockerfile`, or `uv.lock` entry — it was always an external
binary invoked by hand. This task is entirely prose.

**Keep as historical record** (rewriting these would falsify what happened):
`SETUP.md:284,287`'s PR #3 rehearsal row, which genuinely *was* delivered over a
quick tunnel, and the `docs/2026-08-03-*` migration documents.

- [ ] **Step 1: Fix CLAUDE.md — the authoritative file**

Line 9: replace `Cloudflare Tunnel (public URL, not localhost).` with
`Render (stable public URL, not localhost), with the queue in Supabase Postgres.`

Line 25: replace the `**Deploy**` bullet with:

```markdown
- **Deploy**: Docker on Render (free tier) + Supabase Postgres, kept warm by a
  free external pinger. See `cost.md` for the alternatives that were weighed.
```

- [ ] **Step 2: Fix README.md**

Delete the whole `### Local testing with a tunnel (optional)` section
(lines 88-101).

Replace the live-rehearsal steps 1-2 (lines 138-141) with:

```markdown
1. Ensure the Render service is deployed and `/healthz` returns 200.
2. The GitHub App's webhook URL is already the stable Render URL — no per-run
   update needed (`uv run python -m bot.scripts.deploy` sets it once).
```

Delete the `- **Local tunnel testing (optional)** — …` bullet (lines 179-181).

Line 207: change `Demo runs at **$0** (Groq + Cloudflare free tiers).` to
`Demo runs at **$0** (Groq + Render + Supabase free tiers).`

- [ ] **Step 3: Fix SETUP.md**

Lines 20-24 (§1's webhook bullet): replace with:

```markdown
- **Webhook URL**: set to the deployed service's `<public-url>/webhook`. This is
  stable and set once by `uv run python -m bot.scripts.deploy` (§3.4) — it does not
  need re-editing between runs.
```

Delete §3.6 entirely (lines 251-261), including the
`gcloud` and `cloudflared` were installed via `winget` note.

In the redo-from-scratch notes, delete the tunnel-start bullet (lines 298-300).

- [ ] **Step 4: Fix SPEC.md**

Lines 292-293 (§10 step 0's prerequisite): replace the **Cloudflare Tunnel**
bullet with:

```markdown
   - **Hosting**: create a Supabase project (Session-mode pooler URL as
     `DATABASE_URL`) and a Render service from `render.yaml`; the Render URL is
     the stable public webhook target.
```

Lines 319-320 (§11 verification): replace the `cloudflared tunnel` bullet with:

```markdown
- The deployed Render URL is set as the GitHub App webhook; a manual GitHub
  "Redeliver" of a `pull_request` event produces a comment.
```

- [ ] **Step 5: Reword cost.md's comparison**

Keep **both** table rows — they are the documented rationale for choosing Render
+ Supabase, not stale instructions. Reword the "Choice rationale" paragraph
(lines ~19-21) to close off the tunnel as a current option:

```markdown
**Choice rationale:** the chosen path is $0 on free tiers with stable public URLs
and durable queue state via Postgres. The keep-warm pinger (~$0, free service)
mitigates Render and Supabase idle spin-down, keeping both within the demo's 15s
responsiveness target. The two lower rows were evaluated and **rejected**, and
are listed here only to document that comparison: the Cloudflare Container loses
queue state on redeploy, and the local-machine-plus-tunnel setup — which this
project used before the Render migration — has no stable webhook URL and needs a
laptop kept awake. Neither is a supported deployment path today.
```

- [ ] **Step 6: Update the demo plan's environment facts and checklist**

In `docs/superpowers/specs/2026-08-03-demo-plan-design.md`, replace the two
tooling/process environment facts (lines 27-32) with:

```markdown
- `gh` and `uv` both resolve and work from this session (Windows binaries via
  `/mnt/c/Program Files/...`, `/mnt/c/Users/Home/.local/bin/uv.exe`).
  `gh auth status` shows `✓ Logged in ... (keyring)` with `repo` scope.
- The bot runs as a deployed Render service, kept warm by the free pinger, so
  there is no local process to start. The runbook's first step checks
  `<render-url>/healthz` and only redeploys if it does not respond.
```

Replace the stale `queue.db` / `init_db()` fact (lines ~55-59) with:

```markdown
- The queue lives in Supabase Postgres, not a local `queue.db`.
  `store.init_pool()` runs `CREATE TABLE IF NOT EXISTS` on every boot, so no
  setup is needed — but tickets now **survive** restarts, so any leftover
  `deferred`/`retrying` rows from a rehearsal must be checked (and allowed to
  drain) before the real call, or they will muddy the burst timing.
- Changing `LLM_PROVIDER` means a Render redeploy rather than a local `uvicorn`
  restart, since `Settings()` is built once at import. The webhook URL is stable
  and never needs re-editing.
```

Replace the pre-call checklist (lines 155-169) with:

```markdown
## Pre-call / pre-rehearsal checklist

1. `curl <render-url>/healthz` → `200`. If not, redeploy and wait for
   `Application startup complete.` in the logs.
2. Confirm the keep-warm monitor is active so the instance is not cold.
3. Confirm `LLM_PROVIDER=groq` on the Render service (the resting state every
   segment assumes).
4. Check for leftover `deferred`/`retrying` tickets from a prior rehearsal and
   let them drain.
5. Confirm the GitHub App's webhook URL is `<render-url>/webhook`.
6. Sanity check: an unsigned `curl -X POST <render-url>/webhook` → `401`.
7. Confirm `gh auth status` shows you logged in as the account that owns the
   `GITHUB_TARGET_REPO` testbed.
```

Add directly under the document's title:

```markdown
> **Status note:** the per-segment narrative below still assumes the old
> local-plus-tunnel flow and its two-second `uvicorn` restarts. Its environment
> facts and checklist have been updated to the hosted stack, but the choreography
> and timing await the §13 re-validation — see
> `docs/2026-08-05-first-hosted-run-findings.md` for the measured Render restart
> durations that re-validation needs.
```

- [ ] **Step 7: Verify only records still mention the tunnel**

Run: `grep -rni "cloudflare\|tunnel" --include="*.md" . | grep -v "^./.venv" | grep -v "docs/2026-08-03\|docs/superpowers/plans/2026-08-03"`
Expected: only `cost.md`'s comparison row plus its rejected-alternatives wording,
`SETUP.md`'s PR #3 rehearsal row, and the demo plan's pending-re-validation note.

Run: `uv run pytest -q && uv run ruff check .`
Expected: all green (docs-only change; this is a tripwire).

- [ ] **Step 8: Commit**

```bash
git add CLAUDE.md README.md SETUP.md SPEC.md cost.md docs/superpowers/specs/2026-08-03-demo-plan-design.md
git commit -m "docs: retire the Cloudflare Tunnel as a documented path

The Render migration retired the tunnel, but CLAUDE.md still described the
deployment as 'Docker + Cloudflare Tunnel' and README/SETUP/SPEC still offered
it as a local option. There was never a dependency to remove -- cloudflared
appears in no pyproject/Dockerfile/uv.lock entry.

cost.md keeps both comparison rows, reworded as evaluated-and-rejected, since
they document why Render + Supabase won. SETUP.md's PR #3 rehearsal row and the
2026-08-03 migration docs stay as historical record -- that PR really was
delivered over a tunnel."
```

---

## Phase 2 — The first hosted run

**Read before starting.** These tasks are operational, not TDD. They make **no
repo changes** until Task 13. Each has explicit pass criteria; a criterion that
does not hold is *recorded*, not fixed. Steps marked **OPERATOR** need a
dashboard login and cannot be done by an agent — stop and ask.

Keep a running findings file at
`/tmp/claude-*/scratchpad/hosted-run-findings.md` from Task 6 onward and append
as you go; Task 13 turns it into the committed doc. Never write a secret into it.

### Task 6: Pre-flight and query rehearsal (A0)

**Files:** none modified. Creates only scratchpad notes.

**Interfaces:**
- Produces: a verified set of read-only verification queries reused by Tasks 7,
  9, 10, 11, 12.

- [ ] **Step 1: Confirm the repo is clean and green**

```bash
git status --porcelain          # expect empty
uv run pytest -q                # expect all pass
uv run ruff check .             # expect clean
gh auth status                  # expect logged in as the GITHUB_TARGET_REPO owner
grep -n "^LLM_PROVIDER" .env    # expect groq
```

- [ ] **Step 2: Rehearse every verification query against local Postgres**

The point is that no SQL gets debugged against live infrastructure later. Start a
throwaway Postgres, let the app create the schema, and run each query.

```bash
docker run --rm -d -p 55432:5432 -e POSTGRES_PASSWORD=pw --name rehearse postgres:16-alpine
sleep 5
DATABASE_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres' uv run python -c "
from app.config import settings
from app.queue import store
import psycopg

# (1) baseline query -- before init_pool, tickets must be absent
with psycopg.connect(settings.database_url, connect_timeout=10) as c:
    print('baseline to_regclass:', c.execute(\"SELECT to_regclass('public.tickets')\").fetchone()[0])

store.init_pool()   # the app creates its own schema

with psycopg.connect(settings.database_url, connect_timeout=10) as c:
    # (2) column-set check
    cols = {r[0] for r in c.execute(
        \"SELECT column_name FROM information_schema.columns \"
        \"WHERE table_schema='public' AND table_name='tickets'\").fetchall()}
    print('columns match dataclass:', cols == set(store.Ticket.__dataclass_fields__), len(cols))
    # (3) ticket-state query shape used in Tasks 10-12
    print('ticket rows:', c.execute(
        'SELECT status, comment_id IS NOT NULL, last_reviewed_at IS NOT NULL, '
        'not_before FROM tickets ORDER BY id').fetchall())
"
docker rm -f rehearse
```

Expected: baseline prints `None`; columns match prints `True 15`; ticket rows
prints `[]`. Record the exact commands that worked in the findings file — Tasks 7
through 12 reuse them verbatim.

- [ ] **Step 3: Record the starting state**

Append to the findings file: current commit SHA, the date, whether
`RENDER_API_KEY` is set (lane selection), and the rehearsal output above.

---

### Task 7: Supabase project and pre-boot baseline (A1-A2)

**Files:** none modified except `.env` (gitignored, never committed).

**Interfaces:**
- Consumes: Task 6's baseline query.
- Produces: a populated `DATABASE_URL` in `.env`, and the recorded fact that
  `tickets` did not exist before first boot.

- [ ] **Step 1: OPERATOR — create the Supabase project**

Following the **corrected** SETUP.md §3.1 literally (this is the point of the
sequencing — divergences are findings):

1. Create the project.
2. Wait until it reports ready.
3. Copy the Session-mode pooler string (port 5432) verbatim from **Connect**.
4. Paste it into `.env` as `DATABASE_URL=`.

Record: how long provisioning took, and whether the dashboard's string matched
§3.1's documented shape — including the actual region subdomain prefix.

- [ ] **Step 2: Verify connectivity without printing the credential**

```bash
uv run python -c "
import psycopg
from app.config import settings
with psycopg.connect(settings.database_url, connect_timeout=15) as c:
    print('select 1:', c.execute('SELECT 1').fetchone()[0])
    print('server:', c.execute('SHOW server_version').fetchone()[0])
    print('user:', c.execute('SELECT current_user').fetchone()[0])
"
```

Pass: prints `select 1: 1`. Record the server version and `current_user` (this is
the first real evidence about the pooler's role). If this fails, the message is a
finding — note whether it was `Tenant or user not found` (username shape) or
something else.

- [ ] **Step 3: Establish the pre-boot baseline**

```bash
uv run python -c "
import psycopg
from app.config import settings
with psycopg.connect(settings.database_url, connect_timeout=15) as c:
    print('to_regclass:', c.execute(\"SELECT to_regclass('public.tickets')\").fetchone()[0])
"
```

Pass: prints `to_regclass: None`. **This must be captured before Task 8's deploy**
— it is what makes "the app created its own schema" a real claim rather than an
assumption. If it already prints `tickets`, stop and record: something
provisioned the table out of band, which invalidates the run's central check.

---

### Task 8: Render service and the deploy that counts (A3)

**Files:** none modified except `.env` (gitignored).

**Interfaces:**
- Consumes: `DATABASE_URL` from Task 7.
- Produces: a live Render service URL, recorded in the findings file.

- [ ] **Step 1: OPERATOR — decide the lane**

If you want the fast lane, create a Render API key (Account Settings → API Keys)
and paste it into `.env` as `RENDER_API_KEY=`. Otherwise skip it; Step 3 has a
full manual path. Record which lane was chosen.

- [ ] **Step 2: OPERATOR — create the service from the Blueprint**

Render dashboard → **New +** → **Blueprint** → point at this repo's
`render.yaml`. It declares `runtime: docker`, `plan: free`, and
`healthCheckPath: /healthz`, so there is no Build/Start command to set.

- **Fast lane:** enter throwaway placeholder values (e.g. `x`) for the
  `sync: false` variables. Step 3 replaces them with the real values, so nothing
  sensitive is typed into a form.
- **Manual lane:** enter the real values now, per corrected SETUP.md §3.2. The
  `GITHUB_APP_PRIVATE_KEY_B64` paste (~1700 chars, produced by
  `base64 -w0 < github-app-private-key.pem`) is the likeliest silent slip —
  a truncated PEM shows up later as a JWT signing error, not as a clear message.

Record the service's public URL.

- [ ] **Step 3: Set the authoritative env vars and deploy**

**Fast lane** — eight variables, one request each. Use the **single-key**
endpoint only; `PUT /v1/services/{id}/env-vars` would delete everything omitted.

```bash
uv run python -c "
import base64, os, pathlib, httpx
from app.config import settings

key = os.environ['RENDER_API_KEY']          # exported from .env; never printed
h = {'Authorization': f'Bearer {key}', 'Accept': 'application/json'}
services = httpx.get('https://api.render.com/v1/services', headers=h, timeout=30).json()
sid = [s['service']['id'] for s in services if s['service']['name'] == 'pr-review-engine'][0]
print('service:', sid)

# Local .env normally sets the PEM *path*; Render needs the base64 form. Prefer a
# configured b64 value, else derive it from the PEM (app/config.py:10-11).
pem_b64 = settings.github_app_private_key_b64 or base64.b64encode(
    pathlib.Path(settings.github_app_private_key_path).read_bytes()
).decode()

wanted = {
    'DATABASE_URL': settings.database_url,
    'GITHUB_APP_ID': str(settings.github_app_id),
    'GITHUB_APP_PRIVATE_KEY_B64': pem_b64,
    'GITHUB_TARGET_REPO': settings.github_target_repo,
    'GITHUB_WEBHOOK_SECRET': settings.github_webhook_secret,
    'LLM_PROVIDER': 'groq',
    'GROQ_API_KEY': settings.groq_api_key,
    'GITHUB_MODELS_TOKEN': settings.github_models_token,
}
missing = [n for n, v in wanted.items() if not v]
assert not missing, f'empty locally, fix .env first: {missing}'
for name, value in wanted.items():
    r = httpx.put(f'https://api.render.com/v1/services/{sid}/env-vars/{name}',
                  headers=h, json={'value': value}, timeout=30)
    print(name, r.status_code, len(value))     # length only, never the value
"
```

Every attribute above is verified to exist in `app/config.py:8-32`. The `missing`
assertion fires before any request, so a gap in local `.env` is caught here rather
than as a confusing boot failure later.

Then trigger the deploy (env changes do **not** auto-deploy):

```bash
uv run python -c "
import os, httpx
key = os.environ['RENDER_API_KEY']
h = {'Authorization': f'Bearer {key}', 'Accept': 'application/json'}
services = httpx.get('https://api.render.com/v1/services', headers=h, timeout=30).json()
sid = [s['service']['id'] for s in services if s['service']['name'] == 'pr-review-engine'][0]
r = httpx.post(f'https://api.render.com/v1/services/{sid}/deploys', headers=h, json={}, timeout=30)
print(r.status_code, r.json().get('id'))
"
```

**Manual lane:** the Blueprint deploy from Step 2 is already the deploy that
counts. If it failed on placeholders, fix the values in the dashboard and click
**Manual Deploy**.

- [ ] **Step 4: Record what the placeholder deploy did (fast lane only)**

If a placeholder deploy ran and failed first, record its failure text verbatim —
particularly whether Task 1's `RuntimeError` message appeared and whether
`error connecting in 'pool-1'` was visible above it. This is opportunistic
evidence about the hardening change under real conditions, not a designed test.

---

### Task 9: The verification battery (A4-A5)

**Files:** none modified.

**Interfaces:**
- Consumes: the service URL from Task 8; queries from Task 6.
- Produces: the empirical answer to the parked provisioning investigation.

- [ ] **Step 1: OPERATOR — create the keep-warm monitor**

UptimeRobot → new HTTP(s) monitor on `https://<service>/healthz`, 5-minute
interval. It will report **down** until the deploy is live; that is expected and
is itself evidence the monitor works.

- [ ] **Step 2: Verify the boot — in both lanes, once**

Fast lane, read the log stream directly:

```bash
uv run python -c "
import os, httpx
key = os.environ['RENDER_API_KEY']
h = {'Authorization': f'Bearer {key}', 'Accept': 'application/json'}
services = httpx.get('https://api.render.com/v1/services', headers=h, timeout=30).json()
sid = [s['service']['id'] for s in services if s['service']['name'] == 'pr-review-engine'][0]
deploys = httpx.get(f'https://api.render.com/v1/services/{sid}/deploys?limit=3', headers=h, timeout=30).json()
for d in deploys:
    dep = d['deploy']
    print(dep['id'], dep['status'], dep.get('createdAt'), dep.get('finishedAt'))
"
```

Also ask the operator to paste the same window from the dashboard **once** — per
spec §7.2c, this proves the fallback lane for the cost of a single paste.

Pass: latest deploy status is `live`; logs contain
`Application startup complete.`; logs contain **no** `PoolTimeout`, no
`RuntimeError` from `init_pool`, and no `error connecting in 'pool-1'`.

Installation auto-discovery is verified **by inference**: `app/github_app.py`
logs nothing on discovery, and a discovery failure fails startup loudly, so a
clean startup proves it resolved. Record that reasoning rather than hunting for a
log line that does not exist.

- [ ] **Step 3: Verify the public health endpoint**

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://<service>.onrender.com/healthz
curl -s https://<service>.onrender.com/healthz
```

Pass: `200` and `{"status":"ok"}`.

- [ ] **Step 4: Verify the app created its own schema**

```bash
uv run python -c "
import psycopg
from app.config import settings
from app.queue import store
with psycopg.connect(settings.database_url, connect_timeout=15) as c:
    cols = {r[0] for r in c.execute(
        \"SELECT column_name FROM information_schema.columns \"
        \"WHERE table_schema='public' AND table_name='tickets'\").fetchall()}
print('column count:', len(cols))
print('matches dataclass:', cols == set(store.Ticket.__dataclass_fields__))
print('missing:', set(store.Ticket.__dataclass_fields__) - cols)
print('extra:', cols - set(store.Ticket.__dataclass_fields__))
"
```

Pass: 15 columns, `matches dataclass: True`, both diffs empty. Combined with Task
7 Step 3's `None`, this is the conclusive answer: `CREATE TABLE IF NOT EXISTS`
through the session pooler works on a first boot against a real Supabase project.

- [ ] **Step 5: Register the webhook**

```bash
PUBLIC_BASE_URL=https://<service>.onrender.com uv run python -m bot.scripts.deploy
```

Pass: exit 0 and a line like `registered: installation=<id> webhook=…/webhook`.
Confirm in the App settings that the webhook URL is the Render URL.

- [ ] **Step 6: Verify the pinger by its effect**

Wait ~11 minutes (this may overlap Task 10 rather than block it), then confirm at
least two `GET /healthz` hits about five minutes apart — from the log stream in
the fast lane, or one pasted window in the manual lane.

Pass: two or more spaced hits. Record the observed interval.

---

### Task 10: Happy path end-to-end (B1)

**Files:** none modified.

- [ ] **Step 1: Open a real PR**

```bash
uv run python scripts/seed_demo_pr.py
```

Record the printed PR URL and the wall-clock time.

- [ ] **Step 2: Verify the comment**

```bash
gh pr view <pr-number> --repo <owner>/<repo> --comments
```

Pass: one bot comment with three specialist rows naming real findings. Record
latency from PR creation to comment appearing, against the 15s target — noting
that a cold Render instance may add spin-up time, which is a finding worth
recording rather than a failure.

- [ ] **Step 3: Verify the ticket row**

```bash
uv run python -c "
import psycopg
from app.config import settings
with psycopg.connect(settings.database_url, connect_timeout=15) as c:
    print(c.execute(
        'SELECT status, comment_id IS NOT NULL, last_reviewed_at IS NOT NULL, '
        'attempts, cooldown_level FROM tickets WHERE pr_number = %s', (<pr-number>,)
    ).fetchone())
"
```

Pass: `('done', True, True, 0, 0)`.

---

### Task 11: Segment B — provider swap under a real vendor outage (B2)

**Files:** none modified.

**Background:** `github_models` is genuinely retired (2026-07-30), so all three
specialists are expected to fail. The point is that the comment still posts with
three real failed rows — partial failure is always visible — and that swapping
back recovers without a code change.

- [ ] **Step 1: Note the ticket state before the restart**

```bash
uv run python -c "
import psycopg
from app.config import settings
with psycopg.connect(settings.database_url, connect_timeout=15) as c:
    print(c.execute('SELECT id, pr_number, status, updated_at FROM tickets ORDER BY id').fetchall())
"
```

Record the rows — Step 4 compares against them.

- [ ] **Step 2: OPERATOR/fast lane — switch to github_models and redeploy**

Fast lane: `PUT /v1/services/{id}/env-vars/LLM_PROVIDER` with
`{"value": "github_models"}`, then `POST …/deploys`. Manual lane: edit in the
dashboard, then **Manual Deploy**.

**Record the restart duration** (fast lane: `createdAt` to `finishedAt` on the
deploy; manual lane: the Events tab, coarser). The demo plan assumes a
two-second `uvicorn` restart; the real number is what its narration must be
rebuilt around.

- [ ] **Step 3: Seed PR-2 and verify graceful failure**

```bash
uv run python scripts/seed_demo_pr.py
gh pr view <pr-2> --repo <owner>/<repo> --comments
```

Pass: a comment posts with three rows, each showing a real error rather than
findings, and nothing silently dropped.

- [ ] **Step 4: Verify the queue survived the restart**

Re-run Step 1's query. Pass: the pre-existing ticket `id`s are unchanged (rows
persisted across a container replacement) — a claim that was not meaningful under
the old local SQLite setup.

- [ ] **Step 5: Swap back to groq and redeploy**

Same mechanism as Step 2, with `{"value": "groq"}`. Record this restart duration
too.

- [ ] **Step 6: Trigger a re-review of PR-2 and verify in-place edit**

```bash
gh pr checkout <pr-2> --repo <owner>/<repo>     # in a scratch dir
git commit --allow-empty -m "trigger re-review"
git push
gh pr view <pr-2> --repo <owner>/<repo> --comments
```

Pass: the **same** comment (same marker, same `comment_id` in the DB) now shows
real Groq findings — the provider seam survived a full vendor outage with only an
env var change.

---

### Task 12: Segment C — quota exhaustion and auto-recovery (B3)

**Files:** none modified.

**This runs exactly once.** It deliberately drives Groq's free-tier 12K TPM cap
into real 429s. Per CLAUDE.md, if Groq returns **403** instead of 429, stop
immediately, record it, and do not retry — a lost provider costs the demo its
primary path.

- [ ] **Step 1: Fire four new PRs, then a re-review of PR-1, in quick succession**

```bash
uv run python scripts/seed_demo_pr.py     # PR-3
uv run python scripts/seed_demo_pr.py     # PR-4
uv run python scripts/seed_demo_pr.py     # PR-5
uv run python scripts/seed_demo_pr.py     # PR-6
# last, so quota is already exhausted when its ticket is claimed:
gh pr checkout <pr-1> --repo <owner>/<repo> && git commit --allow-empty -m "trigger re-review" && git push
```

- [ ] **Step 2: Capture the deferral state while it is happening**

```bash
uv run python -c "
import psycopg
from app.config import settings
with psycopg.connect(settings.database_url, connect_timeout=15) as c:
    print(c.execute(
        'SELECT pr_number, status, not_before, attempts, cooldown_level, '
        'last_reviewed_at IS NOT NULL FROM tickets ORDER BY id').fetchall())
"
```

Pass: at least one row with `status` in `('deferred','retrying')` and a non-null
`not_before`. Record the full table — the actual counts, not the predicted ones.

- [ ] **Step 3: Verify the two comment behaviors**

Check each PR's comments. Pass:
- New PRs with no prior review show a **plain placeholder** comment.
- PR-1, which has a good visible review, shows that review **with a schedule
  footnote appended** — not overwritten. This is the "never overwrite a good
  visible review" guarantee.

- [ ] **Step 4: Verify automatic recovery**

Wait for the deferred tickets' `not_before` to pass, then re-run Step 2's query
and re-check comments. Pass: all rows reach `done` with no manual intervention;
placeholders are replaced by real reviews; PR-1's footnote disappears and its
original comment is intact.

- [ ] **Step 5: Record actual numbers**

Append to findings: how many reviews succeeded before the first 429, the real
`Retry-After` values observed, total time to full recovery, and any divergence
from the demo plan's token math.

---

### Task 13: Record and close out (Block C)

**Files:**
- Create: `docs/2026-08-05-first-hosted-run-findings.md`
- Modify: `SETUP.md` (rehearsal table — add hosted rows, keep PR #3's row)
- Modify: `docs/2026-08-05-supabase-first-deploy-provisioning-handoff.md`
  (mark resolved)
- Modify: `docs/superpowers/specs/2026-08-05-deploy-command-design.md`
  (lift `Status: Paused`)

- [ ] **Step 1: Write the findings doc**

Turn the scratchpad notes into `docs/2026-08-05-first-hosted-run-findings.md`,
covering: which tooling lane was used (and that the other is therefore
unverified); every divergence from the corrected SETUP.md, especially the actual
pooler string shape and provisioning duration; whether Task 1's diagnostic
message ever fired, and if so whether it was actually useful; measured Render
restart durations for the demo plan's re-validation; happy-path latency including
any cold-start component; Segment C's real counts and recovery time; and a
concrete `check_database` recommendation for `/deploy`. Scrub anything
token-shaped.

Carry forward the one observation the spec (§9) recorded rather than fixed, and
note whether the run confirmed it: `app/webhook.py:57`'s `logger.info` for a
non-target-repo skip never surfaces under uvicorn's default root level of
WARNING, so that path is invisible in production logs.

- [ ] **Step 2: Add hosted rows to SETUP.md's rehearsal table**

Append rows for the hosted happy path, Segment B, and Segment C. **Do not edit**
the PR #3 row — it records a tunnel-delivered run that genuinely happened.

- [ ] **Step 3: Mark the handoff resolved**

Change `docs/2026-08-05-supabase-first-deploy-provisioning-handoff.md`'s
`**Status:** Open …` to resolved, with a one-line answer to each of its five
unknowns and a pointer to the findings doc.

- [ ] **Step 4: Unblock the /deploy design**

In `docs/superpowers/specs/2026-08-05-deploy-command-design.md`, change
`**Status:** Paused …` to indicate it is ready to resume, and replace its
`check_database` open question with what the run actually established.

- [ ] **Step 5: OPERATOR — revoke the Render API key**

If one was created, revoke it in Render Account Settings and remove the line from
`.env`. Leave the service running so the pinger keeps it warm for the demo.

- [ ] **Step 6: Final tripwire**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all green — unchanged from Task 5, confirming the run did not perturb
the repo.

- [ ] **Step 7: Commit**

```bash
git add docs/2026-08-05-first-hosted-run-findings.md SETUP.md docs/2026-08-05-supabase-first-deploy-provisioning-handoff.md docs/superpowers/specs/2026-08-05-deploy-command-design.md
git commit -m "docs: record the first hosted Render + Supabase run

First end-to-end run on the hosted stack: the app provisioned its own tickets
table on first boot against a real Supabase project, the happy path posted a
real review, and both demo resilience beats were observed with the queue now
surviving restarts.

Resolves the first-deploy provisioning handoff and unblocks the /deploy design."
```

---

## Notes for the executor

- **Docker must be running** for the local test suite (testcontainers) — see
  SETUP.md §2c.
- **Phase 1 is fully offline.** No live infrastructure, no API keys, no network
  beyond package installs. Tasks 1-5 can be completed and reviewed before anyone
  logs into a dashboard.
- **Phase 2 cannot be batched.** Tasks 7, 8, and 9 Step 1 need operator actions;
  stop and ask rather than guessing at dashboard state. Task 12 is irreversible
  in the sense that it consumes real quota — do not "re-run to check."
- If Phase 2 stops on a code-level blocker, record it, finish every unaffected
  step, and hand back a clear statement of what remains. A partial run honestly
  reported is worth more than a run that looks complete.
