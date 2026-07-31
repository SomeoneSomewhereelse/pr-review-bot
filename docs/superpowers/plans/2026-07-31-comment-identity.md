# Robust Bot-Comment Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the bot from editing the wrong PR comment by identifying its own comment robustly (persisted `comment_id` first, an author-filtered marker scan as fallback) instead of a bare marker-substring scan — and clean up the 5 parked minors from the escalating-cooldown review.

**Architecture:** A `_find_bot_comment` resolver in `github_app` tries the stored `comment_id`, then a scan requiring `user.type == "Bot"` AND the marker. `attempt_review` captures/returns the posted comment's id; the dispatcher threads `ticket.comment_id` through the post paths and persists it via `finalize_review` (the column already exists — no schema change).

**Tech Stack:** Python ≥3.12, PyGithub, stdlib `sqlite3`, `pytest`/`pytest-asyncio` (`asyncio_mode = "auto"`), `uv`.

## Global Constraints

- Python `>=3.12`; managed with `uv`. Run tests: `uv run pytest`; lint: `uv run ruff check .`.
- `ruff` line-length **100**.
- `asyncio_mode = "auto"` — async tests need no decorator.
- **No new runtime dependency**; **no `store.py` schema change** (`comment_id` column + `finalize_review(comment_id=...)` already exist).
- **No secret is ever logged or shown.**
- **Identity = `user.type == "Bot"` + our marker** (no login match, no `GET /app`, no new config). A comment found by a known `comment_id` is trusted without re-verification.
- **Persist the id only at `finalize_review`.** Placeholder/failure posts pass the id when known but add no new store writes — the author-filtered scan re-finds an un-persisted placeholder/failure comment.
- **Environment:** driven from WSL — pass **Linux paths** (`/mnt/c/...`) to file tools; `git` runs natively (relative paths fine). Stage only each task's named files (`git add <files>`, never `git add .`) — a `.gitattributes` now enforces LF but avoid sweeping unrelated files.
- Full design: `docs/superpowers/specs/2026-07-31-comment-identity-design.md`. Finding 2 (re-review notice) is **deferred** (§6 of the spec). Branch: `master`.

## File Structure

| File | Responsibility | Modify |
|---|---|---|
| `app/github_app.py` | `GithubException` import; `_is_bot_comment` + `_find_bot_comment`; `comment_id` param on `upsert_comment`/`append_review_footnote` | ✓ |
| `app/orchestrator.py` | `ReviewCompleted.comment_id`; `attempt_review` gains `comment_id`, passes + captures it | ✓ |
| `app/queue/dispatcher.py` | Thread `ticket.comment_id` into attempt/placeholder/failure calls; persist via `finalize_review` | ✓ |
| `SPEC.md` | §12 note on robust identity | ✓ |
| `tests/test_github_app.py` | Resolution-order tests; add `user.type` to existing mocks | ✓ |
| `tests/test_orchestrator_rate_limited.py` | `attempt_review` id capture/threading | ✓ |
| `tests/test_dispatcher.py` | Thread `comment_id`; composed churn-ramp minor | ✓ |
| `tests/test_queue_store.py` | Latent-nonzero-level minor test | ✓ |

**Task order** (green at every commit): 1 (github_app) → 2 (orchestrator) → 3 (dispatcher) → 4 (minors: coverage tests) → 5 (minors: docstrings + SPEC). Tasks 1 and 2 leave existing callers passing `comment_id=None` (default), so behavior is unchanged until Task 3 wires the real ids.

---

### Task 1: `github_app` — identity resolver + `comment_id` params

**Files:**
- Modify: `app/github_app.py`
- Test: `tests/test_github_app.py`

**Interfaces:**
- Produces: `_is_bot_comment(comment) -> bool`; `_find_bot_comment(repo, pr, comment_id) -> IssueComment | None`; `upsert_comment(repo_full_name, pr_number, body, comment_id=None) -> IssueComment`; `append_review_footnote(repo_full_name, pr_number, footnote, comment_id=None) -> IssueComment`.

- [ ] **Step 1: Update existing mocks + write the new tests**

