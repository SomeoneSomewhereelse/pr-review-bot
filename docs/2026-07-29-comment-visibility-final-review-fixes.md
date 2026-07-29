# Final-review fixes — comment-visibility follow-ups

**Date:** 2026-07-29
**Branch:** `fix/comment-visibility` (kept as-is, not yet merged to `master`)
**Status:** Resolved — these were found and fixed within this same implementation session, not deferred.

## Context

The comment-visibility follow-ups plan (`docs/superpowers/plans/2026-07-29-comment-visibility-followups.md`)
implemented three findings from the prior dispatcher-followups final review
(preserve a good review under a rate-limit placeholder; preserve a good review
on terminal hard-stop, with a fixed double-failure ordering; an explicit
`BEGIN IMMEDIATE` transaction around `enqueue_or_update`). All 6 tasks were
implemented and individually reviewed clean. The plan's own final
whole-branch review then caught two real cross-task bugs — this doc records
what they were and how they were fixed (commit `e1a78bb`, on top of
`0e61dcf`).

## Fix 1 — bounded the notice-post retry loop

**The bug:** Finding 2's double-failure fix (post the terminal notice
*before* `mark_failed`; on a failed post, `defer_failed` and retry rather
than stranding the ticket) had no upper bound. If notice-posting failed for a
**persistent** reason — not a transient GitHub outage, but something that
will never clear on retry (the GitHub App uninstalled, the repo archived,
the PR locked, or the comment body exceeding GitHub's 65,536-char cap) — the
ticket could never reach terminal status again. `next_attempt` only grows,
so the `next_attempt >= dispatcher_max_failure_attempts` condition stayed
permanently true, and the ticket re-entered the same notice-post-failure
branch forever: a full 3-specialist review re-run every cycle, capped only
by the 300s backoff ceiling, for as long as the persistent condition held.

**The fix:** Added `dispatcher_max_notice_post_attempts: int = 3`
(`app/config.py`). Once `next_attempt` exceeds
`dispatcher_max_failure_attempts + dispatcher_max_notice_post_attempts`
(default 5 + 3 = 8), the dispatcher gives up trying to post the notice and
calls `store.mark_failed(...)` directly — accepting a lost notice in this
rare compound-failure case, which is strictly better than looping forever.
Verified: the transient case (notice-post fails once, succeeds later) is
unaffected — it still reaches terminal via the normal success path before
the new ceiling is ever evaluated.

## Fix 2 — footnote-strip regex could delete real review content

**The bug:** `append_review_footnote`'s footnote-strip logic
(`_FAIL_NOTE_RE`, a regex matching from the first `FAIL_NOTE_START` to the
next `FAIL_NOTE_END`) assumed at most one occurrence of the marker pair per
comment. But nothing prevents a specialist's finding text from containing
the literal marker string — and `app/github_app.py`'s own source now
contains it verbatim (`<!-- ai-review-fail-note -->`), so a self-review PR
touching this file, reviewed by a specialist that quotes the line, would
trigger exactly this. The regex would then match from that **stray**
marker all the way to the **real** trailing footnote's end marker, silently
deleting every real finding in between — the exact harm this whole feature
exists to prevent.

**The fix:** Replaced the regex with `_strip_existing_footnote(body)`, which
only strips a **well-formed trailing** footnote block: find the *last*
`FAIL_NOTE_START` via `rfind`, and only treat it as real if the body
(rstripped) actually `endswith(FAIL_NOTE_END)`. A stray, non-trailing marker
with no matching end is left untouched. Verified against all four cases (no
footnote; a real trailing footnote; a stray unterminated marker; a stray
marker followed by more real content followed by a genuine trailing
footnote) — only the last case's genuine block is ever removed.

## Fix 3 (minor) — stale module docstring

`app/queue/dispatcher.py`'s module docstring still said "any ticket that
can't run now gets a placeholder comment," which stopped being true once
Finding 1 (of this same plan) added the `_has_visible_review` suppression.
Corrected to note the exception.

## Verification

All three fixes plus their regression tests landed in one commit
(`e1a78bb`); a scoped re-review confirmed all three ADDRESSED with no new
breakage. Full suite: 136/136 passing, `ruff check .` clean.
