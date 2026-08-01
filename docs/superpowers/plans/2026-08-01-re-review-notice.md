# Re-review Scheduled Notice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fully-silent cooldown/rate-limit wait with a self-cleaning "re-review scheduled ~HH:MM UTC" footnote, unified across every trigger that puts a reviewed ticket into `deferred`, without ever showing it on a ticket that's actually silently retrying after a hard failure.

**Architecture:** A new `'retrying'` ticket status makes `'deferred'` mean exactly "cooldown/rate-limit schedule wait" (never a hard-failure backoff). A new dispatcher-loop sweep (`post_pending_notices`) posts/refreshes the footnote on every `deferred` ticket whose schedule changed since a persisted `notice_not_before` marker was last set; claiming a ticket strips any live footnote immediately, since the wait is over regardless of outcome. Both new GitHub-writing functions route through the existing `comment_id`-first identity resolver.

**Tech Stack:** Python ≥3.12, stdlib `sqlite3`, PyGithub, `pytest`/`pytest-asyncio` (`asyncio_mode = "auto"`), `uv`.

## Global Constraints

- Python `>=3.12`; managed with `uv`. Run tests: `uv run pytest`; lint: `uv run ruff check .`. If `uv` is not on PATH, use `.venv/Scripts/python.exe -m pytest` / `.venv/Scripts/python.exe -m ruff check .` instead.
- `ruff` line-length **100**.
- `asyncio_mode = "auto"` — async tests need no decorator.
- **No new runtime dependency.**
- **No secret is ever logged or shown** — the schedule notice carries only a formatted timestamp.
- **Content: absolute UTC time only** — `"🔄 Re-review scheduled ~HH:MM UTC"`. No relative time, no mention of escalation level or throttling mechanics.
- **`'deferred'` means exactly a cooldown/rate-limit schedule wait.** Hard-failure retry backoff uses a new, distinct status, `'retrying'` — `defer_failed` sets it; `claim_next_due` and `enqueue_or_update`'s ride-out branch treat it identically to `'deferred'` for claimability/push-handling.
- **No new process, no new config** — the sweep runs inside the existing `run_forever` loop; its effective cadence is `dispatcher_idle_sleep_seconds` (already exists).
- **`append_review_footnote` stays completely unchanged** — a new, separate `append_schedule_notice` (identical shape) is added instead, so Finding-1-approved code/tests are not touched.
- **`_strip_existing_footnote(body)`'s signature does not change** — it is generalized internally to recognize either known footnote marker pair, not by adding a parameter.
- **Environment:** driven from WSL — pass Linux paths (`/mnt/c/...`) to file tools; `git` runs natively. Stage only each task's named files (never `git add .`) — a `.gitattributes` enforces LF; don't sweep unrelated files.
- Full design: `docs/superpowers/specs/2026-08-01-re-review-notice-design.md`. Branch: create `fix/re-review-notice` off `master` before starting (do not commit to `master` directly).

## File Structure

| File | Responsibility | Modify |
|---|---|---|
| `app/queue/store.py` | `'retrying'` status split (`defer_failed`/`claim_next_due`/`enqueue_or_update`); `notice_not_before` column + migration; `tickets_needing_notice`/`mark_notice_posted`/`clear_notice` | ✓ |
| `app/github_app.py` | `SCHEDULE_NOTE_START`/`END`; generalized `_strip_existing_footnote`; `append_schedule_notice`; `clear_schedule_notice` | ✓ |
| `app/formatting.py` | `format_schedule_notice` | ✓ |
| `app/queue/dispatcher.py` | Claim-time cleanup step; `post_pending_notices` sweep; `run_forever` wiring | ✓ |
| `SPEC.md` | §12 — document the schedule notice, the `'retrying'` split, and refine the "silent" statements | ✓ |
| `tests/test_queue_store.py` | `'retrying'` status tests; `notice_not_before` column/migration; `tickets_needing_notice`/`mark_notice_posted`/`clear_notice` tests | ✓ |
| `tests/test_dispatcher.py` | `'retrying'` status-name updates; claim-time cleanup tests; sweep tests | ✓ |
| `tests/test_github_app.py` | `append_schedule_notice`/`clear_schedule_notice`/cross-footnote-robustness tests | ✓ |
| `tests/test_schedule_notice_formatting.py` | New — `format_schedule_notice` tests (mirrors `tests/test_placeholder_formatting.py`) | ✓ (create) |

**Task order** (green at every commit): 1 (store: `'retrying'` split) → 2 (store: notice columns/functions) → 3 (github_app) → 4 (dispatcher: claim-time cleanup) → 5 (dispatcher: sweep + wiring) → 6 (docs). Task 3 has no dependency on 1/2 but is sequenced after them for narrative flow; Tasks 4 and 5 both depend on 2 and 3.

---

### Task 1: Store — split `'retrying'` from `'deferred'`

**Files:**
- Modify: `app/queue/store.py`
- Test: `tests/test_queue_store.py`, `tests/test_dispatcher.py`

**Interfaces:**
- Produces: `defer_failed` sets `status='retrying'` (was `'deferred'`); `claim_next_due` treats `'retrying'` as claimable identically to `'deferred'`; `enqueue_or_update`'s ride-out branch treats `'retrying'` identically to `'deferred'`.

- [ ] **Step 1: Update existing tests + add new ones**

In `tests/test_queue_store.py`, change `test_defer_failed_increments_attempts` (currently asserts `t.status == "deferred"` after `store.defer_failed`) to assert `"retrying"`:

```python
def test_defer_failed_increments_attempts():
    tid = _enqueue()
    store.claim_next_due(now=T0)
    store.defer_failed(tid, not_before=FUTURE, now=T0)
    t = store.get_ticket(tid)
    assert t.status == "retrying"
    assert t.attempts == 1
```

Rename and update `test_push_during_hard_failure_deferred_rides_out_keeping_not_before_and_attempts` to `test_push_during_retrying_rides_out_keeping_not_before_and_attempts`:

```python
def test_push_during_retrying_rides_out_keeping_not_before_and_attempts():
    tid = _enqueue(sha="sha1")
    store.claim_next_due(now=T0)                       # -> running
    store.defer_failed(tid, not_before=FUTURE, now=T0)  # hard-failure backoff, attempts -> 1
    store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=1, head_sha="sha2", provider="groq", now=T1
    )
    t = store.get_ticket(tid)
    assert t.status == "retrying"       # not reset to pending
    assert t.not_before == FUTURE       # failure backoff deadline NOT shortened/reset
    assert t.attempts == 1              # not reset by the push
```

