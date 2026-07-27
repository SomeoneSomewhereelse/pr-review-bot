# Durable Review Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Absorb per-minute rate-limit bursts and defer reviews (durably, surviving restart) when a provider's daily quota is exhausted, driven purely by `429` + `Retry-After`.

**Architecture:** Split the webhook into a **producer** (verify → dedup → write a durable SQLite ticket → `202`) and a single serial **dispatcher** consumer that drains tickets, gated by a per-provider `blocked_until` timestamp learned only from `Retry-After`. Reviews are atomic (whole review or nothing); any delayed review posts a placeholder comment that the real result later edits in place via the existing comment marker.

**Tech Stack:** FastAPI (async), stdlib `sqlite3` (no new dependency), Pydantic v2, `pytest`/`pytest-asyncio` (`asyncio_mode = "auto"`), existing `groq`/`openai`/`google-genai` provider SDKs.

## Global Constraints

- Python `>=3.12`; project managed with `uv`; run tests with `uv run pytest`, lint with `uv run ruff check .`.
- `ruff` line-length **100** (`pyproject.toml [tool.ruff]`).
- `asyncio_mode = "auto"` — async test functions need no decorator (existing files may still use `@pytest.mark.asyncio`; either is fine).
- **No new runtime dependency** — persistence uses the standard library `sqlite3`.
- Async throughout; one-purpose modules with narrow interfaces.
- **No secret is ever logged.**
- **Partial failure is always visible** in the PR comment — a failed specialist renders a real row; never silently dropped.
- **Reactive, `Retry-After` only.** No usage counters, no `x-ratelimit-*` parsing, **no hardcoded per-provider RPM/RPD caps.** The only fallback constant is `DEFAULT_RETRY_AFTER_SECONDS` (default `60`) when a `429` omits a usable header.
- **Atomic reviews.** If any of a review's three calls is rate-limited, discard all partial results and defer the whole review — never post a partial comment.
- **Single serial dispatcher.** Exactly one consumer per process; it is the only caller of the review path.
- All timestamps are timezone-aware **UTC**; persisted as ISO-8601 strings (`datetime.isoformat()`), compared as text in SQL.
- Full design: `docs/superpowers/specs/2026-07-27-queue-features-design.md`. The ping/notify feature in that spec's §13 is **out of scope** for this plan.

## File Structure

| File | Responsibility | Created / Modified |
|---|---|---|
| `app/providers/base.py` | Provider protocol + `LLMResponse` + **new** `RateLimited` exception and `Retry-After` parsing helpers | Modify |
| `app/providers/groq.py`, `github_models.py`, `google_genai.py` | Adapters — **new**: convert a `429` into `RateLimited(retry_after)` | Modify |
| `app/queue/__init__.py` | Package marker | Create |
| `app/queue/store.py` | SQLite ticket store: schema, enqueue/claim/defer/done/recover | Create |
| `app/queue/dispatcher.py` | Serial consumer: `blocked_until` gate + `process_next_due` + `run_forever` | Create |
| `app/orchestrator.py` | **new** `attempt_review` returning `ReviewCompleted \| ReviewRateLimited`; `run_review` kept as a wrapper | Modify |
| `app/specialists/base.py` | Let `RateLimited` escape the never-raise catch | Modify |
| `app/formatting.py` | **new** `format_placeholder(...)` | Modify |
| `app/config.py` | New env vars (`queue_db_path`, `default_retry_after_seconds`, `dispatcher_idle_sleep_seconds`) | Modify |
| `app/webhook.py` | Producer: enqueue a ticket instead of a background task | Modify |
| `app/main.py` | Lifespan: init DB, recover, start/stop dispatcher | Modify |
| `.gitignore` | Ignore `queue.db*` | Modify |
| `tests/test_provider_rate_limited.py` | Task 1 tests | Create |
| `tests/test_queue_store.py` | Task 2 tests | Create |
| `tests/test_orchestrator_rate_limited.py` | Task 3 tests | Create |
| `tests/test_placeholder_formatting.py` | Task 4 tests | Create |
| `tests/test_dispatcher.py` | Task 5 tests | Create |
| `tests/test_webhook.py` | Task 6 — updated for the producer change | Modify |
| `SPEC.md`, `README.md`, `cost.md` | Task 7 — document the feature | Modify |

**Task dependency order:** 1 → (2, 3, 4 independent of each other) → 5 → 6 → 7. Task 3 and Task 5 both depend on Task 1; Task 5 depends on 2, 3, 4; Task 6 depends on 2, 5.

---

### Task 1: `RateLimited` exception + adapters raise it on `429`

**Files:**
- Modify: `app/providers/base.py`
- Modify: `app/providers/groq.py`, `app/providers/github_models.py`, `app/providers/google_genai.py`
- Test: `tests/test_provider_rate_limited.py`

**Interfaces:**
- Produces:
  - `class RateLimited(Exception)` with attribute `retry_after: float`.
  - `def parse_retry_after(value: str | None, now: datetime, default: float) -> float`
  - `def rate_limited_or_none(exc: Exception, now: datetime, default: float) -> RateLimited | None`
  - Each adapter's `complete(...)` now raises `RateLimited` on a `429` transport error (all other errors propagate unchanged).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_provider_rate_limited.py
