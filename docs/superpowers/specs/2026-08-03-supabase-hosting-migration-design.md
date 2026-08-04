# Design — Persistent hosting (Render) + Supabase Postgres durable queue

**Date:** 2026-08-03
**Status:** Approved for planning
**Relates to:** `docs/2026-08-03-supabase-hosting-migration-handoff.md` (the
handoff/context), `SPEC.md` §9 (deploy) + §12 (durable queue), `app/queue/store.py`
(the store being ported), `README.md`/`SETUP.md` (tunnel hosting docs to retire),
`cost.md` (service/cost model to update), `docs/superpowers/specs/2026-08-03-demo-plan-design.md`
(**paused** — re-validated as a follow-up, §12).

## 1. Problem

Today the app runs as `uvicorn` + a **Cloudflare quick tunnel**, both from the
developer's laptop, started fresh per session — it doesn't exist independently of
that machine being awake, and the tunnel hostname changes every restart. A new
requirement: the instructor may be added as a collaborator on the testbed repo
and test the bot **on their own schedule**, so the bot needs a **persistent,
always-reachable public URL**. And on an ephemeral-filesystem host, the durable
queue's local SQLite file (`queue.db`) would be wiped on every restart, defeating
SPEC.md §12's whole point (surviving restarts, absorbing rate limits, the
escalating cooldown). Fix: host the app on a persistent free container platform,
and move the queue off the local file into **Supabase-hosted Postgres**.

## 2. Scope guardrails (hard)

- **Single-tenant, unchanged behavior.** Still one GitHub App installation, one
  target testbed repo served at a time. This migration **softcodes** that repo
  from source/scripts into a single config value (§6, §7) — still exactly one
  repo, still single-tenant: **no** user registration, per-user config, dynamic
  repo discovery, or multi-tenant data model. Only *where the app runs*, *where
  the queue persists*, and *how the one repo is configured* change — not what it
  does.
- **One deployment serves both** the graded Zoom demo and later ad-hoc
  instructor testing. Not two environments.
- **Migrating the queue to Supabase is decided** (the handoff settled *whether*);
  this spec settles *how*.

## 3. Decisions (locked during brainstorming)

| Area | Decision |
|---|---|
| Compute host | **Render free web service** (deploys the existing `Dockerfile`, auto HTTPS URL, no card) |
| Always-on | External **keep-warm pinger** (cron-job.org/UptimeRobot → `/healthz` ~every 10 min) within Render's 750 instance-h/mo |
| Queue storage | **Supabase free Postgres**, single backend everywhere (local Postgres for tests) — SQLite removed entirely |
| Driver | **psycopg3** (`psycopg[binary]` + `psycopg_pool`), raw SQL |
| Store async-ness | **Synchronous** store functions over a module-level connection pool; async callers wrap in `asyncio.to_thread` |
| Connection | Supabase **Session-mode pooler** (IPv4) + a small client pool (~1–4 conns) |
| Timestamps | Keep columns **`TEXT` ISO-8601** (lexical comparison already correct; minimal change) |
| Tests | DB-touching tests hit a **throwaway local Postgres** (testcontainers locally, `services: postgres` in CI); never Supabase |
| PEM secret | Load from **base64 env var** (`GITHUB_APP_PRIVATE_KEY_B64`), file-path fallback for local dev |
| Target repo | **Configured** via `GITHUB_TARGET_REPO` (single repo, still single-tenant) |
| Cloudflare Tunnel | **Retired** |

## 4. Architecture

The FastAPI app and the in-process dispatcher loop (`app/main.py`'s lifespan
starting `run_forever`) run **in the same Render container** — unchanged; Render's
free web service keeps that one process alive (when awake), so the co-located
background loop runs exactly as it does locally. The **only** durability change is
the queue's storage: `app/queue/store.py` reads/writes **Supabase Postgres**
instead of a local SQLite file, decoupling durability from the compute host's
ephemeral disk.

