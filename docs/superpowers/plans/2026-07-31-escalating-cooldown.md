# Escalating Re-review Cooldown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bound cooldown-driven re-review cost by escalating a PR's cooldown geometrically while it keeps churning (capped at 1h), resetting when it goes quiet — without ever abandoning a legitimate PR and without changing normal PRs' behavior.

**Architecture:** A per-PR `cooldown_level` column drives `effective_cooldown(level) = min(base·2^level, cap)`. One level bump happens per *scheduled re-review* at the two re-arm sites — `enqueue_or_update`'s done/failed branch (Site A) and `finalize_review`'s dirty-flag branch (Site B) — and the level resets to 0 when a push arrives after a full quiet window. Escalation is silent (only lengthens `not_before`); `finalize_review` stays a single atomic UPDATE.

**Tech Stack:** Python ≥3.12, stdlib `sqlite3`, Pydantic-settings, `pytest`/`pytest-asyncio` (`asyncio_mode = "auto"`), `uv`.

## Global Constraints

- Python `>=3.12`; managed with `uv`. Run tests: `uv run pytest`; lint: `uv run ruff check .`.
- `ruff` line-length **100**.
- `asyncio_mode = "auto"` — async tests need no decorator.
- **No new runtime dependency** — stdlib only.
- All timestamps are timezone-aware **UTC** ISO-8601 strings; tests pass `now` in explicitly (injected clock — no real sleeps).
- **Silent by design:** escalation only changes a ticket's `not_before`/`cooldown_level`; it posts/edits **no** comment (preserves the cooldown's no-placeholder UX).
- **Never abandon:** a churning PR is throttled, never stopped — no per-PR hard cap, no "give up".
- **Level 0 == today:** `effective_cooldown(0)` must equal the old flat cooldown (`dispatcher_rereview_cooldown_seconds`, 300s) so normal PRs are unaffected.
- **Deadlock invariant (Finding 3, unchanged):** `enqueue_or_update`'s `BEGIN IMMEDIATE` transaction body opens no second connection and calls no other `store` function; `effective_cooldown`/`next_cooldown_level`/`_due_after_cooldown` open no connection, so this holds.
- **Environment note:** this repo is currently driven from WSL — pass **Linux paths** (`/mnt/c/...`) to file tools; `git` runs natively (relative paths in the commands below are fine). See `memory.md`'s `file_path_style` toggle.
- Full design: `docs/superpowers/specs/2026-07-31-escalating-cooldown-design.md`. Finding: `docs/2026-07-29-cooldown-review-invocation-followup.md`. Branch: `fix/comment-visibility` (unmerged; this builds on it).

## File Structure

| File | Responsibility | Modify |
|---|---|---|
| `app/config.py` | Add `dispatcher_rereview_cooldown_max_seconds` | ✓ |
| `app/queue/store.py` | `effective_cooldown`/`next_cooldown_level`/`_MAX_COOLDOWN_LEVEL`; `cooldown_level` column+migration+`Ticket` field; `_due_after_cooldown` refactor (Site A); `finalize_review` param (Site B) | ✓ |
| `app/queue/dispatcher.py` | Compute escalated `rereview_not_before` + pass `rereview_cooldown_level` | ✓ |
| `SPEC.md` | §12 — document the escalating cooldown + new config | ✓ |
| `tests/test_queue_store.py` | Helpers, Site-A, `_due_after_cooldown`, `finalize_review` tests | ✓ |
| `tests/test_dispatcher.py` | Dispatcher escalation test + `finalize_review` call-site fix | ✓ |

**Task order** (green at every commit): 1 (config + pure helpers) → 2 (schema) → 3 (Site A) → 4 (Site B + dispatcher) → 5 (docs). Tasks 3 and 4 both depend on 1+2; 3 sets up its own state via direct SQL so it does **not** depend on `finalize_review`'s Task-4 signature change.

---

### Task 1: Config var + pure escalation helpers

**Files:**
- Modify: `app/config.py`, `app/queue/store.py`
- Test: `tests/test_queue_store.py`

