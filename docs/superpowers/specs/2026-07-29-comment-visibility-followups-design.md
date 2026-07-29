# Design — Comment-Visibility Follow-ups + enqueue atomicity

**Date:** 2026-07-29
**Status:** Approved for planning
**Relates to:** `docs/2026-07-29-comment-visibility-followups.md` (motivating
findings), `docs/superpowers/specs/2026-07-28-dispatcher-followups-design.md`
(backoff + re-review/cooldown design this extends), `SPEC.md` §12, branch
`master` (dispatcher-followups already merged).

## 1. Problem

Three findings from the dispatcher-followups final review (full context in
`docs/2026-07-29-comment-visibility-followups.md`):

1. **Finding 1 — the cooldown gate can replace a good review with a placeholder.**
   `process_next_due`'s pre-flight `blocked_until` gate posts a rate-limit
   placeholder unconditionally. When a cooldown-driven coalesced re-review
   becomes due at the same moment the active provider happens to be
   rate-limited, that placeholder **overwrites an already-good, already-visible
   review** — a strictly worse comment, caused by an unlucky timing coincidence.

2. **Finding 2 — the terminal failure comment overwrites a good review.**
   `format_failure` is marker-prefixed, so the hard-stop path upserts it onto
   the same comment as a real review, **destroying a prior good review** when a
   later re-review hits `dispatcher_max_failure_attempts` consecutive hard
   failures. Two smaller issues live in that path: a **silent double-failure**
   (if `upsert_comment` raises after `mark_failed`, the notice is lost and the
   "partial failure is always visible" guarantee quietly breaks) and a
   **grammar bug** ("1 attempts").

3. **Finding 3 (independent) — `enqueue_or_update`'s SELECT-then-UPDATE is a
   latent race.** It is safe only because it runs synchronously on the
   single-threaded event loop with no `await` between the read and the write
   (documented at `app/queue/store.py:96-100`). A future change wrapping it in
   `asyncio.to_thread` would silently reintroduce a real race against
   `claim_next_due`/`finalize_review`.

Findings 1 & 2 share a root cause: every comment write targets the same
marker comment via `github_app.upsert_comment`, and nothing asks "is what's
already on the PR better than what I'm about to post?" They are designed
together (Part A). Finding 3 is unrelated (Part B).

## 2. Core decisions (locked during brainstorming)

| Decision | Choice |
|---|---|
| "Good review present" signal | `ticket.last_reviewed_at is not None` (existing per-PR column, set only by a successful `finalize_review`) |
| Finding 1 fix | Suppress placeholder posting when a good review is present; still defer silently |
| Finding 2 display (good review present) | **Append** a self-cleaning footnote to the preserved comment (sub-marker delimited) |
| Finding 2 display (no good review) | Overwrite the marker comment with `format_failure` as today (grammar fixed) |
| Silent double-failure | Post the notice **before** `mark_failed`; on post failure, `defer_failed` (retry) instead of going terminal |
| Finding 3 atomicity | Wrap `enqueue_or_update`'s read-branch-write in an explicit `BEGIN IMMEDIATE` transaction |

---

## Part A — findings 1 & 2 (preserve a good visible review)

### A1. The shared guard

A review currently on the PR is worth preserving exactly when
`ticket.last_reviewed_at is not None`. Why this bit is sufficient (and why the
"too broad" worry from the findings doc does not apply):

- It is **per-PR** (one row per `(repo, pr)`), set **only** by
  `finalize_review` on a genuinely successful completion. So it is never
  "stale from an unrelated cycle" — there is no unrelated cycle for a given PR.
- `attempt_review` posts the good review in place before `finalize_review`
  records `last_reviewed_at`; placeholders are only posted when
  `last_reviewed_at is None`. So `last_reviewed_at is not None` reliably means
  "the marker comment currently holds a real review."