"""Adapters convert a 429 transport error into RateLimited(retry_after).

Uses a lightweight fake exception (status_code/code + response.headers) rather
than constructing real SDK error objects, so the test is SDK-agnostic and makes
no network call. Live confirmation that GitHub Models actually sends Retry-After
is a separate one-deliberate-call verification (per CLAUDE.md hygiene).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from app.config import settings
from app.providers.base import RateLimited, parse_retry_after
from app.providers.groq import GroqProvider


class Greeting(BaseModel):
    message: str


NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class FakeRateLimitError(Exception):
    def __init__(self, retry_after_header: str | None):
        super().__init__("429 rate limited")
        self.status_code = 429
        self.response = SimpleNamespace(
            headers={"retry-after": retry_after_header} if retry_after_header is not None else {}
        )


def _groq_raising(exc: Exception, monkeypatch):
    create = AsyncMock(side_effect=exc)
    monkeypatch.setattr(
        "app.providers.groq.AsyncGroq",
        lambda **kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        ),
    )


def test_parse_retry_after_seconds():
    assert parse_retry_after("30", NOW, default=60) == 30.0


def test_parse_retry_after_http_date():
    # 2026-01-01 12:02:00 GMT is 120 seconds after NOW.
    assert parse_retry_after("Thu, 01 Jan 2026 12:02:00 GMT", NOW, default=60) == 120.0


def test_parse_retry_after_missing_uses_default():
    assert parse_retry_after(None, NOW, default=60) == 60.0


async def test_groq_429_with_header_raises_rate_limited(monkeypatch):
    _groq_raising(FakeRateLimitError("30"), monkeypatch)
    with pytest.raises(RateLimited) as ei:
        await GroqProvider().complete("s", "u", Greeting)
    assert ei.value.retry_after == 30.0


async def test_groq_429_without_header_uses_default(monkeypatch):
    monkeypatch.setattr(settings, "default_retry_after_seconds", 60.0)
    _groq_raising(FakeRateLimitError(None), monkeypatch)
    with pytest.raises(RateLimited) as ei:
        await GroqProvider().complete("s", "u", Greeting)
    assert ei.value.retry_after == 60.0


async def test_groq_non_429_error_propagates_unchanged(monkeypatch):
    _groq_raising(RuntimeError("network down"), monkeypatch)
    with pytest.raises(RuntimeError):
        await GroqProvider().complete("s", "u", Greeting)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_provider_rate_limited.py -v`
Expected: FAIL — `ImportError: cannot import name 'RateLimited'` (and `parse_retry_after`).

- [ ] **Step 3: Add the exception + helpers to `app/providers/base.py`**

Append to `app/providers/base.py` (keep existing `LLMResponse` / `LLMProvider`):

```python
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


class RateLimited(Exception):
    """Raised by an adapter when the provider returns HTTP 429.

    ``retry_after`` is seconds until a retry is allowed, taken from the
    provider's ``Retry-After`` header (or ``DEFAULT_RETRY_AFTER_SECONDS`` when
    the header is absent/unparseable). It is the SINGLE quota signal the
    dispatcher understands — a short value means a per-minute limit, a long
    value means a daily limit; the code does not distinguish them.
    """

    def __init__(self, retry_after: float):
        super().__init__(f"rate limited; retry after {retry_after}s")
        self.retry_after = retry_after


def parse_retry_after(value: str | None, now: datetime, default: float) -> float:
    """Parse a ``Retry-After`` header value (delta-seconds or HTTP-date)."""
    if value is None:
        return default
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        dt = parsedate_to_datetime(value)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (dt - now).total_seconds())
    except (TypeError, ValueError):
        pass
    return default


def rate_limited_or_none(exc: Exception, now: datetime, default: float) -> "RateLimited | None":
    """Return a ``RateLimited`` if ``exc`` is a 429 transport error, else None.

    SDK-agnostic: OpenAI/Groq errors expose ``.status_code``; google-genai's
    ``APIError`` exposes ``.code``. Headers (if any) live on ``.response.headers``.
    """
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status != 429:
        return None
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    retry_after = parse_retry_after(headers.get("retry-after"), now, default)
    return RateLimited(retry_after)
```

- [ ] **Step 4: Wrap each adapter's `create()`/`generate_content()` call**

In **`app/providers/groq.py`** and **`app/providers/github_models.py`**, add imports and wrap the existing `create(...)` call. Groq example (apply the identical pattern to `github_models.py`, whose call is `self._client.chat.completions.create(...)` too):

```python
from datetime import datetime, timezone

from app.providers.base import LLMResponse, rate_limited_or_none

# ... inside complete(), replace the bare `response = await self._client...create(...)`:
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _schema_system_prompt(system, schema)},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
            )
        except Exception as exc:  # noqa: BLE001 - re-raised unless it's a 429
            rl = rate_limited_or_none(
                exc, now=datetime.now(timezone.utc), default=settings.default_retry_after_seconds
            )
            if rl is not None:
                raise rl from exc
            raise
```

In **`app/providers/google_genai.py`**, wrap the `generate_content(...)` call inside `_complete(...)` with the same `try/except` block (it already imports `settings`); add `from datetime import datetime, timezone` and `rate_limited_or_none` to its imports. `google-genai`'s `APIError.code == 429` is picked up by `rate_limited_or_none` via the `.code` branch.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_provider_rate_limited.py -v`
Expected: PASS (6 tests).

- [ ] **Step 6: Confirm no regression in existing provider tests + lint**

Run: `uv run pytest tests/test_groq_provider.py tests/test_github_models_provider.py tests/test_providers.py -v && uv run ruff check .`
Expected: PASS, no lint errors.

- [ ] **Step 7: Commit**

```bash
git add app/providers/base.py app/providers/groq.py app/providers/github_models.py app/providers/google_genai.py tests/test_provider_rate_limited.py
git commit -m "feat(providers): raise RateLimited(retry_after) on 429"
```

---

### Task 2: SQLite ticket store

**Files:**
- Create: `app/queue/__init__.py`, `app/queue/store.py`
- Modify: `app/config.py`, `.gitignore`
- Test: `tests/test_queue_store.py`

**Interfaces:**
- Consumes: `settings.queue_db_path` (new, Task 2 adds it).
- Produces (all read `settings.queue_db_path`; times are ISO-8601 UTC strings passed in by the caller for determinism):
  - `@dataclass class Ticket` with fields `id:int, repo_full_name:str, pr_number:int, head_sha:str|None, status:str, provider:str, not_before:str|None, attempts:int, comment_id:int|None, enqueued_at:str, updated_at:str`
  - `def init_db() -> None`
  - `def enqueue_or_update(*, repo_full_name:str, pr_number:int, head_sha:str|None, provider:str, now:str) -> int`
  - `def claim_next_due(now:str) -> Ticket | None`
  - `def defer(ticket_id:int, not_before:str, now:str) -> None`
  - `def mark_done(ticket_id:int, now:str, comment_id:int|None=None) -> None`
  - `def recover_on_startup(now:str) -> None`
  - `def get_ticket(ticket_id:int) -> Ticket | None`  (test/inspection helper)

- [ ] **Step 1: Add config + gitignore (folded into this task — the store needs them)**

In `app/config.py`, add these fields to `Settings` (after `github_models_model`):

```python
    queue_db_path: str = "./queue.db"
    default_retry_after_seconds: float = 60.0
    dispatcher_idle_sleep_seconds: float = 1.0
```

Append to `.gitignore`:

```
queue.db
queue.db-wal
queue.db-shm
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_queue_store.py
"""Ticket store: enqueue/collapse, atomic claim, defer, recover-on-startup.