**Always-on within free tiers.** Render free spins the instance down after ~15 min
idle; a keep-warm pinger hitting `/healthz` keeps the one instance awake (fits
inside 750 instance-h/mo for a single instance). Because the dispatcher polls
`store.claim_next_due` every tick, a kept-warm app also issues continuous Postgres
activity, which incidentally keeps the **Supabase free project from pausing** (free
projects pause after ~7 days of *inactivity*). If the pinger lapses long enough,
both spin down and a push/webhook cold-starts the stack (≈1 min).

## 5. The store port — `app/queue/store.py` (the crux)

Single Postgres backend for local tests *and* Supabase prod — **no dual dialect;
`sqlite3` is removed**. Store functions **stay synchronous** (plain `def`, raw
SQL) over a **module-level `psycopg_pool.ConnectionPool`** that replaces the
`_connect()`-per-call pattern; each function does `with _pool.connection() as conn:`.
Async callers (`dispatcher.process_next_due`, `webhook`) wrap store calls in
`asyncio.to_thread` so Postgres network latency (tens of ms) never blocks the event
loop — the same pattern already used for GitHub calls.

### 5.1 Pool lifecycle

A new `store.init_pool()` opens the pool from `settings.database_url` and creates
the schema; `store.close_pool()` closes it. `app/main.py`'s lifespan calls
`init_pool()` (before `recover_on_startup`) and `close_pool()` on shutdown. Tests
call `init_pool()` against the local-Postgres URL in a fixture.

### 5.2 Schema

One `CREATE TABLE IF NOT EXISTS tickets (...)` carrying **all** current columns
(the `PRAGMA table_info` + `ALTER TABLE`-based idempotent migration is dropped —
a fresh Postgres DB has no legacy rows to backfill). `id INTEGER PRIMARY KEY` →
`id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY`. `UNIQUE(repo_full_name,
pr_number)` kept. **Timestamp columns stay `TEXT`** (`not_before`, `updated_at`,
`enqueued_at`, `last_reviewed_at`, `notice_not_before`) — ISO-8601 UTC lexical
comparison (`not_before <= %s`, `notice_not_before != not_before`) is already
correct and keeps every Python `datetime` path identical.

### 5.3 Atomic primitives (correctness under Postgres, not a mechanical port)

- **`claim_next_due`** — one atomic statement (replaces the SQLite
  SELECT-then-guarded-UPDATE):
  ```sql
  UPDATE tickets SET status='running', updated_at=%s, rereview_requested=0
  WHERE id = (
    SELECT id FROM tickets
    WHERE status='pending'
       OR (status IN ('deferred','retrying') AND not_before IS NOT NULL AND not_before <= %s)
    ORDER BY enqueued_at ASC, id ASC
    LIMIT 1
    FOR UPDATE SKIP LOCKED
  )
  RETURNING *;
  ```
  `FOR UPDATE SKIP LOCKED` is the canonical safe work-queue claim — a second
  concurrent claim can never grab the same row.

- **`enqueue_or_update`** — a real transaction (replaces `BEGIN IMMEDIATE`):
  `SELECT * FROM tickets WHERE repo_full_name=%s AND pr_number=%s FOR UPDATE`
  (locks the row if it exists) → branch on `status` → `UPDATE`. For the no-row
  case: `INSERT ... ON CONFLICT (repo_full_name, pr_number) DO NOTHING RETURNING id`;
  if that returns no row (a concurrent insert won), a follow-up
  `SELECT id ... FOR UPDATE` **blocks until the winner commits, then reads the
  committed row**. The read-branch-write is thus atomic even under real
  concurrency (today's single process never triggers the race, but the primitive
  is correct, not merely incidental). Branch logic (`running`→dirty flag;
  `pending`/`deferred`/`retrying`→ride-out; `done`/`failed`→`_due_after_cooldown`
  re-arm) is unchanged — only the locking envelope changes.

- **Everything else is already single-statement/atomic** and ports mechanically
  (`?`→`%s`, driver API, `RETURNING`/`rowcount` where used): `finalize_review`
  (single `UPDATE ... CASE`), `defer_rate_limited`, `defer_failed`, `mark_failed`,
  `mark_notice_posted`, `clear_notice`, `recover_on_startup`, `tickets_needing_notice`
  (batch `LIMIT` SELECT), `get_ticket`. `_due_after_cooldown`, `effective_cooldown`,
  `next_cooldown_level` are pure Python — untouched.

