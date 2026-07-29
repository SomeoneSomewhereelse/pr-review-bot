# Dispatcher Follow-ups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the review-queue dispatcher a real failure guardrail (per-ticket exponential backoff + hard-stop-and-notify) and a single coherent re-review policy (dirty-flag self-re-enqueue for pushes during a run, ride-out for pushes during a wait, and a per-PR cooldown).

**Architecture:** Split the two conflated wait-types — `RateLimited` stays a per-provider `blocked_until` gate (uncounted, floored against degenerate `Retry-After`); hard failures become per-ticket deferrals with exponential backoff and a terminal cutoff. Re-review triggering becomes explicit per ticket state in `enqueue_or_update`, with a dirty flag resolved atomically at completion by a new `finalize_review`.

**Tech Stack:** Python ≥3.12, stdlib `sqlite3`, Pydantic-settings, `pytest`/`pytest-asyncio` (`asyncio_mode = "auto"`), `uv`.

## Global Constraints

- Python `>=3.12`; managed with `uv`. Run tests: `uv run pytest`; lint: `uv run ruff check .`.
- `ruff` line-length **100**.
- `asyncio_mode = "auto"` — async tests need no decorator.
- **No new runtime dependency** — everything is stdlib (`sqlite3`, `random`, `datetime`).
- All timestamps are timezone-aware **UTC**, persisted as ISO-8601 strings (`datetime.isoformat()`), compared as text in SQL. Test helpers pass `now` in explicitly (injected clock — no real sleeps).
- **No secret is ever logged or shown.** The user-visible failure comment shows only the attempt count, never raw exception text.
- **RateLimited defers are per-provider and NOT counted toward the hard stop; hard failures are per-ticket and counted.** This split is the spine of the design — do not merge the two paths.
- **Jitter is off by default** (`dispatcher_backoff_jitter_seconds = 0.0`) and comes through an injectable seam (`dispatcher._jitter`) so all tests are deterministic.
- Full design: `docs/superpowers/specs/2026-07-28-dispatcher-followups-design.md`. Motivating findings: `docs/2026-07-28-dispatcher-followups.md`.

## File Structure

| File | Responsibility | Created / Modified |
|---|---|---|
| `app/config.py` | Six new dispatcher settings | Modify |
| `app/queue/dispatcher.py` | `compute_backoff` + `_jitter`; `process_next_due` rewrite (failure backoff/hard-stop, RL floor, finalize completion) | Modify |
| `app/queue/store.py` | Two new columns + migration; `defer_rate_limited`/`defer_failed`; `finalize_review` + `_due_after_cooldown`; `enqueue_or_update` per-state rewrite; `recover_on_startup` flag clear; retire `defer`/`mark_done` | Modify |
| `app/formatting.py` | `format_failure(pr_number, attempts)` | Modify |
| `SPEC.md` | §12 updated to describe the designed backoff + re-review/cooldown behavior + new config | Modify |
| `tests/test_dispatcher_backoff.py` | Task 1 — `compute_backoff` unit tests | Create |
| `tests/test_queue_store.py` | Tasks 2–5 — schema, defers, finalize, enqueue policy | Modify |
| `tests/test_placeholder_formatting.py` | Task 6 — `format_failure` test | Modify |
| `tests/test_dispatcher.py` | Task 7 — failure/RL/dirty-review behavior | Modify |

**Task dependency order:** 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8. The codebase stays green at every commit: the old `store.defer`/`store.mark_done` remain callable until Task 7 migrates the dispatcher off them and removes them in the same task.

---

### Task 1: Config vars + `compute_backoff` pure function

**Files:**
- Modify: `app/config.py`, `app/queue/dispatcher.py`
- Test: `tests/test_dispatcher_backoff.py`

**Interfaces:**
- Produces:
  - Settings fields: `dispatcher_failure_base_backoff_seconds: float = 2.0`, `dispatcher_failure_max_backoff_seconds: float = 300.0`, `dispatcher_max_failure_attempts: int = 5`, `dispatcher_min_retry_after_seconds: float = 1.0`, `dispatcher_backoff_jitter_seconds: float = 0.0`, `dispatcher_rereview_cooldown_seconds: float = 300.0`
  - `dispatcher.compute_backoff(attempts: int, jitter: float = 0.0) -> float`
  - `dispatcher._jitter() -> float` (injectable seam; returns 0.0 unless the jitter config > 0)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dispatcher_backoff.py
"""Pure exponential-backoff math for hard-failure retries (jitter injected)."""
from __future__ import annotations

import pytest

from app.config import settings
from app.queue import dispatcher


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(settings, "dispatcher_failure_base_backoff_seconds", 2.0)
    monkeypatch.setattr(settings, "dispatcher_failure_max_backoff_seconds", 300.0)


def test_first_attempt_is_base():
    assert dispatcher.compute_backoff(1, jitter=0.0) == 2.0


