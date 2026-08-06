# Design — Supabase first-deploy hardening + first hosted run

**Date:** 2026-08-05
**Status:** Approved (brainstorming complete; implementation plan next)
**Resolves:** `docs/2026-08-05-supabase-first-deploy-provisioning-handoff.md`
**Unblocks:** `docs/superpowers/specs/2026-08-05-deploy-command-design.md` (parked)
**Relates to:** `app/queue/store.py` (`init_pool`, `_SCHEMA`), `app/main.py`
(lifespan), `SETUP.md` §1/§3.1/§3.2, `render.yaml`,
`docs/superpowers/specs/2026-08-03-supabase-hosting-migration-design.md`
§5.1/§5.2/§11, `docs/superpowers/specs/2026-08-03-demo-plan-design.md`
(Segments B and C)

## 1. Problem

Two problems, one spec.

**The hardening problem.** The investigation behind this spec resolved every
mechanical unknown in the handoff doc as *no gap*: TLS negotiates fine (libpq
18.0 with SSL compiled in, default `sslmode=prefer` tries SSL first and
Supavisor supports it); session-mode pooler is Supabase's own documented path
for DDL; the default `postgres` role has the privileges. What is **not** fine
is operator-facing:

- `SETUP.md:177`'s example connection string is the wrong shape for a pooler.
  It shows user `postgres` and `[host].pooler.supabase.com`; the real string is
  `postgres.<project-ref>@aws-<region>.pooler.supabase.com`. Reconstructing from
  the example rather than copy-pasting yields `FATAL: Tenant or user not found`,
  the most commonly reported Supabase first-connection failure.
- Measured locally: `ConnectionPool(..., open=True)` does not raise at
  construction (no `wait=True`), so a first-boot failure surfaces ~30s later as
  `psycopg_pool.PoolTimeout: couldn't get a connection after 30.00 sec`. The
  driver's real cause *is* logged (`error connecting in 'pool-1': …` at WARNING,
  reaching stderr via logging's last-resort handler), but the top-line
  exception reads like a hang. uvicorn then logs `Application startup failed.
  Exiting.` and exits **3**.
- On Render that means the deploy fails. Render does not auto-retry failed
  deploys, and its automatic instance restart applies to health-check failures
  *after* a successful deploy — so a first deploy leaves no running instance
  until a manual redeploy. Not a crash-loop; a single failed deploy plus an
  undocumented remedy.
- `SETUP.md` §1 is a past-tense record of what was done, so it never tells a
  reader how to obtain their own App ID, and §3.2 says only `(from .env)`.