### 5.4 Row mapping

`_row_to_ticket` maps a psycopg row to the `Ticket` dataclass. Use
`psycopg.rows.dict_row` so rows are dict-like (keeps `_row_to_ticket`'s by-name
construction). Column list and `Ticket` fields are unchanged.

## 6. Config & secrets — `app/config.py`, `app/github_app.py`

- Remove `queue_db_path`; add **`database_url: str`** (psycopg connection string).
  Local/CI point it at the throwaway Postgres; prod at Supabase's Session-mode
  pooler URL (IPv4 — Supabase *direct* is IPv6-only and Render egress is IPv4).
- Add **`github_target_repo: str`** (env `GITHUB_TARGET_REPO`, e.g.
  `SomeoneSomewhereelse/pr-review-bot-testbed`) — the single repo this deployment
  serves. The webhook gates on it (§7) and scripts read it instead of a hardcoded
  literal. Still one repo — this is configuration, not multi-tenancy.
- **PEM:** add `github_app_private_key_b64: str = ""`. `github_app._read_private_key`
  prefers it (base64-decode → PEM text) and falls back to
  `github_app_private_key_path` for local dev. Base64 avoids multiline-in-dashboard
  breakage and is host-agnostic. Never logged (unchanged rule).
- All other secrets (`GROQ_API_KEY`, `GITHUB_WEBHOOK_SECRET`, `GITHUB_APP_ID`,
  `GITHUB_APP_INSTALLATION_ID`, `GITHUB_MODELS_TOKEN`, `DATABASE_URL`) → **Render's
  env-var dashboard** in prod; `.env` stays the local mechanism. `.env.example`
  updated (add `DATABASE_URL`, `GITHUB_APP_PRIVATE_KEY_B64`, `GITHUB_TARGET_REPO`;
  drop `QUEUE_DB_PATH`).
- Dependencies: add `psycopg[binary]`, `psycopg_pool`; add `testcontainers` to the
  dev group.

## 7. Softcoding the testbed repo (hardcoded → configured)

Today the app is single-tenant by *installation scope*, not by code:
`app/webhook.py` derives `repo_full_name` from the webhook payload and would
process any repo the App installation delivers events for; the repo literal only
appears in scripts (`scripts/seed_demo_pr.py`, `manual_verify_step3.py`,
`demo_provider_swap.py`), `tests/test_github_app.py`, and `SETUP.md`. This
migration makes the single served repo an explicit config value — cleaner for a
real deployment (no repo baked into source) and a small defense-in-depth for
single-tenancy. **Still one repo; this is not multi-tenant.**

- **Config:** `settings.github_target_repo` (§6).
- **Webhook gate (`app/webhook.py`):** after HMAC verification and the existing
  action filter, ignore any payload whose `repo_full_name !=
  settings.github_target_repo` — return the same `202` no-op the app already uses
  for irrelevant actions (a valid *signed* webhook for a different repo is simply
  not ours to review; it is not a `401`). This makes "one repo" explicit and
  enforced in code, independent of the App's installation scope.
- **Scripts:** `seed_demo_pr.py` / `manual_verify_step3.py` /
  `demo_provider_swap.py` read the repo from `settings.github_target_repo` (env)
  instead of the hardcoded string, so a redeploy or a different testbed needs
  only an env change.
