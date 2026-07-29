# Comment-Visibility Follow-ups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the dispatcher from destroying a good, already-visible review comment — via a rate-limit placeholder (Finding 1) or a terminal failure notice (Finding 2) — and harden `enqueue_or_update`'s SELECT-then-UPDATE against a future off-loop race (Finding 3).

**Architecture:** Findings 1 & 2 share one guard — `ticket.last_reviewed_at is not None` means "a good review is already on the PR." When set, the placeholder is suppressed (silent defer) and the terminal failure is appended as a self-cleaning footnote instead of overwriting. Finding 3 wraps the read-branch-write in a `BEGIN IMMEDIATE` transaction.

**Tech Stack:** Python ≥3.12, stdlib `sqlite3`, PyGithub, `pytest`/`pytest-asyncio` (`asyncio_mode = "auto"`), `uv`.

## Global Constraints

- Python `>=3.12`; managed with `uv`. Run tests: `uv run pytest`; lint: `uv run ruff check .`.
- `ruff` line-length **100**.
- `asyncio_mode = "auto"` — async tests need no decorator.
- **No new runtime dependency** — stdlib + existing PyGithub only.
- All timestamps are timezone-aware **UTC** ISO-8601 strings; tests pass `now` in explicitly (injected clock, no real sleeps).
- **No secret is ever logged or shown.** The failure notice/footnote shows only the attempt count, never raw exception text.
- **Preserve signal:** a good review is worth keeping exactly when `ticket.last_reviewed_at is not None` (per-PR, set only by a successful `finalize_review`). Never overwrite it with a placeholder or bare failure notice.
- **Finding 3 invariants (must hold):** the `BEGIN IMMEDIATE` transaction body must never open a second connection or call another `store` function (`_due_after_cooldown` is pure — keep it so); always `commit`/`rollback` + `conn.close()` in `finally` so the write lock is never leaked.
- Full design: `docs/superpowers/specs/2026-07-29-comment-visibility-followups-design.md`. Motivating findings: `docs/2026-07-29-comment-visibility-followups.md`.

## File Structure

| File | Responsibility | Created / Modified |
|---|---|---|
| `app/github_app.py` | `FAIL_NOTE_START`/`FAIL_NOTE_END` sub-markers + `append_review_footnote(...)` | Modify |
| `app/formatting.py` | `format_failure` grammar fix + new `format_failure_footnote(attempts)` | Modify |
| `app/queue/dispatcher.py` | `_has_visible_review` helper; placeholder guard (F1); terminal-block rewrite (F2) | Modify |
| `app/queue/store.py` | `BEGIN IMMEDIATE` transaction around `enqueue_or_update` (F3) | Modify |
| `SPEC.md` | §12 — preserve-a-good-review rule + enqueue atomicity guarantee | Modify |
| `tests/test_github_app.py` | Task 1 — `append_review_footnote` tests | Modify |
| `tests/test_placeholder_formatting.py` | Task 2 — grammar + footnote tests | Modify |
| `tests/test_dispatcher.py` | Tasks 3 & 4 — F1/F2 behavior | Modify |
| `tests/test_queue_store.py` | Task 5 — transaction regression + contention | Modify |

**Task order** (codebase stays green at every commit): 1 (github_app constants+fn) → 2 (formatting, imports those constants) → 3 (dispatcher F1) → 4 (dispatcher F2, uses tasks 1+2) → 5 (store F3) → 6 (docs).

---

### Task 1: `append_review_footnote` + sub-marker constants

**Files:**
- Modify: `app/github_app.py`
- Test: `tests/test_github_app.py`

**Interfaces:**
- Produces:
  - `FAIL_NOTE_START = "<!-- ai-review-fail-note -->"`, `FAIL_NOTE_END = "<!-- /ai-review-fail-note -->"`
  - `append_review_footnote(repo_full_name: str, pr_number: int, footnote: str) -> IssueComment` — appends `footnote` under the bot's marker comment, first stripping any prior `FAIL_NOTE_*` block (idempotent, no stacking); if no marker comment exists, creates one carrying `COMMENT_MARKER`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_github_app.py`:

```python
def test_append_review_footnote_edits_marker_and_replaces_prior_footnote(fake_transport, monkeypatch):
    edited = {}
    existing_body = (
        f"{github_app.COMMENT_MARKER}\n## Review\ngood findings\n\n"
        f"{github_app.FAIL_NOTE_START}\n> old failure note\n{github_app.FAIL_NOTE_END}"
    )
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [{"id": 333, "body": existing_body, "user": {"login": "bot"},
          "url": f"{REPO_API_URL}/issues/comments/333"}],
    )

    def send_with_patch_capture(request, **kwargs):
        if request.method == "PATCH" and "/issues/comments/333" in request.url:
            body = json.loads(request.body)
            edited["body"] = body["body"]
            return fake_transport._build_response(
                request, {"id": 333, "body": body["body"], "user": {"login": "bot"}}, 200
            )
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", staticmethod(send_with_patch_capture))

    footnote = f"{github_app.FAIL_NOTE_START}\n> new failure note\n{github_app.FAIL_NOTE_END}"
    github_app.append_review_footnote(REPO_FULL_NAME, PR_NUMBER, footnote)

    assert "good findings" in edited["body"]                 # review preserved
    assert "new failure note" in edited["body"]
    assert "old failure note" not in edited["body"]          # prior footnote replaced
    assert edited["body"].count(github_app.FAIL_NOTE_START) == 1  # no stacking


def test_append_review_footnote_creates_marker_comment_when_none_exists(fake_transport, monkeypatch):
    created = {}
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [{"id": 111, "body": "human comment, no marker", "user": {"login": "someone"}}],
    )

    def send_with_create_capture(request, **kwargs):
        if request.method == "POST" and request.url.endswith(f"/issues/{PR_NUMBER}/comments"):
            body = json.loads(request.body)
            created["body"] = body["body"]
            return fake_transport._build_response(
                request, {"id": 222, "body": body["body"], "user": {"login": "bot"}}, 201
            )
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", staticmethod(send_with_create_capture))

    footnote = f"{github_app.FAIL_NOTE_START}\n> failure note\n{github_app.FAIL_NOTE_END}"
    github_app.append_review_footnote(REPO_FULL_NAME, PR_NUMBER, footnote)

    assert github_app.COMMENT_MARKER in created["body"]
    assert "failure note" in created["body"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_github_app.py -k append_review_footnote -v`
Expected: FAIL — `AttributeError: module 'app.github_app' has no attribute 'FAIL_NOTE_START'`.

- [ ] **Step 3: Implement in `app/github_app.py`**

Add `import re` at the top, then after the `COMMENT_MARKER` definition:

```python
# Sub-marker delimiting an optional failure footnote appended below a preserved
# good review. Idempotent: a new footnote replaces any prior block, and a later
# successful review's full-body overwrite (via upsert_comment) removes it.
FAIL_NOTE_START = "<!-- ai-review-fail-note -->"
FAIL_NOTE_END = "<!-- /ai-review-fail-note -->"
_FAIL_NOTE_RE = re.compile(re.escape(FAIL_NOTE_START) + r".*?" + re.escape(FAIL_NOTE_END), re.DOTALL)
```

Add the function (after `upsert_comment`):

```python
def append_review_footnote(repo_full_name: str, pr_number: int, footnote: str) -> IssueComment:
    """Append a failure footnote below the bot's marker comment, preserving the review.

    Strips any existing FAIL_NOTE_* block first, so repeated failures replace the
    footnote in place rather than stacking. If no marker comment exists (e.g. it was
    manually deleted), creates one carrying COMMENT_MARKER so future upserts find it.
    """
    gh = get_installation_client()
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    for comment in pr.get_issue_comments():
        if COMMENT_MARKER in comment.body:
            base = _FAIL_NOTE_RE.sub("", comment.body).rstrip()
            comment.edit(f"{base}\n\n{footnote}")
            return comment

    return pr.create_issue_comment(f"{COMMENT_MARKER}\n{footnote}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_github_app.py -v`
