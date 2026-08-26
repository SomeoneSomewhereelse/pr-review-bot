# Render Hosting + Supabase Postgres Queue Migration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the durable review queue from a local SQLite file to Supabase-hosted Postgres and host the app persistently on Render (own stable URL), softcode the single testbed repo into config, and add idempotent deploy-time bot registration — without changing what the bot does (still single-tenant, one repo).

**Architecture:** `app/queue/store.py` is rewritten for **psycopg3** over a module connection pool (Supabase Session-mode pooler); atomic ops use Postgres idioms (`FOR UPDATE SKIP LOCKED` claim, transactional read-branch-write enqueue). Store functions stay synchronous and are wrapped in `asyncio.to_thread` at the async call sites. DB-touching tests run against a throwaway **local** Postgres (testcontainers locally, `services: postgres` in CI — never Supabase).

**Tech Stack:** FastAPI, psycopg3 (`psycopg[binary]`, `psycopg_pool`), Postgres, `pytest`/`pytest-asyncio` (`asyncio_mode="auto"`), testcontainers, `uv`, Docker, Render, Supabase.

## Global Constraints

- Python `>=3.12`; managed with `uv`. Tests: `uv run pytest`; lint: `uv run ruff check .`. `ruff` line-length **100**.
- **Single-tenant, unchanged behavior.** One GitHub App installation, one *configured* target repo (`GITHUB_TARGET_REPO`). No registration/per-user/multi-tenant anything.
- **Single Postgres backend everywhere** — SQLite is removed entirely; no dual dialect.
- Store functions stay **synchronous**; async callers wrap them in `asyncio.to_thread`.
- Timestamp columns stay **`TEXT` ISO-8601** (lexical comparison already correct).
- **CI stays deterministic + off real Supabase** — DB tests hit a localhost Postgres only.
- **Zero new secrets** for deploy-time registration (uses the existing App JWT). No secret is ever logged.
- **Environment:** driven from WSL — pass **Linux paths** (`/mnt/c/...`) to file tools; `git` runs natively. Stage only each task's named files (`.gitattributes` enforces LF; never `git add .`).
- Full design: `docs/superpowers/specs/2026-08-03-supabase-hosting-migration-design.md`. Branch: `master`.

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `pyproject.toml` | Add psycopg/psycopg_pool deps + testcontainers dev dep | Modify |
| `tests/conftest.py` | Shared Postgres harness: `db_url`, pool init, per-test `TRUNCATE`, raw `db_exec`/`db_query` helpers | Create |
| `app/queue/store.py` | Rewrite for psycopg3 (pool, Postgres schema, atomic primitives) | Modify |
| `app/config.py` | `database_url`, `github_target_repo`, `public_base_url`, `github_app_private_key_b64`; drop `queue_db_path` | Modify |
| `app/main.py` | `store.init_pool()`/`close_pool()` lifecycle | Modify |
| `app/queue/dispatcher.py`, `app/webhook.py` | Wrap store calls in `asyncio.to_thread`; webhook repo gate | Modify |
| `app/github_app.py` | PEM-from-b64; `discover_installation_id`, `set_webhook_url` | Modify |
| `scripts/deploy.py` | Idempotent deploy-time registration entry (the `/deploy` command wraps this) | Create |
| `scripts/*.py` | Read repo from `settings.github_target_repo` | Modify |
| `render.yaml` | Render blueprint | Create |
| `.github/workflows/ci.yml` | Add `services: postgres` + `DATABASE_URL` | Modify |
| `README.md`, `SETUP.md`, `SPEC.md`, `cost.md`, `.env.example` | Deployment + service/cost docs | Modify |
| `tests/test_queue_store.py`, `test_dispatcher.py`, `test_webhook.py`, `test_main_lifespan.py` | Re-point to Postgres harness | Modify |

**Task order** (each ends green): 1 deps+harness → 2 store port + test re-point (the big one; CI gains Postgres here) → 3 `to_thread` call sites → 4 softcode repo + webhook gate → 5 PEM-from-b64 → 6 registration functions + `scripts/deploy.py` → 7 Render/CI deploy config → 8 docs (README/SETUP/SPEC/cost).

---

### Task 1: Dependencies + Postgres test harness

**Files:** Modify `pyproject.toml`; Create `tests/conftest.py`.

**Interfaces:**
- Produces pytest fixtures/helpers (used by Task 2): `db_url` (session), `db` (per-test: pool up + `TRUNCATE tickets RESTART IDENTITY`), `db_exec(sql, params=())`, `db_query(sql, params=())`.

- [ ] **Step 1: Add dependencies**

In `pyproject.toml`, add to `dependencies`: `"psycopg[binary]>=3.2"`, `"psycopg-pool>=3.2"`. Add to `[dependency-groups] dev`: `"testcontainers[postgres]>=4.0"`.

- [ ] **Step 2: Create `tests/conftest.py`**