Uses a temp DB path via monkeypatching settings.queue_db_path (matching the
codebase's settings-monkeypatch convention). No network, no real time.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.queue import store

T0 = "2026-01-01T12:00:00+00:00"
T1 = "2026-01-01T12:00:01+00:00"
FUTURE = "2026-01-01T18:00:00+00:00"
PAST = "2026-01-01T06:00:00+00:00"


@pytest.fixture(autouse=True)
def _temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "queue_db_path", str(tmp_path / "queue.db"))
    store.init_db()
    yield


def _enqueue(repo="owner/repo", pr=1, sha="sha1", provider="groq", now=T0):
    return store.enqueue_or_update(
        repo_full_name=repo, pr_number=pr, head_sha=sha, provider=provider, now=now
    )


def test_enqueue_creates_pending_ticket():
    tid = _enqueue()
    t = store.get_ticket(tid)
    assert t.status == "pending"
    assert t.repo_full_name == "owner/repo"
    assert t.pr_number == 1
    assert t.head_sha == "sha1"
    assert t.provider == "groq"
    assert t.not_before is None


def test_enqueue_same_pr_collapses_and_updates_head_sha():
    tid1 = _enqueue(sha="sha1")
    tid2 = _enqueue(sha="sha2", now=T1)
    assert tid1 == tid2  # one row per (repo, pr)
    assert store.get_ticket(tid1).head_sha == "sha2"


def test_claim_next_due_returns_pending_and_marks_running():
    tid = _enqueue()
    claimed = store.claim_next_due(now=T1)
    assert claimed.id == tid
    assert store.get_ticket(tid).status == "running"


def test_claim_next_due_returns_none_when_empty():
    assert store.claim_next_due(now=T1) is None


def test_claim_is_fifo_by_enqueued_at():
    a = _enqueue(pr=1, now=T0)
    _enqueue(pr=2, now=T1)
    assert store.claim_next_due(now=T1).id == a


def test_deferred_ticket_not_claimed_before_not_before():
    tid = _enqueue()
    store.claim_next_due(now=T0)          # -> running
    store.defer(tid, not_before=FUTURE, now=T0)
    assert store.claim_next_due(now=T1) is None            # not yet due
    assert store.claim_next_due(now=FUTURE).id == tid       # due now


def test_mark_done_sets_status_and_comment_id():
    tid = _enqueue()
    store.claim_next_due(now=T0)
    store.mark_done(tid, now=T1, comment_id=555)
    t = store.get_ticket(tid)
    assert t.status == "done"
    assert t.comment_id == 555


def test_recover_on_startup_resets_running_to_pending():
    tid = _enqueue()
    store.claim_next_due(now=T0)          # -> running
    store.recover_on_startup(now=T1)
    assert store.get_ticket(tid).status == "pending"


def test_defer_increments_attempts():
    tid = _enqueue()
    store.claim_next_due(now=T0)
    store.defer(tid, not_before=FUTURE, now=T0)
    assert store.get_ticket(tid).attempts == 1
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_queue_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.queue'`.

- [ ] **Step 4: Create the package + store**

`app/queue/__init__.py`:

```python
```

(Empty file — package marker.)

`app/queue/store.py`:

```python
"""Durable ticket store (stdlib sqlite3) — the queue's source of truth.

One row per (repo, pr): UNIQUE collapses re-triggers so a new push updates the
existing ticket's head_sha instead of stacking a duplicate review. A ticket's
persisted ``not_before`` is what actually prevents an early run after a restart
(the dispatcher's in-memory blocked_until is only a soft optimization).

Times are ISO-8601 UTC strings, passed in by the caller so tests are
deterministic. sqlite writes are sub-millisecond at demo scale; the dispatcher
and webhook call these directly (documented acceptable, single-instance).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id              INTEGER PRIMARY KEY,
    repo_full_name  TEXT    NOT NULL,
    pr_number       INTEGER NOT NULL,
    head_sha        TEXT,
    status          TEXT    NOT NULL,
    provider        TEXT    NOT NULL,
    not_before      TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    comment_id      INTEGER,
    enqueued_at     TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    UNIQUE(repo_full_name, pr_number)
);
"""


@dataclass
class Ticket:
    id: int
    repo_full_name: str
    pr_number: int
    head_sha: str | None
    status: str
    provider: str
    not_before: str | None
    attempts: int
    comment_id: int | None
    enqueued_at: str
    updated_at: str


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.queue_db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)


def _row_to_ticket(row: sqlite3.Row) -> Ticket:
    return Ticket(**{k: row[k] for k in row.keys()})


def enqueue_or_update(
    *, repo_full_name: str, pr_number: int, head_sha: str | None, provider: str, now: str
) -> int:
    """Insert a pending ticket, or collapse onto the existing one for this PR.

    On conflict: update head_sha and re-arm to 'pending' (clearing not_before)
    UNLESS the ticket is currently 'running' — a running review is left to
    finish; the newer head_sha is still recorded.
    """
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO tickets
              (repo_full_name, pr_number, head_sha, status, provider,
               not_before, attempts, comment_id, enqueued_at, updated_at)
            VALUES (?, ?, ?, 'pending', ?, NULL, 0, NULL, ?, ?)
            ON CONFLICT(repo_full_name, pr_number) DO UPDATE SET
              head_sha   = excluded.head_sha,
              status     = CASE WHEN tickets.status = 'running'
                                THEN 'running' ELSE 'pending' END,
              not_before = CASE WHEN tickets.status = 'running'
                                THEN tickets.not_before ELSE NULL END,
              updated_at = excluded.updated_at
            """,
            (repo_full_name, pr_number, head_sha, provider, now, now),
        )
        row = conn.execute(
            "SELECT id FROM tickets WHERE repo_full_name = ? AND pr_number = ?",
            (repo_full_name, pr_number),
        ).fetchone()
        return int(row["id"])


def claim_next_due(now: str) -> Ticket | None:
    """Claim the oldest due ticket (pending, or deferred whose not_before passed).

    Atomic: the UPDATE-to-running only succeeds if the row is still claimable,
    so a second concurrent claim of the same row is impossible.
    """
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM tickets
            WHERE status = 'pending'
               OR (status = 'deferred' AND not_before IS NOT NULL AND not_before <= ?)
            ORDER BY enqueued_at ASC, id ASC
            LIMIT 1
            """,
            (now,),
        ).fetchone()
        if row is None:
            return None
        cur = conn.execute(
            "UPDATE tickets SET status = 'running', updated_at = ? "
            "WHERE id = ? AND status IN ('pending', 'deferred')",
            (now, row["id"]),
        )
        if cur.rowcount != 1:
            return None
        claimed = conn.execute("SELECT * FROM tickets WHERE id = ?", (row["id"],)).fetchone()
        return _row_to_ticket(claimed)


def defer(ticket_id: int, not_before: str, now: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'deferred', not_before = ?, "
            "attempts = attempts + 1, updated_at = ? WHERE id = ?",
            (not_before, now, ticket_id),
        )


def mark_done(ticket_id: int, now: str, comment_id: int | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'done', comment_id = ?, updated_at = ? WHERE id = ?",
            (comment_id, now, ticket_id),
        )


def recover_on_startup(now: str) -> None:
    """Reset any ticket interrupted mid-review (crash) back to pending."""
    with _connect() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'pending', updated_at = ? WHERE status = 'running'",
            (now,),
        )


def get_ticket(ticket_id: int) -> Ticket | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return _row_to_ticket(row) if row else None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_queue_store.py -v`
Expected: PASS (9 tests).