- **Tests:** `tests/test_github_app.py` keeps a test-local repo constant (its
  mocked transport doesn't depend on the real repo); tests that reach the webhook
  gate set `settings.github_target_repo` via the existing settings-monkeypatch
  pattern. Add a webhook test asserting a non-target repo is a `202` no-op.
- **Docs:** `SETUP.md` documents `GITHUB_TARGET_REPO` as the single-repo knob.

## 8. Test infrastructure

DB-touching tests (`tests/test_queue_store.py`, `tests/test_dispatcher.py`,
`tests/test_webhook.py`, `tests/test_main_lifespan.py`) run against a **throwaway
local Postgres**:

- **Local:** a session-scoped `testcontainers` Postgres container; a fixture sets
  `settings.database_url` to it and calls `store.init_pool()`.
- **CI:** a GitHub Actions **`services: postgres:16`** container (localhost) — the
  workflow sets `DATABASE_URL` to it. Deterministic, localhost-only, **never
  Supabase**, preserving CLAUDE.md's "no real network / fully deterministic" CI
  contract.
- **Per-test isolation** switches from "temp sqlite file per test" to
  **`TRUNCATE tickets RESTART IDENTITY`** between tests (an autouse fixture).
- Pure-logic tests (cooldown math, formatting, providers, HMAC) are DB-less and
  stay instant/unchanged.

Docker is already a project dependency (SETUP.md), so `uv run pytest` stays a
single command (testcontainers auto-spins the container).

## 9. Deploy & ops

- **`render.yaml`** (blueprint) declaring the web service: Docker runtime from the
  repo `Dockerfile`, `healthCheckPath: /healthz`, free plan, env vars referenced
  (values set in the dashboard, not committed). Port already `8000` / `--host 0.0.0.0`.
- **Keep-warm pinger:** documented deploy step — register the Render URL's
  `/healthz` with cron-job.org or UptimeRobot at ~10-min interval.
- **GitHub App webhook URL** updated once to the stable Render URL (no longer
  per-tunnel-restart).
- `Dockerfile` unchanged (already `uv sync --frozen --no-dev` + uvicorn).

## 10. Docs updates

- **`README.md`/`SETUP.md`:** describe the Render + Supabase + pinger deployment;
  **stop presenting the Cloudflare quick tunnel as the live path** (the
  "webhook URL changes every restart" limitation disappears as a side effect,
  not a goal). Keep the tunnel only as an optional local-testing note if useful.
- **`SPEC.md`:** §9 (deploy → Render + Supabase, tunnel retired) and §12 (queue
  persistence → Supabase Postgres; the atomic primitives now `FOR UPDATE
  SKIP LOCKED` / transactional `enqueue_or_update`).
- **`cost.md`:** update the service/cost model — **retire** Cloudflare quick
  tunnel + local-machine hosting; **introduce** Render free web service (750
  instance-h/mo; spin-down mitigated by the pinger), Supabase free Postgres
  (~500 MB; pauses after ~7 days inactivity, mitigated by the dispatcher's
  continuous polling while kept warm), and the free external pinger. Documented
  monthly total stays **$0** on free tiers; the LLM cost model (§2, ~$4–5/mo at
  brief scale) is independent of hosting and unchanged.

## 11. Error handling / operational notes

- **Startup ordering:** `init_pool()` → `recover_on_startup()` (resets stale
  `running` rows) → `run_forever()`. If Postgres is unreachable at boot, startup
  fails loudly (correct — the app can't function without its queue).
- **Transaction/rollback:** each store transaction commits or rolls back within
  its `with _pool.connection()` block; the pool returns the connection either way.
  The single-serial-dispatcher access pattern means transaction-mode-pooler
  prepared-statement gotchas don't apply — Session-mode + a client pool sidesteps
  them entirely.
- **Polling cost:** the 1s-idle poll issues one lightweight indexed `SELECT`/sec;
  negligible for Supabase free, and it's what keeps the project un-paused.

## 12. Out of scope / non-goals

- Multi-tenant anything (registration, per-user repos/bots, dynamic repo
  discovery) — explicit guardrail. Softcoding the single served repo (§7) is
  configuration, not multi-tenancy.
- `timestamptz` columns / async store functions / an ORM — deliberately not done
  (minimal-change, low-risk port).
- Provider changes (Vertex "Express Mode" etc.) — unrelated, noted in the handoff.
- Horizontal scaling — still one instance, one dispatcher (the `FOR UPDATE
  SKIP LOCKED` claim would *permit* it later, but it is neither built nor tested).

## 13. Follow-up (separate session)

Once this lands, **re-validate the paused Zoom demo plan**
(`docs/superpowers/specs/2026-08-03-demo-plan-design.md`) against the hosted
environment: warm the instance before the call, use the stable Render URL, and
re-check the two live resilience beats against Supabase-backed persistence. Not
built here.