def test_backoff_doubles_each_attempt():
    assert dispatcher.compute_backoff(2, jitter=0.0) == 4.0
    assert dispatcher.compute_backoff(3, jitter=0.0) == 8.0
    assert dispatcher.compute_backoff(4, jitter=0.0) == 16.0


def test_backoff_is_capped():
    # 2 * 2**19 would be ~1M; capped at 300.
    assert dispatcher.compute_backoff(20, jitter=0.0) == 300.0


def test_jitter_is_added_on_top():
    assert dispatcher.compute_backoff(1, jitter=5.0) == 7.0


def test_jitter_seam_returns_zero_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "dispatcher_backoff_jitter_seconds", 0.0)
    assert dispatcher._jitter() == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_dispatcher_backoff.py -v`
Expected: FAIL — `AttributeError: module 'app.queue.dispatcher' has no attribute 'compute_backoff'`.

- [ ] **Step 3: Add the config fields**

In `app/config.py`, add to `Settings` after `default_retry_after_seconds`:

```python
    dispatcher_failure_base_backoff_seconds: float = 2.0
    dispatcher_failure_max_backoff_seconds: float = 300.0
    dispatcher_max_failure_attempts: int = 5
    dispatcher_min_retry_after_seconds: float = 1.0
    dispatcher_backoff_jitter_seconds: float = 0.0
    dispatcher_rereview_cooldown_seconds: float = 300.0
```

- [ ] **Step 4: Add `compute_backoff` + `_jitter` to `app/queue/dispatcher.py`**

Add `import random` to the imports, then (near the top, after `logger`):

```python
def _jitter() -> float:
    """Injectable jitter source — 0.0 unless dispatcher_backoff_jitter_seconds > 0.

    Kept as a module-level seam so tests monkeypatch it to a constant and the
    whole system stays deterministic; a future multi-instance deployment sets
    the config > 0 to spread retries without a code change.
    """
    jitter_max = settings.dispatcher_backoff_jitter_seconds
    if jitter_max <= 0:
        return 0.0
    return random.uniform(0.0, jitter_max)


def compute_backoff(attempts: int, jitter: float = 0.0) -> float:
    """Exponential backoff for a hard-failure retry: min(base*2^(n-1), cap) + jitter.

    ``attempts`` is the 1-based per-ticket hard-failure count (first failure -> base).
    """
    base = settings.dispatcher_failure_base_backoff_seconds
    cap = settings.dispatcher_failure_max_backoff_seconds
    return min(base * 2 ** (attempts - 1), cap) + jitter
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_dispatcher_backoff.py -v`
Expected: PASS (5 tests).

- [ ] **Step 6: Lint + full suite (no regressions)**

Run: `uv run ruff check . && uv run pytest -q`
Expected: no lint errors; suite green (existing behavior unchanged — the new function is not wired in yet).

- [ ] **Step 7: Commit**

```bash
git add app/config.py app/queue/dispatcher.py tests/test_dispatcher_backoff.py
git commit -m "feat(queue): backoff config + compute_backoff pure function"
```

---

### Task 2: Store schema — `rereview_requested` + `last_reviewed_at` columns with migration

**Files:**
- Modify: `app/queue/store.py`
- Test: `tests/test_queue_store.py`

**Interfaces:**
- Produces: `Ticket` gains `rereview_requested: int` and `last_reviewed_at: str | None`; `init_db()` adds the two columns to a pre-existing table idempotently.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_queue_store.py`:

```python
def test_new_ticket_has_rereview_and_last_reviewed_defaults():
    tid = _enqueue()
    t = store.get_ticket(tid)
    assert t.rereview_requested == 0
    assert t.last_reviewed_at is None


def test_init_db_migrates_a_pre_existing_table_missing_new_columns(tmp_path, monkeypatch):
    import sqlite3

    db = str(tmp_path / "old.db")
    monkeypatch.setattr(settings, "queue_db_path", db)
    # Create an OLD-shape table without the two new columns.
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE tickets (
                id INTEGER PRIMARY KEY, repo_full_name TEXT NOT NULL,
                pr_number INTEGER NOT NULL, head_sha TEXT, status TEXT NOT NULL,
                provider TEXT NOT NULL, not_before TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, comment_id INTEGER,
                enqueued_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(repo_full_name, pr_number)
            )
            """
        )
    store.init_db()  # must add the missing columns, not crash
    cols = _column_names(db)
    assert "rereview_requested" in cols
    assert "last_reviewed_at" in cols
```

Add this helper near the top of the test module (after the imports):

```python
def _column_names(db_path: str) -> set[str]:
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute("PRAGMA table_info(tickets)")}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_queue_store.py::test_new_ticket_has_rereview_and_last_reviewed_defaults tests/test_queue_store.py::test_init_db_migrates_a_pre_existing_table_missing_new_columns -v`
Expected: FAIL — `AttributeError: 'Ticket' object has no attribute 'rereview_requested'` / migration column missing.

- [ ] **Step 3: Update schema, `Ticket`, and `init_db`**