Expected: PASS (existing 3 + new 2).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check .
git add app/github_app.py tests/test_github_app.py
git commit -m "feat(github): append_review_footnote (preserve review, self-cleaning footnote)"
```

---

### Task 2: `format_failure` grammar fix + `format_failure_footnote`

**Files:**
- Modify: `app/formatting.py`
- Test: `tests/test_placeholder_formatting.py`

**Interfaces:**
- Consumes: `FAIL_NOTE_START`/`FAIL_NOTE_END` (Task 1).
- Produces:
  - `format_failure(pr_number, attempts)` — now pluralizes ("1 attempt" / "N attempts").
  - `format_failure_footnote(attempts: int) -> str` — the `FAIL_NOTE_*`-delimited footnote block; no raw error text.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_placeholder_formatting.py`:

```python
def test_format_failure_singular_grammar():
    from app.formatting import format_failure

    body = format_failure(pr_number=1, attempts=1)
    assert "1 attempt" in body
    assert "1 attempts" not in body


def test_format_failure_footnote_submarkers_and_grammar():
    from app.formatting import format_failure_footnote
    from app.github_app import FAIL_NOTE_END, FAIL_NOTE_START

    body = format_failure_footnote(attempts=3)
    assert FAIL_NOTE_START in body and FAIL_NOTE_END in body
    assert "3 attempts" in body
    assert "traceback" not in body.lower()  # no raw error text

    single = format_failure_footnote(attempts=1)
    assert "1 attempt" in single and "1 attempts" not in single
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_placeholder_formatting.py -k "singular or footnote" -v`
Expected: FAIL — `ImportError: cannot import name 'format_failure_footnote'` and the singular-grammar assertion fails on the current "1 attempts".

- [ ] **Step 3: Implement in `app/formatting.py`**

Add the import near the top (alongside `COMMENT_MARKER`):

```python
from app.github_app import COMMENT_MARKER, FAIL_NOTE_END, FAIL_NOTE_START
```

Replace `format_failure`'s `note` construction to pluralize, and add the footnote helper:

```python
def format_failure(pr_number: int, attempts: int) -> str:
    """Marker-prefixed comment shown when a review is abandoned after repeated
    hard failures AND no prior good review exists to preserve. Shows only the
    attempt count — never raw exception text (secrets hygiene)."""
    header = f"## 🤖 Automated Code Review — PR #{pr_number}\n"
    plural = "attempt" if attempts == 1 else "attempts"
    note = (
        f"❌ Automated review could not be completed after {attempts} {plural} "
        "due to a service error. It will retry automatically on the next push."
    )
    return f"{COMMENT_MARKER}\n{header}\n_{note}_\n"


def format_failure_footnote(attempts: int) -> str:
    """FAIL_NOTE_*-delimited footnote appended below a preserved good review when a
    later re-review hard-fails. Self-cleaning (the next successful review overwrites
    the whole comment) and idempotent (replaces any prior footnote). No raw error text."""
    plural = "attempt" if attempts == 1 else "attempts"
    return (
        f"{FAIL_NOTE_START}\n"
        f"> ⚠️ A later automated re-review could not be completed after {attempts} "
        f"{plural} (service error). The review above may be behind the latest commit; "
        "it will retry on the next push.\n"
        f"{FAIL_NOTE_END}"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_placeholder_formatting.py -v`
Expected: PASS (existing + new; the existing `attempts=5` test still passes — "5 attempts").

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check .
git add app/formatting.py tests/test_placeholder_formatting.py
git commit -m "feat(formatting): failure footnote + attempt-count grammar fix"
```

---

### Task 3: Finding 1 — suppress placeholder when a good review is visible

**Files:**
- Modify: `app/queue/dispatcher.py`
- Test: `tests/test_dispatcher.py`

**Interfaces:**
- Produces: `_has_visible_review(ticket: store.Ticket) -> bool` (`ticket.last_reviewed_at is not None`). Both placeholder sites in `process_next_due` (pre-flight `blocked_until` gate and `ReviewRateLimited` outcome) still `defer_rate_limited`, but only `_post_placeholder` when `not _has_visible_review(ticket)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_dispatcher.py` (helper first, then two tests):

```python
def _reviewed_then_pushed(pr, monkeypatch):
    """A ticket that HAS a completed review (last_reviewed_at set) and a pending
    re-review queued by a later push (cooldown 0 -> immediately claimable)."""
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 0.0)
    tid = _enqueue(pr=pr)
    store.claim_next_due(NOW.isoformat())
    store.finalize_review(tid, now=NOW.isoformat(), rereview_not_before=NOW.isoformat())
    store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=pr, head_sha="sha2",
        provider="groq", now=NOW.isoformat(),
    )
    return tid


