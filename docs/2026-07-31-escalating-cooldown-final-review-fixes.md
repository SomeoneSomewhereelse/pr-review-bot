# Escalating re-review cooldown — implementation record + final-review findings

**Date:** 2026-07-31
**Status:** Implemented and merged to `master` (commit `e5ec149`, fast-forwarded from `fix/comment-visibility`)
**Relates to:** `docs/2026-07-29-cooldown-review-invocation-followup.md` (the
finding), `docs/superpowers/specs/2026-07-31-escalating-cooldown-design.md`
(the design), `docs/superpowers/plans/2026-07-31-escalating-cooldown.md`
(the task-by-task plan), `docs/2026-07-29-comment-visibility-final-review-fixes.md`
(fix 1 — the failure-path ceiling this feature mirrors but deliberately does
not copy).

## What changed

Implemented via subagent-driven development, one fresh implementer + one
independent task review per task, in order:

| Task | Change | Commit |
|---|---|---|
| 1 | `dispatcher_rereview_cooldown_max_seconds` config (default 3600s) + pure helpers `effective_cooldown(level)` / `next_cooldown_level(level)` / `_MAX_COOLDOWN_LEVEL` in `app/queue/store.py` | `8e4a8ef` |
| 2 | `cooldown_level INTEGER NOT NULL DEFAULT 0` column, migration, `Ticket.cooldown_level` field | `72b1579` |
| 3 | Site A — `_due_after_cooldown` refactored to `(last_reviewed_at, now, level) -> (status, not_before, new_level)`; `enqueue_or_update`'s done/failed re-arm branch escalates on churn, resets to 0 on quiet | `9525196` |
| 4 | Site B — `finalize_review` gains a required `rereview_cooldown_level` param + `CASE` line (dirty branch stores it, non-dirty branch leaves the column unchanged); dispatcher computes both escalated values from the already-in-memory `Ticket.cooldown_level` (no extra DB read) | `d9ec5a4` |
| 5 | `SPEC.md` §12 documents the mechanism + the new config var | `0598b83` |

Net effect: a PR pushed faster than its cooldown, indefinitely, is now
throttled from **300s per re-review, forever** down to **a plateau of 3600s
per re-review after ~4 escalations** (~288 → ~26 reviews/day at defaults) —
without ever abandoning the PR. A PR that goes quiet for one full window
resets to the base cooldown automatically. Normal (non-churning) PRs are
provably unaffected: `effective_cooldown(0) == dispatcher_rereview_cooldown_seconds`
by construction, and the pre-existing 300s-flat-cooldown tests were left
untouched and still pass.

Each task's diff was individually reviewed clean (task reviewers found only
cosmetic, non-blocking Minor issues — see below). Full suite: 145/145
passing, `ruff check .` clean, at every task boundary and after the final
fix wave.

## Final whole-branch review

A broad review (Opus) covering the full `ae732bb..0598b83` range — looking
specifically for cross-task integration problems no single task review
could see — found **zero Critical or Important issues**. It confirmed, by
reading the merged code directly rather than trusting any task's report:

- Site A and Site B are mutually consistent (same helpers, same
  escalate-on-churn/reset-or-leave-latent-on-quiet semantics); no path
  double-escalates a single push.
- The Finding-3 deadlock invariant holds — `_due_after_cooldown`,
  `effective_cooldown`, `next_cooldown_level` are all connection-free;
  `finalize_review` is still one atomic `UPDATE`.
- The dispatcher's no-extra-read approach (`ticket.cooldown_level` off the
  claim result) is race-free because the level is stable while `running`.
- Escalation is silent (zero new comment/notice-posting code) and never
  abandons a PR (worst case is a 1h throttle, never a terminal give-up).
- Level-0 equivalence is real (not just asserted): `min(base·2⁰, cap) ==
  base`, and the pre-existing 300s regression tests are untouched and pass.

It did find 7 Minor findings. Two were flagged as effectively
merge-blocking because they guard binding invariants; the other five were
graded as optional polish. **Fixed** (commit `e5ec149`, scoped re-review
confirmed both addressed with no new breakage):

1. **`cap < base` misconfiguration guard.** `effective_cooldown` returned
   `min(base·2^level, cap)`, so if an operator ever set
   `dispatcher_rereview_cooldown_max_seconds` below
   `dispatcher_rereview_cooldown_seconds`, level 0 would silently return the
   smaller cap instead of the base — shortening the cooldown for every
   normal PR under a bad config (not reachable with shipped defaults, but a
   real footgun). Fixed to `max(base, min(base·2^level, cap))`, with a new
   test asserting `effective_cooldown(0) == base` even when `cap < base`.
2. **Silence invariant not asserted in the new dispatcher test.**
   `test_dispatcher_escalates_cooldown_on_churn_completion` stubbed comment
   posting but discarded the returned list. "Escalation posts nothing" is a
   binding invariant (§3.3 of the design) — the test now captures `posted`
   and asserts `posted == []`.

## Parked (non-blocking) minor findings

Recorded here for visibility; none block merge, none are correctness bugs
on any reachable default-config path.

- `effective_cooldown`'s docstring first line doesn't show the
  `min(level, _MAX_COOLDOWN_LEVEL)` clamping inline (`app/queue/store.py`)
  — cosmetic; the brief's own docstring template omitted it too.
- `mark_failed`'s docstring may still narrate the old flat-cooldown re-arm
  behavior without mentioning the level escalate/reset
  (`app/queue/store.py`) — pre-existing, not introduced by this work, out
  of scope.
- The Site-B non-dirty ("latent level") branch is tested only at level
  `0 -> 0`; a nonzero seeded level (e.g. level `3` surviving a non-dirty
  finalize) isn't directly exercised, though the `CASE`'s `ELSE
  cooldown_level` clause is otherwise a one-line, low-risk read.
- Design §5's explicit "sustained churn 300→600→1200→2400→3600→3600,
  end-to-end through the store" sequence has no single composed test — the
  plateau and each step are covered piecewise (one unit test for the pure
  formula, one integration test per site), but nothing walks a ticket
  through the full ramp as one scenario. This is the one test case listed
  in the design's testing section with no direct 1:1 implementation.
- `app/queue/store.py`'s `_due_after_cooldown` docstring line is 108 chars,
  over the plan's stated 100-char guideline (ruff's default `select`
  doesn't flag `E501`, and a similarly-long line already exists elsewhere
  in the file, so this doesn't fail CI).
- `enqueue_or_update`'s docstring doesn't mention the level escalate/reset
  behavior in its done/failed-branch description — the line most likely to
  be read by the next person touching Site A.

## Unrelated discovery: working-tree CRLF drift

While staging Task 1's commit, the controller found that `git add
<exact-task-files>` still swept in a large, pre-existing, **uncommitted**
CRLF conversion of those same files (the repo's committed history was
already pure LF; the *working tree* on this WSL/Windows mount had drifted
to CRLF before this session started). Fixed per-task by normalizing back to
LF before each commit landed, keeping every task's diff scoped to its real
change.

After the branch merged, a full project-wide audit (`git ls-files` +
`file`, plus an untracked-file check) found the same drift on **59 tracked
files** — matching the original `git status` dirty-file list from the very
start of this work. Normalizing all 59 back to LF produced a **byte-exact
match with the already-LF committed `HEAD`** — confirming the CRLF was
purely a working-tree/checkout artifact, never actually committed to git
history. No content commit was needed. Added `.gitattributes`
(`* text=auto eol=lf`, commit `802a9b8`) so this can't silently recur on
future checkouts. `.venv/` also has CRLF files but is gitignored
third-party dependency code, out of scope.
