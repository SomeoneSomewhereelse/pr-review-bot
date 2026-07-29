# Follow-up findings — comment-visibility gaps in the dispatcher

**Date:** 2026-07-29
**Status:** Open — needs its own planning session (brainstorm + plan), not a quick patch
**Relates to:** `docs/superpowers/specs/2026-07-28-dispatcher-followups-design.md` (backoff +
re-review/cooldown design this extends), `app/queue/dispatcher.py`, `app/formatting.py`,
branch history: dispatcher-followups final-review fix commit `01951f8`.

## Context

The dispatcher-followups plan's final whole-branch review flagged six Minor findings.
Four were either fixed inline (the stale-flag bug's sibling doc corrections) or are
trivial doc-staleness (SPEC.md's module list, a design-doc overclaim about `attempts`
resetting). This doc captures the two that are genuine design gaps — both are about
the dispatcher silently **destroying a good, already-visible review comment** in favor
of a less useful one, in scenarios that are easy to reach in normal operation.

Both share a root cause: every comment write in `app/queue/dispatcher.py` targets the
same marker-based comment via `github_app.upsert_comment`, and nothing before that write
asks "is what's currently on the PR already better than what I'm about to post?"

---

## Finding 1 — the cooldown gate can replace a good review with a rate-limit placeholder

### What happens today

`process_next_due`'s pre-flight gate (`app/queue/dispatcher.py:82-92`) runs **before**
any distinction between "this is a brand-new review" and "this is a cooldown-driven
coalesced re-review of a PR that already has a perfectly good comment on it":

```python
provider = settings.llm_provider
blocked = _blocked_until.get(provider)
if blocked is not None and now < blocked:
    store.defer_rate_limited(ticket.id, not_before=blocked.isoformat(), now=now.isoformat())
    await _post_placeholder(
        ticket.repo_full_name, ticket.pr_number, (blocked - now).total_seconds(), now
    )
    return StepResult(action="deferred", ticket_id=ticket.id)
```

Trace the scenario the dispatcher-followups design intentionally created:

1. PR gets a clean, successful review. `finalize_review` sets `last_reviewed_at`.
2. Author pushes again while the PR is quiet (ticket now `done`). `enqueue_or_update`
   re-arms it — if still within `dispatcher_rereview_cooldown_seconds` (default 300s),
   status becomes `deferred` with `not_before = last_reviewed_at + cooldown`. Per
   design §6.4, this wait is meant to be **silent** — "the previous review comment
   stays visible; no placeholder churn."
3. The cooldown elapses; the ticket becomes claimable. But suppose the active
   provider is *also* rate-limited right now (an unrelated, coincidental fact about
   provider health, nothing to do with this PR).
4. The pre-flight gate above fires. It doesn't know or care that this ticket already
   has a good review comment sitting on the PR — it unconditionally posts
   `format_placeholder(...)`, **overwriting the good review** with "⏳ Queued behind
   rate limit — review will appear shortly."

The PR's author now sees a worse comment than before the cooldown-driven re-review
was ever scheduled, purely because of an unlucky timing coincidence between the
cooldown boundary and provider health. This directly contradicts the "silent cooldown"
promise in the design doc's §6.4 — the silence only holds *during* the cooldown wait
itself, not at the moment the cooldown hands off into a second, unrelated wait.

### Why it needs a real decision, not a one-line patch

- The gate has no way today to know "does this ticket already have a visible,
  still-accurate comment on the PR?" — that information isn't tracked (there's a
  `comment_id` column, unused; see the existing "deliberate simplification" note in
  SPEC.md §12).