async def test_gate_does_not_overwrite_good_review_with_placeholder(monkeypatch):
    posted = _stub_comments(monkeypatch)
    tid = _reviewed_then_pushed(20, monkeypatch)
    dispatcher._blocked_until["groq"] = NOW + timedelta(seconds=120)

    async def fake_attempt(repo, pr):
        raise AssertionError("attempt_review must not run while blocked")

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"
    assert store.get_ticket(tid).status == "deferred"
    assert posted == []  # good review preserved; no placeholder posted


async def test_rate_limited_outcome_does_not_overwrite_good_review(monkeypatch):
    posted = _stub_comments(monkeypatch)
    tid = _reviewed_then_pushed(21, monkeypatch)

    async def rl(repo, pr):
        return orchestrator.ReviewRateLimited(retry_after=30.0)

    monkeypatch.setattr(dispatcher, "attempt_review", rl)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"
    assert store.get_ticket(tid).status == "deferred"
    assert posted == []  # no placeholder over the good review
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_dispatcher.py -k "does_not_overwrite" -v`
Expected: FAIL — the current code posts a placeholder unconditionally, so `posted` is non-empty.

- [ ] **Step 3: Implement in `app/queue/dispatcher.py`**

Add the helper (after `reset_blocked_until`):

```python
def _has_visible_review(ticket: store.Ticket) -> bool:
    """True when a prior successful review is already on the PR (worth preserving
    over a placeholder or a bare failure notice). Set only by finalize_review."""
    return ticket.last_reviewed_at is not None
```

In `process_next_due`, guard the pre-flight gate's placeholder:

```python
    if blocked is not None and now < blocked:
        store.defer_rate_limited(ticket.id, not_before=blocked.isoformat(), now=now.isoformat())
        if not _has_visible_review(ticket):
            await _post_placeholder(
                ticket.repo_full_name, ticket.pr_number, (blocked - now).total_seconds(), now
            )
        return StepResult(action="deferred", ticket_id=ticket.id)