In `app/queue/store.py`, extend `_SCHEMA`'s column list (before the `UNIQUE(...)` line):

```python
    rereview_requested INTEGER NOT NULL DEFAULT 0,
    last_reviewed_at TEXT,
```

Add the two fields to the `Ticket` dataclass (order doesn't matter — `_row_to_ticket` maps by name):

```python
    rereview_requested: int
    last_reviewed_at: str | None
```

Add an idempotent migration and call it from `init_db`:

```python
def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the original schema, if missing (idempotent)."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(tickets)")}
    if "rereview_requested" not in existing:
        conn.execute(
            "ALTER TABLE tickets ADD COLUMN rereview_requested INTEGER NOT NULL DEFAULT 0"
        )
    if "last_reviewed_at" not in existing:
        conn.execute("ALTER TABLE tickets ADD COLUMN last_reviewed_at TEXT")


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        _ensure_columns(conn)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_queue_store.py -v`
Expected: PASS — the two new tests pass and the existing store tests still pass (new columns default cleanly).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check .
git add app/queue/store.py tests/test_queue_store.py
git commit -m "feat(queue): add rereview_requested + last_reviewed_at columns with migration"
```

---

### Task 3: Store — split `defer` into `defer_rate_limited` + `defer_failed`

**Files:**
- Modify: `app/queue/store.py`
- Test: `tests/test_queue_store.py`

**Interfaces:**
- Produces:
  - `defer_rate_limited(ticket_id: int, not_before: str, now: str) -> None` — sets `status='deferred'`, `not_before`; **does not** change `attempts`.
  - `defer_failed(ticket_id: int, not_before: str, now: str) -> None` — sets `status='deferred'`, `not_before`, `attempts = attempts + 1`.
- Note: the old `defer(...)` stays defined for now (the dispatcher still calls it until Task 7).

- [ ] **Step 1: Write the failing test**

In `tests/test_queue_store.py`, replace `test_defer_increments_attempts` with the two tests below, and update `test_deferred_ticket_not_claimed_before_not_before` to use `defer_rate_limited`:

```python
def test_deferred_ticket_not_claimed_before_not_before():
    tid = _enqueue()
    store.claim_next_due(now=T0)          # -> running
    store.defer_rate_limited(tid, not_before=FUTURE, now=T0)
    assert store.claim_next_due(now=T1) is None            # not yet due
    assert store.claim_next_due(now=FUTURE).id == tid       # due now


def test_defer_rate_limited_does_not_increment_attempts():
    tid = _enqueue()
    store.claim_next_due(now=T0)
    store.defer_rate_limited(tid, not_before=FUTURE, now=T0)
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.attempts == 0


def test_defer_failed_increments_attempts():
    tid = _enqueue()
    store.claim_next_due(now=T0)
    store.defer_failed(tid, not_before=FUTURE, now=T0)
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.attempts == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_queue_store.py -k "defer" -v`
Expected: FAIL — `AttributeError: module 'app.queue.store' has no attribute 'defer_rate_limited'`.

- [ ] **Step 3: Add the two methods to `app/queue/store.py`**

```python
def defer_rate_limited(ticket_id: int, not_before: str, now: str) -> None:
    """Per-provider rate-limit deferral. Does NOT count toward the hard stop."""
    with _connect() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'deferred', not_before = ?, updated_at = ? WHERE id = ?",
            (not_before, now, ticket_id),
        )


