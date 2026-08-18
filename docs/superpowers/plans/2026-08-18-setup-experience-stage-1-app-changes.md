# Setup Experience — Stage 1 (App Changes) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the app deployable by a stranger on any Postgres — pricing becomes optional instead of a hard model allowlist, the dollar usage cap is removed, `LLM_PROVIDER` loses its implicit default, the schema DDL becomes a clean final-shape declaration, and the presentation-only dashboard section is removed.

**Architecture:** Nine independent, sequentially-mergeable tasks against `app/` and `scripts/`. Nothing here touches documentation structure or adds new CLI entry points beyond `scripts/pricing_check.py` — those are Stages 2 and 3. Each task ends green and committable.

**Tech Stack:** Python 3.12 · pydantic v2 / pydantic-settings · psycopg3 + psycopg_pool (Postgres) · FastAPI · pytest / pytest-asyncio · `uv`.

**Spec:** `docs/superpowers/specs/2026-08-18-setup-experience-design.md` — §6 in full, plus §8d/§8e/§8f/§8i. Read §6 before starting; every task below argues from it.

## Global Constraints

- **Secret handling overrides everything.** Read `CLAUDE.md`'s "Secret handling" section before touching any file. Never open `.env`. Never print a secret value, length-only or name-only output is the contract (`scripts/_render.py::env_vars()`, `scripts/deploy.py::sync_env()`).
- **Never commit on the user's behalf beyond the commits this plan specifies.** Each task's final step is its own commit; do not squash, amend, or add unrelated files.
- **No live LLM calls.** Every test here is deterministic and mocked. `scripts/pricing_check.py` (Task 8) calls only a metadata/listing endpoint, never a generation endpoint — see `CLAUDE.md`'s "LLM API testing hygiene".
- **Python 3.12** (`.python-version`). Type hints use `X | None`, not `Optional[X]`.
- **DB-touching tests need Docker or a local `DATABASE_URL`** (`tests/conftest.py`'s `db_url` fixture). Without either they fail with an opaque testcontainers error.
- **Every new file read/write passes `encoding="utf-8"`; every write also passes `newline="\n"`** (spec §5a).
- **Lint and test before every commit:** `uv run ruff check .` then `uv run pytest -v`.
- **No migration code.** DDL is a declaration of final shape. The existing live database is handled by an out-of-band one-off (spec §9), not by anything in this repo.

---

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `app/providers/pricing.py` | Rate table + cost estimation; gains provenance, loses its "hard allowlist" role | 2, 3 |
| `app/specialists/schemas.py` | `ReviewResult.est_cost_usd` becomes optional | 3 |
| `app/formatting.py` | Renders the PR comment; omits cost fragments when unpriced | 3 |
| `app/queue/store.py` | Schema DDL (final shape) + `runtime_config` IO | 1, 3, 5 |
| `app/queue/usage_cap_config.py` | Effective caps; drops the cost cap | 5 |
| `app/queue/dispatcher.py` | Cap gate; collapses to token-only | 5 |
| `app/config.py` | `Settings`, `OPERATIONAL_KEYS` | 5, 6 |
| `app/main.py` | Lifespan; gains `LLM_PROVIDER` validation | 6 |
| `app/static/dashboard.html` | Ops dashboard; loses the "How it works" section | 8 |
| `scripts/deploy.py` | Checks + syncs; pricing becomes a WARN row | 4, 5 |
| `scripts/set_override.py` | `--model` stops refusing unpriced models | 4 |
| `scripts/pricing_check.py` | **New.** Compares `_RATES` against Groq's live catalog | 7 |
| `scripts/manual_verify_{step4,groq,vertex}.py` | Live verification; handle a `None` estimate | 3 |

---

### Task 1: Fold the schema DDL to its final shape

Spec §6d. Pure refactor — the provisioned schema is byte-for-byte equivalent, so **there is no red phase here**. The test is a *characterization* test: it locks in the current column set first, then proves the refactor preserved it. Write it, watch it pass, refactor, watch it still pass.

**Files:**
- Create: `tests/test_store_schema.py`
- Modify: `app/queue/store.py:29-83` (the `_SCHEMA` string)

**Interfaces:**
- Consumes: nothing.
- Produces: `_SCHEMA` with no `ALTER TABLE` statements. Tasks 3 and 5 edit `CREATE TABLE` bodies directly and must not reintroduce an `ALTER`.

- [ ] **Step 1: Write the characterization test**

```python
# tests/test_store_schema.py
"""The provisioned schema is declared, not migrated -- app/queue/store.py's
_SCHEMA is a CREATE TABLE declaration of the final shape, with no ALTER
statements (design spec 2026-08-18 section 6d). These tests lock the column
set so the ALTER-folding refactor cannot silently change it."""
from __future__ import annotations

from app.queue import store

EXPECTED_COLUMNS = {
    "tickets": {
        "id", "repo_full_name", "pr_number", "head_sha", "status", "provider",
        "not_before", "attempts", "comment_id", "enqueued_at", "updated_at",
        "rereview_requested", "last_reviewed_at", "cooldown_level",
        "notice_not_before", "last_error", "defer_reason",
    },
    "runtime_config": {
        "id", "provider", "updated_at", "cooldown_base_seconds",
        "cooldown_max_seconds", "cooldown_factor", "gemini_key_index",
        "groq_key_index", "vertex_key_index", "gemini_model", "groq_model",
        "vertex_model", "key_usage_token_cap", "key_usage_cost_cap_usd",
        "key_usage_reset_time_utc",
    },
    "reviews": {
        "id", "repo_full_name", "pr_number", "provider", "model", "comment_id",
        "created_at", "total_elapsed_ms", "total_tokens_in", "total_tokens_out",
        "est_cost_usd", "results", "key_index",
    },
}


def _columns(db_exec, table: str) -> set[str]:
    rows = db_exec(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
        (table,),
    )
    return {r["column_name"] for r in rows}


def test_schema_declares_every_expected_column(db, db_exec):
    for table, expected in EXPECTED_COLUMNS.items():
        assert _columns(db_exec, table) == expected, f"{table} column set changed"


def test_schema_contains_no_alter_statements():
    assert "ALTER TABLE" not in store._SCHEMA.upper(), (
        "_SCHEMA must declare the final shape via CREATE TABLE only -- an ALTER "
        "is migration code, which a fresh clone must not carry (spec section 6d)"
    )
```

- [ ] **Step 2: Run the tests**

Run: `uv run pytest tests/test_store_schema.py -v`
Expected: `test_schema_declares_every_expected_column` **PASSES** (it characterizes today's shape); `test_schema_contains_no_alter_statements` **FAILS** with the assertion message, because `_SCHEMA` still has 15 `ALTER TABLE` lines.

If `db_exec` does not accept a params tuple, check `tests/conftest.py`'s `db_exec` fixture signature and adapt the call — do not change the fixture.

- [ ] **Step 3: Fold every ALTER into its CREATE TABLE**

Replace `_SCHEMA` in `app/queue/store.py` entirely with:

```python
# Declared, not migrated: this is the final shape, provisioned in one pass on
# first boot. No ALTER statements -- a fresh clone carries no migration code
# (design spec 2026-08-18 section 6d), and an existing database is recreated
# out of band rather than migrated in place (section 9).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    repo_full_name     TEXT    NOT NULL,
    pr_number          INTEGER NOT NULL,
    head_sha           TEXT,
    status             TEXT    NOT NULL,
    provider           TEXT    NOT NULL,
    not_before         TEXT,
    attempts           INTEGER NOT NULL DEFAULT 0,
    comment_id         BIGINT,
    enqueued_at        TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL,
    rereview_requested INTEGER NOT NULL DEFAULT 0,
    last_reviewed_at   TEXT,
    cooldown_level     INTEGER NOT NULL DEFAULT 0,
    notice_not_before  TEXT,
    last_error         TEXT,
    defer_reason       TEXT,
    UNIQUE (repo_full_name, pr_number)
);
CREATE TABLE IF NOT EXISTS runtime_config (
    id                       INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    provider                 TEXT,
    updated_at               TEXT NOT NULL,
    cooldown_base_seconds    DOUBLE PRECISION,
    cooldown_max_seconds     DOUBLE PRECISION,
    cooldown_factor          DOUBLE PRECISION,
    gemini_key_index         INTEGER,
    groq_key_index           INTEGER,
    vertex_key_index         INTEGER,
    gemini_model             TEXT,
    groq_model               TEXT,
    vertex_model             TEXT,
    key_usage_token_cap      INTEGER,
    key_usage_cost_cap_usd   DOUBLE PRECISION,
    key_usage_reset_time_utc TEXT
);
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
    results            JSONB   NOT NULL,
    key_index          INTEGER
);
CREATE INDEX IF NOT EXISTS reviews_created_at_idx ON reviews (created_at DESC);
"""
```

`key_usage_cost_cap_usd` stays for now — Task 5 removes it. `est_cost_usd` stays `NOT NULL` for now — Task 3 makes it nullable. Keeping those two changes in their own tasks is what makes each independently reviewable.

- [ ] **Step 4: Run the full suite**

Run: `uv run ruff check . && uv run pytest -v`
Expected: both new tests PASS; no other test changes status. If a pre-existing test fails, the fold changed the schema — diff the column sets and fix the DDL, do not weaken the test.

- [ ] **Step 5: Commit**

```bash
git add app/queue/store.py tests/test_store_schema.py
git commit -m "refactor: declare the final schema shape, dropping 15 ALTER migrations"
```

---

### Task 2: Give every rate entry its provenance

Spec §6f (first half). No behavior change — `estimate_cost_usd` returns the same numbers.

**Files:**
- Modify: `app/providers/pricing.py`
- Test: `tests/test_pricing.py` (create if absent; append if present)

**Interfaces:**
- Consumes: nothing.
- Produces: `Rate` (a `NamedTuple` with `rate_in: float`, `rate_out: float`, `source_url: str`, `verified: str`) and `rate_for(provider: str, model: str) -> Rate | None`. Task 8 consumes both.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pricing.py
"""Rate entries carry their own provenance, so a stale rate is detectable
rather than folded into a prose comment (design spec 2026-08-18 section 6f)."""
from __future__ import annotations

from datetime import date

import pytest

from app.providers import pricing


def test_every_rate_carries_a_source_url_and_a_parseable_verified_date():
    assert pricing._RATES, "rate table must not be empty"
    for (provider, model), rate in pricing._RATES.items():
        assert rate.source_url.startswith("https://"), (
            f"{provider}/{model} has no usable source_url"
        )
        date.fromisoformat(rate.verified)  # raises ValueError if malformed


def test_rate_for_returns_the_entry_or_none():
    assert pricing.rate_for("groq", "llama-3.3-70b-versatile") is not None
    assert pricing.rate_for("groq", "no-such-model") is None


def test_estimate_cost_usd_is_unchanged_by_the_provenance_fields():
    # 1M in + 1M out at (0.59, 0.79) == 1.38
    assert pricing.estimate_cost_usd(
        "groq", "llama-3.3-70b-versatile", 1_000_000, 1_000_000
    ) == pytest.approx(1.38)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pricing.py -v`
Expected: FAIL — `AttributeError: 'tuple' object has no attribute 'source_url'`, and `AttributeError: module 'app.providers.pricing' has no attribute 'rate_for'`.

- [ ] **Step 3: Introduce `Rate` and `rate_for`**

In `app/providers/pricing.py`, replace the `_RATES` block (and its preceding comment block) with:

```python
from typing import NamedTuple


class Rate(NamedTuple):
    """One (provider, model) price, with the provenance needed to tell whether
    it is still true. ``verified`` is an ISO date; ``source_url`` is where the
    number came from and where to re-check it. scripts/pricing_check.py reads
    both."""

    rate_in: float   # USD per 1M input tokens
    rate_out: float  # USD per 1M output tokens
    source_url: str
    verified: str    # ISO date, YYYY-MM-DD


_GROQ_PRICING = "https://groq.com/pricing"
_GEMINI_PRICING = "https://ai.google.dev/gemini-api/docs/pricing"
_VERTEX_PRICING = "https://cloud.google.com/vertex-ai/generative-ai/pricing"

# Rates are representative (see cost.md). Vertex and AI-Studio differ in the
# auth path, not in what a token costs, which is why the same model appears
# under both provider keys -- estimate_cost_usd is called with the ACTIVE
# provider name. vertex/gemini-2.5-flash exists because gemini-flash-latest is
# not a Vertex publisher model for this project (confirmed live 2026-08-14,
# see ISSUES.md).
_RATES: dict[tuple[str, str], Rate] = {
    ("gemini", "gemini-flash-latest"): Rate(0.30, 2.50, _GEMINI_PRICING, "2026-07-23"),
    ("vertex", "gemini-flash-latest"): Rate(0.30, 2.50, _VERTEX_PRICING, "2026-07-23"),
    ("vertex", "gemini-2.5-flash"): Rate(0.30, 2.50, _VERTEX_PRICING, "2026-08-14"),
    ("groq", "llama-3.3-70b-versatile"): Rate(0.59, 0.79, _GROQ_PRICING, "2026-07-23"),
}


def rate_for(provider: str, model: str) -> Rate | None:
    """The rate entry for (provider, model), or None when unpriced."""
    return _RATES.get((provider, model))
```

Then update `estimate_cost_usd`'s arithmetic to use the named fields:

```python
    rate_in, rate_out = rates.rate_in, rates.rate_out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_pricing.py -v && uv run pytest -v`
Expected: all PASS. `is_known` and `models_for` are untouched and their existing callers keep working.

- [ ] **Step 5: Commit**

```bash
git add app/providers/pricing.py tests/test_pricing.py
git commit -m "feat: give each pricing entry a source URL and verified date"
```

---

### Task 3: Make pricing optional end-to-end

Spec §6a. This is the task that stops an unpriced model from being a hard block.

**Files:**
- Modify: `app/providers/pricing.py` (`estimate_cost_usd`)
- Modify: `app/specialists/schemas.py:62`
- Modify: `app/formatting.py:117` and `:129`
- Modify: `app/queue/store.py` (`reviews.est_cost_usd` → nullable)
- Modify: `scripts/manual_verify_step4.py`, `scripts/manual_verify_groq.py`, `scripts/manual_verify_vertex.py`
- Test: `tests/test_pricing.py`, `tests/test_formatting.py`, `tests/test_store_schema.py`

**Interfaces:**
- Consumes: `Rate`, `rate_for` (Task 2).
- Produces: `estimate_cost_usd(...) -> float | None` and `ReviewResult.est_cost_usd: float | None`. Task 4's `check_pricing` and Task 7 rely on unpriced no longer being fatal.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_pricing.py`:

```python
def test_estimate_cost_usd_returns_none_for_an_unpriced_model():
    """An unpriced model must not raise: it used to KeyError inside
    orchestrator.run_review AFTER all three specialists had already made real,
    paid calls (design spec 2026-08-18 section 6a)."""
    assert pricing.estimate_cost_usd("groq", "llama-3.1-8b-instant", 100, 100) is None
```

Append to `tests/test_formatting.py`:

```python
def test_comment_omits_the_cost_when_the_model_is_unpriced():
    result = _review_result(est_cost_usd=None)  # see the module's existing helper
    body = formatting.format_comment(result)
    assert "$" not in body
    assert "tok in" in body and "tok out" in body
    assert "provider:" in body


def test_comment_still_shows_the_cost_when_the_model_is_priced():
    body = formatting.format_comment(_review_result(est_cost_usd=0.0004))
    assert "~$0.0004" in body
    assert "est. $0.0004" in body
```

If `tests/test_formatting.py` has no `_review_result` helper, build the `ReviewResult` inline the same way the neighbouring tests in that file do — match their style rather than inventing a new fixture.

Append to `tests/test_store_schema.py`:

```python
def test_est_cost_usd_is_nullable(db, db_exec):
    rows = db_exec(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'reviews' AND column_name = 'est_cost_usd'"
    )
    assert rows[0]["is_nullable"] == "YES", (
        "an unpriced review records NULL, not 0.0 -- 0.0 would corrupt the "
        "dashboard's SUM(est_cost_usd) aggregate"
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_pricing.py tests/test_formatting.py tests/test_store_schema.py -v`
Expected: FAIL — `KeyError: "No pricing entry for..."`; a `ValidationError` on `est_cost_usd=None`; `is_nullable == 'NO'`.

- [ ] **Step 3: Make each layer accept None**

`app/providers/pricing.py`:

```python
def estimate_cost_usd(
    provider: str, model: str, tokens_in: int, tokens_out: int
) -> float | None:
    """Estimated USD cost, or None when (provider, model) has no rate entry.

    Returning None rather than raising is deliberate. This is called from
    app/orchestrator.py AFTER all three specialists have already made real,
    paid calls, so a KeyError here threw away completed, paid work. Pricing is
    a nice-to-have on the comment, not a gate on which models may run --
    scripts/deploy.py reports an unpriced model as a WARN row instead
    (design spec 2026-08-18 sections 6a and 6b).
    """
    rates = _RATES.get((provider, model))
    if rates is None:
        return None

    return (tokens_in / 1_000_000) * rates.rate_in + (tokens_out / 1_000_000) * rates.rate_out
```

`app/specialists/schemas.py:62`:

```python
    est_cost_usd: float | None = None
```

`app/formatting.py` — replace the `cost_str` line and the two render sites:

```python
    cost_str = f" · ~${result.est_cost_usd:.4f}" if result.est_cost_usd is not None else ""
    cost_footer = (
        f"est. ${result.est_cost_usd:.4f} · " if result.est_cost_usd is not None else ""
    )

    header = (
        f"## 🤖 Automated Code Review — PR #{result.pr_number}\n"
        f"_{n} {plural} · {result.model} ({result.provider}) · {runtime_s:.1f}s{cost_str}_\n"
    )
```

and the footer:

```python
    footer = (
        "\n---\n"
        f"<sub>Runtime {runtime_s:.1f}s · {result.total_tokens_in:,} tok in / "
        f"{result.total_tokens_out:,} tok out · {cost_footer}"
        f"provider: {result.provider}</sub>\n"
    )
```

`app/queue/store.py` — in the `reviews` CREATE TABLE from Task 1, change:

```sql
    est_cost_usd       DOUBLE PRECISION,  -- NULL when the model has no rate entry
```

Just drop `NOT NULL`. SQL `--` comments are legal inside the `_SCHEMA` string, so the note lives on the column itself.

In the three `scripts/manual_verify_*.py` files, guard the print. Each has a line formatting `estimate_cost_usd(...)`; wrap it:

```python
cost = estimate_cost_usd(provider, model, tokens_in, tokens_out)
print(f"est. cost: ${cost:.6f}" if cost is not None else "est. cost: n/a (unpriced model)")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run ruff check . && uv run pytest -v`
Expected: all PASS. Pay attention to `tests/test_dispatcher.py` and any dashboard test that asserts on a cost string — a genuine failure there means a call site still assumes a float.

- [ ] **Step 5: Commit**

```bash
git add app/providers/pricing.py app/specialists/schemas.py app/formatting.py \
        app/queue/store.py scripts/manual_verify_step4.py scripts/manual_verify_groq.py \
        scripts/manual_verify_vertex.py tests/
git commit -m "feat: make pricing optional -- an unpriced model runs without a cost estimate"
```

---

### Task 4: Turn the pricing refusals into warnings

Spec §6b. `check_config` cannot be both FAIL and WARN, so unpriced-model reporting moves to its own check row.

**Files:**
- Modify: `scripts/deploy.py` — `CheckResult` (status literal), `check_config` (`:215-270`), `render_report` (`:766`), `run_checks` (`:1242`), `sync_env` (`:1113-1122`)
- Modify: `scripts/set_override.py:279-285`
- Test: `tests/test_deploy_script.py`, `tests/test_set_override_script.py`

**Interfaces:**
- Consumes: `pricing.is_known`, `pricing.models_for` (unchanged).
- Produces: `check_pricing() -> CheckResult` with status `"WARN"` or `"PASS"`, registered in `run_checks` after `config`. `CheckResult.status` gains `"WARN"`. A WARN never affects the exit code.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_deploy_script.py (append)
def test_unpriced_model_warns_and_does_not_fail_the_run(monkeypatch):
    """An unpriced model is a missing nice-to-have, not a blocker: the review
    still runs, it just carries no cost estimate (spec section 6b)."""
    monkeypatch.setattr(settings, "groq_model", "llama-3.1-8b-instant")
    result = deploy.check_pricing()
    assert result.status == "WARN"
    assert "llama-3.1-8b-instant" in result.detail
    assert "GROQ_MODEL" in result.detail


def test_pricing_check_passes_when_every_model_is_priced(monkeypatch):
    monkeypatch.setattr(settings, "groq_model", "llama-3.3-70b-versatile")
    assert deploy.check_pricing().status == "PASS"


def test_a_warn_row_does_not_count_as_a_failure():
    report = deploy.render_report([
        deploy.CheckResult("config", "PASS"),
        deploy.CheckResult("pricing", "WARN", "GROQ_MODEL='x' has no pricing-table entry"),
    ])
    assert "1 warning" in report
    assert "failed" not in report


def test_sync_env_no_longer_refuses_an_unpriced_model(monkeypatch):
    """The old exit-2 existed only because estimate_cost_usd raised. It does
    not any more (spec section 6a), so the guard is now a warning.

    Asserted against the source because sync_env's other pre-push guards make
    a behavioural test require mocking the whole Render API surface. The
    matched fragment is the f-string literal unique to the removed refusal."""
    import inspect

    monkeypatch.setattr(settings, "groq_model", "llama-3.1-8b-instant")
    assert deploy._unpriced_models(), "precondition: the model must be unpriced"
    source = inspect.getsource(deploy.sync_env)
    assert "refusing to sync: {model_var}" not in source
    assert "warning: {model_var}" in source
```

```python
# tests/test_set_override_script.py (append)
def test_model_override_warns_instead_of_refusing_an_unpriced_model(capsys):
    exit_code = set_override.main(["groq", "--model", "llama-3.1-8b-instant", "--no-activate"])
    assert exit_code == 0
    assert "no pricing-table entry" in capsys.readouterr().err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_deploy_script.py -k "pricing or warn or unpriced" tests/test_set_override_script.py -k "unpriced" -v`
Expected: FAIL — `AttributeError: module has no attribute 'check_pricing'`, and the set_override case exits 2.

- [ ] **Step 3: Add WARN, extract `check_pricing`, drop both refusals**

In `scripts/deploy.py`, widen the status literal:

```python
    status: Literal["PASS", "WARN", "FAIL", "SKIPPED"]
```

Add, immediately after `check_config`:

```python
def check_pricing() -> CheckResult:
    """Whether every provider's effective model has a rate-table entry.

    WARN, never FAIL: an unpriced model runs fine, it simply produces no cost
    estimate on the comment (app/providers/pricing.py::estimate_cost_usd
    returns None). This used to be folded into check_config as a FAIL, back
    when an unpriced model crashed the review after three paid calls
    (design spec 2026-08-18 section 6b).
    """
    overrides: dict[str, str | None] = {}
    if settings.database_url:
        try:
            overrides = _resolved_model_overrides()
        except Exception:  # noqa: BLE001
            overrides = {}
    lines = [
        f"{model_var}={model!r} has no pricing-table entry for {provider} "
        f"(known: {known}) -- reviews run, with no cost estimate"
        for provider, model_var, model, known in _unpriced_models(overrides)
    ]
    if lines:
        return CheckResult("pricing", "WARN", "\n".join(lines))
    return CheckResult("pricing", "PASS", "")
```

In `check_config`, delete the whole `overrides` / `for provider, model_var, model, known in _unpriced_models(overrides):` block (`:238-262`) along with its preceding comment — `check_pricing` now owns it.

In `render_report`, count warnings and mention them:

```python
    failed = sum(1 for r in results if r.status == "FAIL")
    warned = sum(1 for r in results if r.status == "WARN")
    skipped = sum(1 for r in results if r.status == "SKIPPED")
    lines.append("")
    parts = []
    if failed:
        parts.append(f"{failed} failed")
    if warned:
        parts.append(f"{warned} warning" + ("s" if warned != 1 else ""))
    if skipped:
        parts.append(f"{skipped} skipped")
    if failed:
        lines.append(", ".join(parts) + f" -- see {_README_ANCHOR}")
    elif parts:
        lines.append("all checks passed, " + ", ".join(parts))
    else:
        lines.append("all checks passed")
```

Register it in `run_checks`, immediately after the `config` row, using the same `_safe(...)` wrapper the neighbouring checks use.

In `sync_env`, delete the `for provider, model_var, model, known in _unpriced_models():` refusal block (`:1113-1122`) and its comment, replacing it with a non-blocking warning:

```python
    for provider, model_var, model, known in _unpriced_models():
        print(
            f"warning: {model_var}={model!r} has no pricing-table entry for {provider} "
            f"(known: {known}); reviews will run without a cost estimate",
            file=sys.stderr,
        )
```

In `scripts/set_override.py:279-285`, change the refusal to a warning: drop the `and not args.force` condition and the non-zero return, keep the message, and prefix it `warning:`. Leave `--force` itself alone — it still governs the live-credential check.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run ruff check . && uv run pytest -v`
Expected: all PASS. Existing tests asserting `check_config` FAILs on an unpriced model must now be updated to call `check_pricing` — update them, that is the intended behavior change.

- [ ] **Step 5: Commit**

```bash
git add scripts/deploy.py scripts/set_override.py tests/
git commit -m "feat: report an unpriced model as a warning instead of blocking the deploy"
```

---

### Task 5: Remove `KEY_USAGE_COST_CAP_USD`

Spec §6c. A spend cap resting on rates the code itself calls "representative" can silently fail open; the token cap is exact and already wins outright.

**Files:**
- Modify: `app/config.py:25` (`OPERATIONAL_KEYS`), `:135` (the field)
- Modify: `app/queue/usage_cap_config.py` (whole module — trio becomes a pair)
- Modify: `app/queue/dispatcher.py:245-258`
- Modify: `app/queue/store.py` — `runtime_config` DDL, `get_usage_cap_overrides`, `set_usage_cap_override`
- Modify: `scripts/deploy.py:99` (`_DB_SYNCED_OPERATIONAL_KEYS`), `:964` (`_DB_SYNCED_COLUMNS`), `:1012` (`sync_config_db`)
- Modify: `.env.config.example:54`
- Test: `tests/test_config.py`, `tests/test_usage_cap_config.py`, `tests/test_dispatcher.py`, `tests/test_deploy_script.py`, `tests/test_store_schema.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `usage_cap_config.effective_caps() -> tuple[int | None, time]` (was a 3-tuple), `store.get_usage_cap_overrides() -> tuple[int | None, str | None]`, `store.set_usage_cap_override(tokens, reset, now)`. Nothing later in this plan consumes these.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config.py (append)
def test_cost_cap_is_gone_entirely():
    """A dollar cap built on unverified rates is a safety control that can fail
    open -- worse than no cap (design spec 2026-08-18 section 6c)."""
    assert "KEY_USAGE_COST_CAP_USD" not in OPERATIONAL_KEYS
    assert not hasattr(Settings(_env_file=None), "key_usage_cost_cap_usd")
```

```python
# tests/test_usage_cap_config.py (append)
def test_effective_caps_returns_a_token_cap_and_a_reset_time(monkeypatch):
    monkeypatch.setattr(settings, "key_usage_token_cap", 20_000)
    tokens, reset = usage_cap_config.effective_caps()
    assert tokens == 20_000
    assert reset == settings.key_usage_reset_time_utc
```

```python
# tests/test_store_schema.py -- remove "key_usage_cost_cap_usd" from
# EXPECTED_COLUMNS["runtime_config"], then add:
def test_runtime_config_has_no_cost_cap_column(db, db_exec):
    assert "key_usage_cost_cap_usd" not in _columns(db_exec, "runtime_config")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py tests/test_usage_cap_config.py tests/test_store_schema.py -v`
Expected: FAIL on all three — the key is still in `OPERATIONAL_KEYS`, `effective_caps` still returns a 3-tuple, the column still exists.

- [ ] **Step 3: Delete the cost cap**

1. `app/config.py`: remove `"KEY_USAGE_COST_CAP_USD",` from `OPERATIONAL_KEYS` and delete the `key_usage_cost_cap_usd` field at `:135`. In the block comment above the caps (`:120-133`), delete the sentence beginning "KEY_USAGE_TOKEN_CAP WINS OUTRIGHT when both are set" — there is no second cap left to win against.
2. `app/queue/usage_cap_config.py`: delete `_cost`, drop it from `_env_caps`, `effective_caps`, `set_override_cache`, and `reset_override_cache`; both now deal in `(tokens, reset)`. Update the module docstring's "token cap / cost cap / reset time" to "token cap / reset time".
3. `app/queue/dispatcher.py:245-258`:

```python
    cap_reset_at: datetime | None = None
    token_cap, reset_time = usage_cap_config.effective_caps()
    if token_cap is not None:
        try:
            bucket_start = store.usage_bucket_start(now, reset_time)
            tokens, _cost = await asyncio.to_thread(
                store.get_key_usage,
                provider,
                key_index.active_key_index(provider),
                bucket_start.isoformat(),
            )
            if tokens >= token_cap:
                cap_reset_at = bucket_start + timedelta(hours=24)
        except Exception:  # noqa: BLE001
            logger.exception("failed to check key usage cap; proceeding without it")
            cap_reset_at = None
```

Also update the FAILS OPEN comment above it: "the safe default for a cost cap" → "the safe default for a usage cap".

4. `app/queue/store.py`: drop `key_usage_cost_cap_usd` from the `runtime_config` CREATE TABLE; drop it from `get_usage_cap_overrides`'s SELECT and return tuple, and from `set_usage_cap_override`'s signature, INSERT column list, placeholders, and ON CONFLICT assignments.
5. `scripts/deploy.py`: remove `"KEY_USAGE_COST_CAP_USD"` from the `:99` tuple and `"key_usage_cost_cap_usd"` from `_DB_SYNCED_COLUMNS`; in `sync_config_db`, delete `cost = settings.key_usage_cost_cap_usd` and remove `cost` from `wanted`.
6. `.env.config.example`: delete the `# KEY_USAGE_COST_CAP_USD=0.50` line and rewrite the surrounding comment so it describes one cap. Note in the comment that a dollar budget is expressed by dividing by the rate once and setting a token cap.

- [ ] **Step 4: Delete and simplify the obsolete tests, then run everything**

Delete `tests/test_dispatcher.py::test_cost_cap_applies_when_no_token_cap_is_set` (`:1020`) and `::test_token_cap_wins_outright_when_both_caps_are_set` (`:1040`). Simplify `::test_no_cap_configured_never_queries_usage` (`:1058`) to unset only the token cap. Remove the `key_usage_cost_cap_usd` monkeypatch at `:55` and every remaining reference in `tests/test_config.py`, `tests/test_usage_cap_config.py`, and `tests/test_deploy_script.py:48,1736,1740`.

Run: `uv run ruff check . && uv run pytest -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/ scripts/deploy.py .env.config.example tests/
git commit -m "feat: remove KEY_USAGE_COST_CAP_USD, a spend cap built on unverified rates"
```

---

### Task 6: `LLM_PROVIDER` loses its implicit default

Spec §6e. Validated in the lifespan, **not** as a pydantic required field — `app/config.py:158` builds `Settings()` at module scope, so a required field would raise at *import* and break `pytest`, `encode_credential.py`, and the future `doctor.py` before any of them could report the problem.

**Files:**
- Modify: `app/config.py:64`
- Modify: `app/main.py` (lifespan)
- Modify: `scripts/deploy.py::check_config`
- Modify: `.env.config.example:11`
- Test: `tests/test_config.py`, `tests/test_main.py` (or wherever lifespan tests live), `tests/test_deploy_script.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Settings.llm_provider` defaults to `""`. Any code branching on provider must treat `""` as unconfigured, not as a provider name.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config.py (append)
def test_llm_provider_has_no_implicit_default(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert Settings(_env_file=None).llm_provider == ""


def test_importing_config_with_llm_provider_unset_does_not_raise(monkeypatch):
    """A pydantic *required* field would raise at import, because
    app/config.py builds Settings() at module scope -- which would break
    pytest and scripts/doctor.py before either could report the problem
    (design spec 2026-08-18 section 6e). This pins that trap shut."""
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    import importlib
    import app.config
    importlib.reload(app.config)  # must not raise
```

```python
# tests/test_main.py (append -- match the file's existing lifespan-test style)
@pytest.mark.asyncio
async def test_lifespan_refuses_to_start_without_llm_provider(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "")
    monkeypatch.setattr(settings, "github_webhook_secret", "s3cret")
    with pytest.raises(RuntimeError) as exc:
        async with main.lifespan(main.app):
            pass
    message = str(exc.value)
    assert "LLM_PROVIDER" in message
    for provider in ("gemini", "groq", "vertex"):
        assert provider in message
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -k llm_provider tests/test_main.py -k lifespan -v`
Expected: FAIL — the default is still `"gemini"`, and the lifespan starts happily with an empty provider.

- [ ] **Step 3: Remove the default and validate at startup**

`app/config.py:64`:

```python
    # No implicit default: guessing a provider means silently running (and
    # billing) against one the operator never chose. Validated in
    # app/main.py's lifespan rather than as a pydantic required field --
    # `settings = Settings()` below is module-scope, so a required field would
    # raise at IMPORT, breaking pytest and scripts/doctor.py before either
    # could report the problem (design spec 2026-08-18 section 6e).
    llm_provider: str = ""
```

`app/main.py`, as the first statement in `lifespan`, before the webhook-secret check:

```python
    if settings.llm_provider not in registry.PROVIDERS:
        raise RuntimeError(
            f"LLM_PROVIDER={settings.llm_provider!r} is not a supported provider "
            f"-- refusing to start. Set it in .env.config to one of: "
            f"{', '.join(sorted(registry.PROVIDERS))}."
        )
```

`app/main.py` does not import `registry` today (its imports stop at
`app.queue` / `app.webhook`), so add `from app.providers import registry`
alongside them. `registry.PROVIDERS` (`app/providers/registry.py:14`) is the
canonical provider map — `scripts/deploy.py`'s `_PROVIDERS` is documented in
that module as the copy that was moved out of it, so do not use the private
one from `app/`.

`scripts/deploy.py::check_config`: the existing `entry = _PROVIDERS.get(settings.llm_provider)` branch already reports an unsupported provider. Extend its message so an *empty* value reads as unset rather than unsupported:

```python
    if not settings.llm_provider:
        problems.append(
            "LLM_PROVIDER is unset -- there is no default. Set it in .env.config "
            f"to one of: {', '.join(sorted(_PROVIDERS))}"
        )
    elif (entry := _PROVIDERS.get(settings.llm_provider)) is None:
        ...
```

`.env.config.example`: change `LLM_PROVIDER=gemini` to `LLM_PROVIDER=groq` with a comment noting there is no default and that Groq is the recommended starting point (free tier, no card).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run ruff check . && uv run pytest -v`
Expected: all PASS. Several existing tests construct `Settings(_env_file=None)` and may have relied on the `"gemini"` default — set `llm_provider` explicitly in those tests rather than restoring the default.

- [ ] **Step 5: Commit**

```bash
git add app/config.py app/main.py scripts/deploy.py .env.config.example tests/
git commit -m "feat: require LLM_PROVIDER explicitly, validated at startup"
```

---

### Task 7: `scripts/pricing_check.py`

Spec §6f (second half). Metadata-only — this calls a model *listing* endpoint, never a generation endpoint, so `CLAUDE.md`'s one-deliberate-live-call rule does not apply.

**Files:**
- Create: `scripts/pricing_check.py`
- Test: `tests/test_pricing_check.py`

**Interfaces:**
- Consumes: `pricing.Rate`, `pricing.rate_for`, `pricing._RATES` (Task 2).
- Produces: `compare(catalog: dict[str, tuple[float, float]]) -> list[str]` — human-readable drift lines, empty when everything matches. Stage 3's `gen_docs.py` does not depend on this.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pricing_check.py
"""Rate drift is detectable without a live call: compare() is pure, so the
network fetch stays in main() and the logic is fully testable."""
from __future__ import annotations

from scripts import pricing_check


def test_compare_reports_nothing_when_the_catalog_matches():
    assert pricing_check.compare({"llama-3.3-70b-versatile": (0.59, 0.79)}) == []


def test_compare_reports_a_drifted_rate():
    lines = pricing_check.compare({"llama-3.3-70b-versatile": (0.70, 0.79)})
    assert len(lines) == 1
    assert "llama-3.3-70b-versatile" in lines[0]
    assert "0.59" in lines[0] and "0.70" in lines[0]


def test_compare_offers_a_paste_ready_line_for_an_unpriced_model():
    lines = pricing_check.compare({"llama-3.1-8b-instant": (0.05, 0.08)})
    assert any('("groq", "llama-3.1-8b-instant"): Rate(0.05, 0.08' in ln for ln in lines)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pricing_check.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.pricing_check'`.

- [ ] **Step 3: Write the script**

```python
"""Compare app/providers/pricing.py's groq rates against Groq's live catalog.

    uv run python -m scripts.pricing_check

Groq's /openai/v1/models returns pricing.prompt / pricing.completion (USD per
token) inline -- which is where the existing groq entry came from. This is a
METADATA call, not a generation call: CLAUDE.md's one-deliberate-live-call
rule governs completions, not catalog listings.

Google publishes no equivalent endpoint, so gemini/vertex entries stay manual;
their Rate.source_url is where to re-check them by hand.

Prints paste-ready _RATES lines for anything missing or drifted, and never
prints or transmits a credential value -- the key is read from Settings and
attached as a header, never logged.
"""
from __future__ import annotations

import sys

import httpx

from app.config import settings
from app.providers import pricing

_GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"
_HTTP_TIMEOUT = 10.0


def compare(catalog: dict[str, tuple[float, float]]) -> list[str]:
    """Drift lines for groq, comparing `catalog` (model -> USD per 1M tokens)
    against the rate table. Empty when everything matches."""
    lines: list[str] = []
    for model, (rate_in, rate_out) in sorted(catalog.items()):
        known = pricing.rate_for("groq", model)
        if known is None:
            lines.append(
                f'missing: ("groq", "{model}"): '
                f'Rate({rate_in}, {rate_out}, _GROQ_PRICING, "<today>"),'
            )
        elif (known.rate_in, known.rate_out) != (rate_in, rate_out):
            lines.append(
                f"drifted: groq/{model} table says ({known.rate_in}, {known.rate_out}), "
                f"catalog says ({rate_in}, {rate_out}) "
                f"[verified {known.verified}, source {known.source_url}]"
            )
    return lines


def _fetch_groq_catalog() -> dict[str, tuple[float, float]]:
    if not settings.groq_api_key:
        raise SystemExit("GROQ_API_KEY is not set; nothing to check")
    response = httpx.get(
        _GROQ_MODELS_URL,
        headers={"Authorization": f"Bearer {settings.groq_api_key}"},
        timeout=_HTTP_TIMEOUT,
    )
    response.raise_for_status()
    catalog: dict[str, tuple[float, float]] = {}
    for entry in response.json().get("data", []):
        price = entry.get("pricing") or {}
        prompt, completion = price.get("prompt"), price.get("completion")
        if prompt is None or completion is None:
            continue
        # the endpoint reports USD per token; the table stores USD per 1M
        catalog[entry["id"]] = (float(prompt) * 1e6, float(completion) * 1e6)
    return catalog


def main() -> int:
    lines = compare(_fetch_groq_catalog())
    if not lines:
        print("pricing: groq rates match the live catalog")
        return 0
    print("pricing drift detected:")
    for line in lines:
        print(f"  {line}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run ruff check . && uv run pytest tests/test_pricing_check.py -v`
Expected: PASS. Do **not** run `main()` against the live endpoint as part of this task — the unit tests cover `compare`, and a live run is an operator action.

- [ ] **Step 5: Commit**

```bash
git add scripts/pricing_check.py tests/test_pricing_check.py
git commit -m "feat: add scripts/pricing_check.py to detect groq rate drift"
```

---

### Task 8: Remove the dashboard "How it works" section

Spec §6g. Presentation-only scaffolding, replaced later by a Pages landing-page section. Added originally by `docs/superpowers/specs/2026-08-11-how-it-works-section-design.md`.

**Files:**
- Modify: `app/static/dashboard.html`
- Test: `tests/test_dashboard.py` (append; create if absent)

**Interfaces:**
- Consumes: nothing.
- Produces: nothing. `sp_name_security` / `sp_name_performance` / `sp_name_quality` **must survive** — the reviews table maps to them at `dashboard.html:424-426`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dashboard.py (append)
from pathlib import Path

_DASHBOARD = Path(__file__).resolve().parent.parent / "app" / "static" / "dashboard.html"


def test_how_it_works_section_is_gone():
    """Presentation-only scaffolding; a polished version lives on the Pages
    landing page instead (design spec 2026-08-18 section 6g)."""
    html = _DASHBOARD.read_text(encoding="utf-8")
    for token in ("howItWorks", "hiwJumpBtn", "how-it-works", "hiw-", "hiw_"):
        assert token not in html, f"leftover How-it-works markup: {token}"


def test_specialist_name_strings_survive():
    """They are also used by the reviews table's name mapping, not only by the
    removed flow diagram."""
    html = _DASHBOARD.read_text(encoding="utf-8")
    for key in ("sp_name_security", "sp_name_performance", "sp_name_quality"):
        assert key in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_dashboard.py -k how_it_works -v`
Expected: FAIL — `leftover How-it-works markup: howItWorks`.

- [ ] **Step 3: Delete the section**

From `app/static/dashboard.html`, remove exactly:

- `:80` — the standalone `#hiwJumpBtn { margin-inline-end: 1.25rem; }` rule. **Easy to miss** — it sits among the button styles, far above the main block.
- `:186-254` — the `.how-it-works` and every `.hiw-*` CSS rule. This is the last run in the stylesheet; `:255` is `</style>`, so the whole span deletes cleanly.
- `:259` — the `<button id="hiwJumpBtn" ...>` element
- `:290-337` — the entire `<section id="howItWorks" class="how-it-works">` … `</section>`
- `:369-380` — the `hiw_*` keys in the `en` string table
- `:408-419` — the `hiw_*` keys in the `he` string table
- `:636-638` — the `hiwJumpBtn` click listener (the `getElementById` line, its arrow body, and the closing `});`)

Delete from the bottom up so earlier deletions do not shift the later line numbers. The Step 1 test's token scan is the backstop: it fails if any `hiw` reference survives anywhere in the file.

Do **not** touch `sp_name_security` / `sp_name_performance` / `sp_name_quality` at `:365-366` and `:404-405`, nor the mapping at `:424-426`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run ruff check . && uv run pytest -v`
Expected: all PASS. Load the page once to confirm nothing is visually broken: `uv run uvicorn app.main:app --port 8000` then open `http://localhost:8000/` — the stats grid and reviews table should render, with no gap or console error where the section was. (This needs `LLM_PROVIDER` set and a reachable `DATABASE_URL`; skip the visual check if neither is available locally and say so.)

- [ ] **Step 5: Commit**

```bash
git add app/static/dashboard.html tests/test_dashboard.py
git commit -m "refactor: remove the presentation-only How it works dashboard section"
```

---

### Task 9: Full-suite verification and stage close-out

Not a code change — the gate before Stage 2 begins.

- [ ] **Step 1: Run the whole suite clean**

Run: `uv run ruff check . && uv run pytest -v`
Expected: zero failures. Record the test count; it should be near the pre-stage 493 minus the deleted cost-cap tests, plus the ones added here.

- [ ] **Step 2: Confirm the config check reports the new shape**

Run: `uv run python -m scripts.deploy --health-only` against a local or deployed base URL, and separately confirm `deploy.check_pricing()` returns `PASS` for the configured models.

Expected: `pricing` appears as its own row; an unpriced model produces `WARN` and exit code 0.

- [ ] **Step 3: Report the out-of-band step, do not perform it**

Spec §9 requires dropping and recreating `tickets`, `reviews`, and `runtime_config` on the live database, with a drained queue, followed by `deploy.py --sync-config-db` and re-applying any `set_override`. **This is an operator action with data loss (review history and usage-cap accounting reset; deferred tickets abandoned).** Report that it is now required and wait for the operator — do not run it.

- [ ] **Step 4: Report completion**

Summarise: tasks completed, test count before and after, any deviation from this plan and why, and the §9 step still outstanding.

---

## Out of Scope for Stage 1

Deliberately deferred — do not start these:

- **Stage 2** — `scripts/doctor.py`, `scripts/init_env.py`, `scripts/create_github_app.py`, `.claude/commands/setup.md` (spec §4).
- **Stage 3** — the `guide/` MkDocs site, README's reduction, the OS-idiom doc fixes, `scripts/gen_docs.py`, and both CI jobs (spec §3, §5, §7).
- **README/SETUP edits.** Stage 1 changes behavior these documents describe; they are rewritten wholesale in Stage 3 rather than patched twice. The one exception is `.env.config.example`, edited in Tasks 5 and 6 because it is a config file the app reads, not prose.
