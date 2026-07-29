# Follow-up findings — dispatcher backoff & re-review triggering

**Date:** 2026-07-28
**Status:** Open — needs its own planning session (brainstorm + plan), not a quick patch
**Relates to:** `docs/superpowers/specs/2026-07-27-queue-features-design.md` (durable
review queue design), `docs/superpowers/plans/2026-07-27-review-queue.md`
(implementation plan), branch `feat/review-queue` (commit `7a4690e` is where
the interim fixes described below landed).

## Context

During the durable-review-queue implementation, a final whole-branch review
caught six cross-task integration bugs (see the branch's final-review fix
commit `7a4690e`). Two of those fixes were accepted as adequate *for that
review round* but are explicitly flagged here as **interim, not final** —
both need a proper design pass before this feature is considered done.

---

## Finding 1 — dispatcher backoff is a blunt floor, not a real guardrail

### What was fixed (interim)

`app/queue/dispatcher.py`'s `run_forever()` previously slept
`settings.dispatcher_idle_sleep_seconds` only after an `"idle"` step. A
`Retry-After: 0` (or an already-past HTTP-date, which is common under clock
skew) meant a `"deferred"`/`"failed"` step got **zero** backoff, so the loop
could immediately refire against a provider that had just returned a 429 —
exactly the repeated-429-without-backoff pattern that `CLAUDE.md` documents
as having already gotten this project's Gemini access blocked.

The interim fix: `run_forever` now calls
`await asyncio.sleep(settings.dispatcher_idle_sleep_seconds)`
**unconditionally after every iteration**, regardless of the step's outcome.

`settings.dispatcher_idle_sleep_seconds` defaults to **`1.0`** (seconds) —
`app/config.py:29`, a `pydantic_settings.BaseSettings` field (overridable via
the `DISPATCHER_IDLE_SLEEP_SECONDS` env var / `.env`), instantiated once at
import time as the module-level `settings` singleton (`app/config.py:33`).

### Why this is not enough

A flat 1-second floor is not a guardrail against sustained abuse, it's a
speed limit:

- **No escalation.** A provider that keeps 429-ing gets hit once per second,
  forever — no exponential backoff, no circuit breaker, no cap on
  consecutive failures. A provider having a bad day (or a real outage) gets
  hammered at a slow, steady 1 Hz instead of being given room to recover.
- **No distinction between failure types.** A `RateLimited` deferral, a
  `mark_failed` (real GitHub/network error), and a healthy `idle` tick all
  get treated identically — the same fixed sleep. A repeated hard failure
  (not a 429, a genuine error) probably deserves a different, likely longer,
  backoff than "provider is fine, queue is just empty."
