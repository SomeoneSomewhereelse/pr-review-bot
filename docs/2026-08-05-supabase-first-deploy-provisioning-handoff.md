# Handoff — verify the first-time Supabase database provisioning story

**Date:** 2026-08-05
**Status:** Open — needs investigation, not necessarily a full plan; may turn
out there's no real gap
**Relates to:** `app/queue/store.py` (`init_pool`, `_SCHEMA`), `SETUP.md` §3.1
("Supabase setup"), `docs/superpowers/specs/2026-08-03-supabase-hosting-migration-design.md`
§5.1 (pool lifecycle), `docs/superpowers/specs/2026-08-05-deploy-command-design.md`
(**paused** pending this — its `check_database` check assumes whatever this
investigation finds is already handled correctly)

## Context

This surfaced while brainstorming the `/deploy` slash command. That design's
`check_database` check needs to say something meaningful about "is the
database ready," and answering that exposed a gap in what's actually been
verified: nobody has confirmed the app's schema-provisioning story works
against a **real, brand-new Supabase project**, only against local/CI
Postgres (a container that's already fully up, same default database, no
pooler/TLS/cold-start quirks).

**How the app currently handles the schema, as read from the code:**

`app/queue/store.py:67-75`:

```python
def init_pool() -> None:
    """Open the connection pool (if not already) and ensure the schema. Idempotent."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings.database_url, min_size=1, max_size=4, configure=_configure, open=True
        )
    with _pool.connection() as conn:
        conn.execute(_SCHEMA)
```

where `_SCHEMA` is a single `CREATE TABLE IF NOT EXISTS tickets (...)`
(`app/queue/store.py:20-39`). `app/main.py`'s lifespan calls `init_pool()`
on every startup, before `recover_on_startup()` and the dispatcher task. So
the *intent* is clear: there is no separate migration tool or manual SQL
step — the app is meant to bootstrap its own schema on first boot, every
boot, idempotently. That's a reasonable design *if* the underlying
connection reliably succeeds and can run DDL on a database that has never
seen this app before.

**What SETUP.md currently documents as the human-side first-time steps**
(§3.1, `SETUP.md:171-176`): create a Supabase project, copy the
**Session-mode pooler** connection string (port 5432, not 6543) from the
dashboard, set it as `DATABASE_URL`. Nothing about waiting for the project
to finish provisioning, nothing about `sslmode`, nothing about verifying
the first connection actually works before considering the deploy done.

## Concrete unknowns to resolve

1. **TLS.** Does `ConnectionPool(settings.database_url, ...)` — with
   whatever connection string a user actually copy-pastes from the Supabase
   dashboard per SETUP.md's instructions — negotiate TLS correctly against
   Supabase's pooler? Supabase's pooler generally requires/prefers SSL;
   psycopg3's default `sslmode` (`prefer`) may or may not be sufficient
   depending on what the pooler actually enforces, and the example string in
   SETUP.md (`postgresql://postgres:[password]@[host].pooler.supabase.com:5432/postgres`)
   has no `sslmode` param at all. Verify whether a bare connection string
   this shape actually works, or whether SETUP.md needs to tell users to
   append `?sslmode=require` (and whether `init_pool` should enforce/default
   it rather than trusting the copied string).
2. **Cold project.** A brand-new Supabase project takes roughly a couple of
   minutes to finish provisioning after creation. If `init_pool()` runs (via
   Render's first boot) before the project is fully ready, the connection
   attempt fails. `init_pool()` has no retry/backoff — it's a single
   `ConnectionPool(...)` construction; per the existing design decision
   (`docs/superpowers/specs/2026-08-03-supabase-hosting-migration-design.md`
   §11, "Startup ordering... If Postgres is unreachable at boot, startup
   fails loudly (correct)"), failing loudly here was a deliberate choice for
   the steady-state case — but is it still the right call for the **very
   first** deploy, where the operator has no way to know in advance whether
   the project is ready yet? Does Render's restart-on-crash behavior turn
   this into a self-resolving retry loop in practice, or a confusing
   crash-loop the operator has to manually diagnose?
3. **Privileges.** Does the default `postgres` role in a fresh Supabase
   project's connection string have `CREATE TABLE` privileges through the
   Session-mode pooler specifically (as opposed to a direct connection)?
   Expected yes, but nobody has confirmed it against a real project — only
   assumed by analogy to local/CI Postgres, where the test/CI role is a
   fresh superuser-equivalent with no pooler in front of it.
4. **Idempotency across restarts.** `CREATE TABLE IF NOT EXISTS` self-evidently
   handles "table already exists" on every subsequent boot — that part isn't
   in question. The open question is specifically the *first* boot against a
   database instance the app has never touched.
5. **Documentation gap.** Independent of whether the connection mechanics
   actually work, should SETUP.md's §3.1 tell the operator to verify the
   first connection succeeds (e.g. by watching Render's boot logs for the
   lifespan completing, or manually testing a connection) before considering
   the Supabase step "done," rather than silently trusting that a Render
   deploy will just work?

## What's needed

Read `app/queue/store.py`'s `init_pool`/`_SCHEMA`/`ConnectionPool` usage,
`SETUP.md` §3.1-3.2, and the design spec's §5.1 (pool lifecycle) and §11
(error handling/startup ordering) in full. Then determine:

- **(a)** Is there an actual functional gap for first-time deploy, or does
  `CREATE TABLE IF NOT EXISTS` genuinely just work once the Supabase project
  is ready and the pooler connection string is correctly formed (TLS
  included)? Answer with evidence (psycopg/Supabase docs, or a live
  verification against a real free-tier Supabase project if one is
  available and this doesn't conflict with this project's LLM/API testing
  hygiene rules in `CLAUDE.md` — a one-time, deliberate check, not
  repeated probing) — not speculation.
- **(b)** If there's a real gap, what's the minimal fix? Candidates to
  weigh, not a prescribed answer: explicit `sslmode=require` guidance (or
  enforcement) in the connection-string handling; a bounded retry/backoff
  on `init_pool()`'s first connection attempt (specifically for the
  first-boot-against-a-cold-project case, without abandoning the
  deliberate "fail loudly once truly unreachable" behavior for steady
  state); or purely a documentation fix in SETUP.md (e.g., "wait ~2 minutes
  after creating the Supabase project before deploying to Render, then
  verify boot logs show a clean startup").
- **(c)** Does this warrant a new task in a plan before the parked `/deploy`
  design resumes? If yes, scope it narrowly — this is a hardening fix to
  the existing hosting migration, not new functionality. If no (confirmed
  no gap), say so explicitly so the `/deploy` design can resume with
  `check_database` as originally scoped.

## Suggested next step

This is a research/investigation task, not a design task — dispatch it as
an investigation (e.g. a read-only Explore or general-purpose agent pass
over the code/docs, plus a live check if one can be done safely and
narrowly per the constraint above) to get a definitive, evidence-backed
answer. Only loop back into brainstorming if a real gap is confirmed and a
design decision is actually needed to fix it.
