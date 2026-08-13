# Demo plan — Zoom screen-share, course grading presentation

Date: 2026-08-03 (rewritten 2026-08-10, rehearsed 2026-08-10, hardened and
re-rehearsed twice more on 2026-08-12: once with a gemini/groq provider
split, once superseding that with a groq-only key-index split)
Status: **Ready, provisioned for tomorrow's presentation.** Rehearsed
against the real hosted service through two rounds of same-day hardening.
The live plan below is the key-index version (groq throughout, index 0 for
Segments 2/B, index 1 for Segment C) — the gemini-split version earlier in
this doc's history is superseded, not deleted, since it's still a real,
tested fallback if groq itself becomes unusable. Numbers are measured, not
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

## Rehearsal with an associate (2026-08-12) — Finding 3 recurred, worse

Ran the full plan (Segments 1-C) live against the updated dashboard-at-`/`
service, ahead of a rehearsal with an associate. Segments 1, 2, and B's
failure+cooldown-defer mechanics all ran clean and exactly as documented
below. Segment C reproduced Finding 3 again, worse than the 2026-08-10
measurement:

- PR #23 (happy path) and PR #25 (Segment C, first bulk PR) both ran clean.
- PR #24 (dead-vendor `github_models`) failed with the real `410`, deferred
  on push per the cooldown as expected, and **healed at 07:46:54 UTC** with
  real `groq` findings (3.3s, $0.0034) — confirms Finding 4 still holds.