**The verification problem.** The hosting-migration code landed, but the last
live E2E (SETUP.md's rehearsal table, PR #3) exercised the *pre-migration*
local tunnel path. Render + Supabase has never actually been run. Nothing has
confirmed the app provisions its own schema against a real Supabase project.

## 2. Scope

One spec, two phases, in order.

**Phase 1 (repo change):** the hardening fix (§3.1-3.4), plus two prerequisites
for anyone repeating this procedure — de-hardcoding the owner/App identifiers
(§3.5) and retiring the remaining Cloudflare Tunnel references (§3.6). All of it
lands and is committed *before* any live infrastructure exists, so Phase 2
follows the corrected docs literally and the run becomes the test of the docs.

**Phase 2 (live run, no repo changes between A0 and the end of Block B):**
provision Supabase and Render, verify first-boot schema provisioning
empirically, run the happy-path E2E, then rehearse demo Segments B and C.

### Out of scope

- The `/deploy` command — resumes after this, with the run's findings as input.
- `check_database`'s final shape — recorded as a recommendation only.
- `uptimerobot_api_key` in `app/config.py` — belongs to `/deploy`.
- Schema migrations (§12 non-goal). `CREATE TABLE IF NOT EXISTS` will not apply
  a future added column to an existing table; that stays a known limitation.
- `sslmode` enforcement in code — documented as optional hardening only.
- Supabase CLI/MCP and UptimeRobot API automation — see §6.

### Success criteria

A real PR on the testbed repo receives a real review comment from the
Render-hosted service backed by Supabase, with `tickets` created by the app's
own first boot and nothing provisioned by hand; both resilience beats observed
on the hosted stack; SETUP.md's rehearsal table carrying the hosted results.

## 3. Phase 1 — the hardening fix

### 3.1 `SETUP.md` §3.1 (Supabase setup)

Rewritten to five steps. The example connection string becomes the real
session-pooler shape:

```
postgresql://postgres.<project-ref>:<password>@aws-<region>.pooler.supabase.com:5432/postgres
```

Additions:

- Copy it verbatim from Settings → Database → Connect rather than
  reconstructing it. Both the `postgres.<project-ref>` username and the
  region-varying subdomain are project-specific, and either one wrong produces
  `FATAL: Tenant or user not found`.
- Percent-encode the password if it contains `@ # / ?`.
- Wait until the dashboard reports the project ready (~2 min) before deploying.
- One optional-hardening line: libpq's default `prefer` gets encryption but no
  certificate verification; `verify-full` plus Supabase's CA cert is the upgrade
  if wanted. Not enforced in code.

### 3.2 `SETUP.md` §3.2 (Render setup)

- `GITHUB_APP_ID: (from .env)` becomes a pointer to §1's instructions rather
  than assuming a populated `.env`.
- Note that `GITHUB_APP_INSTALLATION_ID` is intentionally omitted so the app
  auto-discovers it at boot.
- `RENDER_API_KEY` documented as **optional** operator-local tooling (§6).
- A verification step: the deploy is done only once the logs show the lifespan
  completing and `/healthz` returns 200 over the public URL.
- A troubleshooting line: a first deploy failing with `error connecting in
  'pool-1'` or a `PoolTimeout` usually means the project was not ready yet;
  Render does not retry failed deploys, so use Manual Deploy.

### 3.3 `SETUP.md` §1 (GitHub App) — obtaining the App ID

An explicit "obtaining the App ID" line: App settings →
`https://github.com/settings/apps/<app-slug>` → General → **App ID**, or
returned directly by the manifest exchange. Plus the three-way distinction,
because all three sit near each other and only two are used:

- **App ID** (`GITHUB_APP_ID`) — short integer. `app/config.py:8` types it as
  `int`, so a non-numeric paste fails config validation. `app/github_app.py:159`
  already raises "likely a bad `GITHUB_APP_ID`" on a 401.
- **Installation ID** (`GITHUB_APP_INSTALLATION_ID`) — now optional;
  auto-discovered at boot when unset. The existing `GET /app/installations`
  method stays documented for manual capture.
- **Client ID** — not used by this project at all.

`.env.example:12` gets a one-line comment pointing at SETUP.md §1.

### 3.4 `app/queue/store.py`

Two changes:

1. A module constant `_POOL_TIMEOUT_SECONDS = 30` passed explicitly as
   `ConnectionPool(..., timeout=_POOL_TIMEOUT_SECONDS)`. Same value as psycopg's
   current default, made explicit purely so a test can shrink it.
2. `init_pool()`'s `with _pool.connection()` block wrapped so `PoolTimeout`
   re-raises as a `RuntimeError` naming the three real causes — project still
   provisioning, pooler username missing its `.<project-ref>` suffix, password
   not percent-encoded — and pointing at the driver's own `error connecting in
   'pool-1'` lines above it in the log.

`RuntimeError` specifically: it matches `_require_pool()`'s existing convention,
and `app/main.py:19-24`'s lifespan comment already documents a `RuntimeError`
propagating out of startup as the intended fail-loudly path. The wrapper is
unconditional inside `init_pool()` rather than guarded on first-open, since
`init_pool()` is startup-only — any `PoolTimeout` raised there *is* a boot
failure. Semantics are unchanged: `from exc` preserves the chain, uvicorn still
exits 3.

**The message never interpolates `settings.database_url`.** It describes failure
shapes instead. The connection string carries the password, and CLAUDE.md's
no-secret-ever-logged rule is absolute.

### 3.5 Repeatability — de-hardcode the owner and App identifiers

This procedure is meant to be repeatable by someone other than its author, so no
document or docstring should name a specific GitHub account or App slug as if it
were the only one.

The **code is already correct**: `scripts/seed_demo_pr.py:38`,
`scripts/manual_verify_step3.py:27`, and `scripts/demo_provider_swap.py:32` all
read `settings.github_target_repo`, so the hosting-migration plan's
de-hardcoding step did land. Only prose lags behind it:

- **Docstrings** — `scripts/seed_demo_pr.py:9` and
  `scripts/manual_verify_step3.py:4-5` still name the owner and the App slug.
  They become references to `GITHUB_TARGET_REPO` and "the configured GitHub
  App", matching what the code actually does.
- **Test fixture** — `tests/test_github_app.py:21`'s `REPO_FULL_NAME` becomes a
  neutral placeholder. It is a fake value in a mocked test, so this is hygiene,
  not a behavior change.
- **`SETUP.md`** — lines 16, 194, 217, and 296 become `<your-user>/…`
  placeholders, and the `settings/apps/<app-slug>` URLs stop hardcoding one App
  slug. This dovetails with §3.3, which is already rewriting §1 to explain how a
  reader obtains their *own* App ID.
- **`docs/superpowers/specs/2026-08-03-demo-plan-design.md`** lines 40 and 170 —
  genericized, since that document is paused-but-live rather than a record.

Historical documents are left alone: `docs/2026-08-03-supabase-hosting-migration-handoff.md`,
`docs/superpowers/plans/2026-08-03-supabase-hosting-migration.md`, and
`docs/superpowers/specs/2026-08-03-supabase-hosting-migration-design.md` record
what was actually done at the time.

### 3.6 Retire the remaining Cloudflare Tunnel references

The Render migration retired the tunnel, but references survive in normative
docs — including `CLAUDE.md:9,25`, which still describes the deployment as
"Docker + Cloudflare Tunnel" and is the most authoritative file in the repo.

Worth stating up front: **there is no dependency to remove.** `cloudflared`
appears in no `pyproject.toml`, `Dockerfile`, or `uv.lock` entry. It was always
an external binary invoked by hand, so this is entirely a documentation change.

**Cleaned (normative):**

- `CLAUDE.md:9,25` — deployment described as Docker on Render + Supabase.
- `README.md:88-99, 139-141, 179-181` — the tunnel sections go; `README.md:207`'s
  "Groq + Cloudflare free tiers" becomes Groq + Render + Supabase free tiers.
- `SETUP.md:23-24` — §1's webhook bullet stops describing a per-tunnel-restart
  webhook edit as the live path. SETUP.md's own §3.6 (lines 251-261) is deleted
  outright,
  including the `winget`-installed-`cloudflared` note. Lines 298-300's
  tunnel-start instructions go from the redo-from-scratch notes.
- `SPEC.md:292,319` — replaced by the Render deployment path.
- `cost.md:14-15` — **both rows stay**, because they are the documented
  comparison that justifies choosing Render + Supabase, and deleting them would
  remove the rationale rather than a stale instruction. The surrounding prose is
  reworded to state plainly that both were evaluated and rejected, removing any
  implication that the tunnel is a current fallback.
- `docs/superpowers/specs/2026-08-03-demo-plan-design.md` — its environment facts
  (lines 27, 32, 58-59) and pre-call checklist (lines 161-169) move to the hosted
  stack. Its per-segment narrative is left to the §13 re-validation follow-up;
  updating the checklist without it would be half a job, but rewriting the
  choreography belongs to that separate pass, and this spec's Block B is what
  will inform it.

**Kept as historical record**, because rewriting them would falsify what
happened: `SETUP.md:284,287`'s PR #3 rehearsal row genuinely *was* delivered over
a quick tunnel — Block C adds hosted rows alongside it rather than editing it —
along with the `docs/2026-08-03-*` handoff, plan, and migration design.

### 3.7 Rejected alternatives

- **Docs-only, no code change.** Defensible — the pool already retries ~30s
  internally, the real cause is already logged, and the remedy is one redeploy.
  Rejected because it leaves the misleading top-line `PoolTimeout` in place,
  which is the one weakness actually measured.
- **Bounded retry/backoff on first connect.** Rejected: it reverses §11's
  explicit decision, adds a config knob, and makes the *common* failure (a wrong
  connection string) take minutes to report instead of 30s. The pool already
  covers the first 30s, and a failed Render deploy needs a manual redeploy
  regardless, so the retry would only buy the 30s-to-3min window.

## 4. Phase 2 — the first hosted run

Provisioning and verification are front-loaded into Block A. One hard ordering
dependency: the `tickets`-absent baseline (A2) must be observed after project
creation but before Render's first boot, or the battery has no "before" state.

### Block A — provision, then verify

- **A0** (Claude, local): full suite green, `ruff` clean, `gh auth status`
  showing the account that owns the `GITHUB_TARGET_REPO` testbed, `.env` has
  `LLM_PROVIDER=groq`. Rehearse every verification query from §7.2a against
  local Postgres.
- **A1** (operator): create the Supabase project, wait for ready, paste the
  Session-mode pooler string into `.env` as `DATABASE_URL`.
- **A2** (Claude): baseline — `SELECT 1` succeeds and
  `SELECT to_regclass('public.tickets')` returns NULL.
- **A3a** (operator, optional): create a Render API key, paste into `.env` as
  `RENDER_API_KEY`. `app/config.py:6` sets `extra="ignore"`, so an unknown var
  there is harmless and needs no code change.
- **A3b** (operator): create the service from `render.yaml` via Blueprint. With
  a key, use throwaway placeholders for the secret values; without one, enter
  the real values here.
- **A3c** (Claude, key only): discover the service ID via `GET /v1/services`,
  set the eight variables from `.env` via the **per-key** endpoint, trigger the
  deploy that counts. The eight are `DATABASE_URL`, `GITHUB_APP_ID`,
  `GITHUB_APP_PRIVATE_KEY_B64`, `GITHUB_TARGET_REPO`, `GITHUB_WEBHOOK_SECRET`,
  `LLM_PROVIDER`, `GROQ_API_KEY`, and `GITHUB_MODELS_TOKEN` — the last set now
  so B2 needs no second env round-trip. `GITHUB_APP_INSTALLATION_ID` is
  deliberately not among them.
- **A4** (operator): create the UptimeRobot monitor on `<render-url>/healthz`
  at a 5-minute interval. Created after A3b so the URL exists; it will report
  **down** until A5's deploy is live, which is expected and is itself evidence
  the monitor works.
- **A5** (Claude, one battery): deploy succeeded and boot log clean with no
  `PoolTimeout`; `/healthz` returns 200 publicly; `tickets` exists with the
  column set from §7.2a; `scripts/deploy.py` registers the webhook and exits 0;
  pinger hits appear roughly every five minutes.

The pinger check is the one criterion with an inherent wait — it needs an
~11-minute window to show two hits. It may overlap B1 rather than blocking it;
Block A is otherwise complete before Block B starts.

Installation auto-discovery is verified **by inference, not by a log line** —
`app/github_app.py` logs nothing on discovery, and a discovery failure fails
startup loudly, so a clean startup proves it resolved.

### Block B — exercise

- **B1 happy path (PR-1).** `uv run python scripts/seed_demo_pr.py`. A comment
  with three specialist rows appears; record wall-clock latency against the 15s
  target; confirm the ticket row per §7.2a.
- **B2 Segment B (provider swap).** Each "restart" is a Render redeploy: with a
  key, a per-key env update followed by an explicit deploy trigger (env changes
  do not auto-deploy); without one, a dashboard edit plus Manual Deploy.
  Set `LLM_PROVIDER=github_models`, restart,
  seed PR-2 → all three specialists render as real failed rows (partial failure
  visible, per the guarantee). Back to `groq`, restart, push a follow-up commit
  to PR-2's branch → the same comment edits in place with real findings, **and**
  the ticket survived the restart. That persistence check is new; under
  local SQLite it was not a meaningful claim.
- **B3 Segment C (quota), once.** Fire PR-3 through PR-6, then last a follow-up
  commit on PR-1. Expect roughly the first two to succeed, the rest to hit real
  429s and defer, new PRs to get plain placeholder comments, PR-1 to get the
  self-cleaning footnote rather than an overwrite, and all to recover without
  intervention. Record actual counts rather than the predicted ones.

A Render env-var change is a redeploy (~1-2 min), not a two-second `uvicorn`
restart. The beats still work, but the demo plan's narration timing needs
revisiting; measured restart duration is a recorded output.

### Block C — record

Hosted rows added to SETUP.md's rehearsal table; the findings doc (§8);
`RENDER_API_KEY` revoked; the handoff doc marked resolved and the `/deploy`
design's `Status: Paused` lifted. Service left running so the pinger keeps it
warm.

## 5. Discipline

**Observation only.** "Fix nothing during the run" means **no repo changes**
between A0 and the end of Block B. Correcting operator-side configuration (a
mistyped connection string, a missing env var, a monitor interval) is part of
*executing* the run, not fixing it. A genuine code-level blocker stops the run
where it stands, gets recorded, and resumes in a second sitting after a
follow-up fix. Doc divergences are recorded as they happen and applied in one
pass in Block C — not edited mid-flight.

**Secret hygiene.** `RENDER_API_KEY` and `DATABASE_URL` live only in the
gitignored `.env`, never echoed into commands, output, or the findings doc.
Render log quotes get scrubbed of anything token-shaped. Nothing new goes into
`render.yaml`.

**LLM hygiene (CLAUDE.md).** Segment C runs exactly once. If Groq returns 403
rather than 429, stop calling it immediately and record it as a finding rather
than retrying. No model shopping. `github_models` is used once, for Segment B,
where failure is the expected result — a retired endpoint, not a quota probe.

## 6. Tooling

**Decision: `gh` + `git` + psycopg + `scripts/deploy.py`, plus an optional
Render API key. Nothing else.**

Removing `gh` was considered and rejected: its token lives in the OS keyring,
outside the repo and outside `.env`, so every replacement is *worse* on secret
exposure — a PAT is a new credential in `.env`, SSH keys are new key management,
and `git push` currently rides `gh`'s credential helper anyway. The App cannot
substitute: `contents: read` (SETUP.md:14) cannot push a branch, and having the
bot author the PRs it reviews would corrupt the demo's semantics. Opening PRs by
hand is viable for the happy path but fragile for Segment C's rapid-fire
sequence during a narrated call.

Supabase CLI and UptimeRobot API were rejected on friction accounting.
`supabase projects create` removes a few clicks but not the dashboard visit,
since the pooler string still comes from the Connect panel — near-zero friction
removed for the broadest credential of the three. An UptimeRobot monitor is four
clicks once, and 5-minute intervals are the free-tier default.

The Render API key earns its place on two frictions: **secret transcription**
in A3 (eight variables including a ~1700-character base64 PEM, where both
plausible slips are silent and produce exactly the confusing failures this spec
exists to explain), and **boot-log access**. It also covers Segment B's two env
flips, including Render's don't-auto-deploy behavior.

Hard rule: only the **single-key** add-or-update endpoint. Render's
`PUT /v1/services/{id}/env-vars` replaces the entire list and would silently
delete `DATABASE_URL` and the App key.

Cost, stated plainly: Render API keys are account-scoped, so the key can delete
services, not just read logs. Mitigation is create-for-the-run, revoke in
Block C, operator-local in `.env`, never in `render.yaml` — the same principle
`/deploy` already established for `UPTIMEROBOT_API_KEY`.

### 6.1 Optional, with a real fallback

`RENDER_API_KEY` is optional in both `.env` and `.env.example`, documented as an
accelerator so a reader deploying their own instance is never confused into
thinking it is required. Absent, everything still completes; **no step's pass
criteria change**, only how evidence is acquired.

| Step | With the key | Without it |
|---|---|---|
| A3 env vars | Set from `.env` via per-key endpoint, then trigger deploy | Operator enters them in the Blueprint form; the PEM is the likeliest silent slip, verified by effect |
| A5 boot verification | Read the deploy's log stream directly | Operator pastes lines matching `Application startup complete`, `Application startup failed`, `error connecting in 'pool-1'`, `Exited with status` |
| A5 schema check | psycopg — no key involved | identical |
| A5 webhook | `scripts/deploy.py` — no key involved | identical |
| A5 pinger | Deploy log, `GET /healthz` every ~5 min | Operator pastes an ~11-minute window of access lines |
| B2 restart duration | `GET /v1/services/{id}/deploys` timestamps, precise | Render Events tab, coarser |
| B1-B3 review outcomes | DB + PR comments — no key involved | identical |

The fallback lane's only real cost is coarser restart timing plus a couple of
targeted paste requests. The findings doc records which lane was used, so nobody
later mistakes coarse numbers for precise ones.

This works because the app logs sparsely — three lines in `app/webhook.py`, six
`logger.exception` calls in `app/queue/dispatcher.py`, and nothing for a
completed review, a provider 429, or installation discovery. Segment C's
rate-limit evidence therefore lives in the **database** and the **PR comments**,
both directly readable without any Render key.

## 7. Testing

### 7.1 Phase 1 unit tests (new)

Existing coverage: `tests/test_main_lifespan.py` covers `init_pool()` running
inside lifespan and both discovery branches; `tests/test_queue_store.py` covers
the store functions. Neither covers `init_pool()`'s failure path or the schema's
shape. Five additions, all offline:

1. **Actionable error.** Unreachable `127.0.0.1:1` with `_POOL_TIMEOUT_SECONDS`
   monkeypatched to ~1: `init_pool()` raises `RuntimeError` naming all three
   causes, `__cause__` is `PoolTimeout`.
2. **No secret in the message.** The test URL carries a distinctive sentinel
   password; assert it and the full URL are absent from `str(exc)` and the
   exception's `args`. Guards the one rule where a future well-meaning "include
   the URL for debuggability" edit would do real damage.
3. **Wrapper stays narrow.** A malformed conninfo propagates as its own psycopg
   error, unwrapped, so the friendly message cannot mask a different fault. Also
   asserts `store._pool is None` afterward, guarding against a refactor leaving
   a half-built pool installed globally.
4. **Schema matches the dataclass.** After `init_pool()` against the test
   Postgres, the `information_schema.columns` set for `tickets` equals
   `Ticket.__dataclass_fields__` exactly. `_row_to_ticket` does `Ticket(**row)`,
   so divergence between `_SCHEMA` and the dataclass is a runtime `TypeError`
   today. This also turns A5's column criterion into a machine-checked
   invariant.
5. **Lifespan still fails loudly.** Extend `test_main_lifespan.py`: an
   unreachable DB means `async with main.lifespan(main.app)` raises and no
   dispatcher task is left running. Pins §11's deliberate decision.

### 7.2 Phase 2 coverage

A runbook cannot have unit tests. It gets these instead.

**a. Pass criteria as literal queries, not prose.**

| Stage | Query | Expected |
|---|---|---|
| A2 | `SELECT to_regclass('public.tickets')` | NULL |
| A5 | `SELECT column_name FROM information_schema.columns WHERE table_name='tickets'` | set equals `Ticket.__dataclass_fields__` (15 columns) |
| B1 | `SELECT status, comment_id IS NOT NULL, last_reviewed_at IS NOT NULL FROM tickets WHERE pr_number = %s` | `('done', true, true)` |
| B2 | same ticket `id` before and after the restart | `id` unchanged, `updated_at` advanced |
| B3 | `SELECT status, not_before FROM tickets WHERE status IN ('deferred','retrying')` | non-empty with non-null `not_before` during the storm; all `done` after recovery |

**b. Rehearse every one of those against local Postgres in A0**, before Supabase
exists — so live infrastructure is never the place SQL gets debugged. This is
the coverage for the verification tooling itself.

**c. Exercise the fallback lane once where it is nearly free.** At A5's boot
check, do it both ways: read via the API *and* have the operator paste that same
window once. Both lanes proven for the cost of one paste; same for the pinger.
Segment C is the honest exception — it runs once, so its fallback stays
unexercised.

**d. Label unexercised paths unverified.** Whichever lane goes unused is marked
unverified in SETUP.md rather than presented as equally proven. A doc claiming
two working paths when one never ran is precisely how this bites later.

**e. Full suite as a tripwire** before A0 and again after Block C — unchanged,
confirming the run did not perturb the repo.

## 8. Deliverables

1. **Phase 1**, as three separate commits so each is reviewable on its own terms:
   - **Hardening** — SETUP.md §1/§3.1/§3.2, the `.env.example` App ID comment and
     optional `RENDER_API_KEY` entry, `store.py`'s `_POOL_TIMEOUT_SECONDS` and
     diagnostic wrapper, and the five tests (§3.1-3.4, §7.1).
   - **Repeatability** — docstrings, the test fixture, and the SETUP.md and
     demo-plan identifiers (§3.5).
   - **Tunnel retirement** — CLAUDE.md, README.md, SETUP.md §3.6, SPEC.md,
     cost.md's rewording, and the demo plan's environment/checklist (§3.6).
2. **`docs/2026-08-05-first-hosted-run-findings.md`** — divergences from the
   corrected docs, which tooling lane was used, measured Render restart duration
   for the demo plan's timing, actual Segment C counts, whether the diagnostic
   wrapper fired, and a concrete `check_database` recommendation.
3. **SETUP.md rehearsal table** — hosted rows (Block C, after the run).
4. **Close-out** — handoff doc marked resolved; `/deploy` design's
   `Status: Paused` lifted.

## 9. Recorded findings (observations, deliberately not fixed here)

- `app/webhook.py:57`'s `logger.info` never surfaces under uvicorn's default
  root level of WARNING, so the non-target-repo skip is invisible in production.
- `/deploy`'s `check_database` should use a plain short-timeout
  `psycopg.connect`, **not** the app's pool, so it reports the real libpq error
  instead of a 30-second `PoolTimeout`. Whether it also asserts `tickets` exists
  is decided by what Block A observes.