```

And the `ReviewRateLimited` outcome's placeholder:

```python
    if isinstance(outcome, ReviewRateLimited):
        wait = max(outcome.retry_after, settings.dispatcher_min_retry_after_seconds)
        until = now + timedelta(seconds=wait)
        _blocked_until[provider] = until
        store.defer_rate_limited(ticket.id, not_before=until.isoformat(), now=now.isoformat())
        if not _has_visible_review(ticket):
            await _post_placeholder(ticket.repo_full_name, ticket.pr_number, wait, now)
        return StepResult(action="deferred", ticket_id=ticket.id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_dispatcher.py -v`
Expected: PASS — new tests pass; existing placeholder tests (fresh tickets, `last_reviewed_at is None`) still post as before.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check .
git add app/queue/dispatcher.py tests/test_dispatcher.py
git commit -m "fix(dispatcher): don't overwrite a good review with a rate-limit placeholder"
```

---

### Task 4: Finding 2 — terminal failure preserves a good review (append) + double-failure fix

**Files:**
- Modify: `app/queue/dispatcher.py`
- Test: `tests/test_dispatcher.py`

**Interfaces:**
- Consumes: `format_failure`, `format_failure_footnote` (Task 2), `github_app.append_review_footnote` (Task 1), `_has_visible_review` (Task 3).
- Produces: terminal hard-stop block that appends a footnote when a good review exists (else overwrites as today), posts the notice **before** `mark_failed`, and on a post failure `defer_failed`s instead of going terminal.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_dispatcher.py` (a footnote stub, then three tests):

```python
def _stub_footnotes(monkeypatch):
    appended = []
    monkeypatch.setattr(dispatcher.github_app, "append_review_footnote",
                        lambda repo, pr, footnote: appended.append((pr, footnote)))
    return appended


async def test_terminal_failure_appends_footnote_when_good_review_exists(monkeypatch):
    posted = _stub_comments(monkeypatch)
    appended = _stub_footnotes(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 1)
    tid = _reviewed_then_pushed(22, monkeypatch)

    async def boom(repo, pr):
        raise RuntimeError("outage")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "failed"
    assert store.get_ticket(tid).status == "failed"
    assert appended and appended[0][0] == 22   # footnote appended
    assert posted == []                         # good review NOT overwritten


async def test_terminal_failure_overwrites_when_no_good_review(monkeypatch):
    posted = _stub_comments(monkeypatch)
    appended = _stub_footnotes(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 1)
    tid = _enqueue(pr=24)  # fresh: last_reviewed_at is None

    async def boom(repo, pr):
        raise RuntimeError("outage")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "failed"
    assert store.get_ticket(tid).status == "failed"
    assert posted and posted[0][0] == 24        # overwrite via upsert_comment
    assert "could not be completed" in posted[0][1].lower()
    assert appended == []                        # no footnote when nothing to preserve


async def test_terminal_notice_post_failure_defers_instead_of_stranding(monkeypatch):
    monkeypatch.setattr(settings, "dispatcher_max_failure_attempts", 1)
    monkeypatch.setattr(settings, "dispatcher_failure_base_backoff_seconds", 2.0)
    monkeypatch.setattr(dispatcher, "_jitter", lambda: 0.0)
    tid = _enqueue(pr=25)  # fresh -> overwrite path

    def boom_post(repo, pr, body):
        raise RuntimeError("github down")

    monkeypatch.setattr(dispatcher.github_app, "upsert_comment", boom_post)

    async def boom(repo, pr):
        raise RuntimeError("review outage")

    monkeypatch.setattr(dispatcher, "attempt_review", boom)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"           # NOT failed (visibility guaranteed first)
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.attempts == 1
    assert t.not_before == (NOW + timedelta(seconds=2)).isoformat()
```

Note: `test_hard_stop_marks_failed_and_posts_failure_comment` (existing, `pr=8`, fresh ticket) remains valid — it's the overwrite path and still passes.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_dispatcher.py -k "terminal" -v`
Expected: FAIL — current code always overwrites via `upsert_comment` (no footnote branch) and marks failed even when the post raises.

- [ ] **Step 3: Implement in `app/queue/dispatcher.py`**

Extend the formatting import:

```python
from app.formatting import format_failure, format_failure_footnote, format_placeholder
```

Replace the terminal hard-stop block inside the `except Exception as exc:` handler:

```python
        next_attempt = ticket.attempts + 1
        if next_attempt >= settings.dispatcher_max_failure_attempts:
            try:
                if _has_visible_review(ticket):
                    # Preserve the good review; append a self-cleaning footnote.
                    await asyncio.to_thread(
                        github_app.append_review_footnote,
                        ticket.repo_full_name,
                        ticket.pr_number,
                        format_failure_footnote(next_attempt),
                    )
                else:
                    # No good review to preserve — the notice takes the marker comment.
                    await asyncio.to_thread(
                        github_app.upsert_comment,
                        ticket.repo_full_name,
                        ticket.pr_number,
                        format_failure(ticket.pr_number, next_attempt),
                    )
            except Exception:  # noqa: BLE001 - couldn't post the notice; don't strand as terminal
                logger.exception("failed to post terminal failure notice for ticket %s", ticket.id)
                backoff = compute_backoff(next_attempt, _jitter())
                store.defer_failed(
                    ticket.id,
                    not_before=(now + timedelta(seconds=backoff)).isoformat(),
                    now=now.isoformat(),
                )
                return StepResult(action="deferred", ticket_id=ticket.id)
            store.mark_failed(ticket.id, now=now.isoformat(), error=str(exc))
            return StepResult(action="failed", ticket_id=ticket.id)
        backoff = compute_backoff(next_attempt, _jitter())
        until = now + timedelta(seconds=backoff)
        store.defer_failed(ticket.id, not_before=until.isoformat(), now=now.isoformat())
        return StepResult(action="deferred", ticket_id=ticket.id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_dispatcher.py -v`
Expected: PASS (new 3 + existing, incl. `test_hard_stop_marks_failed_and_posts_failure_comment`).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check .
git add app/queue/dispatcher.py tests/test_dispatcher.py
git commit -m "fix(dispatcher): preserve good review on terminal failure; fix silent double-failure"
```

---

### Task 5: Finding 3 — `BEGIN IMMEDIATE` transaction around `enqueue_or_update`

**Files:**
- Modify: `app/queue/store.py`
- Test: `tests/test_queue_store.py`

**Interfaces:**
- Produces: `enqueue_or_update(...)` — same signature and behavior, now wrapped in an explicit `BEGIN IMMEDIATE` transaction with `commit`/`rollback` + `close` in `finally`. No other `store` function changes.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_queue_store.py`:

```python
def test_enqueue_or_update_serializes_under_concurrent_writers():
    """Two threads enqueue the same PR concurrently. With BEGIN IMMEDIATE they
    serialize (write lock up front) rather than interleave: both complete without
    error and the final row is consistent. Serialization smoke test — a true race
    needs threads and is timing-dependent, so this asserts the observable invariant
    (no lost/corrupt write), not a specific interleaving."""
    import threading

    tid = _enqueue(pr=50, sha="sha0")
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def worker(sha: str) -> None:
        try:
            barrier.wait(timeout=5)
            store.enqueue_or_update(
                repo_full_name="owner/repo", pr_number=50,
                head_sha=sha, provider="groq", now=T1,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(s,)) for s in ("shaA", "shaB")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []                          # no "database is locked" / no crash
    final = store.get_ticket(tid)
    assert final.head_sha in ("shaA", "shaB")    # a consistent, complete write won
    assert final.status == "pending"
```

- [ ] **Step 2: Run test to verify current behavior + establish the transaction**

Run: `uv run pytest tests/test_queue_store.py::test_enqueue_or_update_serializes_under_concurrent_writers -v`
Expected: this test may PASS incidentally today (the GIL + sub-ms writes often avoid a visible interleave), so it is a *guard* rather than a red-first test. The real deliverable is the transaction; Step 4 re-runs the whole store suite to prove no regression. Proceed to implement.

- [ ] **Step 3: Wrap `enqueue_or_update` in `BEGIN IMMEDIATE`**

In `app/queue/store.py`, replace the `# SELECT-then-UPDATE ... needs an explicit transaction.` comment block and the `with _connect() as conn:` body of `enqueue_or_update` with an explicit immediate transaction. Keep the SELECT/branch/INSERT/UPDATE logic byte-for-byte; only the transaction envelope changes:

```python
    # Atomic against claim_next_due/finalize_review even off the event loop:
    # BEGIN IMMEDIATE takes the write lock up front, so no concurrent writer can
    # interleave between this SELECT and its UPDATE. Invariants that keep this
    # deadlock-free (see the design doc's Finding 3): the body opens no second
    # connection and calls no other store function (_due_after_cooldown is pure),
    # and the write lock is always released via commit/rollback + close in finally.
    cooldown = settings.dispatcher_rereview_cooldown_seconds
    conn = _connect()
    conn.isolation_level = None  # manual transaction control (issue our own BEGIN)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
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
                ticket_id = int(row["id"])
            else:
                status = row["status"]
                ticket_id = int(row["id"])
                if status == "running":
                    conn.execute(
                        "UPDATE tickets SET head_sha = ?, rereview_requested = 1, "
                        "updated_at = ? WHERE id = ?",
                        (head_sha, now, ticket_id),
                    )
                elif status in ("pending", "deferred"):
                    conn.execute(
                        "UPDATE tickets SET head_sha = ?, updated_at = ? WHERE id = ?",
                        (head_sha, now, ticket_id),
                    )
                else:  # 'done'/'failed' -> re-arm honoring the cooldown, fresh attempts
                    new_status, not_before = _due_after_cooldown(row["last_reviewed_at"], now, cooldown)
                    conn.execute(
                        "UPDATE tickets SET head_sha = ?, status = ?, not_before = ?, "
                        "attempts = 0, rereview_requested = 0, updated_at = ? WHERE id = ?",
                        (head_sha, new_status, not_before, now, ticket_id),
                    )
            conn.execute("COMMIT")
            return ticket_id
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
```

(The function signature and docstring stay; only the body from the old comment onward is replaced.)

- [ ] **Step 4: Run the store suite to verify no regression**

Run: `uv run pytest tests/test_queue_store.py -v`
Expected: PASS — every existing `enqueue_or_update` behavior test (pending/deferred/running/done re-arm, cooldown branches) plus the new contention test.

- [ ] **Step 5: Full suite + lint**

Run: `uv run pytest -q && uv run ruff check .`
Expected: green; no lint errors.

- [ ] **Step 6: Commit**

```bash
git add app/queue/store.py tests/test_queue_store.py
git commit -m "fix(queue): wrap enqueue_or_update in a BEGIN IMMEDIATE transaction"
```

---

### Task 6: Documentation — SPEC.md §12

**Files:**
- Modify: `SPEC.md`

**Interfaces:** none (docs only).

- [ ] **Step 1: Update the placeholder + failure wording in §12**

In the "Placeholder → result" and "Failure backoff + hard stop" paragraphs, add the preserve-a-good-review rule: a ticket with `last_reviewed_at` set (a review already on the PR) does **not** get its comment overwritten — the rate-limit placeholder is suppressed (silent defer) and a terminal hard-failure is appended as a self-cleaning `FAIL_NOTE_*` footnote via `github_app.append_review_footnote` (rather than overwriting via `format_failure`). Note the double-failure guarantee: the notice is posted before `mark_failed`, and a failed post re-defers instead of stranding the ticket terminal.

- [ ] **Step 2: Update the enqueue atomicity note in §12**

Replace the "SELECT-then-UPDATE safe only synchronously" caveat with the guarantee: `enqueue_or_update` runs its read-branch-write inside a `BEGIN IMMEDIATE` transaction, so it is atomic against `claim_next_due`/`finalize_review` even if moved off the event loop; it is deadlock-free (single DB, single connection per transaction, no lock held across an `await`).

- [ ] **Step 3: Sanity-check + commit**

Run: `uv run ruff check .` (tree still clean; docs don't affect lint)

```bash
git add SPEC.md
git commit -m "docs: preserve-a-good-review rule + enqueue atomicity guarantee"
```

---

## Self-Review (completed by plan author)

**1. Spec coverage** — every spec section maps to a task:
- §A1 guard (`_has_visible_review`) → Task 3. §A2 Finding 1 (both placeholder sites) → Task 3. §A3 Finding 2 append/overwrite → Task 4 (+ `append_review_footnote` Task 1, `format_failure_footnote` Task 2). §A4 silent-double-failure reorder → Task 4. §A5 grammar → Task 2; non-goals respected (no `comment_id`, no staleness). §B1 `BEGIN IMMEDIATE` → Task 5. §B2 deadlock analysis / §B3 invariants → Task 5 (encoded as the code comment + the `finally: close`). §3 testing layers → each task's tests. §4 docs → Task 6.

**2. Placeholder scan** — no "TBD/TODO/handle appropriately"; every code and test step has runnable content. The one non-standard note (Task 5 Step 2) explicitly explains why the contention test is a guard, not a red-first test — not a placeholder.

**3. Type consistency** — names match across tasks: `FAIL_NOTE_START`/`FAIL_NOTE_END` (Task 1) imported by `format_failure_footnote` (Task 2) and matched in Task 1's strip regex; `append_review_footnote(repo_full_name, pr_number, footnote)` (Task 1) called with those args in Task 4; `format_failure_footnote(attempts)` / `format_failure(pr_number, attempts)` (Task 2) called in Task 4; `_has_visible_review(ticket)` (Task 3) used in Tasks 3 & 4; `store.defer_failed`/`mark_failed`/`compute_backoff`/`_jitter` reused with their existing signatures. `StepResult.action` values unchanged (`idle`/`ran`/`deferred`/`failed`), with `deferred` now also covering the post-failure-retry case.
```