In `tests/test_github_app.py`: add `"type": "Bot"` to the bot comment's `user` in `test_upsert_comment_edits_existing_marker_comment_in_place` (the comment at id 333) so the author-filtered scan matches it — change its `"user": {"login": "bot"}` to `"user": {"login": "bot", "type": "Bot"}`. Then add these tests:

```python
def test_upsert_comment_skips_human_comment_containing_the_marker(fake_transport, monkeypatch):
    created = {}
    # A human comment that quotes the marker must NOT be edited.
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [{"id": 501, "body": f"quoting the bot: {github_app.COMMENT_MARKER}",
          "user": {"login": "a-human", "type": "User"},
          "url": f"{REPO_API_URL}/issues/comments/501"}],
    )

    def send(request, **kwargs):
        if request.method == "PATCH" and "/issues/comments/501" in request.url:
            raise AssertionError("must not edit a human comment that merely quotes the marker")
        if request.method == "POST" and request.url.endswith(f"/issues/{PR_NUMBER}/comments"):
            body = json.loads(request.body)
            created["body"] = body["body"]
            return fake_transport._build_response(
                request, {"id": 777, "body": body["body"], "user": {"login": "bot", "type": "Bot"}}, 201
            )
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", staticmethod(send))

    result = github_app.upsert_comment(REPO_FULL_NAME, PR_NUMBER, "## Review\nfresh")
    assert result.id == 777                       # created a new bot comment
    assert github_app.COMMENT_MARKER in created["body"]


def test_upsert_comment_edits_by_id_when_comment_id_given(fake_transport, monkeypatch):
    edited = {}
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/comments/333",
        {"id": 333, "body": f"{github_app.COMMENT_MARKER}\nold", "user": {"login": "bot", "type": "Bot"},
         "url": f"{REPO_API_URL}/issues/comments/333"},
    )

    def send(request, **kwargs):
        if request.method == "GET" and request.url.endswith(f"/issues/{PR_NUMBER}/comments"):
            raise AssertionError("must not scan the thread when a comment_id is known")
        if request.method == "PATCH" and "/issues/comments/333" in request.url:
            body = json.loads(request.body)
            edited["body"] = body["body"]
            return fake_transport._build_response(
                request, {"id": 333, "body": body["body"], "user": {"login": "bot", "type": "Bot"}}, 200
            )
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", staticmethod(send))

    result = github_app.upsert_comment(REPO_FULL_NAME, PR_NUMBER, "## Review\nnew", comment_id=333)
    assert result.id == 333
    assert "new" in edited["body"]


def test_upsert_comment_falls_back_to_scan_when_comment_id_deleted(fake_transport, monkeypatch):
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/issues/comments/999",
                         {"message": "Not Found"}, 404)
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [{"id": 333, "body": f"{github_app.COMMENT_MARKER}\nold", "user": {"login": "bot", "type": "Bot"},
          "url": f"{REPO_API_URL}/issues/comments/333"}],
    )

    def send(request, **kwargs):
        if request.method == "PATCH" and "/issues/comments/333" in request.url:
            return fake_transport._build_response(
                request, {"id": 333, "body": json.loads(request.body)["body"],
                          "user": {"login": "bot", "type": "Bot"}}, 200
            )
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", staticmethod(send))

    result = github_app.upsert_comment(REPO_FULL_NAME, PR_NUMBER, "## Review\nnew", comment_id=999)
    assert result.id == 333   # deleted id -> fell back to the author-filtered scan
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_github_app.py -k "skips_human or edits_by_id or falls_back" -v`
Expected: FAIL — `upsert_comment()` has no `comment_id` param; no author check yet.

- [ ] **Step 3: Implement the resolver + params in `app/github_app.py`**

Change the import line to add `GithubException`:

```python
from github import Auth, Github, GithubException
```

Add the helpers (after `COMMENT_MARKER`/`FAIL_NOTE_*`, before `_read_private_key`):

```python
def _is_bot_comment(comment: IssueComment) -> bool:
    """True if authored by a GitHub App bot (not a human), so a human quoting
    the marker is never mistaken for the bot's own comment."""
    return getattr(comment.user, "type", None) == "Bot"


def _find_bot_comment(repo, pr, comment_id: int | None) -> IssueComment | None:
    """Locate the bot's own comment: by stored id first (trusted — we created it),
    else an author-filtered marker scan (bot-authored AND our marker). Returns
    None if neither finds one, so the caller creates a fresh marker comment."""
    if comment_id is not None:
        try:
            return repo.get_issue_comment(comment_id)
        except GithubException:
            pass  # deleted/unknown id -> fall back to the scan
    for comment in pr.get_issue_comments():
        if _is_bot_comment(comment) and COMMENT_MARKER in comment.body:
            return comment
    return None
```

