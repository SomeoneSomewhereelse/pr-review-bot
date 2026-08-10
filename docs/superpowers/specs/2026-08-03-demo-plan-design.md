# Demo plan — Zoom screen-share, course grading presentation

Date: 2026-08-03 (rewritten 2026-08-10, rehearsed 2026-08-10)
Status: **Rehearsed against the real hosted service.** Every segment below
has run for real at least once, post-fixes. Numbers are measured, not
estimated, unless explicitly marked otherwise.

**Supersedes:** the original 2026-08-03 version in full, and the
2026-08-10 rewrite's Segment C sizing (which was a re-derived estimate at
the time — see the rehearsal findings below for what changed after actually
running it).

## Purpose

A live, screen-shared walkthrough of the Autonomous Code Review Engine for a
course grading presentation. Budget: 10-15 minutes of demo time. All core
moments (the bot actually reviewing PRs) are **live against the real GitHub
webhook path, no recorded fallback** — the "failure" beats below are
*engineered into the plan on purpose*, not things we're hoping don't happen.

## Rehearsal findings (2026-08-10) — read before running this plan again

Four real, load-bearing issues surfaced during rehearsal, none of which were
visible from reading the code alone:

### 1. The deployed service was 61 commits stale — this blocked the entire premise of Segment B

`render.yaml`'s service builds from a separate GitHub remote
(`SomeoneSomewhereelse/pr-review-bot`, this project's own repo — not the
testbed repo), and that remote's `main` had not been pushed to since Aug 7.
The DB-backed provider-override feature, Vertex's retirement, and all the
deploy-hardening work existed only in the local working tree. First symptom:
setting the override to `github_models` and opening a PR still got reviewed
by `groq` — the deployed code simply didn't have the feature.
**Fix:** `git push origin master:main` (a clean fast-forward, 61 commits,
zero conflicts) → Render auto-deployed. **New standing checklist item:
confirm local `HEAD` matches `origin/main` before rehearsing or presenting
— don't assume "committed locally" means "live."**

### 2. Groq's SDK was silently retrying 429s, hiding them from the app entirely

`AsyncGroq` defaults to `max_retries=2`. A 429 got retried internally by the
SDK, with backoff, before `app/providers/groq.py`'s `except Exception` clause
ever ran — so the app's own `RateLimited`/defer/placeholder path (the actual
thing Segment C exists to demonstrate) never fired, no matter how the load
was sized. Confirmed live: a review that should have hit the cap ran in
43.1s instead of the normal ~5s (silent retry-and-succeed), with `zero`
`429`s surfacing anywhere. **Fixed** (commit `6397cfc`, deployed):
`AsyncGroq(api_key=..., max_retries=0)` — the app's own durable queue
already owns retry/backoff (durable across a restart, visible via a
placeholder), so a second, hidden retry layer underneath it was actively
counterproductive, not just redundant. Gemini's SDK does **not** have this
problem — `google-genai`'s `retry_args()` defaults to
`stop_after_attempt(1)` ("never retry") when `http_options.retry_options` is
unset, which `GeminiProvider` never sets.

### 2b. Segment C's token-sizing estimate was accurate — the fix above was the actual blocker, not the math

Once the fix was deployed, the padded fixture behaved almost exactly as
designed: **10,664 measured tokens/review** against an estimate of ~10,712 —
well within noise. Two oversized PRs fired back-to-back: the first
succeeded normally (5.6s), the second hit a real `429` and showed the
`"⏳ Queued behind rate limit"` placeholder, then healed automatically once
due. **The original Segment C design (§4 below) is confirmed correct as
written — it just needed the SDK fix to actually manifest.**

### 3. Groq's request-count bucket refills far slower than its token bucket — a real scheduling risk