def defer_failed(ticket_id: int, not_before: str, now: str) -> None:
    """Per-ticket hard-failure deferral. Increments attempts (drives backoff + hard stop)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'deferred', not_before = ?, "
            "attempts = attempts + 1, updated_at = ? WHERE id = ?",
            (not_before, now, ticket_id),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_queue_store.py -v`
Expected: PASS (new defer tests pass; the rest still green — `defer` still exists).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check .
git add app/queue/store.py tests/test_queue_store.py
git commit -m "feat(queue): split defer into defer_rate_limited + defer_failed"
```

---

### Task 4: Store — `finalize_review` + `_due_after_cooldown`

**Files:**
- Modify: `app/queue/store.py`
- Test: `tests/test_queue_store.py`

**Interfaces:**
- Produces:
  - `_due_after_cooldown(last_reviewed_at: str | None, now: str, cooldown_seconds: float) -> tuple[str, str | None]` — returns `("deferred", <iso>)` while cooling down, else `("pending", None)`.
  - `finalize_review(ticket_id: int, now: str, rereview_not_before: str, comment_id: int | None = None) -> None` — sets `last_reviewed_at=now`, `comment_id`; if `rereview_requested=1` → `status='deferred'`, `not_before=rereview_not_before`, `attempts=0`, flag cleared; else → `status='done'`.
- Note: `mark_done(...)` stays defined for now (removed in Task 7).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_queue_store.py` (and add `T_COOL = "2026-01-01T12:05:00+00:00"` beside the other time constants):

```python
def test_finalize_review_without_flag_marks_done():
    tid = _enqueue()
    store.claim_next_due(now=T0)          # -> running
    store.finalize_review(tid, now=T1, rereview_not_before=T_COOL, comment_id=99)
    t = store.get_ticket(tid)
    assert t.status == "done"
    assert t.comment_id == 99
    assert t.last_reviewed_at == T1
    assert t.not_before is None


def test_finalize_review_with_flag_re_arms_deferred_at_cooldown_and_resets_attempts():
    tid = _enqueue()
    store.claim_next_due(now=T0)          # -> running
    store.defer_failed(tid, not_before=T0, now=T0)   # attempts -> 1
    store.claim_next_due(now=T0)          # -> running again
    # Simulate a push during the run setting the dirty flag:
    import sqlite3
    with sqlite3.connect(settings.queue_db_path) as conn:
        conn.execute("UPDATE tickets SET rereview_requested = 1 WHERE id = ?", (tid,))
    store.finalize_review(tid, now=T1, rereview_not_before=T_COOL)
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.not_before == T_COOL
    assert t.attempts == 0
    assert t.rereview_requested == 0
    assert t.last_reviewed_at == T1


def test_due_after_cooldown_branches():
    assert store._due_after_cooldown(None, T1, 300.0) == ("pending", None)
    # last review at T0 (12:00:00), cooldown 300s -> due at 12:05:00
    status, nb = store._due_after_cooldown(T0, T1, 300.0)   # T1 is 12:00:01, still cooling
    assert status == "deferred"
    assert nb == T_COOL
    assert store._due_after_cooldown(T0, FUTURE, 300.0) == ("pending", None)  # long past cooldown
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_queue_store.py -k "finalize or cooldown" -v`
Expected: FAIL — `AttributeError: module 'app.queue.store' has no attribute 'finalize_review'`.

- [ ] **Step 3: Implement in `app/queue/store.py`**

Add `from datetime import datetime, timedelta` to the imports, then:

```python
def _due_after_cooldown(
    last_reviewed_at: str | None, now: str, cooldown_seconds: float
) -> tuple[str, str | None]:
    """Decide re-arm state honoring the per-PR cooldown (keyed on last completed review).

    Returns ('deferred', <not_before>) while still cooling down, else ('pending', None).
    """
    if last_reviewed_at is None:
        return ("pending", None)
    due = datetime.fromisoformat(last_reviewed_at) + timedelta(seconds=cooldown_seconds)
    if datetime.fromisoformat(now) < due:
        return ("deferred", due.isoformat())
    return ("pending", None)


def finalize_review(
    ticket_id: int, now: str, rereview_not_before: str, comment_id: int | None = None
) -> None:
    """Finalize a completed review, resolving the dirty flag in one statement.

    Always records last_reviewed_at + comment_id. If a push set rereview_requested
    during the run, re-arm to 'deferred' at rereview_not_before (= now + cooldown)
    with a fresh attempts budget; otherwise mark 'done'.
    """
    with _connect() as conn:
        conn.execute(
            """
            UPDATE tickets SET
              last_reviewed_at   = :now,
              comment_id         = :comment_id,
              status             = CASE WHEN rereview_requested = 1 THEN 'deferred' ELSE 'done' END,
              not_before         = CASE WHEN rereview_requested = 1 THEN :rnb ELSE NULL END,
              attempts           = CASE WHEN rereview_requested = 1 THEN 0 ELSE attempts END,
              rereview_requested = 0,
              updated_at         = :now
            WHERE id = :id
            """,
            {"now": now, "comment_id": comment_id, "rnb": rereview_not_before, "id": ticket_id},
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_queue_store.py -v`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check .
git add app/queue/store.py tests/test_queue_store.py
git commit -m "feat(queue): finalize_review + cooldown helper"
```

---

### Task 5: Store — `enqueue_or_update` per-state re-review policy + `recover_on_startup` flag clear

**Files:**
- Modify: `app/queue/store.py`
- Test: `tests/test_queue_store.py`

**Interfaces:**
- Consumes: `_due_after_cooldown` (Task 4), `settings.dispatcher_rereview_cooldown_seconds` (Task 1).
- Produces: `enqueue_or_update(...)` implements: `pending`→update sha, stay pending; `deferred`→ride out (update sha only); `running`→update sha + set `rereview_requested=1`; `done`/`failed`→re-arm via cooldown helper, reset `attempts=0`. `recover_on_startup` also clears `rereview_requested`. Return type unchanged (`int` ticket id).

- [ ] **Step 1: Write the failing test**

In `tests/test_queue_store.py`: the existing `test_enqueue_same_pr_collapses_and_updates_head_sha` (pending case) still holds. Replace `test_mark_failed_ticket_is_re_armed_to_pending_by_a_fresh_push` with the cooldown-aware version and add the deferred/running/cooldown cases:

```python
def test_push_during_deferred_rides_out_keeping_not_before():
    tid = _enqueue(sha="sha1")
    store.claim_next_due(now=T0)                       # -> running
    store.defer_rate_limited(tid, not_before=FUTURE, now=T0)   # provider wait
    store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=1, head_sha="sha2", provider="groq", now=T1
    )
    t = store.get_ticket(tid)
    assert t.status == "deferred"       # not reset to pending
    assert t.not_before == FUTURE       # provider clock NOT shortened
    assert t.head_sha == "sha2"         # latest commit recorded


def test_push_during_running_sets_rereview_flag_and_keeps_running():
    tid = _enqueue(sha="sha1")
    store.claim_next_due(now=T0)                       # -> running
    store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=1, head_sha="sha2", provider="groq", now=T1
    )
    t = store.get_ticket(tid)
    assert t.status == "running"
    assert t.rereview_requested == 1
    assert t.head_sha == "sha2"


def test_push_to_done_ticket_within_cooldown_re_arms_deferred(monkeypatch):
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    tid = _enqueue(sha="sha1")
    store.claim_next_due(now=T0)
    store.finalize_review(tid, now=T0, rereview_not_before=T_COOL)  # done, last_reviewed_at=T0
    store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=1, head_sha="sha2", provider="groq", now=T1
    )
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.not_before == T_COOL       # last_reviewed_at(T0) + 300s
    assert t.attempts == 0


def test_push_to_done_ticket_past_cooldown_re_arms_pending(monkeypatch):
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    tid = _enqueue(sha="sha1")
    store.claim_next_due(now=T0)
    store.finalize_review(tid, now=T0, rereview_not_before=T_COOL)
    store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=1, head_sha="sha2", provider="groq", now=FUTURE
    )
    t = store.get_ticket(tid)
    assert t.status == "pending"
    assert t.not_before is None


def test_recover_on_startup_clears_rereview_flag():
    tid = _enqueue()
    store.claim_next_due(now=T0)                       # -> running
    import sqlite3
    with sqlite3.connect(settings.queue_db_path) as conn:
        conn.execute("UPDATE tickets SET rereview_requested = 1 WHERE id = ?", (tid,))
    store.recover_on_startup(now=T1)
    t = store.get_ticket(tid)
    assert t.status == "pending"
    assert t.rereview_requested == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_queue_store.py -k "push_during or push_to or recover" -v`
Expected: FAIL — current `enqueue_or_update` resets deferred→pending and drops the running push; `recover_on_startup` doesn't clear the flag.

- [ ] **Step 3: Rewrite `enqueue_or_update` and update `recover_on_startup`**

Replace `enqueue_or_update` in `app/queue/store.py` with the per-state version:

```python
def enqueue_or_update(
    *, repo_full_name: str, pr_number: int, head_sha: str | None, provider: str, now: str
) -> int:
    """Enqueue a review ticket, applying the per-state re-review policy.

    - no row        -> insert 'pending'
    - 'pending'     -> update head_sha, stay pending (first review not yet run)
    - 'deferred'    -> ride out: update head_sha only; keep status/not_before
                       (a push cannot shorten a provider/cooldown wait)
    - 'running'     -> update head_sha + set rereview_requested (dirty flag)
    - 'done'/'failed' -> re-arm via cooldown helper; reset attempts to 0
    """
    cooldown = settings.dispatcher_rereview_cooldown_seconds
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM tickets WHERE repo_full_name = ? AND pr_number = ?",
            (repo_full_name, pr_number),
        ).fetchone()

        if row is None:
            conn.execute(
                """
                INSERT INTO tickets
                  (repo_full_name, pr_number, head_sha, status, provider,
                   not_before, attempts, comment_id, enqueued_at, updated_at,
                   rereview_requested, last_reviewed_at)
                VALUES (?, ?, ?, 'pending', ?, NULL, 0, NULL, ?, ?, 0, NULL)
                ON CONFLICT(repo_full_name, pr_number) DO NOTHING
                """,
                (repo_full_name, pr_number, head_sha, provider, now, now),
            )
            row = conn.execute(
                "SELECT id FROM tickets WHERE repo_full_name = ? AND pr_number = ?",
                (repo_full_name, pr_number),
            ).fetchone()
            return int(row["id"])

        status = row["status"]
        ticket_id = int(row["id"])

        if status == "running":
            conn.execute(
                "UPDATE tickets SET head_sha = ?, rereview_requested = 1, updated_at = ? WHERE id = ?",
                (head_sha, now, ticket_id),
            )
        elif status in ("pending", "deferred"):
            # pending: stays pending. deferred: ride out (keep status + not_before).
            conn.execute(
                "UPDATE tickets SET head_sha = ?, updated_at = ? WHERE id = ?",
                (head_sha, now, ticket_id),
            )
        else:  # 'done' or 'failed' -> re-arm, honoring the cooldown, fresh attempts budget
            new_status, not_before = _due_after_cooldown(row["last_reviewed_at"], now, cooldown)
            conn.execute(
                "UPDATE tickets SET head_sha = ?, status = ?, not_before = ?, "
                "attempts = 0, rereview_requested = 0, updated_at = ? WHERE id = ?",
                (head_sha, new_status, not_before, now, ticket_id),
            )
        return ticket_id
```

Update `recover_on_startup` to clear the flag:

```python
def recover_on_startup(now: str) -> None:
    """Reset any ticket interrupted mid-review (crash) back to pending, clearing the
    dirty flag (the fresh pending review already covers the latest commit)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'pending', rereview_requested = 0, updated_at = ? "
            "WHERE status = 'running'",
            (now,),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_queue_store.py -v`
Expected: PASS. (Existing `test_enqueue_creates_pending_ticket`, `test_enqueue_same_pr_collapses_and_updates_head_sha`, claim/fifo tests still green.)

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check .
git add app/queue/store.py tests/test_queue_store.py
git commit -m "feat(queue): per-state re-review policy (ride-out + dirty flag + cooldown)"
```