**Interfaces:**
- Produces:
  - Setting `dispatcher_rereview_cooldown_max_seconds: float = 3600.0`
  - `store.effective_cooldown(level: int) -> float` = `min(base·2^min(level,_MAX), cap)`
  - `store.next_cooldown_level(level: int) -> int` = `min(level+1, _MAX)`
  - `store._MAX_COOLDOWN_LEVEL = 30`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_queue_store.py`:

```python
def test_effective_cooldown_escalates_and_caps(monkeypatch):
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_max_seconds", 3600.0)
    assert store.effective_cooldown(0) == 300.0     # level 0 == today's flat cooldown
    assert store.effective_cooldown(1) == 600.0
    assert store.effective_cooldown(2) == 1200.0
    assert store.effective_cooldown(3) == 2400.0
    assert store.effective_cooldown(4) == 3600.0    # 300*16=4800 -> capped
    assert store.effective_cooldown(50) == 3600.0   # capped, no 2**50 blowup


def test_next_cooldown_level_increments_and_guards():
    assert store.next_cooldown_level(0) == 1
    assert store.next_cooldown_level(4) == 5
    assert store.next_cooldown_level(30) == 30      # _MAX_COOLDOWN_LEVEL guard
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_queue_store.py -k "effective_cooldown or next_cooldown_level" -v`
Expected: FAIL — `AttributeError: module 'app.queue.store' has no attribute 'effective_cooldown'`.

- [ ] **Step 3: Add the config var**

In `app/config.py`, add after `dispatcher_rereview_cooldown_seconds`:

```python
    dispatcher_rereview_cooldown_max_seconds: float = 3600.0
```

- [ ] **Step 4: Add the helpers to `app/queue/store.py`**

Add near the top (after the imports / before `enqueue_or_update`):

```python
_MAX_COOLDOWN_LEVEL = 30


def effective_cooldown(level: int) -> float:
    """Escalated per-PR cooldown: min(base * 2^level, cap).

    level 0 -> base (identical to a non-escalating cooldown, so normal PRs are
    unaffected). Each consecutive rapid re-review raises the level, geometrically
    lengthening the next wait, capped at dispatcher_rereview_cooldown_max_seconds.
    """
    base = settings.dispatcher_rereview_cooldown_seconds
    cap = settings.dispatcher_rereview_cooldown_max_seconds
    return min(base * 2 ** min(level, _MAX_COOLDOWN_LEVEL), cap)


def next_cooldown_level(level: int) -> int:
    """Level for the next re-review after a churn re-review (guarded against overflow)."""
    return min(level + 1, _MAX_COOLDOWN_LEVEL)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_queue_store.py -k "effective_cooldown or next_cooldown_level" -v`
Expected: PASS.

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check .
git add app/config.py app/queue/store.py tests/test_queue_store.py
git commit -m "feat(queue): escalating-cooldown config + pure helpers"
```

---

### Task 2: `cooldown_level` column + migration

**Files:**
- Modify: `app/queue/store.py`
- Test: `tests/test_queue_store.py`

**Interfaces:**
- Produces: `Ticket.cooldown_level: int`; a fresh ticket defaults to `0`; `init_db` backfills the column on a pre-existing table.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_queue_store.py`:

```python
def test_new_ticket_has_cooldown_level_zero():
    tid = _enqueue()
    assert store.get_ticket(tid).cooldown_level == 0