- **No jitter.** A single-process dispatcher doesn't need it today, but a
  fixed-interval retry is the wrong default to bake in if this queue is ever
  run with more than one instance (the design doc's §3 already notes the
  ticket-claim atomicity was written with that possibility in mind, even
  though it isn't built for it yet).
- **No hard ceiling.** Nothing stops a ticket from deferring indefinitely if
  a provider is down for hours or days — `attempts` is tracked on the ticket
  (`store.py`'s `defer()` increments it) but nothing currently *acts* on that
  count (no max-attempts cutoff, no escalating alert).

### Direction for the next planning session

Needs a real design decision, not a one-line patch:
- Exponential backoff (with a cap) keyed on consecutive failures per
  provider, separate from the `Retry-After`-driven `blocked_until` gate.
- Whether `mark_failed` tickets should get their own backoff/retry policy
  distinct from `RateLimited` deferrals (right now `mark_failed` doesn't
  even retry — worth deciding if it should, and how many times).
- Whether `attempts` should ever trip a hard stop (e.g., "fail permanently
  and notify after N attempts") rather than deferring forever.
- Whether jitter is worth adding now, given the multi-instance groundwork
  already laid in the ticket-claim design.

---

## Finding 2 — re-review triggering is inconsistent and undocumented across ticket states

### What was fixed (interim)

SPEC.md previously overclaimed that a push landing during an in-flight
review would make its newer `head_sha` "the next claim" — implying the
newer commit gets reviewed automatically. In reality `head_sha` is written
to the ticket row but never read by any code path, and `mark_done()`
finalizes the ticket unconditionally regardless of what `head_sha` is on
the row by the time it completes. The interim fix corrected the SPEC.md
wording only — no behavior changed.

### What the current (undocumented-as-a-design, verified-by-testing) behavior actually is

Tracing `app/queue/store.py`'s `enqueue_or_update()` CASE logic against each
ticket state a push can land on:

| Ticket state when the push lands | What happens |
|---|---|
| `pending` (queued, not yet claimed) | Row updates `head_sha`, stays `pending` — next claim naturally reviews the current (latest) diff since `attempt_review` fetches the diff live from GitHub, not from the stored `head_sha`. Clean. |
| `deferred` (rate-limited / daily-wall, waiting on `not_before`) | Row updates `head_sha`, **status resets to `pending`, `not_before` clears** — the ticket is immediately eligible again and gets reviewed fresh (still subject to the `blocked_until` gate). Clean, but this is a side effect of the CASE logic protecting only `'running'`, not a deliberately designed "coalesce rapid pushes" behavior. |
| `running` (dispatcher is actively mid-review) | Row updates `head_sha` for record-keeping only; the in-flight review finishes and posts against the **original** commit it started with. The new commit is silently **not** re-reviewed as part of that ticket. Only a further push, landing *after* this ticket reaches `done`/`failed`, will enqueue a genuinely fresh review. |
| `done` / `failed` (terminal) | Normal path — `enqueue_or_update` inserts fresh via the `ON CONFLICT` upsert, same as a brand-new PR. |

### Why this needs a real decision, not just a doc correction

- **The `deferred` case silently produces one review per push during a
  block period**, gated only by the shared `blocked_until`/`not_before`. If
  a PR gets pushed 5 times while a provider is blocked for an hour, all 5
  pushes will have coalesced into the single latest `head_sha` by the time
  the block lifts (good — no spam there), but if the provider clears
  briefly and re-blocks, multiple distinct reviews *can* fire for what was
  really one logical "author is iterating" burst. There's no cooldown /
  debounce concept at all.
- **The `running` case is the opposite problem**: a push is dropped, not
  spammed. A fast-follow commit (fixing a typo the review would have
  flagged) can go unreviewed until the *next unrelated* push happens to
  land — which may be much later, or never for a short-lived PR. Whether
  that's acceptable, or whether the running review should be cancelled and
  restarted against the new commit, or the ticket should self-re-enqueue
  once `done`, is an unmade design decision today.
- **No cooldown period exists anywhere.** Every re-review trigger is
  immediate and unconditional (once a ticket is in a re-triggerable state).
  For a PR under active, rapid iteration, this can mean a review fires on
  nearly every push — worth deciding if a minimum spacing between reviews
  for the same PR is wanted, independent of provider rate limits.

### Direction for the next planning session

- Decide whether "cancel and restart the running review" is in scope, or
  whether the current "finish then let a later push re-trigger" behavior is
  the accepted, *documented* design (as opposed to today's undocumented
  side effect of the CASE logic).
- Decide on an explicit per-PR cooldown/debounce policy — e.g., "don't
  re-review the same PR more than once per N seconds/minutes regardless of
  push frequency" — versus leaving it fully reactive to pushes as today.
- Decide what "removing triggers for re-review" would mean concretely: e.g.
  should a push while `deferred` update `head_sha` without resetting
  `not_before`/`status` (i.e., let the already-scheduled review just run
  once, on the latest commit, instead of resetting the clock)? That's a
  small, well-scoped behavior change once decided.
- Once decided, update SPEC.md's queue section to describe the *chosen*
  behavior precisely (matching Finding 1's need for the backoff policy to
  also be documented as designed, not incidental).

---

## Suggested prompt to continue planning

See the accompanying message in this conversation, or paste the following
into a fresh planning session:

> Brainstorm and then write an implementation plan for two follow-up
> improvements to the durable review queue feature on `feat/review-queue`
> (already merged/kept as its own branch). Read
> `docs/2026-07-28-dispatcher-followups.md` in full first — it has the
> complete context, current behavior, and open questions for both. Topics:
> (1) replace the dispatcher's flat 1-second backoff floor
> (`app/queue/dispatcher.py`'s `run_forever`) with a real guardrail —
> escalating/exponential backoff on consecutive failures, a policy for
> `mark_failed` tickets distinct from `RateLimited` deferrals, and whether
> `attempts` should ever trip a hard stop; (2) decide and implement a
> coherent re-review triggering policy — what should happen to a push that
> lands while a review is `running` (currently silently dropped until a
> later push), whether a per-PR cooldown/debounce is wanted, and whether a
> push during `deferred` should reset the wait clock or just ride out the
> existing one. Follow this repo's usual TDD/task-plan conventions
> (`docs/superpowers/plans/2026-07-27-review-queue.md` is a good reference
> for the expected plan format and rigor).
