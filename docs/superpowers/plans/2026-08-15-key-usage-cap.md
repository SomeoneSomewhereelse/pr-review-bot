# Proactive Per-Key Daily Usage Cap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the app from starting a review once the currently-active
`(provider, key slot)` has already burned a configured number of tokens (or
dollars) since today's UTC reset — deferring the ticket until the next reset
instead, with a PR notice that says so.

**Architecture:** Usage is derived, not tracked: a `SUM` over the existing
insert-only `reviews` table, scoped to `(provider, key_index)` and to the
current usage-day bucket. Two nullable columns carry the new facts
(`reviews.key_index` — which slot paid for that review; `tickets.defer_reason`
— why a deferred ticket is waiting). The gate itself is one fail-open block in
`dispatcher.process_next_due`, placed immediately before the existing
blocked-provider check, and it reuses the deferred-ticket machinery wholesale
— no new state machine, no new table, no background job.

**Tech Stack:** FastAPI/Python async backend, `pydantic-settings` for config,
`psycopg`/Postgres (Supabase in production, local/CI Postgres in tests via
`tests/conftest.py`'s `db` fixture), `pytest`/`pytest-asyncio` (`asyncio_mode
= "auto"`, so `async def test_*` needs no decorator), `uv run` for every
command.

**Spec:** `docs/superpowers/specs/2026-08-15-key-usage-cap-design.md` — read it
first; this plan implements it exactly, with four refinements discovered
during planning:

1. **No custom parser for `KEY_USAGE_RESET_TIME_UTC`.** The spec says the
   value is "parsed via `time.fromisoformat`". A typed `datetime.time` field
   on `Settings` already does exactly that — pydantic-settings parses both
   `"04:00"` and `"04:07:30"` into `datetime.time` natively (verified against
   this repo's pinned pydantic during planning). No validator is written.
2. **The dispatcher block is restructured, same semantics.** The spec's
   snippet assigns `bucket_start` inside the `try` and reads it outside,
   which is a possibly-unbound reference if the `try` fails early — and the
   whole point of the block is that *nothing* in it may abort a review.
   Task 7 uses a single `cap_reset_at: datetime | None` that is only set on
   the over-cap path, so the entire computation (including `usage_bucket_start`
   and the `reset_at` arithmetic) is inside the fail-open `try`. Identical
   observable behavior.
3. **`record_review`'s new `key_index` parameter is required, not defaulted.**
   A default of `0` would silently attribute a review to slot 0 whenever a
   caller forgot it — corrupting the exact per-slot accounting this feature
   exists to provide. Task 3 therefore also updates the eight existing test
   call sites; they are listed explicitly.
4. **`render.yaml` is deliberately NOT touched.** Its entries are all
   `sync: false` (operator-supplied) and `scripts/deploy.py::_wanted_env`
   never reads the file, so adding the new keys buys nothing — while an
   operator who declares them and leaves the dashboard value blank would
   feed `KEY_USAGE_TOKEN_CAP=""` to a `int | None` field and fail startup
   validation. Both caps are off-by-default and are documented in
   `README.md` and `.env.example` (Task 8) as optional additions.

## Global Constraints

- Secrets only via env vars; **no secret is ever logged**. Nothing this plan
  adds touches a credential — only an integer slot index reaches Postgres
  (root `CLAUDE.md`).
- **Never make a live LLM API call.** Every test in this plan is
  deterministic and DB-only or pure-function; no provider SDK is exercised
  (root `CLAUDE.md`, LLM API testing hygiene).
- **A usage-check failure must never block a review.** The new gate fails
  *open* — an exception anywhere in it logs and proceeds as "not capped",
  the same degrade-to-safe-default posture as the three existing per-ticket
  override refreshes (spec §4).
- **Both caps default to `None` (feature off).** A deployment that sets
  neither env var must see byte-identical behavior to today, including the
  exact wording of every existing PR comment (spec §2.1).
- **`KEY_USAGE_TOKEN_CAP` takes precedence when both caps are set** — the
  cost cap is not consulted at all in that case, not merely a tiebreak
  (spec §2.1).
- **Never leave a stale `defer_reason`.** Every code path that sets
  `status = 'deferred'` other than `defer_usage_capped` must explicitly write
  `defer_reason = NULL` (spec §4.1).
- Async throughout; every synchronous `store.*` call from async code goes
  through `asyncio.to_thread` (existing convention, `app/queue/store.py`
  module docstring).
- Line length 100 (`ruff` `E501` is enabled). Run `uv run ruff check .` and
  `uv run pytest <path>` before every commit.
- DB-touching tests need Docker (testcontainers spins a throwaway Postgres 16)
  or a local `DATABASE_URL`; they request the `db` fixture from
  `tests/conftest.py`.

---

### Task 1: Config — the three cap settings

**Files:**
- Modify: `app/config.py:66` (append after `dispatcher_notice_sweep_batch_size`)
- Test: `tests/test_config.py` (extend)

**Interfaces:**
- Produces: `settings.key_usage_token_cap: int | None`,
  `settings.key_usage_cost_cap_usd: float | None`,
  `settings.key_usage_reset_time_utc: datetime.time` (default `time(4, 0)`).
  Tasks 2 and 7 read all three.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_key_usage_caps_default_to_off(monkeypatch):
    """Both caps default to None so an existing deployment that sets neither
    env var sees no behavior change (design doc §2.1). _env_file=None plus
    delenv because these defaults must be asserted against the code, not
    against whatever this working copy's .env happens to say."""
    for name in (
        "KEY_USAGE_TOKEN_CAP",
        "KEY_USAGE_COST_CAP_USD",
        "KEY_USAGE_RESET_TIME_UTC",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    assert settings.key_usage_token_cap is None
    assert settings.key_usage_cost_cap_usd is None
    assert settings.key_usage_reset_time_utc == time(4, 0)


def test_key_usage_reset_time_parses_hh_mm(monkeypatch):
    """Arbitrary wall-clock granularity, not whole hours only -- a demo run
    sets the reset a couple of minutes out rather than waiting for the next
    hour boundary (design doc §2.1)."""
    monkeypatch.setenv("KEY_USAGE_RESET_TIME_UTC", "04:07")
    assert Settings(_env_file=None).key_usage_reset_time_utc == time(4, 7)


def test_key_usage_reset_time_parses_hh_mm_ss(monkeypatch):
    monkeypatch.setenv("KEY_USAGE_RESET_TIME_UTC", "23:59:30")
    assert Settings(_env_file=None).key_usage_reset_time_utc == time(23, 59, 30)


def test_key_usage_caps_parse_from_env(monkeypatch):
    monkeypatch.setenv("KEY_USAGE_TOKEN_CAP", "20000")
    monkeypatch.setenv("KEY_USAGE_COST_CAP_USD", "0.25")
    settings = Settings(_env_file=None)
    assert settings.key_usage_token_cap == 20000
    assert settings.key_usage_cost_cap_usd == 0.25


def test_key_usage_reset_time_rejects_garbage(monkeypatch):
    monkeypatch.setenv("KEY_USAGE_RESET_TIME_UTC", "not-a-time")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
```

And add `time` to that file's imports (top of `tests/test_config.py`, after
`from __future__ import annotations`):

```python
from datetime import time
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'key_usage_token_cap'`

- [ ] **Step 3: Add the settings**

In `app/config.py`, add `time` to the datetime import at the top of the file
(the file currently imports nothing from `datetime`, so add a new line above
the pydantic imports):

```python
from datetime import time
```

Then insert immediately after the `dispatcher_notice_sweep_batch_size` field
(`app/config.py:66`), before the `# --- Optional operator tooling` block:

```python
    # --- Proactive per-key daily usage cap. Both caps default to None
    # (feature off): a deployment that sets neither env var behaves exactly
    # as before. KEY_USAGE_TOKEN_CAP WINS OUTRIGHT when both are set -- the
    # cost cap is then not consulted at all, not used as a tiebreak. The
    # reset time is a plain "HH:MM" (or "HH:MM:SS") UTC wall-clock string;
    # a `time` field makes pydantic parse it, giving arbitrary granularity
    # rather than whole-hour-only resets, specifically so a demo can set the
    # reset a couple of minutes out instead of waiting for the next hour.
    key_usage_token_cap: int | None = None
    key_usage_cost_cap_usd: float | None = None
    key_usage_reset_time_utc: time = Field(default=time(4, 0))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS (all tests, old and new)

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check .
git add app/config.py tests/test_config.py
git commit -m "feat(config): add per-key daily usage cap settings"
```

---

### Task 2: `usage_bucket_start` — the usage-day boundary

**Files:**
- Modify: `app/queue/store.py:18` (import), `app/queue/store.py:186` (add the
  helper right after `next_cooldown_level`)
- Test: `tests/test_key_usage_cap.py` (new)

**Interfaces:**
- Consumes: `settings.key_usage_reset_time_utc` (Task 1) — at the call site,
  not inside the helper.
- Produces: `store.usage_bucket_start(now: datetime, reset_time: time) -> datetime`
  — a timezone-aware UTC `datetime`, the instant the current usage window
  began. Task 7 calls it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_key_usage_cap.py`:

```python
"""Per-key daily usage cap: the pure usage-day bucket helper and the
`reviews`-derived usage query that the dispatcher's pre-flight cap check
reads (design doc 2026-08-15-key-usage-cap-design.md §3).

usage_bucket_start is pure and needs no database; the get_key_usage tests
below use the shared Postgres test harness (tests/conftest.py).
"""
from __future__ import annotations

from datetime import datetime, time, timezone

from app.queue import store