Replace `upsert_comment` and `append_review_footnote` with the `comment_id`-aware versions:

```python
def upsert_comment(
    repo_full_name: str, pr_number: int, body: str, comment_id: int | None = None
) -> IssueComment:
    """Find the bot's own comment (by id, else author-filtered marker scan) and edit
    it in place; else create one. Returns the resulting IssueComment."""
    gh = get_installation_client()
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    marked_body = body if COMMENT_MARKER in body else f"{COMMENT_MARKER}\n{body}"
    existing = _find_bot_comment(repo, pr, comment_id)
    if existing is not None:
        existing.edit(marked_body)
        return existing
    return pr.create_issue_comment(marked_body)


def append_review_footnote(
    repo_full_name: str, pr_number: int, footnote: str, comment_id: int | None = None
) -> IssueComment:
    """Append a failure footnote below the bot's own comment, preserving the review.
    Finds the comment by id then author-filtered marker scan; creates a
    marker-carrying comment if none exists."""
    gh = get_installation_client()
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    existing = _find_bot_comment(repo, pr, comment_id)
    if existing is not None:
        base = _strip_existing_footnote(existing.body)
        existing.edit(f"{base}\n\n{footnote}")
        return existing
    return pr.create_issue_comment(f"{COMMENT_MARKER}\n{footnote}")
```

- [ ] **Step 4: Run the github_app suite to verify pass**

Run: `uv run pytest tests/test_github_app.py -v`
Expected: PASS — new tests pass; the updated `edits_existing_marker_comment_in_place` (now with `type: Bot`) still edits in place; `creates_when_no_marker_comment_exists` still creates (human comment has no marker).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check .
git add app/github_app.py tests/test_github_app.py
git commit -m "fix(github): robust bot-comment identity (id-first, author-filtered scan)"
```

---

### Task 2: `orchestrator` — capture + return `comment_id`

**Files:**
- Modify: `app/orchestrator.py`
- Test: `tests/test_orchestrator_rate_limited.py`

**Interfaces:**
- Consumes: `github_app.upsert_comment(..., comment_id=None)` (Task 1).
- Produces: `ReviewCompleted(review, comment_id: int | None = None)`; `attempt_review(repo_full_name, pr_number, comment_id: int | None = None)` passes `comment_id` to `upsert_comment` and returns the posted comment's id.

- [ ] **Step 1: Update the completion test**

In `tests/test_orchestrator_rate_limited.py`: add `from types import SimpleNamespace` near the top, and replace `test_attempt_review_completes_and_posts_when_ok`'s stub + assertions:

```python
async def test_attempt_review_completes_and_posts_when_ok(monkeypatch):
    import app.orchestrator as orchestrator

    monkeypatch.setattr(orchestrator.github_app, "fetch_pr_diff", lambda repo, pr: "diff")
    posted = {}

    def fake_upsert(repo, pr, body, comment_id=None):
        posted["body"] = body
        posted["comment_id_in"] = comment_id
        return SimpleNamespace(id=222)

    monkeypatch.setattr(orchestrator.github_app, "upsert_comment", fake_upsert)

    async def mk(name):
        async def _inner(_):
            return _ok(name)
        return _inner

    monkeypatch.setattr(orchestrator, "run_security_specialist", await mk("Security"))
    monkeypatch.setattr(orchestrator, "run_performance_specialist", await mk("Performance"))
    monkeypatch.setattr(orchestrator, "run_quality_specialist", await mk("Code Quality"))

    outcome = await orchestrator.attempt_review("owner/repo", 2, comment_id=555)

    assert isinstance(outcome, orchestrator.ReviewCompleted)
    assert outcome.review.pr_number == 2
    assert "PR #2" in posted["body"]
    assert posted["comment_id_in"] == 555   # incoming id threaded to the post
    assert outcome.comment_id == 222         # posted comment's id captured
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_orchestrator_rate_limited.py::test_attempt_review_completes_and_posts_when_ok -v`
Expected: FAIL — `attempt_review()` takes no `comment_id`; `ReviewCompleted` has no `comment_id`.

- [ ] **Step 3: Implement in `app/orchestrator.py`**

Add the field to `ReviewCompleted`:

```python
@dataclass
class ReviewCompleted:
    review: ReviewResult
    comment_id: int | None = None