```python
"""Shared Postgres test harness. Uses DATABASE_URL if the environment already
provides one (CI's `services: postgres`); otherwise spins a throwaway Postgres
via testcontainers (local dev — Docker required). Never touches Supabase."""
from __future__ import annotations

import os

import pytest

from app.config import settings
from app.queue import store


@pytest.fixture(scope="session")
def db_url() -> str:
    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        yield env_url
        return
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        # psycopg (not psycopg2) driver URL
        yield pg.get_connection_url(driver="psycopg")


@pytest.fixture
def db(db_url, monkeypatch):
    """Point the store at the test Postgres, ensure schema, and truncate between
    tests. Opt-in (DB-touching test modules request it via an autouse wrapper)."""
    monkeypatch.setattr(settings, "database_url", db_url)
    store.close_pool()
    store.init_pool()
    with store._require_pool().connection() as conn:
        conn.execute("TRUNCATE tickets RESTART IDENTITY")
    yield
    store.close_pool()


@pytest.fixture
def db_exec(db_url):
    """Run a raw statement against the test DB (replaces test-side sqlite3.connect)."""
    import psycopg

    def _exec(sql: str, params: tuple = ()):
        with psycopg.connect(db_url) as conn:
            conn.execute(sql, params)
            conn.commit()

    return _exec


@pytest.fixture
def db_query(db_url):
    """Run a raw query and return the rows (list of tuples)."""
    import psycopg

    def _query(sql: str, params: tuple = ()):
        with psycopg.connect(db_url) as conn:
            return conn.execute(sql, params).fetchall()

    return _query
```

- [ ] **Step 3: Verify nothing breaks (fixtures unused yet)**

Run: `uv run pytest -q` and `uv run ruff check .`
Expected: PASS — the conftest fixtures are defined but not yet requested by any test (existing tests still use their sqlite fixtures), so the suite is unchanged. (`store.close_pool`/`init_pool`/`_require_pool` are referenced by the fixtures but only executed when a test requests `db` — that lands in Task 2.)

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock tests/conftest.py
git commit -m "test: add Postgres test harness (testcontainers/CI service) + deps"
```

---

### Task 2: Port `store.py` to Postgres + re-point DB tests (the migration core)

**Files:** Modify `app/queue/store.py`, `app/config.py`, `app/main.py`, `.github/workflows/ci.yml`; Modify `tests/test_queue_store.py`, `tests/test_dispatcher.py`, `tests/test_webhook.py`, `tests/test_main_lifespan.py`.

**Interfaces:**
- Consumes the Task 1 harness (`db`, `db_exec`, `db_query`).
- Produces: `store.init_pool()`, `store.close_pool()`, `store._require_pool()`, and every existing store function with **unchanged signatures/return types** (`Ticket`, `enqueue_or_update`, `claim_next_due`, `finalize_review`, `defer_rate_limited`, `defer_failed`, `mark_failed`, `mark_notice_posted`, `clear_notice`, `tickets_needing_notice`, `recover_on_startup`, `get_ticket`, and the pure `effective_cooldown`/`next_cooldown_level`/`_due_after_cooldown`). `settings.database_url` replaces `settings.queue_db_path`.

- [ ] **Step 1: Add `database_url`, drop `queue_db_path` (config)**

In `app/config.py`: replace `queue_db_path: str = "./queue.db"` with `database_url: str = ""`.

- [ ] **Step 2: Rewrite `app/queue/store.py` for psycopg3**

Replace the whole file:

```python
"""Durable ticket store (Postgres via psycopg3) — the queue's source of truth.

One row per (repo, pr): UNIQUE collapses re-triggers so a new push updates the
existing ticket's head_sha instead of stacking a duplicate. A ticket's persisted
``not_before`` prevents an early run after a restart. Timestamps are ISO-8601 UTC
TEXT (lexical comparison == chronological). Functions are synchronous; async
callers wrap them in asyncio.to_thread so Postgres network latency never blocks
the event loop.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    repo_full_name     TEXT    NOT NULL,
    pr_number          INTEGER NOT NULL,
    head_sha           TEXT,
    status             TEXT    NOT NULL,
    provider           TEXT    NOT NULL,
    not_before         TEXT,
    attempts           INTEGER NOT NULL DEFAULT 0,
    comment_id         BIGINT,
    enqueued_at        TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL,
    rereview_requested INTEGER NOT NULL DEFAULT 0,
    last_reviewed_at   TEXT,
    cooldown_level     INTEGER NOT NULL DEFAULT 0,
    notice_not_before  TEXT,
    UNIQUE (repo_full_name, pr_number)
);
"""

_pool: ConnectionPool | None = None


@dataclass
class Ticket:
    id: int
    repo_full_name: str
    pr_number: int
    head_sha: str | None
    status: str
    provider: str
    not_before: str | None
    attempts: int
    comment_id: int | None
    enqueued_at: str
    updated_at: str
    rereview_requested: int
    last_reviewed_at: str | None
    cooldown_level: int
    notice_not_before: str | None


