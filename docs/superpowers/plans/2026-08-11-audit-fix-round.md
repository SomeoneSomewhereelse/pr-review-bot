# Audit Fix Round Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all 9 code-level findings and add 3 doc-only notes from the 2026-08-11 security/performance/code-quality audit (`docs/2026-08-11-full-project-review-security-performance-quality.md`), per the approved design (`docs/superpowers/specs/2026-08-11-audit-fix-round-design.md`).

**Architecture:** No new modules or architectural changes. Each task is a small, self-contained fix to an existing file, landed as its own commit on one branch. Two tasks touch all three provider adapters identically (timeout, de-dup) — still one task each, since the change is mechanical and identical across the three files.

**Tech Stack:** FastAPI, Pydantic v2, psycopg3, pytest/pytest-asyncio (asyncio_mode=auto — no `@pytest.mark.asyncio` needed on new async tests, though existing tests in these files use it and new tests should match the surrounding file's style).

## Global Constraints

- Ruff: `line-length = 100`, `select = ["E4", "E7", "E9", "F", "E501"]` (pyproject.toml) — keep lines ≤100 chars.
- Never log or embed a secret; never include raw exception text in a PR-facing comment (existing convention in `app/formatting.py`).
- Every broad `except Exception` in `app/` carries a `# noqa: BLE001` comment with a one-line rationale — match this when adding one.
- Postgres schema changes must be idempotent (`CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`) since `_SCHEMA` runs unconditionally on every `init_pool()` call, including against an already-deployed database.
- Testing bar for this plan: best-effort — write a test only where a task changes observable behavior; pure refactors rely on existing tests continuing to pass unchanged (per user decision during brainstorming).
- One commit per task, in the order given below (matches the design doc's commit plan).
- Run `uv run pytest <specific test file(s)>` after each task, and `uv run ruff check app/ tests/` before each commit.

---

### Task 1: Escape Markdown table syntax in LLM finding text

**Files:**
- Modify: `app/formatting.py`
- Test: `tests/test_formatting.py`

**Interfaces:**
- Produces: `_escape_cell(value: object) -> str` in `app/formatting.py`, used internally by `_render_section` and `_file_line`. Not imported anywhere else.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_formatting.py`:

```python
def test_format_comment_escapes_pipe_and_newline_in_finding_text():
    """A crafted finding (attacker-controlled via the PR diff) must not be able
    to inject extra Markdown table columns/rows via `|` or a newline."""
    result = ReviewResult(
        pr_number=99,
        provider="groq",
        model="llama-3.3-70b-versatile",
        results=[
            SpecialistResult(
                name="Security",
                status="ok",
                findings=[
                    {
                        "severity": "high",
                        "file": "app.py",
                        "line": 1,
                        "description": "Legit issue | fake col ```\n### Injected header",
                        "fix": "Escape it",
                    }
                ],
                elapsed_ms=100,
            )
        ],
        total_elapsed_ms=100,
        total_tokens_in=1,
        total_tokens_out=1,
        est_cost_usd=0.0,
    )
    body = format_comment(result)

    # The raw pipe/newline must not survive unescaped inside the table.
    assert "fake col ```\n### Injected header" not in body
    assert "\\|" in body
    # Still exactly one table: no fake extra header injected via a newline.
    assert body.count("### ") == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_formatting.py::test_format_comment_escapes_pipe_and_newline_in_finding_text -v`
Expected: FAIL — the raw injected text currently passes through unescaped, so both assertions fail (the literal string is present, and `body.count("### ")` is 2 because the injected `### Injected header` renders as a second heading).

- [ ] **Step 3: Write minimal implementation**

In `app/formatting.py`, add `_escape_cell` right after `_SECTION_CONFIG` (before `_file_line`):

```python
def _escape_cell(value: object) -> str:
    """Neutralize Markdown table syntax in LLM-generated, PR-diff-derived text."""
    text = str(value)
    return text.replace("|", "\\|").replace("`", "'").replace("\n", " ")
```

Update `_file_line` to escape the diff-derived `file` field:

```python
def _file_line(finding: dict) -> str:
    return f"`{_escape_cell(finding.get('file', '?'))}:{finding.get('line', '?')}`"
```

Update `_render_section`'s cell-building loop (replace the `cells.append(str(value))` line):

```python
    for finding in spec.findings:
        cells = []
        for key, _label, fmt in columns:
            if key == "_file_line":
                value = _file_line(finding)
            else:
                raw = finding.get(key, "")
                value = fmt(raw) if fmt else raw
            cells.append(_escape_cell(value))
        rows.append("| " + " | ".join(cells) + " |")
```

(Note: `_file_line`'s return value is already escaped internally, so wrapping it again in `_escape_cell` in the loop is safe — it only replaces the backtick-wrapping characters at that point, which aren't `|`/backtick/newline themselves. Verify this doesn't double-escape: `_file_line` returns a string like `` `app.py:1` `` — the surrounding backticks are literal characters that WILL be replaced by `_escape_cell`'s backtick→`'` rule when the outer loop calls `_escape_cell` again on `_file_line`'s output. To avoid this, do NOT re-escape the `_file_line` result in the loop — special-case it:)

```python
    for finding in spec.findings:
        cells = []
        for key, _label, fmt in columns:
            if key == "_file_line":
                cells.append(_file_line(finding))
                continue
            raw = finding.get(key, "")
            value = fmt(raw) if fmt else raw
            cells.append(_escape_cell(value))
        rows.append("| " + " | ".join(cells) + " |")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_formatting.py -v`
Expected: PASS — all tests in the file, including the new one and the pre-existing ones (e.g. `test_format_comment_renders_findings_table` still asserts `"app.py:14" in body`, which still holds since `_file_line` still renders `` `app.py:14` ``).

- [ ] **Step 5: Commit**

```bash
git add app/formatting.py tests/test_formatting.py
git commit -m "fix(formatting): escape Markdown table syntax in LLM finding text"
```

---

### Task 2: Fail startup on empty GITHUB_WEBHOOK_SECRET

**Files:**
- Modify: `app/main.py`
- Test: `tests/test_main_lifespan.py`

**Interfaces:**
- No new public functions — the check is inline in `lifespan`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_main_lifespan.py`:

```python
async def test_lifespan_fails_loudly_when_webhook_secret_is_empty(monkeypatch):
    """An empty GITHUB_WEBHOOK_SECRET makes verify_signature accept any
    signature (HMAC with an empty key) -- an effective auth bypass. Startup
    must refuse to run rather than silently degrade."""
    monkeypatch.setattr(dispatcher, "run_forever", _hang_forever)
    monkeypatch.setattr(settings, "github_app_installation_id", 12345)
    monkeypatch.setattr(settings, "github_webhook_secret", "")

    with pytest.raises(RuntimeError, match="GITHUB_WEBHOOK_SECRET"):
        async with main.lifespan(main.app):
            pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_main_lifespan.py::test_lifespan_fails_loudly_when_webhook_secret_is_empty -v`
Expected: FAIL — `lifespan` currently starts successfully with an empty secret, so `pytest.raises` never catches a `RuntimeError`.

- [ ] **Step 3: Write minimal implementation**

In `app/main.py`, add the check as the first line inside `lifespan`, before the installation-id block:

```python
@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.github_webhook_secret:
        raise RuntimeError(
            "GITHUB_WEBHOOK_SECRET is unset -- refusing to start "
            "(an empty secret would accept any webhook signature)."
        )
    if not settings.github_app_installation_id:
        ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_main_lifespan.py -v`
Expected: PASS — all tests in the file. Every other test in this file already sets real-ish settings via monkeypatch but never sets `github_webhook_secret` to a non-empty value explicitly; check `app/config.py`'s `Settings` default and the test environment's `.env`/fixtures to confirm `github_webhook_secret` is non-empty in the existing passing tests before this change — if any of them fail because `github_webhook_secret` is empty in the test environment, add `monkeypatch.setattr(settings, "github_webhook_secret", "test-secret")` to that test's setup (do this only if a test actually fails; don't add it speculatively).

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_main_lifespan.py
git commit -m "fix(config): fail startup on empty GITHUB_WEBHOOK_SECRET"
```

---

### Task 3: Soften the planted secret in the demo fixtures

**Files:**
- Modify: `fixtures/bad_code/billing_report.py`
- Modify: `fixtures/demo_bulk_bad_code/billing_report_bulk.py`

**Interfaces:** None — content-only edit, no code calls either constant by value.

- [ ] **Step 1: Confirm no test depends on the exact string**

Run: `grep -rn "sk_live_51Hj9aQqX7ZkTmvW2nP8sR3fA6bC0dE4gH" tests/ scripts/`
Expected: no output (already verified during design — re-verify here since this step has no test of its own to gate on).

- [ ] **Step 2: Edit both fixture files**

In `fixtures/bad_code/billing_report.py`, replace line 13-14:

```python
# Synthetic demo credential -- planted for the security specialist to flag;
# not a real key.
REPORTING_API_KEY = "FAKE-DEMO-KEY-fA6bC0dE4gH-DO-NOT-ROTATE"
```

In `fixtures/demo_bulk_bad_code/billing_report_bulk.py`, apply the identical replacement to its copy of the same two lines (find via `grep -n "sk_live_51Hj9aQqX7ZkTmvW2nP8sR3fA6bC0dE4gH\|Rotated quarterly" fixtures/demo_bulk_bad_code/billing_report_bulk.py` to locate the exact line numbers in that file, since they may differ from the first file).

- [ ] **Step 3: Run the full test suite to confirm nothing broke**

Run: `uv run pytest tests/ -v -k "seed or demo or fixture"`
Expected: PASS, or no tests collected (this fixture has no dedicated test — confirmed in Step 1). If any test does reference these files, read it before proceeding to confirm it doesn't assert the exact secret string.

- [ ] **Step 4: Commit**

```bash
git add fixtures/bad_code/billing_report.py fixtures/demo_bulk_bad_code/billing_report_bulk.py
git commit -m "fix(fixtures): soften planted secret to an unambiguously synthetic value"
```

---

### Task 4: Run the Docker container as a non-root user

**Files:**
- Modify: `Dockerfile`

**Interfaces:** None.

- [ ] **Step 1: Edit the Dockerfile**

Current `Dockerfile`:

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app

EXPOSE 8000

CMD ["uv", "run", "--no-dev", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Replace with:

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uv", "run", "--no-dev", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

(`chown -R appuser:appuser /app` before `USER appuser` so the app files copied while still root are readable by the new user — `uv run` needs to read `pyproject.toml`/`uv.lock`/the synced venv under `/app`.)

- [ ] **Step 2: Build the image and verify it runs as non-root**

Run: `docker build -t pr-review-engine-test . && docker run --rm pr-review-engine-test whoami`
Expected: output is `appuser`, not `root`. (This requires Docker to be available locally; if it isn't, skip the build/run verification and rely on review — note this explicitly rather than silently skipping.)

Run: `docker run --rm pr-review-engine-test uv run python -c "import app.main"` (or similar minimal import smoke check that doesn't require real env vars/DB) to confirm the app package is still importable as the new user — if this fails with a permissions error, the `chown` step didn't cover the right path.

- [ ] **Step 3: Commit**

```bash
git add Dockerfile
git commit -m "fix(docker): run container as non-root user"
```

---

### Task 5: Confirm the dashboard no-auth rationale still holds

**Files:**
- Modify: `docs/superpowers/specs/2026-08-11-ops-dashboard-design.md`

**Interfaces:** None — documentation only.

- [ ] **Step 1: Edit the Purpose section**

In `docs/superpowers/specs/2026-08-11-ops-dashboard-design.md`, the Purpose section currently ends with:

```
No auth (nothing secret is shown — no credentials, tokens, or internal URLs;
it does surface repo/PR identifiers, timing, cost, and the reviewed findings
themselves — file paths, line numbers, and LLM-written descriptions — so this
assumes either a public target repo or that the dashboard's own exposure is
acceptable for the demo). No new Python dependency and no CDN script — plain
server-rendered HTML with a small inline vanilla-JS polling loop.
```

Append one sentence directly after that paragraph:

```

**Re-checked 2026-08-11 (security audit):** the findings-text exposure noted
above was re-examined during the full-project security/performance/quality
audit and confirmed to be the same known, deliberate tradeoff recorded here
originally — not something the audit surfaced for the first time.
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-08-11-ops-dashboard-design.md
git commit -m "docs: confirm dashboard no-auth rationale still covers findings text"
```

---

### Task 6: Add an explicit request timeout to all three LLM provider clients

**Files:**
- Modify: `app/config.py`
- Modify: `app/providers/google_genai.py`
- Modify: `app/providers/groq.py`
- Modify: `app/providers/github_models.py`

**Interfaces:**
- Produces: `settings.llm_request_timeout_seconds: float` (default `45.0`), read by all three provider `__init__` methods.

- [ ] **Step 1: Add the setting**

In `app/config.py`, add near the other LLM-related settings (after `github_models_model`):

```python
    # Ceiling on a single LLM request, in seconds. The dispatcher is a single
    # serial consumer of the whole queue (app/queue/dispatcher.py) -- a hung
    # call with no timeout would stall every pending PR's review, not just
    # one, for however long the SDK's own default timeout is (several
    # minutes). 45s is well under that while still tolerating a genuinely
    # slow-but-healthy response.
    llm_request_timeout_seconds: float = 45.0
```

- [ ] **Step 2: Run existing provider tests to confirm they still pass before changing the clients**

Run: `uv run pytest tests/test_providers.py tests/test_groq_provider.py tests/test_github_models_provider.py tests/test_provider_rate_limited.py -v`
Expected: PASS (adding an unused setting doesn't change behavior yet).

- [ ] **Step 3: Wire the timeout into each client constructor**

In `app/providers/google_genai.py`, `GeminiProvider.__init__` — the google-genai SDK takes timeout in milliseconds via `http_options`:

```python
class GeminiProvider:
    """``gemini`` (AI-Studio) — the actually-live provider in this environment."""

    def __init__(self) -> None:
        self._client = genai.Client(
            api_key=settings.gemini_api_key,
            http_options=types.HttpOptions(
                timeout=int(settings.llm_request_timeout_seconds * 1000)
            ),
        )
        self._model = settings.llm_model
```

(`types` is already imported at the top of this file.)

In `app/providers/groq.py`, `GroqProvider.__init__` — the Groq SDK (httpx-based) takes timeout in seconds:

```python
    def __init__(self) -> None:
        # max_retries=0: ... (existing comment unchanged)
        self._client = AsyncGroq(
            api_key=settings.groq_api_key,
            max_retries=0,
            timeout=settings.llm_request_timeout_seconds,
        )
        self._model = settings.groq_model
```

In `app/providers/github_models.py`, `GitHubModelsProvider.__init__` — the OpenAI SDK (httpx-based) also takes timeout in seconds:

```python
    def __init__(self) -> None:
        self._client = AsyncOpenAI(
            base_url=_BASE_URL,
            api_key=settings.github_models_token,
            timeout=settings.llm_request_timeout_seconds,
        )
        self._model = settings.github_models_model
```

- [ ] **Step 4: Run the same provider tests again to confirm nothing broke**

Run: `uv run pytest tests/test_providers.py tests/test_groq_provider.py tests/test_github_models_provider.py tests/test_provider_rate_limited.py -v`
Expected: PASS — every one of these tests monkeypatches the SDK client class itself (e.g. `monkeypatch.setattr("app.providers.groq.AsyncGroq", lambda **kwargs: ...)`), so the extra `timeout=` kwarg is simply absorbed by the fake's `**kwargs` and asserted on nothing — no test changes needed.

- [ ] **Step 5: Commit**

```bash
git add app/config.py app/providers/google_genai.py app/providers/groq.py app/providers/github_models.py
git commit -m "fix(providers): add explicit request timeout to all three LLM clients"
```

---

### Task 7: Cache provider client instances in the factory

**Files:**
- Modify: `app/providers/factory.py`
- Test: `tests/test_providers.py`

**Interfaces:**
- Produces: `reset_provider_cache() -> None` in `app/providers/factory.py`, for test isolation.
- `get_provider()`'s signature and return type are unchanged (`LLMProvider`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_providers.py`, in the `# factory.py` section:

```python
def test_factory_returns_the_same_instance_on_repeated_calls(monkeypatch):
    from app.providers.factory import reset_provider_cache

    monkeypatch.setattr(settings, "llm_provider", "groq")
    reset_provider_cache()
    first = get_provider()
    second = get_provider()
    assert first is second
    reset_provider_cache()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_providers.py::test_factory_returns_the_same_instance_on_repeated_calls -v`
Expected: FAIL with `AttributeError: module 'app.providers.factory' has no attribute 'reset_provider_cache'` (or, once that's stubbed, `assert first is second` failing because each call currently constructs a fresh instance).

- [ ] **Step 3: Write minimal implementation**

Replace `app/providers/factory.py` in full:

```python
"""Provider selection by ``LLM_PROVIDER``.

Narrow on purpose: this module knows nothing about provider internals beyond
which class to instantiate.

One instance per provider name is cached for the process lifetime -- each
``complete()`` call was previously paying a fresh SDK client construction
(and its underlying HTTP client/connection) on every single specialist call.
Settings are read once at import and provider adapters hold no per-call
mutable state, so caching by name is safe.
"""

from __future__ import annotations

from app.providers.active import active_provider
from app.providers.base import LLMProvider
from app.providers.github_models import GitHubModelsProvider
from app.providers.google_genai import GeminiProvider
from app.providers.groq import GroqProvider

_instances: dict[str, LLMProvider] = {}


def _build(provider: str) -> LLMProvider:
    if provider == "gemini":
        return GeminiProvider()
    if provider == "groq":
        return GroqProvider()
    if provider == "github_models":
        return GitHubModelsProvider()

    raise ValueError(
        f"Unknown provider: {provider!r} "
        "(expected 'gemini', 'groq', or 'github_models')"
    )


def get_provider() -> LLMProvider:
    provider = active_provider()
    if provider not in _instances:
        _instances[provider] = _build(provider)
    return _instances[provider]


def reset_provider_cache() -> None:
    """Clear the cache. Test-only -- production never needs to invalidate it."""
    _instances.clear()
```

(Note: `_build` raising for an unknown provider must NOT be cached — the current code only caches on success, since the `raise` happens before `_instances[provider] = ...` is reached. Confirm this by re-reading the `get_provider` body above: the assignment line is never executed on the `_build` raise path, so `test_factory_raises_for_unknown_provider` calling `get_provider()` again afterward would retry `_build` rather than returning a cached error — matching current behavior of raising every time.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_providers.py -v`
Expected: PASS — including the new test and all pre-existing factory tests (`test_factory_selects_gemini`, `test_factory_rejects_retired_vertex_provider`, `test_factory_selects_groq`, `test_factory_selects_github_models`, `test_factory_raises_for_unknown_provider`).

- [ ] **Step 5: Add cache-reset test isolation for the whole file**

Since `_instances` is module-level state that now persists across tests in the same pytest process, add an autouse fixture at the top of `tests/test_providers.py` (after the imports, before the first test) so no factory test leaks a cached instance into another:

```python
@pytest.fixture(autouse=True)
def _reset_provider_cache():
    from app.providers.factory import reset_provider_cache

    reset_provider_cache()
    yield
    reset_provider_cache()
```

- [ ] **Step 6: Run the full test file once more**

Run: `uv run pytest tests/test_providers.py -v`
Expected: PASS, same as Step 4 (this step only adds isolation, doesn't change what's asserted).

- [ ] **Step 7: Run the full test suite to check for any other caller affected by caching**

Run: `uv run pytest tests/ -v`
Expected: PASS. (`app/specialists/base.py:55` is the only production call site of `get_provider()`, confirmed via `grep -rn "get_provider(" app/ tests/` during planning — no other test mocks the SDK client boundary and calls through `get_provider()`, so no other test file needs the reset fixture.)

- [ ] **Step 8: Commit**

```bash
git add app/providers/factory.py tests/test_providers.py
git commit -m "perf(providers): cache provider client instances in factory"
```

---

### Task 8: Serve dashboard.html from memory instead of a per-request disk read

**Files:**
- Modify: `app/dashboard.py`
- Modify: `docs/superpowers/specs/2026-08-11-ops-dashboard-design.md` (doc-only note on polling/dispatcher — bundled here per the design's commit plan)
- Modify: `SPEC.md` (doc-only note on polling/dispatcher)

**Interfaces:** `dashboard_page()`'s route signature and response type are unchanged (`HTMLResponse`).

- [ ] **Step 1: Run the existing dashboard-page test to confirm current behavior**

Run: `uv run pytest tests/test_dashboard_page.py -v`
Expected: PASS (baseline before the change).

- [ ] **Step 2: Edit app/dashboard.py**

Replace the disk-read-per-request pattern. Current:

```python
_STATIC_DIR = Path(__file__).parent / "static"
```
...
```python
@router.get("/dashboard")
async def dashboard_page() -> HTMLResponse:
    html = (_STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")
    return HTMLResponse(html)
```

New: read once at import time, drop the `asyncio` import if it's no longer used elsewhere in this file (check: `api_dashboard` still uses `asyncio.to_thread`, so keep the import).

```python
_STATIC_DIR = Path(__file__).parent / "static"
_DASHBOARD_HTML = (_STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")
```
...
```python
@router.get("/dashboard")
async def dashboard_page() -> HTMLResponse:
    return HTMLResponse(_DASHBOARD_HTML)
```

- [ ] **Step 3: Run the dashboard-page tests again**

Run: `uv run pytest tests/test_dashboard_page.py -v`
Expected: PASS, unchanged — every existing test reads `resp.text` from the live route, which still returns the same file content, now served from a module-level string instead of a fresh disk read.

- [ ] **Step 4: Add the polling/dispatcher doc note to SPEC.md**

In `SPEC.md`, immediately after the paragraph ending "...serial dispatch is anti-burst by construction." (the paragraph containing "A single serial dispatcher (`app/queue/dispatcher.py`, `run_forever`)..."), add:

```

**Re-checked 2026-08-11 (performance audit):** the current polling cadence
(dispatcher's ~1s idle sleep between `claim_next_due` attempts; the
dashboard's 4s client-side poll of `/api/dashboard`) and this single-serial
dispatch design were both reviewed during the full-project audit and
confirmed correct as deliberate tradeoffs at free-tier scale and per the
Trust & Safety pacing discipline in CLAUDE.md -- no change made.
```

- [ ] **Step 5: Run the full test suite**

Run: `uv run pytest tests/ -v`
Expected: PASS (SPEC.md edit has no test surface).

- [ ] **Step 6: Commit**

```bash
git add app/dashboard.py SPEC.md
git commit -m "perf(dashboard): serve dashboard.html from memory instead of per-request disk read"
```

---

### Task 9: Share `parse_or_none` and rate-limit translation across the three provider adapters

**Files:**
- Modify: `app/providers/base.py`
- Modify: `app/providers/google_genai.py`
- Modify: `app/providers/groq.py`
- Modify: `app/providers/github_models.py`

**Interfaces:**
- Produces: `parse_or_none(raw_text: str, schema: type[BaseModel]) -> BaseModel | None` in `app/providers/base.py`.
- Produces: `translate_rate_limit(default: float)` — an async context manager in `app/providers/base.py`, re-raising a 429 transport error as `RateLimited`, propagating everything else unchanged.
- Consumes (unchanged): `rate_limited_or_none` (already in `base.py`, now called only from inside `translate_rate_limit`).

This is a pure refactor — no behavior change, so no new tests. The existing per-adapter tests (`tests/test_providers.py`, `tests/test_groq_provider.py`, `tests/test_github_models_provider.py`, `tests/test_provider_rate_limited.py`) are the test bar: they must all still pass unchanged.

- [ ] **Step 1: Run the full provider test suite to capture the baseline**

Run: `uv run pytest tests/test_providers.py tests/test_groq_provider.py tests/test_github_models_provider.py tests/test_provider_rate_limited.py -v`
Expected: PASS (this is the "before" snapshot — same command will be re-run at the end).

- [ ] **Step 2: Add the two shared helpers to app/providers/base.py**

Add `import json` and `from contextlib import asynccontextmanager` to the top imports, and `ValidationError` to the `pydantic` import:

```python
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Protocol

from pydantic import BaseModel, ValidationError
```

Add after `rate_limited_or_none`'s definition (end of file):

```python
def parse_or_none(raw_text: str, schema: type[BaseModel]) -> BaseModel | None:
    """Best-effort JSON parse + schema validation. Never raises."""
    try:
        data = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        return None
    try:
        return schema.model_validate(data)
    except ValidationError:
        return None


@asynccontextmanager
async def translate_rate_limit(default: float):
    """Re-raise a 429 transport error as ``RateLimited``; anything else propagates.

    Wraps a single provider SDK call so every adapter shares one 429-detection
    path instead of duplicating the same try/except.
    """
    try:
        yield
    except Exception as exc:  # noqa: BLE001 -- re-raised as RateLimited or re-raised as-is below
        rl = rate_limited_or_none(exc, now=datetime.now(timezone.utc), default=default)
        if rl is not None:
            raise rl from exc
        raise
```

- [ ] **Step 3: Update app/providers/google_genai.py to use both helpers**

Remove the local `_parse` function and its `json`/`ValidationError` imports (keep `datetime`/`timezone` only if still used elsewhere in the file — they are, inside the old try/except being replaced, so check after Step 3b whether they're still referenced; if not, remove them too).

Replace the whole file's provider-call section. Before:

```python
from __future__ import annotations

import json
from datetime import datetime, timezone

from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError

from app.config import settings
from app.providers.base import LLMResponse, rate_limited_or_none


def _parse(raw_text: str, schema: type[BaseModel]) -> BaseModel | None:
    """Best-effort JSON parse + schema validation. Never raises."""
    try:
        data = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        return None
    try:
        return schema.model_validate(data)
    except ValidationError:
        return None


async def _complete(
    client: genai.Client, model: str, system: str, user: str, schema: type[BaseModel]
) -> LLMResponse:
    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_schema=schema,
    )
    try:
        response = await client.aio.models.generate_content(
            model=model, contents=user, config=config
        )
    # re-raised unless it's a 429
    except Exception as exc:  # noqa: BLE001
        rl = rate_limited_or_none(
            exc, now=datetime.now(timezone.utc), default=settings.default_retry_after_seconds
        )
        if rl is not None:
            raise rl from exc
        raise

    raw_text = response.text or ""
    usage = response.usage_metadata
    tokens_in = (usage.prompt_token_count or 0) if usage else 0
    tokens_out = (usage.candidates_token_count or 0) if usage else 0

    return LLMResponse(
        raw_text=raw_text,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        parsed=_parse(raw_text, schema),
    )
```

After:

```python
from __future__ import annotations

from google import genai
from google.genai import types
from pydantic import BaseModel

from app.config import settings
from app.providers.base import LLMResponse, parse_or_none, translate_rate_limit


async def _complete(
    client: genai.Client, model: str, system: str, user: str, schema: type[BaseModel]
) -> LLMResponse:
    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_schema=schema,
    )
    async with translate_rate_limit(default=settings.default_retry_after_seconds):
        response = await client.aio.models.generate_content(
            model=model, contents=user, config=config
        )

    raw_text = response.text or ""
    usage = response.usage_metadata
    tokens_in = (usage.prompt_token_count or 0) if usage else 0
    tokens_out = (usage.candidates_token_count or 0) if usage else 0

    return LLMResponse(
        raw_text=raw_text,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        parsed=parse_or_none(raw_text, schema),
    )
```

(`GeminiProvider.__init__`, below `_complete`, is untouched by this task — it already has its timeout wiring from Task 6.)

- [ ] **Step 4: Run the gemini-provider tests**

Run: `uv run pytest tests/test_providers.py -v`
Expected: PASS — `test_gemini_provider_parses_valid_structured_output`, `test_provider_returns_none_parsed_on_malformed_json`, `test_provider_returns_none_parsed_on_off_schema_json` all still pass since `parse_or_none`'s behavior is byte-identical to the old `_parse`.

- [ ] **Step 5: Update app/providers/groq.py identically**

Before:

```python
from __future__ import annotations

import json
from datetime import datetime, timezone

from groq import AsyncGroq
from pydantic import BaseModel, ValidationError

from app.config import settings
from app.providers.base import LLMResponse, rate_limited_or_none


def _parse(raw_text: str, schema: type[BaseModel]) -> BaseModel | None:
    """Best-effort JSON parse + schema validation. Never raises."""
    try:
        data = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        return None
    try:
        return schema.model_validate(data)
    except ValidationError:
        return None
```

After (keep `_schema_system_prompt` and everything else in the file unchanged):

```python
from __future__ import annotations

import json

from groq import AsyncGroq
from pydantic import BaseModel

from app.config import settings
from app.providers.base import LLMResponse, parse_or_none, translate_rate_limit
```

(`json` is still needed here for `_schema_system_prompt`'s `json.dumps` call — keep the import.)

Replace `GroqProvider.complete`'s body. Before:

```python
    async def complete(self, system: str, user: str, schema: type[BaseModel]) -> LLMResponse:
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _schema_system_prompt(system, schema)},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
            )
        # re-raised unless it's a 429
        except Exception as exc:  # noqa: BLE001
            rl = rate_limited_or_none(
                exc, now=datetime.now(timezone.utc), default=settings.default_retry_after_seconds
            )
            if rl is not None:
                raise rl from exc
            raise

        raw_text = response.choices[0].message.content or ""
        usage = response.usage
        tokens_in = (usage.prompt_tokens or 0) if usage else 0
        tokens_out = (usage.completion_tokens or 0) if usage else 0

        return LLMResponse(
            raw_text=raw_text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            parsed=_parse(raw_text, schema),
        )
```

After:

```python
    async def complete(self, system: str, user: str, schema: type[BaseModel]) -> LLMResponse:
        async with translate_rate_limit(default=settings.default_retry_after_seconds):
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _schema_system_prompt(system, schema)},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
            )

        raw_text = response.choices[0].message.content or ""
        usage = response.usage
        tokens_in = (usage.prompt_tokens or 0) if usage else 0
        tokens_out = (usage.completion_tokens or 0) if usage else 0

        return LLMResponse(
            raw_text=raw_text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            parsed=parse_or_none(raw_text, schema),
        )
```

- [ ] **Step 6: Run the groq-provider and rate-limit tests**

Run: `uv run pytest tests/test_groq_provider.py tests/test_provider_rate_limited.py -v`
Expected: PASS — including `test_groq_429_with_header_raises_rate_limited`, `test_groq_429_without_header_uses_default`, and `test_groq_non_429_error_propagates_unchanged`, since `translate_rate_limit` reproduces the exact same re-raise/propagate logic as the removed inline block.

- [ ] **Step 7: Update app/providers/github_models.py identically**

Before:

```python
from __future__ import annotations

import json
from datetime import datetime, timezone

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app.config import settings
from app.providers.base import LLMResponse, rate_limited_or_none

_BASE_URL = "https://models.github.ai/inference"


def _parse(raw_text: str, schema: type[BaseModel]) -> BaseModel | None:
    """Best-effort JSON parse + schema validation. Never raises."""
    try:
        data = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        return None
    try:
        return schema.model_validate(data)
    except ValidationError:
        return None
```

After (keep `_add_additional_properties_false` and `_response_format` unchanged; `json` is still needed for `model_json_schema`/`_response_format` internals — check: `_response_format` calls `schema.model_json_schema()`, not `json.dumps`, so confirm whether `json` is used anywhere else in this file before deciding to keep or drop the import; if unused after removing `_parse`, drop it):

```python
from __future__ import annotations

from openai import AsyncOpenAI
from pydantic import BaseModel

from app.config import settings
from app.providers.base import LLMResponse, parse_or_none, translate_rate_limit

_BASE_URL = "https://models.github.ai/inference"
```

Replace `GitHubModelsProvider.complete`'s body. Before:

```python
    async def complete(self, system: str, user: str, schema: type[BaseModel]) -> LLMResponse:
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format=_response_format(schema),
            )
        # re-raised unless it's a 429
        except Exception as exc:  # noqa: BLE001
            rl = rate_limited_or_none(
                exc, now=datetime.now(timezone.utc), default=settings.default_retry_after_seconds
            )
            if rl is not None:
                raise rl from exc
            raise

        raw_text = response.choices[0].message.content or ""
        usage = response.usage
        tokens_in = (usage.prompt_tokens or 0) if usage else 0
        tokens_out = (usage.completion_tokens or 0) if usage else 0

        return LLMResponse(
            raw_text=raw_text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            parsed=_parse(raw_text, schema),
        )
```

After:

```python
    async def complete(self, system: str, user: str, schema: type[BaseModel]) -> LLMResponse:
        async with translate_rate_limit(default=settings.default_retry_after_seconds):
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format=_response_format(schema),
            )

        raw_text = response.choices[0].message.content or ""
        usage = response.usage
        tokens_in = (usage.prompt_tokens or 0) if usage else 0
        tokens_out = (usage.completion_tokens or 0) if usage else 0

        return LLMResponse(
            raw_text=raw_text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            parsed=parse_or_none(raw_text, schema),
        )
```

- [ ] **Step 8: Run the github_models-provider tests**

Run: `uv run pytest tests/test_github_models_provider.py -v`
Expected: PASS, all 5 tests including `test_response_format_sets_additional_properties_false_on_nested_defs` (untouched — `_response_format`/`_add_additional_properties_false` weren't modified).

- [ ] **Step 9: Run the full provider test suite once more (matches Step 1's baseline)**

Run: `uv run pytest tests/test_providers.py tests/test_groq_provider.py tests/test_github_models_provider.py tests/test_provider_rate_limited.py -v`
Expected: PASS — identical pass/fail set to Step 1's baseline run.

- [ ] **Step 10: Ruff check for now-unused imports**

Run: `uv run ruff check app/providers/google_genai.py app/providers/groq.py app/providers/github_models.py`
Expected: no `F401` (unused import) errors. If `datetime`/`timezone` in `google_genai.py`, or `json` in `github_models.py`, show as unused, remove them.

- [ ] **Step 11: Run the full test suite**

Run: `uv run pytest tests/ -v`
Expected: PASS.

- [ ] **Step 12: Commit**

```bash
git add app/providers/base.py app/providers/google_genai.py app/providers/groq.py app/providers/github_models.py
git commit -m "refactor(providers): share parse_or_none and translate_rate_limit across adapters"
```

---

### Task 10: Persist `mark_failed`'s error; de-duplicate the `KNOWN_PROVIDERS` list

**Files:**
- Modify: `app/queue/store.py`
- Modify: `app/providers/base.py`
- Modify: `app/dashboard.py`
- Test: `tests/test_queue_store.py`

**Interfaces:**
- Produces: `KNOWN_PROVIDERS: tuple[str, ...]` in `app/providers/base.py` — `("gemini", "groq", "github_models")`.
- `mark_failed(ticket_id: int, now: str, error: str | None = None) -> None`'s signature is unchanged; its persisted effect changes (now actually stores `error`).
- `Ticket` dataclass in `app/queue/store.py` gains a `last_error: str | None` field — every place that constructs a `Ticket` via `_row_to_ticket(row)` (i.e. every `SELECT * FROM tickets` call site) must still work since `_row_to_ticket` does `Ticket(**row)` and the new column will appear in `row` automatically once added to the schema.

- [ ] **Step 1: Write the failing test for error persistence**

Add to `tests/test_queue_store.py`, right after the existing `test_mark_failed_sets_status_failed`:

```python
def test_mark_failed_persists_the_error_message():
    tid = _enqueue()
    store.claim_next_due(now=T0)
    store.mark_failed(tid, now=T1, error="boom: connection reset")
    t = store.get_ticket(tid)
    assert t.last_error == "boom: connection reset"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_queue_store.py::test_mark_failed_persists_the_error_message -v`
Expected: FAIL with `AttributeError: 'Ticket' object has no attribute 'last_error'` (the schema/dataclass don't have the column yet).

- [ ] **Step 3: Add the column and dataclass field**

In `app/queue/store.py`, add to `_SCHEMA` right after the `tickets` table's closing `);` (before `CREATE TABLE IF NOT EXISTS runtime_config`):

```sql
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS last_error TEXT;
```

Add `last_error: str | None` to the `Ticket` dataclass, after `notice_not_before`:

```python
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
    rereview_requested: int
    last_reviewed_at: str | None
    cooldown_level: int
    notice_not_before: str | None
    last_error: str | None
```

Update `mark_failed` to persist `error` and drop the stale docstring caveat:

```python
def mark_failed(ticket_id: int, now: str, error: str | None = None) -> None:
    """Mark a ticket as failed after a non-rate-limit exception from attempt_review.

    A push to a 'failed' (or 'done') ticket is handled by
    ``enqueue_or_update``'s terminal-state branch: it calls
    ``_due_after_cooldown`` and re-arms the ticket to 'pending' (cooldown
    elapsed, or no prior successful review) or 'deferred' (still cooling down
    from the last completed review), escalating/resetting ``cooldown_level``
    per the escalation policy and resetting ``attempts`` to 0 either way.
    """
    with _require_pool().connection() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'failed', last_error = %s, updated_at = %s WHERE id = %s",
            (error, now, ticket_id),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_queue_store.py -v`
Expected: PASS — including the new test and `test_mark_failed_sets_status_failed` (unaffected — it doesn't check `last_error`).

Note: `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` runs against the same test-Postgres database across the whole test session (per the `db` fixture referenced elsewhere in this test suite) — confirm via `uv run pytest tests/ -v` at the end of this task that no other test's `Ticket` construction (e.g. via `enqueue_or_update`'s `RETURNING id` + a later `get_ticket`) breaks from the new column; `_row_to_ticket`'s `Ticket(**row)` unpacking means every `SELECT *` naturally picks up `last_error` as `None` for tickets inserted before this change (or in the same test run, since `INSERT INTO tickets` doesn't list `last_error` and it has no `NOT NULL` constraint, so it defaults to `NULL`/`None`).

- [ ] **Step 5: Add the shared KNOWN_PROVIDERS constant**

In `app/providers/base.py`, add near the top (after the `LLMProvider` Protocol definition, before `RateLimited`):

```python
KNOWN_PROVIDERS = ("gemini", "groq", "github_models")
```

- [ ] **Step 6: Update app/dashboard.py to import it instead of defining its own copy**

Before:

```python
from app.queue import dispatcher, store

logger = logging.getLogger(__name__)

router = APIRouter()

_REVIEWS_LIMIT = 50
# Kept local rather than imported from app/providers/factory.py: factory.py
# has no shared constant for this list, and the dashboard has no other
# reason to depend on it.
_KNOWN_PROVIDERS = ("gemini", "groq", "github_models")
_STATIC_DIR = Path(__file__).parent / "static"
```

After:

```python
from app.providers.base import KNOWN_PROVIDERS
from app.queue import dispatcher, store

logger = logging.getLogger(__name__)

router = APIRouter()

_REVIEWS_LIMIT = 50
_STATIC_DIR = Path(__file__).parent / "static"
_DASHBOARD_HTML = (_STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")
```

(`_DASHBOARD_HTML` here assumes Task 8 already landed first, per this plan's ordering — if executed out of order, don't duplicate the line.)

Update the one usage inside `build_dashboard_payload`:

```python
    backoff_raw = dispatcher.backoff_status()
    backoff = {provider: backoff_raw.get(provider) for provider in KNOWN_PROVIDERS}
```

- [ ] **Step 7: Update app/providers/factory.py's docstring/comment to reference the shared constant (no behavioral change — factory.py's `_build` doesn't currently loop over a provider list, so there's no code change here beyond the one-line docstring already covering "which class to instantiate")**

No code edit needed in `factory.py` for this step — `KNOWN_PROVIDERS` is now the single source of truth and `factory.py`'s `if provider == "gemini": ...` branches don't need to reference it to stay correct. Skip any speculative refactor of `_build` into a loop over `KNOWN_PROVIDERS` — that would change `_build`'s error message behavior (currently a static string naming all three) for no benefit; leave `_build` as-is.

- [ ] **Step 8: Run the dashboard tests**

Run: `uv run pytest tests/test_dashboard_api.py tests/test_dashboard_page.py -v`
Expected: PASS — `test_empty_state_shape` and `test_includes_a_recorded_review_and_active_backoff` both assert on `body["queue"]["backoff"]` keyed by `"gemini"`/`"groq"`/`"github_models"`, which is unchanged since `KNOWN_PROVIDERS` has the identical three values in the identical order.

- [ ] **Step 9: Run the full test suite**

Run: `uv run pytest tests/ -v`
Expected: PASS, all tests.

- [ ] **Step 10: Ruff check**

Run: `uv run ruff check app/`
Expected: clean.

- [ ] **Step 11: Commit**

```bash
git add app/queue/store.py app/providers/base.py app/dashboard.py tests/test_queue_store.py
git commit -m "fix(queue): persist mark_failed's error column; dedupe KNOWN_PROVIDERS constant"
```

---

## Final verification

- [ ] Run the full suite once more end to end: `uv run pytest tests/ -v`
- [ ] Run ruff across the whole tree: `uv run ruff check app/ tests/`
- [ ] Confirm 10 commits exist on the branch in the order above: `git log --oneline -10`
