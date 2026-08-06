# Demo plan — Zoom screen-share, course grading presentation

> **Status note:** the per-segment narrative below still assumes the old
> local-plus-tunnel flow and its two-second `uvicorn` restarts. Its environment
> facts and checklist have been updated to the hosted stack, but the choreography
> and timing await the §13 re-validation — see
> `docs/2026-08-05-first-hosted-run-findings.md` for the measured Render restart
> durations that re-validation needs.

Date: 2026-08-03
Status: **Paused** — a new requirement (persistent internet hosting, so the
instructor can test the bot independently as a collaborator on the testbed
repo, not just during the scheduled Zoom call) surfaced mid-planning and
needs its own brainstorm + plan first. See
`docs/2026-08-03-supabase-hosting-migration-handoff.md`. This document's
segment content (happy path, GitHub Models dead-vendor swap, Groq quota
exhaustion + cooldown) is still the intended demo material — resume this
plan once the hosting migration lands, re-validating any step that assumed
your own terminal (e.g. restarting `uvicorn` for a provider swap may work
differently on a managed host).

## Purpose

A live, screen-shared walkthrough of the Autonomous Code Review Engine for a
course grading presentation. Budget: 10-15 minutes of demo time. All core
moments (the bot actually reviewing PRs) are **live against the real GitHub
webhook path, no recorded fallback** — confirmed acceptable risk given the
project's prior rehearsal history (PR #2, #3) and the fact that the two
"failure" beats below are now *engineered into the plan on purpose*, not
things we're hoping don't happen.

## Environment facts established during planning (2026-08-03)

- `gh` and `uv` both resolve and work from this session (Windows binaries via
  `/mnt/c/Program Files/...`, `/mnt/c/Users/Home/.local/bin/uv.exe`).
  `gh auth status` shows `✓ Logged in ... (keyring)` with `repo` scope.
- The bot runs as a deployed Render service, kept warm by the free pinger, so
  there is no local process to start. The runbook's first step checks
  `<render-url>/healthz` and only redeploys if it does not respond.
- **GitHub Models is fully retired** as of 2026-07-30 (confirmed via GitHub's
  own changelog — playground, catalog, inference API, and BYOK gone for all
  customers, including existing ones with active usage; no drop-in official
  replacement). This is not a rate-limit or misconfiguration — the endpoint
  is genuinely gone. `.env`'s `LLM_PROVIDER` currently reads `groq`.
- Test repo: `<your-user>/pr-review-bot-testbed`. GitHub App:
  `<your-app-slug>`
  (`https://github.com/settings/apps/<your-app-slug>`).
- Measured live (one deliberate call, per `CLAUDE.md`'s LLM-testing-hygiene
  rule): a single specialist call against the real `fixtures/bad_code`
  diff via Groq costs **1,673 tokens in / 77 tokens out** (~1,750 total).
  Three specialists/review ⇒ **~5,250 tokens per full review**. Against
  Groq's free-tier **12K TPM** cap (the binding constraint — 30 RPM gives
  10 reviews/min of headroom, far looser than TPM's ~2.3 reviews/min), two
  reviews land safely (~10.5K), a third has only ~1.5K headroom against a
  ~5.25K need — a real 429 is very likely by the 3rd review in the same
  60s window, guaranteed by any review after that.
- The queue lives in Supabase Postgres, not a local `queue.db`.
  `store.init_pool()` runs `CREATE TABLE IF NOT EXISTS` on every boot, so no
  setup is needed — but tickets now **survive** restarts, so any leftover
  `deferred`/`retrying` rows from a rehearsal must be checked (and allowed to
  drain) before the real call, or they will muddy the burst timing.
- Changing `LLM_PROVIDER` means a Render redeploy rather than a local `uvicorn`
  restart, since `Settings()` is built once at import. The webhook URL is stable
  and never needs re-editing.
- `attempt_review`'s "atomic" behavior only applies when a specialist call
  raises `RateLimited` (429) — that defers the *whole* ticket with no
  comment posted. A specialist that fails for any other reason (dead
  endpoint, network error) is caught inside `run_specialist` and turned
  into a `status="failed"` result; `attempt_review` still completes
  normally and **posts a real comment with failed rows** — this is not
  gated by the "preserve a good review" guard (that guard only fires on
  the dispatcher's rate-limit pre-flight gate and the terminal hard-stop
  retry path). This means reviewing an *already-reviewed* PR with a fully
  dead provider would overwrite its good comment with an all-failed one —
  which is why Segment B below uses a **fresh PR**, not the happy-path PR.

## Segment plan (~14 min total)

### 1. Architecture overview (~2 min)