---

### Task 6: Formatting — `format_failure`

**Files:**
- Modify: `app/formatting.py`
- Test: `tests/test_placeholder_formatting.py`

**Interfaces:**
- Produces: `format_failure(pr_number: int, attempts: int) -> str` — marker-prefixed Markdown, states the review failed after N attempts, **no raw error text**.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_placeholder_formatting.py`:

```python
def test_format_failure_has_marker_pr_and_attempts_no_error_text():
    from app.formatting import format_failure

    body = format_failure(pr_number=42, attempts=5)
    assert COMMENT_MARKER in body
    assert "PR #42" in body
    assert "5" in body                       # attempt count surfaced
    assert "traceback" not in body.lower()   # no raw error/exception text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_placeholder_formatting.py -k failure -v`
Expected: FAIL — `ImportError: cannot import name 'format_failure'`.

- [ ] **Step 3: Add `format_failure` to `app/formatting.py`**

```python
def format_failure(pr_number: int, attempts: int) -> str:
    """Marker-prefixed comment shown when a review is abandoned after repeated
    hard failures. Shows only the attempt count — never raw exception text
    (secrets hygiene). The marker edits any existing review/placeholder in place.
    """
    header = f"## 🤖 Automated Code Review — PR #{pr_number}\n"
    note = (
        f"❌ Automated review could not be completed after {attempts} attempts "
        "due to a service error. It will retry automatically on the next push."
    )
    return f"{COMMENT_MARKER}\n{header}\n_{note}_\n"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_placeholder_formatting.py -v`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check .
