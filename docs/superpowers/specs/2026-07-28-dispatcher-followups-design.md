# Design — Dispatcher Follow-ups (backoff guardrail + re-review policy)

**Date:** 2026-07-28
**Status:** Approved for planning
**Relates to:** `docs/2026-07-28-dispatcher-followups.md` (the findings that
motivated this), `docs/superpowers/specs/2026-07-27-queue-features-design.md`
(the durable review queue this extends), branch `feat/review-queue`
(current code, fix commit `7a4690e`).

## 1. Problem

Two interim decisions from the durable-review-queue build were flagged as "not
final" and deferred to this session (full detail in
`docs/2026-07-28-dispatcher-followups.md`):

1. **The dispatcher's backoff is a blunt floor, not a guardrail.**
   `run_forever` now sleeps `dispatcher_idle_sleep_seconds` (1.0s)
   unconditionally after every iteration. That stops the tight-loop
   `Retry-After: 0` hammering, but there is no escalation on repeated
   failures, no distinction between a `RateLimited` deferral and a genuine
   error, and no ceiling on how long a ticket can churn.

2. **Re-review triggering is inconsistent across ticket states and undocumented
   as a design.** A push landing while a review is `running` is silently
   dropped (the in-flight review finishes against its original commit and the
   new commit waits for some *later* push). A push landing while `deferred`
   resets the wait clock as an incidental side effect of the `enqueue_or_update`
   CASE logic. There is no per-PR cooldown, so a rapidly-iterated PR can trigger
   a review on nearly every push.

## 2. Root cause (shared by both findings)

The current code routes two semantically different waits through one path.
`defer()` is used for both a `RateLimited` deferral (**per-provider** — the
provider is throttling *everyone*) and would-be error retries (**per-ticket** —
*this* review hit an error), and it increments `attempts` for both. Separating
these two wait-types is the spine of this design.

## 3. Goals / non-goals

**Goals**

- Escalating (exponential, capped) backoff for **hard failures**, keyed
  per-ticket, so a repeatedly-failing review does not hammer and does not block
  the queue for other PRs.
- A **hard stop**: after `MAX_FAILURE_ATTEMPTS` consecutive hard failures, mark
  the ticket terminally failed and post a visible failure comment.
- `RateLimited` deferrals stay **per-provider**, are **not** counted toward the
  hard stop (a daily wall must survive arbitrarily many defers), and get a
  minimum floor so a degenerate `Retry-After: 0` can't cause a tight loop.
- A single, coherent, **documented** re-review policy across every ticket state:
  coalesce pushes to the latest commit, never shorten a provider wait, and never
  run two *completed* reviews for the same PR less than a cooldown apart.
- Fix the `running`-push drop without introducing task cancellation.

**Non-goals (YAGNI)**

- **No cancel-and-restart of a running review.** Rejected: it needs
  `attempt_review` run as a cancellable task, cancellation propagated into the
  three concurrent LLM calls, and half-written-comment cleanup — too much for a
  single-process, demo-scale dispatcher. The dirty-flag re-enqueue (§6) gets the
  same practical outcome without it.
- **No multi-instance operation.** Jitter is built as an *off-by-default* hook
  (§5.3) so a future multi-instance deployment can enable it without a code
  change, but nothing here runs or is tested with more than one dispatcher.
- **No new error-text column / no widening of what is logged.** The failure
  *comment* shows a redacted, generic message (§5.4); raw error text is not
  surfaced to the PR (secrets hygiene, per `CLAUDE.md`).

## 4. Core decisions (locked during brainstorming)

| Decision | Choice |
|---|---|
| Failure policy | Per-ticket exponential backoff, hard-stop + notify after `MAX_FAILURE_ATTEMPTS` |
| RateLimited vs failure | Separate paths: `RateLimited` → per-provider `blocked_until`, uncounted; failure → per-ticket `not_before`, counted |
| Degenerate `Retry-After` | Floor every RateLimited wait at `MIN_RETRY_AFTER_SECONDS` |
| Jitter | Built as an injectable, off-by-default hook; deterministic (0) in tests and single-instance |
| Running-push | Dirty flag (`rereview_requested`) → self-re-enqueue on completion; no cancellation |
| Deferred-push | **Ride out** — update `head_sha`, keep `not_before`/status; never reset the provider clock |
| Cooldown | Per-PR, keyed on `last_reviewed_at` (last *completed* review); default 300s |
| Cooldown UX | Silent — the previous review comment stays visible; no placeholder churn while cooling down |

---

## 5. Finding 1 — backoff guardrail

