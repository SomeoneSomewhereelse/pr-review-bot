# Demo plan — Zoom screen-share, course grading presentation

Date: 2026-08-03 (rewritten 2026-08-10)
Status: Draft, re-validated against the now-live Render + Supabase deployment
and the DB-backed provider override. **Not yet rehearsed against the real
hosted service** — Segment C's sizing in particular is a re-derived estimate,
not a measurement (see "Open items / risks" and the new-fixture prerequisite
below).

**Supersedes:** the original 2026-08-03 version in full. That version assumed
a local `uvicorn` + Cloudflare quick tunnel that no longer exists, and a
5-PR-burst design for Segment C that was tried for real (per
`docs/2026-08-05-first-hosted-run-findings.md`) and did not trip a 429.

## Purpose

A live, screen-shared walkthrough of the Autonomous Code Review Engine for a
course grading presentation. Budget: 10-15 minutes of demo time. All core
moments (the bot actually reviewing PRs) are **live against the real GitHub
webhook path, no recorded fallback** — the "failure" beats below are
*engineered into the plan on purpose*, not things we're hoping don't happen.

## Environment facts established during this rewrite (2026-08-10)

- **Hosting is Render + Supabase, live right now.** Confirmed via a plain
  `GET https://pr-review-engine.onrender.com/healthz` → `200 {"status":"ok"}`
  (no auth, no write). Local `uvicorn` + Cloudflare quick tunnel is retired
  entirely — there is no local process to start and no per-session webhook
  URL to re-paste.
- **`gh` and `uv` are native Linux binaries** in this environment now (not
  Windows paths through WSL) — a side effect of the hosting-migration work,
  not something this plan needs to manage.
- **Provider switching is a DB write, not a redeploy.** `scripts/set_provider.py`
  writes to a `runtime_config` table in the same Supabase Postgres the
  dispatcher reads; the override takes effect on the **next ticket claimed**
  — no restart, no redeploy, no ~60-90s wait. This replaces both the original
  plan's "restart `uvicorn`" step and the interim "Render redeploy" timing
  recorded in the first hosted run — that measurement (`groq → github_models`:
  65.5s) predates this feature and no longer applies.
- **A `/deploy` skill and `scripts/deploy.py` CLI now exist** for verification
  (`config`, `github-app`, `health`, `database`, `provider`, `render-service`,
  `uptime-pinger` checks) and for pushing config (`--sync-env`). Running it
  can **write** the GitHub App's webhook URL if it's found to be wrong — worth
  knowing before running it casually; it is not needed for this plan's
  choreography, only for pre-call verification (see the checklist).
- **Vertex is deleted from code**, not just undemonstrated. Irrelevant to this
  plan (we never used it), noted only so nobody goes looking for it.
- **GitHub Models is still a valid provider value in code** — the
  `_PROVIDERS` table (`scripts/deploy.py`) still lists it. The real external
  API's July 30, 2026 retirement is untouched by any of this project's
  changes, so Segment B's premise still holds.
- **Corrected Groq ground truth (one live header probe, this session):**

  ```
  x-ratelimit-limit-tokens:     12000   (per-minute, continuously refilling)
  x-ratelimit-remaining-tokens: 11958   (after one trivial call)
  x-ratelimit-reset-tokens:     210ms   (refill is smooth, not a fixed-block reset)
  x-ratelimit-limit-requests:   1000    (this is a per-DAY bucket, confirmed by
                                          its own reset-time math — not a second
                                          per-minute constraint to worry about)
  ```

  The bucket refills continuously at **~200 tokens/sec**. This is why the
  original plan's design (a time-spaced burst of small reviews) failed for
  real: **`docs/2026-08-05-first-hosted-run-findings.md` fired 4 new PRs plus
  a resync exactly once and measured ~26.5K tokens over ~90s with zero
  `429`s** — with ~9s between serially-dispatched reviews, ~1,800 tokens
  refill between each one, comfortably outrunning ~5,250-token reviews
  spaced that far apart. **Do not repeat that design.**
