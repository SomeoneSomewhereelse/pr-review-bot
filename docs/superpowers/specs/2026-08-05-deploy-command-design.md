# Design — deploy verification CLI (`scripts/deploy.py`) + `/deploy` wrapper

**Date:** 2026-08-05 (design completed 2026-08-07)
**Status:** Approved for planning

> **The CLI is the deliverable.** `scripts/deploy.py` is a standalone tool that
> anyone can run with `uv run python -m scripts.deploy`, with no Claude Code and
> no assistant involved. `/deploy` is a thin convenience wrapper around it for
> people who happen to use Claude Code — one of two front doors, not the main
> one. Every design decision below is made for the CLI first; §4.2 states the
> boundary between what it automates and what remains irreducibly manual.

**Relates to:** `scripts/deploy.py` (the registration script this design
extends), `app/github_app.py` (`discover_installation_id`, `set_webhook_url`),
`app/queue/store.py` (`init_pool`/`_SCHEMA` — deliberately *not* reused, §7.5),
`app/main.py` (`/healthz`, now `GET` + `HEAD`),
`docs/superpowers/specs/2026-08-03-supabase-hosting-migration-design.md` §9
("Deploy-time registration — hint only... to become a `/deploy` slash command"),
`docs/superpowers/plans/2026-08-03-supabase-hosting-migration.md` Task 6 (built
`scripts/deploy.py`), `docs/2026-08-05-first-hosted-run-findings.md` (the
empirical run that unblocked and reshaped this design),
`docs/2026-08-05-supabase-first-deploy-provisioning-handoff.md` (resolved).

## 1. Problem

Getting this app actually working on Render + Supabase requires roughly a
dozen things to be simultaneously true: seven service env vars set correctly,
a GitHub App installed on the target repo, its webhook pointed at the right
URL, a reachable Postgres whose schema the app has provisioned, a live
service, and an external pinger keeping it warm. Today, verifying that is a
manual walk through `SETUP.md` §3, and **most failures are silent from the
outside**: the service returns 200 while the pinger quietly 404s, or the queue
accepts tickets and fails every review because one env var is wrong.

The first hosted run (`docs/2026-08-05-first-hosted-run-findings.md`) made
this concrete. Two real defects hid behind a healthy-looking deployment:

- The keep-warm monitor's URL had a trailing comma (`/healthz,`) — 14
  consecutive `404`s over 71 minutes, on schedule, invisible.
- After that was fixed, the pinger *still* failed: UptimeRobot's free tier
  sends `HEAD`, and `/healthz` was registered `GET`-only, returning `405`. No
  dashboard setting could fix it; it needed a code change (`ed4ec55`).

Neither was visible without deliberately probing. `scripts/deploy.py` today
does the two registration steps and nothing else, so it could not have caught
either.

## 2. Scope guardrails (hard)

- **Not an infrastructure provisioner — but genuinely a deploy driver.** It
  never *creates* Supabase projects, Render services, GitHub Apps, or
  UptimeRobot monitors; each is a one-time manual step (§4.2). It does
  *deploy to* infrastructure that already exists: `--sync-env` pushes config,
  triggers the deploy, waits for it to go live, and verifies the result.
- **Usable without Claude Code.** No check's output depends on an assistant
  reading it. Every `FAIL` line is actionable on its own (§7.4), and the exit
  code is scriptable (§7.2), so the CLI works the same in a terminal, a
  Makefile, or CI.
- **The default invocation is safe to re-run.** A bare run performs no
  destructive or surprising remote mutation (§6.2 is the single, narrow
  exception, and it self-reports). Anything that pushes secrets is behind an
  explicit opt-in flag (§8).
- **No secret is ever printed, logged, or written to a report.** Inherited
  verbatim from `CLAUDE.md`; §9 states how each check honors it.
- **CI stays deterministic and offline.** Every test mocks HTTP (`respx`) and
  Postgres (the existing harness or a dead port). No test in this feature
  makes a live call to GitHub, Render, UptimeRobot, or Supabase.
- **Single-tenant, unchanged.** One configured repo, one service. Nothing here
  introduces multi-tenancy.