```

Change `attempt_review`'s signature and its post tail:

```python
async def attempt_review(
    repo_full_name: str, pr_number: int, comment_id: int | None = None
) -> ReviewCompleted | ReviewRateLimited:
```

Replace the final two lines (the `body = ...` / `upsert_comment` / `return`):

```python
    body = format_comment(review_result)
    posted = await asyncio.to_thread(
        github_app.upsert_comment, repo_full_name, pr_number, body, comment_id
    )
    return ReviewCompleted(review=review_result, comment_id=posted.id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_orchestrator_rate_limited.py -v`
Expected: PASS — the rate-limited tests are unaffected (their stubs accept `*a, **k`); the completion test passes.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check .
git add app/orchestrator.py tests/test_orchestrator_rate_limited.py
git commit -m "feat(orchestrator): thread + capture the review comment id"
```

---

### Task 3: `dispatcher` — thread `ticket.comment_id` + persist

**Files:**
- Modify: `app/queue/dispatcher.py`
- Test: `tests/test_dispatcher.py`

**Interfaces:**
- Consumes: `attempt_review(..., comment_id=None) -> ReviewCompleted(comment_id=...)` (Task 2); `github_app.upsert_comment`/`append_review_footnote(..., comment_id=None)` (Task 1); `store.finalize_review(..., comment_id=None)`.
- Produces: every post path uses `ticket.comment_id`; a completed review persists the posted id via `finalize_review`.

- [ ] **Step 1: Update the dispatcher test stubs + add the persistence assertion**

In `tests/test_dispatcher.py`:

(a) Update the comment stubs to accept `comment_id`:

```python
def _stub_comments(monkeypatch):
    posted = []
    monkeypatch.setattr(dispatcher.github_app, "upsert_comment",
                        lambda repo, pr, body, comment_id=None: posted.append((pr, body)))
    return posted
```

If a `_stub_footnotes` helper exists, update it the same way:

```python
def _stub_footnotes(monkeypatch):
    appended = []
    monkeypatch.setattr(dispatcher.github_app, "append_review_footnote",
                        lambda repo, pr, footnote, comment_id=None: appended.append((pr, footnote)))
    return appended
```

(b) Every stubbed `attempt_review` coroutine must accept the new keyword. Change each `async def <name>(repo, pr):` that is monkeypatched onto `dispatcher.attempt_review` to `async def <name>(repo, pr, comment_id=None):`. `ReviewCompleted(review=...)` calls need no change (`comment_id` defaults to `None`).

(c) Add a test asserting the posted id is persisted:

```python
async def test_completed_review_persists_returned_comment_id(monkeypatch):
    _stub_comments(monkeypatch)
    tid = _enqueue(pr=60)

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})(), comment_id=4242)

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "ran"
    assert store.get_ticket(tid).comment_id == 4242


async def test_attempt_review_is_called_with_ticket_comment_id(monkeypatch):
    _stub_comments(monkeypatch)
    tid = _enqueue(pr=61)
    import sqlite3

    with sqlite3.connect(settings.queue_db_path) as conn:
        conn.execute("UPDATE tickets SET comment_id = 909 WHERE id = ?", (tid,))
    seen = {}

    async def fake_attempt(repo, pr, comment_id=None):
        seen["comment_id"] = comment_id
        return orchestrator.ReviewCompleted(review=type("R", (), {})(), comment_id=909)

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    await dispatcher.process_next_due(NOW)
    assert seen["comment_id"] == 909   # ticket's stored id passed into attempt_review
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_dispatcher.py -k "persists_returned_comment_id or called_with_ticket_comment_id" -v`
Expected: FAIL — the dispatcher doesn't yet pass/persist `comment_id`.

- [ ] **Step 3: Thread `comment_id` through `app/queue/dispatcher.py`**

Give `_post_placeholder` a `comment_id` param:

```python
async def _post_placeholder(
    repo: str, pr: int, retry_after: float, now: datetime, comment_id: int | None = None
) -> None:
    await asyncio.to_thread(
        github_app.upsert_comment, repo, pr, format_placeholder(pr, retry_after, now), comment_id
    )
```

Pass `ticket.comment_id` at both placeholder call sites (the `blocked` gate and the `ReviewRateLimited` branch):

```python
            await _post_placeholder(
                ticket.repo_full_name, ticket.pr_number, (blocked - now).total_seconds(), now,
                ticket.comment_id,
            )
```
```python
            await _post_placeholder(
                ticket.repo_full_name, ticket.pr_number, wait, now, ticket.comment_id
            )
```

Pass `ticket.comment_id` on the terminal failure posts (both branches):

```python
                    await asyncio.to_thread(
                        github_app.append_review_footnote,
                        ticket.repo_full_name,
                        ticket.pr_number,
                        format_failure_footnote(next_attempt),
                        ticket.comment_id,
                    )
```
```python
                    await asyncio.to_thread(
                        github_app.upsert_comment,
                        ticket.repo_full_name,
                        ticket.pr_number,
                        format_failure(ticket.pr_number, next_attempt),
                        ticket.comment_id,
                    )
```

Pass `ticket.comment_id` into `attempt_review`, and persist the returned id via `finalize_review`:

```python
    try:
        outcome = await attempt_review(
            ticket.repo_full_name, ticket.pr_number, comment_id=ticket.comment_id
        )
```
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
        comment_id=outcome.comment_id,
    )
    return StepResult(action="ran", ticket_id=ticket.id)