Narrate the flow from `README.md`/`SPEC.md`'s diagram: webhook → HMAC verify
→ dedup → durable SQLite ticket → single serial dispatcher → diff fetch +
annotate → 3 concurrent specialists → merge (atomic on rate-limit, never
partial otherwise) → upsert PR comment. No live action needed yet.

### 2. Happy path (~3 min) — establishes the baseline

- Provider: `groq` (already the default in `.env`).
- `uv run python scripts/seed_demo_pr.py` → opens **PR-1** with the planted
  `fixtures/bad_code/billing_report.py` issues (hardcoded credential, N+1
  query, magic number).
- Narrate while waiting (~8s historically): webhook → 202 → dispatcher
  claims the ticket → 3 specialists run concurrently → comment posts.
- Show the resulting comment: real findings across Security/Performance/
  Code Quality, footer with runtime/tokens/cost.

### 3. Segment B — a real vendor died overnight (~4 min)

- Switch `.env`: `LLM_PROVIDER=github_models`. Restart `uvicorn` only (tunnel
  stays up, no webhook URL change needed).
- `uv run python scripts/seed_demo_pr.py` → opens **PR-2** (fresh PR, not
  PR-1 — see the "atomic" note above for why).
- Live result: all 3 specialists fail (dead endpoint) → comment posts
  immediately (no dispatcher-level retry needed — this is a same-attempt
  completed review with 3 failed rows, not a deferred one) → narrate: this
  is GitHub's actual July 30, 2026 retirement, not a staged failure — link
  to `github.blog/changelog/2026-07-30-github-models-is-now-retired`.
- Switch `.env` back: `LLM_PROVIDER=groq`. Restart `uvicorn` again.
- Trigger a re-review of PR-2: push a trivial follow-up commit to its
  branch (`synchronize` event) — need to keep the branch checked out
  (`gh pr checkout <PR-2 number>` in a scratch dir) since
  `seed_demo_pr.py`'s clone is discarded after each run.
- Live result: same comment (same marker) edits in place, now showing real
  findings from `groq`. This is the "provider seam survives a full vendor
  outage" proof.

### 4. Segment C — quota exhaustion + auto-recovery (~4-5 min)

Still on `groq`. Fire, in quick succession:

1. `seed_demo_pr.py` → **PR-3** (new)
2. `seed_demo_pr.py` → **PR-4** (new)
3. `seed_demo_pr.py` → **PR-5** (new)
4. `seed_demo_pr.py` → **PR-6** (new)
5. **Last**, a follow-up commit on **PR-1** (the happy-path PR, already has
   a good review) — fire this one last so quota is already exhausted by
   the time its ticket is claimed, maximizing the odds it's the one that
   demonstrates the footnote path rather than succeeding outright.

Expected (per the token math above, some timing variance possible — narrate
this as "roughly" rather than promising an exact count live):
- The first ~2 tickets claimed likely **succeed** normally (~10.5K
  cumulative tokens, under the 12K TPM cap).
- The remaining new-PR tickets (no prior review) hit a real 429 → deferred
  → **plain placeholder comment** posted (`format_placeholder`).
- PR-1's re-review ticket (has `last_reviewed_at` set → a good review is
  already visible) hits the same 429 → deferred → **self-cleaning schedule
  footnote** appended instead of overwriting the good comment
  (`format_schedule_notice` / `append_schedule_notice`), per the
  "never overwrite a good visible review" guarantee.
- Narrate while waiting for the automatic retry (dispatcher polls every
  `dispatcher_idle_sleep_seconds` = 1s; the deferred tickets' `not_before`
  is set from Groq's real `Retry-After`) — no manual intervention. Once due,
  each deferred ticket re-attempts and, once the per-minute TPM window has
  rolled forward, succeeds: placeholders get replaced with real reviews,
  and PR-1's footnote disappears, leaving the original comment intact.

### 5. Wrap-up (~1 min)

- Cost model: `cost.md`'s ~$8-10/mo at brief scale, demo ran at $0
  (Groq + Render + Supabase free tiers).
- What's not live: Vertex (mocked-only, needs GCP billing).
- One-line callout: GitHub Models' retirement happened *during this
  project's life* and the architecture absorbed it without a code change —
  only an env var + restart.

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

## Open items / risks accepted

- Segment C's exact split between "succeeds" vs. "defers" depends on real
  Groq timing and is not deterministic — the 5-item burst (4 new + 1
  resync, resync fired last) was sized with margin from a real measurement,
  not guessed, but a live rehearsal (next step) is what actually confirms
  it, not this document.
- No recorded fallback for any segment, by explicit choice — a live failure
  during the actual call has no on-the-spot recovery.
- The GitHub Models failure mode (Segment B) depends on it staying dead
  between now and the call date — extremely likely given it's a confirmed
  permanent retirement, not a transient outage.