### 5.1 Two wait mechanisms, kept separate

- **`RateLimited` → per-provider `blocked_until` (global gate, unchanged shape).**
  On a `ReviewRateLimited(retry_after)` outcome (or the pre-flight
  `blocked_until` gate firing), the ticket is deferred with
  `not_before = now + max(retry_after, MIN_RETRY_AFTER_SECONDS)` via
  **`defer_rate_limited`** (which does **not** touch `attempts`).
  `MIN_RETRY_AFTER_SECONDS` (default 1.0) kills the `Retry-After: 0`/past-date
  tight loop. This wait is *not* counted toward the hard stop — the provider
  will free up eventually, and a daily wall legitimately defers for hours.

- **Hard failure → per-ticket `not_before` (local, escalating).**
  Any non-`RateLimited` exception from `attempt_review` is a hard failure. The
  ticket is deferred via **`defer_failed`** (which increments `attempts`) with an
  exponential backoff computed from the *new* attempt count. Per-ticket is
  deliberate: a "poison" ticket that always errors only delays itself and never
  blocks other PRs from the same provider (which a global block would).

### 5.2 Backoff formula (pure, testable)

```
backoff(attempts) = min(BASE * 2 ** (attempts - 1), CAP) + jitter()
```

- `attempts` is the per-ticket hard-failure count, 1-based (first failure → `BASE`).
- `BASE = dispatcher_failure_base_backoff_seconds` (default 2.0).
- `CAP = dispatcher_failure_max_backoff_seconds` (default 300.0).
- `jitter()` — see §5.3.

Implemented as a pure function `compute_backoff(attempts, jitter_seconds)` so the
math is unit-tested with jitter fixed at 0.

### 5.3 Jitter (off-by-default hook)

`jitter()` returns a value in `[0, dispatcher_backoff_jitter_seconds]`
(default `0.0` → deterministic, no jitter). The randomness comes through an
injectable module-level seam (`dispatcher._jitter`) that tests monkeypatch to a
constant. With the default of 0 the whole system is deterministic; a future
multi-instance deployment sets the config > 0 to spread retries without touching
code.

### 5.4 Hard stop + notify

On a hard failure the dispatcher computes `next_attempt = ticket.attempts + 1`:

- **`next_attempt >= MAX_FAILURE_ATTEMPTS`** (`dispatcher_max_failure_attempts`,
  default 5) → terminal: `store.mark_failed(...)` and post a failure comment via
  a new `formatting.format_failure(pr_number, attempts)` — marker-prefixed so it
  edits any existing review/placeholder comment in place. The comment states the
  review failed after N attempts; it does **not** include raw exception text
  (redacted for secrets hygiene). This satisfies "partial failure is always
  visible."
- **otherwise** → `store.defer_failed(ticket.id, not_before = now +
  compute_backoff(next_attempt, jitter), now)`. Silent (no placeholder): a
  transient error usually clears within a couple of retries, and the terminal
  comment covers the visible-failure guarantee if it doesn't.

A subsequent successful review (or a fresh push, §6) resets `attempts` to 0.

---

## 6. Finding 2 — unified re-review + cooldown policy

One rule, applied consistently: **coalesce pushes to the latest commit; never
shorten a provider wait; never run two *completed* reviews for the same PR less
than the cooldown apart.** The cooldown is keyed on `last_reviewed_at` — set
only on a successful completion — so the **first** review of a PR, and any
rate-limited or failed ticket, is never delayed by cooldown.

### 6.1 Behavior by ticket state when a push lands (`enqueue_or_update`)

