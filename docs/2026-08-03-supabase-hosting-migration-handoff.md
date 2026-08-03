# Handoff — migrate to persistent internet hosting + Supabase-backed queue

**Date:** 2026-08-03
**Status:** Open — needs its own planning session (brainstorm + plan), not a quick patch
**Relates to:** `SPEC.md` §12 (durable review queue), `app/queue/store.py`,
`README.md` (Cloudflare Tunnel hosting section), `SETUP.md` (tunnel
limitations), `docs/superpowers/specs/2026-08-03-demo-plan-design.md`
(**paused** pending this work)

## Context

This surfaced while planning a Zoom demo of the PR-review bot for a course
grading presentation. The current hosting model is: `uvicorn` + a
**Cloudflare quick tunnel**, both run from the developer's own machine,
started fresh before each session — fine for a scheduled screen-share, but
not something that exists independently of that machine being on.

Mid-planning, the user raised a real new requirement: **the instructor might
ask to be added as a collaborator on the testbed repo
(`SomeoneSomewhereelse/pr-review-bot-testbed`) and test the bot themselves**,
on their own schedule — not just watch it work once during the graded call.
That means the bot needs a **persistent, always-reachable public URL**, not
a per-session tunnel tied to a laptop being awake.

This is explicitly **not** a request to build a multi-tenant product —
**scope stays exactly as it is today**: one GitHub App installation, one
hardcoded testbed repo. The only thing changing is *where the existing app
runs*, not what it does.

### Research already done this session (informs, doesn't decide, the plan)

- Surveyed free hosting options. **Render.com**'s free web-service tier
  stood out: no credit card, deploys from the existing `Dockerfile`, runs a
  real persistent container (so the dispatcher's `run_forever()` background
  loop can actually run — unlike serverless/functions-style free tiers).
  Caveats found: free instances spin down after a period of inactivity
  (current info says up to a week, but free-tier terms shift often and
  should be re-verified at decision time), and — the important one for this
  handoff — **the filesystem is ephemeral**, wiped on every redeploy/restart.
- `app/queue/store.py`'s durable ticket queue is currently a **local SQLite
  file** (`queue_db_path`, default `./queue.db`). On an ephemeral host, that
  file — and every ticket/cooldown/`last_reviewed_at` row in it — disappears
  on every restart, which defeats the entire point of SPEC.md §12's durable
  queue (surviving a restart mid-review, absorbing rate limits across
  process lifetimes, the escalating re-review cooldown, etc.).
- The proposed fix: move the ticket store from local SQLite to **Supabase's
  free-tier hosted Postgres**, decoupling durability from wherever the
  compute happens to run. Supabase itself does **not** host the FastAPI
  app/background loop (it's a backend-as-a-service — Postgres + Auth +
  Storage + Edge Functions, not general container hosting) — it only
  replaces the *storage* layer. Compute still needs a host like Render (or
  whatever the new planning session decides).
- Also looked at Vertex AI as a possible provider change (unrelated to
  hosting, came up in the same conversation) — not part of this handoff;
  noted here only so the next session doesn't rediscover the same research:
  Vertex AI "Express Mode" may allow no-billing usage, unverified against
  this project's already Trust-Safety-flagged Google account, and out of
  scope for this hosting migration regardless.

## Explicit requirements from the user

1. **One deployment serves both purposes** — the same hosted instance used
   for the graded Zoom demo is the same instance the instructor would later
   use for their own testing. Do not build/plan two separate environments
   (e.g. a demo-only local run plus a separate "production" host).
2. **Stay single-tenant.** Still one hardcoded GitHub App installation, one
   hardcoded testbed repo. No user registration, no per-user repo/bot
   config, no multi-tenant data model. This is a scope *guardrail* for the
   upcoming design, not a feature to add.
3. Migrate the durable queue to Supabase (decided, not open) — the open
   question is *how*, not *whether*.

## What the next planning session needs to actually decide

- **Compute host.** Render was the strongest free/no-card candidate found
  this session, but wasn't formally locked in — re-verify its current free
  tier terms (they change) before committing, and confirm it can run a
  long-lived background asyncio loop (not just request/response) alongside
  the FastAPI app in the same process, as `app/main.py`'s lifespan currently
  assumes.