- A slightly-stale real review still beats a placeholder ("review will appear
  shortly") or a bare failure notice — this is the project's own stance
  (findings doc, Finding 2). So no staleness/push-count threshold is added
  (explicit non-goal, §A5).

The claimed `Ticket` already carries `last_reviewed_at`, so the guard needs no
extra DB read. A tiny helper expresses it:

```python
def _has_visible_review(ticket: store.Ticket) -> bool:
    return ticket.last_reviewed_at is not None
```

### A2. Finding 1 — placeholder sites

`process_next_due` posts a placeholder at two sites: the pre-flight
`blocked_until` gate and the `ReviewRateLimited` outcome. Both keep their
`store.defer_rate_limited(...)` call unchanged; both gate the placeholder post:

- `if not _has_visible_review(ticket): await _post_placeholder(...)`.
- When a good review is present, the defer is **silent** — the good review
  stays up until a later successful re-review overwrites it in place.

First-ever review with nothing good yet (`last_reviewed_at is None`) → the
placeholder posts exactly as today (it is the only signal available).

### A3. Finding 2 — terminal hard-stop

Two sub-cases at the hard-stop branch:

- **No good review present** (`last_reviewed_at is None`): overwrite the marker
  comment with `formatting.format_failure(pr_number, attempts)` as today — it
  is the only signal.
- **Good review present**: **append** a footnote to the preserved comment via a
  new `github_app.append_review_footnote(...)`. The good review is untouched.

The footnote is delimited by sub-markers so it is idempotent and self-cleaning:

- Repeated terminal failures **replace** the prior footnote block in place (no
  stacking).
- A later successful review calls `attempt_review` → `format_comment` →
  `upsert_comment`, which fully overwrites the comment body — the footnote
  vanishes automatically (no separate cleanup step, no `comment_id` tracking).

### A4. Silent double-failure fix

Reorder the terminal block so visibility is guaranteed *before* the ticket goes
terminal:

1. Post the notice first (`append_review_footnote` or `upsert_comment` per §A3).
2. **Only on a successful post** → `store.mark_failed(...)`, return
   `StepResult(action="failed")`.
3. **If the post itself raises** → do **not** strand the ticket as terminal.
   Log, then `store.defer_failed(...)` with `compute_backoff(next_attempt,
   _jitter())` so it retries and re-attempts the notice next cycle. Return
   `StepResult(action="deferred")`.

This keeps "partial failure is always visible" intact even in the
compound-failure case (e.g. a GitHub outage failing both the review and the
notice): the ticket keeps retrying, capped by the existing backoff, until the
notice actually lands. `attempts` may inflate slightly across these retries —
harmless (it only drives backoff, which is capped).

### A5. Grammar fix + non-goals

- `format_failure` and the new footnote pluralize correctly: "1 attempt" /
  "N attempts".
- **Non-goals:** populating `comment_id` / the deferred "ping comment" feature
  (SPEC.md §12 "Future work"); any staleness or push-count tracking; notifying
  the author of a terminal failure (the footnote is an edit; GitHub does not
  notify on edits — acceptable, since a terminal failure is a transient,
  self-retrying infra event that says "will retry on next push").

### A6. Surface (Part A)

- `app/queue/dispatcher.py` — `_has_visible_review` helper; the guard at both
  placeholder sites; the reordered terminal block (post → mark_failed, or
  defer on post failure; append vs overwrite per `_has_visible_review`).
- `app/github_app.py` — `FAIL_NOTE_START` / `FAIL_NOTE_END` sub-marker
  constants; `append_review_footnote(repo_full_name, pr_number, footnote) ->
  IssueComment` (find the marker comment; strip any existing sub-marker block;
  append the new one; edit. If no marker comment exists — e.g. manual deletion
  — create one carrying `COMMENT_MARKER` + the footnote so future upserts still
  find it).
- `app/formatting.py` — `format_failure` grammar fix; new
  `format_failure_footnote(attempts) -> str` returning the sub-marker-delimited
  block.

---

## Part B — finding 3 (enqueue_or_update atomicity)

### B1. Change

Wrap `enqueue_or_update`'s read-branch-write in an explicit `BEGIN IMMEDIATE`
transaction on its single connection, so the whole SELECT → branch → INSERT/
UPDATE is atomic against `claim_next_due` / `finalize_review` / `defer_*` even
if the call is later moved off the event loop (`asyncio.to_thread`). The Python
branching and `_due_after_cooldown` math are unchanged; only the transaction
boundary is added. The existing `store.py:96-100` comment (documenting the
fragile invariant) is replaced with one describing the enforced guarantee.

Concrete shape (explicit begin/commit/rollback/close so a lock is never leaked
on error, sidestepping Python `sqlite3`'s implicit-BEGIN quirk):

```python
conn = _connect()
try:
    conn.execute("BEGIN IMMEDIATE")
    # ... existing SELECT + branch + INSERT/UPDATE (unchanged) ...
    conn.commit()
    return ticket_id
except BaseException:
    conn.rollback()
    raise
finally:
    conn.close()
```

### B2. Deadlock analysis (verified against the code)

`BEGIN IMMEDIATE` **cannot introduce a deadlock** here, and removes the one
classic SQLite deadlock:

- **One lockable resource, one connection per transaction.** Every `store.py`
  function is a single `with _connect()` on the single `queue.db`, and
  `enqueue_or_update`'s transaction body calls only `_due_after_cooldown` (pure
  Python — no DB). A deadlock needs a circular wait over ≥2 resources acquired
  in different orders; with one lock hierarchy and no nested/second connection,
  no cycle can form.
- **No transaction is held across an `await` or any other wait.** Dispatcher
  store calls are synchronous statements; `attempt_review` (which awaits
  network I/O) touches no DB. So no connection holds a write lock while blocked
  on anything else.
- **Contention → bounded wait then a clean error, never a hang.** The default
  5 s busy-timeout means a second writer waits up to 5 s for the RESERVED lock,
  then raises `OperationalError("database is locked")`. Transactions here are
  sub-millisecond, so real waiting is negligible.
- **It removes a deadlock.** The current deferred pattern is the SQLite footgun
  (two connections each hold a SHARED read lock, then both try to upgrade to
  write → mutual `SQLITE_BUSY`). `BEGIN IMMEDIATE` takes the write lock before
  the read, so the loser blocks up front holding nothing.

### B3. Invariants that keep B2 true (stated in code comments + enforced by the plan)

- The `BEGIN IMMEDIATE` transaction body must never open a second connection or
  call another `store` function (would self-deadlock the same thread against
  its own write lock). `_due_after_cooldown` being pure preserves this.
- `commit`/`rollback` + `conn.close()` in `finally` so the write lock is always
  released, even on error.
- All other `store` transactions stay short and open-and-close (no long-lived
  read transaction elsewhere that could starve the commit-time upgrade) — true
  today.

---

## 3. Testing (deterministic-first; injected clock; stubbed GitHub)

Extends the existing layers (`tests/test_dispatcher.py`,
`tests/test_github_app.py`, `tests/test_placeholder_formatting.py`,
`tests/test_queue_store.py`). All new tests use an injected `now` and stub the
`github_app` boundary — no network, no real sleeps.

**Dispatcher (Finding 1):**
- Pre-flight gate, `last_reviewed_at` set → ticket deferred, **no** placeholder
  posted (good review preserved).
- Pre-flight gate, `last_reviewed_at` None → placeholder posted (unchanged).
- `ReviewRateLimited` outcome, `last_reviewed_at` set → deferred, no placeholder.

**Dispatcher (Finding 2):**
- Terminal hard-stop, `last_reviewed_at` set → `append_review_footnote` called,
  `upsert_comment` (overwrite) **not** called, ticket `failed`.
- Terminal hard-stop, `last_reviewed_at` None → `upsert_comment` with
  `format_failure` called (overwrite), ticket `failed`.
- Terminal hard-stop, notice post raises → ticket **deferred** (not `failed`),
  backoff applied (double-failure fix).

**github_app:**
- `append_review_footnote` appends under the marker comment; a second call
  **replaces** the prior footnote block (asserts no stacking — body contains
  exactly one `FAIL_NOTE_START`); no marker comment present → creates one
  carrying `COMMENT_MARKER`.

**formatting:**
- `format_failure` / `format_failure_footnote`: "1 attempt" vs "3 attempts";
  the footnote contains both sub-markers.

**store (Finding 3):**
- All existing `enqueue_or_update` behaviors preserved under the transaction
  (regression: the per-state re-review tests still pass).
- A threaded contention test: two threads enqueue/mutate the same PR behind a
  `threading.Barrier`; assert the final row is consistent and no exception
  escapes (serialization smoke test — noted as such, since a true race needs
  threads and is inherently timing-dependent).

## 4. Documentation

`SPEC.md` §12: add the "never downgrade a good visible review" rule (guarded by
`last_reviewed_at`) covering both the placeholder gate and the terminal-failure
footnote, and document `enqueue_or_update`'s `BEGIN IMMEDIATE` atomicity
guarantee (replacing the current "safe only synchronously" caveat).

## 5. Out of scope

`comment_id` population / the "ping comment" future feature; staleness or
push-count tracking; author notification on terminal failure; multi-instance
operation (Finding 3 removes a landmine, it does not add multi-instance
support).
