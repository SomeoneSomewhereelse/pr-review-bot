# Design — `/deploy` slash command

**Date:** 2026-08-05
**Status:** Ready to resume (unblocked 2026-08-07) — the provisioning handoff
this design was paused on is resolved; see
`docs/2026-08-05-supabase-first-deploy-provisioning-handoff.md` and
`docs/2026-08-05-first-hosted-run-findings.md`. Resume brainstorming from
"Design, part 2: data flow, error handling, testing" per the "Resuming this
design" section at the bottom of this document. That section's open question
on `check_database`'s exact behavior is answered by the findings doc: use a
short-timeout `psycopg.connect` + `SELECT 1` directly, not the app's
`ConnectionPool`, so a real connection failure reports immediately rather
than waiting out `init_pool()`'s 30-second pool timeout.
**Relates to:** `scripts/deploy.py` (existing registration script this
command wraps), `app/github_app.py` (`discover_installation_id`,
`set_webhook_url`), `app/queue/store.py` (`init_pool`/`_SCHEMA` — the piece
under investigation), `docs/superpowers/specs/2026-08-03-supabase-hosting-migration-design.md`
§9 ("Deploy-time registration — hint only... to become a `/deploy` slash
command"), `docs/superpowers/plans/2026-08-03-supabase-hosting-migration.md`
Task 6 (built `scripts/deploy.py` itself; this design extends it).

## What's agreed so far (from brainstorming, before the pause)

**Scope:** `/deploy` becomes a comprehensive deploy-verification checklist —
not just a thin wrapper around the existing registration script. The
Q&A that shaped this:

- The command should do **pre-flight checks covering as much as possible**
  to guarantee a successful, working deployed app — not just run
  `scripts/deploy.py` and report its exit code.
- **Pinger verification is real, not a checklist reminder.** UptimeRobot was
  chosen (free tier: 5-min checks, well under Render's ~15-min spin-down;
  simple REST API) over cron-job.org, via a new **optional** env var
  (`UPTIMEROBOT_API_KEY`) — a deliberate, opt-in exception to the "zero new
  secrets" rule that governs the *App's own* runtime secrets (this key is
  used only by the operator's local machine running the script, never by
  the deployed service, never added to `render.yaml`).
- The pinger check verifies both **existence/active status AND check
  interval** (flag a monitor that exists but polls too infrequently to
  prevent spin-down) — not just "a monitor exists."
- **Full checklist, not fail-fast.** Every check runs regardless of earlier
  failures; the command prints a complete pass/fail table at the end, so
  one run surfaces every problem instead of just the first.
- **Architecture: tested Python, not markdown logic.** The checks live as
  functions in `scripts/deploy.py` (httpx is already a project dependency —
  no new deps needed either way), each independently pytest-covered with
  mocked HTTP (`respx`, already a dev dependency). The slash command file
  (`.claude/commands/deploy.md`) is a thin wrapper that runs
  `uv run python -m scripts.deploy` and surfaces the output — matching this
  project's existing TDD-everything convention, and explicitly rejected the
  alternative of embedding curl/bash logic directly in the command's
  markdown (untestable, inconsistent with project conventions).