git add app/formatting.py tests/test_placeholder_formatting.py
git commit -m "feat(formatting): hard-failure notification comment"
```

---

### Task 7: Dispatcher — `process_next_due` rewrite (backoff/hard-stop, RL floor, finalize, dirty re-review) + retire old store methods

**Files:**
- Modify: `app/queue/dispatcher.py`, `app/queue/store.py`
- Test: `tests/test_dispatcher.py`

**Interfaces:**
- Consumes: `compute_backoff`/`_jitter` (Task 1), `store.defer_rate_limited`/`defer_failed` (Task 3), `store.finalize_review` (Task 4), `format_failure` (Task 6), `settings.dispatcher_min_retry_after_seconds` / `dispatcher_max_failure_attempts` / `dispatcher_rereview_cooldown_seconds`.
- Produces: `process_next_due` with `StepResult.action` meaning: `"deferred"` = RateLimited OR a *retryable* hard failure; `"failed"` = *terminal* hard-stop only; `"ran"` = completed; `"idle"` = nothing due. Removes `store.defer` and `store.mark_done`.

- [ ] **Step 1: Update the existing dispatcher tests + add the new behavior tests**

In `tests/test_dispatcher.py`: **replace** `test_non_rate_limited_exception_marks_ticket_failed_not_stuck_running` and `test_failed_ticket_re_armed_to_pending_by_a_fresh_push` with the versions below, and **add** the four new tests. (The `test_completed_ticket_runs_and_marks_done`, `test_rate_limited_ticket_defers_posts_placeholder_and_blocks`, `test_blocked_provider_*`, `test_daily_wall_*` tests remain valid unchanged.)

```python
async def test_first_hard_failure_defers_with_backoff_not_terminal(monkeypatch):
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_failure_base_backoff_seconds", 2.0)
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 5)
    monkeypatch.setattr(dispatcher, "_jitter", lambda: 0.0)
    tid = _enqueue(pr=5)

    async def boom(repo, pr):
        raise RuntimeError("github api exploded")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"          # retryable, NOT terminal
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.attempts == 1
    assert t.not_before == (NOW + timedelta(seconds=2)).isoformat()  # base backoff


async def test_hard_stop_marks_failed_and_posts_failure_comment(monkeypatch):
    posted = _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 1)  # first failure is terminal
    tid = _enqueue(pr=8)

    async def boom(repo, pr):
        raise RuntimeError("still broken")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "failed"
    assert store.get_ticket(tid).status == "failed"
    assert posted and posted[0][0] == 8
    assert "could not be completed" in posted[0][1].lower()


