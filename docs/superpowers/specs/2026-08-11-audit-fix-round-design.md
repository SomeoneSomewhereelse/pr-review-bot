# Design: Fix Round for 2026-08-11 Full-Project Audit

**Status:** Approved
**Source:** `docs/2026-08-11-full-project-review-security-performance-quality.md`
**Scope:** All 12 findings from the security/performance/code-quality audit — 9 code changes, 3 doc-only notes.

## Overview

The audit found no high/critical issues. This design fixes every medium/low finding, adds small hardening for two info-level items, and documents (without changing) three items the audit itself judged correct-as-is.

Landing: one branch, one commit per finding (10 commits total — see Commit Plan). Testing bar: best-effort — new/updated tests only where a fix changes observable behavior; pure refactors (the provider de-dup) rely on existing adapter tests continuing to pass unchanged.

## 1. Security fixes

### 1.1 Escape LLM finding text in PR comment tables (was: Security #1, medium)

**File:** `app/formatting.py`, `_render_section` (~line 90-99)

Add a module-level helper:

```python
def _escape_cell(value: object) -> str:
    """Neutralize Markdown table syntax in LLM-generated, PR-diff-derived text."""
    text = str(value)
    return text.replace("|", "\\|").replace("`", "'").replace("\n", " ")
```

Apply it to every cell in the loop instead of the bare `str(value)`:

```python
cells.append(_escape_cell(value))
```

This is the only place LLM/diff-derived finding fields (`description`, `fix`, `issue`, `suggestion`, `type`, `category`, `file`) reach the outgoing Markdown. `_file_line` output should also pass through `_escape_cell` for consistency, since `file` is diff-derived too.

**Why this shape:** matches the existing single-purpose-function style in `formatting.py`; backtick→`'` (not escaped) because an escaped backtick still lets an attacker fake inline code spans that visually blend with real column boundaries — replacing it outright is simpler and safer than escaping.

### 1.2 Fail fast on empty webhook secret (was: Security #2, medium)

**File:** `app/main.py`, in `lifespan`

At the top of `lifespan`, before pool init:

```python
if not settings.github_webhook_secret:
    raise RuntimeError(
        "GITHUB_WEBHOOK_SECRET is unset — refusing to start (would accept any signature)."
    )
```

No new setting, no default change — `Settings.github_webhook_secret` keeps its `""` default so tests that don't need a real secret aren't forced to set one; only startup enforces non-emptiness.

### 1.3 Soften the planted secret in fixtures/bad_code/billing_report.py (was: Security #3, info)

**Files:** `fixtures/bad_code/billing_report.py`, `fixtures/demo_bulk_bad_code/billing_report_bulk.py` (identical duplicated constant)

Replace the Stripe-shaped value `"sk_live_51Hj9aQqX7ZkTmvW2nP8sR3fA6bC0dE4gH"` with an unambiguously synthetic one, e.g. `"FAKE-DEMO-KEY-fA6bC0dE4gH-DO-NOT-ROTATE"`, and soften the `# Rotated quarterly.` comment (which reads as if the key is real) to `# Synthetic demo credential — planted for the security specialist to flag; not a real key.` in both files. No test depends on the exact string (verified via grep), so this is a safe drop-in edit — the security specialist still has an obvious hardcoded-secret finding to catch, just no longer shaped exactly like a real Stripe live key.

### 1.4 Dockerfile non-root user (was: Security #4, low)

**File:** `Dockerfile`

Add before the final `CMD`:

```dockerfile
RUN useradd -m -u 1000 appuser
USER appuser
```

Placed after all `COPY`/`RUN pip install`/build steps so file ownership from build layers isn't disturbed; if any `RUN` step needs root after this point, none currently do.

### 1.5 Dashboard no-auth rationale — doc-only note (was: Security #5, info)