## 3. Decisions (locked during brainstorming)

| Area | Decision |
|---|---|
| Command scope | **Comprehensive pre-flight checklist**, not a thin wrapper around the existing script |
| Where logic lives | **Tested Python in `scripts/deploy.py`**; the command file is a thin wrapper (rejected: curl/bash logic inside the markdown — untestable) |
| Failure mode | **Run every check, print a full table** (rejected: fail-fast — forces re-runs to discover each next problem) |
| Code structure | **Composable per-check functions** returning `CheckResult` (rejected: one inline `main()`; a declarative registry — YAGNI for a fixed six) |
| Optional checks | **Skip with an actionable hint** when unconfigured; **fail the run** when attempted and genuinely broken |
| Health check | **`GET` *and* `HEAD`**, both must be 200 |
| Webhook check | **Read first, write only on mismatch**; report "already correct" vs "updated" |
| Database check | **`connect` + `SELECT 1` + `to_regclass('public.tickets')`**, via raw `psycopg` — never `store.init_pool()` |
| Pinger | **UptimeRobot** (free tier: 5-min interval, simple REST API), via optional `UPTIMEROBOT_API_KEY` |
| Render API | **Optional `check_render_service`** + an opt-in `--sync-env` mode, via optional `RENDER_API_KEY` |
| Env sync | **Explicit `--sync-env` flag**, single-key endpoint only, then trigger a deploy and poll until `live` |
| Primary interface | **The standalone CLI** (`python -m scripts.deploy`); `/deploy` is a wrapper, not the product |
| Output | **Terse aligned table**, fragments not sentences; explanatory depth lives in the docs (§7.4) |

## 4. Architecture

Three files carry the feature; each has one job.

**`scripts/deploy.py`** — the product. All the logic, as plain testable
Python. Keeps its existing `python -m scripts.deploy` entry point and its
existing dependencies (`app.github_app`, `app.config`); gains `httpx` (already
a project dependency) and `psycopg` (already a project dependency). No new
packages. Nothing about it assumes Claude Code, an assistant, or an
interactive terminal.

**`.claude/commands/deploy.md`** — a thin convenience wrapper for Claude Code
users. Frontmatter with a one-line description. Body instructs: run
`uv run python -m scripts.deploy`, show the printed table verbatim, and on a
non-zero exit help the user act on each `FAIL` line using the hint that line
already printed. It also documents `--sync-env` as the follow-up when the
diagnosis is config drift. **No verification logic lives here** — the markdown
cannot be tested, so it holds none of the behavior, and deleting this file
would cost convenience but no capability.

**`app/config.py`** — three new optional fields (§5).

### 4.1 `CheckResult`

```python
@dataclass(frozen=True)
class CheckResult:
    name: str                                    # column 1 of the table
    status: Literal["PASS", "FAIL", "SKIPPED"]   # column 2
    detail: str                                  # column 3: outcome, or hint when SKIPPED
```

`detail` is the whole user experience for a failing line: it must name what is
wrong and what to do, because a terminal user has nothing else to work from.

### 4.2 Standalone use — the automation boundary

The honest answer to "can someone run this and get a working deployment?" is
**yes, after a one-time setup** — and the boundary is not arbitrary.

**Fully automated by `--sync-env` (repeatable, every deploy):**

| Step | Mechanism |
|---|---|
| Push all eight service env vars | `PUT /v1/services/{id}/env-vars/{KEY}` (§8) |
| Trigger the deploy | `POST /v1/services/{id}/deploys` |
| Wait until it is actually serving | poll until `live` (~55–65s measured) |
| Discover the installation id | App JWT, `GET /repos/{repo}/installation` |
| Point the webhook at this deployment | `PATCH /app/hook/config`, only on drift |
| Verify all six checks | §6 |

**One-time manual prerequisites** — the CLI *reports* each as a `FAIL` naming
what is missing, but cannot perform it:

| Step | Why it stays manual |
|---|---|
| Install the GitHub App on the target repo | **Structurally impossible to automate.** GitHub does not permit an App to install itself; a repo admin must authorize it in the GitHub UI. This is the one true hard stop. |
| Create the Supabase project | Needs account-level credentials far broader than any check here holds; also has a wait-for-ready step no API makes reliable (§1 of the hardening spec). |
| Create the Render service from `render.yaml` | Blueprint creation is account-level; the service must exist before `--sync-env` has anything to target. |
| Create the UptimeRobot monitor | Account-level; and a monitor is worth creating once, deliberately, with the interval and URL the check then verifies. |

Consequently the CLI has two distinct, useful modes for a non-Claude user:

- **Before setup is complete** — a bare run is a diagnostic checklist that
  names precisely which prerequisite is missing, one per line.
- **After setup is complete** — `--sync-env` is a complete, repeatable,
  one-command deploy.

## 5. Config

Added to `app/config.py`, all optional, all **operator-local tooling** — none
is ever added to `render.yaml` or given to the deployed service:

| Field | Env var | Default | Purpose |
|---|---|---|---|
| `uptimerobot_api_key` | `UPTIMEROBOT_API_KEY` | `""` | Read-only key; enables the pinger check |
| `render_api_key` | `RENDER_API_KEY` | `""` | Enables the service check and `--sync-env` |
| `render_service_name` | `RENDER_SERVICE_NAME` | `"pr-review-engine"` | Matches `render.yaml`'s `name`; non-secret |

`RENDER_API_KEY` is **already** documented in `.env.example` as optional
operator tooling. Promoting it to a real `Settings` field makes one line there
stale — "Unknown vars are ignored by `app/config.py`" — which §10 fixes.

**Why this is a deliberate, bounded exception to "zero new secrets."** That
rule (hosting-migration design §6) governs *the deployed App's own runtime
secrets* — what must be present for the service to function. Both new keys are
the opposite: they live only on the operator's machine, are read only by a
local script, are never set on Render, and their absence degrades to `SKIPPED`
rather than breaking anything. The deployed service's secret surface is
unchanged.

## 6. The six checks

Run in this fixed order — cheapest and most foundational first, so a
misconfiguration is reported before the checks that would fail as a
consequence of it.

### 6.1 `check_config()` — required

Confirms every value the deployed service needs is resolvable locally:
`GITHUB_APP_ID` (non-zero), a private-key source (`GITHUB_APP_PRIVATE_KEY_B64`,
or `GITHUB_APP_PRIVATE_KEY_PATH` resolving to a file that exists),
`GITHUB_WEBHOOK_SECRET`, `GITHUB_TARGET_REPO`, a public base URL
(`PUBLIC_BASE_URL` or `RENDER_EXTERNAL_URL`), and the provider key implied by
`LLM_PROVIDER` (`GROQ_API_KEY` for `groq`, `GITHUB_MODELS_TOKEN` for
`github_models`, `GEMINI_API_KEY` for `gemini`).

`FAIL` names **every** missing key at once, never their values.

### 6.2 `check_installation_and_webhook(repo, base)` — required

Two facts in one line, because they share the App JWT.

1. `discover_installation_id(repo)` — existing function, unchanged. A `404`
   surfaces its existing actionable "not installed → install via the GitHub
   UI" `RuntimeError`; GitHub does not permit an App to install itself, so
   this fails fast rather than attempting a workaround.
2. **Read then conditionally write.** `GET /app/hook/config`, compare `url` to
   `{base}/webhook`:
   - match → `PASS`, detail `"installation=<id>, webhook already correct"`,
     **and no PATCH is issued**;
   - mismatch → `PATCH /app/hook/config`, then `PASS` with detail
     `"installation=<id>, webhook updated: <old> → <new>"`.
   - **response carries no `url`, or an empty one** (an App whose webhook was
     never configured) → treated as a mismatch: `PATCH`, then `PASS` with
     detail `"webhook set: <new>"`. This is the genuine first-deploy path and
     must not read as an error.
   - **the read itself errors** (non-404 GitHub failure) → `FAIL` naming the
     status, and **no `PATCH` is attempted** — writing blind after a failed
     read is how a good URL gets clobbered by a stale one.