def _utc(y, mo, d, h, mi, s=0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


RESET = time(4, 0)


def test_bucket_start_is_today_when_now_is_after_the_reset():
    assert store.usage_bucket_start(_utc(2026, 1, 1, 12, 0), RESET) == _utc(2026, 1, 1, 4, 0)


def test_bucket_start_is_yesterday_when_now_is_before_the_reset():
    assert store.usage_bucket_start(_utc(2026, 1, 1, 3, 59), RESET) == _utc(2025, 12, 31, 4, 0)


def test_bucket_start_at_the_exact_reset_instant_is_today():
    """The reset instant itself starts the NEW window (>=, not >), so a review
    landing exactly on the boundary is accounted to the fresh day."""
    assert store.usage_bucket_start(_utc(2026, 1, 1, 4, 0), RESET) == _utc(2026, 1, 1, 4, 0)


def test_bucket_start_respects_minute_granularity():
    reset = time(4, 30)
    assert store.usage_bucket_start(_utc(2026, 1, 1, 4, 29), reset) == _utc(2025, 12, 31, 4, 30)
    assert store.usage_bucket_start(_utc(2026, 1, 1, 4, 31), reset) == _utc(2026, 1, 1, 4, 30)


def test_bucket_start_respects_second_granularity():
    reset = time(4, 30, 15)
    assert store.usage_bucket_start(_utc(2026, 1, 1, 4, 30, 14), reset) == _utc(
        2025, 12, 31, 4, 30, 15
    )
    assert store.usage_bucket_start(_utc(2026, 1, 1, 4, 30, 16), reset) == _utc(
        2026, 1, 1, 4, 30, 15
    )


def test_bucket_start_with_midnight_reset_is_calendar_day():
    midnight = time(0, 0)
    assert store.usage_bucket_start(_utc(2026, 1, 1, 0, 0), midnight) == _utc(2026, 1, 1, 0, 0)
    assert store.usage_bucket_start(_utc(2026, 1, 1, 23, 59), midnight) == _utc(2026, 1, 1, 0, 0)


def test_bucket_start_crosses_a_month_boundary_backwards():
    assert store.usage_bucket_start(_utc(2026, 3, 1, 1, 0), RESET) == _utc(2026, 2, 28, 4, 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_key_usage_cap.py -v`
Expected: FAIL — `AttributeError: module 'app.queue.store' has no attribute 'usage_bucket_start'`

- [ ] **Step 3: Add the helper**

In `app/queue/store.py`, widen the datetime import on line 18:

```python
from datetime import datetime, time, timedelta, timezone
```

Then add immediately after `next_cooldown_level` (`app/queue/store.py:186`):

```python
def usage_bucket_start(now: datetime, reset_time: time) -> datetime:
    """UTC instant the current usage window began.

    If ``now``'s UTC time-of-day is before ``reset_time``, the window started
    at *yesterday's* reset_time; otherwise today's. The boundary instant
    itself belongs to the NEW window, so a review landing exactly on it is
    accounted to the fresh day.

    Pure function of (now, reset_time) -- no DB state -- and colocated here
    with effective_cooldown/next_cooldown_level, the existing precedent for
    small pure helpers living beside the module that calls them.
    """
    candidate = datetime.combine(now.date(), reset_time, tzinfo=timezone.utc)
    if now.time() < reset_time:
        candidate -= timedelta(days=1)
    return candidate
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_key_usage_cap.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check .
git add app/queue/store.py tests/test_key_usage_cap.py
git commit -m "feat(store): add usage_bucket_start usage-day boundary helper"
```

---

### Task 3: Record which key slot paid for each review

**Files:**
- Modify: `app/queue/store.py:74` (schema: new `reviews.key_index` column),
  `app/queue/store.py:366-392` (`record_review`)
- Modify: `app/orchestrator.py:27` (import), `app/orchestrator.py:127-135`
  (pass the active key index)
- Test: `tests/test_reviews_store.py` (extend + update 6 existing call sites),
  `tests/test_dashboard_api.py:52` (update 1 call site),
  `tests/test_orchestrator.py:170` (update 1 fake signature)

**Interfaces:**
- Consumes: `app.providers.key_index.active_key_index(provider) -> int`
  (already exists — returns the DB-override slot when cached, else `0`).
- Produces: `store.record_review(repo_full_name, pr_number, review, comment_id,
  now, key_index)` — `key_index: int` is a new **required** trailing
  parameter. Task 4's `get_key_usage` reads the column it writes.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_reviews_store.py`:

```python
def test_record_review_persists_the_key_index_it_was_given(db_query):
    """Which key slot paid for a review is the whole basis of the per-slot
    usage cap (design doc §3) -- it must be persisted, not inferred later
    from whatever slot happens to be active at read time."""
    store.record_review(
        "owner/repo", 44, _review(pr_number=44), comment_id=None,
        now="2026-08-11T12:00:00+00:00", key_index=2,
    )
    rows = db_query("SELECT key_index FROM reviews WHERE pr_number = 44")
    assert rows == [(2,)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_reviews_store.py::test_record_review_persists_the_key_index_it_was_given -v`
Expected: FAIL — `TypeError: record_review() got an unexpected keyword argument 'key_index'`

- [ ] **Step 3: Add the column and the parameter**

In `app/queue/store.py`'s `_SCHEMA`, add one line immediately after the
`reviews_created_at_idx` line (`app/queue/store.py:74`), just before the
closing `"""`:

```sql
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS key_index INTEGER;
```

Then rewrite `record_review` (`app/queue/store.py:366-392`) as:

```python
def record_review(
    repo_full_name: str,
    pr_number: int,
    review: ReviewResult,
    comment_id: int | None,
    now: str,
    key_index: int,
) -> None:
    """Persist a completed review for the dashboard (insert-only).

    ``key_index`` is the API-key slot that actually paid for this review; it
    is what get_key_usage() sums over, so the per-slot daily cap can be
    scoped to one credential. Deliberately has NO default: silently
    attributing a review to slot 0 would corrupt exactly the accounting the
    cap depends on. Rows written before this column existed are NULL and are
    read as index 0.

    Callers must never let a failure here affect the review itself -- the PR
    comment is already posted by the time this is called.
    """
    results = Jsonb([r.model_dump() for r in review.results])
    with _require_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO reviews
              (repo_full_name, pr_number, provider, model, comment_id, created_at,
               total_elapsed_ms, total_tokens_in, total_tokens_out, est_cost_usd, results,
               key_index)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                repo_full_name, pr_number, review.provider, review.model, comment_id, now,
                review.total_elapsed_ms, review.total_tokens_in, review.total_tokens_out,
                review.est_cost_usd, results, key_index,
            ),
        )
```

- [ ] **Step 4: Update the existing `record_review` call sites**

`tests/test_reviews_store.py` — add `key_index=0` to all six calls:

```python
    store.record_review(
        "owner/repo", 42, _review(), comment_id=999, now="2026-08-11T12:00:00+00:00",
        key_index=0,
    )
```
```python
    store.record_review(
        "owner/repo", 43, _review(pr_number=43), comment_id=None,
        now="2026-08-11T12:00:00+00:00", key_index=0,
    )
```
```python
    store.record_review(
        "owner/repo", 1, _review(pr_number=1), 1, now="2026-08-11T12:00:00+00:00", key_index=0
    )
    store.record_review(
        "owner/repo", 2, _review(pr_number=2), 2, now="2026-08-11T12:00:01+00:00", key_index=0
    )
    store.record_review(
        "owner/repo", 3, _review(pr_number=3), 3, now="2026-08-11T12:00:02+00:00", key_index=0
    )
```
```python
    store.record_review(
        "owner/repo", 1, _review(cost=0.001), 1, now="2026-08-11T12:00:00+00:00", key_index=0
    )
    store.record_review(
        "owner/repo", 2, _review(cost=0.002), 2, now="2026-08-11T12:00:01+00:00", key_index=0
    )
```

`tests/test_dashboard_api.py:52`:

```python
    store.record_review(
        "owner/repo", 42, review, comment_id=999, now="2026-08-11T12:00:00+00:00", key_index=0
    )
```

`tests/test_orchestrator.py:170` — the fake gains the parameter and asserts it:

```python
    def fake_record_review(repo_full_name, pr_number, review, comment_id, now, key_index):
        recorded["repo_full_name"] = repo_full_name
        recorded["pr_number"] = pr_number
        recorded["review"] = review
        recorded["comment_id"] = comment_id
        recorded["now"] = now
        recorded["key_index"] = key_index
```

and add one assertion at the end of that test's existing assertion block:

```python
    assert recorded["key_index"] == 0     # no override cached -> the base slot
```

- [ ] **Step 5: Wire the orchestrator to pass the active index**

In `app/orchestrator.py`, add the import beside the existing
`from app.providers.active import active_provider` (line 27):

```python
from app.providers.key_index import active_key_index
```

Then extend the `store.record_review` call (`app/orchestrator.py:127-135`) —
`provider` is already resolved above on line 108:

```python
    try:
        await asyncio.to_thread(
            store.record_review,
            repo_full_name,
            pr_number,
            review_result,
            posted.id,
            datetime.now(timezone.utc).isoformat(),
            active_key_index(provider),
        )
```

- [ ] **Step 6: Run the affected tests**

Run: `uv run pytest tests/test_reviews_store.py tests/test_dashboard_api.py tests/test_orchestrator.py -v`
Expected: PASS

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check .
git add app/queue/store.py app/orchestrator.py tests/test_reviews_store.py \
        tests/test_dashboard_api.py tests/test_orchestrator.py
git commit -m "feat(store): record the API-key slot that paid for each review"
```

---

### Task 4: `store.get_key_usage` — the per-slot daily usage query

**Files:**
- Modify: `app/queue/store.py` (add after `record_review`)
- Test: `tests/test_key_usage_cap.py` (extend)

**Interfaces:**
- Consumes: the `reviews.key_index` column from Task 3.
- Produces: `store.get_key_usage(provider: str, key_index: int, since: str) ->
  tuple[int, float]` — `(tokens_total, cost_total_usd)`. Task 7 calls it.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_key_usage_cap.py` — and add these imports/fixtures at
the top of the file, below the existing `from app.queue import store`:

```python
import pytest

from app.specialists.schemas import ReviewResult, SpecialistResult


@pytest.fixture
def _temp_db(db):
    """Opt-in: only the get_key_usage tests below touch the database; the
    usage_bucket_start tests above are pure and must stay DB-free."""
    yield


def _review(provider: str, tokens_in: int, tokens_out: int, cost: float) -> ReviewResult:
    return ReviewResult(
        pr_number=1,
        provider=provider,
        model="llama-3.3-70b-versatile",
        results=[SpecialistResult(name="Security", status="ok", findings=[], elapsed_ms=1)],
        total_elapsed_ms=1,
        total_tokens_in=tokens_in,
        total_tokens_out=tokens_out,
        est_cost_usd=cost,
    )


def _record(provider="groq", key_index=0, created_at="2026-01-01T12:00:00+00:00",
            tokens_in=100, tokens_out=50, cost=0.01):
    review = _review(provider, tokens_in, tokens_out, cost)
    store.record_review("owner/repo", 1, review, None, created_at, key_index)


SINCE = "2026-01-01T04:00:00+00:00"
```

then the tests themselves:

```python
def test_get_key_usage_on_empty_table_is_zero(_temp_db):
    assert store.get_key_usage("groq", 0, SINCE) == (0, 0.0)


def test_get_key_usage_sums_tokens_and_cost_for_the_matching_slot(_temp_db):
    _record(tokens_in=100, tokens_out=50, cost=0.01)
    _record(tokens_in=200, tokens_out=25, cost=0.02)
    tokens, cost = store.get_key_usage("groq", 0, SINCE)
    assert tokens == 375                      # (100+50) + (200+25)
    assert cost == pytest.approx(0.03)


def test_get_key_usage_ignores_other_key_slots(_temp_db):
    _record(key_index=0, tokens_in=100, tokens_out=0, cost=0.01)
    _record(key_index=1, tokens_in=900, tokens_out=0, cost=0.09)
    assert store.get_key_usage("groq", 0, SINCE) == (100, pytest.approx(0.01))
    assert store.get_key_usage("groq", 1, SINCE) == (900, pytest.approx(0.09))


def test_get_key_usage_ignores_other_providers(_temp_db):
    _record(provider="groq", tokens_in=100, tokens_out=0, cost=0.01)
    _record(provider="vertex", tokens_in=900, tokens_out=0, cost=0.09)
    assert store.get_key_usage("groq", 0, SINCE) == (100, pytest.approx(0.01))


def test_get_key_usage_treats_a_null_key_index_as_slot_zero(_temp_db, db_exec):
    """Rows written before the key_index column existed are NULL; they must
    count toward slot 0, the same nullable-means-index-0 convention the
    runtime_config override columns already use (design doc §3)."""
    db_exec(
        "INSERT INTO reviews (repo_full_name, pr_number, provider, model, comment_id, "
        "created_at, total_elapsed_ms, total_tokens_in, total_tokens_out, est_cost_usd, "
        "results) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '[]'::jsonb)",
        ("owner/repo", 7, "groq", "m", None, "2026-01-01T12:00:00+00:00", 1, 40, 10, 0.005),
    )
    assert store.get_key_usage("groq", 0, SINCE) == (50, pytest.approx(0.005))


def test_get_key_usage_excludes_rows_before_the_window(_temp_db):
    _record(created_at="2026-01-01T03:59:59+00:00", tokens_in=900, tokens_out=0, cost=0.09)
    _record(created_at="2026-01-01T12:00:00+00:00", tokens_in=100, tokens_out=0, cost=0.01)
    assert store.get_key_usage("groq", 0, SINCE) == (100, pytest.approx(0.01))


def test_get_key_usage_includes_a_row_exactly_at_since(_temp_db):
    """`since` is inclusive -- it is the bucket-start instant itself, which
    belongs to the new window (see usage_bucket_start)."""
    _record(created_at=SINCE, tokens_in=100, tokens_out=0, cost=0.01)
    assert store.get_key_usage("groq", 0, SINCE) == (100, pytest.approx(0.01))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_key_usage_cap.py -v`
Expected: FAIL — `AttributeError: module 'app.queue.store' has no attribute 'get_key_usage'`

- [ ] **Step 3: Add the query**

In `app/queue/store.py`, add immediately after `record_review` (before the
`_TICKET_STATUSES` constant):

```python
def get_key_usage(provider: str, key_index: int, since: str) -> tuple[int, float]:
    """(tokens_total, cost_total_usd) recorded for this (provider, key_index)
    since ``since`` (inclusive, an ISO-8601 UTC string).

    Derived with a SUM over `reviews` rather than kept in a dedicated
    running-total table: at free-tier volume (~20 PRs/day) the aggregate
    costs nothing, and there is no second copy of the number that could
    drift out of sync with the review history it is supposed to describe.
    A NULL key_index (row written before that column existed) counts as
    index 0.
    """
    with _require_pool().connection() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(total_tokens_in + total_tokens_out), 0) AS tokens,
                   COALESCE(SUM(est_cost_usd), 0) AS cost
            FROM reviews
            WHERE provider = %s
              AND COALESCE(key_index, 0) = %s
              AND created_at >= %s
            """,
            (provider, key_index, since),
        ).fetchone()
    return (int(row["tokens"]), float(row["cost"]))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_key_usage_cap.py -v`
Expected: PASS (14 tests)

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check .
git add app/queue/store.py tests/test_key_usage_cap.py
git commit -m "feat(store): add get_key_usage per-slot daily usage query"
```

---

### Task 5: `tickets.defer_reason` and `store.defer_usage_capped`

**Files:**
- Modify: `app/queue/store.py:48` (schema), `app/queue/store.py:96-113`
  (`Ticket` dataclass), `app/queue/store.py:253-261` (`enqueue_or_update`
  re-arm branch), `app/queue/store.py:287-294` (`defer_rate_limited`),
  `app/queue/store.py:310-346` (`finalize_review`), plus the new
  `defer_usage_capped`
- Test: `tests/test_queue_store.py` (extend)

**Interfaces:**
- Produces: `store.Ticket.defer_reason: str | None` (read by Task 7's
  `post_pending_notices` passthrough); `store.defer_usage_capped(ticket_id:
  int, not_before: str, now: str) -> None` (called by Task 7).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_queue_store.py`:

```python
def test_defer_usage_capped_defers_with_the_usage_cap_reason():
    """The bot's own self-imposed cap must be distinguishable from a provider
    rate-limit wait, because the PR notice wording differs (design doc §4.1).
    The ticket row is the durable carrier of that distinction -- the notice
    sweep has no other memory of why a ticket is deferred."""
    tid = _enqueue()
    store.defer_usage_capped(tid, not_before=FUTURE, now=T1)
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.not_before == FUTURE
    assert t.defer_reason == "usage_cap"
    assert t.attempts == 0            # a self-imposed wait is not a failure


def test_defer_rate_limited_clears_a_stale_usage_cap_reason():
    """A stale 'usage_cap' must never survive into a later, unrelated
    deferral of the same row -- it would mislabel a genuine provider wait."""
    tid = _enqueue()
    store.defer_usage_capped(tid, not_before=FUTURE, now=T1)
    store.defer_rate_limited(tid, not_before=FUTURE, now=T1)
    assert store.get_ticket(tid).defer_reason is None


def test_cooldown_re_arm_on_push_clears_a_stale_usage_cap_reason(db_exec):
    tid = _enqueue()
    store.defer_usage_capped(tid, not_before=FUTURE, now=T1)
    # Terminal state + a recent completed review -> enqueue_or_update's
    # done/failed re-arm branch, which re-defers for the cooldown.
    db_exec(
        "UPDATE tickets SET status='done', last_reviewed_at=%s WHERE id=%s", (T0, tid)
    )
    _enqueue(now=T1)
    t = store.get_ticket(tid)
    assert t.status == "deferred"      # still inside the 300s cooldown
    assert t.defer_reason is None


def test_finalize_review_clears_a_stale_usage_cap_reason(db_exec):
    tid = _enqueue()
    store.defer_usage_capped(tid, not_before=FUTURE, now=T1)
    db_exec("UPDATE tickets SET status='running', rereview_requested=1 WHERE id=%s", (tid,))
    store.finalize_review(tid, now=T1, rereview_not_before=FUTURE, rereview_cooldown_level=1)
    t = store.get_ticket(tid)
    assert t.status == "deferred"      # dirty-flag re-arm
    assert t.defer_reason is None


def test_new_ticket_has_no_defer_reason():
    assert store.get_ticket(_enqueue()).defer_reason is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_queue_store.py -v -k "defer_reason or usage_cap"`
Expected: FAIL — `AttributeError: module 'app.queue.store' has no attribute 'defer_usage_capped'`

Also note `test_schema_columns_match_the_ticket_dataclass` (already in this
file) will fail once the column is added but the dataclass is not — that test
is the machine-checked guard that keeps `_SCHEMA` and `Ticket` in sync.

- [ ] **Step 3: Add the column and the dataclass field**

In `app/queue/store.py`'s `_SCHEMA`, add one line immediately after the
existing `ALTER TABLE tickets ADD COLUMN IF NOT EXISTS last_error TEXT;`
(`app/queue/store.py:48`):

```sql
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS defer_reason TEXT;
```

And add the matching field as the last entry of the `Ticket` dataclass
(after `last_error: str | None`, `app/queue/store.py:113`):

```python
    # Why this ticket is deferred: NULL/'provider' = a provider rate limit or
    # a re-review cooldown (today's only meaning); 'usage_cap' = the bot's own
    # per-key daily cap. Drives which wording the PR notice uses.
    defer_reason: str | None
```

- [ ] **Step 4: Add `defer_usage_capped`**

In `app/queue/store.py`, add immediately after `defer_rate_limited`:

```python
def defer_usage_capped(ticket_id: int, not_before: str, now: str) -> None:
    """Defer until the bot's own per-key daily usage cap resets.

    The ONLY writer of defer_reason='usage_cap'. Like defer_rate_limited this
    does NOT touch `attempts` -- a self-imposed wait is not a failure and must
    never count toward the hard stop.
    """
    with _require_pool().connection() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'deferred', not_before = %s, "
            "defer_reason = 'usage_cap', updated_at = %s WHERE id = %s",
            (not_before, now, ticket_id),
        )
```

- [ ] **Step 5: Clear `defer_reason` in the three other deferral writers**

`defer_rate_limited` (`app/queue/store.py:287-294`) — extend the docstring and
the statement:

```python
def defer_rate_limited(ticket_id: int, not_before: str, now: str) -> None:
    """Per-provider rate-limit deferral. Does NOT count toward the hard stop.

    Explicitly clears defer_reason: a stale 'usage_cap' left over from an
    earlier deferral of this same row would mislabel this provider wait as
    the bot's own cap in the PR notice.
    """
    with _require_pool().connection() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'deferred', not_before = %s, "
            "defer_reason = NULL, updated_at = %s WHERE id = %s",
            (not_before, now, ticket_id),
        )
```

`enqueue_or_update`'s done/failed re-arm branch (`app/queue/store.py:257-261`)
— same reason, add `defer_reason = NULL` to the UPDATE:

```python
            conn.execute(
                "UPDATE tickets SET head_sha = %s, status = %s, not_before = %s, attempts = 0, "
                "rereview_requested = 0, cooldown_level = %s, defer_reason = NULL, "
                "updated_at = %s WHERE id = %s",
                (head_sha, new_status, not_before, new_level, now, ticket_id),
            )
```

`finalize_review` (`app/queue/store.py:326-338`) — add the assignment to the
statement, unconditionally (both the 'deferred' re-arm and the 'done' branch
want it cleared):

```python
            UPDATE tickets SET
              last_reviewed_at   = %(now)s,
              comment_id         = COALESCE(%(comment_id)s, comment_id),
              status             = CASE WHEN rereview_requested = 1 THEN 'deferred' ELSE 'done' END,
              not_before         = CASE WHEN rereview_requested = 1 THEN %(rnb)s ELSE NULL END,
              attempts           = CASE WHEN rereview_requested = 1 THEN 0 ELSE attempts END,
              cooldown_level     = CASE WHEN rereview_requested = 1
                                        THEN %(new_level)s ELSE cooldown_level END,
              rereview_requested = 0,
              defer_reason       = NULL,
              updated_at         = %(now)s
            WHERE id = %(id)s
```

Note deliberately NOT changed: `defer_failed` sets `status = 'retrying'`, not
`'deferred'`, and nothing reads `defer_reason` on a non-deferred row — every
path that later returns such a ticket to `'deferred'` goes through one of the
three writers above, each of which writes the column explicitly.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_queue_store.py -v`
Expected: PASS (including `test_schema_columns_match_the_ticket_dataclass`)

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check .
git add app/queue/store.py tests/test_queue_store.py
git commit -m "feat(store): add tickets.defer_reason and defer_usage_capped"
```

---

### Task 6: Distinguishable PR wording for a usage-cap wait

**Files:**
- Modify: `app/formatting.py:17` (import `Literal`),
  `app/formatting.py:136-152` (`format_placeholder`),
  `app/formatting.py:182-195` (`format_schedule_notice`)
- Test: `tests/test_placeholder_formatting.py`,
  `tests/test_schedule_notice_formatting.py` (both extended)

**Interfaces:**
- Produces:
  `format_placeholder(pr_number: int, retry_after: float, now: datetime,
  reason: Literal["provider", "usage_cap"] = "provider") -> str` and
  `format_schedule_notice(not_before: datetime,
  reason: Literal["provider", "usage_cap"] = "provider") -> str`.
  Task 7 passes `reason` through from both dispatcher call paths.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_placeholder_formatting.py`:

```python
def test_usage_cap_placeholder_is_distinct_from_a_provider_rate_limit():
    """The bot's own cap must not read as the provider's problem -- an
    operator debugging a stalled review needs to know which limit hit."""
    body = format_placeholder(pr_number=42, retry_after=6 * 3600, now=NOW, reason="usage_cap")
    assert COMMENT_MARKER in body
    assert "PR #42" in body
    assert "usage limit" in body.lower()
    assert "not a provider rate limit" in body.lower()
    assert "18:00 UTC" in body                      # ETA still computed from now+retry_after


def test_usage_cap_placeholder_wording_ignores_the_wait_magnitude():
    """Unlike the provider branch, the usage-cap wording does not switch on
    short-vs-long waits -- the cause is the same either way."""
    short = format_placeholder(pr_number=42, retry_after=30.0, now=NOW, reason="usage_cap")
    assert "usage limit" in short.lower()
    assert "queued behind rate limit" not in short.lower()
    assert "12:00 UTC" in short                     # now + 30s still rounds to 12:00


def test_placeholder_default_reason_is_byte_identical_to_the_old_output():
    """Every existing call site passes no `reason` -- their output must not
    shift by a single character (design doc §4.1)."""
    for retry_after in (30.0, 6 * 3600):
        assert format_placeholder(42, retry_after, NOW) == format_placeholder(
            42, retry_after, NOW, reason="provider"
        )
    assert "Queued behind rate limit" in format_placeholder(42, 30.0, NOW)
    assert "Daily model quota reached" in format_placeholder(42, 6 * 3600, NOW)
```

Append to `tests/test_schedule_notice_formatting.py`:

```python
def test_usage_cap_schedule_notice_is_distinct_and_still_markered():
    body = format_schedule_notice(NOW, reason="usage_cap")
    assert body.startswith(SCHEDULE_NOTE_START)
    assert body.endswith(SCHEDULE_NOTE_END)
    assert "12:00 UTC" in body
    assert "usage limit" in body.lower()
    assert "not a provider quota issue" in body.lower()


def test_schedule_notice_default_reason_is_byte_identical_to_the_old_output():
    """Every existing call site passes no `reason` -- their output must not
    shift by a single character (design doc §4.1)."""
    assert format_schedule_notice(NOW) == format_schedule_notice(NOW, reason="provider")
    assert format_schedule_notice(NOW) == (
        f"{SCHEDULE_NOTE_START}\n🔄 Re-review scheduled ~12:00 UTC\n{SCHEDULE_NOTE_END}"
    )


def test_usage_cap_schedule_notice_still_rejects_a_naive_datetime():
    naive = datetime(2026, 1, 1, 12, 0, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        format_schedule_notice(naive, reason="usage_cap")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_placeholder_formatting.py tests/test_schedule_notice_formatting.py -v`
Expected: FAIL — `TypeError: format_placeholder() got an unexpected keyword argument 'reason'`

- [ ] **Step 3: Add the `reason` parameter to both formatters**

In `app/formatting.py`, add to the imports (after the `datetime` import on
line 17):

```python
from typing import Literal
```

Replace `format_placeholder` (`app/formatting.py:136-152`):

```python
def format_placeholder(
    pr_number: int,
    retry_after: float,
    now: datetime,
    reason: Literal["provider", "usage_cap"] = "provider",
) -> str:
    """Marker-prefixed placeholder comment shown while a review is delayed.

    The real result later edits this same comment in place (found via the
    marker). ``reason`` defaults to "provider", so every pre-existing call
    site renders byte-identically to before:

    - "provider": wording is chosen by wait magnitude -- short = per-minute
      rate limit; long = the provider's daily quota, with an ETA computed
      from ``now + retry_after``.
    - "usage_cap": the bot's OWN per-key daily cap. Always the same wording
      regardless of wait length (the cause doesn't change with magnitude),
      and explicit that this is not the provider's limit -- an operator
      debugging a stalled review must not go hunting at the provider.
    """
    header = f"## 🤖 Automated Code Review — PR #{pr_number}\n"
    eta = (now + timedelta(seconds=retry_after)).strftime("%H:%M UTC")
    if reason == "usage_cap":
        note = (
            "⏳ Bot's own daily usage limit reached for this key — review "
            "queued, will post automatically after the limit resets "
            f"(~{eta}). This is not a provider rate limit."
        )
    elif retry_after < PLACEHOLDER_DAILY_THRESHOLD_SECONDS:
        note = "⏳ Queued behind rate limit — review will appear shortly."
    else:
        note = (
            "⏳ Daily model quota reached — review queued, will post "
            f"automatically after the provider's limit resets (~{eta})."
        )
    return f"{COMMENT_MARKER}\n{header}\n_{note}_\n"
```

Replace `format_schedule_notice` (`app/formatting.py:182-195`):

```python
def format_schedule_notice(
    not_before: datetime, reason: Literal["provider", "usage_cap"] = "provider"
) -> str:
    """Self-cleaning notice appended below a preserved good review when the
    next re-review is scheduled (cooldown, rate-limit wait, or the bot's own
    usage cap). Absolute UTC time only -- GitHub's comment body can't be
    localized per viewer, and this note is only edited on a re-arm event (not
    continuously updated), so a relative string would go stale the moment
    it's posted. Requires a timezone-aware ``not_before``:
    ``datetime.astimezone()`` silently treats a naive datetime as system-local
    time rather than raising, so a naive value is rejected explicitly here
    instead of producing a host-timezone-dependent result.

    ``reason`` defaults to "provider", preserving today's exact wording for
    every pre-existing call site.
    """
    if not_before.tzinfo is None:
        raise ValueError("format_schedule_notice requires a timezone-aware datetime")
    eta = not_before.astimezone(timezone.utc).strftime("%H:%M UTC")
    if reason == "usage_cap":
        body = (
            f"🔄 Re-review scheduled ~{eta} (usage limit reached — resets "
            "automatically, not a provider quota issue)"
        )
    else:
        body = f"🔄 Re-review scheduled ~{eta}"
    return f"{SCHEDULE_NOTE_START}\n{body}\n{SCHEDULE_NOTE_END}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_placeholder_formatting.py tests/test_schedule_notice_formatting.py tests/test_formatting.py -v`
Expected: PASS

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check .
git add app/formatting.py tests/test_placeholder_formatting.py \
        tests/test_schedule_notice_formatting.py
git commit -m "feat(formatting): distinguish a usage-cap wait from a provider wait"
```

---

### Task 7: The dispatcher pre-flight cap check

**Files:**
- Modify: `app/queue/dispatcher.py:91-96` (`_post_placeholder` gains
  `reason`), `app/queue/dispatcher.py:99-122` (`post_pending_notices` passes
  `ticket.defer_reason`), `app/queue/dispatcher.py:180-198` (new block
  inserted immediately above the blocked-provider check)
- Test: `tests/test_dispatcher.py` (extend)

**Interfaces:**
- Consumes: `settings.key_usage_token_cap` / `key_usage_cost_cap_usd` /
  `key_usage_reset_time_utc` (Task 1); `store.usage_bucket_start` (Task 2);
  `store.get_key_usage` (Task 4); `store.defer_usage_capped` and
  `Ticket.defer_reason` (Task 5); the `reason` parameter on
  `format_placeholder` / `format_schedule_notice` (Task 6);
  `key_index.active_key_index(provider)` (already exists).
- Produces: no new public names — `process_next_due` returns the existing
  `StepResult(action="deferred", ticket_id=...)` on the capped path.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dispatcher.py`. First a helper, placed beside the other
helpers near the top of the file (after `_set_comment_id`):

```python
def _record_usage(db_exec, tokens=0, cost=0.0, provider="groq", key_index=0, created_at=None):
    """Insert one completed-review row directly. Raw SQL rather than
    store.record_review so this file needn't build a whole ReviewResult; the
    `results` JSONB is an inline SQL literal so no json adapter is needed."""
    db_exec(
        "INSERT INTO reviews (repo_full_name, pr_number, provider, model, comment_id, "
        "created_at, total_elapsed_ms, total_tokens_in, total_tokens_out, est_cost_usd, "
        "results, key_index) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'[]'::jsonb,%s)",
        ("owner/repo", 1, provider, "m", None, created_at or NOW.isoformat(), 1,
         tokens, 0, cost, key_index),
    )
```

then the tests:

```python
# NOW is 2026-01-01T12:00Z and the reset defaults to 04:00 UTC, so the
# current usage bucket started at 04:00 today and next resets at 04:00 on
# the 2nd -- 16 hours out.
CAP_RESET_AT = datetime(2026, 1, 2, 4, 0, 0, tzinfo=timezone.utc)


async def test_over_token_cap_defers_without_calling_attempt_review(monkeypatch, db_exec):
    posted = _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "key_usage_token_cap", 500)
    tid = _enqueue(pr=90)
    _set_comment_id(db_exec, tid, 9090)
    _record_usage(db_exec, tokens=500)          # exactly at the cap -> already over

    called = []

    async def fake_attempt(repo, pr, comment_id=None):
        called.append(pr)
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    result = await dispatcher.process_next_due(NOW)

    assert result.action == "deferred"
    assert called == []                          # the whole point: no call is made at all
    t = store.get_ticket(tid)
    assert t.status == "deferred"
    assert t.defer_reason == "usage_cap"
    assert t.not_before == CAP_RESET_AT.isoformat()
    assert posted and posted[0][0] == 90
    assert "usage limit" in posted[0][1].lower()
    assert posted[0][2] == 9090                  # threaded comment_id preserved


async def test_under_token_cap_runs_normally(monkeypatch, db_exec):
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "key_usage_token_cap", 500)
    tid = _enqueue(pr=91)
    _record_usage(db_exec, tokens=499)

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    assert (await dispatcher.process_next_due(NOW)).action == "ran"
    assert store.get_ticket(tid).status == "done"


async def test_usage_before_the_bucket_start_does_not_count(monkeypatch, db_exec):
    """Yesterday's spend must not hold today's reviews hostage."""
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "key_usage_token_cap", 500)
    _enqueue(pr=92)
    _record_usage(db_exec, tokens=9000, created_at="2026-01-01T03:00:00+00:00")

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    assert (await dispatcher.process_next_due(NOW)).action == "ran"


async def test_another_key_slots_usage_does_not_count(monkeypatch, db_exec):
    """A slot swap grants a fresh budget with no special-case code -- the
    query is scoped to whatever active_key_index() resolves to now."""
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "key_usage_token_cap", 500)
    _enqueue(pr=93)
    _record_usage(db_exec, tokens=9000, key_index=1)

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    assert (await dispatcher.process_next_due(NOW)).action == "ran"


async def test_cost_cap_applies_when_no_token_cap_is_set(monkeypatch, db_exec):
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "key_usage_token_cap", None)
    monkeypatch.setattr(settings, "key_usage_cost_cap_usd", 0.05)
    tid = _enqueue(pr=94)
    _record_usage(db_exec, tokens=1, cost=0.06)

    called = []

    async def fake_attempt(repo, pr, comment_id=None):
        called.append(pr)
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    assert (await dispatcher.process_next_due(NOW)).action == "deferred"
    assert called == []
    assert store.get_ticket(tid).defer_reason == "usage_cap"


async def test_token_cap_wins_outright_when_both_caps_are_set(monkeypatch, db_exec):
    """The cost cap is not consulted AT ALL when a token cap is set -- not
    merely a tiebreak (design doc §2.1). A blown cost cap must therefore not
    defer while the token cap still has headroom."""
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "key_usage_token_cap", 500)
    monkeypatch.setattr(settings, "key_usage_cost_cap_usd", 0.0001)
    _enqueue(pr=95)
    _record_usage(db_exec, tokens=100, cost=9.99)

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    assert (await dispatcher.process_next_due(NOW)).action == "ran"


async def test_no_cap_configured_never_queries_usage(monkeypatch, db_exec):
    """Feature off by default: an existing deployment must not even pay for
    the query, let alone change behavior."""
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "key_usage_token_cap", None)
    monkeypatch.setattr(settings, "key_usage_cost_cap_usd", None)
    _enqueue(pr=96)
    _record_usage(db_exec, tokens=10**9, cost=10**6)

    queried = []
    monkeypatch.setattr(
        dispatcher.store, "get_key_usage",
        lambda *a, **kw: queried.append(a) or (0, 0.0),
    )

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    assert (await dispatcher.process_next_due(NOW)).action == "ran"
    assert queried == []


async def test_usage_check_failure_fails_open_and_runs_the_review(monkeypatch, db_exec):
    """Cost-cap enforcement degrading to off is the same posture as every
    other override here degrading to its safe default -- a broken usage query
    must never be able to block every review."""
    _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "key_usage_token_cap", 1)
    _enqueue(pr=97)
    _record_usage(db_exec, tokens=9000)

    def boom(*args, **kwargs):
        raise RuntimeError("usage query exploded")

    monkeypatch.setattr(dispatcher.store, "get_key_usage", boom)

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    assert (await dispatcher.process_next_due(NOW)).action == "ran"


async def test_capped_ticket_with_a_visible_review_gets_no_placeholder(monkeypatch, db_exec):
    """A good review already on the PR is preserved; the notice sweep shows
    the schedule footnote instead (same rule as every other deferral)."""
    posted = _stub_comments(monkeypatch)
    monkeypatch.setattr(settings, "key_usage_token_cap", 1)
    tid = _enqueue(pr=98)
    db_exec("UPDATE tickets SET last_reviewed_at=%s WHERE id=%s", (NOW.isoformat(), tid))
    _record_usage(db_exec, tokens=9000)

    async def fake_attempt(repo, pr, comment_id=None):
        return orchestrator.ReviewCompleted(review=type("R", (), {})())

    monkeypatch.setattr(dispatcher, "attempt_review", fake_attempt)

    assert (await dispatcher.process_next_due(NOW)).action == "deferred"
    assert posted == []


async def test_notice_sweep_uses_usage_cap_wording_for_a_usage_capped_ticket(
    monkeypatch, db_exec
):
    """The sweep runs on a later iteration with no memory of why the ticket
    was deferred -- the ticket row is the durable carrier (design doc §4.1)."""
    posted = _stub_append_schedule(monkeypatch)
    tid = _enqueue(pr=99)
    db_exec(
        "UPDATE tickets SET status='deferred', not_before=%s, last_reviewed_at=%s, "
        "defer_reason='usage_cap' WHERE id=%s",
        (CAP_RESET_AT.isoformat(), NOW.isoformat(), tid),
    )

    count = await dispatcher.post_pending_notices(NOW)

    assert count == 1
    assert "usage limit" in posted[0][1].lower()


async def test_notice_sweep_uses_the_unchanged_wording_when_defer_reason_is_null(
    monkeypatch, db_exec
):
    posted = _stub_append_schedule(monkeypatch)
    tid = _enqueue(pr=100)
    db_exec(
        "UPDATE tickets SET status='deferred', not_before=%s, last_reviewed_at=%s WHERE id=%s",
        ((NOW + timedelta(hours=1)).isoformat(), NOW.isoformat(), tid),
    )

    await dispatcher.post_pending_notices(NOW)

    assert "usage limit" not in posted[0][1].lower()
    assert "Re-review scheduled ~13:00 UTC" in posted[0][1]
```

The `_stub_append_schedule` helper already exists further down this file
(`tests/test_dispatcher.py:647`); the two sweep tests above must be placed
below its definition.

Finally, add an autouse fixture beside the other autouse fixtures at the top
of `tests/test_dispatcher.py`, so tests that don't opt in are unaffected by
whatever the caps happen to default to:

```python
@pytest.fixture(autouse=True)
def _caps_off_by_default(monkeypatch):
    monkeypatch.setattr(settings, "key_usage_token_cap", None)
    monkeypatch.setattr(settings, "key_usage_cost_cap_usd", None)
    yield
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_dispatcher.py -v -k "cap or usage"`
Expected: FAIL — the over-cap test reports `action == "ran"` (no gate exists
yet) and `defer_reason` is `None`.

- [ ] **Step 3: Add the `reason` passthrough to `_post_placeholder`**

Replace `app/queue/dispatcher.py:91-96`:

```python
async def _post_placeholder(
    repo: str,
    pr: int,
    retry_after: float,
    now: datetime,
    comment_id: int | None = None,
    reason: str = "provider",
) -> None:
    await asyncio.to_thread(
        github_app.upsert_comment,
        repo,
        pr,
        format_placeholder(pr, retry_after, now, reason=reason),
        comment_id,
    )
```

- [ ] **Step 4: Pass `defer_reason` through the notice sweep**

In `post_pending_notices` (`app/queue/dispatcher.py:108-114`), replace the
`append_schedule_notice` call's third argument:

```python
            await asyncio.to_thread(
                github_app.append_schedule_notice,
                ticket.repo_full_name,
                ticket.pr_number,
                format_schedule_notice(
                    datetime.fromisoformat(ticket.not_before),
                    # The ticket row is the durable record of WHY this ticket
                    # is waiting -- the sweep runs on a later iteration and
                    # has no other memory of it. NULL means today's original
                    # meaning: a provider rate limit or a cooldown wait.
                    reason=ticket.defer_reason or "provider",
                ),
                ticket.comment_id,
            )
```

- [ ] **Step 5: Insert the pre-flight cap check**

In `app/queue/dispatcher.py`, replace the comment + `provider = active_provider()`
line at `180-184` with the block below (the blocked-provider `if` that follows
it stays exactly as-is):

```python
    # Gate on the ACTIVE provider (the DB override when set, else the
    # env-configured default), not the provider recorded on the ticket at
    # enqueue time — attempt_review always runs against whatever provider is
    # active now, so that's what can be blocked or capped. Resolved once here
    # and shared by both gates below.
    provider = active_provider()

    # Pre-flight cap: has this (provider, key slot) already spent its
    # self-imposed daily budget? Checked BEFORE the review, never predicted:
    # a review's real usage is only known once it completes, so the cap bounds
    # when the NEXT review may start, not the exact daily total — the same
    # shape the reactive-429 gate below already has.
    #
    # FAILS OPEN. Every other per-ticket refresh above degrades to its safe
    # default on error; the safe default for a cost cap is "not enforced",
    # because a broken usage query must never be able to block every review.
    # That is why the whole computation — bucket, query, comparison, reset
    # instant — sits inside one try, and why nothing outside it is read.
    cap_reset_at: datetime | None = None
    if settings.key_usage_token_cap is not None or settings.key_usage_cost_cap_usd is not None:
        try:
            bucket_start = store.usage_bucket_start(now, settings.key_usage_reset_time_utc)
            tokens, cost = await asyncio.to_thread(
                store.get_key_usage,
                provider,
                key_index.active_key_index(provider),
                bucket_start.isoformat(),
            )
            # The token cap WINS OUTRIGHT when both are set: the cost cap is
            # not consulted at all, not used as a tiebreak.
            over_cap = (
                tokens >= settings.key_usage_token_cap
                if settings.key_usage_token_cap is not None
                else cost >= settings.key_usage_cost_cap_usd
            )
            if over_cap:
                cap_reset_at = bucket_start + timedelta(hours=24)
        except Exception:  # noqa: BLE001
            logger.exception("failed to check key usage cap; proceeding without it")
            cap_reset_at = None

    if cap_reset_at is not None:
        await asyncio.to_thread(
            store.defer_usage_capped,
            ticket.id,
            not_before=cap_reset_at.isoformat(),
            now=now.isoformat(),
        )
        if not _has_visible_review(ticket):
            await _post_placeholder(
                ticket.repo_full_name,
                ticket.pr_number,
                (cap_reset_at - now).total_seconds(),
                now,
                ticket.comment_id,
                reason="usage_cap",
            )
        return StepResult(action="deferred", ticket_id=ticket.id)

```

- [ ] **Step 6: Run the dispatcher tests**

Run: `uv run pytest tests/test_dispatcher.py tests/test_dispatcher_backoff.py -v`
Expected: PASS (all, old and new)

- [ ] **Step 7: Run the whole suite**

Run: `uv run pytest -q`
Expected: PASS — no existing test's behavior changed, because both caps
default to `None`.

- [ ] **Step 8: Lint and commit**

```bash
uv run ruff check .
git add app/queue/dispatcher.py tests/test_dispatcher.py
git commit -m "feat(dispatcher): defer reviews once the active key hits its daily cap"
```

---

### Task 8: Document the cap

**Files:**
- Modify: `SPEC.md:329-600` (new subsection under §12; amend the config list
  and the "Out of scope" paragraph)
- Modify: `README.md:249` (new subsection after "Tuning the re-review
  cooldown without a redeploy")
- Modify: `.env.example` (new optional block)

**Interfaces:**
- Consumes: the env var names and precedence rule from Task 1; the deferral
  behavior from Tasks 5–7. Produces no code.

- [ ] **Step 1: Add the SPEC.md §12 subsection**

In `SPEC.md`, insert immediately after the "**Swapping API-key slots.**"
paragraph (which ends `...the DB says index 2, but nobody ever pushed
GROQ_API_KEY_2 to Render.`) and before "**Re-review scheduled notice.**":

```markdown
**Proactive per-key daily usage cap.** The rate-limit handling above is
*reactive* — it waits for a real 429. `KEY_USAGE_TOKEN_CAP` (or
`KEY_USAGE_COST_CAP_USD`) adds a *proactive* ceiling: before starting a
review, the dispatcher sums `total_tokens_in + total_tokens_out` (or
`est_cost_usd`) over the `reviews` rows belonging to the currently-active
`(provider, key slot)` since the last `KEY_USAGE_RESET_TIME_UTC` boundary
(default `04:00` UTC, any `HH:MM`/`HH:MM:SS` granularity). At or over the
cap, the ticket is deferred to the next reset instead of run — no call is
made at all. Both caps are unset by default, so a deployment that sets
neither env var is unaffected; `KEY_USAGE_TOKEN_CAP` wins outright when both
are set (the cost cap is then not consulted at all). Usage is *derived*
from the persisted `reviews` history rather than counted in memory, so a
restart or redeploy never resets or loses it; a new `reviews.key_index`
column records which slot paid for each review, so swapping slots with
`scripts/set_override.py` immediately grants a fresh budget with no
special-case code. The check is deliberately check-before, not
predict-before: a review's real usage is only known once it completes, so
the cap bounds when the *next* review may start, not the exact daily total —
the same shape the reactive backoff already has. It also **fails open**: any
error while checking logs and proceeds as "not capped", because a broken
usage query must never be able to block every review. A capped ticket's PR
notice is deliberately distinguishable from a provider wait (a new
`tickets.defer_reason` column carries the distinction to the later notice
sweep), so an operator debugging a stalled review isn't sent hunting at the
provider for a limit this app imposed on itself. Full design rationale:
`docs/superpowers/specs/2026-08-15-key-usage-cap-design.md`.
```

- [ ] **Step 2: Amend the §12 config list and the out-of-scope paragraph**

In `SPEC.md`'s **Config** paragraph (`SPEC.md:575-584`), change the closing of
the list from:

```
`DISPATCHER_REREVIEW_COOLDOWN_MAX_SECONDS` (default `3600.0`).
```

to:

```
`DISPATCHER_REREVIEW_COOLDOWN_MAX_SECONDS` (default `3600.0`),
`KEY_USAGE_TOKEN_CAP` (default unset — cap off), `KEY_USAGE_COST_CAP_USD`
(default unset — cap off; ignored entirely when `KEY_USAGE_TOKEN_CAP` is
set), `KEY_USAGE_RESET_TIME_UTC` (default `04:00`). The last three are the
one set of *per-key* caps here; every other var above is a pacing knob.
```

And in the **Out of scope** paragraph (`SPEC.md:590-596`), replace:

```
**Out of scope** (unchanged from the design doc, all deliberate): provider
failover on a daily wall, proactive quota accounting (no `x-ratelimit-*`
tracking, no hardcoded caps), a priority scheme (FIFO is sufficient), and
```

with:

```
**Out of scope** (unchanged from the design doc, all deliberate): provider
failover on a daily wall, a priority scheme (FIFO is sufficient), and
```

and append this sentence to the end of that same paragraph:

```
One item this list previously named — *proactive quota accounting* — was
deliberately reopened and built: see "Proactive per-key daily usage cap"
above. What remains out of scope within it is the provider-reported half —
no `x-ratelimit-*` header tracking, no knowledge of the provider's own
limits; the cap is entirely self-imposed and locally computed.
```

- [ ] **Step 3: Add the README subsection**

In `README.md`, insert a new `####` subsection immediately after the "Tuning
the re-review cooldown without a redeploy" section (i.e. after the paragraph
ending `...will appear to do nothing until you run --clear.`, before
"#### Deploying an image, when the Render service has no connected repo"):

````markdown
#### Capping how much one API key may spend per day

```bash
KEY_USAGE_TOKEN_CAP=20000        # tokens/day for the ACTIVE key slot
KEY_USAGE_COST_CAP_USD=0.50      # or a dollar ceiling instead
KEY_USAGE_RESET_TIME_UTC=04:00   # when the day rolls over (default 04:00 UTC)
```

Both caps are **unset by default** — set neither and nothing changes. When a
cap is in force, the dispatcher checks the currently-active `(provider, key
slot)`'s usage so far today *before* starting a review; at or over the cap it
defers the ticket to the next reset rather than making the call, and the PR
gets a notice saying so in wording that's explicitly distinct from a provider
rate limit. This is the proactive counterpart to the reactive 429 backoff:
it's what stops a bug or a PR spike from burning a free-tier credit before
anyone notices.

**`KEY_USAGE_TOKEN_CAP` wins outright when both are set** — the cost cap is
then not consulted at all, not used as a tiebreak.

Three things worth knowing:

- **The cap is per key slot, not global.** Swapping slots with
  `uv run python -m scripts.set_override groq --index 1` immediately grants
  a fresh budget, exactly as key rotation already works — nothing auto-swaps
  on a breach; a human decides.
- **Usage survives restarts.** It's summed from the persisted `reviews`
  history, not counted in memory, so a redeploy neither resets nor loses it.
- **The reset time takes any `HH:MM` (or `HH:MM:SS`)**, not whole hours —
  set it a couple of minutes out to watch a cap reset during a demo instead
  of waiting for the next hour boundary.

The cap is a ceiling on when the *next* review may start, not on the exact
daily total: a review's real token usage is only known once it finishes, so
the run that crosses the line is allowed to complete. And a failure while
checking usage **fails open** — it logs and proceeds — because a broken
usage query must never be able to block every review.
````

- [ ] **Step 4: Add the `.env.example` block**

In `.env.example`, append at the end of the file (after the operator-tooling
block):

```bash
# --- Optional: proactive per-key daily usage cap ---
# Unset by default (cap off). When set, the dispatcher refuses to START a
# review once the ACTIVE (provider, key slot) has already used this much
# since the last reset, deferring the ticket to the next reset instead.
# KEY_USAGE_TOKEN_CAP wins outright when both are set -- the cost cap is
# then not consulted at all. See README "Capping how much one API key may
# spend per day".
# KEY_USAGE_TOKEN_CAP=20000
# KEY_USAGE_COST_CAP_USD=0.50
# When the usage day rolls over, UTC. Any HH:MM or HH:MM:SS -- not
# whole-hours-only, so a demo can set a reset a couple of minutes out.
KEY_USAGE_RESET_TIME_UTC=04:00
```

- [ ] **Step 5: Verify nothing regressed and the docs are consistent**

Run: `uv run pytest -q && uv run ruff check .`
Expected: PASS

Then re-read the whole of `SPEC.md` §12 top to bottom (not just the edited
lines) and confirm the "Out of scope" paragraph no longer contradicts the new
subsection — this is exactly the "re-read the whole passage after an
override" rule in `CLAUDE.md`.

- [ ] **Step 6: Commit**

```bash
git add SPEC.md README.md .env.example
git commit -m "docs: document the proactive per-key daily usage cap"
```

---

## Final verification

- [ ] `uv run pytest -q` — whole suite green
- [ ] `uv run ruff check .` — clean
- [ ] Confirm the feature is genuinely off by default:
      `uv run python -c "from app.config import Settings; s = Settings();
      print(s.key_usage_token_cap, s.key_usage_cost_cap_usd)"` prints
      `None None` against this repo's real `.env`.
- [ ] Confirm the two new columns are additive-only: `ALTER TABLE ... ADD
      COLUMN IF NOT EXISTS` runs on every `init_pool()`, so a redeploy
      against the existing Supabase database migrates in place with no
      manual step and no data loss. Existing `reviews` rows keep
      `key_index = NULL` and are read as slot 0.