async def test_rate_limited_zero_retry_after_is_floored(monkeypatch):
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_min_retry_after_seconds", 1.0)
    tid = _enqueue(pr=9)

    async def rl(repo, pr):
        return orchestrator.ReviewRateLimited(retry_after=0.0)

    monkeypatch.setattr(dispatcher, "attempt_review", rl)

    await dispatcher.process_next_due(NOW)
    t = store.get_ticket(tid)
    assert t.not_before == (NOW + timedelta(seconds=1)).isoformat()   # floored, not now+0
    assert t.attempts == 0                                            # RL not counted
    assert dispatcher._blocked_until["groq"] == NOW + timedelta(seconds=1)


async def test_push_during_running_triggers_one_cooldown_re_review(monkeypatch):
    posted = _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    tid = _enqueue(pr=10)

    async def attempt_then_push(repo, pr):
        # A push lands mid-review -> dirty flag on the running ticket.
        store.enqueue_or_update(
            repo_full_name="owner/repo", pr_number=10, head_sha="sha2",
            provider="groq", now=NOW.isoformat(),
        )
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", attempt_then_push)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "ran"
    t = store.get_ticket(tid)
    assert t.status == "deferred"                                     # re-armed, not done
    assert t.not_before == (NOW + timedelta(seconds=300)).isoformat()  # at cooldown

    # During the cooldown wait: nothing due, and NO placeholder churn.
    posted.clear()
    assert (await dispatcher.process_next_due(NOW + timedelta(seconds=60))).action == "idle"
    assert posted == []

    # After cooldown: the re-review runs.
    async def ok(repo, pr):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", ok)
    result = await dispatcher.process_next_due(NOW + timedelta(seconds=300))
    assert result.action == "ran"
    assert store.get_ticket(tid).status == "done"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_dispatcher.py -k "hard or floored or during_running" -v`
Expected: FAIL — current dispatcher marks failed on the first exception, doesn't floor `retry_after`, and doesn't re-review on the dirty flag.

- [ ] **Step 3: Rewrite `process_next_due` in `app/queue/dispatcher.py`**

Update imports: `from app.formatting import format_failure, format_placeholder`. Replace the body of `process_next_due` (keep `claim_next_due` + the `blocked_until` gate; change the gate's `store.defer` to `store.defer_rate_limited`, and rewrite the failure/RateLimited/completion tail):

```python
async def process_next_due(now: datetime) -> StepResult:
    """Claim and process one due ticket. Returns what happened.

    action semantics: "deferred" = RateLimited OR a retryable hard failure;
    "failed" = terminal hard-stop; "ran" = completed; "idle" = nothing due.
    """
    ticket = store.claim_next_due(now.isoformat())
    if ticket is None:
        return StepResult(action="idle")

    provider = settings.llm_provider
    blocked = _blocked_until.get(provider)
    if blocked is not None and now < blocked:
        store.defer_rate_limited(ticket.id, not_before=blocked.isoformat(), now=now.isoformat())
        await _post_placeholder(
            ticket.repo_full_name, ticket.pr_number, (blocked - now).total_seconds(), now
        )
        return StepResult(action="deferred", ticket_id=ticket.id)

    try:
        outcome = await attempt_review(ticket.repo_full_name, ticket.pr_number)
    except Exception as exc:  # noqa: BLE001 - hard failure: back off per-ticket, hard-stop at the cap
        logger.exception("review attempt failed for ticket %s", ticket.id)
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
        backoff = compute_backoff(next_attempt, _jitter())
        until = now + timedelta(seconds=backoff)
        store.defer_failed(ticket.id, not_before=until.isoformat(), now=now.isoformat())
        return StepResult(action="deferred", ticket_id=ticket.id)

    if isinstance(outcome, ReviewRateLimited):
        wait = max(outcome.retry_after, settings.dispatcher_min_retry_after_seconds)
        until = now + timedelta(seconds=wait)
        _blocked_until[provider] = until
        store.defer_rate_limited(ticket.id, not_before=until.isoformat(), now=now.isoformat())
        await _post_placeholder(ticket.repo_full_name, ticket.pr_number, wait, now)
        return StepResult(action="deferred", ticket_id=ticket.id)

    rereview_not_before = (
        now + timedelta(seconds=settings.dispatcher_rereview_cooldown_seconds)
    ).isoformat()
    store.finalize_review(ticket.id, now=now.isoformat(), rereview_not_before=rereview_not_before)
    return StepResult(action="ran", ticket_id=ticket.id)