This is the one place the default run may mutate remote state, and it is the
command's original purpose. Reading first is what makes a re-run honest: the
table distinguishes "was already right" from "I just fixed drift," which an
unconditional `PATCH` cannot.

### 6.3 `check_health_endpoint(base)` — required

`GET {base}/healthz` **and** `HEAD {base}/healthz`; both must return 200. A
`GET`-200/`HEAD`-405 split is a distinct `FAIL` that names `HEAD` explicitly
and points at the pinger consequence.

**Why both:** this is the run's 71-minute failure, encoded. `/healthz` now
carries both `@app.get` and `@app.head` (`ed4ec55`), and outside
`tests/test_skeleton.py` nothing else guards it. A refactor dropping the
`HEAD` decorator would break keep-warm silently, weeks later, in a way that
looks like a Render problem.

### 6.4 `check_database()` — optional

`SKIPPED` with a hint when `settings.database_url` is empty (the normal state
on a dev machine — it is a Render dashboard secret). The hint says to export
the Supabase Session-mode pooler URL temporarily to enable the check.

When set, a **raw `psycopg.connect(..., connect_timeout=10)`** — deliberately
not `store.init_pool()` (§7.5) — then:

| Observation | Result | Detail |
|---|---|---|
| Connection refused/timeout | `FAIL` | The driver's own error shape, scrubbed of the connection string |
| `SELECT 1` ok, `to_regclass('public.tickets')` is `NULL` | `FAIL` | "connected, but `tickets` is absent — the app has not completed a successful boot against this database" |
| `SELECT 1` ok, table present | `PASS` | "connected; `tickets` present" |

The middle row is the one worth having. It is exactly the state produced by a
`DATABASE_URL` pointing at the wrong (or a brand-new) Supabase project — a
setup mistake that a bare `SELECT 1` reports as success. The first hosted run
established this as a fast, read-only, meaningful signal: `to_regclass` was
`NULL` before the first deploy and returned the table immediately after.

**Not** verifying the full column set here: commit `d3d16b7` already pins
`_SCHEMA` against `Ticket.__dataclass_fields__` as a CI regression guard.
Re-checking it live would duplicate a stronger existing test.

### 6.5 `check_render_service()` — optional

`SKIPPED` with a hint when `settings.render_api_key` is empty. When set:
`GET /v1/services` → find `render_service_name` → `GET
/v1/services/{id}/deploys?limit=1` → `PASS` iff the latest deploy's status is
`live`; otherwise `FAIL` naming the actual status (`build_failed`,
`update_failed`, `deactivated`, `build_in_progress`, …).

**Its job is *why*, not *whether*.** §6.3 already answers whether the service
responds — from the outside, which is what actually matters to a user of the
bot. This check exists to turn a failing §6.3 from a symptom into a cause: a
suspended free instance, a crash-loop on a bad env var, and a deploy still
building are three very different problems that look identical over HTTP.

### 6.6 `check_uptime_pinger(base)` — optional

`SKIPPED` with a hint when `settings.uptimerobot_api_key` is empty. When set:
`POST https://api.uptimerobot.com/v2/getMonitors`, then locate a monitor whose
`url` **exactly equals** `{base}/healthz`.

- No exact match → `FAIL`, listing the monitored URLs found so a near-miss is
  obvious on sight. *The run's trailing-comma `/healthz,` must land here, not
  in `PASS`.*
- Found but paused (`status == 0`) → `FAIL`.
- Found, active, `interval > 600` → `FAIL` naming the interval. A monitor that
  exists but polls every 30 minutes lets Render's ~15-minute spin-down win —
  the failure mode a mere existence check would bless.
- Found, active, `interval <= 600` → `PASS`, reporting the interval and the
  monitor's current up/down status as information.

## 7. Execution model

### 7.1 Default mode

Resolve `repo` (`settings.github_target_repo`) and `base`
(`settings.public_base_url` or `RENDER_EXTERNAL_URL`) → run all six in order →
print an aligned three-column table → exit `0` if no `FAIL`, else `1`.

