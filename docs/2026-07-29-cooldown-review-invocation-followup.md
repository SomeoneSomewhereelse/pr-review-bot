# Follow-up finding — no cap on cooldown-driven review invocation

**Date:** 2026-07-29
**Status:** Open — needs its own planning session (brainstorm + plan), not a quick patch
**Relates to:** `docs/superpowers/specs/2026-07-29-comment-visibility-followups-design.md`
(the per-PR cooldown this concerns), `docs/2026-07-29-comment-visibility-final-review-fixes.md`
(the parallel fix on the *failure* path that this finding mirrors on the
*success* path), branch `fix/comment-visibility` (kept as-is, not yet merged).

## Context

The comment-visibility follow-ups' final review caught and fixed an
unbounded retry loop on the **hard-failure** path: if the terminal notice
kept failing to post for a persistent reason, the ticket could retry
forever. The fix added `dispatcher_max_notice_post_attempts`, giving that
path a hard ceiling (`dispatcher_max_failure_attempts +
dispatcher_max_notice_post_attempts`) before giving up and going terminal.

The **success/cooldown** path has no equivalent ceiling, and was never
designed to have one — worth deciding deliberately rather than leaving as
an accidental asymmetry.

## The mechanism

- `store.enqueue_or_update`'s `done`/`failed` branch re-arms a ticket on
  every fresh push, honoring `_due_after_cooldown`: if still within
  `dispatcher_rereview_cooldown_seconds` (default 300s) of the ticket's
  `last_reviewed_at`, it re-arms to `deferred` at that boundary; once the
  boundary passes, `pending`.
- `finalize_review` records a new `last_reviewed_at` on every successful
  completion — so this is a **rolling** window, not a fixed total: each
  completed review resets the clock for the *next* one.
- Nothing anywhere counts or caps the number of times this can repeat for a
  single PR. Confirmed by reading `app/queue/store.py` in full — there is
  no per-PR review counter, no daily/session cap, no escalating cooldown for
  rapid repeat re-reviews (unlike the hard-failure path's *exponential*
  backoff).

## Why this is a real (if lower-severity) risk

If a PR receives pushes faster than the cooldown period, indefinitely — a
CI bot amending commits every few minutes, an auto-formatter auto-fixing and
re-pushing on every push, or simply a very actively-iterated PR — the
dispatcher will keep re-reviewing it, one full 3-specialist LLM fan-out
every `dispatcher_rereview_cooldown_seconds`, **forever**, for as long as
the push cadence keeps up. This is "working as designed" in the sense that
each individual re-review is legitimate (a real push happened, coalesced to
at most one review per cooldown window) — but there is no upper bound on
how long or how many times this can repeat for one PR, which is exactly the
same class of unbounded-cost risk the hard-failure-path fix (see the
final-review-fixes doc) just closed on the *other* branch of this same
dispatcher.

This is lower severity than the failure-path bug: it requires sustained,
repeated legitimate activity (not a single latent condition), and each
review is individually useful (the PR really did change). But the *absence*
of any cap is an asymmetry worth deciding on purpose, not by omission —
especially since this project's `cost.md` documents a specific target
budget, and an actively-iterated PR under CI churn could quietly consume a
large share of it.

## Why it needs a real decision, not a one-line patch

Unlike the failure-path fix (a straightforward "give up after N attempts"),
there's no equally simple analog here, because giving up isn't obviously
right — a PR that's still being actively worked on legitimately *should*
keep getting reviewed. Candidate directions, none obviously correct without
a design pass:

- **A per-PR daily/session review cap** (e.g. "at most N automated reviews
  per PR per rolling 24h") — needs a decision on what "per PR" means across
  a PR's whole lifetime, how the cap resets (rolling window vs. calendar
  day), and what happens once it's hit (silently stop, post a "review
  cadence capped" notice, or something else — mirroring the visibility
  question the comment-visibility plan just answered on the failure side).
- **Escalating cooldown for rapid repeat re-reviews** — e.g. each
  consecutive re-review within some short window doubles the next cooldown
  (mirroring `compute_backoff`'s shape, but keyed on review *frequency*
  rather than *failure* count) — self-limits a hot PR without an absolute
  cap, but changes the "silent, no placeholder churn" UX the cooldown
  design deliberately chose (§6.4 of the dispatcher-followups design).
- **Leave it uncapped, but track and surface it** — e.g. log/metric review
  count per PR so cost is visible even if not bounded, deferring the "is
  this actually a problem in practice" question to observed data rather
  than a preemptive cap.

## Suggested prompt to continue planning

> Brainstorm and then write an implementation plan for one follow-up to the
> durable review queue's dispatcher, found while fixing an unrelated bug on
> branch `fix/comment-visibility` (kept as-is, not yet merged to `master`).
> Read `docs/2026-07-29-cooldown-review-invocation-followup.md` in full
> first — it has the complete context, the mechanism, and why this mirrors
> (but isn't identical to) a bug just fixed on the failure path
> (`docs/2026-07-29-comment-visibility-final-review-fixes.md`, fix 1).
> Topic: the per-PR cooldown re-arm path in
> `app/queue/store.py`'s `enqueue_or_update` (the `done`/`failed` branch)
> has no cap on how many times a single PR can be re-reviewed — a PR pushed
> faster than `dispatcher_rereview_cooldown_seconds` (default 300s),
> indefinitely, triggers a full review every cooldown window, forever,
> unlike the hard-failure path which now has an explicit ceiling
> (`dispatcher_max_failure_attempts` + `dispatcher_max_notice_post_attempts`).
> Decide whether and how to bound this: a per-PR review cap (and what
> "per PR" and "cap reached" should mean/do), an escalating cooldown for
> rapid repeat re-reviews, or a deliberate decision to leave it uncapped but
> observable — this needs a real design choice, not a mechanical copy of
> the failure-path fix, since (unlike a persistent failure) each individual
> re-review here is legitimate work responding to a real push. Follow this
> repo's usual TDD/task-plan conventions
> (`docs/superpowers/plans/2026-07-29-comment-visibility-followups.md` is a
> good reference for the expected plan format and rigor).