**File:** `docs/superpowers/specs/2026-08-11-ops-dashboard-design.md`, Purpose section — this is where the no-auth rationale actually lives (already amended once by commit d2fb0ed to mention findings-text exposure; SPEC.md only references this doc, it doesn't restate the rationale itself).

Add one sentence confirming the rationale was re-checked in the 2026-08-11 audit and still holds: the dashboard's exposure of raw findings text (file paths, line numbers, LLM-written descriptions) was already a known, deliberate tradeoff before this audit, not something the audit surfaced for the first time.

## 2. Performance fixes

### 2.1 Explicit LLM client timeout, 20s (was: Performance #1, medium)

**Files:** `app/config.py`, `app/providers/google_genai.py`, `app/providers/groq.py`, `app/providers/github_models.py`

Add to `Settings`:

```python
llm_request_timeout_seconds: float = 45.0
```

45s — well under the SDK's multi-minute default (bounding worst-case queue-stall to tens of seconds instead of minutes), while still tolerant of a genuinely slow-but-healthy provider response. A specialist whose call exceeds this renders as a real `❌ check failed` row via the existing partial-failure path (`app/orchestrator.py`) — it does not block the other two specialists, and does not affect the webhook's `202`-immediate response, which is already fully decoupled from review execution.

Pass `timeout=settings.llm_request_timeout_seconds` to each SDK client constructor:
- `genai.Client(...)` — via its `http_options`/timeout param (google-genai SDK)
- `AsyncGroq(api_key=..., max_retries=0, timeout=...)`
- `AsyncOpenAI(...)` (github_models adapter) — `timeout=...`

### 2.2 Cache provider client instances (was: Performance #2, low)

**File:** `app/providers/factory.py`

Replace per-call construction with a lazy module-level cache:

```python
_INSTANCES: dict[str, LLMProvider] = {}

def get_provider() -> LLMProvider:
    name = settings.llm_provider
    if name not in _INSTANCES:
        _INSTANCES[name] = _build(name)  # existing construction logic, renamed
    return _INSTANCES[name]
```

One instance per provider name for the process lifetime — safe because `Settings` is loaded once at import and provider adapters hold no per-call mutable state.

### 2.3 Cache dashboard.html in memory (was: Performance #3, info)

**File:** `app/dashboard.py`

Read `dashboard.html` once at module import time into a module-level `_DASHBOARD_HTML: str`, and have the route handler return that string directly instead of re-reading from disk (and dropping the `to_thread` disk-read wrapper for this path, since there's no longer any I/O to offload).

### 2.4 Polling intervals + single-serial dispatcher — doc-only note (was: Performance #4-5, info)

**File:** `SPEC.md` (near the dispatcher/polling design section)

Add a short note recording that the current polling cadence (dispatcher ~1s idle sleep, dashboard 4s client poll) and the single-ticket-serial dispatcher design were reviewed in the 2026-08-11 audit and confirmed correct as deliberate tradeoffs (free-tier scale; Trust & Safety pacing discipline per CLAUDE.md) — no code change.

## 3. Code quality fixes

### 3.1 De-duplicate `_parse` across provider adapters (was: Quality #1, medium)

**File:** `app/providers/base.py` — add:

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
```

(needs `import json` and `from pydantic import ValidationError` added to `base.py`'s imports). Each of `google_genai.py`, `groq.py`, `github_models.py` deletes its own `_parse` and imports `parse_or_none` from `app.providers.base`, calling it at the same call sites.

### 3.2 De-duplicate rate-limit translation across provider adapters (was: Quality #2, medium)

**File:** `app/providers/base.py` — add an async context manager:

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def translate_rate_limit(default: float):
    """Re-raise a 429 transport error as RateLimited; anything else propagates."""
    try:
        yield
    except Exception as exc:  # noqa: BLE001 — fail-isolation boundary, re-raised below
        rl = rate_limited_or_none(exc, now=datetime.now(timezone.utc), default=default)
        if rl is not None:
            raise rl from exc
        raise
```

Each adapter replaces its own `try/except Exception as exc: rl = ...; raise` block with:

```python
async with translate_rate_limit(default=settings.default_retry_after_seconds):
    response = await self._client....create(...)
```

Preserves identical behavior (same exception types, same re-raise semantics) — pure refactor, no test changes expected beyond the existing adapter tests continuing to pass.

### 3.3 Persist `mark_failed`'s `error` (was: Quality #3, low)

**File:** `app/queue/store.py`

`dispatcher.py:236` already calls `mark_failed(..., error=str(exc))` expecting persistence — the schema is missing the column, not the call site. Add to `_SCHEMA`:

```sql
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS last_error TEXT;
```

(idempotent, safe on both fresh and already-deployed databases — matches this file's existing `CREATE TABLE IF NOT EXISTS` style). Update `mark_failed`'s `UPDATE tickets SET ...` to also set `last_error = %s`, and drop the docstring's "not persisted" caveat.

### 3.4 Shared `KNOWN_PROVIDERS` constant (was: Quality #4, low)

**File:** `app/providers/base.py` — add:

```python
KNOWN_PROVIDERS = ("gemini", "groq", "github_models")
```

`app/dashboard.py` deletes its own `_KNOWN_PROVIDERS` and imports `KNOWN_PROVIDERS` from `app.providers.base`; `app/providers/factory.py`'s provider-name branches reference the same constant (e.g. iterating or validating against it) instead of repeating the three names as bare string literals.

## Commit plan (one branch, one commit per finding)

1. `fix(formatting): escape Markdown table syntax in LLM finding text` (§1.1)
2. `fix(config): fail startup on empty GITHUB_WEBHOOK_SECRET` (§1.2)
3. `fix(fixtures): soften planted secret to an unambiguously synthetic value` (§1.3)
4. `fix(docker): run container as non-root user` (§1.4)
5. `docs: confirm dashboard no-auth rationale still covers findings text` (§1.5)
6. `fix(providers): add explicit request timeout to all three LLM clients` (§2.1)
7. `perf(providers): cache provider client instances in factory` (§2.2)
8. `perf(dashboard): serve dashboard.html from memory instead of per-request disk read` (§2.3 + §2.4 doc note)
9. `refactor(providers): share parse_or_none and translate_rate_limit across adapters` (§3.1 + §3.2 — kept as one commit since both touch the same three files identically)
10. `fix(queue): persist mark_failed's error column; dedupe KNOWN_PROVIDERS constant` (§3.3 + §3.4 — kept as one commit, both small store/dashboard cleanups)

## Testing

Best-effort, per user decision — no blanket new-test mandate:

- §1.1 (escape): add one test asserting a finding containing `|`/newline doesn't break the rendered table structure — this is new observable behavior.
- §1.2 (fail-fast): add one test that `lifespan` raises when `github_webhook_secret` is empty — new observable behavior.
- §2.1 (timeout): no new test required — a timeout value isn't practically testable without a slow-mock harness the audit didn't ask for; covered by existing adapter tests continuing to pass with the constructor change.
- §2.2, §2.3 (caching): existing tests should pass unchanged since the changes are internal to previously-untested implementation details (factory construction, disk read); add one test only if `factory.py` currently has no test coverage at all, otherwise skip.
- §3.1, §3.2 (de-dup refactor): existing per-adapter tests must continue to pass unchanged — this is the test bar for a pure refactor.
- §3.3 (mark_failed persistence): add one test that `error` survives a `mark_failed` → re-read round trip — new observable behavior.
- §3.4 (constant de-dup): no new test — existing dashboard tests should pass unchanged.
- §1.3 (fixture secret): no new test — no test asserts the exact string (verified via grep across `scripts/` and `tests/`), so this is a content-only edit.
