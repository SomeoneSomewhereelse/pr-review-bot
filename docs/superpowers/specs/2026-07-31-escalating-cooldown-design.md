# Design — Escalating re-review cooldown (bound cooldown-driven invocation)

**Date:** 2026-07-31
**Status:** Approved for planning
**Relates to:** `docs/2026-07-29-cooldown-review-invocation-followup.md` (the
finding), `docs/superpowers/specs/2026-07-29-comment-visibility-followups-design.md`
(the cooldown + §6.4 "silent" UX this preserves), `docs/2026-07-29-comment-visibility-final-review-fixes.md`
(fix 1 — the *failure*-path ceiling this mirrors but deliberately does not copy),
branch `fix/comment-visibility` (this builds on it; still unmerged).

## 1. Problem

The per-PR cooldown re-arm path (`store.enqueue_or_update`'s `done`/`failed`
branch, plus `finalize_review`'s dirty-flag branch) has **no bound** on how many
times a single PR can be re-reviewed. A PR pushed faster than
`dispatcher_rereview_cooldown_seconds` (default 300s), indefinitely, gets one
full 3-specialist review every cooldown window, **forever** — a sustained drip,
not a burst (pushes correctly coalesce via the dirty flag and the deferred
ride-out, so it is at most one review per window). On the constrained provider
this is a real harm: GitHub Models' ~150 req/day means one churning PR
(~288 reviews/day = ~864 calls at 300s) exhausts the entire daily quota in
~17 reviews (~1.5h), starving every other PR until reset.

Unlike the failure path (fix 1's "give up after N attempts"), **giving up is
wrong here** — each re-review is legitimate work responding to a real push. The
bound must throttle a churning PR without ever abandoning it.

## 2. Decision

**Escalating cooldown.** Keep the short base cooldown so normal PRs re-review
promptly; lengthen the cooldown geometrically for a PR that keeps churning,
capped at 1h, resetting when the PR goes quiet. Chosen over a hard per-PR cap
(abandons legitimate pushes when hit) and over observe-only (leaves the
quota-starvation risk unbounded).

| Decision | Choice |
|---|---|
| Mechanism | Per-PR `cooldown_level`; `effective_cooldown(level) = min(base·2^level, cap)` |
| Base | `dispatcher_rereview_cooldown_seconds` = 300s (unchanged) |
| Cap | `dispatcher_rereview_cooldown_max_seconds` = **3600s** (new) |
| Grace | **None** — escalate from the 2nd consecutive rapid re-review |
| UX | **Silent** — escalation only lengthens `not_before`; no placeholder/notice (preserves §6.4) |
| Abandonment | **Never** — a churning PR is throttled, not stopped |

## 3. Mechanism

### 3.1 State + formula

- New column `cooldown_level INTEGER NOT NULL DEFAULT 0` on `tickets` (added to
  `_SCHEMA`, backfilled by `_ensure_columns`, added to the `Ticket` dataclass).
- `store.effective_cooldown(level) -> float = min(base · 2^min(level, _MAX_COOLDOWN_LEVEL), cap)`
  — `base = settings.dispatcher_rereview_cooldown_seconds`,
  `cap = settings.dispatcher_rereview_cooldown_max_seconds`. Level 0 → 300s
  (today's behavior), 1 → 600, 2 → 1200, 3 → 2400, 4 → 3600 (capped), …
- `store.next_cooldown_level(level) -> int = min(level + 1, _MAX_COOLDOWN_LEVEL)`.
- `_MAX_COOLDOWN_LEVEL = 30` — a guard so `2^level` cannot blow up if a PR
  churns for an extraordinarily long time; `effective_cooldown` plateaus at
  `cap` far below this (level 4 for the default 300/3600), so the constant is
  purely defensive.

### 3.2 Escalate / reset — one level change per *scheduled re-review*

Level changes only when a re-review is actually **scheduled**, never per push
(multiple pushes in a window coalesce into one re-review = one level change):

| Event | `not_before` | `cooldown_level` |
|---|---|---|
| **Site A** — push to `done`/`failed`, still **within** `effective_cooldown(level)` of `last_reviewed_at` (churn) | `last_reviewed_at + effective_cooldown(level)` | `next_cooldown_level(level)` |
| **Site A** — push to `done`/`failed`, cooldown **elapsed** or never reviewed (quiet) | `NULL` → `pending` now | **reset to 0** |
| **Site B** — `finalize_review` with the dirty flag set (push landed mid-run = churn) | `now + effective_cooldown(level)` | `next_cooldown_level(level)` |
| **Site B** — `finalize_review`, no dirty flag | `NULL` → `done` | unchanged (latent; Site A resolves it on the next push) |
| new ticket / `pending` / `deferred` ride-out / `running` dirty-set / `claim` / `recover` | (unchanged from today) | unchanged |

**Consequences:**
- The **first** re-review of any PR is `effective_cooldown(0)` = 300s — identical
  to today. Escalation only compounds for a PR pushed again *inside* each
  (growing) window.
- Once a PR stays quiet for one full effective window, the next push hits Site
  A's "elapsed" case → `pending` + **level reset to 0**. Escalation is fully
  self-healing.
- A `done`-with-no-dirty completion leaves the level **latent** (unchanged), so
  a push arriving shortly after (within the window) still counts as churn and
  escalates via Site A; a push arriving after the window resets it. This is why
  the level is not reset in Site B's non-dirty branch.

### 3.3 Silent — preserves §6.4

Escalation changes only the computed `not_before`. No placeholder, no notice, no
comment mutation. The prior good review stays visible throughout (and the
comment-visibility work already prevents a coincidental rate-limit placeholder
from clobbering it). Nothing new ever appears on the PR because of escalation.

### 3.4 Atomicity preserved

`cooldown_level` is **stable while a ticket is `running`**: a push to a running
ticket takes `enqueue_or_update`'s `running` branch, which sets only
`rereview_requested` + `head_sha`, never the level. So the dispatcher can read
`ticket.cooldown_level` (from the `claim_next_due` result) and precompute both
escalated values with no extra read and no race:

```python
level = ticket.cooldown_level
rereview_not_before = (now + timedelta(seconds=store.effective_cooldown(level))).isoformat()
store.finalize_review(
    ticket.id, now=now.isoformat(),
    rereview_not_before=rereview_not_before,
    rereview_cooldown_level=store.next_cooldown_level(level),
)
```

`finalize_review` keeps its **single atomic UPDATE** — it gains one param and one
`CASE` line (`cooldown_level = CASE WHEN rereview_requested = 1 THEN :new_level
ELSE cooldown_level END`); the dirty flag is still resolved atomically in SQL, so
the mid-run-push race the single-UPDATE design guards against stays guarded.

Site A lives entirely inside `enqueue_or_update`'s existing `BEGIN IMMEDIATE`
transaction. `_due_after_cooldown` is refactored to take the current `level` and
return `(status, not_before, new_level)`; `effective_cooldown` /
`next_cooldown_level` open no connection, so the Finding-3 deadlock invariants
(no second connection / no other store fn inside the transaction) still hold.

## 4. Surface

- `app/config.py` — add `dispatcher_rereview_cooldown_max_seconds: float = 3600.0`.
- `app/queue/store.py` — `cooldown_level` column + migration + `Ticket` field;
  `effective_cooldown` / `next_cooldown_level` helpers + `_MAX_COOLDOWN_LEVEL`;
  refactor `_due_after_cooldown` to `(last_reviewed_at, now, level) ->
  (status, not_before, new_level)`; Site-A branch writes the new level;
  `finalize_review` gains `rereview_cooldown_level` + the `CASE` line.
- `app/queue/dispatcher.py` — compute `rereview_not_before` via
  `effective_cooldown(ticket.cooldown_level)` and pass
  `rereview_cooldown_level`; no other behavior change.
- `SPEC.md` §12 — document the escalating cooldown + the new config var.

## 5. Testing (deterministic-first, injected clock)

- **`effective_cooldown` / `next_cooldown_level` (pure):** level 0 → base;
  doubling per level; capped at `cap`; `_MAX_COOLDOWN_LEVEL` guards `2^level`.
- **Store schema/migration:** `cooldown_level` present on a fresh DB and
  backfilled (default 0) on a pre-existing table.
- **`_due_after_cooldown`:** never-reviewed → `(pending, None, 0)`; within
  window → `(deferred, due, level+1)`; elapsed → `(pending, None, 0)`.
- **Site A (`enqueue_or_update` done/failed):** push within window escalates the
  stored level + defers at `last_reviewed_at + effective_cooldown(level)`; push
  after the window re-arms `pending` and **resets level to 0**.
- **Site B (`finalize_review`):** dirty flag → stores `deferred` at the passed
  `rereview_not_before` with the passed `rereview_cooldown_level`; no dirty flag
  → `done`, level unchanged.
- **Dispatcher:** on completion with a mid-run push, `finalize_review` is called
  with `effective_cooldown(ticket.cooldown_level)`-based `not_before` and
  `next_cooldown_level`.
- **Regression:** a single, isolated re-review of an otherwise-quiet PR stays at
  300s / level 0 (normal PRs are unaffected).
- **Sustained churn sequence:** repeated within-window re-reviews escalate
  300 → 600 → 1200 → 2400 → 3600 → 3600 (plateau at cap).

## 6. Non-goals

- No per-PR hard cap / no "give up" — the whole point of escalation is that each
  re-review stays legitimate; a churning PR is throttled, never abandoned.
- No grace window (the first re-review is already prompt at base; escalation is
  gentle).
- No new UX / notice / metric surface — escalation is silent by design.
- No change to the failure-path ceiling, the rate-limit gate, the dirty-flag
  coalescing, or the ride-out behavior.