- PR #26 (Segment C, second bulk PR) hit a real `429` as designed, but this
  time the placeholder escalated from "queued behind rate limit" straight
  to **`"⏳ Daily model quota reached"`**, with a backoff timestamp that
  climbed live from ~07:44 UTC to **08:24:43 UTC (~40 minutes out)** across
  repeated checks — not the ~8-second heal from the 2026-08-10 rehearsal.
  Root cause: this session had already fired 4 groq reviews (PRs #23-#26)
  plus retries within ~5 minutes, depleting the same slow-refilling
  requests-bucket Finding 3 already flagged as a risk, not a new bug.
- **Decision (with the user): stopped firing anything more at groq** and
  left PR #26 to heal on its own, rather than burning more quota chasing a
  faster resolution — consistent with CLAUDE.md's hygiene rule.
- **Implication for the live call:** Finding 3's risk is confirmed to
  compound with rehearsal frequency — a single rehearsal pass right
  before presenting is no longer just "not recommended" (per checklist
  item 7), it is now confirmed capable of leaving the account in exactly
  the depleted state a real audience would then walk into. If a rehearsal
  with an associate happens same-day as the graded call, budget real
  recovery time (tens of minutes, not the ~5s good case) between the two,
  or use Gemini for that day's Segment C run instead (per Finding 5,
  Gemini doesn't reproduce a 429 at these sizes at all, so it can't
  demonstrate the segment — only usable as a "quota risk is avoided
  entirely by swapping providers" fallback narration, not a live 429 demo).

## Project updates since the last rehearsal (2026-08-12)

Two more changes landed after the 2026-08-11 update below, re-verified live
before touching this plan further:

- **The dashboard now lives at `/`, not `/dashboard`.** `GET /dashboard` is
  a plain **404** — confirmed live, there is no redirect kept for the old
  path. Every reference below is updated to `https://pr-review-engine.onrender.com/`.
- **A new "How it works" section renders on the same page, below the review
  list** — five static steps (PR opened → diff fetched → 3 specialists in
  parallel → findings merged → comment posted), confirmed present in the
  live HTML (`id="howItWorks"`). It's translated UI chrome, not live data —
  no new poll, no new endpoint. This is a genuinely useful visual aid for
  Segment 1: it's the same flow the narration already covers, now visible
  on the same tab as the live stats, so **Segment 1 can point at it instead
  of (or alongside) narrating from memory.**
- **A runtime-tunable re-review cooldown & escalation factor is in
  progress, not deployed** (`docs/superpowers/specs/2026-08-12-runtime-cooldown-tuning-design.md`,
  work-in-progress in a separate worktree). This would let Segment B's
  ~5-minute cooldown wait be shortened live via a DB write (mirroring
  `set_override.py`), but it isn't in production yet — **this plan makes no
  use of it.** Once it ships, revisit Segment B's wait-or-narrate fallback
  (see "Open items" below).
- **9 more leftover PRs (#10, #11, #12, #15, #16, #19, #20, #21, #22) from
  the 2026-08-10 rehearsal were closed** at the start of this rehearsal
  (2026-08-12) — the testbed repo is shared across rehearsals and needs
  this sweep every time (see checklist item 5).

## Project updates since the rehearsal (2026-08-11)

A dashboard feature and an audit fix round landed after the 2026-08-10
rehearsal. Re-verified against the live service before touching this plan
further:

- **New: Ops/Demo Dashboard**, confirmed live at
  `https://pr-review-engine.onrender.com/dashboard`
  (`GET /api/dashboard` returns real queue/backoff/review data). Built
  specifically to serve this demo's own purpose — its spec's stated goal is
  "pull it up live while triggering a real PR review and watch it update."
  **Folded into the plan below**: opened once, kept visible for the entire
  walkthrough, not just one segment. Its `reviews` history only starts from
  when this feature deployed — none of this rehearsal's PRs (#10-#22) will
  appear in it, but every PR from here on will.
- **The deploy-credential-verification gap is resolved** — the parked
  thread from `docs/2026-08-10-deploy-provider-credential-verification-gap.md`.
  `scripts/deploy.py` now has a `provider-live` check, and
  `scripts/set_override.py` proactively verifies the target provider's
  credential against Render *before* writing an override, refusing by
  default (with a `--force` escape hatch) if it's missing or mismatched —
  the exact Gemini scenario hit live during rehearsal can't silently recur.
- **Audit fix round** (12 findings, security/performance/quality) landed
  and was checked against this plan's dependencies — no regressions:
  `factory.py`'s new per-provider instance caching still keys off
  `active_provider()` (the DB override), so Segment B/C's provider-swap
  mechanics are untouched; the planted secret in both fixtures was softened
  to an unambiguously synthetic value (still triggers the same
  hardcoded-credential finding, just doesn't look like a real leaked key
  anymore — cosmetic only); a 45s explicit LLM client timeout was added
  (irrelevant at this demo's real ~5-45s review latencies).
- **PR #16 confirmed healed** — real findings now posted, placeholder gone.
  No action needed; this was already the expected outcome per Finding 3.
- Still open, unrelated to the above, tracked separately: more Groq
  quota/request-bucket testing and hardening (Finding 3's risk isn't
  mitigated in code, just documented as a scheduling risk).

## Environment facts

- **Hosting is Render + Supabase**, confirmed live at
  `https://pr-review-engine.onrender.com/healthz`. Local `uvicorn` +
  Cloudflare quick tunnel is retired entirely.
- **`gh` and `uv` are native Linux binaries** in this environment.
- **Provider switching is a DB write** (`scripts/set_override.py` →
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

**Pre-demo setup, before narration starts:**

- `uv run python -m scripts.set_cooldown --base 30` — the presentation-
  friendly cooldown (2026-08-12), set now, off-camera, rather than
  mid-Segment-B. It's inert until a review actually finalizes, so setting
  it this early causes no visible effect before Segment B needs it.
- `uv run python -m scripts.set_provider --clear` and `uv run python -m
  scripts.set_api_key groq --clear` — deterministic starting state:
  provider falls back to the env default (`groq`), key index falls back
  to 0 (the base `GROQ_API_KEY`), regardless of whatever was left over
  from a prior rehearsal.

**Key-index hardening (2026-08-12), supersedes the gemini-split
hardening above:** the new per-provider API-key-slot override
(`scripts/set_override.py`, live per the 2026-08-12 api-key-index-override
design, later unified with the provider override by the 2026-08-12
override-cli-unification design) makes the earlier groq/gemini provider
split unnecessary — Segment
C can now get its own **separate quota bucket** via a different key index
on the *same* provider, instead of switching vendors. This drops Gemini
from the live segment plan entirely: no more 34s response times, no more
transient 504/503 variance, no `GEMINI_API_KEY` dependency for these
segments. Two of the three provisioned Render `GROQ_API_KEY*` slots are
assigned; the third (`GROQ_API_KEY_2`, index 2) is an unassigned spare —
if index 0 or 1 turns out exhausted right before or during the real call,
`uv run python -m scripts.set_api_key groq 2` is a live escape hatch with
no redeploy.

**Open `https://pr-review-engine.onrender.com/` in a second tab/window now**
and leave it visible (polling every 4s) for the rest of the walkthrough.
Narrate the flow from `README.md`/`SPEC.md`'s diagram (webhook → HMAC verify
→ dedup → durable Postgres ticket → single serial dispatcher → diff fetch +
annotate → 3 concurrent specialists → merge (atomic on rate-limit, never
partial otherwise) → upsert PR comment), then **scroll to the "How it
works" section on the same page** — it's the same five-step flow rendered
as a visual, so the audience gets it twice, once spoken and once on
screen, on the tab they're about to watch for the rest of the demo. Mention
Render + Supabase as the production home.

Scroll back up before moving on — stat tiles (total reviews, cost, avg
time), queue depth by status, per-provider backoff state, and a live review
list are what turns every later segment from "trust me, it worked" into
something the audience
watches happen in real time.

### 2. Happy path (~2 min) — establishes the baseline

**Key-index hardening (2026-08-12):** stays on `groq`, index 0 (the base
`GROQ_API_KEY`) — already the state from Segment 1's pre-demo setup, no
script call needed here.

- `uv run python -m scripts.seed_demo_pr` → opens a PR with the planted
  `fixtures/bad_code/billing_report.py` issues.
- Previously measured on `groq`: 4.9s, $0.0034 — re-measure at the next
  rehearsal on the current `GROQ_API_KEY` (rotated 2026-08-12); the
  provider is unchanged but the specific key is new, so don't assume the
  old number without re-checking.
- Show the resulting comment, then point at the dashboard: the review list
  gains a row (real findings expandable inline) and the stat tiles update —
  total reviews, cost, avg time all tick from their prior values.

### 3. Segment B — a real vendor died overnight, and self-heals on its own schedule (~2 min, was ~5)

**Cooldown hardening (2026-08-12):** the re-review cooldown is now
DB-tunable (`scripts/set_cooldown.py`, live per the 2026-08-12 runtime-
cooldown-tuning design) — already set to a presentation-friendly 30s back
in Segment 1's pre-demo setup, replacing the 300s default. This is what
turns the old "narrate the mechanism and move on without waiting"
fallback into an actual on-screen heal that fits the time budget.

- Provider is already `groq` (index 0) from Segment 2 — unchanged.
- **Set the DB override:** `uv run python -m scripts.set_provider github_models`.
- `uv run python -m scripts.seed_demo_pr` → opens a **fresh** PR (not the
  happy-path one — a fully dead provider's failure still finalizes the
  review and would overwrite a good comment with an all-failed one).
- **Measured live (previous rehearsal):** comment posts immediately (no
  dispatcher retry — a *completed* review with 3 failed rows) showing the
  real error: `Error code: 410 - {'error': {'code':
  'github_models_retirement_brownout', ...}}` — narrate that this is
  GitHub's actual retirement, not a staged failure. The dashboard's review
  list shows this row too — a real failed review, visible and countable,
  not swept under the rug.
- **Clear the override:** `uv run python -m scripts.set_provider --clear`
  — falls back to `groq`, index 0 (the key-index override is untouched by
  a provider swap; each provider tracks its own slot independently, so
  returning to `groq` returns to whichever index was active before, still
  0 here). This is simpler than the gemini-split version of this step:
  clearing is now correct, not a trap, because groq is the env default.
- Push a trivial follow-up commit to the same PR's branch (`gh pr checkout`
  into a scratch clone, since `seed_demo_pr`'s own clone is discarded).
- **Expected, with the 30s override:** the push lands inside the
  (now 30s, not 300s) cooldown window and gets deferred with a schedule
  footnote: `"🔄 Re-review scheduled ~<time>"`. Narrate this explicitly as
  the self-cleaning re-review cooldown protecting against redundant work
  on rapid pushes — not a quota effect, and not a mistake in the demo.
  **This PR is the one expected to actually complete on screen** — unlike
  Segment C's second PR below, which is expected to stay blocked.
- **Once due** (~30s later, not ~5 min): the same comment (same marker)
  updates in place with real `groq` findings (index 0 — same quota bucket
  as Segment 2, not Segment C's), footnote gone, live, within the
  segment's own time budget.

### 4. Segment C — quota exhaustion + auto-recovery (~4 min)

**Key-index hardening (2026-08-12):** stay on `groq` (unchanged since
Segment 2), but switch to **index 1** — a separate `GROQ_API_KEY_1`
credential, meaning this segment's own quota-exhaustion story trips a
limit on a bucket Segments 2 and B never touched, and vice versa: this
segment's burst can't spill over and cause an unrelated defer somewhere
else in the walkthrough.

- **Set the key-index override:** `uv run python -m scripts.set_api_key groq 1`.

Fire, back-to-back:

1. `uv run python -m scripts.seed_bulk_demo_pr` → PR A
2. Same → PR B

**Confirmed live (2026-08-10, post-fix):**

- PR A succeeded normally (5.6s, 10,664 tokens).
- PR B hit a real `429` → `RateLimited` → deferred → placeholder:
  `"⏳ Queued behind rate limit — review will appear shortly."` — **this is
  the dashboard's best moment**: point at `queue.backoff.groq` populating
  with a real until-timestamp, and `queue.by_status.deferred` ticking to 1,
  live, at the exact instant the placeholder appears on GitHub.
- PR B healed automatically ~8 seconds later on that occasion, no manual
  action — the dashboard's backoff field clears and `deferred` drops back
  to 0 in the same refresh cycle the GitHub comment updates in.
- Narrate the guarantee explicitly: this review's own demand (~10,664)
  stays under the 12,000 absolute cap, so recovery is *guaranteed* once
  enough real time passes — unlike a review sized at or above the full cap,
  which could never recover. That distinction is the actual engineering
  point, not just "it retries."

**Key-index hardening expectation (2026-08-12):** `GROQ_API_KEY_1` (index
1) is a freshly-provisioned, previously-unused slot as of this hardening
pass — expect it to behave like a healthy account (a fast, on-screen heal,
seconds not minutes) the first time it's actually exercised, the same way
the rotated base key did at index 0. That said, **don't promise the fast
heal as a hard guarantee**: this same slot can be depleted by rehearsing
Segment C repeatedly before the real call, exactly like every prior key
was (Finding 3's risk applies to whichever slot is active, not to a
specific key value) — if a rehearsal pass has already burned this slot's
budget, plan for PR B to stay blocked for the rest of the live segment and
treat that as expected, not a failure to fix live. The placeholder itself
— a real 429, a real until-timestamp on the dashboard, a real guarantee of
eventual recovery — is the demo point regardless of which way it goes.

**If PR A itself doesn't trip a 429** (headroom can vary — see Finding 3),
fire a third immediately rather than re-deriving the math: three reviews'
demand against a 12,000 cap is strictly stronger than two.

### 5. Wrap-up (~1 min)

- Cost model: `cost.md`'s ~$8-10/mo at brief scale, demo ran at $0 (Groq +
  Render + Supabase free tiers — Gemini is no longer part of the live
  segment plan as of the 2026-08-12 key-index hardening). Real measured
  costs from the last full rehearsal: $0.0034 (happy path) to
  $0.0073-0.0074 (Segment C's two oversized reviews) — all on `groq`.
- What's not live: Vertex — evaluated and removed from the codebase
  entirely; Gemini remains a working, tested alternative (Segments 2/B ran
  on it during the 2026-08-12 rehearsal) but isn't exercised in the
  current live plan.
- One-line callout: GitHub Models' retirement happened *during this
  project's life*, and the architecture absorbed it with a single DB write
  and no code change or redeploy.
- Second callout, new this hardening pass: **the same DB-override pattern
  now covers four independent knobs live** — provider and which API-key
  slot it uses (`set_override.py`), and the re-review cooldown's
  base/cap/escalation-factor (`set_cooldown.py`) — all writable
  with zero redeploy, three of the four demonstrated in this same
  walkthrough (cooldown, key-index; provider swap is Segment B's own
  story).
- **Clean up before ending the call:** `uv run python -m scripts.set_provider
  --clear`, `uv run python -m scripts.set_api_key groq --clear`, and `uv
  run python -m scripts.set_cooldown --clear` — reverts provider to env
  default, key index to 0, and the cooldown from Segment 1's 30s back to
  the real 300s default, so nothing demo-tuned is left live afterward.

## Pre-call / pre-rehearsal checklist

1. **Confirm local `HEAD` matches `origin/main`** (`git log origin/main..HEAD`
   should be empty) — push first if not. This is now the single most
   important step; everything else assumes the deployed code actually has
   the features this plan depends on (Finding 1).
2. `curl https://pr-review-engine.onrender.com/healthz` → `200`.
2b. `curl https://pr-review-engine.onrender.com/api/dashboard` → real JSON
   (stats/queue/reviews, no `"error"` fields) — confirms the dashboard
   itself is healthy before relying on it live in Segment 1.
3. Confirm the UptimeRobot monitor is active.
4. `uv run python -m scripts.set_provider --clear`, `uv run python -m
   scripts.set_api_key groq --clear`, and `uv run python -m
   scripts.set_cooldown --clear` — no stale DB override of any of the
   three kinds left over from a prior rehearsal.
5. Check the testbed repo for leftover open PRs from earlier rehearsals;
   close anything that would confuse the live narration.
6. **Both `GROQ_API_KEY_1` and `GROQ_API_KEY_2` must actually be present
   on Render, not just local `.env`** (2026-08-12) — provisioning a new
   numbered slot needs a one-off manual push (see the finding above:
   `--sync-env` does *not* push numbered slots despite what README says;
   this is a real doc/code gap in the shipped feature, not yet fixed).
   Confirm via `uv run python -m scripts.deploy` (`api-key-live` only
   checks the *currently active* provider+index, so this alone doesn't
   prove index 1/2 are present — check presence directly, e.g. via
   `_render.env_vars()`, if a fresh slot was provisioned since the last
   check).
7. **Don't rehearse Segment C heavily in the hour before the real call** —
   groq's requests-bucket refills slowly enough (~1 slot/86s) that a
   rehearsal session can deplete it in a way that only recovers over tens
   of minutes to hours under sustained use (Finding 3, confirmed worse on
   2026-08-12: one ticket took over 2.5 hours to clear). This now applies
   per-key-index, not per-account: Segment C's `GROQ_API_KEY_1` (index 1)
   is its own bucket, separate from Segments 2/B's `GROQ_API_KEY` (index
   0) — rehearsing 1-2 and B no longer burns Segment C's budget, and
   vice versa, but repeated Segment C rehearsals still deplete index 1
   specifically.
8. Confirm `gh auth status` shows the account that owns `GITHUB_TARGET_REPO`.
9. **After rotating any provider API key** (e.g. a fresh Groq key after a
   prior rehearsal exhausted the account): update it locally, then push
   with `--sync-env` and re-run `scripts.deploy` to confirm `provider-live`
   passes for the *currently overridden* provider before relying on it —
   the check only verifies whichever provider is active when it runs, not
   every provider's credential at once.

## Open items / risks accepted

- No recorded fallback for any segment, by explicit choice.
- GitHub Models' failure mode depends on it staying dead between now and
  the call date — extremely likely, given it's a confirmed permanent
  retirement.
- ~~Segment B's cooldown-heal wait (~5 min) may not fit comfortably in a
  live time budget~~ — **superseded 2026-08-12**: confirmed live at the
  30s override — healed well within the segment's budget (dispatcher
  claimed the due ticket ~15-20s after the push, comment updated
  ~30-45s after that). Timing has some natural looseness (poll interval +
  actual review runtime stack on top of the 30s floor), but stayed
  comfortably inside acceptable margins both times measured — **not** a
  precise 30-second guarantee, budget more like "under a minute."
- **New 2026-08-12:** Segment B's re-review itself hit real, transient
  Gemini-side errors on one run (`504 DEADLINE_EXCEEDED`, `503
  UNAVAILABLE` — high demand), independent of anything this project
  controls. Accepted as ordinary provider variance, not tracked further —
  the partial-failure guarantee held (1 of 3 specialists still succeeded
  and posted), which is itself the point if it recurs live.
- **New 2026-08-12:** the freshly-rotated groq key behaves like a healthy
  account — Segment C's second PR healed in ~18s on it, vs. the old key's
  multi-hour stall. This key is now real, spent quota too: the existing
  "don't over-rehearse Segment C before the real call" guidance
  (checklist item 7) applies to it exactly as it did to the old one — a
  fresh key resets the clock, it doesn't remove the risk.
- The testbed repo and Groq's daily request budget are both shared,
  cross-session state — either can drift for reasons unrelated to this
  plan between the last rehearsal and the real call. **Superseded again,
  same day (key-index hardening)**: every segment is back on `groq`, but
  each uses its own key index (0 for Segments 2/B, 1 for Segment C), so
  the shared-budget risk is now scoped per-index, not eliminated for any
  segment — see checklist item 7.
- A rate-limited ticket's auto-heal is not a bounded-time guarantee under
  sustained load — confirmed live twice now: first a push from 09:56 UTC
  to 10:41 UTC, then the 2026-08-12 associate rehearsal where one ticket's
  wait climbed past 2.5 hours. **Accepted as Segment C's actual behavior,
  not something to fix live**: plan for the second oversized PR to stay
  deferred through the rest of the segment (see Segment 4 above) rather
  than promising or waiting for an on-screen heal.
- ~~New 2026-08-12: Gemini is now load-bearing for two segments (2, B)~~
  — **superseded same day**: the key-index hardening above drops Gemini
  from the live plan entirely; this concern no longer applies.
- **New 2026-08-12 (key-index hardening):** swapping providers/indices
  mid-plan (`groq` idx0 → `github_models` → `groq` idx0 → `groq` idx1)
  means a missed `set_provider` or `set_api_key` step leaves the *wrong*
  credential active for a whole segment with no error — narrate each swap
  explicitly and verify the dashboard/comment's `provider:` field matches
  what's expected before moving on, don't just trust the script ran. This
  risk is smaller than the gemini-split version (fewer distinct swaps,
  same vendor throughout except the one deliberate Segment B failure), but
  not zero.
- **New 2026-08-12 (key-index hardening):** the `--sync-env`/README
  mismatch found while provisioning `GROQ_API_KEY_1`/`_2` (checklist item
  6) is a real gap in shipped code/docs, not just a rehearsal footnote —
  worth a proper fix (either teach `_wanted_env()` about numbered slots,
  or correct README's claim) before this feature gets used again outside
  a one-off manual push.
