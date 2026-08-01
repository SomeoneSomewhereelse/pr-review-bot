# Design — Re-review scheduled notice (Finding 2)

**Date:** 2026-08-01
**Status:** Approved for planning
**Relates to:** `docs/2026-07-31-comment-lifecycle-followups.md` (Finding 2 —
this; Finding 1, robust comment identity, is a prerequisite and is now
merged), `docs/superpowers/specs/2026-07-29-comment-visibility-followups-design.md`
(§6.4 "silent cooldown" — this design refines that statement),
`docs/superpowers/specs/2026-07-31-escalating-cooldown-design.md` (§3.3
"silent" — refined here too; also the source of the two re-arm sites this
design hooks into), `docs/superpowers/specs/2026-07-31-comment-identity-design.md`
(the `comment_id`-first resolver this design routes all posts through).
Branch: `master`.

## 1. Problem

Both the comment-visibility design (§6.4) and the escalating-cooldown design
(§3.3) chose the cooldown/rate-limit wait to be **fully silent**: while a
re-review is deferred, a prior good review's comment stays visible,
unedited, and nothing new is posted — "there is nothing to notify."

With escalation, that wait can now reach an hour. A viewer opening the PR
sees a stale review with no explanation — indistinguishable from a broken
bot. Separately, tracing the current code surfaced a second, previously
unnoticed instance of the same gap: when a good review already exists and
the *next* attempt gets rate-limited (not cooldown-deferred), the dispatcher
today posts nothing at all (`_post_placeholder` is only called when
`not _has_visible_review(ticket)`) — the identical silent-wait problem,
under a different trigger.

## 2. Decision

**Append a self-cleaning "re-review scheduled" footnote, unified across
every trigger that puts a reviewed ticket into `deferred`.** One mechanism,
one new dispatcher-loop step, no new process.

| Decision | Choice |
|---|---|
| Scope | Any `deferred` ticket with a visible prior review — covers cooldown re-arm (webhook- or dispatcher-triggered) AND rate-limit defer-with-good-review alike |
| Content | Absolute UTC time only — `"🔄 Re-review scheduled ~HH:MM UTC"` |
| Throttling visibility | Not mentioned — the note states the schedule, not the mechanism |
| Mechanism | A sweep step in the dispatcher's existing loop, keyed by a persisted "last notified `not_before`" marker |
| Failure-backoff exclusion | A new `'retrying'` ticket status, so `'deferred'` means *exactly* "cooldown/rate-limit schedule wait" — never a hard-failure retry |
| Cleanup | Strip the notice the moment a ticket is claimed — the wait is over, whatever happens next |
| New process/config | None (one new status value; no new service) |

### Why absolute time, not relative

GitHub's comment body is static, sanitized Markdown/HTML rendered
identically for every viewer — there is no per-viewer timezone
localization available to third-party comment content (the `<relative-time>`
custom element GitHub itself uses for commit/review timestamps is stripped
by the sanitizer on ordinary posted comments). Given that constraint,
relative time (`"in ~45 min"`) is actively worse than it first appears: this
note is only *edited* on a re-arm event, not continuously updated like a
live chat UI, so a relative string starts going stale the moment it's
posted. Absolute UTC stays accurate for as long as the comment sits
unedited, and matches the wording style already used in the existing
placeholder's daily-quota case (`format_placeholder`).

### Why a unified sweep, not per-site inline posting

Tracing the current code, of the four moments a ticket becomes `deferred`,
three already run inside the dispatcher (which already does GitHub work):
the dirty-flag re-arm on completion, and both rate-limit branches in
`process_next_due`. Only one — a push arriving at the webhook while a
ticket sits `done`/`deferred`/`failed` (`enqueue_or_update`'s re-arm
branches) — has no GitHub-capable process watching it, since the webhook
handler does zero GitHub work by design (HMAC verify → enqueue → `202`
immediately).

Rather than special-case that one webhook-triggered path (inline posting at
the three dispatcher-side moments, plus a narrow sweep only for the fourth),
a single uniform sweep over `deferred` tickets, run once per existing
dispatcher loop tick, covers all four triggers with one code path. The
cost is bounded by the dispatcher's existing poll cadence
(`dispatcher_idle_sleep_seconds`) — negligible — and, more importantly, the
GitHub-call volume is bounded by *actual schedule changes*, not polling
frequency: the persisted marker means a ticket is only touched again once
its `not_before` has actually moved, so cost scales with PR push activity,
not with how often the dispatcher wakes up. This directly answers the
original finding's "disproportionate for a demo" cost concern — the
mechanism was never as expensive as "a new dispatcher" implied; it's one
more bounded query and at-most-one-call-per-re-arm added to the process
that already exists.