- [ ] **Step 6: Lint**

Run: `uv run ruff check .`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add app/queue/__init__.py app/queue/store.py app/config.py .gitignore tests/test_queue_store.py
git commit -m "feat(queue): durable SQLite ticket store"
```

---

### Task 3: `attempt_review` outcome in the orchestrator

**Files:**
- Modify: `app/orchestrator.py`, `app/specialists/base.py`
- Test: `tests/test_orchestrator_rate_limited.py`

**Interfaces:**
- Consumes: `RateLimited` (Task 1).
- Produces:
  - `@dataclass class ReviewCompleted: review: ReviewResult`
  - `@dataclass class ReviewRateLimited: retry_after: float`
  - `async def attempt_review(repo_full_name: str, pr_number: int) -> ReviewCompleted | ReviewRateLimited` — on completion, posts the comment and returns `ReviewCompleted`; on any rate-limited call, posts **nothing** and returns `ReviewRateLimited(max retry_after)`.
  - `async def run_review(repo_full_name, pr_number) -> ReviewResult` — unchanged for callers: returns the `ReviewResult` on completion, **raises** `RateLimited` if rate-limited.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_orchestrator_rate_limited.py
"""attempt_review distinguishes a rate-limited review (defer, no comment) from a
completed one (post comment). A non-quota specialist error still COMPLETES with a
visible failed row — only a real 429 makes the whole review rate-limited.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.providers.base import RateLimited
from app.specialists.schemas import SpecialistResult


def _ok(name):
    return SpecialistResult(name=name, status="ok", findings=[], elapsed_ms=1, tokens_in=1, tokens_out=1)


@pytest.fixture(autouse=True)
def _provider(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "groq")


async def test_attempt_review_returns_rate_limited_and_posts_nothing(monkeypatch):
    import app.orchestrator as orchestrator

    monkeypatch.setattr(orchestrator.github_app, "fetch_pr_diff", lambda repo, pr: "diff")
    posted = []
    monkeypatch.setattr(orchestrator.github_app, "upsert_comment", lambda *a, **k: posted.append(a))

    async def sec(_):
        return _ok("Security")

    async def perf(_):
        raise RateLimited(30.0)

    async def qual(_):
        raise RateLimited(45.0)

    monkeypatch.setattr(orchestrator, "run_security_specialist", sec)
    monkeypatch.setattr(orchestrator, "run_performance_specialist", perf)
    monkeypatch.setattr(orchestrator, "run_quality_specialist", qual)

    outcome = await orchestrator.attempt_review("owner/repo", 1)

    assert isinstance(outcome, orchestrator.ReviewRateLimited)
    assert outcome.retry_after == 45.0  # max of the two
    assert posted == []                 # no comment on a rate-limited review


async def test_attempt_review_completes_and_posts_when_ok(monkeypatch):
    import app.orchestrator as orchestrator

    monkeypatch.setattr(orchestrator.github_app, "fetch_pr_diff", lambda repo, pr: "diff")
    posted = {}
    monkeypatch.setattr(
        orchestrator.github_app, "upsert_comment",
        lambda repo, pr, body: posted.update(body=body) or "cid",
    )

    async def mk(name):
        async def _inner(_):
            return _ok(name)
        return _inner

    monkeypatch.setattr(orchestrator, "run_security_specialist", await mk("Security"))
    monkeypatch.setattr(orchestrator, "run_performance_specialist", await mk("Performance"))
    monkeypatch.setattr(orchestrator, "run_quality_specialist", await mk("Code Quality"))

    outcome = await orchestrator.attempt_review("owner/repo", 2)

    assert isinstance(outcome, orchestrator.ReviewCompleted)
    assert outcome.review.pr_number == 2
    assert "PR #2" in posted["body"]


async def test_run_review_raises_on_rate_limited(monkeypatch):
    import app.orchestrator as orchestrator

    monkeypatch.setattr(orchestrator.github_app, "fetch_pr_diff", lambda repo, pr: "diff")
    monkeypatch.setattr(orchestrator.github_app, "upsert_comment", lambda *a, **k: None)

    async def rl(_):
        raise RateLimited(12.0)

    async def ok(_):
        return _ok("Security")

    monkeypatch.setattr(orchestrator, "run_security_specialist", ok)
    monkeypatch.setattr(orchestrator, "run_performance_specialist", rl)
    monkeypatch.setattr(orchestrator, "run_quality_specialist", ok)

    with pytest.raises(RateLimited):
        await orchestrator.run_review("owner/repo", 3)


async def test_run_specialist_lets_rate_limited_escape(monkeypatch):
    """run_specialist normally never raises — but RateLimited MUST escape so the
    orchestrator can defer instead of rendering a failed row."""
    import app.specialists.base as base

    class FakeProvider:
        async def complete(self, system, user, schema):
            raise RateLimited(20.0)

    monkeypatch.setattr(base, "get_provider", lambda: FakeProvider())

    from app.specialists.security import SecurityFindings, SECURITY_SYSTEM_PROMPT

    with pytest.raises(RateLimited):
        await base.run_specialist(
            name="Security",
            annotated_diff="diff",
            system_prompt=SECURITY_SYSTEM_PROMPT,
            container_schema=SecurityFindings,
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_orchestrator_rate_limited.py -v`
Expected: FAIL — `AttributeError: module 'app.orchestrator' has no attribute 'ReviewRateLimited'` / `attempt_review`.