Add a new test asserting a `'retrying'` ticket becomes claimable once its wait elapses:

```python
def test_retrying_ticket_is_claimable_once_not_before_passes():
    tid = _enqueue()
    store.claim_next_due(now=T0)
    store.defer_failed(tid, not_before=T1, now=T0)
    claimed = store.claim_next_due(now=T1)
    assert claimed.id == tid
    assert store.get_ticket(tid).status == "running"
```

In `tests/test_dispatcher.py`, change `test_first_hard_failure_defers_with_backoff_not_terminal`'s `assert t.status == "deferred"` to `assert t.status == "retrying"`:

```python
async def test_first_hard_failure_defers_with_backoff_not_terminal(monkeypatch):
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_failure_base_backoff_seconds", 2.0)
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 5)
    monkeypatch.setattr(dispatcher, "_jitter", lambda: 0.0)
    tid = _enqueue(pr=5)

    async def boom(repo, pr, comment_id=None):
        raise RuntimeError("github api exploded")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"          # retryable, NOT terminal
    t = store.get_ticket(tid)
    assert t.status == "retrying"
    assert t.attempts == 1
    assert t.not_before == (NOW + timedelta(seconds=2)).isoformat()  # base backoff
```

Change `test_terminal_notice_post_failure_defers_instead_of_stranding`'s `assert t.status == "deferred"` to `assert t.status == "retrying"`:

```python
async def test_terminal_notice_post_failure_defers_instead_of_stranding(monkeypatch):
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 1)
    monkeypatch.setattr(settings, "dispatcher_failure_base_backoff_seconds", 2.0)
    monkeypatch.setattr(dispatcher, "_jitter", lambda: 0.0)
    tid = _enqueue(pr=25)  # fresh -> overwrite path

    def boom_post(repo, pr, body, comment_id=None):
        raise RuntimeError("github down")

    monkeypatch.setattr(dispatcher.github_app, "upsert_comment", boom_post)

    async def boom(repo, pr, comment_id=None):
        raise RuntimeError("review outage")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"           # NOT failed (visibility guaranteed first)
    t = store.get_ticket(tid)
    assert t.status == "retrying"
    assert t.attempts == 1
    assert t.not_before == (NOW + timedelta(seconds=2)).isoformat()
```

In `test_repeated_notice_post_failure_eventually_goes_terminal`, change the docstring's `"looping in 'deferred' indefinitely"` to `"looping in 'retrying' indefinitely"` and the loop's `assert t.status == "deferred"` to `assert t.status == "retrying"`:

```python
async def test_repeated_notice_post_failure_eventually_goes_terminal(monkeypatch):
    """Regression test for the unbounded-retry-loop finding: if the terminal
    notice itself keeps failing to post, forever, the ticket must eventually
    give up and go 'failed' rather than looping in 'retrying' indefinitely.
    """
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 1)
    monkeypatch.setattr(settings, "dispatcher_max_notice_post_attempts", 3)
    monkeypatch.setattr(settings, "dispatcher_failure_base_backoff_seconds", 2.0)
    monkeypatch.setattr(settings, "dispatcher_failure_max_backoff_seconds", 300.0)
    monkeypatch.setattr(dispatcher, "_jitter", lambda: 0.0)
    tid = _enqueue(pr=26)  # fresh -> overwrite path (upsert_comment)

    def boom_post(repo, pr, body, comment_id=None):
        raise RuntimeError("github down")

    monkeypatch.setattr(dispatcher.github_app, "upsert_comment", boom_post)

    async def boom(repo, pr, comment_id=None):
        raise RuntimeError("review outage")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    now = NOW
    result = None
    for _ in range(20):  # plenty more than the notice-post ceiling
        t = store.get_ticket(tid)
        if t.status == "failed":
            break
        result = await dispatcher.process_next_due(now)
        t = store.get_ticket(tid)
        if t.status == "failed":
            break
        assert t.status == "retrying"
        # Advance past not_before so the next iteration can claim it again.
        now = datetime.fromisoformat(t.not_before) + timedelta(seconds=1)

    final = store.get_ticket(tid)
```

