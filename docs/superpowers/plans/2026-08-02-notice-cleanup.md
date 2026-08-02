# Re-review-notice Parked-Minors Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix four cheap, safe parked-minor findings from the re-review-notice feature's final review: bound the notice sweep's per-tick GitHub-call cost, fix a mis-attributed code comment, make the schedule notice's UTC labeling self-enforcing, and add the feature's own missing self-cleaning test.

**Architecture:** Each fix is small and independent. A new config setting bounds `tickets_needing_notice`'s result set with a `LIMIT`; leftover tickets self-heal via the existing stale-`notice_not_before`-marker mechanism (no new state). The other three are a comment reword, a one-line normalization call, and one new test — no new architecture.

**Tech Stack:** Python ≥3.12, stdlib `sqlite3`, `pytest`/`pytest-asyncio` (`asyncio_mode = "auto"`), `uv`.

## Global Constraints

- Python `>=3.12`; managed with `uv`. Run tests: `uv run pytest`; lint: `uv run ruff check .`. If `uv` is not on PATH, use `.venv/Scripts/python.exe -m pytest` / `.venv/Scripts/python.exe -m ruff check .` instead.
- `ruff` line-length **100**.
- `asyncio_mode = "auto"` — async tests need no decorator.
- **No new runtime dependency.**
- **`dispatcher_notice_sweep_batch_size` is a new config setting** (matching every other `dispatcher_*` tunable in `app/config.py`), not a hardcoded constant.
- **Leftover tickets past the batch cap must self-heal with no new state** — a ticket not included in one sweep's batch keeps its stale `notice_not_before` and matches again next tick.
- **No behavior change for any existing caller** — the UTC-normalization step is a no-op for every current caller (all already pass UTC-aware datetimes); the comment reword changes no code; the batch cap doesn't change results for any tick with fewer matching tickets than the cap.
- **Environment:** driven from WSL — pass Linux paths (`/mnt/c/...`) to file tools; `git` runs natively. Stage only each task's named files (never `git add .`) — a `.gitattributes` enforces LF; don't sweep unrelated files.
- Full design: `docs/superpowers/specs/2026-08-02-notice-cleanup-design.md`. Branch: create `fix/notice-cleanup` off `master` before starting (do not commit to `master` directly).

## File Structure

| File | Responsibility | Modify |
|---|---|---|
| `app/config.py` | `dispatcher_notice_sweep_batch_size: int = 20` | ✓ |
| `app/queue/store.py` | `tickets_needing_notice` gains a bound `LIMIT` | ✓ |
| `app/github_app.py` | Reword the `SCHEDULE_NOTE_START` comment | ✓ |
| `app/formatting.py` | `format_schedule_notice` normalizes to UTC before formatting | ✓ |
| `tests/test_queue_store.py` | Batch-cap test | ✓ |
| `tests/test_github_app.py` | Missing self-cleaning test | ✓ |
| `tests/test_schedule_notice_formatting.py` | UTC-normalization test | ✓ |

**Task order** (green at every commit): 1 (batch cap) → 2 (github_app comment + self-cleaning test) → 3 (UTC normalization). All three are independent of each other; this order is narrative only.

---

### Task 1: Bound the notice sweep with a batch cap

**Files:**
- Modify: `app/config.py`, `app/queue/store.py`
- Test: `tests/test_queue_store.py`

**Interfaces:**
- Produces: `Settings.dispatcher_notice_sweep_batch_size: int = 20`; `tickets_needing_notice(now)` returns at most that many tickets, oldest-waiting first.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_queue_store.py` (uses the existing `_seed_deferred_with_review` helper already in this file):

```python
def test_tickets_needing_notice_respects_batch_cap(monkeypatch):
    monkeypatch.setattr(settings, "dispatcher_notice_sweep_batch_size", 2)
    tids = []
    for pr in range(1, 4):  # 3 tickets, cap is 2
        tid = _enqueue(pr=pr, now=T0)
        _seed_deferred_with_review(tid, not_before=FUTURE, notice_not_before=None)
        tids.append(tid)

    first_batch = store.tickets_needing_notice(now=T0)
    assert len(first_batch) == 2
    assert [t.id for t in first_batch] == tids[:2]  # oldest-enqueued first

    for t in first_batch:
        store.mark_notice_posted(t.id, FUTURE)

    second_batch = store.tickets_needing_notice(now=T0)
    assert [t.id for t in second_batch] == tids[2:]  # the leftover ticket, picked up next "tick"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_queue_store.py::test_tickets_needing_notice_respects_batch_cap -v`