def _configure(conn) -> None:
    conn.row_factory = dict_row


def init_pool() -> None:
    """Open the connection pool (if not already) and ensure the schema. Idempotent."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings.database_url, min_size=1, max_size=4, configure=_configure, open=True
        )
    with _pool.connection() as conn:
        conn.execute(_SCHEMA)


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def _require_pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("store.init_pool() has not been called")
    return _pool


def _row_to_ticket(row: dict) -> Ticket:
    return Ticket(**row)


_MAX_COOLDOWN_LEVEL = 30


def effective_cooldown(level: int) -> float:
    base = settings.dispatcher_rereview_cooldown_seconds
    cap = settings.dispatcher_rereview_cooldown_max_seconds
    return max(base, min(base * 2 ** min(level, _MAX_COOLDOWN_LEVEL), cap))


def next_cooldown_level(level: int) -> int:
    return min(level + 1, _MAX_COOLDOWN_LEVEL)


def _due_after_cooldown(
    last_reviewed_at: str | None, now: str, level: int
) -> tuple[str, str | None, int]:
    if last_reviewed_at is None:
        return ("pending", None, 0)
    due = datetime.fromisoformat(last_reviewed_at) + timedelta(seconds=effective_cooldown(level))
    if datetime.fromisoformat(now) < due:
        return ("deferred", due.isoformat(), next_cooldown_level(level))
    return ("pending", None, 0)


def enqueue_or_update(
    *, repo_full_name: str, pr_number: int, head_sha: str | None, provider: str, now: str
) -> int:
    """Enqueue/update a ticket under the per-state re-review policy. The whole
    read-branch-write runs in one transaction; SELECT ... FOR UPDATE locks the
    row so a concurrent writer cannot interleave (Postgres analogue of the old
    SQLite BEGIN IMMEDIATE)."""
    with _require_pool().connection() as conn:
        row = conn.execute(
            "SELECT * FROM tickets WHERE repo_full_name = %s AND pr_number = %s FOR UPDATE",
            (repo_full_name, pr_number),
        ).fetchone()
        if row is None:
            inserted = conn.execute(
                """
                INSERT INTO tickets
                  (repo_full_name, pr_number, head_sha, status, provider, not_before,
                   attempts, comment_id, enqueued_at, updated_at, rereview_requested,
                   last_reviewed_at, cooldown_level, notice_not_before)
                VALUES (%s, %s, %s, 'pending', %s, NULL, 0, NULL, %s, %s, 0, NULL, 0, NULL)
                ON CONFLICT (repo_full_name, pr_number) DO NOTHING
                RETURNING id
                """,
                (repo_full_name, pr_number, head_sha, provider, now, now),
            ).fetchone()
            if inserted is not None:
                return int(inserted["id"])
            # A concurrent insert won the race; block on its lock, then read it.
            row = conn.execute(
                "SELECT * FROM tickets WHERE repo_full_name = %s AND pr_number = %s FOR UPDATE",
                (repo_full_name, pr_number),
            ).fetchone()

        status = row["status"]
        ticket_id = int(row["id"])
        if status == "running":
            conn.execute(
                "UPDATE tickets SET head_sha = %s, rereview_requested = 1, updated_at = %s WHERE id = %s",
                (head_sha, now, ticket_id),
            )
        elif status in ("pending", "deferred", "retrying"):
            conn.execute(
                "UPDATE tickets SET head_sha = %s, updated_at = %s WHERE id = %s",
                (head_sha, now, ticket_id),
            )
        else:  # 'done'/'failed' -> re-arm honoring the escalating cooldown
            new_status, not_before, new_level = _due_after_cooldown(
                row["last_reviewed_at"], now, row["cooldown_level"]
            )
            conn.execute(
                "UPDATE tickets SET head_sha = %s, status = %s, not_before = %s, attempts = 0, "
                "rereview_requested = 0, cooldown_level = %s, updated_at = %s WHERE id = %s",
                (head_sha, new_status, not_before, new_level, now, ticket_id),
            )
        return ticket_id


def claim_next_due(now: str) -> Ticket | None:
    """Atomically claim the oldest due ticket via FOR UPDATE SKIP LOCKED."""
    with _require_pool().connection() as conn:
        row = conn.execute(
            """
            UPDATE tickets SET status = 'running', updated_at = %s, rereview_requested = 0
            WHERE id = (
                SELECT id FROM tickets
                WHERE status = 'pending'
                   OR (status IN ('deferred','retrying') AND not_before IS NOT NULL AND not_before <= %s)
                ORDER BY enqueued_at ASC, id ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING *
            """,
            (now, now),
        ).fetchone()
        return _row_to_ticket(row) if row else None


def defer_rate_limited(ticket_id: int, not_before: str, now: str) -> None:
    with _require_pool().connection() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'deferred', not_before = %s, updated_at = %s WHERE id = %s",
            (not_before, now, ticket_id),
        )


def defer_failed(ticket_id: int, not_before: str, now: str) -> None:
    with _require_pool().connection() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'retrying', not_before = %s, "
            "attempts = attempts + 1, updated_at = %s WHERE id = %s",
            (not_before, now, ticket_id),
        )


def finalize_review(
    ticket_id: int,
    now: str,
    rereview_not_before: str,
    rereview_cooldown_level: int,
    comment_id: int | None = None,
) -> None:
    with _require_pool().connection() as conn:
        conn.execute(
            """
            UPDATE tickets SET
              last_reviewed_at   = %(now)s,
              comment_id         = COALESCE(%(comment_id)s, comment_id),
              status             = CASE WHEN rereview_requested = 1 THEN 'deferred' ELSE 'done' END,
              not_before         = CASE WHEN rereview_requested = 1 THEN %(rnb)s ELSE NULL END,
              attempts           = CASE WHEN rereview_requested = 1 THEN 0 ELSE attempts END,
              cooldown_level     = CASE WHEN rereview_requested = 1 THEN %(new_level)s ELSE cooldown_level END,
              rereview_requested = 0,
              updated_at         = %(now)s
            WHERE id = %(id)s
            """,
            {
                "now": now,
                "comment_id": comment_id,
                "rnb": rereview_not_before,
                "new_level": rereview_cooldown_level,
                "id": ticket_id,
            },
        )


def mark_failed(ticket_id: int, now: str, error: str | None = None) -> None:
    with _require_pool().connection() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'failed', updated_at = %s WHERE id = %s", (now, ticket_id)
        )


def tickets_needing_notice(now: str) -> list[Ticket]:
    with _require_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM tickets
            WHERE status = 'deferred'
              AND not_before IS NOT NULL
              AND not_before > %s
              AND last_reviewed_at IS NOT NULL
              AND (notice_not_before IS NULL OR notice_not_before != not_before)
            ORDER BY enqueued_at ASC, id ASC
            LIMIT %s
            """,
            (now, settings.dispatcher_notice_sweep_batch_size),
        ).fetchall()
        return [_row_to_ticket(row) for row in rows]