- **Optional checks skip gracefully with a hint, they don't fail the run —
  but if attempted, a real failure does fail the run.** Applies to both the
  DB check (`DATABASE_URL` not normally present on a dev machine — it's a
  Render dashboard secret) and the pinger check (`UPTIMEROBOT_API_KEY`
  optional). The user was explicit that skipped checks must **hint at
  proper configuration** rather than leave the operator hunting for why
  something isn't running — every `SKIPPED` result needs an actionable
  one-liner (e.g. "export the Supabase Session-mode pooler URL to enable
  this check").
- **Code structure: composable check functions (chosen over two
  alternatives).** Each check is its own small function returning a
  `CheckResult(name, status, detail)` dataclass; `main()` calls a fixed
  sequence of five, builds the table, computes the exit code. Rejected: (a)
  one big inline `main()` — simpler but untestable per-check and prone to
  growing unbounded; (b) a declarative check-registry — more extensible for
  hypothetical future checks, but overengineered for a fixed list of five
  (YAGNI).

## Architecture drafted so far (Design, part 1 — presented and approved before the pause)

**`.claude/commands/deploy.md`** (new) — thin slash command. Frontmatter
with a one-line description; body tells Claude to run
`uv run python -m scripts.deploy`, show the printed checklist verbatim, and
on a non-zero exit code, help the user act on whichever lines say `FAIL`
using that line's printed hint. No bash/curl logic embedded in the markdown
itself.

**`scripts/deploy.py`** (rewritten, same entry point) — gains:

- `CheckResult` — `name: str`, `status: Literal["PASS","FAIL","SKIPPED"]`,
  `detail: str`.
- Five check functions, each returning one `CheckResult`, run in this
  order regardless of earlier failures:
  1. `check_config() -> CheckResult` — **required.** Confirms
     `GITHUB_APP_ID`, a private-key source (`GITHUB_APP_PRIVATE_KEY_B64` or
     `GITHUB_APP_PRIVATE_KEY_PATH` resolving to a real file),
     `GITHUB_WEBHOOK_SECRET`, `GITHUB_TARGET_REPO`, a public base URL
     (`PUBLIC_BASE_URL` or `RENDER_EXTERNAL_URL`), and the LLM key implied
     by `LLM_PROVIDER` (`GROQ_API_KEY` for groq, `GITHUB_MODELS_TOKEN` for
     github_models) are all present.
  2. `check_installation_and_webhook(repo, base) -> CheckResult` —
     **required.** Wraps the existing `discover_installation_id` +
     `set_webhook_url` calls (unchanged logic), reported as one checklist
     line instead of bare prints.
  3. `check_health_endpoint(base) -> CheckResult` — **required.**
     `httpx.get(f"{base}/healthz")`, PASS on 200 within a short timeout.
  4. `check_database() -> CheckResult` — **optional.** `SKIPPED` with a
     hint if `settings.database_url` is unset; otherwise a lightweight
     `psycopg.connect(...)` + `SELECT 1` with a short timeout. **This is
     the check blocked on the parked investigation** — it currently
     assumes "can I open a connection and run a trivial query" is a
     sufficient proxy for "is this database correctly provisioned for the
     app," which may not hold on a brand-new Supabase project (see the
     handoff doc).
  5. `check_uptime_pinger(base) -> CheckResult` — **optional.** `SKIPPED`
     with a hint if `settings.uptimerobot_api_key` is unset; otherwise
     POSTs to UptimeRobot's `getMonitors` endpoint, finds a monitor
     targeting `{base}/healthz`, PASSes only if active AND interval ≤ 600s
     (10 min).
- `main()` — calls all five in order, prints an aligned checklist table,
  returns 0 only if every result is `PASS`/`SKIPPED`; any `FAIL` (required
  or attempted-optional) returns 1.

**`app/config.py`** — adds `uptimerobot_api_key: str = ""` (optional,
local-tool-only — never added to `render.yaml`, since it's used by the
operator's machine running `scripts/deploy.py`, not by the deployed
service).

## What's NOT yet decided (design was mid-flow when paused)

- Error handling section (per-check exception handling, timeouts) —
  not yet presented.
- Testing section (exact pytest/respx coverage plan per check) — not yet
  presented.
- Whether `check_database`'s "lightweight `SELECT 1`" is even the right
  check, or whether it should instead verify the `tickets` table/schema
  exists — **directly depends on the parked investigation's answer.**
- Final spec write-up, self-review, and user sign-off on the full design —
  none of this has happened yet. This document is a paused snapshot, not an
  approved spec.

## Resuming this design

Once `docs/2026-08-05-supabase-first-deploy-provisioning-handoff.md` is
resolved (a real gap found + fixed, or confirmed there's no gap), resume
brainstorming from "Design, part 2: data flow, error handling, testing" —
the architecture in part 1 above was already presented and did not need
revision on its own terms, but `check_database`'s exact behavior may need
to change based on what the investigation finds.