A rejected alternative: have the webhook post the notice itself via a
FastAPI `BackgroundTask`. Rejected because it would give the webhook
handler a new GitHub-calling responsibility it doesn't have today (breaking
its "HMAC + enqueue only" boundary) and duplicate identity/posting logic
across two processes instead of centralizing it in the dispatcher, which
already owns all GitHub-facing work in this system.

### Why a new `'retrying'` status, not an `attempts == 0` heuristic

Tracing the code further surfaced a real gap in the first draft of this
design: `status = 'deferred'` is not unique to a cooldown/rate-limit wait —
`defer_failed` (hard-failure retry backoff, used when `attempt_review`
raises) *also* sets `status = 'deferred'`. A sweep keyed only on
`status = 'deferred'` would sometimes post a schedule note on a ticket that
is actually silently retrying after an error, stepping on the separate,
already-existing failure-footnote/ceiling mechanism this design must not
touch (§6 non-goals).

`attempts` is not a reliable discriminator either: a genuine cooldown re-arm
always resets `attempts` to 0, but a rate-limited wait leaves `attempts`
untouched — so a ticket with leftover nonzero `attempts` from an earlier,
unrelated failure cycle could later become genuinely rate-limited while
still showing `attempts > 0`, wrongly suppressing a real notice under an
`attempts == 0` gate.

The precise fix: give hard-failure backoff its own status, `'retrying'`,
distinct from `'deferred'`. No schema migration complexity — `tickets.status`
has no `CHECK` constraint, so this is purely an additional string value.
`'deferred'` then means *exactly* "cooldown or rate-limit schedule wait,"
with no ambiguity and no heuristic needed. Blast radius, traced against the
current code: `defer_failed` sets the new status instead of `'deferred'`;
`claim_next_due`'s claimability check and its claiming `UPDATE` both need
`'retrying'` added alongside `'deferred'` (a failure-backoff ticket must
still become claimable once its wait elapses); `enqueue_or_update`'s
ride-out branch (`elif status in ("pending", "deferred")`) needs
`'retrying'` added too (a push arriving mid-retry-backoff should still just
ride out, not fall into the terminal-state re-arm branch). A handful of
existing tests asserting `status == "deferred"` for `defer_failed`-driven
scenarios need to assert `"retrying"` instead; tests covering rate-limit and
cooldown-push scenarios are unaffected.

## 3. Mechanism

### 3.1 State

- New nullable column on `tickets`: `notice_not_before TEXT NULL` — records
  which `not_before` value the currently-posted notice (if any) reflects. A
  ticket whose `notice_not_before` doesn't match its current `not_before`
  needs a fresh (or first) notice; no explicit reset is needed elsewhere,
  since every re-arm changes `not_before` and the mismatch falls out
  automatically.
- New status value `'retrying'`, used by `defer_failed` instead of
  `'deferred'` (see rationale above). `'deferred'` now means exclusively a
  cooldown/rate-limit schedule wait.

### 3.2 Store additions (`app/queue/store.py`)

```python
def tickets_needing_notice(now: str) -> list[Ticket]:
    """Deferred (schedule-wait, never retry-backoff) tickets with a visible
    prior review whose schedule changed since the last notice (or none was
    posted yet)."""
    # status = 'deferred' AND not_before IS NOT NULL
    # AND last_reviewed_at IS NOT NULL   -- has a visible review to preserve;
    #     a never-reviewed deferred ticket keeps today's immediate placeholder,
    #     posted inline in process_next_due, untouched by this feature
    # AND (notice_not_before IS NULL OR notice_not_before != not_before)


def mark_notice_posted(ticket_id: int, not_before: str) -> None:
    """Record that a notice reflecting `not_before` was just posted.
    A single independent UPDATE — not inside enqueue_or_update's or
    finalize_review's transactions, same pattern as mark_failed."""


def clear_notice(ticket_id: int) -> None:
    """Clear the notice marker after the dispatcher has stripped the
    schedule footnote from GitHub (called right after a ticket is claimed —
    see 3.4). A single independent UPDATE, same pattern as mark_failed."""
```

`defer_failed` sets `status = 'retrying'` (was `'deferred'`). `claim_next_due`'s
`SELECT`/`UPDATE` and `enqueue_or_update`'s ride-out branch both add
`'retrying'` alongside `'deferred'` (see rationale above). No other change
to `enqueue_or_update`, `finalize_review`, or either rate-limit branch in
`process_next_due` — the `last_reviewed_at IS NOT NULL` + `deferred` +
`not_before` shape those functions already produce is exactly what the
sweep query matches. This is what makes the rate-limit-with-good-review
case fall out "for free": it was never a distinct code path to add, just
the same data shape the sweep already looks for.