- **Gemini is the wrong tool for the quota-exhaustion beat.** Its documented
  failure mode is a **403 `PERMISSION_DENIED`** (an account-level Trust &
  Safety flag), not a 429. `app/providers/google_genai.py` only converts an
  exception to `RateLimited` when `rate_limited_or_none()` recognizes a real
  429 — a 403 falls through to the generic exception path, which
  `run_specialist` turns into `status="failed"`. Using Gemini here would
  reproduce **Segment B's** failure shape (3 failed rows), not this
  segment's (deferred → placeholder → auto-heal).
- **The fix: concurrency, not elapsed time.** All 3 specialists in one review
  fire together via `asyncio.gather` — faster than any refill can matter.
  Sizing one review's *total* demand near, but under, the 12,000-token
  absolute cap makes a single review's own burst do the work that a
  time-spaced burst of small reviews cannot. See Segment C below.
- `attempt_review`'s atomic rate-limit behavior is unchanged: a `RateLimited`
  specialist defers the *whole* ticket with no comment posted; any other
  failure (dead endpoint, network error) is caught inside `run_specialist`
  and still produces a **completed** review with failed rows. This is why
  Segment B uses a fresh PR, not the happy-path one — reviewing an
  already-good PR with a fully dead provider would overwrite its good
  comment with an all-failed one (not gated by the "preserve a good review"
  guard, which only fires on the rate-limit and terminal-hard-stop paths).
- The escalating re-review cooldown (`dispatcher_rereview_cooldown_seconds`,
  default 300s) is **not** part of `render.yaml`'s env var list or
  `--sync-env`'s push set — it's not on the deploy CLI's config surface at
  all. Lowering it for the demo (so the cooldown-footnote beat visibly
  resolves within the segment, not just appears) would need a manual edit in
  Render's dashboard, outside any of this project's tooling.
- **This is now a real, shared, already-used service.** The testbed repo may
  already carry PRs/comments from the hosting-migration work's own
  rehearsals (Aug 5-8). Check for leftover open PRs before choosing a
  rehearsal time, so the live demo isn't confused by stale state, and be
  aware today's Groq daily quota (1,000 RPD) may already be partially spent
  by any prior testing.

## Prerequisite: a new padded fixture (not yet built)

Segment C (below) needs a diff sized to load **~3,000 tokens of content per
specialist call** (~12,000 characters — comfortably under
`diff_utils.annotate_and_cap`'s 24,000-char/6,000-token truncation ceiling,
so no truncation-marker side effects). The current
`fixtures/bad_code/billing_report.py` is ~1,800 characters — far too small.

**This is real code work, not a config change**, and is not done yet:

- A new fixture (e.g. `fixtures/bad_code_bulk/billing_report_bulk.py`),
  separate from the happy-path fixture so Segments 2 and 3 stay unaffected —
  padded toward ~12,000 characters with plausible (if repetitive) billing/
  reporting code, not obvious filler, since it may be shown on screen.
- A seed-script variant (a flag on `scripts/seed_demo_pr.py`, or a small
  sibling script) that copies this fixture instead of the default one.
- One live rehearsal to confirm the actual per-call token cost this diff
  produces — the ~3,000-token target is derived from `diff_utils`' rough
  4-chars/token heuristic plus a measured system-prompt overhead, **not**
  measured directly against this specific padded content. Given this
  project's history of estimates needing correction against real headers and
  real bursts, treat the character count as a starting point to adjust after
  one real run, not a guarantee.

## Segment plan (~14 min total)

### 1. Architecture overview (~2 min)

Narrate the flow from `README.md`/`SPEC.md`'s diagram: webhook → HMAC verify
→ dedup → durable Postgres ticket → single serial dispatcher → diff fetch +
annotate → 3 concurrent specialists → merge (atomic on rate-limit, never
partial otherwise) → upsert PR comment. Mention Render + Supabase as the
production home. No live action needed yet.

