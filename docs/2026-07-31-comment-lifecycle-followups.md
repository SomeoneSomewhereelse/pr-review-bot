# Follow-up findings — comment-lifecycle hardening (identity + re-review notice)

**Date:** 2026-07-31
**Status:** Open — needs its own planning session (brainstorm + plan), not a quick patch
**Relates to:** `app/github_app.py` (`COMMENT_MARKER`, `upsert_comment`,
`append_review_footnote`), `app/queue/store.py` (`comment_id` column,
`finalize_review`), `docs/superpowers/specs/2026-07-29-comment-visibility-followups-design.md`
(§6.4 "silent cooldown"), `docs/superpowers/specs/2026-07-31-escalating-cooldown-design.md`
(§3.3 "silent", and the longer waits that motivate Finding 2),
`docs/2026-07-29-comment-visibility-final-review-fixes.md` (fix 2 — the same
marker-substring bug-class, on the footnote-strip path). Branch
`fix/comment-visibility` (unmerged).

## Context

Two issues raised while reviewing the escalating-cooldown design. Both are
about **the bot's own PR comment**: how to reliably identify it, and what to
show in it while a re-review is pending. They share a domain (`github_app` +
the `comment_id`/footnote machinery) and are largely orthogonal to the
escalating-cooldown *cost* logic, so they were deliberately kept out of that
plan and grouped here for their own session. The escalating-cooldown mechanism
is unaffected; building it silent-first and layering Finding 2's notice later
needs no rework.

---

## Finding 1 — comment identity by marker substring is fragile

### What happens today

`app/github_app.py` identifies "the bot's comment" to edit on a re-review by a
**substring match** on a hidden marker over *every* comment in the PR thread:

```python
COMMENT_MARKER = "<!-- ai-code-review-bot -->"
...
for comment in pr.get_issue_comments():
    if COMMENT_MARKER in comment.body:
        comment.edit(marked_body)
        return comment
return pr.create_issue_comment(marked_body)
```

### Why it's fragile

The marker can legitimately appear in a comment the bot does **not** own:

- A PR author (or a teammate) quoting the bot, or discussing the bot's markup,
  puts the literal marker in a human comment → the bot edits *that* comment.
- Another bot/integration echoing or including the string.
- This is the **same bug-class already hit once** on this branch: the
  footnote-strip regex matched a stray `<!-- ai-review-fail-note -->` that
  appeared in source code / a specialist's quoted finding (see
  `2026-07-29-comment-visibility-final-review-fixes.md`, fix 2). Substring
  matching on a marker that can appear in user-controlled content is the
  recurring root cause.

The blast radius is real: editing the wrong comment overwrites someone else's
content (or silently posts the review into the wrong place), which is exactly
what the comment-visibility work exists to prevent.

### Candidate directions (design pass needed)

- **Author check (cheap):** in the find loop, additionally require the
  comment's author to be the bot itself — `comment.user.type == "Bot"` and/or a
  specific app login (e.g. `<app-slug>[bot]`). Kills the "a human/other comment
  contains the marker" vector with a one-loop change. Decide how to obtain the
  expected identity (config value vs. a `GET /app` lookup vs. remembering the
  author of the comment we created).
- **Persist the comment id (robust):** the `tickets.comment_id` column already
  exists and is currently unused (kept for this class of use). Capture the id
  when the bot first creates its comment and thereafter **edit by id**, falling
  back to the author-filtered scan, then to create. Eliminates thread scanning
  entirely. Needs plumbing: the orchestrator's post must return the created
  comment's id, the dispatcher must pass it to `finalize_review(comment_id=...)`
  (which already accepts the argument), and `upsert_comment` /
  `append_review_footnote` must accept an optional target id.
- Decide whether the marker stays as a secondary disambiguator/fallback or is
  retired once id-based identity lands.

---

## Finding 2 — surface a "re-review scheduled" notice instead of a fully silent wait