### 3.3 Formatting + marker (`app/formatting.py`, `app/github_app.py`)

```python
SCHEDULE_NOTE_START = "<!-- ai-review-schedule-note -->"
SCHEDULE_NOTE_END = "<!-- /ai-review-schedule-note -->"


def format_schedule_notice(not_before: datetime) -> str:
    """Self-cleaning notice appended below a preserved good review when the
    next re-review is scheduled (cooldown or rate-limit wait)."""
    eta = not_before.strftime("%H:%M UTC")
    return f"{SCHEDULE_NOTE_START}\n🔄 Re-review scheduled ~{eta}\n{SCHEDULE_NOTE_END}"
```

`_strip_existing_footnote(body)` (currently `FAIL_NOTE_*`-only) is
generalized to recognize *either* known footnote marker pair — trying
`FAIL_NOTE_START`/`END` and `SCHEDULE_NOTE_START`/`END` in turn, each with
the same "last START, only strip if body ends with END" logic that already
guards against the stray-marker-substring bug (`2026-07-29-comment-
visibility-final-review-fixes.md`, fix 2) — rather than taking a
marker-pair parameter. This keeps every call site's signature unchanged and
gives a robustness property for free: whichever footnote-writing function
runs next (failure note or schedule note) cleans up a stale leftover of the
*other* kind too, so even if the claim-time strip (3.4) ever fails, the
system can't end up showing two contradictory footnotes at once — the next
write to either slot self-heals it.

Two new functions, both mirroring `append_review_footnote`'s existing
shape exactly (found bot comment → strip → edit-in-place; create a
marker-carrying comment if none exists), so `append_review_footnote`
itself is left completely unchanged:

```python
def append_schedule_notice(
    repo_full_name: str, pr_number: int, footnote: str, comment_id: int | None = None
) -> IssueComment:
    """Append/refresh the schedule-wait footnote below the bot's own comment."""


def clear_schedule_notice(
    repo_full_name: str, pr_number: int, comment_id: int | None = None
) -> IssueComment | None:
    """Strip any existing footnote (schedule or, defensively, failure) from the
    bot's comment — called once a deferred ticket is claimed and its wait is
    over. No-op (no edit call) if the comment has no footnote to strip, or if
    no bot comment exists yet."""
```

### 3.4 Dispatcher: sweep + claim-time cleanup (`app/queue/dispatcher.py`)

**The sweep** — a new function, called once per `run_forever` iteration
immediately after the existing `process_next_due` call:

```python
async def post_pending_notices(now: datetime) -> int:
    """Refresh the schedule footnote on every deferred ticket whose
    not_before changed since the last notice. Returns the count posted."""
    posted = 0
    for ticket in store.tickets_needing_notice(now.isoformat()):
        try:
            await asyncio.to_thread(
                github_app.append_schedule_notice,
                ticket.repo_full_name,
                ticket.pr_number,
                format_schedule_notice(datetime.fromisoformat(ticket.not_before)),
                ticket.comment_id,
            )
            store.mark_notice_posted(ticket.id, ticket.not_before)
            posted += 1
        except Exception:  # noqa: BLE001 - one ticket's failure must not block the rest
            logger.exception("failed to post schedule notice for ticket %s", ticket.id)
    return posted
```

`run_forever` gains one line: `await post_pending_notices(now)` alongside
the existing `await process_next_due(now)`, both inside the same
try/except-and-continue loop body it already has.

**Claim-time cleanup** — a new step inside `process_next_due`, immediately
after `claim_next_due` returns a ticket (before the blocked-provider gate,
so it runs regardless of what happens to this attempt):

```python
    if ticket.notice_not_before is not None:
        try:
            await asyncio.to_thread(
                github_app.clear_schedule_notice,
                ticket.repo_full_name, ticket.pr_number, ticket.comment_id,
            )
            store.clear_notice(ticket.id)
        except Exception:  # noqa: BLE001 - a stale note is cosmetic; must not block the review
            logger.exception("failed to clear schedule notice for ticket %s", ticket.id)
```

This can't be folded into `claim_next_due`'s own atomic `UPDATE` (clearing
`notice_not_before` there would mean the re-`SELECT`ed, returned `Ticket`
never shows the dispatcher that a notice existed to strip) — it has to be a
separate call the dispatcher makes *after* successfully stripping the
GitHub comment, mirroring the sweep's `post → mark_notice_posted` pattern
with a parallel `strip → clear_notice`.

Both new call sites route through Finding 1's `comment_id`-first,
author-filtered-scan resolver — no new identity logic needed anywhere in
this design.