### 2. Happy path (~3 min) — establishes the baseline

- Provider: `groq` (confirm via `uv run python -m scripts.set_provider --clear`
  beforehand, so there's no leftover override from earlier testing).
- `uv run python -m scripts.seed_demo_pr` → opens **PR-1** with the planted
  `fixtures/bad_code/billing_report.py` issues (hardcoded credential, N+1
  query, magic number).
- Narrate while waiting (~9.2s measured against the real hosted service, per
  `docs/2026-08-05-first-hosted-run-findings.md`): webhook → 202 → dispatcher
  claims the ticket → 3 specialists run concurrently → comment posts.
- Show the resulting comment: real findings across Security/Performance/
  Code Quality, footer with runtime/tokens/cost.

### 3. Segment B — a real vendor died overnight, plus a bonus cooldown reveal (~5 min)

- **Set the DB override:** `uv run python -m scripts.set_provider github_models`
  — takes effect on the next claimed ticket, no restart, no redeploy.
- `uv run python -m scripts.seed_demo_pr` → opens **PR-2** (a fresh PR, not
  PR-1 — see the atomic-behavior note above for why).
- Live result: all 3 specialists fail against the dead endpoint → comment
  posts immediately with 3 failed rows (a *completed* review, not a
  deferred one — no dispatcher-level retry involved) → narrate: this is
  GitHub's actual July 30, 2026 retirement, not a staged failure — link to
  `github.blog/changelog/2026-07-30-github-models-is-now-retired`.
- **Clear the override:** `uv run python -m scripts.set_provider --clear`
  (falls back to `.env`'s `LLM_PROVIDER=groq`) — again, instant.
- Trigger a re-review of PR-2: push a trivial follow-up commit to its branch
  (`synchronize` event) — keep the branch checked out
  (`gh pr checkout <PR-2 number>` in a scratch dir), since
  `scripts.seed_demo_pr`'s clone is discarded after each run.
- Live result: the same comment (same marker) edits in place with real
  findings from `groq` — the provider seam surviving a full vendor outage,
  demonstrated with no restart anywhere in this segment.
- **Bonus, same segment:** shortly after PR-1's happy-path review completed
  (well inside the 300s cooldown window), push a trivial follow-up commit to
  **PR-1**'s branch too. Expected: the ticket goes `deferred` with
  `not_before` ~5 minutes out — the escalating re-review cooldown, not a
  quota effect — and a **self-cleaning schedule footnote** is appended to
  PR-1's existing good comment rather than overwriting it. Narrate that this
  will self-heal in ~5 minutes without any action; given the cooldown isn't
  on the deploy CLI's config surface (see above), don't wait for it live
  unless the Render dashboard value was manually lowered beforehand.

### 4. Segment C — quota exhaustion + auto-recovery (~4-5 min)

Requires the new padded fixture (see prerequisite above). Still on `groq`.
Fire, back-to-back:

1. `uv run python -m scripts.seed_demo_pr --bulk` (or the chosen variant
   invocation) → **PR-3**
2. Same → **PR-4**
3. Same → **PR-5**

Expected, from the refill math above (starting near a full ~12,000-token
bucket, ~10,500 tokens/review, ~1,800 tokens refilled per ~9s dispatcher
cycle) — narrate this as "roughly," not an exact guarantee:

- PR-3 likely **succeeds** (~12,000 → ~1,500 remaining).
- PR-4 very likely **fails**: ~1,500 + 1,800 refill ≈ 3,300 available against
  a ~10,500 need → a real 429 → `RateLimited` → the whole ticket deferred →
  **plain placeholder comment** (`format_placeholder`) — no prior good
  review exists for this PR, so there's nothing to preserve.
- PR-5 very likely **also fails** the same way (~3,300 + 1,800 ≈ 5,100 vs.
  ~10,500) — a second placeholder, which is fine: more than one deferred
  ticket makes the point more clearly, not less.
- Since each review's own demand (~10,500) stays under the 12,000 **absolute**
  cap, both deferred tickets are guaranteed to succeed once enough real time
  passes (unlike a review sized at or above the full cap, which could never
  recover) — narrate this distinction explicitly, since it's the actual
  engineering guarantee being demonstrated, not just "it retries."
- Wait for the automatic retries (dispatcher polls every
  `dispatcher_idle_sleep_seconds` = 1s; each deferred ticket's `not_before`
  comes from Groq's real `Retry-After`) — no manual intervention. Once due,
  placeholders get replaced with real reviews.

### 5. Wrap-up (~1 min)

- Cost model: `cost.md`'s ~$8-10/mo at brief scale, demo ran at $0
  (Groq + Render + Supabase free tiers).
- What's not live: Vertex — evaluated and removed from the codebase entirely
  (no-card constraint), not merely mocked-and-untested.
- One-line callout: GitHub Models' retirement happened *during this
  project's life*, and the architecture absorbed it with a single DB write
  and no code change or redeploy.

## Pre-call / pre-rehearsal checklist

1. `curl https://pr-review-engine.onrender.com/healthz` → `200`. If not,
   check Render's dashboard for a stalled deploy before doing anything else.
2. Confirm the UptimeRobot monitor is active (keeps both Render and Supabase
   warm) — a cold instance's first response can blow the 15s target.
3. `uv run python -m scripts.set_provider --clear` — ensure no stale DB
   override survives from earlier testing; every segment's resting state
   assumes `.env`'s `LLM_PROVIDER=groq` on the service.
4. Check the testbed repo for leftover open PRs/comments from earlier
   rehearsals (this project's own hosting-migration work already used this
   repo) — close or ignore anything that would confuse the live narration.
5. Optionally run `/deploy` (or `uv run python -m scripts.deploy` with
   `PUBLIC_BASE_URL` set) for a full health/config/webhook check — note it
   can **write** a corrected webhook URL if one is found wrong; know that
   before running it minutes before going live.
6. Confirm today's Groq usage headroom is reasonable (no way to check
   remaining daily quota without a live call; if in doubt, budget for
   Segment C possibly behaving a bit differently than described, not for it
   failing outright — the daily cap is 1,000 requests, not something a
   handful of demo reviews meaningfully threatens).
7. Confirm `gh auth status` shows the account that owns `GITHUB_TARGET_REPO`.

## Open items / risks accepted

- **Segment C's sizing is a re-derived estimate, not a measurement** — the
  ~10,500-tokens/review, ~12,000-character fixture numbers come from
  `diff_utils`'s rough heuristic plus one measured single-call sample, not a
  direct measurement of the new padded fixture. One live rehearsal, after
  the fixture and seed-script variant exist, is what actually confirms this
  — not this document. This project has twice needed to correct a
  quota-sizing estimate against real behavior already (the original 12K-TPM
  read was directionally right but missed the continuous-refill dynamic);
  budget rehearsal time accordingly rather than trusting this on the first
  try.
- No recorded fallback for any segment, by explicit choice — a live failure
  during the actual call has no on-the-spot recovery.
- The GitHub Models failure mode (Segment B) depends on it staying dead
  between now and the call date — extremely likely, given it's a confirmed
  permanent retirement, not a transient outage.
- The cooldown-footnote beat's "self-heals in ~5 minutes" claim is
  unverified within this segment's own time budget unless
  `dispatcher_rereview_cooldown_seconds` is manually lowered on Render
  outside any tooling this project has for that — decide before the call
  whether to do that edit or just narrate without waiting.
- The testbed repo is shared with this project's own development work now
  (not exclusively a demo fixture), so its state (open PRs, consumed daily
  quota) can drift between rehearsals for reasons unrelated to this plan.