def test_init_db_backfills_cooldown_level_on_pre_existing_table(tmp_path, monkeypatch):
    import sqlite3

    db = str(tmp_path / "old.db")
    monkeypatch.setattr(settings, "queue_db_path", db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE tickets (
                id INTEGER PRIMARY KEY, repo_full_name TEXT NOT NULL,
                pr_number INTEGER NOT NULL, head_sha TEXT, status TEXT NOT NULL,
                provider TEXT NOT NULL, not_before TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, comment_id INTEGER,
                enqueued_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                rereview_requested INTEGER NOT NULL DEFAULT 0, last_reviewed_at TEXT,
                UNIQUE(repo_full_name, pr_number)
            )
            """
        )
    store.init_db()
    assert "cooldown_level" in _column_names(db)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_queue_store.py -k "cooldown_level" -v`
Expected: FAIL — `Ticket` has no `cooldown_level` / column absent after migration.

- [ ] **Step 3: Add the column, migration, and dataclass field**

In `app/queue/store.py`:

Add to `_SCHEMA` (before the `UNIQUE(...)` line):

```python
    cooldown_level INTEGER NOT NULL DEFAULT 0,
```

Add to `_ensure_columns` (after the `last_reviewed_at` block):

```python
    if "cooldown_level" not in existing:
        conn.execute("ALTER TABLE tickets ADD COLUMN cooldown_level INTEGER NOT NULL DEFAULT 0")
```

Add to the `Ticket` dataclass:

```python
    cooldown_level: int
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_queue_store.py -v`
Expected: PASS — new tests pass; existing store tests still pass (new column defaults cleanly, `_row_to_ticket` maps it by name).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check .
git add app/queue/store.py tests/test_queue_store.py
git commit -m "feat(queue): add cooldown_level column with migration"
```

---

### Task 3: Site A — escalate/reset in `enqueue_or_update`'s done/failed branch

**Files:**
- Modify: `app/queue/store.py`
- Test: `tests/test_queue_store.py`

**Interfaces:**
- Consumes: `effective_cooldown`/`next_cooldown_level` (Task 1), `cooldown_level` column (Task 2).
- Produces: `_due_after_cooldown(last_reviewed_at: str|None, now: str, level: int) -> tuple[str, str|None, int]` (status, not_before, new_level); `enqueue_or_update`'s done/failed branch writes the new level.

- [ ] **Step 1: Write the failing tests**

In `tests/test_queue_store.py`, **replace** `test_due_after_cooldown_branches` with the version below, add the `_make_done` helper (near the other helpers), and add the two Site-A tests. (`T_400` is `T0 + 400s`.)

```python
T_400 = "2026-01-01T12:06:40+00:00"   # T0 (12:00:00) + 400s


def _make_done(tid, last_reviewed_at, level, now=T0):
    """Directly force a ticket to a completed state (bypasses finalize_review so
    Task 3 tests don't depend on finalize's Task-4 signature)."""
    import sqlite3

    with sqlite3.connect(settings.queue_db_path) as conn:
        conn.execute(
            "UPDATE tickets SET status='done', last_reviewed_at=?, cooldown_level=?, "
            "updated_at=? WHERE id=?",
            (last_reviewed_at, level, now, tid),
        )


def test_due_after_cooldown_branches(monkeypatch):
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_max_seconds", 3600.0)
    # never reviewed -> pending, level 0
    assert store._due_after_cooldown(None, T1, 0) == ("pending", None, 0)
    # within window (level 0 -> 300s; T0=12:00:00, T1=12:00:01) -> deferred, escalate to 1
    assert store._due_after_cooldown(T0, T1, 0) == ("deferred", T_COOL, 1)
    # elapsed -> pending, reset to 0
    assert store._due_after_cooldown(T0, FUTURE, 0) == ("pending", None, 0)
    # at level 1 the window is 600s; a push 400s after T0 is still within -> deferred, escalate to 2
    status, nb, lvl = store._due_after_cooldown(T0, T_400, 1)
    assert status == "deferred"
    assert nb == "2026-01-01T12:10:00+00:00"   # T0 + 600s
    assert lvl == 2


def test_enqueue_push_within_cooldown_escalates_level(monkeypatch):
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_max_seconds", 3600.0)
    tid = _enqueue(sha="sha1")
    _make_done(tid, last_reviewed_at=T0, level=1)   # last review T0, already at level 1 (eff=600s)
    store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=1, head_sha="sha2", provider="groq", now=T_400
    )
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.not_before == "2026-01-01T12:10:00+00:00"   # T0 + 600s
    assert t.cooldown_level == 2                          # escalated
    assert t.head_sha == "sha2"


def test_enqueue_push_after_cooldown_resets_level(monkeypatch):
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_max_seconds", 3600.0)
    tid = _enqueue(sha="sha1")
    _make_done(tid, last_reviewed_at=T0, level=3)   # window eff(3)=2400s (until 12:40)
    store.enqueue_or_update(   # FUTURE = 18:00, well past the window -> quiet -> reset
        repo_full_name="owner/repo", pr_number=1, head_sha="sha2", provider="groq", now=FUTURE
    )
    t = store.get_ticket(tid)
    assert t.status == "pending"
    assert t.not_before is None
    assert t.cooldown_level == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_queue_store.py -k "due_after_cooldown or escalates_level or resets_level" -v`
Expected: FAIL — `_due_after_cooldown` still has the old 3-arg / 2-tuple shape and doesn't touch `cooldown_level`.

- [ ] **Step 3: Refactor `_due_after_cooldown` and Site A**

In `app/queue/store.py`, replace `_due_after_cooldown` with:

```python
def _due_after_cooldown(
    last_reviewed_at: str | None, now: str, level: int
) -> tuple[str, str | None, int]:
    """Re-arm state + next escalation level, honoring the escalating cooldown.

    Churn (still within effective_cooldown(level) of the last completed review)
    -> ('deferred', due, next_cooldown_level(level)). Quiet or never-reviewed
    -> ('pending', None, 0) (escalation resets).
    """
    if last_reviewed_at is None:
        return ("pending", None, 0)
    due = datetime.fromisoformat(last_reviewed_at) + timedelta(seconds=effective_cooldown(level))
    if datetime.fromisoformat(now) < due:
        return ("deferred", due.isoformat(), next_cooldown_level(level))
    return ("pending", None, 0)
```

In `enqueue_or_update`, replace the `else:` (done/failed) branch body:

```python
                else:  # 'done'/'failed' -> re-arm honoring the escalating cooldown
                    new_status, not_before, new_level = _due_after_cooldown(
                        row["last_reviewed_at"], now, row["cooldown_level"]
                    )
                    conn.execute(
                        "UPDATE tickets SET head_sha = ?, status = ?, not_before = ?, "
                        "attempts = 0, rereview_requested = 0, cooldown_level = ?, "
                        "updated_at = ? WHERE id = ?",
                        (head_sha, new_status, not_before, new_level, now, ticket_id),
                    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_queue_store.py -v`
Expected: PASS — new/updated tests pass; existing done/failed re-arm tests still pass (a level-0 ticket's `effective_cooldown(0)` == 300s == the old flat cooldown, so their status/`not_before` assertions are unchanged).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check .
git add app/queue/store.py tests/test_queue_store.py
git commit -m "feat(queue): escalate/reset cooldown_level on push re-arm (Site A)"
```

---

### Task 4: Site B — `finalize_review` level param + dispatcher wiring

**Files:**
- Modify: `app/queue/store.py`, `app/queue/dispatcher.py`
- Test: `tests/test_queue_store.py`, `tests/test_dispatcher.py`

**Interfaces:**
- Consumes: `effective_cooldown`/`next_cooldown_level` (Task 1), `cooldown_level` (Task 2).
- Produces: `finalize_review(ticket_id, now, rereview_not_before, rereview_cooldown_level, comment_id=None)` — on the dirty-flag branch it stores `cooldown_level = rereview_cooldown_level`; the dispatcher computes `rereview_not_before` from `effective_cooldown(ticket.cooldown_level)` and passes `rereview_cooldown_level = next_cooldown_level(ticket.cooldown_level)`.

- [ ] **Step 1: Update existing `finalize_review` call sites + write new tests**

In `tests/test_queue_store.py`, add `rereview_cooldown_level=...` to the four existing `finalize_review` calls and assert the level, then add a dedicated Site-B test:

- Line ~160 (`test_push_to_done_ticket_within_cooldown_re_arms_deferred`): change the setup call to
  `store.finalize_review(tid, now=T0, rereview_not_before=T_COOL, rereview_cooldown_level=0)`.
- Line ~174 (`test_push_to_done_ticket_past_cooldown_re_arms_pending`): same change —
  `store.finalize_review(tid, now=T0, rereview_not_before=T_COOL, rereview_cooldown_level=0)`.
- In `test_finalize_review_without_flag_marks_done`, change the call to
  `store.finalize_review(tid, now=T1, rereview_not_before=T_COOL, rereview_cooldown_level=7, comment_id=99)`
  and add: `assert store.get_ticket(tid).cooldown_level == 0`  # non-dirty -> level unchanged (passed value ignored).
- In `test_finalize_review_with_flag_re_arms_deferred_at_cooldown_and_resets_attempts`, change the call to
  `store.finalize_review(tid, now=T1, rereview_not_before=T_COOL, rereview_cooldown_level=2)`
  and add: `assert store.get_ticket(tid).cooldown_level == 2`  # dirty -> stores passed level.

Add a new test:

```python
def test_finalize_review_dirty_flag_stores_passed_cooldown_level():
    tid = _enqueue()
    store.claim_next_due(now=T0)
    import sqlite3

    with sqlite3.connect(settings.queue_db_path) as conn:
        conn.execute("UPDATE tickets SET rereview_requested = 1 WHERE id = ?", (tid,))
    store.finalize_review(tid, now=T1, rereview_not_before=T_COOL, rereview_cooldown_level=3)
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.not_before == T_COOL
    assert t.cooldown_level == 3
```

In `tests/test_dispatcher.py`, update the `_reviewed_then_pushed` helper's finalize call (line ~263) to
`store.finalize_review(tid, now=NOW.isoformat(), rereview_not_before=NOW.isoformat(), rereview_cooldown_level=0)`,
and add the dispatcher escalation test:

```python
async def test_dispatcher_escalates_cooldown_on_churn_completion(monkeypatch):
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_max_seconds", 3600.0)
    tid = _enqueue(pr=30)
    import sqlite3

    with sqlite3.connect(settings.queue_db_path) as conn:
        conn.execute("UPDATE tickets SET cooldown_level = 1 WHERE id = ?", (tid,))

    async def attempt_then_push(repo, pr):
        store.enqueue_or_update(
            repo_full_name="owner/repo", pr_number=30, head_sha="sha2",
            provider="groq", now=NOW.isoformat(),
        )
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", attempt_then_push)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "ran"
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.not_before == (NOW + timedelta(seconds=600)).isoformat()   # effective_cooldown(1)
    assert t.cooldown_level == 2                                        # next_cooldown_level(1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_queue_store.py tests/test_dispatcher.py -k "cooldown or finalize or reviewed_then_pushed or escalates" -v`
Expected: FAIL — `finalize_review()` has no `rereview_cooldown_level` parameter yet, and the dispatcher doesn't escalate.

- [ ] **Step 3: Add the `finalize_review` param + CASE line**

In `app/queue/store.py`, change `finalize_review`'s signature and add the `cooldown_level` CASE:

```python
def finalize_review(
    ticket_id: int,
    now: str,
    rereview_not_before: str,
    rereview_cooldown_level: int,
    comment_id: int | None = None,
) -> None:
    """Finalize a completed review, resolving the dirty flag in one statement.

    Always records last_reviewed_at + comment_id. If a push set rereview_requested
    during the run, re-arm to 'deferred' at rereview_not_before with a fresh
    attempts budget and store the escalated rereview_cooldown_level; otherwise mark
    'done' and leave the level unchanged (latent — the next push resolves it).
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
              cooldown_level     = CASE WHEN rereview_requested = 1 THEN :new_level ELSE cooldown_level END,
              rereview_requested = 0,
              updated_at         = :now
            WHERE id = :id
            """,
            {
                "now": now,
                "comment_id": comment_id,
                "rnb": rereview_not_before,
                "new_level": rereview_cooldown_level,
                "id": ticket_id,
            },
        )
```

- [ ] **Step 4: Wire the dispatcher to compute the escalated values**

In `app/queue/dispatcher.py`, replace the completion tail of `process_next_due` (the `rereview_not_before = ...` line and the `store.finalize_review(...)` call):

```python
    level = ticket.cooldown_level
    rereview_not_before = (
        now + timedelta(seconds=store.effective_cooldown(level))
    ).isoformat()
    store.finalize_review(
        ticket.id,
        now=now.isoformat(),
        rereview_not_before=rereview_not_before,
        rereview_cooldown_level=store.next_cooldown_level(level),
    )
    return StepResult(action="ran", ticket_id=ticket.id)
```

- [ ] **Step 5: Run the full suite to verify pass**

Run: `uv run pytest -v`
Expected: PASS. In particular `test_push_during_running_triggers_one_cooldown_re_review` still asserts `not_before == NOW + 300s` (a fresh ticket is level 0, `effective_cooldown(0)` == 300s), and the new escalation test passes.

- [ ] **Step 6: Lint + boot smoke check + commit**

```bash
uv run ruff check . && uv run python -c "from app.main import app; print('import ok')"
git add app/queue/store.py app/queue/dispatcher.py tests/test_queue_store.py tests/test_dispatcher.py
git commit -m "feat(queue): escalate cooldown_level on dirty-flag re-review (Site B) + dispatcher"
```

---

### Task 5: Documentation — SPEC.md §12

**Files:**
- Modify: `SPEC.md`

**Interfaces:** none (docs only).

- [ ] **Step 1: Document the escalating cooldown in §12**

In the paragraph describing the per-PR cooldown, add: the cooldown now **escalates** per PR — a `cooldown_level` raises the effective wait geometrically (`effective_cooldown(level) = min(base·2^level, cap)`, `DISPATCHER_REREVIEW_COOLDOWN_MAX_SECONDS` default 3600s) for a PR that keeps being pushed inside each window, resetting to 0 once the PR stays quiet for a full window. Level 0 equals the base cooldown, so normal PRs are unchanged; escalation is silent (only lengthens `not_before`); it bounds a churning PR from ~288 to ~26 reviews/day without ever abandoning it. Note the two escalation sites (`enqueue_or_update` done/failed re-arm, and `finalize_review`'s dirty-flag branch).

- [ ] **Step 2: Add the new config var to §12's config list**

Add `DISPATCHER_REREVIEW_COOLDOWN_MAX_SECONDS` (default `3600.0`) alongside `DISPATCHER_REREVIEW_COOLDOWN_SECONDS`.

- [ ] **Step 3: Sanity-check + commit**

Run: `uv run ruff check .`  (tree still clean; docs don't affect lint)

```bash
git add SPEC.md
git commit -m "docs: document the escalating re-review cooldown"
```

---

## Self-Review (completed by plan author)

**1. Spec coverage** — every spec section maps to a task:
- §3.1 formula/state (`cooldown_level`, `effective_cooldown`, `next_cooldown_level`, `_MAX_COOLDOWN_LEVEL`) → Tasks 1 + 2. §3.2 escalate/reset table: Site A → Task 3, Site B → Task 4, "level 0 == today" verified in Tasks 1/3/4. §3.3 silent → no comment code added anywhere (Tasks 3/4 touch only `store`/`dispatcher` state). §3.4 atomicity: `finalize_review` stays a single UPDATE (Task 4); Site A stays inside `BEGIN IMMEDIATE`; helpers open no connection → deadlock invariant preserved. §4 surface → Tasks 1–5 exactly. §5 testing → each task's tests (pure helpers, schema/migration, `_due_after_cooldown`, Site A escalate+reset, Site B dirty, dispatcher escalation, regression via unchanged existing tests). §6 non-goals respected (no cap/notice/metric; no change to failure ceiling, rate-limit gate, coalescing, ride-out).

**2. Placeholder scan** — no "TBD/TODO/handle appropriately"; every code and test step has runnable content. `_make_done` (Task 3) is deliberately introduced so Task 3 doesn't depend on Task 4's `finalize_review` signature — keeps each commit green.

**3. Type consistency** — names match across tasks: `effective_cooldown(level)->float` and `next_cooldown_level(level)->int` (Task 1) are called in Tasks 3 (`_due_after_cooldown`) and 4 (dispatcher); `_due_after_cooldown(last_reviewed_at, now, level)->(status, not_before, new_level)` (Task 3) is called only in `enqueue_or_update`; `finalize_review(..., rereview_cooldown_level, comment_id=None)` (Task 4) is called with that keyword at all five updated sites; `Ticket.cooldown_level` (Task 2) is read as `ticket.cooldown_level`/`row["cooldown_level"]` in Tasks 3/4. `StepResult.action` values unchanged.
```