## 4. Error handling

A single ticket's notice-post failure inside the sweep (transient network
error, etc.) is caught per-ticket (so one bad ticket doesn't block the rest
of the batch) and logged, matching the existing `run_forever`
belt-and-suspenders pattern. It self-heals with no new backoff/ceiling
bookkeeping: since `notice_not_before` wasn't updated on failure, the same
ticket matches the sweep query again next tick — and unlike the
failure-path ceiling, there is no "give up" case, because the ticket
naturally stops mattering to this sweep once it becomes due for a real
review (status changes away from `deferred`).

A claim-time strip failure is likewise caught and logged without blocking
the review attempt that just claimed the ticket. The residual risk — a
stale schedule note lingering if both the strip fails *and* this attempt
goes on to hard-fail all the way to the failure ceiling — is bounded and
self-healing: the generalized `_strip_existing_footnote` (§3.3) means the
next write to either footnote slot (a real review's full-body overwrite, a
later successful strip, or even the terminal `FAIL_NOTE` itself) cleans up
the leftover automatically. No secret is ever included in any new note — it
carries only a formatted timestamp.

## 5. Testing (deterministic-first, injected clock, stubbed GitHub)

- **`'retrying'` status migration:** `defer_failed` sets `status = 'retrying'`
  (not `'deferred'`); a `'retrying'` ticket whose `not_before` has passed is
  still claimable (`claim_next_due`); a push arriving while `'retrying'`
  still rides out (`enqueue_or_update`, head_sha updates, not_before/attempts
  unchanged) exactly as a `'deferred'` ticket does today. Existing tests
  asserting `status == "deferred"` for `defer_failed`-driven scenarios are
  updated to assert `"retrying"`.
- **`tickets_needing_notice` (pure query shape):** never-notified (`notice_not_before`
  is `NULL`) → matches; stale (`notice_not_before != not_before`) → matches;
  up-to-date (`notice_not_before == not_before`) → excluded; no visible
  review (`last_reviewed_at IS NULL`) → excluded regardless of notice state;
  wrong status (`pending`/`running`/`retrying`/`done`/`failed`) → excluded —
  in particular, a `'retrying'` ticket with a visible review and a future
  `not_before` (the exact shape that would have false-matched under the
  original `attempts == 0` heuristic) is excluded by status alone.
- **`mark_notice_posted`:** persists the marker; a ticket re-queried
  afterward no longer matches `tickets_needing_notice` for that same
  `not_before`.
- **`clear_notice`:** clears the marker; a ticket re-queried afterward has
  `notice_not_before is None`.
- **Dispatcher sweep:** posts a correctly-formatted UTC-time footnote for a
  matching ticket via the stubbed `append_schedule_notice`, passing the
  ticket's `comment_id`; does not post again on a second sweep tick with no
  schedule change; a per-ticket stubbed failure is caught and logged without
  stopping the sweep from processing other due tickets.
- **Claim-time cleanup:** claiming a ticket with a non-`None`
  `notice_not_before` calls the stubbed `clear_schedule_notice` with the
  ticket's `comment_id` and then clears the marker; claiming a ticket with no
  notice does not call it at all; a stubbed clear failure is caught and
  logged without preventing the review attempt from proceeding.
- **Self-cleaning:** a subsequent full review completion (`upsert_comment`'s
  full-body overwrite) removes the schedule footnote — same test shape as
  the existing failure-footnote self-cleaning test.
- **Cross-footnote robustness:** `_strip_existing_footnote` strips a
  schedule note even when called from the failure-note path (and vice
  versa) — the generalized helper recognizes either marker pair.
- **Regression:** no existing test in `process_next_due`'s rate-limit or
  cooldown paths changes behavior beyond the `'retrying'` rename described
  above — the sweep and claim-time cleanup are purely additive.

## 6. Non-goals

- No change to escalation math, the rate-limit gate, or the dirty-flag
  coalescing logic. The failure-path ceiling's own behavior is unchanged;
  only its ticket status changes name (`'deferred'` → `'retrying'`).
- No new configuration — reuses `dispatcher_idle_sleep_seconds` as the
  sweep's effective cadence.
- Rewording the existing "no review yet" placeholder's copy
  (`format_placeholder`) to match the new notice's tone — a separate,
  optional follow-up, not required for this fix to be complete.
- Mentioning escalation level or throttling mechanics in the notice text —
  deliberately kept to just the schedule, per design decision above.
- Guaranteeing the claim-time strip never races a stray leftover footnote in
  every conceivable failure sequence — the generalized strip helper makes
  this self-healing (§4) rather than impossible; not worth more machinery
  for a demo-scale bot.