def mark_notice_posted(ticket_id: int, not_before: str) -> None:
    with _require_pool().connection() as conn:
        conn.execute(
            "UPDATE tickets SET notice_not_before = %s WHERE id = %s", (not_before, ticket_id)
        )


def clear_notice(ticket_id: int) -> None:
    with _require_pool().connection() as conn:
        conn.execute("UPDATE tickets SET notice_not_before = NULL WHERE id = %s", (ticket_id,))


def recover_on_startup(now: str) -> None:
    with _require_pool().connection() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'pending', rereview_requested = 0, updated_at = %s "
            "WHERE status = 'running'",
            (now,),
        )


def get_ticket(ticket_id: int) -> Ticket | None:
    with _require_pool().connection() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE id = %s", (ticket_id,)).fetchone()
        return _row_to_ticket(row) if row else None
```

- [ ] **Step 3: Update `app/main.py` lifespan**

Replace `store.init_db()` with `store.init_pool()`, and add `store.close_pool()` on shutdown:

```python
@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    store.init_pool()
    store.recover_on_startup(datetime.now(timezone.utc).isoformat())
    task = asyncio.create_task(dispatcher.run_forever())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        store.close_pool()
```

- [ ] **Step 4: Re-point the DB-touching test fixtures**

For each of `tests/test_queue_store.py`, `tests/test_dispatcher.py`, `tests/test_webhook.py`, `tests/test_main_lifespan.py`:

1. **Replace the temp-sqlite autouse fixture** (`_temp_db`/`_env`/`_isolate`, which does `monkeypatch.setattr(settings, "queue_db_path", ...)` + `store.init_db()`) with an autouse fixture that **requests the shared `db` fixture** and keeps that module's other setup (e.g. `dispatcher.reset_blocked_until()`, `webhook.reset_dedup_cache()`):

   ```python
   @pytest.fixture(autouse=True)
   def _env(db, monkeypatch):            # `db` = conftest Postgres harness (pool + truncate)
       monkeypatch.setattr(settings, "llm_provider", "groq")   # keep any existing per-module setup
       dispatcher.reset_blocked_until()                        # (dispatcher module only)
       yield
       dispatcher.reset_blocked_until()
   ```

2. **Replace every raw `with sqlite3.connect(settings.queue_db_path) as conn: conn.execute("... ?", (...))` setup/inspection block** with the `db_exec` / `db_query` fixtures and `%s` placeholders. Worked example — a dispatcher test seeding a column:

   ```python
   # BEFORE:
   #   with sqlite3.connect(settings.queue_db_path) as conn:
   #       conn.execute("UPDATE tickets SET cooldown_level = 3 WHERE id = ?", (tid,))
   # AFTER (add `db_exec` to the test's parameters):
   db_exec("UPDATE tickets SET cooldown_level = 3 WHERE id = %s", (tid,))
   ```

   Remove `import sqlite3` from those test modules once no block remains.

3. **Delete the SQLite-migration tests** that no longer apply (Postgres uses one `CREATE TABLE`, no `ALTER`-backfill path): in `tests/test_queue_store.py` remove `test_init_db_migrates_a_pre_existing_table_missing_new_columns`, `test_init_db_backfills_cooldown_level_on_pre_existing_table`, and `test_init_db_backfills_notice_not_before_on_pre_existing_table` (and the `_column_names` helper if now unused).

4. Any test asserting an exact `id`/`enqueued_at` ordering still holds (identity ids start at 1 after `TRUNCATE ... RESTART IDENTITY`).

- [ ] **Step 5: Add the Postgres service to CI**

In `.github/workflows/ci.yml`, add a `services:` block to the job and export `DATABASE_URL` so the conftest uses it (not testcontainers) in CI:

```yaml
    services:
      postgres:
        image: postgres:16-alpine
        env:
          POSTGRES_PASSWORD: postgres
          POSTGRES_DB: test
        ports:
          - 5432:5432
        options: >-
          --health-cmd "pg_isready -U postgres" --health-interval 5s
          --health-timeout 5s --health-retries 10
    env:
      DATABASE_URL: "postgresql://postgres:postgres@localhost:5432/test"
