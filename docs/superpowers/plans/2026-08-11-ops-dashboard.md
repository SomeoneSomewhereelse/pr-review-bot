# Ops/Demo Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `/dashboard` page (and its `/api/dashboard` JSON backing endpoint) to the FastAPI app that shows live review history, cost/timing stats, and queue/ops health, with light/dark/system theming and English/Hebrew (RTL) language support — for both live demos and day-to-day ops monitoring.

**Architecture:** A new `reviews` Postgres table persists each completed `ReviewResult` (written by `orchestrator.attempt_review()` right after it posts the PR comment). A new `app/dashboard.py` module reads that table plus the existing `tickets` table and the dispatcher's in-memory backoff state to build one JSON payload, served at `GET /api/dashboard`; `GET /dashboard` serves a static HTML/CSS/JS page (`app/static/dashboard.html`) that polls that endpoint every 4s and renders it.

**Tech Stack:** FastAPI, psycopg3 (existing `app/queue/store.py` pool), plain HTML/CSS/JS (no framework, no CDN script, no build step) — per the approved design spec.

## Global Constraints

- No new Python dependency and no CDN script — plain server-rendered HTML with hand-rolled vanilla JS (spec, "Purpose" + user's explicit choice of vanilla JS over htmx).
- No auth on `/dashboard` or `/api/dashboard` (spec, "Purpose").
- Only UI chrome (labels, headers, specialist names, status words, severity tags) is translated between English/Hebrew; LLM-generated finding text (descriptions, fixes, suggestions, free-form category/type/impact strings) always renders in English (spec, "Theming, internationalization & responsive design").
- No retention/pruning logic for the `reviews` table (spec, "Data model").
- No WebSocket/SSE push, no historical charts, no languages beyond English/Hebrew, no themes beyond light/dark/system (spec, "Out of scope").
- Python 3.12, `uv`-managed deps, `ruff` line-length 100, `pytest` with `asyncio_mode = "auto"` (`pyproject.toml`).
- A failure to record a review for the dashboard, or to load dashboard data, must never break the review pipeline or crash the page — degrade visibly instead (spec, "Error handling").

---

### Task 1: `reviews` table + store read/write helpers

**Files:**
- Modify: `app/queue/store.py` (schema constant, new functions)
- Modify: `tests/conftest.py:66` (TRUNCATE statement)
- Test: `tests/test_reviews_store.py` (new)

**Interfaces:**
- Consumes: `app.specialists.schemas.ReviewResult` (existing: `pr_number`, `provider`, `model`, `results: list[SpecialistResult]`, `total_elapsed_ms`, `total_tokens_in`, `total_tokens_out`, `est_cost_usd`); `SpecialistResult.model_dump()` (existing pydantic method).
- Produces:
  - `store.record_review(repo_full_name: str, pr_number: int, review: ReviewResult, comment_id: int | None, now: str) -> None`
  - `store.dashboard_stats() -> dict` → `{"total_reviews": int, "total_cost_usd": float, "avg_elapsed_ms": int}`
  - `store.dashboard_queue_counts() -> dict[str, int]` → keys `"pending"`, `"running"`, `"deferred"`, `"retrying"`, `"done"`, `"failed"`, all present, defaulting to 0
  - `store.dashboard_reviews(limit: int = 50) -> list[dict]` → each dict has keys `repo`, `pr_number`, `provider`, `model`, `created_at`, `elapsed_ms`, `tokens_in`, `tokens_out`, `est_cost_usd`, `comment_url` (`str | None`), `specialists` (`list[dict]`, each `{"name", "status", "findings", "error", "elapsed_ms", "tokens_in", "tokens_out"}` per `SpecialistResult`)

- [ ] **Step 1: Write the failing test for the schema + round-trip**

Create `tests/test_reviews_store.py`:

```python
"""Tests for the `reviews` table: record_review() + the dashboard read
helpers. Uses the shared Postgres test harness (tests/conftest.py)."""
from __future__ import annotations

import pytest

from app.queue import store
from app.specialists.schemas import ReviewResult, SpecialistResult


@pytest.fixture(autouse=True)
def _temp_db(db):
    yield


def _review(pr_number=42, cost=0.0021) -> ReviewResult:
    return ReviewResult(
        pr_number=pr_number,
        provider="groq",
        model="llama-3.3-70b-versatile",
        results=[
            SpecialistResult(
                name="Security",
                status="ok",
                findings=[{"severity": "high", "file": "app/x.py", "line": 3,
                           "description": "d", "fix": "f"}],
                elapsed_ms=10,
                tokens_in=5,
                tokens_out=2,
            ),
            SpecialistResult(
                name="Performance",
                status="failed",
                findings=[],
                error="rate limited",
                elapsed_ms=1,
            ),
        ],
        total_elapsed_ms=11,
        total_tokens_in=5,
        total_tokens_out=2,
        est_cost_usd=cost,
    )


def test_record_review_round_trips_through_dashboard_reviews():
    store.record_review(
        "owner/repo", 42, _review(), comment_id=999, now="2026-08-11T12:00:00+00:00"
    )

    rows = store.dashboard_reviews()
    assert len(rows) == 1
    row = rows[0]
    assert row["repo"] == "owner/repo"
    assert row["pr_number"] == 42
    assert row["provider"] == "groq"
    assert row["model"] == "llama-3.3-70b-versatile"
    assert row["created_at"] == "2026-08-11T12:00:00+00:00"
    assert row["elapsed_ms"] == 11
    assert row["tokens_in"] == 5
    assert row["tokens_out"] == 2
    assert row["est_cost_usd"] == 0.0021
    assert row["comment_url"] == "https://github.com/owner/repo/pull/42#issuecomment-999"
    assert row["specialists"][0]["name"] == "Security"
    assert row["specialists"][0]["findings"][0]["severity"] == "high"
    assert row["specialists"][1]["status"] == "failed"
    assert row["specialists"][1]["error"] == "rate limited"


def test_record_review_with_no_comment_id_has_no_comment_url():
    store.record_review(
        "owner/repo", 43, _review(pr_number=43), comment_id=None, now="2026-08-11T12:00:00+00:00"
    )
    assert store.dashboard_reviews()[0]["comment_url"] is None


def test_dashboard_reviews_orders_newest_first_and_respects_limit():
    store.record_review("owner/repo", 1, _review(pr_number=1), 1, now="2026-08-11T12:00:00+00:00")
    store.record_review("owner/repo", 2, _review(pr_number=2), 2, now="2026-08-11T12:00:01+00:00")
    store.record_review("owner/repo", 3, _review(pr_number=3), 3, now="2026-08-11T12:00:02+00:00")

    rows = store.dashboard_reviews(limit=2)
    assert [r["pr_number"] for r in rows] == [3, 2]


def test_dashboard_stats_aggregates_across_all_reviews():
    store.record_review("owner/repo", 1, _review(cost=0.001), 1, now="2026-08-11T12:00:00+00:00")
    store.record_review("owner/repo", 2, _review(cost=0.002), 2, now="2026-08-11T12:00:01+00:00")

    stats = store.dashboard_stats()
    assert stats["total_reviews"] == 2
    assert stats["total_cost_usd"] == pytest.approx(0.003)
    assert stats["avg_elapsed_ms"] == 11


def test_dashboard_stats_on_empty_table():
    assert store.dashboard_stats() == {
        "total_reviews": 0, "total_cost_usd": 0.0, "avg_elapsed_ms": 0,
    }


def test_dashboard_queue_counts_includes_all_statuses_defaulted_to_zero():
    store.enqueue_or_update(
        repo_full_name="owner/repo", pr_number=1, head_sha="sha1", provider="groq",
        now="2026-08-11T12:00:00+00:00",
    )
    counts = store.dashboard_queue_counts()
    assert counts == {
        "pending": 1, "running": 0, "deferred": 0, "retrying": 0, "done": 0, "failed": 0,
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_reviews_store.py -v`
Expected: FAIL — `AttributeError: module 'app.queue.store' has no attribute 'record_review'` (and similar for the other three functions).

- [ ] **Step 3: Add the `reviews` table to the schema**

In `app/queue/store.py`, extend the `_SCHEMA` constant (the module-level string containing `CREATE TABLE IF NOT EXISTS tickets ...` and `runtime_config`) by appending a third table, right after the `runtime_config` table's closing `);`:

```sql
CREATE TABLE IF NOT EXISTS reviews (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    repo_full_name     TEXT    NOT NULL,
    pr_number          INTEGER NOT NULL,
    provider           TEXT    NOT NULL,
    model              TEXT    NOT NULL,
    comment_id         BIGINT,
    created_at         TEXT    NOT NULL,
    total_elapsed_ms   INTEGER NOT NULL,
    total_tokens_in    INTEGER NOT NULL,
    total_tokens_out   INTEGER NOT NULL,
    est_cost_usd       DOUBLE PRECISION NOT NULL,
    results            JSONB   NOT NULL
);
CREATE INDEX IF NOT EXISTS reviews_created_at_idx ON reviews (created_at DESC);
```

- [ ] **Step 4: Add the imports `record_review` needs**

Near the top of `app/queue/store.py`, alongside the existing `from psycopg.rows import dict_row` line, add:

```python
from psycopg.types.json import Jsonb
```

And alongside the other `app.*` imports, add:

```python
from app.specialists.schemas import ReviewResult
```

- [ ] **Step 5: Implement `record_review`**

Add to `app/queue/store.py` (near the other write functions, e.g. after `finalize_review`):

```python
def record_review(
    repo_full_name: str,
    pr_number: int,
    review: ReviewResult,
    comment_id: int | None,
    now: str,
) -> None:
    """Persist a completed review for the dashboard (insert-only).

    Callers must never let a failure here affect the review itself — the PR
    comment is already posted by the time this is called.
    """
    results = Jsonb([r.model_dump() for r in review.results])
    with _require_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO reviews
              (repo_full_name, pr_number, provider, model, comment_id, created_at,
               total_elapsed_ms, total_tokens_in, total_tokens_out, est_cost_usd, results)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                repo_full_name, pr_number, review.provider, review.model, comment_id, now,
                review.total_elapsed_ms, review.total_tokens_in, review.total_tokens_out,
                review.est_cost_usd, results,
            ),
        )
```

- [ ] **Step 6: Implement the three read helpers**

Add to `app/queue/store.py`, right after `record_review`:

```python
_TICKET_STATUSES = ("pending", "running", "deferred", "retrying", "done", "failed")


def dashboard_stats() -> dict:
    with _require_pool().connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(est_cost_usd), 0) AS cost, "
            "COALESCE(AVG(total_elapsed_ms), 0) AS avg_ms FROM reviews"
        ).fetchone()
    return {
        "total_reviews": int(row["n"]),
        "total_cost_usd": round(float(row["cost"]), 4),
        "avg_elapsed_ms": int(row["avg_ms"]),
    }


def dashboard_queue_counts() -> dict[str, int]:
    counts = {status: 0 for status in _TICKET_STATUSES}
    with _require_pool().connection() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM tickets GROUP BY status"
        ).fetchall()
    for row in rows:
        counts[row["status"]] = int(row["n"])
    return counts


def dashboard_reviews(limit: int = 50) -> list[dict]:
    with _require_pool().connection() as conn:
        rows = conn.execute(
            "SELECT repo_full_name, pr_number, provider, model, comment_id, created_at, "
            "total_elapsed_ms, total_tokens_in, total_tokens_out, est_cost_usd, results "
            "FROM reviews ORDER BY created_at DESC LIMIT %s",
            (limit,),
        ).fetchall()
    reviews = []
    for row in rows:
        comment_url = None
        if row["comment_id"] is not None:
            comment_url = (
                f"https://github.com/{row['repo_full_name']}/pull/"
                f"{row['pr_number']}#issuecomment-{row['comment_id']}"
            )
        reviews.append({
            "repo": row["repo_full_name"],
            "pr_number": row["pr_number"],
            "provider": row["provider"],
            "model": row["model"],
            "created_at": row["created_at"],
            "elapsed_ms": row["total_elapsed_ms"],
            "tokens_in": row["total_tokens_in"],
            "tokens_out": row["total_tokens_out"],
            "est_cost_usd": row["est_cost_usd"],
            "comment_url": comment_url,
            "specialists": row["results"],
        })
    return reviews
```

Note: psycopg3 auto-parses `jsonb` columns back into Python lists/dicts on
SELECT (no `json.loads` needed) — Step 8 confirms this. If `row["results"]`
turns out to be a raw string instead, wrap it in `json.loads(row["results"])`
right there and re-run the test.

- [ ] **Step 7: Update the test-DB truncate to include `reviews`**

In `tests/conftest.py:66`, change:

```python
        conn.execute("TRUNCATE tickets, runtime_config RESTART IDENTITY")
```

to:

```python
        conn.execute("TRUNCATE tickets, runtime_config, reviews RESTART IDENTITY")
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `pytest tests/test_reviews_store.py -v`
Expected: PASS (all 6 tests). If `test_record_review_round_trips_through_dashboard_reviews` fails specifically on the `findings` assertion with a string-vs-list mismatch, apply the `json.loads` fallback noted in Step 6.

- [ ] **Step 9: Run the full existing store test suite to check for regressions**

Run: `pytest tests/test_queue_store.py tests/test_store_init.py -v`
Expected: PASS (unchanged — confirms the schema addition didn't break ticket tests).

- [ ] **Step 10: Commit**

```bash
git add app/queue/store.py tests/conftest.py tests/test_reviews_store.py
git commit -m "feat: persist completed reviews for the dashboard"
```

---

### Task 2: Wire `orchestrator.attempt_review()` to record every completed review

**Files:**
- Modify: `app/orchestrator.py:17-31` (imports), `app/orchestrator.py:122-125` (after `upsert_comment`)
- Test: `tests/test_orchestrator.py` (extend)

**Interfaces:**
- Consumes: `store.record_review(repo_full_name, pr_number, review, comment_id, now)` (Task 1).
- Produces: no new public interface — `attempt_review()`'s existing return type (`ReviewCompleted | ReviewRateLimited`) is unchanged; this task only adds a side effect.

- [ ] **Step 1: Write the failing tests**

In `tests/test_orchestrator.py`, add (this file already has `_ok_result` and the `fake_upsert` pattern used by the existing tests — reuse that pattern):

```python
async def test_run_review_records_the_completed_review(monkeypatch):
    import app.orchestrator as orchestrator

    monkeypatch.setattr(orchestrator.github_app, "fetch_pr_diff", lambda repo, pr: "raw diff text")
    monkeypatch.setattr(
        orchestrator.github_app, "upsert_comment",
        lambda repo, pr, body, comment_id=None: SimpleNamespace(id=111),
    )

    async def fake_security(annotated_diff):
        return _ok_result("Security")

    async def fake_performance(annotated_diff):
        return _ok_result("Performance")

    async def fake_quality(annotated_diff):
        return _ok_result("Code Quality")

    monkeypatch.setattr(orchestrator, "run_security_specialist", fake_security)
    monkeypatch.setattr(orchestrator, "run_performance_specialist", fake_performance)
    monkeypatch.setattr(orchestrator, "run_quality_specialist", fake_quality)
    monkeypatch.setattr(settings, "llm_provider", "groq")

    recorded = {}

    def fake_record_review(repo_full_name, pr_number, review, comment_id, now):
        recorded["repo_full_name"] = repo_full_name
        recorded["pr_number"] = pr_number
        recorded["review"] = review
        recorded["comment_id"] = comment_id
        recorded["now"] = now

    monkeypatch.setattr(orchestrator.store, "record_review", fake_record_review)

    result = await orchestrator.run_review("owner/repo", 99)

    assert recorded["repo_full_name"] == "owner/repo"
    assert recorded["pr_number"] == 99
    assert recorded["review"] is result
    assert recorded["comment_id"] == 111
    assert recorded["now"]  # a non-empty ISO timestamp string


async def test_run_review_survives_record_review_raising(monkeypatch):
    """A dashboard-persistence failure must never fail an otherwise-successful
    review — the PR comment is already posted by this point."""
    import app.orchestrator as orchestrator

    monkeypatch.setattr(orchestrator.github_app, "fetch_pr_diff", lambda repo, pr: "raw diff text")
    monkeypatch.setattr(
        orchestrator.github_app, "upsert_comment",
        lambda repo, pr, body, comment_id=None: SimpleNamespace(id=111),
    )

    async def fake_security(annotated_diff):
        return _ok_result("Security")

    async def fake_performance(annotated_diff):
        return _ok_result("Performance")

    async def fake_quality(annotated_diff):
        return _ok_result("Code Quality")

    monkeypatch.setattr(orchestrator, "run_security_specialist", fake_security)
    monkeypatch.setattr(orchestrator, "run_performance_specialist", fake_performance)
    monkeypatch.setattr(orchestrator, "run_quality_specialist", fake_quality)
    monkeypatch.setattr(settings, "llm_provider", "groq")

    def boom(*args, **kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(orchestrator.store, "record_review", boom)

    result = await orchestrator.run_review("owner/repo", 99)  # must not raise
    assert result.pr_number == 99
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_orchestrator.py -k record_review -v`
Expected: FAIL — `AttributeError: module 'app.orchestrator' has no attribute 'store'`.

- [ ] **Step 3: Add the imports**

In `app/orchestrator.py`, change the import block at lines 17-19:

```python
import asyncio
import time
from dataclasses import dataclass
```

to:

```python
import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
```

and add, alongside the other `app.*` imports (after `from app.providers.pricing import estimate_cost_usd`):

```python
from app.queue import store
```

Then, right after the import block (before `_SPECIALIST_NAMES = (...)`), add:

```python
logger = logging.getLogger(__name__)
```

- [ ] **Step 4: Call `record_review` after the comment is posted**

In `app/orchestrator.py`, change lines 122-125 from:

```python
    posted = await asyncio.to_thread(
        github_app.upsert_comment, repo_full_name, pr_number, body, comment_id
    )
    return ReviewCompleted(review=review_result, comment_id=posted.id)
```

to:

```python
    posted = await asyncio.to_thread(
        github_app.upsert_comment, repo_full_name, pr_number, body, comment_id
    )
    try:
        await asyncio.to_thread(
            store.record_review,
            repo_full_name,
            pr_number,
            review_result,
            posted.id,
            datetime.now(timezone.utc).isoformat(),
        )
    # a dashboard-persistence failure must never fail an already-posted review
    except Exception:  # noqa: BLE001
        logger.exception(
            "failed to record review for the dashboard (repo=%s pr=%s)",
            repo_full_name, pr_number,
        )
    return ReviewCompleted(review=review_result, comment_id=posted.id)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_orchestrator.py -v`
Expected: PASS (all tests in the file, including the two new ones and the pre-existing ones).

- [ ] **Step 6: Commit**

```bash
git add app/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: record every completed review for the dashboard"
```

---

### Task 3: `dispatcher.backoff_status()`

**Files:**
- Modify: `app/queue/dispatcher.py` (add function near `reset_blocked_until`)
- Test: `tests/test_dispatcher_backoff.py` (extend)

**Interfaces:**
- Consumes: the existing module-level `_blocked_until: dict[str, datetime]`.
- Produces: `dispatcher.backoff_status() -> dict[str, str]` — a snapshot mapping provider name to an ISO-8601 timestamp string, containing only providers currently in `_blocked_until` (callers merge in defaults for providers not present).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_dispatcher_backoff.py`:

```python
from datetime import datetime, timezone


def test_backoff_status_empty_when_nothing_blocked():
    dispatcher.reset_blocked_until()
    assert dispatcher.backoff_status() == {}


def test_backoff_status_reports_blocked_providers():
    dispatcher.reset_blocked_until()
    until = datetime(2026, 8, 11, 14, 32, tzinfo=timezone.utc)
    dispatcher._blocked_until["groq"] = until
    assert dispatcher.backoff_status() == {"groq": "2026-08-11T14:32:00+00:00"}
    dispatcher.reset_blocked_until()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_dispatcher_backoff.py -k backoff_status -v`
Expected: FAIL — `AttributeError: module 'app.queue.dispatcher' has no attribute 'backoff_status'`.

- [ ] **Step 3: Implement `backoff_status`**

In `app/queue/dispatcher.py`, right after `reset_blocked_until`:

```python
def backoff_status() -> dict[str, str]:
    """Snapshot of the in-memory per-provider rate-limit gate, for the
    dashboard. Only providers currently blocked appear; the caller fills in
    a default for every other known provider."""
    return {provider: until.isoformat() for provider, until in _blocked_until.items()}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_dispatcher_backoff.py -v`
Expected: PASS (all tests in the file).

- [ ] **Step 5: Commit**

```bash
git add app/queue/dispatcher.py tests/test_dispatcher_backoff.py
git commit -m "feat: expose dispatcher backoff state for the dashboard"
```

---

### Task 4: `GET /api/dashboard` JSON endpoint

**Files:**
- Create: `app/dashboard.py`
- Modify: `app/main.py:38` (router registration)
- Test: `tests/test_dashboard_api.py` (new)

**Interfaces:**
- Consumes: `store.dashboard_stats()`, `store.dashboard_queue_counts()`, `store.dashboard_reviews(limit=...)` (Task 1); `dispatcher.backoff_status()` (Task 3).
- Produces:
  - `dashboard.build_dashboard_payload() -> dict` (sync function; the JSON shape below)
  - `dashboard.router` (a `fastapi.APIRouter` with `GET /api/dashboard`), included into `app.main.app`

Payload shape:

```json
{
  "stats": {"total_reviews": 0, "total_cost_usd": 0.0, "avg_elapsed_ms": 0},
  "queue": {
    "by_status": {"pending": 0, "running": 0, "deferred": 0, "retrying": 0, "done": 0, "failed": 0},
    "backoff": {"gemini": null, "groq": null, "github_models": null}
  },
  "reviews": []
}
```

On a store failure, the affected section becomes `{"error": "data unavailable"}` in place of `stats`, `queue.by_status`, or `reviews` respectively — the other sections still populate normally.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_dashboard_api.py`:

```python
"""Tests for GET /api/dashboard — the dashboard's JSON payload."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app import dashboard
from app.main import app
from app.queue import dispatcher, store
from app.specialists.schemas import ReviewResult, SpecialistResult


@pytest.fixture(autouse=True)
def _isolate(db):
    dispatcher.reset_blocked_until()
    yield
    dispatcher.reset_blocked_until()


async def _client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_empty_state_shape():
    client = await _client()
    resp = await client.get("/api/dashboard")
    assert resp.status_code == 200
    body = resp.json()
    assert body["stats"] == {"total_reviews": 0, "total_cost_usd": 0.0, "avg_elapsed_ms": 0}
    assert body["queue"]["by_status"] == {
        "pending": 0, "running": 0, "deferred": 0, "retrying": 0, "done": 0, "failed": 0,
    }
    assert body["queue"]["backoff"] == {"gemini": None, "groq": None, "github_models": None}
    assert body["reviews"] == []


async def test_includes_a_recorded_review_and_active_backoff():
    review = ReviewResult(
        pr_number=42,
        provider="groq",
        model="llama-3.3-70b-versatile",
        results=[
            SpecialistResult(name="Security", status="ok", findings=[{"severity": "high"}],
                              elapsed_ms=10, tokens_in=5, tokens_out=2),
        ],
        total_elapsed_ms=10,
        total_tokens_in=5,
        total_tokens_out=2,
        est_cost_usd=0.001,
    )
    store.record_review("owner/repo", 42, review, comment_id=999, now="2026-08-11T12:00:00+00:00")

    from datetime import datetime, timezone
    dispatcher._blocked_until["groq"] = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)

    client = await _client()
    resp = await client.get("/api/dashboard")
    body = resp.json()

    assert body["stats"]["total_reviews"] == 1
    row = body["reviews"][0]
    assert row["repo"] == "owner/repo"
    assert row["pr_number"] == 42
    assert row["comment_url"] == "https://github.com/owner/repo/pull/42#issuecomment-999"
    assert row["specialists"][0]["name"] == "Security"
    assert body["queue"]["backoff"]["groq"] == "2026-08-11T14:00:00+00:00"
    assert body["queue"]["backoff"]["gemini"] is None


async def test_degrades_a_single_section_on_store_error(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(dashboard.store, "dashboard_stats", boom)

    client = await _client()
    resp = await client.get("/api/dashboard")
    assert resp.status_code == 200
    body = resp.json()
    assert body["stats"] == {"error": "data unavailable"}
    assert body["reviews"] == []  # unaffected sections still populate


async def test_degrades_queue_by_status_independently_of_backoff(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(dashboard.store, "dashboard_queue_counts", boom)
    from datetime import datetime, timezone
    dispatcher._blocked_until["groq"] = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)

    client = await _client()
    resp = await client.get("/api/dashboard")
    body = resp.json()
    assert body["queue"]["by_status"] == {"error": "data unavailable"}
    assert body["queue"]["backoff"]["groq"] == "2026-08-11T14:00:00+00:00"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_dashboard_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.dashboard'`.

- [ ] **Step 3: Create `app/dashboard.py`**

```python
"""Ops/demo dashboard: GET /api/dashboard (JSON) backing GET /dashboard's
static page. Knows nothing about LLM providers or GitHub — only reads
app.queue.store and app.queue.dispatcher, same separation formatting.py
keeps from the LLM layer.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.queue import dispatcher, store

logger = logging.getLogger(__name__)

router = APIRouter()

_REVIEWS_LIMIT = 50
# Kept local rather than imported from app/providers/factory.py: factory.py
# has no shared constant for this list, and the dashboard has no other
# reason to depend on it.
_KNOWN_PROVIDERS = ("gemini", "groq", "github_models")


def build_dashboard_payload() -> dict:
    """Assemble the /api/dashboard JSON body. Each section degrades to
    {"error": "data unavailable"} independently on failure."""
    payload: dict = {}

    try:
        payload["stats"] = store.dashboard_stats()
    except Exception:  # noqa: BLE001
        logger.exception("dashboard: failed to load stats")
        payload["stats"] = {"error": "data unavailable"}

    backoff_raw = dispatcher.backoff_status()
    backoff = {provider: backoff_raw.get(provider) for provider in _KNOWN_PROVIDERS}
    try:
        by_status = store.dashboard_queue_counts()
    except Exception:  # noqa: BLE001
        logger.exception("dashboard: failed to load queue counts")
        by_status = {"error": "data unavailable"}
    payload["queue"] = {"by_status": by_status, "backoff": backoff}

    try:
        payload["reviews"] = store.dashboard_reviews(limit=_REVIEWS_LIMIT)
    except Exception:  # noqa: BLE001
        logger.exception("dashboard: failed to load reviews")
        payload["reviews"] = {"error": "data unavailable"}

    return payload


@router.get("/api/dashboard")
async def api_dashboard() -> JSONResponse:
    payload = await asyncio.to_thread(build_dashboard_payload)
    return JSONResponse(payload)
```

- [ ] **Step 4: Register the router**

In `app/main.py`, change line 38:

```python
app.include_router(webhook_router)
```

to:

```python
app.include_router(webhook_router)
app.include_router(dashboard_router)
```

and add, alongside the other `app.*` imports at the top of the file:

```python
from app.dashboard import router as dashboard_router
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_dashboard_api.py -v`
Expected: PASS (all 4 tests).

- [ ] **Step 6: Run the full test suite to check for regressions**

Run: `pytest -v`
Expected: PASS (everything, including Tasks 1-3's tests and every pre-existing test).

- [ ] **Step 7: Commit**

```bash
git add app/dashboard.py app/main.py tests/test_dashboard_api.py
git commit -m "feat: add GET /api/dashboard JSON endpoint"
```

---

### Task 5: Dashboard page shell — theme + language switching, RTL

**Files:**
- Create: `app/static/dashboard.html`
- Modify: `app/dashboard.py` (add the `/dashboard` route)
- Test: `tests/test_dashboard_page.py` (new)

**Interfaces:**
- Consumes: none (static asset + a route that just reads and returns it).
- Produces: `GET /dashboard` (HTML). The page exposes, for Task 6 to build on:
  - `#stats` and `#reviews` — empty `<section>` containers Task 6 fills with rendered content.
  - `#errorBanner` — a `hidden` `<section>` Task 6 shows/hides and sets text on.
  - `t(key)` — the JS translation lookup function, and the `STRINGS` object it reads (Task 6 extends `STRINGS` with more keys and calls `t()`).
  - `applyLanguage(lang)` — JS function Task 6 appends a `refreshDashboard()` call to.
  - The final bootstrap line `applyLanguage(currentLang);` — Task 6 replaces this line to also start polling.

- [ ] **Step 1: Write the failing test**

Create `tests/test_dashboard_page.py`:

```python
"""Tests for GET /dashboard — the static HTML page shell."""
from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from app.main import app


async def _client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_dashboard_page_serves_html_with_theme_and_language_controls():
    client = await _client()
    resp = await client.get("/dashboard")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert 'id="themeToggleBtn"' in body
    assert 'id="langToggleBtn"' in body
    assert 'name="theme"' in body
    assert 'name="lang"' in body
    assert "עברית" in body
    assert 'dir="ltr"' in body
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_dashboard_page.py -v`
Expected: FAIL — 404 (no `/dashboard` route yet) or `FileNotFoundError`.

- [ ] **Step 3: Create the static page shell**

Create `app/static/dashboard.html`:

```html
<!doctype html>
<html lang="en" dir="ltr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PR Review Engine — Dashboard</title>
<style>
  :root {
    --bg: #f5f6f8;
    --surface: #ffffff;
    --surface-2: #eef0f3;
    --text: #1f2933;
    --text-muted: #5c6773;
    --border: #dde2e7;
    --accent: #3a6ea5;
    --ok: #2f7d4f;
    --fail: #b3454b;
    --sev-critical: #b3454b;
    --sev-high: #c07a2e;
    --sev-medium: #8a8330;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #12161b;
      --surface: #1a1f26;
      --surface-2: #22282f;
      --text: #e6e9ec;
      --text-muted: #9aa5b1;
      --border: #2b323a;
      --accent: #7ba7d9;
      --ok: #5fbf87;
      --fail: #e08086;
      --sev-critical: #e08086;
      --sev-high: #e3ac6a;
      --sev-medium: #d9d07a;
    }
  }
  :root[data-theme="dark"] {
    --bg: #12161b;
    --surface: #1a1f26;
    --surface-2: #22282f;
    --text: #e6e9ec;
    --text-muted: #9aa5b1;
    --border: #2b323a;
    --accent: #7ba7d9;
    --ok: #5fbf87;
    --fail: #e08086;
    --sev-critical: #e08086;
    --sev-high: #e3ac6a;
    --sev-medium: #d9d07a;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  }
  header.topbar {
    display: flex;
    justify-content: flex-end;
    gap: 0.5rem;
    padding: 0.75rem 1rem;
    border-bottom: 1px solid var(--border);
  }
  button.control {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    background: var(--surface);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 0.4rem 0.9rem;
    font-size: 0.9rem;
    cursor: pointer;
  }
  button.control:hover { border-color: var(--accent); }
  .popup-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(15, 20, 25, 0.35);
    display: none;
    align-items: center;
    justify-content: center;
    z-index: 10;
  }
  .popup-backdrop.open { display: flex; }
  .popup {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 0.75rem;
    padding: 1rem 1.25rem;
    min-width: 220px;
    max-width: 90vw;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.2);
  }
  .popup h2 { margin: 0 0 0.75rem; font-size: 1rem; }
  .radio-group { display: flex; flex-direction: column; gap: 0.5rem; }
  .radio-group label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.35rem 0.25rem;
    cursor: pointer;
    font-size: 0.95rem;
  }
  main { max-width: 1100px; margin: 0 auto; padding: 1rem 1rem 2.5rem; }
  h1 { font-size: 1.3rem; margin: 0.5rem 0 1rem; }
</style>
</head>
<body>
  <header class="topbar">
    <button id="themeToggleBtn" class="control" type="button" aria-haspopup="dialog"></button>
    <button id="langToggleBtn" class="control" type="button" aria-haspopup="dialog"></button>
  </header>

  <div id="themePopupBackdrop" class="popup-backdrop">
    <div class="popup" role="dialog" aria-modal="true" aria-labelledby="themePopupTitle">
      <h2 id="themePopupTitle"></h2>
      <div class="radio-group">
        <label><input type="radio" name="theme" value="light"> <span data-i18n="theme_light"></span></label>
        <label><input type="radio" name="theme" value="dark"> <span data-i18n="theme_dark"></span></label>
        <label><input type="radio" name="theme" value="system"> <span data-i18n="theme_system"></span></label>
      </div>
    </div>
  </div>

  <div id="langPopupBackdrop" class="popup-backdrop">
    <div class="popup" role="dialog" aria-modal="true" aria-labelledby="langPopupTitle">
      <h2 id="langPopupTitle"></h2>
      <div class="radio-group">
        <label><input type="radio" name="lang" value="en"> 🇺🇸 English</label>
        <label><input type="radio" name="lang" value="he"> 🇮🇱 עברית</label>
      </div>
    </div>
  </div>

  <main>
    <h1 data-i18n="dashboard_title"></h1>
    <section id="errorBanner" hidden></section>
    <section id="stats" class="stats-grid"></section>
    <section id="reviews"></section>
  </main>

  <script>
    const STRINGS = {
      en: {
        dashboard_title: "PR Review Engine — Dashboard",
        theme_light: "Light",
        theme_dark: "Dark",
        theme_system: "System",
        theme_popup_title: "Theme",
        lang_popup_title: "Language",
        loading: "Loading…",
      },
      he: {
        dashboard_title: "מנוע בדיקת קוד — לוח בקרה",
        theme_light: "בהיר",
        theme_dark: "כהה",
        theme_system: "מערכת",
        theme_popup_title: "עיצוב",
        lang_popup_title: "שפה",
        loading: "טוען…",
      },
    };

    const THEME_ICON = { light: "☀️", dark: "🌙", system: "🖥️" };
    const LANG_LABEL = { en: "🇺🇸 English", he: "🇮🇱 עברית" };

    let currentLang = localStorage.getItem("dashboard_lang") || "en";
    let currentTheme = localStorage.getItem("dashboard_theme") || "system";

    function t(key) {
      return STRINGS[currentLang][key] || STRINGS.en[key] || key;
    }

    function applyTheme(theme) {
      currentTheme = theme;
      localStorage.setItem("dashboard_theme", theme);
      document.documentElement.setAttribute("data-theme", theme === "system" ? "" : theme);
      document.getElementById("themeToggleBtn").textContent =
        `${THEME_ICON[theme]} ${t("theme_" + theme)}`;
      document.querySelector(`input[name="theme"][value="${theme}"]`).checked = true;
    }

    function applyLanguage(lang) {
      currentLang = lang;
      localStorage.setItem("dashboard_lang", lang);
      document.documentElement.setAttribute("lang", lang);
      document.documentElement.setAttribute("dir", lang === "he" ? "rtl" : "ltr");
      document.getElementById("langToggleBtn").textContent = LANG_LABEL[lang];
      document.querySelectorAll("[data-i18n]").forEach((el) => {
        el.textContent = t(el.getAttribute("data-i18n"));
      });
      document.getElementById("themePopupTitle").textContent = t("theme_popup_title");
      document.getElementById("langPopupTitle").textContent = t("lang_popup_title");
      document.querySelector(`input[name="lang"][value="${lang}"]`).checked = true;
      applyTheme(currentTheme); // refresh theme button label in the new language
    }

    function openPopup(id) {
      closeAllPopups();
      document.getElementById(id).classList.add("open");
    }

    function closeAllPopups() {
      document.querySelectorAll(".popup-backdrop").forEach((el) => el.classList.remove("open"));
    }

    document.getElementById("themeToggleBtn").addEventListener("click", () => openPopup("themePopupBackdrop"));
    document.getElementById("langToggleBtn").addEventListener("click", () => openPopup("langPopupBackdrop"));

    document.querySelectorAll(".popup-backdrop").forEach((backdrop) => {
      backdrop.addEventListener("click", (event) => {
        if (event.target === backdrop) closeAllPopups();
      });
    });

    document.querySelectorAll('input[name="theme"]').forEach((radio) => {
      radio.addEventListener("change", (event) => {
        applyTheme(event.target.value);
        closeAllPopups();
      });
    });

    document.querySelectorAll('input[name="lang"]').forEach((radio) => {
      radio.addEventListener("change", (event) => {
        applyLanguage(event.target.value);
        closeAllPopups();
      });
    });

    applyLanguage(currentLang);
  </script>
</body>
</html>
```

- [ ] **Step 4: Add the `/dashboard` route**

In `app/dashboard.py`, add near the top (after the existing imports):

```python
from pathlib import Path

from fastapi.responses import HTMLResponse
```

(merge `HTMLResponse` into the existing `from fastapi.responses import JSONResponse` line so it reads `from fastapi.responses import HTMLResponse, JSONResponse`, and add the `Path` import on its own line above the `fastapi` imports.)

Then add, alongside `_KNOWN_PROVIDERS`:

```python
_STATIC_DIR = Path(__file__).parent / "static"
```

And add the route at the bottom of the file:

```python
@router.get("/dashboard")
async def dashboard_page() -> HTMLResponse:
    html = (_STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")
    return HTMLResponse(html)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `pytest tests/test_dashboard_page.py -v`
Expected: PASS.

- [ ] **Step 6: Manually verify theming, language, and RTL in a browser**

Run: `uv run uvicorn app.main:app --reload` (requires `DATABASE_URL` etc. set per `SETUP.md`, or temporarily stub `store.init_pool`/skip the lifespan if just eyeballing the static page), then open `http://localhost:8000/dashboard` and confirm:
- The theme button shows the current mode and opens a popup with Light/Dark/System radios; selecting one changes the page colors and persists across a reload.
- The language button shows the current language (with its flag) and opens a popup with English 🇺🇸 / עברית 🇮🇱 radios; selecting עברית flips the page to RTL (`dir="rtl"` on `<html>`), translates the title and popup labels, and persists across a reload.
- Resizing the window down to a phone width keeps the header controls and popups usable (popups stay centered and within the viewport).

- [ ] **Step 7: Commit**

```bash
git add app/static/dashboard.html app/dashboard.py tests/test_dashboard_page.py
git commit -m "feat: add dashboard page shell with theme and language switching"
```

---

### Task 6: Stat tiles, review list, and live polling

**Files:**
- Modify: `app/static/dashboard.html` (CSS + JS additions)
- Test: `tests/test_dashboard_page.py` (extend)

**Interfaces:**
- Consumes: `GET /api/dashboard`'s JSON shape (Task 4); the `#stats`/`#reviews`/`#errorBanner` containers, `STRINGS`/`t()`, and `applyLanguage()` from Task 5.
- Produces: no new backend interface — this task only extends the static page's own CSS/JS.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_dashboard_page.py`:

```python
async def test_dashboard_page_includes_polling_and_rendering_hooks():
    client = await _client()
    resp = await client.get("/dashboard")
    body = resp.text
    assert "/api/dashboard" in body
    assert "setInterval" in body
    assert "renderReviews" in body
    assert "renderStats" in body
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_dashboard_page.py -k polling -v`
Expected: FAIL — none of `/api/dashboard`, `setInterval`, `renderReviews`, `renderStats` appear in the page yet.

- [ ] **Step 3: Add the CSS for stat tiles, the review list, and mobile breakpoints**

In `app/static/dashboard.html`, insert the following immediately before the closing `</style>` tag:

```css
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 0.75rem;
    margin-bottom: 1.5rem;
  }
  @media (max-width: 900px) { .stats-grid { grid-template-columns: repeat(2, 1fr); } }
  @media (max-width: 500px) { .stats-grid { grid-template-columns: 1fr; } }
  .stat-tile {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 0.6rem;
    padding: 0.85rem 1rem;
  }
  .stat-tile .stat-label { color: var(--text-muted); font-size: 0.8rem; }
  .stat-tile .stat-value { font-size: 1.4rem; font-weight: 600; margin-top: 0.15rem; }
  #errorBanner {
    background: var(--surface-2);
    border: 1px solid var(--fail);
    color: var(--fail);
    border-radius: 0.5rem;
    padding: 0.6rem 0.9rem;
    margin-bottom: 1rem;
  }
  .review-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 0.6rem;
    margin-bottom: 0.6rem;
    overflow: hidden;
  }
  .review-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.75rem;
    padding: 0.7rem 1rem;
    cursor: pointer;
  }
  .review-row .field { font-size: 0.85rem; }
  .review-row .field .field-value { font-weight: 500; }
  @media (max-width: 640px) {
    .review-row { flex-direction: column; align-items: stretch; }
    .review-row .field { display: flex; justify-content: space-between; width: 100%; }
    .review-row .field .field-label { color: var(--text-muted); }
  }
  .review-row .chevron {
    margin-inline-start: auto;
    display: inline-block;
    transition: transform 0.15s ease;
  }
  .review-row[aria-expanded="true"] .chevron { transform: rotate(180deg); }
  .review-findings { display: none; border-top: 1px solid var(--border); padding: 0.75rem 1rem; }
  .review-card.expanded .review-findings { display: block; }
  .finding-group h3 { font-size: 0.85rem; margin: 0.5rem 0 0.25rem; }
  .finding { font-size: 0.85rem; padding: 0.35rem 0; border-bottom: 1px dashed var(--border); }
  .finding:last-child { border-bottom: none; }
  .sev-critical { color: var(--sev-critical); font-weight: 600; }
  .sev-high { color: var(--sev-high); font-weight: 600; }
  .sev-medium { color: var(--sev-medium); font-weight: 600; }
  .status-ok { color: var(--ok); }
  .status-failed { color: var(--fail); }
  a.comment-link { color: var(--accent); text-decoration: none; }
```

- [ ] **Step 4: Expand the `STRINGS` object**

Replace the entire `STRINGS` object (from `const STRINGS = {` through its closing `};`) with:

```javascript
    const STRINGS = {
      en: {
        dashboard_title: "PR Review Engine — Dashboard",
        theme_light: "Light",
        theme_dark: "Dark",
        theme_system: "System",
        theme_popup_title: "Theme",
        lang_popup_title: "Language",
        loading: "Loading…",
        stat_total_reviews: "Total reviews",
        stat_total_cost: "Total est. cost",
        stat_avg_time: "Avg. review time",
        stat_queue: "Queue",
        stat_provider_backoff: "Provider backoff",
        error_banner: "Live data is unavailable right now.",
        empty_reviews: "No reviews yet.",
        q_pending: "Pending", q_running: "Running", q_deferred: "Deferred",
        q_retrying: "Retrying", q_done: "Done", q_failed: "Failed",
        col_pr: "PR", col_repo: "Repository", col_provider: "Provider / Model",
        col_time: "Elapsed", col_tokens: "Tokens", col_cost: "Est. cost",
        col_when: "When", col_comment: "View comment",
        sp_ok: "OK", sp_failed: "Failed",
        sev_critical: "Critical", sev_high: "High", sev_medium: "Medium",
        backoff_none: "none",
      },
      he: {
        dashboard_title: "מנוע בדיקת קוד — לוח בקרה",
        theme_light: "בהיר",
        theme_dark: "כהה",
        theme_system: "מערכת",
        theme_popup_title: "עיצוב",
        lang_popup_title: "שפה",
        loading: "טוען…",
        stat_total_reviews: "סה\"כ בדיקות",
        stat_total_cost: "עלות משוערת כוללת",
        stat_avg_time: "זמן בדיקה ממוצע",
        stat_queue: "תור",
        stat_provider_backoff: "חסימת ספק",
        error_banner: "המידע החי אינו זמין כרגע.",
        empty_reviews: "אין עדיין בדיקות.",
        q_pending: "מחכה", q_running: "רץ", q_deferred: "נדחה",
        q_retrying: "מנסה שוב", q_done: "הושלם", q_failed: "נכשל",
        col_pr: "מספר PR", col_repo: "מאגר", col_provider: "ספק / מודל",
        col_time: "זמן", col_tokens: "טוקנים", col_cost: "עלות משוערת",
        col_when: "מתי", col_comment: "צפייה בתגובה",
        sp_ok: "תקין", sp_failed: "נכשל",
        sev_critical: "קריטי", sev_high: "גבוה", sev_medium: "בינוני",
        backoff_none: "אין",
      },
    };
```

- [ ] **Step 5: Add the rendering + polling functions and wire them into `applyLanguage`**

Replace this block (from Task 5):

```javascript
    function applyLanguage(lang) {
      currentLang = lang;
      localStorage.setItem("dashboard_lang", lang);
      document.documentElement.setAttribute("lang", lang);
      document.documentElement.setAttribute("dir", lang === "he" ? "rtl" : "ltr");
      document.getElementById("langToggleBtn").textContent = LANG_LABEL[lang];
      document.querySelectorAll("[data-i18n]").forEach((el) => {
        el.textContent = t(el.getAttribute("data-i18n"));
      });
      document.getElementById("themePopupTitle").textContent = t("theme_popup_title");
      document.getElementById("langPopupTitle").textContent = t("lang_popup_title");
      document.querySelector(`input[name="lang"][value="${lang}"]`).checked = true;
      applyTheme(currentTheme); // refresh theme button label in the new language
    }
```

with:

```javascript
    function applyLanguage(lang) {
      currentLang = lang;
      localStorage.setItem("dashboard_lang", lang);
      document.documentElement.setAttribute("lang", lang);
      document.documentElement.setAttribute("dir", lang === "he" ? "rtl" : "ltr");
      document.getElementById("langToggleBtn").textContent = LANG_LABEL[lang];
      document.querySelectorAll("[data-i18n]").forEach((el) => {
        el.textContent = t(el.getAttribute("data-i18n"));
      });
      document.getElementById("themePopupTitle").textContent = t("theme_popup_title");
      document.getElementById("langPopupTitle").textContent = t("lang_popup_title");
      document.querySelector(`input[name="lang"][value="${lang}"]`).checked = true;
      applyTheme(currentTheme); // refresh theme button label in the new language
      refreshDashboard();
    }

    const POLL_INTERVAL_MS = 4000;
    const expandedPrs = new Set();

    function renderStats(stats, queue) {
      const el = document.getElementById("stats");
      if (stats.error || queue.error) {
        el.innerHTML = "";
        return;
      }
      const tiles = [
        [t("stat_total_reviews"), String(stats.total_reviews)],
        [t("stat_total_cost"), `$${stats.total_cost_usd.toFixed(4)}`],
        [t("stat_avg_time"), `${(stats.avg_elapsed_ms / 1000).toFixed(1)}s`],
        [t("stat_queue"), Object.entries(queue.by_status || {})
          .map(([status, count]) => `${t("q_" + status)}: ${count}`).join(" · ")],
        [t("stat_provider_backoff"), Object.entries(queue.backoff || {})
          .filter(([, until]) => until)
          .map(([provider, until]) => `${provider} → ${new Date(until).toLocaleTimeString()}`)
          .join(", ") || t("backoff_none")],
      ];
      el.innerHTML = tiles.map(([label, value]) => `
        <div class="stat-tile">
          <div class="stat-label">${label}</div>
          <div class="stat-value">${value}</div>
        </div>
      `).join("");
    }

    function findingRows(specialist) {
      if (specialist.status === "failed") {
        return `<div class="finding">${specialist.error || ""}</div>`;
      }
      if (!specialist.findings || !specialist.findings.length) {
        return `<div class="finding">${t("empty_reviews")}</div>`;
      }
      return specialist.findings.map((finding) => {
        const parts = [];
        if (finding.severity) {
          parts.push(`<span class="sev-${finding.severity}">${t("sev_" + finding.severity)}</span>`);
        }
        if (finding.estimated_impact) parts.push(finding.estimated_impact);
        if (finding.category) parts.push(finding.category);
        if (finding.type) parts.push(finding.type);
        if (finding.file) parts.push(`${finding.file}:${finding.line ?? "?"}`);
        const text = finding.description || finding.issue || finding.suggestion || "";
        const fix = finding.fix || finding.refactoring_suggestion || "";
        return `<div class="finding">${parts.join(" · ")} — ${text}${fix ? ` → ${fix}` : ""}</div>`;
      }).join("");
    }

    function renderReviews(reviews) {
      const el = document.getElementById("reviews");
      if (reviews.error) {
        el.innerHTML = "";
        return;
      }
      if (!reviews.length) {
        el.innerHTML = `<p>${t("empty_reviews")}</p>`;
        return;
      }
      el.innerHTML = reviews.map((review) => {
        const key = `${review.repo}#${review.pr_number}#${review.created_at}`;
        const isExpanded = expandedPrs.has(key);
        const statuses = review.specialists.map((s) =>
          `<span class="status-${s.status}">${s.name}: ${s.status === "ok" ? t("sp_ok") : t("sp_failed")}</span>`
        ).join(" · ");
        const findings = review.specialists.map((s) => `
          <div class="finding-group">
            <h3>${s.name}</h3>
            ${findingRows(s)}
          </div>
        `).join("");
        return `
          <div class="review-card${isExpanded ? " expanded" : ""}" data-key="${key}">
            <div class="review-row" role="button" tabindex="0" aria-expanded="${isExpanded}">
              <span class="field"><span class="field-label">${t("col_pr")}: </span><span class="field-value">#${review.pr_number}</span></span>
              <span class="field"><span class="field-label">${t("col_repo")}: </span><span class="field-value">${review.repo}</span></span>
              <span class="field"><span class="field-label">${t("col_provider")}: </span><span class="field-value">${review.provider}/${review.model}</span></span>
              <span class="field"><span class="field-label">${t("col_time")}: </span><span class="field-value">${(review.elapsed_ms / 1000).toFixed(1)}s</span></span>
              <span class="field"><span class="field-label">${t("col_tokens")}: </span><span class="field-value">${review.tokens_in}/${review.tokens_out}</span></span>
              <span class="field"><span class="field-label">${t("col_cost")}: </span><span class="field-value">$${review.est_cost_usd.toFixed(4)}</span></span>
              <span class="field"><span class="field-label">${t("col_when")}: </span><span class="field-value">${new Date(review.created_at).toLocaleString()}</span></span>
              <span class="field">${statuses}</span>
              ${review.comment_url ? `<a class="comment-link field" href="${review.comment_url}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${t("col_comment")}</a>` : ""}
              <span class="chevron">▾</span>
            </div>
            <div class="review-findings">${findings}</div>
          </div>
        `;
      }).join("");

      el.querySelectorAll(".review-row").forEach((row) => {
        row.addEventListener("click", () => {
          const card = row.closest(".review-card");
          const key = card.getAttribute("data-key");
          const nowExpanded = !card.classList.contains("expanded");
          card.classList.toggle("expanded", nowExpanded);
          row.setAttribute("aria-expanded", String(nowExpanded));
          if (nowExpanded) expandedPrs.add(key); else expandedPrs.delete(key);
        });
      });
    }

    async function refreshDashboard() {
      const banner = document.getElementById("errorBanner");
      try {
        const response = await fetch("/api/dashboard");
        const data = await response.json();
        const hasError = data.stats.error || data.queue.by_status.error || data.reviews.error;
        banner.hidden = !hasError;
        if (hasError) banner.textContent = t("error_banner");
        renderStats(data.stats, data.queue);
        renderReviews(data.reviews);
      } catch (err) {
        banner.hidden = false;
        banner.textContent = t("error_banner");
      }
    }
```

- [ ] **Step 6: Start polling from the bootstrap line**

Replace this line (from Task 5):

```javascript
    applyLanguage(currentLang);
```

with:

```javascript
    applyLanguage(currentLang);
    setInterval(refreshDashboard, POLL_INTERVAL_MS);
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `pytest tests/test_dashboard_page.py -v`
Expected: PASS (all tests in the file).

- [ ] **Step 8: Run the full test suite**

Run: `pytest -v`
Expected: PASS — everything from every prior task plus every pre-existing test.

- [ ] **Step 9: Manually verify the live data in a browser**

With the app running and at least one review recorded (either a real webhook-triggered review, or by calling `store.record_review(...)` directly against your dev DB), open `http://localhost:8000/dashboard` and confirm:
- The stat tiles show a nonzero review count, a dollar cost, and a queue breakdown, and update automatically within ~4s of a new review completing.
- A review row expands on click to show its findings grouped by specialist, and collapses on a second click; the expand state survives an auto-refresh.
- Switching to עברית re-renders the stat tiles and review list in Hebrew (translated labels; the findings' own description/fix text stays in English) and the whole layout mirrors to RTL.
- At a narrow (phone-width) viewport, the stat tiles stack to one column and each review row becomes a stacked label/value list instead of a single wide row.
- Temporarily stopping the database (or pointing `DATABASE_URL` at something unreachable) makes the error banner appear instead of the page crashing.

- [ ] **Step 10: Commit**

```bash
git add app/static/dashboard.html tests/test_dashboard_page.py
git commit -m "feat: render dashboard stats and review list with live polling"
```