- A naive fix ("don't post a placeholder if `last_reviewed_at` is set") is too broad —
  it would also suppress the placeholder for a ticket that's *never* been reviewed
  before but happens to have a stale `last_reviewed_at` from a much earlier, unrelated
  review cycle, or would need care around what counts as "still accurate" (a dozen
  pushes since the last review probably shouldn't count as "still good enough").
- This is the same class of question the original durable-queue design explicitly
  deferred (SPEC.md's "Future work" ping-comment section) — worth deciding together
  with any related comment-lifecycle work rather than patching in isolation.

---

## Finding 2 — the terminal failure comment overwrites a good prior review

### What happens today

`format_failure` (`app/formatting.py`) is marker-prefixed like every other comment
this bot posts, so it **upserts onto the same comment** as a real review result.
The terminal hard-stop path (`app/queue/dispatcher.py:96-107`):

```python
except Exception as exc:
    ...
    next_attempt = ticket.attempts + 1
    if next_attempt >= settings.dispatcher_max_failure_attempts:
        store.mark_failed(ticket.id, now=now.isoformat(), error=str(exc))
        await asyncio.to_thread(
            github_app.upsert_comment,
            ticket.repo_full_name,
            ticket.pr_number,
            format_failure(ticket.pr_number, next_attempt),
        )
        return StepResult(action="failed", ticket_id=ticket.id)
```

Scenario:

1. PR gets a clean, successful review — a genuinely useful comment is on the PR.
2. Author pushes again. The new review attempt hits `dispatcher_max_failure_attempts`
   (default 5) consecutive hard failures — e.g. a transient GitHub/provider outage,
   nothing to do with the PR's actual content.
3. The terminal comment **replaces** the good prior review with a bare
   "❌ Automated review could not be completed after 5 attempts... It will retry
   automatically on the next push." The original, still-valid findings are gone from
   the PR entirely, replaced by strictly less useful information.

This is worse than doing nothing: leaving the stale-but-real prior review up would
have given the author more signal than an empty failure notice.

Two smaller issues live in the same code path and are worth deciding alongside this:

- **Silent double-failure**: if `github_app.upsert_comment` itself raises inside this
  same block (line 101-106), the ticket is *already* `mark_failed` (correct — no
  strand), but the failure comment is never posted and nothing retries posting it.
  The "partial failure is always visible" guarantee quietly breaks in this one
  compound-failure case.
- **Grammar**: `format_failure`'s message reads "after 1 attempts" when
  `dispatcher_max_failure_attempts=1` (or generally, singular/plural isn't handled).
  Trivial, but cheap to fix alongside the real design work.

### Why it needs a real decision, not a one-line patch

- The fix isn't just "don't overwrite" — a failure notice needs to be visible
  *somewhere*, per the "partial failure is always visible" project-wide rule. The
  question is whether that's a new comment (GitHub doesn't notify on comment
  *edits* — see SPEC.md's existing "Future work" ping-comment section, which already
  flags this exact tension for a different reason), an appended section within the
  existing comment (requires knowing the old comment's content, which isn't stored),
  or something else.
- This overlaps directly with Finding 1's root cause (no tracked notion of "is the
  currently-visible comment worth preserving") — worth designing together rather than
  as two unrelated patches that could conflict.

---

## Finding 3 — `enqueue_or_update`'s SELECT-then-UPDATE is a latent race, guarded only by an invariant nobody enforces

### What happens today

`app/queue/store.py`'s `enqueue_or_update` reads the ticket's current row via a
`SELECT`, branches on its `status` in Python, then writes via a separate `UPDATE`
keyed by `id` — not one atomic statement. This is already flagged as a code comment
(`app/queue/store.py:96-100`, added during the dispatcher-followups final-review fix
round):

```python
# SELECT-then-UPDATE (not one atomic statement) is safe only because this
# always runs synchronously on the single-threaded event loop with no
# `await` in between (called directly from webhook.py, never wrapped in
# asyncio.to_thread). If ever moved off the loop, this becomes a real race
# against claim_next_due/finalize_review and needs an explicit transaction.
```

It is genuinely safe **today**: `enqueue_or_update` is called synchronously (no
`await`, no `asyncio.to_thread`) from `webhook.py`, so on the single-threaded asyncio
event loop nothing can interleave between the `SELECT` and the `UPDATE` — the whole
function body runs as one uninterrupted turn.

### Why it needs a real decision, not a one-line patch

The comment documents an invariant, but nothing *enforces* it — a future change that
wraps this call in `asyncio.to_thread` (a very plausible "optimization" someone might
make later, since the codebase already does exactly that for other blocking calls —
see `app/orchestrator.py`'s GitHub API calls and `_post_placeholder`) would silently
reintroduce a real race: a webhook-triggered push could interleave between this
function's `SELECT` and `UPDATE` and the dispatcher's `claim_next_due`/`finalize_review`,
in ways that could lose a push (the read-stale-status branch taken, then overwritten by
a concurrent claim) or corrupt the `rereview_requested`/`attempts` bookkeeping the
dispatcher-followups plan just built. This needs the same kind of design attention as
Findings 1 and 2 — likely either an explicit SQLite transaction (`BEGIN IMMEDIATE`) around
the read-then-write, or a single atomic UPDATE with the branching expressed in SQL (the
way `finalize_review` and `claim_next_due` already do it) — decided deliberately rather
than discovered as a regression after someone moves the call off the event loop.

---

## Suggested prompt to continue planning

> Brainstorm and then write an implementation plan for two comment-visibility
> follow-ups to the durable review queue's dispatcher, both found during the
> dispatcher-followups plan's final review, on branch `master` (already merged).
> Read `docs/2026-07-29-comment-visibility-followups.md` in full first — it has
> the complete context, current behavior, and root cause for both. Topics:
> (1) the pre-flight `blocked_until` gate in `app/queue/dispatcher.py`'s
> `process_next_due` can replace an already-good, already-visible review comment
> with a rate-limit placeholder when a cooldown-driven coalesced re-review becomes
> due at the same moment the active provider happens to be rate-limited — decide
> whether/how the dispatcher should recognize "this PR already has a good comment
> that a placeholder would needlessly downgrade" before posting one; (2) the
> terminal hard-stop path's `format_failure` comment unconditionally overwrites
> the marker-based comment, destroying a prior good review if the PR was
> previously reviewed successfully and a later re-review hard-fails — decide
> whether the failure notice should append rather than replace, post as a
> separate comment (noting GitHub's no-notify-on-edit constraint already flagged
> in `SPEC.md`'s "Future work" section), or something else, and fix the related
> silent-double-failure and "1 attempts" grammar issues found alongside it. Both
> findings share a root cause (no tracked notion of whether the current comment
> is worth preserving) — design them together, not as two unrelated patches. (3)
> Separately (an unrelated concern — scope it as its own plan section rather than
> folding it into 1/2's design): `app/queue/store.py`'s `enqueue_or_update` does a
> SELECT then a separate UPDATE rather than one atomic statement — safe only because
> it always runs synchronously on the single-threaded event loop with no `await` in
> between (documented as a code comment at `app/queue/store.py:96-100`). Decide
> whether to make this robust against a future change that wraps the call in
> `asyncio.to_thread` (as the codebase already does for other blocking calls) — e.g.
> an explicit SQLite transaction (`BEGIN IMMEDIATE`) or rewriting the branch as one
> atomic UPDATE the way `finalize_review`/`claim_next_due` already do it — before
> it's discovered as a real race regression rather than decided deliberately now.
> Follow this repo's usual TDD/task-plan conventions
> (`docs/superpowers/plans/2026-07-28-dispatcher-followups.md` is a good
> reference for the expected plan format and rigor).