```

- [ ] **Step 6: Run the full suite against Postgres**

Run: `uv run pytest -v` (locally, with Docker running for testcontainers) and `uv run ruff check .`
Expected: PASS. If a test fails on a Postgres/SQLite semantic difference (e.g. an integer where Postgres returns a different type), fix that test's assertion; do not weaken a store guarantee.

- [ ] **Step 7: Commit**

```bash
git add app/queue/store.py app/config.py app/main.py .github/workflows/ci.yml \
        tests/test_queue_store.py tests/test_dispatcher.py tests/test_webhook.py tests/test_main_lifespan.py
git commit -m "feat(queue): port the durable store from SQLite to Supabase Postgres (psycopg3)"
```

---

### Task 3: Wrap async store call sites in `asyncio.to_thread`

**Files:** Modify `app/queue/dispatcher.py`, `app/webhook.py`.

**Interfaces:** no signature changes; store calls inside async functions become `await asyncio.to_thread(store.fn, ...)`.

- [ ] **Step 1: Wrap the dispatcher's store calls**

In `app/queue/dispatcher.py` (`process_next_due` and the notice-sweep path), replace each direct `store.<fn>(...)` with `await asyncio.to_thread(store.<fn>, ...)`. `store.claim_next_due` returns a value: `ticket = await asyncio.to_thread(store.claim_next_due, now.isoformat())`. (`asyncio` is already imported.) Pure helpers `store.effective_cooldown`/`store.next_cooldown_level` are CPU-only — leave them direct.

- [ ] **Step 2: Wrap the webhook enqueue**

In `app/webhook.py`, make `_enqueue_from_payload` `async def` and `await asyncio.to_thread(store.enqueue_or_update, ...)`; `await _enqueue_from_payload(payload)` in the route (add `import asyncio`).

- [ ] **Step 3: Run the suite**

Run: `uv run pytest -v && uv run ruff check .`
Expected: PASS — behavior is unchanged (tests `await` the async entry points; the `to_thread` hop is transparent). The dispatcher/webhook tests that stub `store` still work (they patch the module attribute, which `to_thread` calls).

- [ ] **Step 4: Commit**

```bash
git add app/queue/dispatcher.py app/webhook.py
git commit -m "perf(queue): run Postgres store calls off the event loop via asyncio.to_thread"
```

---

### Task 4: Softcode the testbed repo — config + webhook gate + scripts

**Files:** Modify `app/config.py`, `app/webhook.py`, `scripts/seed_demo_pr.py`, `scripts/manual_verify_step3.py`, `scripts/demo_provider_swap.py`; Test `tests/test_webhook.py`.

**Interfaces:** Produces `settings.github_target_repo`; the webhook `202`-no-ops payloads for other repos.

- [ ] **Step 1: Write the failing test**

In `tests/test_webhook.py`, add (the module already signs payloads and asserts enqueues):

```python
async def test_webhook_ignores_non_target_repo(monkeypatch, db_query):
    monkeypatch.setattr(settings, "github_target_repo", "owner/target-repo")
    payload = {"action": "opened",
               "repository": {"full_name": "someone/OTHER-repo"},
               "pull_request": {"number": 5, "head": {"sha": "abc"}}}
    body = json.dumps(payload).encode()
    sig = _sign(body)   # existing helper in this module
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/webhook", content=body,
                            headers={"X-Hub-Signature-256": sig, "X-GitHub-Delivery": "d-nonmatch"})
    assert resp.status_code == 202                      # accepted, but...
    assert db_query("SELECT count(*) FROM tickets") == [(0,)]   # ...no ticket enqueued
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_webhook.py -k non_target_repo -v`
Expected: FAIL — the current webhook enqueues any repo.

- [ ] **Step 3: Add the config var + the gate**

In `app/config.py` add: `github_target_repo: str = ""`.

In `app/webhook.py`'s `_enqueue_from_payload`, after extracting `repo_full_name`, before enqueue:

```python
    if repo_full_name != settings.github_target_repo:
        logger.info("Ignoring webhook for non-target repo %s", repo_full_name)
        return