| State push lands on | New behavior |
|---|---|
| `pending` (never claimed) | Update `head_sha`, stay `pending`. No cooldown (never reviewed). Unchanged. |
| `deferred` (provider wait **or** cooldown wait) | **Ride out**: update `head_sha`, **keep** `status='deferred'` and `not_before`. A push cannot shorten a provider's rate-limit clock. (Change from today's reset.) |
| `running` | Update `head_sha` **and set `rereview_requested = 1`** (dirty flag). Status stays `running`. (Change from today's silent drop.) |
| `done` / `failed` (terminal) | Re-arm: `head_sha` updated, `attempts` reset to 0, and status/`not_before` set by the cooldown helper (§6.3) — `deferred` until `last_reviewed_at + cooldown` if still cooling down, else `pending` now. |

### 6.2 Dirty-flag completion (`finalize_review`)

`mark_done`'s direct use in the dispatcher is replaced by
`store.finalize_review(ticket_id, now, rereview_not_before, comment_id=None)`,
which in **one** statement (race-free against a concurrent webhook that set the
flag mid-run):

- always sets `last_reviewed_at = now` and `comment_id`;
- if `rereview_requested = 1` → `status = 'deferred'`,
  `not_before = rereview_not_before` (= `now + cooldown`, computed by the
  dispatcher), `attempts = 0`, and clears the flag → exactly one coalesced
  follow-up review of the latest commit, no sooner than the cooldown;
- else → `status = 'done'`.

Using `deferred` (not `pending`) for the cooldown re-arm is required: `pending`
is claimed immediately regardless of `not_before`, whereas `deferred` respects
it.

### 6.3 Cooldown helper

`_due_after_cooldown(last_reviewed_at, now, cooldown_seconds) -> (status,
not_before)` returns `('deferred', <iso>)` when `now < last_reviewed_at +
cooldown`, else `('pending', None)`. Shared by the `done`/`failed` re-arm in
`enqueue_or_update` and by the dispatcher when computing `rereview_not_before`
for `finalize_review`, so the cooldown math lives in exactly one place.

### 6.4 Cooldown UX

Silent. While a re-review waits out the cooldown, the **previous** review's
comment is already on the PR, so there is nothing to notify — the comment simply
updates when the re-review runs. Placeholders remain only for the "no review
visible yet / genuinely blocked" cases (unchanged §9 of the queue design).

---

## 7. Data model changes (`app/queue/store.py`)

### 7.1 Schema

Two new columns on `tickets`:

- `rereview_requested INTEGER NOT NULL DEFAULT 0`
- `last_reviewed_at TEXT`

`init_db` gains an idempotent migration (`PRAGMA table_info(tickets)` → `ALTER
TABLE ... ADD COLUMN` for any missing column) so an existing `queue.db` upgrades
in place. `CREATE TABLE IF NOT EXISTS` keeps the two columns for fresh DBs.

### 7.2 Methods

- `defer_rate_limited(ticket_id, not_before, now)` — `status='deferred'`,
  set `not_before`; **does not** change `attempts`. (Replaces `defer` on the
  RateLimited path and the pre-flight `blocked_until` path.)
- `defer_failed(ticket_id, not_before, now)` — `status='deferred'`, set
  `not_before`, `attempts = attempts + 1`. (Hard-failure path.)
- `finalize_review(ticket_id, now, rereview_not_before, comment_id=None)` —
  §6.2. (Replaces the dispatcher's `mark_done` call.)
- `enqueue_or_update(...)` — rewritten to the §6.1 per-state logic (read the
  existing row, branch on status, update accordingly), using `_due_after_cooldown`
  and reading `settings.dispatcher_rereview_cooldown_seconds`.
- `mark_failed(ticket_id, now)` — unchanged shape (terminal `status='failed'`).
- `recover_on_startup(now)` — also clears `rereview_requested` on the
  `running → pending` reset (the fresh pending review already covers the latest
  commit, so the flag is moot).
- The old single `defer(...)` is removed; all call sites move to the split
  methods.

## 8. Dispatcher changes (`app/queue/dispatcher.py`)

`process_next_due(now)` gains the split; pseudocode:

```
ticket = claim_next_due(now)
if ticket is None: return idle

if now < blocked_until[provider]:
    defer_rate_limited(ticket, not_before=blocked_until[provider])   # no attempts++
    post_placeholder(...);  return deferred

try:
    outcome = await attempt_review(...)
except Exception as exc:                       # hard failure
    next_attempt = ticket.attempts + 1
    if next_attempt >= MAX_FAILURE_ATTEMPTS:
        mark_failed(ticket, now)
        post_failure_comment(ticket, next_attempt)      # format_failure
        return failed
    defer_failed(ticket, not_before = now + compute_backoff(next_attempt, jitter))
    return failed                                # silent, no placeholder

if isinstance(outcome, ReviewRateLimited):
    wait = max(outcome.retry_after, MIN_RETRY_AFTER_SECONDS)
    blocked_until[provider] = now + wait
    defer_rate_limited(ticket, not_before = now + wait)  # no attempts++
    post_placeholder(...);  return deferred

# completed
rereview_not_before = now + cooldown           # only used if the flag is set
finalize_review(ticket, now, rereview_not_before, comment_id=...)
return ran
```

`run_forever` keeps its unconditional post-iteration
`sleep(dispatcher_idle_sleep_seconds)` as the coarse loop floor; the real
escalation now lives in the per-ticket `not_before`/`blocked_until` waits above.
`StepResult.action` values are unchanged (`idle`/`ran`/`deferred`/`failed`).

## 9. Config additions (`app/config.py`)

| Var | Default | Meaning |
|---|---|---|
| `dispatcher_failure_base_backoff_seconds` | `2.0` | Backoff for the first hard failure. |
| `dispatcher_failure_max_backoff_seconds` | `300.0` | Backoff cap. |
| `dispatcher_max_failure_attempts` | `5` | Consecutive hard failures before terminal + notify. |
| `dispatcher_min_retry_after_seconds` | `1.0` | Floor on every RateLimited wait (degenerate `Retry-After`). |
| `dispatcher_backoff_jitter_seconds` | `0.0` | Max jitter added to failure backoff (0 = deterministic/off). |
| `dispatcher_rereview_cooldown_seconds` | `300.0` | Min spacing between completed reviews for one PR. |

## 10. Formatting change (`app/formatting.py`)

`format_failure(pr_number: int, attempts: int) -> str` — marker-prefixed
Markdown, e.g. `## 🤖 Automated Code Review — PR #N` + a line stating the
review could not be completed after `attempts` attempts and will retry on the
next push. **No raw exception text** (secrets hygiene). Marker ensures it edits
any existing comment in place.

## 11. Error handling / edge cases

- **Concurrent push during a run** setting `rereview_requested` after the
  dispatcher claimed the ticket: handled by `finalize_review` reading the flag in
  its single UPDATE, so the flag set at any point before finalize is honored.
- **Restart mid-run** with the flag set: `recover_on_startup` resets to
  `pending` and clears the flag; the fresh review covers the latest commit anyway.
- **Cooldown re-arm producing a `deferred` with a future `not_before`**: correct
  — `claim_next_due` only claims `deferred` once `not_before <= now`.
- **Hard failure then a fresh push** before the backoff elapses: the push lands
  on a `deferred` ticket → ride-out keeps the failure `not_before`. (Acceptable:
  the backoff is short early on; a stuck ticket hard-stops after
  `MAX_FAILURE_ATTEMPTS` regardless.)
- **`MIN_RETRY_AFTER_SECONDS` vs `dispatcher_idle_sleep_seconds`**: independent
  floors — the former bounds a single ticket's `not_before`, the latter bounds
  loop spin.

## 12. Testing strategy (deterministic-first, injected clock + jitter)

New/updated layers, all with `now` injected and `_jitter` monkeypatched to 0:

1. **Backoff (pure)** — `compute_backoff`: first attempt = `BASE`; doubling;
   cap; jitter added when the seam returns nonzero.
2. **Store** — `defer_rate_limited` leaves `attempts` unchanged;
   `defer_failed` increments it; `finalize_review` → `done` when the flag is
   clear, → `deferred` + `not_before = now+cooldown` + `attempts=0` when set;
   migration adds columns to a pre-existing table; `recover_on_startup` clears
   the flag.
3. **`enqueue_or_update` per state** — `pending` push updates `head_sha` stays
   `pending`; `deferred` push rides out (keeps `not_before`); `running` push
   sets `rereview_requested`; `done` push within cooldown → `deferred` at
   `last_reviewed_at+cooldown`; `done` push past cooldown → `pending`.
4. **Dispatcher — failures** — one hard failure → `defer_failed` with
   `not_before = now+BASE`, `attempts=1`, no comment; repeated failures escalate;
   at `MAX_FAILURE_ATTEMPTS` → `mark_failed` + failure comment posted.
5. **Dispatcher — RateLimited** — `retry_after=0` → wait floored at
   `MIN_RETRY_AFTER_SECONDS`; `attempts` unchanged; `blocked_until` set.
6. **Dispatcher — dirty re-review** — flag set during a run → after completion
   the ticket is `deferred` and becomes due at `now+cooldown`, then runs; the
   cooldown wait posts no placeholder.
7. **Formatting** — `format_failure` contains the marker, PR number, attempt
   count, and no raw error string.

Existing `test_dispatcher.py` / `test_queue_store.py` are updated for the split
methods and `finalize_review`.

## 13. Documentation

Update `SPEC.md` §12 to describe the *designed* backoff and re-review/cooldown
behavior precisely (replacing the current "running ticket … not re-reviewed as
part of that ticket" wording and the incidental deferred-reset description), and
list the six new config vars.

## 14. Out of scope

Cancel-and-restart of running reviews (§3); multi-instance operation (jitter hook
is built but off); persisting raw error text; any change to the provider adapters
or `attempt_review`'s internal contract.