- **Postgres driver/library for `app/queue/store.py`.** Currently raw
  `sqlite3`, synchronous, called via `asyncio.to_thread` from async callers.
  Needs a real decision: `asyncpg`, `psycopg` (3.x, has an async mode), or an
  ORM/query-builder (SQLAlchemy Core, etc.) — and how much of the existing
  SQL needs rewriting for Postgres-dialect differences.
- **Concurrency correctness under Postgres, not SQLite.** This is the part
  that needs the most care, not a mechanical port. `store.py` has
  intricate atomicity requirements — `claim_next_due`'s
  claim-one-ticket-atomically logic, `enqueue_or_update`'s
  read-branch-write CASE logic (a known latent race even under SQLite,
  previously scoped for a `BEGIN IMMEDIATE` fix — see
  `docs/2026-07-29-comment-visibility-followups.md` / the corresponding
  design spec), and the escalating cooldown math in `_due_after_cooldown`.
  SQLite's whole-file locking and Postgres's row/MVCC locking model are
  different enough that this needs its own correctness analysis, not an
  assumption that "the same SQL mostly works."
- **Connection pooling.** Supabase's Postgres expects pooled connections
  (Supavisor/PgBouncer) for typical workloads. This app's access pattern is
  a **single serial dispatcher process** (low concurrency by design), so
  pooling is likely simple — but confirm the pooling mode (transaction vs.
  session) doesn't break whatever multi-statement transaction the
  concurrency-correctness decision above lands on.
- **Test suite impact.** The current 186 deterministic tests (per
  `README.md`) almost certainly exercise `store.py` against a real
  (temp-file or in-memory) SQLite database directly. Migrating storage to
  Postgres needs a decision that preserves CI's "no real network calls,
  fully deterministic" contract (`CLAUDE.md`'s CI conventions) — options
  include a storage-layer abstraction with a fake/in-memory backend for
  tests, a Dockerized local Postgres for CI, or something else entirely.
  Don't default to pointing CI at a real Supabase project.
- **Secrets on the new host.** `GITHUB_APP_PRIVATE_KEY_PATH` currently
  points at a local PEM file (`github-app-private-key.pem`, gitignored).
  Whatever host is chosen needs a decision: mount it as a secret file, or
  support loading the key from an env var directly (a small code change
  either way) — plus secure handling of the Supabase connection
  string/service key and the existing `.env` secrets (`GROQ_API_KEY`, etc.)
  on that platform's secret-management mechanism.
- **Cloudflare Tunnel goes away entirely** once genuinely hosted — the
  chosen platform provides its own stable public URL. This incidentally
  resolves `SETUP.md`'s documented "webhook URL changes every tunnel
  restart" limitation as a side effect, not a goal — `README.md`/`SETUP.md`
  should be updated once this lands to stop describing the tunnel as the
  live deployment path.

## Suggested prompt to continue planning

> Brainstorm and then write an implementation plan for migrating this
> project's hosting from "local machine + Cloudflare quick tunnel" to a
> genuinely persistent, always-on internet deployment, backed by a
> Supabase-hosted Postgres durable queue instead of the current local
> SQLite file. Read `docs/2026-08-03-supabase-hosting-migration-handoff.md`
> in full first — it has the complete context, the explicit scope
> guardrails (single deployment serves both the graded demo and later
> ad-hoc instructor testing; stay single-tenant, hardcoded to the one
> testbed repo — this is NOT a multi-tenant product build), and the open
> technical questions (compute host choice, Postgres driver, concurrency
> correctness under Postgres's locking model for `app/queue/store.py`,
> connection pooling, test-suite impact, secrets handling on the new host).
> Follow this repo's usual brainstorm → spec → plan conventions (see
> `docs/superpowers/specs/2026-07-27-queue-features-design.md` and
> `docs/superpowers/plans/2026-07-27-review-queue.md` for the expected
> format and rigor for the original queue feature this is modifying).
> Once the migration is planned and implemented, the paused Zoom demo plan
> at `docs/superpowers/specs/2026-08-03-demo-plan-design.md` should be
> revisited and re-validated against the new hosted environment.