- [ ] **Step 3: Let `RateLimited` escape `run_specialist`**

In `app/specialists/base.py`, add the import and a targeted re-raise **above** the broad `except Exception`:

```python
from app.providers.base import RateLimited
```

```python
    except RateLimited:
        raise  # must reach the orchestrator so it can defer, not render a failed row
    except Exception as exc:  # noqa: BLE001 - a specialist must never crash the orchestrator
        return SpecialistResult(
            ...
        )
```

- [ ] **Step 4: Refactor `orchestrator.py` into `attempt_review` + wrapper `run_review`**

Replace the body of `app/orchestrator.py`'s `run_review` with the split below (keep all existing imports; add `from dataclasses import dataclass` and `from app.providers.base import RateLimited`):

```python
@dataclass
class ReviewCompleted:
    review: ReviewResult


@dataclass
class ReviewRateLimited:
    retry_after: float


async def attempt_review(
    repo_full_name: str, pr_number: int
) -> ReviewCompleted | ReviewRateLimited:
    """Run the full review once. Atomic: if any call is rate-limited, discard all
    partial results and return ReviewRateLimited WITHOUT posting a comment."""
    started = time.monotonic()

    raw_diff = github_app.fetch_pr_diff(repo_full_name, pr_number)
    annotated = annotate_and_cap(raw_diff)

    raw_results = await asyncio.gather(
        run_security_specialist(annotated.text),
        run_performance_specialist(annotated.text),
        run_quality_specialist(annotated.text),
        return_exceptions=True,
    )

    rate_limits = [r.retry_after for r in raw_results if isinstance(r, RateLimited)]
    if rate_limits:
        return ReviewRateLimited(retry_after=max(rate_limits))

    results = [
        outcome
        if isinstance(outcome, SpecialistResult)
        else SpecialistResult(name=name, status="failed", findings=[], error=str(outcome), elapsed_ms=0)
        for name, outcome in zip(_SPECIALIST_NAMES, raw_results)
    ]

    total_tokens_in = sum(r.tokens_in for r in results)
    total_tokens_out = sum(r.tokens_out for r in results)
    total_elapsed_ms = int((time.monotonic() - started) * 1000)

    provider = settings.llm_provider
    model = _active_model()
    est_cost_usd = estimate_cost_usd(provider, model, total_tokens_in, total_tokens_out)

    review_result = ReviewResult(
        pr_number=pr_number,
        provider=provider,
        model=model,
        results=results,
        total_elapsed_ms=total_elapsed_ms,
        total_tokens_in=total_tokens_in,
        total_tokens_out=total_tokens_out,
        est_cost_usd=est_cost_usd,
    )

    body = format_comment(review_result)
    github_app.upsert_comment(repo_full_name, pr_number, body)
    return ReviewCompleted(review=review_result)


async def run_review(repo_full_name: str, pr_number: int) -> ReviewResult:
    """Back-compat entry point for scripts/tests: returns the ReviewResult on
    completion, raises RateLimited if the review was rate-limited."""
    outcome = await attempt_review(repo_full_name, pr_number)
    if isinstance(outcome, ReviewRateLimited):
        raise RateLimited(outcome.retry_after)
    return outcome.review
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_orchestrator_rate_limited.py tests/test_orchestrator.py tests/test_specialists_base.py -v`
Expected: PASS (new 4 + existing orchestrator/specialist tests still green — `run_review` behavior unchanged for the completed/partial-failure cases).

- [ ] **Step 6: Lint**

Run: `uv run ruff check .`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add app/orchestrator.py app/specialists/base.py tests/test_orchestrator_rate_limited.py
git commit -m "feat(orchestrator): attempt_review returns Completed | RateLimited"
```

---

### Task 4: Placeholder comment formatting

**Files:**
- Modify: `app/formatting.py`
- Test: `tests/test_placeholder_formatting.py`

**Interfaces:**
- Produces:
  - `PLACEHOLDER_DAILY_THRESHOLD_SECONDS = 300`
  - `def format_placeholder(pr_number: int, retry_after: float, now: datetime) -> str` — marker-prefixed Markdown. Short wait → "rate limit, shortly"; long wait → "daily quota reached … ~HH:MM UTC" using `now + retry_after`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_placeholder_formatting.py
from __future__ import annotations

from datetime import datetime, timezone

from app.formatting import format_placeholder
from app.github_app import COMMENT_MARKER

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_short_wait_is_rate_limit_wording_and_has_marker():
    body = format_placeholder(pr_number=42, retry_after=30.0, now=NOW)
    assert COMMENT_MARKER in body
    assert "PR #42" in body
    assert "rate limit" in body.lower()


def test_long_wait_is_daily_quota_wording_with_eta_and_marker():
    # 6 hours -> 18:00 UTC
    body = format_placeholder(pr_number=42, retry_after=6 * 3600, now=NOW)
    assert COMMENT_MARKER in body
    assert "daily" in body.lower()
    assert "18:00 UTC" in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_placeholder_formatting.py -v`
