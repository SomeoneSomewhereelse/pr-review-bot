# Design — Robust bot-comment identity (marker fragility fix)

**Date:** 2026-07-31
**Status:** Approved for planning
**Relates to:** `docs/2026-07-31-comment-lifecycle-followups.md` (Finding 1 — this;
Finding 2 deferred, see §6), `app/github_app.py`, `app/orchestrator.py`,
`app/queue/store.py` (`comment_id` column, `finalize_review`),
`docs/2026-07-29-comment-visibility-final-review-fixes.md` (fix 2 — the same
marker-substring bug-class). Branch: `master` (escalating cooldown merged).

## 1. Problem

`github_app.py` identifies "the bot's own PR comment" to edit on a re-review by a
**substring match** on a hidden marker over *every* comment in the thread
(`upsert_comment` and `append_review_footnote` both do
`if COMMENT_MARKER in comment.body`). The marker can legitimately appear in a
comment the bot does **not** own — a PR author quoting the bot, another
integration, or review content echoing the string — so the bot can edit the
wrong comment, overwriting someone else's content. This is the same bug-class
already fixed once on the footnote-strip path
(`2026-07-29-comment-visibility-final-review-fixes.md`, fix 2). The `comment_id`
column exists but is unused; `attempt_review` posts the review comment and
discards the returned `IssueComment` (its id is available but thrown away).

## 2. Decision

**Defense in depth: `comment_id` as the primary identity, an author-filtered
marker scan as the fallback.** Edit-by-id is fast and unambiguous when we know
the id; the fallback is correct when we don't (or the comment was deleted).

| Decision | Choice |
|---|---|
| Primary identity | The persisted `comment_id` (edit by id) |
| Fallback identity | Scan for a comment that is **bot-authored** (`user.type == "Bot"`) **and** carries `COMMENT_MARKER` |
| Author precision | `type == "Bot"` + our marker (no login match, no new config, no `GET /app`) |
| Persistence point | Only `finalize_review` (the long-lived review comment); placeholder/failure paths rely on the fallback |
| Boundary | `comment_id` is a GitHub identifier threaded through `attempt_review`; the dispatcher (ticket owner) supplies it — no queue concept leaks into the orchestrator |

## 3. Mechanism

### 3.1 Identity resolution (`github_app.py`)

One helper, used by both posting functions:

```python
from github import GithubException  # add to imports

def _is_bot_comment(comment) -> bool:
    """True if the comment was authored by a GitHub App bot (not a human)."""
    return getattr(comment.user, "type", None) == "Bot"


def _find_bot_comment(repo, pr, comment_id: int | None) -> IssueComment | None:
    """Locate the bot's own comment: by stored id first, else an author-filtered
    marker scan. Returns None if neither finds one (caller creates a fresh one)."""
    if comment_id is not None:
        try:
            return repo.get_issue_comment(comment_id)   # repo-level issue comment
        except GithubException:
            pass  # deleted/unknown -> fall back to the scan
    for comment in pr.get_issue_comments():
        if _is_bot_comment(comment) and COMMENT_MARKER in comment.body:
            return comment
    return None
```

- A comment found **by id** is trusted (we created it) — no re-verification.
- The **scan** requires `type == "Bot"` *and* the marker: a human/PR-author
  quoting the marker is `type == "User"` → skipped; other bots lack our private
  marker string. Given the design keeps exactly one bot marker-comment per PR,
  the first match is unambiguous.

`upsert_comment` and `append_review_footnote` each gain an optional
`comment_id: int | None = None` param, resolve via `_find_bot_comment`, then edit
the found comment (full-body replace / footnote-append respectively) or
`create_issue_comment` a fresh marker-carrying one. Both still return the
`IssueComment`.

### 3.2 Persisting the id (plumbing)

- `orchestrator.ReviewCompleted` gains `comment_id: int`.
- `attempt_review(repo_full_name, pr_number, comment_id: int | None = None)`
  passes `comment_id` into `upsert_comment` (so a re-review edits the known
  comment) and **captures** the returned comment's `.id` into
  `ReviewCompleted`. `run_review` (the back-compat wrapper) is unchanged.
- The **dispatcher** supplies `ticket.comment_id` into `attempt_review`, and on
  `ReviewCompleted` passes `outcome.comment_id` to
  `finalize_review(..., comment_id=outcome.comment_id)`, which already persists
  it (the column exists — **no schema change**). The placeholder post
  (`_post_placeholder` → `upsert_comment`) and the failure post
  (`append_review_footnote`, and the no-good-review `upsert_comment` overwrite)
  also pass `ticket.comment_id` so they edit-by-id when it's known.

### 3.3 Why persist only at finalize is sufficient

A placeholder or failure comment created while `comment_id` is still unset is
re-found on the next call by the **author-filtered scan** (it is bot-authored
and carries the marker). So the two tiers compose: id when known, scan as the
safety net. Persisting an id at every posting path (which would add store writes
to the webhook/defer paths) is unnecessary and out of scope.

## 4. Surface

- `app/github_app.py` — `GithubException` import; `_is_bot_comment` +
  `_find_bot_comment`; `comment_id` param on `upsert_comment` and
  `append_review_footnote`; both use the resolver.
- `app/orchestrator.py` — `ReviewCompleted.comment_id`; `attempt_review` gains
  `comment_id` param, passes it, and captures the posted comment's id.
- `app/queue/dispatcher.py` — thread `ticket.comment_id` into the
  `attempt_review` / placeholder / failure calls; pass `outcome.comment_id` into
  `finalize_review`.
- No `store.py` schema change (`comment_id` column + `finalize_review` param
  already exist).

## 5. Testing (deterministic-first; stubbed GitHub)

- **`github_app` resolution order:** edit-by-id when a live `comment_id` is
  given; a `GithubException` on that id falls back to the scan; the scan matches
  a bot-authored marker comment and **skips a human comment that contains the
  marker** (`type == "User"`); creates when none found. Same coverage for
  `append_review_footnote`. Existing `github_app` tests' mock comments gain a
  `user.type` field so the author check is exercised.
- **`orchestrator`:** `attempt_review` returns `ReviewCompleted` whose
  `comment_id` is the posted comment's id, and passes the incoming `comment_id`
  through to `upsert_comment`.
- **`dispatcher`:** on completion, `attempt_review` is called with
  `ticket.comment_id` and `finalize_review` is called with the returned
  `comment_id` (persisted on the ticket); placeholder/failure posts pass
  `ticket.comment_id`.

## 6. Out of scope / deferred

- **Finding 2 (re-review-pending notice) is deferred.** The valuable case — the
  cooldown/escalation wait (up to 1h) — is set by `enqueue_or_update` (Site A)
  **in the webhook**, which does no GitHub work, and the dispatcher's
  `claim_next_due` only returns *due* tickets, so there is **no dispatcher
  touch-point** at which to post a notice for a deferred-not-yet-due ticket.
  Notifying it needs a new dispatcher sweep over waiting tickets + a
  notice-posted flag + per-ticket GitHub calls — disproportionate for a demo
  where the silent cooldown is acceptable. Re-open if it proves worth it; this
  robust-identity work is a prerequisite for safely appending any such notice.
- Login-specific author matching (vs `type == "Bot"`); persisting the id at the
  placeholder/failure paths; any change to the cooldown/escalation, rate-limit,
  or failure-backoff logic.