```

- [ ] **Step 4: Run the full suite to verify pass**

Run: `uv run pytest -v`
Expected: PASS across the suite (all existing dispatcher tests pass with the updated stub/fake signatures; new persistence tests pass).

- [ ] **Step 5: Lint + boot smoke check + commit**

```bash
uv run ruff check . && uv run python -c "from app.main import app; print('import ok')"
git add app/queue/dispatcher.py tests/test_dispatcher.py
git commit -m "feat(queue): thread + persist the bot comment id through the dispatcher"
```

---

### Task 4: Parked minors — coverage tests (latent nonzero level; composed churn ramp)

**Files:**
- Modify: `tests/test_queue_store.py`, `tests/test_dispatcher.py`

**Interfaces:** none (test-only; both assert already-implemented behavior — they should PASS on first run; a failure is a real finding to surface).

- [ ] **Step 1: Add the latent-nonzero-level store test**

In `tests/test_queue_store.py`:

```python
def test_finalize_non_dirty_leaves_nonzero_cooldown_level():
    tid = _enqueue()
    store.claim_next_due(now=T0)
    import sqlite3

    with sqlite3.connect(settings.queue_db_path) as conn:
        conn.execute("UPDATE tickets SET cooldown_level = 3 WHERE id = ?", (tid,))  # rereview_requested stays 0
    store.finalize_review(tid, now=T1, rereview_not_before=T_COOL, rereview_cooldown_level=9)
    t = store.get_ticket(tid)
    assert t.status == "done"
    assert t.cooldown_level == 3   # non-dirty -> ELSE keeps the existing level, ignores the passed 9
```

- [ ] **Step 2: Add the composed churn-ramp dispatcher test**

In `tests/test_dispatcher.py`:

```python
async def test_sustained_churn_escalates_then_plateaus(monkeypatch):
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_seconds", 300.0)
    monkeypatch.setattr(settings, "dispatcher_rereview_cooldown_max_seconds", 3600.0)
    tid = _enqueue(pr=40)

    async def complete_with_push(repo, pr, comment_id=None):
        # a push lands during every review -> dirty flag -> re-arm each cycle
        store.enqueue_or_update(
            repo_full_name="owner/repo", pr_number=40, head_sha="s",
            provider="groq", now="2026-01-01T00:00:00+00:00",
        )
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", complete_with_push)

    t = NOW
    for secs in (300, 600, 1200, 2400, 3600, 3600):   # levels 0..5, plateau at the 3600 cap
        result = await dispatcher.process_next_due(t)
        assert result.action == "ran"
        tk = store.get_ticket(tid)
        assert tk.status == "deferred"
        assert tk.not_before == (t + timedelta(seconds=secs)).isoformat()
        t = t + timedelta(seconds=secs)   # advance to the next due time