Expected: FAIL — `ImportError: cannot import name 'format_placeholder'`.

- [ ] **Step 3: Add `format_placeholder` to `app/formatting.py`**

Add imports (`from datetime import datetime, timedelta`) and:

```python
PLACEHOLDER_DAILY_THRESHOLD_SECONDS = 300


def format_placeholder(pr_number: int, retry_after: float, now: datetime) -> str:
    """Marker-prefixed placeholder comment shown while a review is delayed.

    The real result later edits this same comment in place (found via the
    marker). Wording is chosen by wait magnitude: short = per-minute rate
    limit; long = daily quota, with an ETA computed from ``now + retry_after``.
    """
    header = f"## 🤖 Automated Code Review — PR #{pr_number}\n"
    if retry_after < PLACEHOLDER_DAILY_THRESHOLD_SECONDS:
        note = "⏳ Queued behind rate limit — review will appear shortly."
    else:
        eta = (now + timedelta(seconds=retry_after)).strftime("%H:%M UTC")
        note = (
            "⏳ Daily model quota reached — review queued, will post "
            f"automatically after the provider's limit resets (~{eta})."
        )
    return f"{COMMENT_MARKER}\n{header}\n_{note}_\n"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_placeholder_formatting.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add app/formatting.py tests/test_placeholder_formatting.py
git commit -m "feat(formatting): delayed-review placeholder comment"
```

---

### Task 5: Dispatcher — `blocked_until` gate + `process_next_due`

**Files:**
- Create: `app/queue/dispatcher.py`
- Test: `tests/test_dispatcher.py`

**Interfaces:**
- Consumes: `store` (Task 2), `attempt_review`/`ReviewCompleted`/`ReviewRateLimited` (Task 3), `format_placeholder` (Task 4), `github_app.upsert_comment`, `settings`.
- Produces:
  - `_blocked_until: dict[str, datetime]` + `def reset_blocked_until() -> None`
  - `@dataclass class StepResult: action: str; ticket_id: int | None = None`  (`action ∈ {"idle","ran","deferred"}`)
  - `async def process_next_due(now: datetime) -> StepResult`
  - `async def run_forever() -> None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dispatcher.py
"""Dispatcher step logic: burst (RPM defer + later run) and daily-wall defer.

Tests drive process_next_due(now) directly with an injected clock and stubbed
attempt_review — the infinite run_forever loop is a thin wrapper and is not
unit-tested. Uses a temp DB and a cleared blocked_until map.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.queue import dispatcher, store
import app.orchestrator as orchestrator

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "queue_db_path", str(tmp_path / "queue.db"))
    monkeypatch.setattr(settings, "llm_provider", "groq")
    store.init_db()
    dispatcher.reset_blocked_until()
    yield
    dispatcher.reset_blocked_until()


def _enqueue(pr, now=NOW):
    return store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=pr, head_sha="sha", provider="groq",
        now=now.isoformat(),
    )


def _stub_comments(monkeypatch):
    posted = []
    monkeypatch.setattr(dispatcher.github_app, "upsert_comment",
                        lambda repo, pr, body: posted.append((pr, body)))
    return posted


async def test_idle_when_no_tickets(monkeypatch):
    _stub_comments(monkeypatch)
    result = await dispatcher.process_next_due(NOW)
    assert result.action == "idle"


async def test_completed_ticket_runs_and_marks_done(monkeypatch):
    _stub_comments(monkeypatch)
    tid = _enqueue(pr=1)

    async def fake_attempt(repo, pr):
        review = type("R", (), {})()
        return orchestrator.ReviewCompleted(review=review)

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "ran"
    assert store.get_ticket(tid).status == "done"


async def test_rate_limited_ticket_defers_posts_placeholder_and_blocks(monkeypatch):
    posted = _stub_comments(monkeypatch)
    tid = _enqueue(pr=2)

    async def fake_attempt(repo, pr):
        return orchestrator.ReviewRateLimited(retry_after=30.0)

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.not_before == (NOW + timedelta(seconds=30)).isoformat()
    assert posted and posted[0][0] == 2            # placeholder posted on PR 2
    assert dispatcher._blocked_until["groq"] == NOW + timedelta(seconds=30)


async def test_blocked_provider_defers_without_calling_attempt(monkeypatch):
    posted = _stub_comments(monkeypatch)
    _enqueue(pr=3)
    dispatcher._blocked_until["groq"] = NOW + timedelta(seconds=120)

    called = []

    async def fake_attempt(repo, pr):
        called.append(pr)
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)
    assert result.action == "deferred"
    assert called == []                            # never fired a doomed call
    assert posted and "rate limit" in posted[0][1].lower()


async def test_daily_wall_defers_then_runs_after_reset(monkeypatch):
    _stub_comments(monkeypatch)
    tid = _enqueue(pr=4)

    async def rate_limited(repo, pr):
        return orchestrator.ReviewRateLimited(retry_after=6 * 3600)

    monkeypatch.setattr(dispatcher, "attempt_review", rate_limited)
    await dispatcher.process_next_due(NOW)
    assert store.get_ticket(tid).status == "deferred"

    # Before reset: nothing is due.
    assert (await dispatcher.process_next_due(NOW + timedelta(hours=1))).action == "idle"

    # After reset: blocked_until has passed, ticket runs.
    async def ok(repo, pr):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", ok)
    later = NOW + timedelta(hours=7)
    result = await dispatcher.process_next_due(later)
    assert result.action == "ran"
    assert store.get_ticket(tid).status == "done"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_dispatcher.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.queue.dispatcher'`.

- [ ] **Step 3: Create `app/queue/dispatcher.py`**

