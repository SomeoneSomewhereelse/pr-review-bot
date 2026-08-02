# Design — Re-review-notice parked-minors cleanup

**Date:** 2026-08-02
**Status:** Approved for planning
**Relates to:** `docs/superpowers/specs/2026-08-01-re-review-notice-design.md`
(the feature these minors were parked against, now merged to `master`).
Branch: `master`.

## 1. Problem

The re-review-notice feature's final whole-branch review parked six Minor
findings as non-blocking. Of those, two (`append_schedule_notice` duplicating
`append_review_footnote`'s body; a deleted bot comment yielding a
schedule-note-only comment) are direct, deliberate consequences of binding
constraints from that feature's own plan and are not worth revisiting. The
other four are cheap, safe, and worth fixing now:

1. `tickets_needing_notice` has no `LIMIT` — a mass re-arm (many PRs pushed
   at once) would post every matching ticket's notice serially inside one
   dispatcher tick, each a multi-round-trip GitHub call, stalling real
   review throughput (`process_next_due`) for the duration.
2. The `SCHEDULE_NOTE_START` comment in `app/github_app.py` claims the two
   footnote kinds are mutually exclusive "by construction" — they aren't;
   it's `_strip_existing_footnote`'s generalized stripping that actually
   guarantees this (a ticket that hits the failure ceiling and is then
   pushed can briefly carry both footnote kinds on GitHub at once).
3. `format_schedule_notice` calls `not_before.strftime("%H:%M UTC")` without
   first normalizing to UTC — correct today since every current caller
   already passes a UTC-aware datetime, but not self-enforcing against a
   future caller that doesn't.
4. The design's own enumerated testing requirement — "a subsequent full
   review completion (`upsert_comment`'s full-body overwrite) removes the
   schedule footnote" — was never actually written as a test.

## 2. Decision

Fix all four. Each is independent and small enough to bundle into one
short plan rather than four separate ones.

| # | Change | File(s) |
|---|---|---|
| 1 | New config `dispatcher_notice_sweep_batch_size: int = 20`; `tickets_needing_notice` gains `LIMIT ?` bound to it | `app/config.py`, `app/queue/store.py` |
| 2 | Reword the `SCHEDULE_NOTE_START` comment to credit `_strip_existing_footnote`, not "construction" | `app/github_app.py` |
| 3 | `format_schedule_notice` normalizes via `.astimezone(timezone.utc)` before `strftime` | `app/formatting.py` |
| 4 | Add the missing self-cleaning test | `tests/test_github_app.py` |

### Why a batch cap, not a policy decision to leave it unbounded

This project's own architecture doc (`CLAUDE.md`) already documents
demo-scale, single-dispatcher operation as a deliberate constraint — but
the *sweep* specifically differs from the rest of the dispatcher in one
way worth bounding regardless of scale: `process_next_due` only ever does
one ticket's worth of GitHub work per tick by construction (`claim_next_due`
claims exactly one row), while `post_pending_notices` iterates an
unbounded result set in a single tick. A cap keeps that asymmetry from
ever mattering, at near-zero cost (a `LIMIT` clause) and with no
downside: an ordinary tick (fewer than the batch size) behaves
identically to today.

### Why a new config setting, not a hardcoded constant

Every other dispatcher tunable (`dispatcher_idle_sleep_seconds`,
`dispatcher_max_failure_attempts`, `dispatcher_rereview_cooldown_seconds`,
etc.) is exposed via `app/config.py`'s `Settings`, not a module constant.
`dispatcher_notice_sweep_batch_size` follows that established pattern —
consistent, and adjustable without a code change or redeploy.

### Why leftover tickets self-heal without extra bookkeeping

A ticket that doesn't fit in one sweep's batch is simply not touched this
tick — its `notice_not_before` marker stays stale (unchanged), so it still
matches `tickets_needing_notice`'s query on the very next tick. No new
state, no "remembered position," no risk of starving a ticket
permanently: the `ORDER BY enqueued_at ASC, id ASC` already used by this
query means the same oldest-first prioritization `claim_next_due` uses
elsewhere in this file, so a consistently-overflowing batch would still
cycle through every waiting ticket over successive ticks rather than
always serving the same prefix.

## 3. Mechanism

### 3.1 Config (`app/config.py`)

```python
dispatcher_notice_sweep_batch_size: int = 20
```

Added alongside the other `dispatcher_*` settings.

### 3.2 `tickets_needing_notice` (`app/queue/store.py`)

Add `LIMIT ?` (bound to `settings.dispatcher_notice_sweep_batch_size`)
after the existing `ORDER BY enqueued_at ASC, id ASC`. No other change to
the function's predicates or signature.

### 3.3 `SCHEDULE_NOTE_START` comment (`app/github_app.py`)

Replace "Mutually exclusive with FAIL_NOTE_* by construction (a ticket is
never both mid-failure-retry and in a completed cooldown/rate-limit wait)"
with wording crediting `_strip_existing_footnote`'s generalized stripping
as what actually enforces this — not the ticket state machine alone.

### 3.4 `format_schedule_notice` (`app/formatting.py`)

```python
eta = not_before.astimezone(timezone.utc).strftime("%H:%M UTC")
```

(was `not_before.strftime("%H:%M UTC")`). Requires `not_before` remain a
timezone-aware `datetime` (unchanged requirement — a naive datetime still
raises, now explicitly via `astimezone`'s own error rather than silently
mislabeling a non-UTC offset as "UTC").

### 3.5 Self-cleaning test (`tests/test_github_app.py`)

New test mirroring the existing failure-footnote self-cleaning test's
shape: post a schedule note via `append_schedule_notice`, then call
`upsert_comment` (the real review-completion path) against the same
comment, and assert the schedule note is gone from the resulting body —
proving the full-body overwrite this test's docstring already documents
as the mechanism.

## 4. Testing

- **Batch cap:** seed more than `dispatcher_notice_sweep_batch_size`
  matching tickets; assert `post_pending_notices` posts exactly the cap's
  worth in one call, and that a second call (simulating the next tick)
  picks up the remainder.
- **UTC normalization:** `format_schedule_notice` called with a non-UTC
  but timezone-aware datetime (e.g. `timezone(timedelta(hours=5))`)
  renders the equivalent UTC clock time, not the offset's local time.
- **Self-cleaning:** covered by the new test in 3.5.
- No existing test changes behavior — all four items are additive or
  cosmetic (a query gains a bound `LIMIT`; a comment is reworded; a
  formatting call gains a normalization step that is a no-op for every
  existing caller, which already passes UTC-aware values).

## 5. Non-goals

- Not revisiting the two parked minors deliberately left alone
  (`append_schedule_notice`/`append_review_footnote` duplication; the
  deleted-bot-comment edge case) — both are consequences of binding
  constraints from the original feature's plan, not defects.
- No change to the sweep's cadence, the escalating-cooldown math, or any
  other part of the dispatcher not named above.