```

- [ ] **Step 4: Retire the now-unused store methods**

In `app/queue/store.py`, delete the `defer(...)` and `mark_done(...)` functions (no caller remains — the dispatcher now uses `defer_rate_limited`/`defer_failed`/`finalize_review`). Keep `mark_failed`. Grep to confirm nothing else references them:

Run: `grep -rn "store.defer\b\|\.mark_done\|store\.defer(" app tests` (expect no hits after the edits below).

If `tests/test_queue_store.py` still has `test_mark_done_sets_status_and_comment_id`, delete it (superseded by `test_finalize_review_without_flag_marks_done` from Task 4).

- [ ] **Step 5: Run the full suite to verify pass**

Run: `uv run pytest -v`
Expected: PASS across all layers.

- [ ] **Step 6: Lint + boot smoke check**

Run: `uv run ruff check . && uv run python -c "from app.main import app; print('import ok')"`
Expected: no lint errors; prints `import ok`.

- [ ] **Step 7: Commit**

```bash
git add app/queue/dispatcher.py app/queue/store.py tests/test_dispatcher.py tests/test_queue_store.py
git commit -m "feat(queue): failure backoff + hard-stop, RL floor, dirty-flag re-review"
```

---

### Task 8: Documentation — SPEC.md §12

**Files:**
- Modify: `SPEC.md`

**Interfaces:** none (docs only).

- [ ] **Step 1: Update the re-review + failure wording in `SPEC.md` §12**

Replace the paragraph describing the `running`-ticket behavior ("… updated on the row for record-keeping, not re-reviewed as part of that ticket. A separate push after the ticket reaches `done`/`failed` …") and the incidental deferred-reset description with the *designed* policy:

- A push during `running` sets a `rereview_requested` flag; on completion the ticket re-arms for exactly one follow-up review of the latest commit (no cancellation).
- A push during `deferred` rides out — it updates `head_sha` but never shortens the provider/cooldown `not_before`.
- A per-PR cooldown (`dispatcher_rereview_cooldown_seconds`, default 300s), keyed on the last *completed* review, spaces re-reviews; the first review and rate-limited/failed tickets are never delayed by it.
- Backoff: `RateLimited` → per-provider `blocked_until`, floored at `dispatcher_min_retry_after_seconds`, uncounted. Hard failures → per-ticket exponential backoff (`compute_backoff`), terminal `failed` + a visible failure comment after `dispatcher_max_failure_attempts`.

- [ ] **Step 2: Update the config list in `SPEC.md` §12**

Add the six new vars (with defaults) to the config paragraph:
`DISPATCHER_FAILURE_BASE_BACKOFF_SECONDS` (2.0), `DISPATCHER_FAILURE_MAX_BACKOFF_SECONDS` (300.0), `DISPATCHER_MAX_FAILURE_ATTEMPTS` (5), `DISPATCHER_MIN_RETRY_AFTER_SECONDS` (1.0), `DISPATCHER_BACKOFF_JITTER_SECONDS` (0.0, off), `DISPATCHER_REREVIEW_COOLDOWN_SECONDS` (300.0).

- [ ] **Step 3: Sanity-check + commit**

Run: `uv run ruff check .` (confirms the tree is still clean; docs don't affect lint)

```bash
git add SPEC.md
git commit -m "docs: document dispatcher backoff + re-review/cooldown policy"
```

---

## Self-Review (completed by plan author)

**1. Spec coverage** — every spec section maps to a task:
- §5.1 two wait mechanisms → Task 3 (split defers) + Task 7 (wiring). §5.2 backoff formula → Task 1. §5.3 jitter hook → Task 1. §5.4 hard-stop + notify → Task 6 (comment) + Task 7 (cutoff). §6.1 per-state re-review → Task 5. §6.2 `finalize_review` dirty flag → Task 4 + Task 7 (rereview_not_before). §6.3 cooldown helper → Task 4. §6.4 silent cooldown → Task 7 test asserts no placeholder. §7.1 schema/migration → Task 2. §7.2 methods (incl. removal of `defer`, `recover_on_startup` flag clear) → Tasks 3/4/5/7. §8 dispatcher pseudocode → Task 7. §9 config → Task 1. §10 `format_failure` → Task 6. §11 edge cases → covered by Task 7 tests (dirty flag read at finalize) + Task 5 (recover clears flag). §12 tests → each task's tests. §13 docs → Task 8.

**2. Placeholder scan** — no "TBD/TODO/handle appropriately"; every code and test step contains runnable content.

**3. Type consistency** — names match across tasks: `compute_backoff(attempts, jitter=0.0)` and `_jitter()` (Task 1) called exactly so in Task 7; `defer_rate_limited`/`defer_failed(ticket_id, not_before, now)` (Task 3) called with those kwargs in Task 7; `finalize_review(ticket_id, now, rereview_not_before, comment_id=None)` (Task 4) called in Task 7 with `rereview_not_before`; `_due_after_cooldown(last_reviewed_at, now, cooldown_seconds) -> (status, not_before)` (Task 4) used in Task 5; `format_failure(pr_number, attempts)` (Task 6) called in Task 7; `StepResult.action` values `{"idle","ran","deferred","failed"}` are exactly those asserted in Task 7 tests, with `"failed"` reserved for the terminal hard-stop.
```