**`base` is normalized exactly once, at resolution, with `.rstrip("/")`** —
matching what `scripts/deploy.py` already does today. Every `{base}/…` in §6
is built from that normalized value. This is not cosmetic: §6.6 compares the
monitor's URL by exact equality, so a trailing slash in `PUBLIC_BASE_URL`
would otherwise produce `https://host//healthz` and fail a correctly
configured pinger.

`SKIPPED` never fails the run; an *attempted* optional check that genuinely
fails does. That asymmetry is the point: not configuring the pinger key is a
choice, but a configured pinger that cannot keep the service warm is a defect.

### 7.2 Exit codes

| Code | Meaning |
|---|---|
| `0` | Every check `PASS` or `SKIPPED` |
| `1` | At least one `FAIL` |
| `2` | Cannot even run: no `GITHUB_TARGET_REPO`/base URL; `--sync-env` without `RENDER_API_KEY`; or `--sync-env` aborted by the clobber guard (§8, step 2) |

`2` preserves the existing script's current contract for unusable input.

### 7.3 No check may abort the run

Each check function catches its own exceptions and converts them to `FAIL`.
A complete table is the deliverable; one exploding check must not deprive the
operator of the other five diagnoses.

### 7.4 Output contract

The report is read in a terminal by someone with no assistant to interpret it,
and `README.md` carries the explanatory depth. So the output is **terse by
contract**, not by accident:

```
config            PASS
github-app        PASS     installation=12345678; webhook already correct
health            FAIL     HEAD /healthz -> 405 (GET ok); pinger sends HEAD
database          PASS     connected; tickets present
render-service    SKIPPED  set RENDER_API_KEY to check deploy status
uptime-pinger     FAIL     no monitor matches .../healthz
                           found: .../healthz,

2 failed, 1 skipped -- see README.md#deploying-to-production
```

Rules the implementation must hold to:

- **One line per check**, three aligned columns; a `detail` may wrap to a
  second, indented continuation line only when it must enumerate observed
  values (as the pinger's `found:` line does).
- **`detail` is a fragment, not a sentence** — observed fact, then the single
  next action. No trailing periods, no prose, no emoji, no ANSI colour.
- **Never explain *why*.** "pinger sends HEAD" is the entire justification a
  line gets; the reasoning lives in `README.md`. A `detail` that runs past
  roughly 70 characters is a signal the explanation belongs in the docs.
- **`SKIPPED` names the env var and what it buys**, in that order
  ("set `RENDER_API_KEY` to check deploy status") — enough to decide whether
  to bother, without a doc lookup.
- **One trailing summary line**: counts plus a section-anchored `README.md`
  pointer. It is the only place the output refers the reader elsewhere.

### 7.5 Why `check_database` bypasses the app's pool

`store.init_pool()` opens a `ConnectionPool` whose first `connection()` blocks
for `_POOL_TIMEOUT_SECONDS` (30) before raising, and its `RuntimeError`
message is deliberately written for *startup* — three first-deploy causes,
aimed at a Render log reader. In a checklist that is the wrong shape twice
over: a 30-second stall per run, and a message that is not the driver's actual
error. A raw connect with a short timeout reports the real cause in about a
second. This is the findings doc's explicit recommendation.

## 8. `--sync-env` mode (opt-in)

Requires `RENDER_API_KEY`; exits `2` without it. The capability the hosted run
demonstrated — pushing `.env` values to the service — kept behind a flag so
the default `/deploy` stays a safe verification.

1. Build the wanted map from settings: `DATABASE_URL`, `GITHUB_APP_ID`,
   `GITHUB_APP_PRIVATE_KEY_B64` (derived from the PEM file when only the path
   is configured locally), `GITHUB_TARGET_REPO`, `GITHUB_WEBHOOK_SECRET`,
   `LLM_PROVIDER`, `GROQ_API_KEY`, `GITHUB_MODELS_TOKEN`.
2. **Clobber guard, before any request:** abort (exit `2`) naming any key whose
   *local* value is empty. A blank `.env` entry must never overwrite a working
   remote secret. Checked up front so a partial push cannot happen.
3. Read the service's current env vars and diff **in memory**, reporting only
   key names and value lengths — never values, on either side.
4. Push only differing/missing keys, one request each, to
   `PUT /v1/services/{id}/env-vars/{KEY}`.
   > **Never** `PUT /v1/services/{id}/env-vars` (no key). That endpoint
   > replaces the entire list and would silently delete every variable not in
   > the payload, `DATABASE_URL` included. This is a hard constraint carried
   > forward from the hosted-run plan.
5. If nothing changed, say so and skip step 6 — no pointless redeploy.
6. If anything changed: `POST /v1/services/{id}/deploys`, then poll until
   status is `live`, bounded at ~5 minutes (the run measured 55–65s). Env-var
   changes **do not auto-deploy**; without this the command would report
   success while the service keeps serving the old values.
7. Fall through to the §7.1 checklist, so the printed table describes the
   freshly deployed service rather than the pre-sync one.

## 9. Secret handling

Per-check, concretely:

- `check_config` reports missing key **names**; never a value, never a length.
- `check_database` builds its `FAIL` detail from the exception's type and a
  fixed message rather than interpolating `settings.database_url`, which
  carries the password. §11 pins this with a sentinel test.
- `--sync-env` prints `KEY (len 1704)` style lines only, for both the local
  and remote sides of the diff.
- `RENDER_API_KEY` and `UPTIMEROBOT_API_KEY` travel in headers/bodies and are
  never echoed, including in error paths.

## 10. Docs updates

Since the CLI is the product (§4) and its output deliberately explains nothing
(§7.4), the docs carry the entire explanatory burden. `README.md` is where
people actually look, so it gets **full parity with `SETUP.md` §3** rather than
a pointer.

- **`README.md` → "Deploying to production (Render + Supabase)"** — expanded
  from today's three-step summary into the complete deployment story at the
  same depth as `SETUP.md` §3: the one-time prerequisites (§4.2's second
  table), the repeatable `--sync-env` deploy, what each of the six checklist
  lines means, the three exit codes, and the two optional keys with what each
  unlocks. A reader who never opens `SETUP.md` must still be able to deploy.
- **`SETUP.md` §3.4** — replace the bare `python -m scripts.deploy` step with
  the checklist, documenting what each line means and the two optional keys.
  Keep the existing runs-locally / `PUBLIC_BASE_URL` guidance, which is still
  correct.
- **`SETUP.md` §3.5** — note that the UptimeRobot monitor must target
  `/healthz` exactly (no trailing characters) and use an interval ≤ 10
  minutes, and that the free tier issues `HEAD`.
- **`.env.example`** — add `UPTIMEROBOT_API_KEY=` and `RENDER_SERVICE_NAME=`
  to the existing optional-operator-tooling block; fix the now-stale "Unknown
  vars are ignored by `app/config.py`" line, since `RENDER_API_KEY` becomes a
  real field (§5).

### 10.1 Keeping the two documents in sync

Full parity means duplicated prose, and duplicated prose drifts. "Update both"
is a convention, not a mechanism — so the one kind of drift that actually
breaks a deployment gets a real guard instead.

The dangerous drift is the **env-var name list**: a variable documented in one
place but missing from `--sync-env`'s push list (silently never deployed), or
pushed but undocumented (nobody knows to set it). `scripts/deploy.py` holds the
authoritative tuple; §11's `test_env_var_names_match_the_docs` asserts that
`README.md` and `SETUP.md` each mention every name in it. Prose wording may
diverge freely; the contract may not.

Both documents are updated in the same commit as any change to that tuple.

## 11. Testing

New `tests/test_deploy_script.py`. Every test offline: `respx` for HTTP
(existing dev dependency), the existing Postgres harness or a dead port for
the DB, `monkeypatch` for settings. No live calls — the CI contract holds.

Per check:

- **`check_config`** — each required key missing in turn → `FAIL` naming it;
  all present → `PASS`; the PEM *path* variant with a nonexistent file → `FAIL`.
- **`check_installation_and_webhook`** — already-correct URL → `PASS` **and
  assert the PATCH route was never called**; mismatched URL → PATCH issued,
  detail shows old → new; absent/empty `url` in the read → PATCH issued
  (first-deploy path, still `PASS`); read fails with `500` → `FAIL` **and
  assert PATCH was never called**; `404` on installation → `FAIL` matching
  `"not installed"`.
- **`check_health_endpoint`** — both 200 → `PASS`; **`GET` 200 + `HEAD` 405 →
  `FAIL` naming `HEAD`** (the run's regression, as a test); connection error →
  `FAIL`.
- **`check_database`** — unset → `SKIPPED` with hint; `127.0.0.1:1` with
  `connect_timeout=1` → `FAIL` in about a second (the trick
  `tests/test_store_init.py` already uses); via the `db` fixture with
  `tickets` dropped → the distinct "has not completed a successful boot"
  `FAIL`; intact → `PASS`.
- **`check_render_service`** — unset → `SKIPPED`; `live` → `PASS`;
  `build_failed` → `FAIL` naming it; service name absent from the list →
  `FAIL` naming the configured name.
- **`check_uptime_pinger`** — unset → `SKIPPED`; exact match + active +
  `interval=300` → `PASS`; **trailing-comma URL → `FAIL`**; paused → `FAIL`;
  `interval=1800` → `FAIL` naming the interval.

Cross-cutting:

- **Secret-leak test** — a sentinel password inside `DATABASE_URL` must not
  appear in any `CheckResult`'s `detail` or `repr`, mirroring the existing
  `test_init_pool_error_never_leaks_the_connection_string`.
- **`test_env_var_names_match_the_docs`** — reads `README.md` and `SETUP.md`
  and asserts each mentions every name in `scripts/deploy.py`'s authoritative
  env-var tuple (§10.1). Checks *names only*, never wording, so the docs stay
  free to explain differently while the contract cannot silently diverge. This
  is the mechanism behind "keep both synced."
- **Output-contract test** — renders a fixed set of `CheckResult`s and asserts
  the table's shape: aligned columns, no `detail` exceeding the §7.4 length
  budget, and a trailing summary line carrying the counts and the `README.md`
  anchor.
- **`main()` exit codes** — `0` when all `PASS`/`SKIPPED`, `1` on any `FAIL`,
  `2` on unusable input.
- **`--sync-env`** — asserts the single-key endpoint is used and the bulk
  `PUT` route is **never** called; an empty local value aborts before any
  request is issued; unchanged values are not pushed; a deploy is triggered
  only when something actually changed; the poll stops at `live`.

## 12. Out of scope / non-goals

- **Provisioning anything.** No creating Supabase projects, Render services,
  GitHub Apps, or UptimeRobot monitors — each is a documented one-time manual
  step, and automating them would need far broader credentials than any check
  here holds.
- **Rotating or generating secrets.** `--sync-env` copies existing local
  values; it never mints them.
- **Verifying the LLM provider with a live call.** `CLAUDE.md`'s API-hygiene
  rules forbid casual live provider calls, and a checklist that burns quota on
  every run is exactly the pattern that got a provider account flagged.
  `check_config` verifies the key is *present*; `/healthz` and a real PR
  verify it *works*.
- **cron-job.org support.** One pinger integration, chosen deliberately;
  `SETUP.md` may still mention cron-job.org as an unautomated alternative.
- **Multi-service / multi-environment deploys.** One service, named by
  `render_service_name`.

## 13. Provenance

Every non-obvious decision here traces to
`docs/2026-08-05-first-hosted-run-findings.md`: the `HEAD` check (§6.3) to the
`405` that broke keep-warm; the exact-URL pinger match (§6.6) to the
trailing-comma monitor; `to_regclass` (§6.4) to the `NULL`-then-table
observation that resolved the provisioning handoff; the raw-`psycopg` choice
(§7.5) to that doc's explicit `check_database` recommendation; and the Render
API surface (§6.5, §8) to its follow-up #3, including the single-key-endpoint
constraint that protects `DATABASE_URL`.