(Leave the rest of that test's body — everything after the `for` loop — exactly as it is; only the two lines shown above change.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_queue_store.py tests/test_dispatcher.py -k "retrying or defer_failed or hard_failure or notice_post_failure" -v`
Expected: FAIL — the renamed test doesn't exist yet under its old name (collection works fine since renaming is just a new function name, but the NEW assertions fail: `defer_failed` still sets `'deferred'`, not `'retrying'`), and `test_retrying_ticket_is_claimable_once_not_before_passes` fails because `claim_next_due` doesn't yet recognize `'retrying'` as claimable.

- [ ] **Step 3: Implement the status split in `app/queue/store.py`**

Change `enqueue_or_update`'s docstring (the `- 'deferred'` bullet) and ride-out branch:

```python
    - 'deferred'/'retrying' -> ride out: update head_sha only; keep
                       status/not_before (a push cannot shorten a
                       provider/cooldown wait or a failure backoff)
```

```python
                elif status in ("pending", "deferred", "retrying"):
```

Change `claim_next_due`'s docstring, `SELECT`, and claiming `UPDATE`:

```python
def claim_next_due(now: str) -> Ticket | None:
    """Claim the oldest due ticket (pending, or deferred/retrying whose not_before passed).

    Atomic: the UPDATE-to-running only succeeds if the row is still claimable,
    so a second concurrent claim of the same row is impossible.
    """
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM tickets
            WHERE status = 'pending'
               OR (status IN ('deferred', 'retrying') AND not_before IS NOT NULL
                   AND not_before <= ?)
            ORDER BY enqueued_at ASC, id ASC
            LIMIT 1
            """,
            (now,),
        ).fetchone()
        if row is None:
            return None
        cur = conn.execute(
            "UPDATE tickets SET status = 'running', updated_at = ?, rereview_requested = 0 "
            "WHERE id = ? AND status IN ('pending', 'deferred', 'retrying')",
            (now, row["id"]),
        )
        if cur.rowcount != 1:
            return None
        claimed = conn.execute("SELECT * FROM tickets WHERE id = ?", (row["id"],)).fetchone()
        return _row_to_ticket(claimed)
```

Change `defer_failed`:

```python
def defer_failed(ticket_id: int, not_before: str, now: str) -> None:
    """Per-ticket hard-failure backoff. Sets status='retrying' (distinct from a
    cooldown/rate-limit 'deferred' wait, so a schedule notice never posts on a
    ticket that's actually silently retrying after an error) and increments
    attempts (drives backoff + hard stop)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'retrying', not_before = ?, "
            "attempts = attempts + 1, updated_at = ? WHERE id = ?",
            (not_before, now, ticket_id),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_queue_store.py tests/test_dispatcher.py -v`
Expected: PASS — all renamed/updated tests pass; every other existing test (rate-limit, cooldown-push, dirty-flag scenarios, all of which stay `"deferred"`) is unaffected.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check .
git add app/queue/store.py tests/test_queue_store.py tests/test_dispatcher.py
git commit -m "feat(queue): split 'retrying' status from 'deferred' for hard-failure backoff"
```

---

### Task 2: Store — `notice_not_before` column + notice functions

**Files:**
- Modify: `app/queue/store.py`
- Test: `tests/test_queue_store.py`

**Interfaces:**
- Consumes: the `'retrying'`/`'deferred'` split (Task 1) — `tickets_needing_notice`'s `status = 'deferred'` filter relies on `'retrying'` being a distinct value.
- Produces: `Ticket.notice_not_before: str | None`; `tickets_needing_notice(now: str) -> list[Ticket]`; `mark_notice_posted(ticket_id: int, not_before: str) -> None`; `clear_notice(ticket_id: int) -> None`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_queue_store.py`:

```python
def test_new_ticket_has_notice_not_before_none():
    tid = _enqueue()
    assert store.get_ticket(tid).notice_not_before is None


def test_init_db_backfills_notice_not_before_on_pre_existing_table(tmp_path, monkeypatch):
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
                cooldown_level INTEGER NOT NULL DEFAULT 0,
                UNIQUE(repo_full_name, pr_number)
            )
            """
        )
    store.init_db()
    assert "notice_not_before" in _column_names(db)


def _seed_deferred_with_review(tid, not_before, notice_not_before=None, last_reviewed_at=T0):
    import sqlite3

    with sqlite3.connect(settings.queue_db_path) as conn:
        conn.execute(
            "UPDATE tickets SET status='deferred', not_before=?, notice_not_before=?, "
            "last_reviewed_at=? WHERE id=?",
            (not_before, notice_not_before, last_reviewed_at, tid),
        )


def test_tickets_needing_notice_matches_never_notified():
    tid = _enqueue()
    _seed_deferred_with_review(tid, not_before=FUTURE, notice_not_before=None)
    result = store.tickets_needing_notice(now=T0)
    assert [t.id for t in result] == [tid]


def test_tickets_needing_notice_matches_stale_marker():
    tid = _enqueue()
    _seed_deferred_with_review(tid, not_before=FUTURE, notice_not_before=T_COOL)
    result = store.tickets_needing_notice(now=T0)
    assert [t.id for t in result] == [tid]


def test_tickets_needing_notice_excludes_up_to_date_marker():
    tid = _enqueue()
    _seed_deferred_with_review(tid, not_before=FUTURE, notice_not_before=FUTURE)
    assert store.tickets_needing_notice(now=T0) == []


def test_tickets_needing_notice_excludes_no_visible_review():
    tid = _enqueue()
    import sqlite3

    with sqlite3.connect(settings.queue_db_path) as conn:
        conn.execute(
            "UPDATE tickets SET status='deferred', not_before=?, notice_not_before=NULL "
            "WHERE id=?",
            (FUTURE, tid),
        )
    assert store.tickets_needing_notice(now=T0) == []


def test_tickets_needing_notice_excludes_already_due_ticket():
    tid = _enqueue()
    _seed_deferred_with_review(tid, not_before=PAST, notice_not_before=None)
    assert store.tickets_needing_notice(now=T0) == []


def test_tickets_needing_notice_excludes_retrying_status():
    tid = _enqueue()
    import sqlite3

    with sqlite3.connect(settings.queue_db_path) as conn:
        conn.execute(
            "UPDATE tickets SET status='retrying', not_before=?, last_reviewed_at=?, "
            "notice_not_before=NULL WHERE id=?",
            (FUTURE, T0, tid),
        )
    assert store.tickets_needing_notice(now=T0) == []


def test_mark_notice_posted_persists_marker():
    tid = _enqueue()
    _seed_deferred_with_review(tid, not_before=FUTURE, notice_not_before=None)
    store.mark_notice_posted(tid, FUTURE)
    assert store.get_ticket(tid).notice_not_before == FUTURE
    assert store.tickets_needing_notice(now=T0) == []


def test_clear_notice_resets_marker_to_none():
    tid = _enqueue()
    _seed_deferred_with_review(tid, not_before=FUTURE, notice_not_before=FUTURE)
    store.clear_notice(tid)
    assert store.get_ticket(tid).notice_not_before is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_queue_store.py -k "notice" -v`
Expected: FAIL — `Ticket` has no `notice_not_before`; `store` has no `tickets_needing_notice`/`mark_notice_posted`/`clear_notice`.

- [ ] **Step 3: Add the column, migration, dataclass field, and new functions**

Add to `_SCHEMA` (before the `UNIQUE(...)` line):

```python
    notice_not_before TEXT,
```

Add to `_ensure_columns` (after the `cooldown_level` block):

```python
    if "notice_not_before" not in existing:
        conn.execute("ALTER TABLE tickets ADD COLUMN notice_not_before TEXT")
```

Add to the `Ticket` dataclass (after `cooldown_level`):

```python
    notice_not_before: str | None
```

Add the three new functions, placed after `mark_failed` and before `recover_on_startup`:

```python
def tickets_needing_notice(now: str) -> list[Ticket]:
    """Deferred (schedule-wait, never retry-backoff since 'retrying' is a
    distinct status) tickets with a visible prior review whose schedule has
    changed since the last notice was posted (or none was posted yet).
    Excludes a ticket whose not_before has already passed -- it is about to
    be claimed for a real review, so a "scheduled" note for a time that's
    already gone would be wrong."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM tickets
            WHERE status = 'deferred'
              AND not_before IS NOT NULL
              AND not_before > ?
              AND last_reviewed_at IS NOT NULL
              AND (notice_not_before IS NULL OR notice_not_before != not_before)
            ORDER BY enqueued_at ASC, id ASC
            """,
            (now,),
        ).fetchall()
        return [_row_to_ticket(row) for row in rows]


def mark_notice_posted(ticket_id: int, not_before: str) -> None:
    """Record that a notice reflecting not_before was just posted. A single
    independent UPDATE -- not inside enqueue_or_update's or finalize_review's
    transactions, same pattern as mark_failed."""
    with _connect() as conn:
        conn.execute(
            "UPDATE tickets SET notice_not_before = ? WHERE id = ?",
            (not_before, ticket_id),
        )


def clear_notice(ticket_id: int) -> None:
    """Clear the notice marker after the dispatcher has stripped the schedule
    footnote from GitHub (called right after a ticket is claimed)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE tickets SET notice_not_before = NULL WHERE id = ?",
            (ticket_id,),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_queue_store.py -v`
Expected: PASS — all new tests pass; every existing test still passes (the new column defaults to `NULL` cleanly, `_row_to_ticket`'s name-keyed mapping needs no change).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check .
git add app/queue/store.py tests/test_queue_store.py
git commit -m "feat(queue): add notice_not_before column + tickets_needing_notice/mark_notice_posted/clear_notice"
```

---

### Task 3: `github_app` — schedule-note marker + append/clear functions

**Files:**
- Modify: `app/github_app.py`
- Test: `tests/test_github_app.py`

**Interfaces:**
- Produces: `SCHEDULE_NOTE_START`/`SCHEDULE_NOTE_END`; a generalized `_strip_existing_footnote(body: str) -> str` (same signature, now recognizes either footnote kind); `append_schedule_notice(repo_full_name: str, pr_number: int, footnote: str, comment_id: int | None = None) -> IssueComment`; `clear_schedule_notice(repo_full_name: str, pr_number: int, comment_id: int | None = None) -> IssueComment | None`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_github_app.py`:

```python
def test_append_schedule_notice_edits_marker_and_adds_note(fake_transport, monkeypatch):
    edited = {}
    existing_body = f"{github_app.COMMENT_MARKER}\n## Review\ngood findings"
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [{"id": 333, "body": existing_body, "user": {"login": "bot", "type": "Bot"},
          "url": f"{REPO_API_URL}/issues/comments/333"}],
    )

    def send_with_patch_capture(request, **kwargs):
        if request.method == "PATCH" and "/issues/comments/333" in request.url:
            body = json.loads(request.body)
            edited["body"] = body["body"]
            return fake_transport._build_response(
                request, {"id": 333, "body": body["body"], "user": {"login": "bot", "type": "Bot"}}, 200
            )
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", staticmethod(send_with_patch_capture))

    note = (
        f"{github_app.SCHEDULE_NOTE_START}\n🔄 Re-review scheduled ~14:00 UTC\n"
        f"{github_app.SCHEDULE_NOTE_END}"
    )
    github_app.append_schedule_notice(REPO_FULL_NAME, PR_NUMBER, note)

    assert "good findings" in edited["body"]
    assert "Re-review scheduled" in edited["body"]


def test_append_schedule_notice_replaces_prior_schedule_note(fake_transport, monkeypatch):
    edited = {}
    existing_body = (
        f"{github_app.COMMENT_MARKER}\n## Review\ngood findings\n\n"
        f"{github_app.SCHEDULE_NOTE_START}\n🔄 Re-review scheduled ~10:00 UTC\n"
        f"{github_app.SCHEDULE_NOTE_END}"
    )
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [{"id": 333, "body": existing_body, "user": {"login": "bot", "type": "Bot"},
          "url": f"{REPO_API_URL}/issues/comments/333"}],
    )

    def send_with_patch_capture(request, **kwargs):
        if request.method == "PATCH" and "/issues/comments/333" in request.url:
            body = json.loads(request.body)
            edited["body"] = body["body"]
            return fake_transport._build_response(
                request, {"id": 333, "body": body["body"], "user": {"login": "bot", "type": "Bot"}}, 200
            )
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", staticmethod(send_with_patch_capture))

    note = (
        f"{github_app.SCHEDULE_NOTE_START}\n🔄 Re-review scheduled ~14:00 UTC\n"
        f"{github_app.SCHEDULE_NOTE_END}"
    )
    github_app.append_schedule_notice(REPO_FULL_NAME, PR_NUMBER, note)

    assert "good findings" in edited["body"]
    assert "~14:00 UTC" in edited["body"]
    assert "~10:00 UTC" not in edited["body"]
    assert edited["body"].count(github_app.SCHEDULE_NOTE_START) == 1


def test_strip_existing_footnote_removes_schedule_note_when_writing_fail_note(
    fake_transport, monkeypatch
):
    """Cross-footnote robustness: append_review_footnote (fail note) must clean
    up a stale leftover schedule note, since _strip_existing_footnote now
    recognizes either marker pair."""
    edited = {}
    existing_body = (
        f"{github_app.COMMENT_MARKER}\n## Review\ngood findings\n\n"
        f"{github_app.SCHEDULE_NOTE_START}\n🔄 Re-review scheduled ~10:00 UTC\n"
        f"{github_app.SCHEDULE_NOTE_END}"
    )
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [{"id": 333, "body": existing_body, "user": {"login": "bot", "type": "Bot"},
          "url": f"{REPO_API_URL}/issues/comments/333"}],
    )

    def send_with_patch_capture(request, **kwargs):
        if request.method == "PATCH" and "/issues/comments/333" in request.url:
            body = json.loads(request.body)
            edited["body"] = body["body"]
            return fake_transport._build_response(
                request, {"id": 333, "body": body["body"], "user": {"login": "bot", "type": "Bot"}}, 200
            )
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", staticmethod(send_with_patch_capture))

    footnote = f"{github_app.FAIL_NOTE_START}\n> failure note\n{github_app.FAIL_NOTE_END}"
    github_app.append_review_footnote(REPO_FULL_NAME, PR_NUMBER, footnote)

    assert "good findings" in edited["body"]
    assert "failure note" in edited["body"]
    assert "Re-review scheduled" not in edited["body"]   # stale schedule note stripped


def test_clear_schedule_notice_strips_note_and_edits(fake_transport, monkeypatch):
    edited = {}
    existing_body = (
        f"{github_app.COMMENT_MARKER}\n## Review\ngood findings\n\n"
        f"{github_app.SCHEDULE_NOTE_START}\n🔄 Re-review scheduled ~10:00 UTC\n"
        f"{github_app.SCHEDULE_NOTE_END}"
    )
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [{"id": 333, "body": existing_body, "user": {"login": "bot", "type": "Bot"},
          "url": f"{REPO_API_URL}/issues/comments/333"}],
    )

    def send_with_patch_capture(request, **kwargs):
        if request.method == "PATCH" and "/issues/comments/333" in request.url:
            body = json.loads(request.body)
            edited["body"] = body["body"]
            return fake_transport._build_response(
                request, {"id": 333, "body": body["body"], "user": {"login": "bot", "type": "Bot"}}, 200
            )
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", staticmethod(send_with_patch_capture))

    result = github_app.clear_schedule_notice(REPO_FULL_NAME, PR_NUMBER)

    assert result.id == 333
    assert "good findings" in edited["body"]
    assert "Re-review scheduled" not in edited["body"]


def test_clear_schedule_notice_is_noop_when_no_footnote_present(fake_transport, monkeypatch):
    existing_body = f"{github_app.COMMENT_MARKER}\n## Review\ngood findings"
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [{"id": 333, "body": existing_body, "user": {"login": "bot", "type": "Bot"},
          "url": f"{REPO_API_URL}/issues/comments/333"}],
    )

    def send_that_forbids_patch(request, **kwargs):
        if request.method == "PATCH":
            raise AssertionError("must not edit when there is no footnote to strip")
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", staticmethod(send_that_forbids_patch))

    result = github_app.clear_schedule_notice(REPO_FULL_NAME, PR_NUMBER)
    assert result.id == 333


def test_clear_schedule_notice_returns_none_when_no_bot_comment_exists(fake_transport, monkeypatch):
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [{"id": 111, "body": "human comment, no marker", "user": {"login": "someone", "type": "User"}}],
    )

    result = github_app.clear_schedule_notice(REPO_FULL_NAME, PR_NUMBER)
    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_github_app.py -k "schedule_notice or strip_existing_footnote_removes" -v`
Expected: FAIL — `github_app` has no `SCHEDULE_NOTE_START`/`append_schedule_notice`/`clear_schedule_notice`.

- [ ] **Step 3: Add the marker, generalize the strip helper, add the two functions**

Add after `FAIL_NOTE_END`:

```python
# Sub-marker delimiting the self-cleaning "re-review scheduled" notice shown
# while a cooldown/rate-limit wait is pending. Mutually exclusive with
# FAIL_NOTE_* by construction (a ticket is never both mid-failure-retry and
# in a completed cooldown/rate-limit wait) -- see _strip_existing_footnote.
SCHEDULE_NOTE_START = "<!-- ai-review-schedule-note -->"
SCHEDULE_NOTE_END = "<!-- /ai-review-schedule-note -->"
```

Replace `_strip_existing_footnote` with the generalized version:

```python
_FOOTNOTE_MARKERS = (
    (FAIL_NOTE_START, FAIL_NOTE_END),
    (SCHEDULE_NOTE_START, SCHEDULE_NOTE_END),
)


def _strip_existing_footnote(body: str) -> str:
    """Strip whichever known footnote block (failure note or schedule note) is
    present as a well-formed TRAILING block, if any.

    Deliberately NOT a regex-from-first-marker-to-next-marker scan: a
    specialist's finding text could plausibly quote a literal marker string
    (this very file contains both), which would make a "first START to next
    END" match span from that stray marker all the way to the real trailing
    footnote's END, deleting genuine review content in between. Instead: for
    each known marker pair, find the LAST occurrence of its START and only
    treat it as a real footnote to strip if the body actually ends with its
    END -- any earlier/unmatched occurrence is left alone as incidental
    review text. Trying both pairs means whichever footnote-writing function
    runs next cleans up a stale leftover of the OTHER kind too, so the two
    kinds can never both be visible at once even if an earlier cleanup step
    failed.
    """
    stripped = body.rstrip()
    for start, end in _FOOTNOTE_MARKERS:
        idx = stripped.rfind(start)
        if idx != -1 and stripped.endswith(end):
            return stripped[:idx].rstrip()
    return stripped
```

Add the two new functions after `append_review_footnote`:

```python
def append_schedule_notice(
    repo_full_name: str, pr_number: int, footnote: str, comment_id: int | None = None
) -> IssueComment:
    """Append/refresh the schedule-wait footnote below the bot's own comment,
    preserving the review. Finds the comment by id then author-filtered marker
    scan; creates a marker-carrying comment if none exists."""
    gh = get_installation_client()
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    existing = _find_bot_comment(repo, pr, comment_id)
    if existing is not None:
        base = _strip_existing_footnote(existing.body)
        existing.edit(f"{base}\n\n{footnote}")
        return existing
    return pr.create_issue_comment(f"{COMMENT_MARKER}\n{footnote}")


def clear_schedule_notice(
    repo_full_name: str, pr_number: int, comment_id: int | None = None
) -> IssueComment | None:
    """Strip any existing footnote (schedule note or, defensively, failure
    note) from the bot's comment -- called once a deferred ticket is claimed
    and its wait is over. No-op (no edit call) if the comment has no footnote
    to strip, or if no bot comment exists yet."""
    gh = get_installation_client()
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    existing = _find_bot_comment(repo, pr, comment_id)
    if existing is None:
        return None
    stripped = _strip_existing_footnote(existing.body)
    if stripped != existing.body.rstrip():
        existing.edit(stripped)
    return existing
```

- [ ] **Step 4: Run the github_app suite to verify pass**

Run: `uv run pytest tests/test_github_app.py -v`
Expected: PASS — new tests pass; every existing footnote/marker test (including the stray-marker-substring regression test) still passes unchanged, since the generalized helper's per-pair logic is identical to the old single-pair logic.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check .
git add app/github_app.py tests/test_github_app.py
git commit -m "feat(github): add schedule-notice marker + append_schedule_notice/clear_schedule_notice"
```

---

### Task 4: Dispatcher — claim-time schedule-notice cleanup

**Files:**
- Modify: `app/queue/dispatcher.py`
- Test: `tests/test_dispatcher.py`

**Interfaces:**
- Consumes: `github_app.clear_schedule_notice` (Task 3); `store.clear_notice` (Task 2); `Ticket.notice_not_before` (Task 2).
- Produces: `process_next_due` strips any live schedule footnote immediately after claiming a ticket, before anything else runs.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_dispatcher.py`:

```python
def _stub_clear_schedule(monkeypatch):
    cleared = []
    monkeypatch.setattr(dispatcher.github_app, "clear_schedule_notice",
                        lambda repo, pr, comment_id=None: cleared.append((pr, comment_id)))
    return cleared


async def test_claim_clears_schedule_notice_when_one_was_pending(monkeypatch):
    _stub_comments(monkeypatch)
    cleared = _stub_clear_schedule(monkeypatch)
    tid = _enqueue(pr=70)
    _set_comment_id(tid, 7070)
    import sqlite3

    with sqlite3.connect(settings.queue_db_path) as conn:
        conn.execute(
            "UPDATE tickets SET status='deferred', not_before=?, last_reviewed_at=?, "
            "notice_not_before=? WHERE id=?",
            (NOW.isoformat(), NOW.isoformat(), "2026-01-01T11:00:00+00:00", tid),
        )

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})(), comment_id=7070)

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "ran"
    assert cleared == [(70, 7070)]
    assert store.get_ticket(tid).notice_not_before is None


async def test_claim_does_not_call_clear_when_no_notice_pending(monkeypatch):
    _stub_comments(monkeypatch)
    cleared = _stub_clear_schedule(monkeypatch)
    tid = _enqueue(pr=71)

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "ran"
    assert cleared == []


async def test_claim_clear_failure_does_not_block_review_attempt(monkeypatch):
    _stub_comments(monkeypatch)
    tid = _enqueue(pr=72)
    _set_comment_id(tid, 7272)
    import sqlite3

    with sqlite3.connect(settings.queue_db_path) as conn:
        conn.execute(
            "UPDATE tickets SET status='deferred', not_before=?, last_reviewed_at=?, "
            "notice_not_before=? WHERE id=?",
            (NOW.isoformat(), NOW.isoformat(), "2026-01-01T11:00:00+00:00", tid),
        )

    def boom_clear(repo, pr, comment_id=None):
        raise RuntimeError("github down")

    monkeypatch.setattr(dispatcher.github_app, "clear_schedule_notice", boom_clear)

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})(), comment_id=7272)

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "ran"                       # review still proceeded
    assert store.get_ticket(tid).notice_not_before == "2026-01-01T11:00:00+00:00"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_dispatcher.py -k "claim_clears_schedule_notice or claim_does_not_call_clear or claim_clear_failure" -v`
Expected: FAIL — `process_next_due` doesn't yet check `ticket.notice_not_before` or call `clear_schedule_notice`/`clear_notice`.

- [ ] **Step 3: Add the claim-time cleanup step**

In `app/queue/dispatcher.py`, immediately after:

```python
    ticket = store.claim_next_due(now.isoformat())
    if ticket is None:
        return StepResult(action="idle")
```

insert:

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

- [ ] **Step 4: Run the full suite to verify pass**

Run: `uv run pytest -v`
Expected: PASS — new tests pass; every existing dispatcher test is unaffected (none of them seed a `notice_not_before`, so the new `if` block is a no-op for all of them).

- [ ] **Step 5: Lint + boot smoke check + commit**

```bash
uv run ruff check . && uv run python -c "from app.main import app; print('import ok')"
git add app/queue/dispatcher.py tests/test_dispatcher.py
git commit -m "feat(queue): strip any pending schedule notice the moment a ticket is claimed"
```

---

### Task 5: Dispatcher — schedule-notice sweep + `run_forever` wiring

**Files:**
- Modify: `app/formatting.py`, `app/queue/dispatcher.py`
- Test: `tests/test_schedule_notice_formatting.py` (new), `tests/test_dispatcher.py`

**Interfaces:**
- Consumes: `store.tickets_needing_notice`/`store.mark_notice_posted` (Task 2); `github_app.append_schedule_notice` (Task 3).
- Produces: `format_schedule_notice(not_before: datetime) -> str`; `post_pending_notices(now: datetime) -> int`; `run_forever` calls `post_pending_notices` once per iteration alongside `process_next_due`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_schedule_notice_formatting.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.formatting import format_schedule_notice
from app.github_app import SCHEDULE_NOTE_END, SCHEDULE_NOTE_START

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_format_schedule_notice_has_markers_and_absolute_utc_time():
    body = format_schedule_notice(NOW)
    assert body.startswith(SCHEDULE_NOTE_START)
    assert body.endswith(SCHEDULE_NOTE_END)
    assert "12:00 UTC" in body
    assert "Re-review scheduled" in body


def test_format_schedule_notice_reflects_the_given_not_before():
    later = NOW + timedelta(hours=2, minutes=30)
    body = format_schedule_notice(later)
    assert "14:30 UTC" in body
```

Add to `tests/test_dispatcher.py`:

```python
def _stub_append_schedule(monkeypatch):
    posted = []
    monkeypatch.setattr(dispatcher.github_app, "append_schedule_notice",
                        lambda repo, pr, footnote, comment_id=None: posted.append(
                            (pr, footnote, comment_id)
                        ))
    return posted


async def test_post_pending_notices_posts_for_matching_ticket(monkeypatch):
    posted = _stub_append_schedule(monkeypatch)
    tid = _enqueue(pr=80)
    _set_comment_id(tid, 8080)
    future = NOW + timedelta(hours=1)
    import sqlite3

    with sqlite3.connect(settings.queue_db_path) as conn:
        conn.execute(
            "UPDATE tickets SET status='deferred', not_before=?, last_reviewed_at=? WHERE id=?",
            (future.isoformat(), NOW.isoformat(), tid),
        )

    count = await dispatcher.post_pending_notices(NOW)

    assert count == 1
    assert posted and posted[0][0] == 80
    assert posted[0][2] == 8080
    assert "13:00 UTC" in posted[0][1]
    assert store.get_ticket(tid).notice_not_before == future.isoformat()


async def test_post_pending_notices_does_not_repost_when_marker_matches(monkeypatch):
    posted = _stub_append_schedule(monkeypatch)
    tid = _enqueue(pr=81)
    future = NOW + timedelta(hours=1)
    import sqlite3

    with sqlite3.connect(settings.queue_db_path) as conn:
        conn.execute(
            "UPDATE tickets SET status='deferred', not_before=?, last_reviewed_at=?, "
            "notice_not_before=? WHERE id=?",
            (future.isoformat(), NOW.isoformat(), future.isoformat(), tid),
        )

    count = await dispatcher.post_pending_notices(NOW)

    assert count == 0
    assert posted == []


async def test_post_pending_notices_per_ticket_failure_does_not_block_others(monkeypatch):
    tid1 = _enqueue(pr=82)
    tid2 = _enqueue(pr=83)
    future = NOW + timedelta(hours=1)
    import sqlite3

    with sqlite3.connect(settings.queue_db_path) as conn:
        conn.execute(
            "UPDATE tickets SET status='deferred', not_before=?, last_reviewed_at=? WHERE id=?",
            (future.isoformat(), NOW.isoformat(), tid1),
        )
        conn.execute(
            "UPDATE tickets SET status='deferred', not_before=?, last_reviewed_at=? WHERE id=?",
            (future.isoformat(), NOW.isoformat(), tid2),
        )

    calls = []

    def flaky_append(repo, pr, footnote, comment_id=None):
        calls.append(pr)
        if pr == 82:
            raise RuntimeError("github down")

    monkeypatch.setattr(dispatcher.github_app, "append_schedule_notice", flaky_append)

    count = await dispatcher.post_pending_notices(NOW)

    assert count == 1                                       # only the successful one counted
    assert set(calls) == {82, 83}                            # both attempted
    assert store.get_ticket(tid1).notice_not_before is None  # failed post -> marker not set
    assert store.get_ticket(tid2).notice_not_before == future.isoformat()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_schedule_notice_formatting.py tests/test_dispatcher.py -k "schedule_notice_has_markers or reflects_the_given or post_pending_notices" -v`
Expected: FAIL — `app.formatting` has no `format_schedule_notice`; `dispatcher` has no `post_pending_notices`.

- [ ] **Step 3: Implement `format_schedule_notice` in `app/formatting.py`**

Change the import line:

```python
from app.github_app import COMMENT_MARKER, FAIL_NOTE_END, FAIL_NOTE_START
```

to:

```python
from app.github_app import (
    COMMENT_MARKER,
    FAIL_NOTE_END,
    FAIL_NOTE_START,
    SCHEDULE_NOTE_END,
    SCHEDULE_NOTE_START,
)
```

Add the function at the end of the file:

```python
def format_schedule_notice(not_before: datetime) -> str:
    """Self-cleaning notice appended below a preserved good review when the
    next re-review is scheduled (cooldown or rate-limit wait). Absolute UTC
    time only -- GitHub's comment body can't be localized per viewer, and this
    note is only edited on a re-arm event (not continuously updated), so a
    relative string would go stale the moment it's posted."""
    eta = not_before.strftime("%H:%M UTC")
    return f"{SCHEDULE_NOTE_START}\n🔄 Re-review scheduled ~{eta}\n{SCHEDULE_NOTE_END}"
```

- [ ] **Step 4: Implement `post_pending_notices` + wire into `run_forever` in `app/queue/dispatcher.py`**

Change the formatting import:

```python
from app.formatting import format_failure, format_failure_footnote, format_placeholder
```

to:

```python
from app.formatting import (
    format_failure,
    format_failure_footnote,
    format_placeholder,
    format_schedule_notice,
)
```

Add `post_pending_notices` after `_post_placeholder`, before `process_next_due`:

```python
async def post_pending_notices(now: datetime) -> int:
    """Refresh the schedule footnote on every deferred ticket whose not_before
    changed since the last notice. Returns the count posted. Called once per
    run_forever iteration, alongside process_next_due."""
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

Update the module docstring's "Delay handling" paragraph:

```python
Delay handling: a ticket that can't run now is deferred; it also gets a
placeholder comment UNLESS a good review is already visible on the PR
(``_has_visible_review``), in which case the existing review is preserved
silently instead. The real result later edits that same comment in place
via the comment marker.
```

to:

```python
Delay handling: a ticket that can't run now is deferred; it also gets a
placeholder comment UNLESS a good review is already visible on the PR
(``_has_visible_review``), in which case a self-cleaning "re-review
scheduled" footnote is shown instead (posted/refreshed by the
``post_pending_notices`` sweep, run once per loop iteration) rather than
staying fully silent. The real result later edits that same comment in
place via the comment marker; claiming a ticket strips any pending
schedule footnote first, since the wait is over regardless of outcome.
```

Change `run_forever`'s loop body:

```python
    while True:
        try:
            await process_next_due(datetime.now(timezone.utc))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the dispatcher must never die on one ticket
            logger.exception("dispatcher step failed")
        await asyncio.sleep(settings.dispatcher_idle_sleep_seconds)
```

to:

```python
    while True:
        now = datetime.now(timezone.utc)
        try:
            await process_next_due(now)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the dispatcher must never die on one ticket
            logger.exception("dispatcher step failed")
        try:
            await post_pending_notices(now)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the dispatcher must never die on one ticket
            logger.exception("dispatcher step failed")
        await asyncio.sleep(settings.dispatcher_idle_sleep_seconds)
```

(`run_forever` itself stays outside unit-test scope, matching this file's existing convention — `tests/test_dispatcher.py`'s module docstring already states "the infinite run_forever loop is a thin wrapper and is not unit-tested.")

- [ ] **Step 5: Run the full suite to verify pass**

Run: `uv run pytest -v`
Expected: PASS across the whole suite.

- [ ] **Step 6: Lint + boot smoke check + commit**

```bash
uv run ruff check . && uv run python -c "from app.main import app; print('import ok')"
git add app/formatting.py app/queue/dispatcher.py tests/test_schedule_notice_formatting.py tests/test_dispatcher.py
git commit -m "feat(queue): add post_pending_notices sweep, wire into run_forever"
```

---

### Task 6: Documentation — SPEC.md §12

**Files:**
- Modify: `SPEC.md`

**Interfaces:** none (docs only).

- [ ] **Step 1: Refine the two "silent" statements**

Change:

```
The cooldown is silent by design — the previous review's comment stays on
the PR while a re-review waits it out, so there is nothing to notify.
```

to:

```
While a re-review waits out the cooldown, a self-cleaning "re-review
scheduled ~HH:MM UTC" footnote is shown below the preserved review (see
below) rather than staying silent.
```

Change:

```
escalation is silent (only lengthens `not_before`); it bounds a churning PR from
```

to:

```
escalation only lengthens `not_before` (the schedule notice's ETA reflects it
automatically); it bounds a churning PR from
```

- [ ] **Step 2: Insert the schedule-notice mechanism paragraph**

Immediately after the sentence ending "...(1) `enqueue_or_update` done/failed re-arm, and (2) `finalize_review`'s dirty-flag branch.", insert a new paragraph:

```
**Re-review scheduled notice.** Rather than a fully silent wait, a deferred
ticket with a visible prior review gets a self-cleaning footnote —
`formatting.format_schedule_notice(not_before)`, "🔄 Re-review scheduled
~HH:MM UTC" (absolute time only; GitHub can't localize a comment per
viewer, and a relative string would go stale since this note is only
edited on a re-arm event) — appended via `github_app.append_schedule_notice`
below the review, delimited by `SCHEDULE_NOTE_START`/`SCHEDULE_NOTE_END`.
Posting/refreshing happens in a dispatcher-loop step,
`post_pending_notices(now)`, run once per `run_forever` iteration alongside
`process_next_due`: it queries `store.tickets_needing_notice(now)` —
`deferred` tickets with a visible review whose `not_before` has moved since
the marker column `notice_not_before` was last set — and calls
`store.mark_notice_posted` after each successful post. This single sweep
covers every trigger that puts a reviewed ticket into `deferred` (cooldown
re-arm, whether from a webhook push or `finalize_review`'s dirty-flag
branch, and a rate-limited wait with a good review already present — the
placeholder mechanism below only fires when no review exists yet) with one
code path, since the webhook process that runs `enqueue_or_update` does no
GitHub work by design. `defer_failed` (hard-failure retry backoff) sets a
distinct status, `'retrying'`, instead of `'deferred'`, so this sweep can
never mistake a silently-retrying ticket for a scheduled one —
`claim_next_due` and `enqueue_or_update`'s ride-out branch treat `'retrying'`
identically to `'deferred'` for claimability and push handling. The moment
a ticket is claimed, `process_next_due` strips any live schedule footnote
(`github_app.clear_schedule_notice` + `store.clear_notice`) before doing
anything else — the wait is over regardless of what happens next — and
`_strip_existing_footnote` recognizes either footnote kind, so whichever
footnote-writing call runs next self-heals a stale leftover of the other
kind even if a strip attempt failed.
```

- [ ] **Step 3: Update the placeholder paragraph**

Change:

```
**Placeholder → result, edited in place.** A ticket that can't run now (soft
`blocked_until` gate, or a fresh `RateLimited`) gets a placeholder comment —
`formatting.format_placeholder()` — posted through the same marker-based
`upsert_comment` used for real results, **unless** a good review is already
present (`_has_visible_review`), in which case the placeholder is
suppressed and the ticket still defers silently — the existing good review
stays up untouched until a later successful re-review overwrites it in
place. (A first-ever review, with `last_reviewed_at` still `None`, always
gets the placeholder — it is the only signal available at that point.) The
real comment later overwrites the placeholder in place, found via the
existing bot marker (no separate tracking needed for this). Wording varies
by wait magnitude: short waits say a rate limit was hit and the review will
appear shortly; waits at or above `PLACEHOLDER_DAILY_THRESHOLD_SECONDS`
(300s) name a daily quota and show an ETA computed from `now + retry_after`.
```

to:

```
**Placeholder → result, edited in place.** A ticket that can't run now (soft
`blocked_until` gate, or a fresh `RateLimited`) gets a placeholder comment —
`formatting.format_placeholder()` — posted through the same marker-based
`upsert_comment` used for real results, **unless** a good review is already
present (`_has_visible_review`), in which case the placeholder is
suppressed — the existing good review stays up, with the schedule-notice
sweep (above) appending its footnote instead of a fully silent wait — until
a later successful re-review overwrites the whole comment in place. (A
first-ever review, with `last_reviewed_at` still `None`, always gets the
placeholder — it is the only signal available at that point.) The real
comment later overwrites the placeholder in place, found via the existing
bot marker (no separate tracking needed for this). Wording varies by wait
magnitude: short waits say a rate limit was hit and the review will appear
shortly; waits at or above `PLACEHOLDER_DAILY_THRESHOLD_SECONDS` (300s) name
a daily quota and show an ETA computed from `now + retry_after`.
```

- [ ] **Step 4: Add the new test file to the Testing paragraph**

Change:

```
placeholder rendering (`tests/test_placeholder_formatting.py`), the
dispatcher's burst/daily-wall/
```

to:

```
placeholder rendering (`tests/test_placeholder_formatting.py`), the
schedule notice (`tests/test_schedule_notice_formatting.py`), the
dispatcher's burst/daily-wall/
```

- [ ] **Step 5: Sanity-check + commit**

Run: `uv run ruff check .` (docs don't affect lint; tree should stay clean)

```bash
git add SPEC.md
git commit -m "docs: document the re-review scheduled notice + 'retrying' status split"
```

---

## Self-Review (completed by plan author)

**1. Spec coverage** — every spec section maps to a task:
§2/§3.1 (the `'retrying'` status + rationale) → Task 1. §3.1 (`notice_not_before` column) + §3.2 (`tickets_needing_notice`/`mark_notice_posted`/`clear_notice`) → Task 2. §3.3 (marker, generalized strip, `append_schedule_notice`/`clear_schedule_notice`) → Task 3. §3.4's claim-time cleanup → Task 4. §3.4's sweep + `run_forever` wiring, plus `format_schedule_notice` → Task 5. §6 non-goals respected throughout (no escalation-math/rate-limit-gate/dirty-flag changes; no new config; no placeholder-wording rework; no throttling-mechanics text). The design's own "Relates to" cross-references (§6.4, §3.3 "silent" statements) are refined in Task 6.

**2. Placeholder scan** — no "TBD/TODO/handle appropriately"; every code and test step has runnable content, including full existing-test bodies shown for the Task-1 renames (not just "update this test" — the whole before/after is given since a reviewer must be able to verify the diff without re-deriving it).

**3. Type consistency** — names match across tasks: `Ticket.notice_not_before` (Task 2) is read in Task 4's `ticket.notice_not_before is not None` check and Task 5's `datetime.fromisoformat(ticket.not_before)`/`ticket.comment_id`; `store.tickets_needing_notice(now: str)`, `store.mark_notice_posted(ticket_id, not_before)`, `store.clear_notice(ticket_id)` (Task 2) are called with matching signatures in Task 5 (sweep) and Task 4 (cleanup); `github_app.append_schedule_notice(repo_full_name, pr_number, footnote, comment_id=None)` and `github_app.clear_schedule_notice(repo_full_name, pr_number, comment_id=None)` (Task 3) are called with matching signatures in Task 5 and Task 4 respectively; `format_schedule_notice(not_before: datetime) -> str` (Task 5) matches its one call site in the same task. `StepResult.action` values unchanged throughout.

**4. One deliberate refinement beyond the design doc's literal text** — `tickets_needing_notice`'s query adds `AND not_before > ?` (bound to `now`), excluding a `deferred` ticket whose wait has already elapsed but hasn't yet been claimed this tick (only one ticket is claimed per `process_next_due` call, so this is a real, if narrow, window). The design doc didn't spell out this predicate; it follows directly from the design's own intent (the notice reflects a genuine future schedule) and doesn't change the mechanism, so it's included here rather than triggering another design round-trip.