Expected: FAIL — `Settings` has no `dispatcher_notice_sweep_batch_size` attribute (`AttributeError` from `monkeypatch.setattr`), and even without that, the query returns all 3 tickets with no cap.

- [ ] **Step 3: Add the config setting**

In `app/config.py`, add after `dispatcher_rereview_cooldown_max_seconds`:

```python
    dispatcher_notice_sweep_batch_size: int = 20
```

- [ ] **Step 4: Add the `LIMIT` to `tickets_needing_notice`**

In `app/queue/store.py`, change `tickets_needing_notice`'s query (add `LIMIT ?` after the existing `ORDER BY`, and bind it in the params tuple):

```python
def tickets_needing_notice(now: str) -> list[Ticket]:
    """Deferred (schedule-wait, never retry-backoff since 'retrying' is a
    distinct status) tickets with a visible prior review whose schedule has
    changed since the last notice was posted (or none was posted yet).
    Excludes a ticket whose not_before has already passed -- it is about to
    be claimed for a real review, so a "scheduled" note for a time that's
    already gone would be wrong. Capped at dispatcher_notice_sweep_batch_size
    per call so a mass re-arm can't stall process_next_due for a whole
    dispatcher tick; any ticket past the cap keeps its stale marker and is
    picked up by the next call (self-healing, no new state)."""
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
            LIMIT ?
            """,
            (now, settings.dispatcher_notice_sweep_batch_size),
        ).fetchall()
        return [_row_to_ticket(row) for row in rows]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_queue_store.py -v`