A live header probe earlier this session measured `x-ratelimit-limit-requests:
1000`, and its own reset-time math implies a refill rate of about one request
slot every **86.4 seconds** (1000 ÷ 86,400s/day) — versus the token bucket's
~200 tokens/**second**. A single rehearsal session firing dozens of review
cycles within about an hour depletes this slow bucket even while the token
bucket (the one Segment C targets) stays healthy. Confirmed live: after the
Segment C burst above, a **separate, later** review hit this different
limit — `Retry-After` of roughly 30 minutes, which `format_placeholder`'s own
wording heuristic correctly rendered as `"⏳ Daily model quota reached"`
rather than the short-wait message. **This is a real risk for the actual
graded call, not just this rehearsal**: if the requests-bucket is already
depleted from rehearsing right before going live, the happy path or Segment
B — neither of which is *designed* to hit a rate limit — could hit this one
unpredictably. **New standing checklist item: don't rehearse heavily in the
hour immediately before the real call.**

**Confirmed live, and worse than "just wait it out":** this same deferred
ticket's own scheduled retry fired exactly on time (09:56 UTC, per
`updated_at`) and **hit the limit again**, pushing its own ETA later —
10:41 UTC, not resolved. `attempts` stayed `0` (rate-limit defers don't
increment the hard-failure attempts counter), so nothing was stuck or
broken — the account-wide resource genuinely hadn't recovered enough yet,
likely compounded by every atomic-discarded retry still burning real
request-quota on whichever of its 3 concurrent specialist calls happened to
succeed before the overall review was thrown away for being rate-limited.
**A rate-limited ticket's wait is not guaranteed to shrink on retry under
sustained account-wide load — it can grow.** This is a real risk to know
about, not just a rehearsal artifact: don't promise a live audience "this
will heal in a few seconds" as a hard guarantee if the account has been
under heavy use.

### 4. Segment B's "push → immediate heal" framing was wrong — the cooldown beat isn't a separate bonus, it's what actually happens

`finalize_review` runs for **any** completed review, success or failure —
confirmed live: the deliberately-failed `github_models` review still set
`last_reviewed_at` and escalated `cooldown_level`. A push-triggered
re-review of that same PR therefore lands inside the 300s cooldown window
and gets **deferred with a schedule footnote**, not re-run immediately — the
exact same mechanic the original plan awkwardly staged as a separate "bonus"
demo on PR-1. **The 2026-08-10 rewrite's "bonus" step is deleted below —
Segment B's own re-review naturally demonstrates it, for free, with no
extra PR needed.** Confirmed live end-to-end: PR-12 deferred with
`"🔄 Re-review scheduled ~08:15 UTC"`, then — once due — the **same comment**
updated in place with real `groq` findings (3.0s, $0.0034), footnote gone.

### 5. Gemini was ruled out for the quota beat for two different reasons, in sequence

First (documented, unresolved as of the 2026-08-10 rewrite): Gemini's
account-level block meant it failed with `403`, not `429` — wrong failure
shape for this segment regardless of sizing. That block **was independently
resolved this same session** (see `README.md`/`SETUP.md`, 2026-08-10) via an
updated API key. Once working, it was tried anyway for Segment C, twice —
first with the small default fixture (2 concurrent reviews, ~5,139
tokens/review), then with the oversized bulk fixture (2 concurrent reviews,
~13,416 tokens/review). **Neither tripped a 429.** This key's real limits
are evidently well above whatever figure was expected going in. A real,
separate gap surfaced along the way: switching the DB override to `gemini`
initially failed with `"No API key was provided"` — `GEMINI_API_KEY` had
never been pushed to Render (`render.yaml` declares it `sync: false`,
operator-supplied, and no prior deploy had ever used Gemini live). Both
`check_config` (env-provider-only, by design) and `provider` (checks the
override's credential **locally**, not on the deployed service) reported
`PASS` regardless — exactly the gap `docs/superpowers/specs/2026-08-08-provider-agnostic-config-and-deploy-hardening-design.md`
§3.5 already documents as a known limitation, not a bug that slipped past a
check meant to catch it. Fixed by running `--sync-env` (which opportunistically
pushes any locally-set provider credential, not just the selected one).
**Conclusion: Groq (post-fix) remains the quota-exhaustion provider. Gemini
is confirmed live and working again, but is not a useful tool for this
specific segment with this account's current limits.**

## Environment facts

- **Hosting is Render + Supabase**, confirmed live at
  `https://pr-review-engine.onrender.com/healthz`. Local `uvicorn` +
  Cloudflare quick tunnel is retired entirely.
- **`gh` and `uv` are native Linux binaries** in this environment.
- **Provider switching is a DB write** (`scripts/set_provider.py` →
  `runtime_config` table), effective on the **next ticket claimed** — no
  restart, no redeploy. Confirmed live, repeatedly, once the service itself
  was up to date (see Finding 1).
- **`scripts/deploy.py` / `/deploy`** verifies `config`, `github-app`,
  `health`, `database`, `provider`, `render-service`, `uptime-pinger`, and
  `--sync-env` pushes config + redeploys. `github-app` can **write** a
  corrected webhook URL; `--sync-env` genuinely pushes secrets and triggers a
  real deploy (both confirmed live this session, deliberately).
- **Vertex is deleted from code.** **GitHub Models is still a valid provider
  value in code** — the real external API's July 30, 2026 retirement is
  unrelated to any of this project's own changes, so Segment B's premise
  holds (confirmed live: real `410 github_models_retirement_brownout`
  error).
- **Groq ground truth, live-measured:** `12,000` token/min cap, continuous
  refill (~200 tokens/sec); `1,000` requests/day cap, continuous refill
  (~1 slot/86.4s) — see Findings 2-3 above for why both matter differently.
- The escalating re-review cooldown (`dispatcher_rereview_cooldown_seconds`,
  default 300s) is not on `render.yaml`'s env list or `--sync-env`'s push
  set — not adjustable via this project's tooling, only Render's dashboard
  directly.
- **The testbed repo is shared** with this project's own development work.
  9 leftover PRs from earlier hosting-migration rehearsals were closed at
  the start of this rehearsal (2026-08-10).

## Prerequisite fixture — built and confirmed

`fixtures/demo_bulk_bad_code/billing_report_bulk.py` +
`scripts/seed_bulk_demo_pr.py` (both git-ignored — demo-only scaffolding,
not part of the graded feature set). Measured live at **10,664 tokens/review**
against Groq (estimate was 10,712) and **13,416 tokens/review** against
Gemini (different tokenizer/model, not independently targeted).

## Segment plan (~13 min total)

### 1. Architecture overview (~2 min)

Narrate the flow from `README.md`/`SPEC.md`'s diagram: webhook → HMAC verify
→ dedup → durable Postgres ticket → single serial dispatcher → diff fetch +
annotate → 3 concurrent specialists → merge (atomic on rate-limit, never
partial otherwise) → upsert PR comment. Mention Render + Supabase as the
production home. No live action needed yet.

### 2. Happy path (~2 min) — establishes the baseline

- Provider: `groq` (confirm via `uv run python -m scripts.set_provider --clear`
  beforehand).
- `uv run python -m scripts.seed_demo_pr` → opens a PR with the planted
  `fixtures/bad_code/billing_report.py` issues.
- **Measured live: 4.9s, real findings across all three specialists, $0.0034.**
- Show the resulting comment.

### 3. Segment B — a real vendor died overnight, and self-heals on its own schedule (~5 min)

- **Set the DB override:** `uv run python -m scripts.set_provider github_models`.
- `uv run python -m scripts.seed_demo_pr` → opens a **fresh** PR (not the
  happy-path one — a fully dead provider's failure still finalizes the
  review and would overwrite a good comment with an all-failed one).
- **Measured live:** comment posts immediately (no dispatcher retry — a
  *completed* review with 3 failed rows) showing the real error:
  `Error code: 410 - {'error': {'code': 'github_models_retirement_brownout', ...}}`
  — narrate that this is GitHub's actual retirement, not a staged failure.
- **Clear the override:** `uv run python -m scripts.set_provider --clear`.
- Push a trivial follow-up commit to the same PR's branch (`gh pr checkout`
  into a scratch clone, since `seed_demo_pr`'s own clone is discarded).
- **Measured live: this does NOT immediately re-review.** The failed review
  already finalized and started the escalating cooldown, so the push lands
  inside the 300s window and gets deferred with a schedule footnote:
  `"🔄 Re-review scheduled ~<time>"`. Narrate this explicitly as the
  self-cleaning re-review cooldown protecting against redundant work on
  rapid pushes — not a quota effect, and not a mistake in the demo.
- **Once due** (confirmed live, ~5 min later): the same comment (same
  marker) updates in place with real `groq` findings, footnote gone. If the
  live time budget doesn't comfortably fit a 5-minute wait, narrate the
  mechanism and move on rather than sitting through it — the mechanism
  itself, not the wait, is the point.

### 4. Segment C — quota exhaustion + auto-recovery (~4 min)

Still on `groq`. Fire, back-to-back:

1. `uv run python -m scripts.seed_bulk_demo_pr` → PR A
2. Same → PR B

**Confirmed live (2026-08-10, post-fix):**

- PR A succeeded normally (5.6s, 10,664 tokens).
- PR B hit a real `429` → `RateLimited` → deferred → placeholder:
  `"⏳ Queued behind rate limit — review will appear shortly."`
- PR B healed automatically ~8 seconds later, no manual action.
- Narrate the guarantee explicitly: this review's own demand (~10,664)
  stays under the 12,000 absolute cap, so recovery is *guaranteed* once
  enough real time passes — unlike a review sized at or above the full cap,
  which could never recover. That distinction is the actual engineering
  point, not just "it retries."

**If a PR doesn't trip a 429** (headroom can vary — see Finding 3 on the
slower-refilling requests bucket also being able to shift timing), fire a
third immediately rather than re-deriving the math: three reviews' demand
against a 12,000 cap is strictly stronger than two.

### 5. Wrap-up (~1 min)

- Cost model: `cost.md`'s ~$8-10/mo at brief scale, demo ran at $0
  (Groq + Render + Supabase free tiers). Real measured costs this rehearsal:
  $0.0034 (happy path) to $0.0073 (oversized Segment C review).
- What's not live: Vertex — evaluated and removed from the codebase entirely.
- One-line callout: GitHub Models' retirement happened *during this
  project's life*, and the architecture absorbed it with a single DB write
  and no code change or redeploy.

## Pre-call / pre-rehearsal checklist

1. **Confirm local `HEAD` matches `origin/main`** (`git log origin/main..HEAD`
   should be empty) — push first if not. This is now the single most
   important step; everything else assumes the deployed code actually has
   the features this plan depends on (Finding 1).
2. `curl https://pr-review-engine.onrender.com/healthz` → `200`.
3. Confirm the UptimeRobot monitor is active.
4. `uv run python -m scripts.set_provider --clear` — no stale DB override.
5. Check the testbed repo for leftover open PRs from earlier rehearsals;
   close anything that would confuse the live narration.
6. If switching to a provider that has never been deployed live before
   (e.g. testing Gemini), confirm its credential is actually on Render —
   `--sync-env` pushes it opportunistically; a locally-set-only credential
   will pass every check and then fail every real review (Finding 5).
7. **Don't rehearse heavily in the hour before the real call** — Groq's
   requests-bucket refills slowly enough (~1 slot/86s) that a rehearsal
   session can deplete it in a way that only recovers over tens of minutes,
   risking an unrelated long defer in a segment that isn't supposed to
   demonstrate one (Finding 3).
8. Confirm `gh auth status` shows the account that owns `GITHUB_TARGET_REPO`.

## Open items / risks accepted

- No recorded fallback for any segment, by explicit choice.
- GitHub Models' failure mode depends on it staying dead between now and
  the call date — extremely likely, given it's a confirmed permanent
  retirement.
- Segment B's cooldown-heal wait (~5 min) may not fit comfortably in a live
  time budget; the fallback is narrating the mechanism without waiting for
  it to resolve on screen.
- The testbed repo and Groq's daily request budget are both shared,
  cross-session state — either can drift for reasons unrelated to this
  plan between the last rehearsal and the real call.
- A rate-limited ticket's auto-heal is not a bounded-time guarantee under
  sustained load — confirmed live, one ticket's own scheduled retry pushed
  its ETA later (09:56 UTC → 10:41 UTC) instead of resolving. Segment C's
  own burst is sized to recover quickly in isolation, but if the account is
  already depleted going into it (e.g. from over-rehearsing beforehand,
  per the checklist item above), don't guarantee a fast on-screen heal.