```python
"""The single serial consumer of the ticket queue.

It is the ONLY caller of the review path, so all pacing/quota decisions are
serialized. ``blocked_until`` is a per-provider soft gate learned only from
Retry-After (via ReviewRateLimited) so we don't fire calls we know will fail;
it is intentionally in-memory — the durable truth is each ticket's not_before.

Delay handling is uniform: any ticket that can't run now gets a placeholder
comment (the notification) and is deferred; the real result later edits that
same comment in place via the comment marker.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app import github_app
from app.config import settings
from app.formatting import format_placeholder
from app.orchestrator import ReviewRateLimited, attempt_review
from app.queue import store

logger = logging.getLogger(__name__)

_blocked_until: dict[str, datetime] = {}


def reset_blocked_until() -> None:
    """Clear the in-memory provider block map (used to isolate tests)."""
    _blocked_until.clear()


@dataclass
class StepResult:
    action: str  # "idle" | "ran" | "deferred"
    ticket_id: int | None = None


def _post_placeholder(repo: str, pr: int, retry_after: float, now: datetime) -> None:
    github_app.upsert_comment(repo, pr, format_placeholder(pr, retry_after, now))


async def process_next_due(now: datetime) -> StepResult:
    """Claim and process one due ticket. Returns what happened."""
    ticket = store.claim_next_due(now.isoformat())
    if ticket is None:
        return StepResult(action="idle")

    provider = ticket.provider
    blocked = _blocked_until.get(provider)
    if blocked is not None and now < blocked:
        remaining = (blocked - now).total_seconds()
        store.defer(ticket.id, not_before=blocked.isoformat(), now=now.isoformat())
        _post_placeholder(ticket.repo_full_name, ticket.pr_number, remaining, now)
        return StepResult(action="deferred", ticket_id=ticket.id)

    outcome = await attempt_review(ticket.repo_full_name, ticket.pr_number)

    if isinstance(outcome, ReviewRateLimited):
        until = now + timedelta(seconds=outcome.retry_after)
        _blocked_until[provider] = until
        store.defer(ticket.id, not_before=until.isoformat(), now=now.isoformat())
        _post_placeholder(ticket.repo_full_name, ticket.pr_number, outcome.retry_after, now)
        return StepResult(action="deferred", ticket_id=ticket.id)

    store.mark_done(ticket.id, now=now.isoformat())
    return StepResult(action="ran", ticket_id=ticket.id)


async def run_forever() -> None:
    """Production loop: drain the queue, idling when empty. Thin wrapper over
    process_next_due (which holds the tested logic)."""
    while True:
        try:
            result = await process_next_due(datetime.now(timezone.utc))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the dispatcher must never die on one ticket
            logger.exception("dispatcher step failed")
            result = StepResult(action="idle")
        if result.action == "idle":
            await asyncio.sleep(settings.dispatcher_idle_sleep_seconds)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_dispatcher.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Lint**

Run: `uv run ruff check .`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add app/queue/dispatcher.py tests/test_dispatcher.py
git commit -m "feat(queue): serial dispatcher with blocked_until gate"
```

---

### Task 6: Webhook producer + app lifespan wiring

**Files:**
- Modify: `app/webhook.py`, `app/main.py`
- Test: `tests/test_webhook.py`

**Interfaces:**
- Consumes: `store.enqueue_or_update` (Task 2), `store.init_db`/`recover_on_startup`, `dispatcher.run_forever` (Task 5).
- Produces: webhook enqueues a durable ticket (no per-request background task); the app starts one dispatcher at lifespan.

- [ ] **Step 1: Update `tests/test_webhook.py`**

Replace the two orchestrator-triggering tests (`test_opened_action_triggers_orchestrator`, `test_ignored_action_does_not_trigger_orchestrator`) and the replay test's `run_review` monkeypatch with ticket-store assertions. Add a temp-DB fixture; the signature/dedup tests are unchanged.

```python
# add near the top imports
from app.queue import store

# extend the autouse fixture to give each test an isolated DB
@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "github_webhook_secret", TEST_SECRET)
    monkeypatch.setattr(settings, "queue_db_path", str(tmp_path / "queue.db"))
    monkeypatch.setattr(settings, "llm_provider", "groq")
    store.init_db()
    webhook.reset_dedup_cache()
    yield
    webhook.reset_dedup_cache()
```

```python
async def test_opened_action_enqueues_ticket():
    payload = {
        "action": "opened",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 7, "head": {"sha": "abc123"}},
    }
    body = json.dumps(payload).encode()
    headers = {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "55555555-5555-5555-5555-555555555555",
    }
    async with await _client() as c:
        response = await c.post("/webhook", content=body, headers=headers)

    assert response.status_code == 202
    ticket = store.claim_next_due(now="2026-01-01T12:00:00+00:00")
    assert ticket is not None
    assert ticket.repo_full_name == "owner/repo"
    assert ticket.pr_number == 7
    assert ticket.head_sha == "abc123"


async def test_ignored_action_does_not_enqueue():
    payload = {
        "action": "closed",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 7, "head": {"sha": "abc123"}},
    }
    body = json.dumps(payload).encode()
    headers = {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "66666666-6666-6666-6666-666666666666",
    }
    async with await _client() as c:
        response = await c.post("/webhook", content=body, headers=headers)

    assert response.status_code == 202
    assert store.claim_next_due(now="2026-01-01T12:00:00+00:00") is None
```

In `test_replayed_delivery_id_is_noop`, remove the `monkeypatch.setattr(webhook, "run_review", fake_run_review)` block and the `assert len(calls) == 1`; keep the `first == 202` / `second == 200` assertions (dedup is unchanged).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_webhook.py -v`
Expected: FAIL — the new tests reference behavior not yet implemented (`store` has no ticket / webhook still uses background task).

- [ ] **Step 3: Rewrite `app/webhook.py`'s enqueue path**

Replace the `run_review` background-task function and the `background_tasks.add_task(...)` call. Keep HMAC verification, dedup, action filter, and the `202`. New shape:

```python
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Response

from app.config import settings
from app.hmac_verify import verify_signature
from app.queue import store

# ... keep logger, _REVIEW_TRIGGER_ACTIONS, router, dedup cache/helpers ...