```

- [ ] **Step 3: Run the tests to verify they pass (behavior already exists)**

Run: `uv run pytest tests/test_queue_store.py::test_finalize_non_dirty_leaves_nonzero_cooldown_level tests/test_dispatcher.py::test_sustained_churn_escalates_then_plateaus -v`
Expected: PASS. (These characterize existing behavior. If either FAILS, stop and surface it — that's a real escalation bug, not a test to force green.)

- [ ] **Step 4: Lint + commit**

```bash
uv run ruff check .
git add tests/test_queue_store.py tests/test_dispatcher.py
git commit -m "test(queue): cover latent nonzero cooldown level + full escalation ramp"
```

---

### Task 5: Parked minors — docstrings + SPEC note

**Files:**
- Modify: `app/queue/store.py`, `SPEC.md`

**Interfaces:** none (docs/comments only).

- [ ] **Step 1: Fix the flagged docstrings in `app/queue/store.py`**

- `effective_cooldown`: make the first docstring line reflect the clamp, e.g. `"""Escalated per-PR cooldown: min(base * 2^min(level, _MAX_COOLDOWN_LEVEL), cap)."""`.
- `_due_after_cooldown`: reflow the docstring so no line exceeds 100 chars.
- `enqueue_or_update`: in the `- 'done'/'failed'` bullet, note it escalates `cooldown_level` on churn and resets it to 0 when the cooldown has elapsed.
- `mark_failed`: update the docstring's re-arm narration to mention that `enqueue_or_update`'s terminal-state branch now also escalates/resets `cooldown_level` (not just `attempts`).

- [ ] **Step 2: Add a robust-identity note to `SPEC.md` §12**

Note that the bot identifies its own comment by the persisted `comment_id` first, falling back to an author-filtered marker scan (`user.type == "Bot"` + marker), so a human/other comment containing the marker is never edited by mistake; `finalize_review` now persists the posted comment's id (the previously-unused `comment_id` column).

- [ ] **Step 3: Lint + commit**

```bash
uv run ruff check .
git add app/queue/store.py SPEC.md
git commit -m "docs: clarify cooldown docstrings + record robust comment identity"
```

---

## Self-Review (completed by plan author)

**1. Spec coverage** — every spec section maps to a task:
- §3.1 resolver (`_is_bot_comment`, `_find_bot_comment`, `comment_id` params) → Task 1. §3.2 plumbing (`ReviewCompleted.comment_id`, `attempt_review` capture, dispatcher threading, persist via `finalize_review`) → Tasks 2 + 3. §3.3 persist-only-at-finalize / scan re-finds un-persisted → honored (no store writes added to placeholder/failure paths; Task 1's scan tests prove re-finding). §4 surface → Tasks 1–3 + §12 note (Task 5). §5 testing → each task's tests (resolution order incl. human-marker-skip, id capture/threading, dispatcher persist). §6 deferral/out-of-scope → no code touches Finding 2, login-match, or new store writes. The user-requested minors cleanup → Tasks 4 (2 coverage tests) + 5 (4 docstrings).

**2. Placeholder scan** — no "TBD/TODO/handle appropriately"; every code/test step has runnable content. The one uniform mechanical edit (Task 3 Step 1b: add `comment_id=None` to each stubbed `attempt_review` signature) is stated precisely with the exact transform, not deferred. Task 4's tests are explicitly characterization tests (expected PASS; a failure is a finding).

**3. Type consistency** — names match across tasks: `upsert_comment(..., comment_id=None)` / `append_review_footnote(..., comment_id=None)` (Task 1) are called with `comment_id` in Tasks 2 (orchestrator) and 3 (dispatcher); `ReviewCompleted(review, comment_id=None)` and `attempt_review(repo, pr, comment_id=None)` (Task 2) are consumed in Task 3; `store.finalize_review(..., comment_id=...)` uses the existing param; `ticket.comment_id` is read off the `Ticket` (existing column). `StepResult.action` values unchanged.