Expected: PASS — the new test passes; every existing `tickets_needing_notice` test still passes (each seeds at most 1 ticket, well under the default cap of 20, so the `LIMIT` doesn't change their results).

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check .
git add app/config.py app/queue/store.py tests/test_queue_store.py
git commit -m "feat(queue): cap the notice sweep's batch size per tick"
```

---

### Task 2: `github_app` — correct the mutual-exclusion comment + add the missing self-cleaning test

**Files:**
- Modify: `app/github_app.py`
- Test: `tests/test_github_app.py`

**Interfaces:** none new (comment-only production change; the test exercises the existing `upsert_comment`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_github_app.py`:

```python
def test_upsert_comment_full_overwrite_removes_stale_schedule_note(fake_transport, monkeypatch):
    """A real review completion (upsert_comment's full-body overwrite) must
    wipe a previously-posted schedule note -- self-cleaning, no separate
    cleanup code needed."""
    edited = {}
    existing_body = (
        f"{github_app.COMMENT_MARKER}\n## Review\nold findings\n\n"
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

    github_app.upsert_comment(REPO_FULL_NAME, PR_NUMBER, "## Review\nnew findings")

    assert "new findings" in edited["body"]
    assert "old findings" not in edited["body"]
    assert "Re-review scheduled" not in edited["body"]   # schedule note wiped by full overwrite
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_github_app.py::test_upsert_comment_full_overwrite_removes_stale_schedule_note -v`
Expected: this test should actually PASS immediately — `upsert_comment`'s full-body replace already wipes any prior footnote, since `upsert_comment` doesn't call `_strip_existing_footnote` at all (it replaces the whole body unconditionally). This is a **characterization test** for already-correct behavior, matching the design's own testing section, not a red-then-green test. If it FAILS, that's a real bug in `upsert_comment` — stop and report it rather than changing the test to match.

- [ ] **Step 3: Reword the `SCHEDULE_NOTE_START` comment**

In `app/github_app.py`, change:

```python
# Sub-marker delimiting the self-cleaning "re-review scheduled" notice shown
# while a cooldown/rate-limit wait is pending. Mutually exclusive with
# FAIL_NOTE_* by construction (a ticket is never both mid-failure-retry and
# in a completed cooldown/rate-limit wait) -- see _strip_existing_footnote.
```

to:

```python
# Sub-marker delimiting the self-cleaning "re-review scheduled" notice shown
# while a cooldown/rate-limit wait is pending. NOT mutually exclusive with
# FAIL_NOTE_* by ticket-state construction alone -- a ticket that hits the
# failure ceiling (fail note posted) and is then pushed can briefly carry
# both footnote kinds on GitHub at once. What actually guarantees only one
# is ever visible is _strip_existing_footnote recognizing both marker pairs:
# whichever footnote-writing function runs next cleans up the other kind.
```

- [ ] **Step 4: Run the full github_app suite to verify pass**

Run: `uv run pytest tests/test_github_app.py -v`
Expected: PASS — the new test passes (as a characterization test per Step 2's note); every existing test is unaffected (comment-only change).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check .
git add app/github_app.py tests/test_github_app.py
git commit -m "test(github): add missing schedule-note self-cleaning test; fix comment"
```

---

### Task 3: `format_schedule_notice` — self-enforcing UTC normalization

**Files:**
- Modify: `app/formatting.py`
- Test: `tests/test_schedule_notice_formatting.py`

**Interfaces:** none new (`format_schedule_notice`'s signature and return type are unchanged).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_schedule_notice_formatting.py` (the file already imports `datetime`/`timedelta`/`timezone`):

```python
def test_format_schedule_notice_normalizes_non_utc_timezone():
    plus_five = timezone(timedelta(hours=5))
    local_time = datetime(2026, 1, 1, 17, 0, 0, tzinfo=plus_five)  # 17:00+05:00 == 12:00 UTC
    body = format_schedule_notice(local_time)
    assert "12:00 UTC" in body
    assert "17:00 UTC" not in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_schedule_notice_formatting.py::test_format_schedule_notice_normalizes_non_utc_timezone -v`
Expected: FAIL — `format_schedule_notice` currently calls `strftime` directly on the passed datetime without normalizing, so it renders `"17:00 UTC"` (the wall-clock time in the +05:00 offset, mislabeled as UTC) instead of `"12:00 UTC"`.

- [ ] **Step 3: Normalize to UTC before formatting**

In `app/formatting.py`, `timezone` is not currently imported — change the existing import line:

```python
from datetime import datetime, timedelta
```

to:

```python
from datetime import datetime, timedelta, timezone
```

Then change `format_schedule_notice`'s body:

```python
    eta = not_before.strftime("%H:%M UTC")
```

to:

```python
    eta = not_before.astimezone(timezone.utc).strftime("%H:%M UTC")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_schedule_notice_formatting.py -v`
Expected: PASS — the new test passes; both existing tests (`test_format_schedule_notice_has_markers_and_absolute_utc_time`, `test_format_schedule_notice_reflects_the_given_not_before`) still pass unchanged, since they already pass UTC-aware datetimes and `.astimezone(timezone.utc)` on an already-UTC datetime is a no-op.

- [ ] **Step 5: Run the full suite + lint + commit**

Run: `uv run pytest -v`
Expected: PASS across the whole suite (this task's change can only affect `format_schedule_notice`'s one call site in `app/queue/dispatcher.py`, which already only ever passes UTC-aware datetimes parsed from stored ISO-8601 UTC strings).

```bash
uv run ruff check .
git add app/formatting.py tests/test_schedule_notice_formatting.py
git commit -m "fix(formatting): normalize format_schedule_notice's UTC labeling"
```

---

## Self-Review (completed by plan author)

**1. Spec coverage** — every spec section maps to a task: §3.1/§3.2 (config + `LIMIT`) → Task 1. §3.3 (comment reword) → Task 2 Step 3. §3.5 (self-cleaning test) → Task 2 Step 1. §3.4 (UTC normalization) → Task 3. §4 testing → each task's own test. §5 non-goals respected (no change to the two deliberately-parked minors, no change to sweep cadence/escalation math).

**2. Placeholder scan** — no "TBD/TODO/handle appropriately"; every code and test step has runnable content. Task 2 Step 2 explicitly flags its test as a characterization test (expected to pass immediately) with instructions to stop and report rather than force a change if it unexpectedly fails — mirroring how the original feature's plan handled its own characterization tests.

**3. Type consistency** — `Settings.dispatcher_notice_sweep_batch_size: int` (Task 1) is read as `settings.dispatcher_notice_sweep_batch_size` inside `tickets_needing_notice`, matching every other `settings.dispatcher_*` read already in that file. `format_schedule_notice(not_before: datetime) -> str`'s signature is unchanged (Task 3) — no caller elsewhere in the codebase needs updating.