def _enqueue_from_payload(payload: dict) -> None:
    """Enqueue a durable review ticket for a triggering PR action (no-op otherwise)."""
    if payload.get("action") not in _REVIEW_TRIGGER_ACTIONS:
        return
    pull_request = payload.get("pull_request") or {}
    repository = payload.get("repository") or {}
    repo_full_name = repository.get("full_name")
    pr_number = pull_request.get("number")
    if not repo_full_name or pr_number is None:
        logger.warning("pull_request webhook missing repo/pr number; skipping enqueue")
        return
    head_sha = (pull_request.get("head") or {}).get("sha")
    store.enqueue_or_update(
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        head_sha=head_sha,
        provider=settings.llm_provider,
        now=datetime.now(timezone.utc).isoformat(),
    )


@router.post("/webhook")
async def webhook(request: Request) -> Response:
    raw_body = await request.body()
    signature_header = request.headers.get("X-Hub-Signature-256")
    delivery_id = request.headers.get("X-GitHub-Delivery")

    if not verify_signature(raw_body, signature_header, settings.github_webhook_secret):
        logger.warning("Rejected webhook: invalid signature (delivery=%s)", delivery_id)
        return Response(status_code=401)

    if delivery_id is not None and _is_duplicate_delivery(delivery_id):
        return Response(status_code=200, content="already processed")

    payload = json.loads(raw_body)
    _enqueue_from_payload(payload)
    return Response(status_code=202)
```

Remove the now-unused `BackgroundTasks` import and the old `run_review`/`_orchestrator_run_review` import.

- [ ] **Step 4: Wire the dispatcher into `app/main.py` via lifespan**

```python
import asyncio
import contextlib
from datetime import datetime, timezone

from fastapi import FastAPI

from app.config import settings
from app.queue import dispatcher, store
from app.webhook import router as webhook_router


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    store.init_db()
    store.recover_on_startup(datetime.now(timezone.utc).isoformat())
    task = asyncio.create_task(dispatcher.run_forever())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="pr-review-engine", lifespan=lifespan)
app.include_router(webhook_router)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
```

- [ ] **Step 5: Run the full suite to verify pass + no regressions**

Run: `uv run pytest -v`
Expected: PASS across all layers. The webhook tests using `ASGITransport(app=app)` now exercise the lifespan; the dispatcher idles harmlessly against the per-test temp DB.

- [ ] **Step 6: Lint + boot smoke check**

Run: `uv run ruff check . && uv run python -c "from app.main import app; print('import ok')"`
Expected: no lint errors; prints `import ok`.

- [ ] **Step 7: Commit**

```bash
git add app/webhook.py app/main.py tests/test_webhook.py
git commit -m "feat(webhook): enqueue durable tickets; start dispatcher at lifespan"
```

---

### Task 7: Documentation

**Files:**
- Modify: `SPEC.md`, `README.md`, `cost.md`

**Interfaces:** none (docs only).

- [ ] **Step 1: Update `SPEC.md`**

Add a new section (e.g. `## 12. Review queue (RPM + daily-quota handling)`) summarizing: producer/consumer split, single serial dispatcher, durable SQLite tickets (one per PR), reactive `Retry-After`-only detection (no caps), atomic reviews, placeholder→result edit-in-place, restart recovery. Cross-reference `docs/superpowers/specs/2026-07-27-queue-features-design.md` as the full design. Update the module-layout tree to include `app/queue/store.py` and `app/queue/dispatcher.py`.

- [ ] **Step 2: Update `README.md`**

Under the architecture/flow, note the webhook now enqueues a ticket and a background dispatcher runs reviews. Add to "Known limitations": single-process dispatcher (no horizontal scaling); `blocked_until` is in-memory (re-learned after restart, tickets' `not_before` is durable); the "never partial" accepted quota waste at the daily boundary; the `DEFAULT_RETRY_AFTER_SECONDS` fallback and the one-open assumption that GitHub Models sends `Retry-After` (to be confirmed with a single live call).

- [ ] **Step 3: Update `cost.md`**

Add a one-line note that the queue changes *when* LLM calls happen, not *how many*, so the documented cost model is unchanged; SQLite persistence adds no infra cost (embedded, local).

- [ ] **Step 4: Verify docs reference real paths + commit**

Run: `uv run ruff check .` (sanity; docs don't affect lint but confirms tree is clean)

```bash
git add SPEC.md README.md cost.md
git commit -m "docs: document the durable review queue"
```

---

## Self-Review (completed by plan author)

**1. Spec coverage** — every spec section maps to a task:
- §3 durable SQLite queue → Task 2. §3 reactive `Retry-After`/atomic → Tasks 1, 3. §5 producer/consumer → Task 6 (producer), Task 5 (consumer). §6.1 ticket schema → Task 2. §6.2 `blocked_until` in-memory → Task 5. §7 per-PR collapsing → Task 2 (`UNIQUE` upsert). §8 `RateLimited` seam → Task 1, propagation Task 3. §9 placeholder→result → Task 4 (render) + Task 5 (post) + marker-based replacement via existing `upsert_comment`. §10 restart recovery → Task 2 (`recover_on_startup`) + Task 6 (lifespan). §11 config/modules → Tasks 2, 5, 6. §12 tests → each task's test file. §13 ping feature → explicitly out of scope.
- Deliberate simplification vs. spec: placeholder→result replacement relies on the existing comment **marker** (not on `comment_id`); `comment_id` is stored (`mark_done` accepts it) but not load-bearing here — it remains available for the §13 ping feature.

**2. Placeholder scan** — no "TBD/TODO/handle appropriately"; every code and test step contains runnable content.

**3. Type consistency** — names match across tasks: `RateLimited.retry_after` (Task 1) consumed in Tasks 3/5; `ReviewCompleted`/`ReviewRateLimited` (Task 3) consumed in Task 5; `store.*` signatures (Task 2) used verbatim in Tasks 5/6; `format_placeholder(pr_number, retry_after, now)` (Task 4) called with the same args in Task 5; `StepResult.action` values `{"idle","ran","deferred"}` are the ones asserted in Task 5 tests.