```

- [ ] **Step 4: Point the scripts at the config**

In `scripts/seed_demo_pr.py`, `scripts/manual_verify_step3.py`, `scripts/demo_provider_swap.py`, replace the hardcoded `"TovTechOrg/pr-review-bot-testbed"` literal with `settings.github_target_repo` (import `from app.config import settings`).

- [ ] **Step 5: Run tests + lint**

Run: `uv run pytest tests/test_webhook.py -v && uv run ruff check .`
Expected: PASS. (Other webhook tests set `settings.github_target_repo` to their payload's repo via the module's autouse fixture — add that monkeypatch to the fixture so existing enqueue tests still match.)

- [ ] **Step 6: Commit**

```bash
git add app/config.py app/webhook.py scripts/seed_demo_pr.py scripts/manual_verify_step3.py \
        scripts/demo_provider_swap.py tests/test_webhook.py
git commit -m "feat(webhook): softcode the target repo + gate non-target payloads"
```

---

### Task 5: Load the GitHub App PEM from a base64 env var

**Files:** Modify `app/config.py`, `app/github_app.py`; Test `tests/test_github_app.py`.

**Interfaces:** Produces `settings.github_app_private_key_b64`; `_read_private_key()` prefers it.

- [ ] **Step 1: Write the failing test**

In `tests/test_github_app.py`:

```python
def test_read_private_key_prefers_base64_env(monkeypatch):
    import base64
    from app import github_app

    pem = "-----BEGIN KEY-----\nabc\n-----END KEY-----\n"
    monkeypatch.setattr(settings, "github_app_private_key_b64", base64.b64encode(pem.encode()).decode())
    assert github_app._read_private_key() == pem
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_github_app.py -k prefers_base64 -v`
Expected: FAIL — no `github_app_private_key_b64` setting / no b64 branch.

- [ ] **Step 3: Implement**

`app/config.py`: add `github_app_private_key_b64: str = ""`.

`app/github_app.py` `_read_private_key`:

```python
def _read_private_key() -> str:
    """Prefer the base64 env var (host-portable); fall back to the PEM file for
    local dev. Never logged."""
    b64 = settings.github_app_private_key_b64
    if b64:
        import base64
        return base64.b64decode(b64).decode()
    key_path = Path(settings.github_app_private_key_path)
    if not key_path.is_absolute():
        key_path = Path.cwd() / key_path
    return key_path.read_text()