### What happens today

Both the comment-visibility design (§6.4) and the escalating-cooldown design
(§3.3) chose the cooldown wait to be **silent**: while a re-review is deferred,
the previous review's comment stays visible unchanged and nothing new is
posted, on the rationale that "there is nothing to notify."

### The proposal

When a summary is already posted and a new push schedules a re-review, **append
a self-cleaning section to the existing summary comment** — e.g.
"🔄 Re-review scheduled (~HH:MM UTC / in ~N min)" — rather than leaving the
wait fully silent. This is an **edit-in-place** (no new comment, no GitHub
notification — GitHub does not notify on edits — so it is *not* comment spam),
reusing the exact append-a-delimited-footnote pattern already built for the
failure notice (`append_review_footnote` + `FAIL_NOTE_*`-style sub-markers,
self-cleaning on the next full review overwrite).

### Why it's worth revisiting the "silent" decision

- **Escalation makes waits long.** With the escalating cooldown, a churning
  PR's next review can be deferred up to an hour. A viewer who opens the PR
  sees a stale review with no explanation; a one-line "refresh coming at ~HH:MM"
  makes the staleness clearly *intentional*, not a broken bot.
- **It's cheap and non-spammy** — one edit-in-place, self-cleaning, no
  notification — so it does not reintroduce the churn the silent decision was
  protecting against.

### Candidate directions (design pass needed)

- Decide **when** the notice appears: only while `deferred` on a cooldown wait?
  Also on a rate-limit/daily-wall wait (partly overlaps today's placeholder for
  the no-good-review case)? Unify with the existing placeholder path so there is
  one coherent "current state of the comment" story instead of three
  (placeholder vs. footnote vs. silent).
- Decide the **content**: absolute time (`~HH:MM UTC`) vs. relative (`in ~N
  min`) vs. both; whether to show the escalation level / that the PR is being
  throttled.
- Decide the **edit cadence**: re-writing the scheduled time on every re-arm is
  an edit each cooldown cycle for a churning PR — acceptable (no notification),
  but note the comment's "edited" timestamp will move.
- This **supersedes/refines** the §6.4 and §3.3 "silent" statements — update
  both specs to describe the chosen behavior once decided.

---

## Note on grouping

Findings 1 and 2 are one concern — *the bot's comment: reliably find it, and
show the right thing in it* — and both touch `github_app` + `comment_id` + the
footnote machinery. They should be brainstormed together. Finding 1 (correct
identity) is arguably a prerequisite for Finding 2 (richer content), since
appending a notice to the *wrong* comment is worse than not appending one.

## Suggested prompt to continue planning

> Brainstorm and then write an implementation plan for two comment-lifecycle
> follow-ups to the durable review queue's dispatcher, on branch
> `fix/comment-visibility` (kept as-is, not yet merged to `master`). Read
> `docs/2026-07-31-comment-lifecycle-followups.md` in full first — it has the
> complete context for both. (1) The bot identifies its own PR comment by a
> fragile substring match on a hidden marker over every thread comment
> (`app/github_app.py`), so a human/other comment containing the marker can be
> edited by mistake — the same bug-class already fixed once on the
> footnote-strip path; decide a robust identity (author check and/or persisting
> the existing unused `comment_id` and editing by id). (2) The cooldown wait is
> currently fully silent (both specs' §6.4/§3.3); decide whether to append a
> self-cleaning "re-review scheduled ~HH:MM" notice to the existing summary
> (edit-in-place, no notification, reusing the failure-footnote pattern), which
> is more valuable now that escalation can push a wait out to an hour — and if
> so, update those specs' "silent" statements. Design the two together (correct
> identity is a prerequisite for safely appending a notice). Follow this repo's
> usual TDD/task-plan conventions
> (`docs/superpowers/plans/2026-07-29-comment-visibility-followups.md` is a good
> reference for the expected plan format and rigor).