```

- [ ] **Step 4: Run tests + lint + commit**

```bash
uv run pytest tests/test_github_app.py -v && uv run ruff check .
git add app/config.py app/github_app.py tests/test_github_app.py
git commit -m "feat(github): load the App private key from a base64 env var"
```

---

### Task 6: Deploy-time registration functions + `scripts/deploy.py`

**Files:** Modify `app/config.py`, `app/github_app.py`; Create `scripts/deploy.py`; Test `tests/test_github_app.py`.

**Interfaces:** Produces `settings.public_base_url`; `github_app.discover_installation_id(repo) -> int` (raises with a clear message if the App isn't installed); `github_app.set_webhook_url(url) -> None`. `scripts/deploy.py` runs both idempotently (the `/deploy` slash command wraps this script).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_github_app.py` (reuse the module's `fake_transport` App-JWT mocking):

```python
def test_discover_installation_id_returns_id(fake_transport):
    from app import github_app
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/installation", {"id": 424242})
    assert github_app.discover_installation_id(REPO_FULL_NAME) == 424242


def test_discover_installation_id_raises_when_not_installed(fake_transport):
    from github import GithubException
    from app import github_app
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/installation", {"message": "Not Found"}, 404)
    with pytest.raises(RuntimeError, match="not installed"):
        github_app.discover_installation_id(REPO_FULL_NAME)


def test_set_webhook_url_patches_hook_config(fake_transport):
    from app import github_app
    seen = {}
    fake_transport.route("PATCH", "/app/hook/config", {"url": "https://x/webhook"})
    github_app.set_webhook_url("https://x/webhook")   # must not raise
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_github_app.py -k "installation_id or webhook_url" -v`
Expected: FAIL — functions don't exist.

- [ ] **Step 3: Implement the App-JWT registration helpers**

`app/config.py`: add
```python
    public_base_url: str = ""   # set from RENDER_EXTERNAL_URL on Render; PUBLIC_BASE_URL override
```
and populate the default from the environment at construction (Render provides `RENDER_EXTERNAL_URL`): add to `Settings` a validator or simply read it in `scripts/deploy.py` (see Step 4) — keep config declarative and let `deploy.py` fall back to `os.environ["RENDER_EXTERNAL_URL"]` when `public_base_url` is empty.

`app/github_app.py` — add an App-JWT (not installation) client + the two helpers, reusing the existing `Auth.AppAuth` (JWT signer) already used by `get_installation_auth`:

```python
def _app_jwt_client() -> Github:
    """Client authenticated as the App itself (JWT), for App-level endpoints."""
    return Github(auth=Auth.AppAuth(settings.github_app_id, _read_private_key()))


def discover_installation_id(repo_full_name: str) -> int:
    """Return the installation id for the App on `repo_full_name` (App JWT).
    Raises RuntimeError with an actionable message if the App is not installed —
    GitHub does not permit an App to install itself; a repo admin must authorize
    it once in the GitHub UI."""
    gh = _app_jwt_client()
    try:
        owner, name = repo_full_name.split("/", 1)
        installation = gh.get_repo(repo_full_name).get_installation()  # GET /repos/{repo}/installation
    except GithubException as exc:
        raise RuntimeError(
            f"GitHub App is not installed on {repo_full_name}: install it once via the "
            f"GitHub UI (repo Settings -> GitHub Apps), then redeploy. ({exc.status})"
        ) from exc
    return int(installation.id)


def set_webhook_url(url: str) -> None:
    """Idempotently point the App's webhook at `url` (PATCH /app/hook/config, App JWT)."""
    gh = _app_jwt_client()
    gh.requester.requestJsonAndCheck("PATCH", "/app/hook/config", input={"url": url})
```

(If PyGithub lacks a typed `get_installation()` on the repo, use
`gh.requester.requestJsonAndCheck("GET", f"/repos/{repo_full_name}/installation")` and read `["id"]` — same endpoint; adjust the test route accordingly.)

- [ ] **Step 4: Create `scripts/deploy.py` (the `/deploy` command wraps this)**

```python
"""Idempotent deploy-time registration: verify the App is installed on the
target repo and point its webhook at this deployment. Safe to run every deploy.
Uses only the existing App JWT — no new secrets."""
from __future__ import annotations

import os
import sys

from app import github_app
from app.config import settings


def main() -> int:
    repo = settings.github_target_repo
    base = settings.public_base_url or os.environ.get("RENDER_EXTERNAL_URL", "")
    if not repo or not base:
        print("GITHUB_TARGET_REPO and a public base URL (PUBLIC_BASE_URL/RENDER_EXTERNAL_URL) "
              "are required", file=sys.stderr)
        return 2
    installation_id = github_app.discover_installation_id(repo)   # raises if not installed
    github_app.set_webhook_url(f"{base.rstrip('/')}/webhook")
    print(f"registered: installation={installation_id} webhook={base.rstrip('/')}/webhook")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run tests + lint + commit**

```bash
uv run pytest tests/test_github_app.py -v && uv run ruff check .
git add app/config.py app/github_app.py scripts/deploy.py tests/test_github_app.py
git commit -m "feat(deploy): idempotent App registration helpers (verify install + set webhook)"
```

---

### Task 7: Render + CI deploy config

**Files:** Create `render.yaml`; Modify `.env.example`; verify `Dockerfile`.

**Interfaces:** none (deploy config). Live Render/Supabase provisioning is a manual verification step (documented in Task 8), not a unit test.

- [ ] **Step 1: Add `render.yaml`**

```yaml
services:
  - type: web
    name: pr-review-engine
    runtime: docker
    dockerfilePath: ./Dockerfile
    plan: free
    healthCheckPath: /healthz
    envVars:
      - key: DATABASE_URL
        sync: false
      - key: GITHUB_APP_ID
        sync: false
      - key: GITHUB_APP_PRIVATE_KEY_B64
        sync: false
      - key: GITHUB_WEBHOOK_SECRET
        sync: false
      - key: GITHUB_TARGET_REPO
        sync: false
      - key: GITHUB_MODELS_TOKEN
        sync: false
      - key: GROQ_API_KEY
        sync: false
      - key: LLM_PROVIDER
        value: groq
```
(`sync: false` = set in the dashboard, never committed. `RENDER_EXTERNAL_URL` is injected by Render automatically.)

- [ ] **Step 2: Update `.env.example`**

Add `DATABASE_URL=`, `GITHUB_TARGET_REPO=`, `GITHUB_APP_PRIVATE_KEY_B64=`, `PUBLIC_BASE_URL=` (optional; auto on Render); remove `QUEUE_DB_PATH`.

- [ ] **Step 3: Confirm the Dockerfile still boots + commit**

Run: `docker build -t pr-review-engine . && docker run --rm -e DATABASE_URL=postgres://invalid pr-review-engine python -c "import app.main"` — a clean import (it won't connect without a real DB; that's fine, this only checks the image builds and imports).

```bash
git add render.yaml .env.example
git commit -m "chore(deploy): Render blueprint + .env.example for Postgres/hosted config"
```

---

### Task 8: Documentation — README, SETUP, SPEC, cost.md

**Files:** Modify `README.md`, `SETUP.md`, `SPEC.md`, `cost.md`.

**Interfaces:** none (docs only).

- [ ] **Step 1: `SETUP.md` — the hosted deploy runbook**

Add a "Deploying to Render + Supabase" section: (1) create a Supabase project, copy the **Session-mode pooler** connection string (port 5432 pooler host) → `DATABASE_URL`; (2) create the Render web service from `render.yaml`, set the dashboard env vars (incl. `GITHUB_APP_PRIVATE_KEY_B64 = base64 of the PEM`, `GITHUB_TARGET_REPO`); (3) one-time: install the GitHub App on the testbed repo via the GitHub UI; (4) run the registration (`/deploy` / `python -m scripts.deploy`) to verify the install + set the webhook URL; (5) register the Render `/healthz` URL with a free cron pinger (cron-job.org / UptimeRobot, ~10 min) to keep it warm; note this also keeps Supabase un-paused via the dispatcher's polling.

- [ ] **Step 2: `README.md` — stop presenting the tunnel as the live path**

Replace the Cloudflare-quick-tunnel "Exposing a public webhook URL" section with the Render deployment (stable URL, no per-restart webhook edits); keep the tunnel only as an optional purely-local testing note.

- [ ] **Step 3: `SPEC.md` — §9 + §12**

§9 (deploy): Render container + Supabase Postgres; tunnel retired. §12 (queue): storage is Supabase Postgres; the atomic primitives are `FOR UPDATE SKIP LOCKED` (claim) and the transactional `SELECT ... FOR UPDATE` + `ON CONFLICT` enqueue (replacing the SQLite `BEGIN IMMEDIATE`); the target repo is configured via `GITHUB_TARGET_REPO`.

- [ ] **Step 4: `cost.md` — retired/introduced services**

Retire: Cloudflare quick tunnel + local-machine hosting. Introduce: Render free web service (750 instance-h/mo; spin-down mitigated by the keep-warm pinger), Supabase free Postgres (~500 MB; pauses after ~7 days inactivity, mitigated by the dispatcher's continuous polling while kept warm), and the free external pinger. Keep the documented monthly total at **$0** on free tiers; the LLM cost model is independent of hosting and unchanged.

- [ ] **Step 5: Lint + commit**

Run: `uv run ruff check .`
```bash
git add README.md SETUP.md SPEC.md cost.md
git commit -m "docs: Render + Supabase deployment; update service/cost model; retire tunnel"
```

---

## Self-Review (completed by plan author)

**1. Spec coverage** — every spec section maps to a task:
- §4/§5 store port + pool + atomic primitives → Task 2 (full rewrite). §5.1 pool lifecycle → Task 2 (main.py). §5.3 `FOR UPDATE SKIP LOCKED` claim + transactional enqueue → Task 2 (verbatim SQL). Sync-store-in-`to_thread` → Task 3. §6 config/secrets (`database_url`, `github_target_repo`, `public_base_url`, PEM b64) → Tasks 2/4/5/6. §7 softcode repo + webhook gate + scripts → Task 4. §8 test infra (testcontainers + CI service + truncate) → Tasks 1/2. §9 deploy (render.yaml, pinger, registration) → Tasks 6/7 + §8 docs. §9 deploy-time registration hint → Task 6 functions + `scripts/deploy.py`. §10 docs (README/SETUP/SPEC/cost) → Task 8. §12 non-goals respected (no timestamptz, no async store, no multi-tenant). §13 demo-plan re-validation → left as the noted follow-up, not a task here.

**2. Placeholder scan** — no "TBD/handle appropriately." The one large mechanical step (Task 2 Step 4: re-point test fixtures) gives the exact fixture replacement, a worked `sqlite3.connect → db_exec` example, and an explicit delete-list — actionable, not vague. The live Render/Supabase provisioning (Task 7/8) is honestly framed as manual verification, matching the project's existing "live verification" convention.

**3. Type consistency** — store function signatures/return types are unchanged from the current file (only bodies + `settings.database_url` change), so all callers/tests keep working post-port; `store.init_pool`/`close_pool`/`_require_pool` (Task 2) are used by the conftest `db` fixture (Task 1) and `main.py` (Task 2); `discover_installation_id`/`set_webhook_url`/`public_base_url` (Task 6) are consumed by `scripts/deploy.py` (Task 6); `github_target_repo` (Task 4) is read by the webhook gate and scripts.
```
