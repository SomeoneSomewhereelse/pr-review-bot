# Onboarding Wizard: Shell + Render Key Frame Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a new, separate `onboarding/` FastAPI service — a stateless credential relay — with a single-page accordion wizard whose first (and for now, only functional) frame lets a visitor paste a Render API key, validates it live against Render's API, and unlocks the next frame. The wizard locks a completed frame behind an explicit "Change" control, works well on mobile, and offers the same light/dark/system theme and English/Hebrew (RTL) language controls as `app/static/dashboard.html`.

**Architecture:** `onboarding/` is a sibling top-level directory to `app/`, its own FastAPI app with no database and no session store. The wizard page is one self-contained static HTML file (inline CSS/JS, no build step, no separate asset mount) served at `GET /`; a single relay endpoint, `POST /api/render/validate-key`, takes a key in the request body, makes one read call to Render's API, and returns a verdict — never the key itself. The browser holds the key in `sessionStorage` for the rest of the tab session; nothing is ever written to disk or a database on the server side. Theme and language are separate, non-secret per-visitor preferences and use `localStorage` instead, mirroring `dashboard.html`'s existing pattern exactly.

**Tech Stack:** FastAPI, httpx (async client), pydantic, pytest + pytest-asyncio + respx (all already dependencies of this repo's single `uv`-managed `pyproject.toml` — no new dependencies needed).

**Spec:** `docs/superpowers/specs/2026-08-26-onboarding-wizard-render-frame-design.md`

## Global Constraints

- **No database, session store, cache, or any server-side persistence of a visitor credential, anywhere in `onboarding/`.** If a task seems to need one, stop — this is a deliberate architectural choice (spec section 3), not a gap to fill.
- **Every relay endpoint takes a credential in the request body and returns a verdict — never the credential itself, never a value derived from it that could reconstruct it.**
- **Never log a visitor-supplied credential, in full or truncated.** Structural facts only (status codes, booleans).
- **`onboarding/` is a separate service from `app/`** — no imports between `onboarding/`'s code and `app/`'s credential-handling paths (`app/config.py`'s `Settings`, `app/github_app.py`, etc.).
- **No new dependencies, no new `pyproject.toml`.** Reuse this repo's existing single `uv`-managed dependency set.
- **Static pages are single self-contained HTML files** (inline `<style>`/`<script>`, following `app/static/dashboard.html`'s existing convention) — no separate `.css`/`.js` files, no `StaticFiles` mount.
- Tests live in the existing root `tests/` directory (pytest's `testpaths = ["tests"]`), flat-named `test_onboarding_*.py`, matching this repo's existing test layout rather than a separate `onboarding/tests/` (a deviation from the design doc's illustrative file tree, which predates this check against the actual pytest config).
- **A completed frame locks behind an explicit "Change" control**; resubmitting it re-locks every later frame until re-validated (spec section 6).
- **Theme (light/dark/system), language (English/Hebrew), and RTL layout mirror `app/static/dashboard.html`'s existing implementation** rather than a new one-off (spec section 7).

**Adjustments from the design doc**, both deliberate, both consistent with "follow existing patterns" in `app/CLAUDE.md`:
1. Tests live in root `tests/`, not `onboarding/tests/` (see above).
2. The wizard page is one file (`onboarding/static/index.html`) with inline `<style>`/`<script>`, not separate `wizard.js`/`wizard.css` — `app/static/` has exactly one precedent (`dashboard.html`) and it's a single self-contained file with no `StaticFiles` mount anywhere in `app/main.py`. Matching that avoids introducing new static-asset infrastructure for no reason.
3. Task 4 adds automated content-substring tests for the wizard page (`tests/test_onboarding_page.py`), not just the spec's "manual click-through" — `tests/test_dashboard_page.py` already establishes this as a real, working convention for regression-testing a single-file static page in this repo (asserting JS/HTML source substrings without executing them), and it's cheap given the precedent exists. Manual verification is kept too, as the one check that actually confirms browser behavior rather than source text.
4. The design doc's section 6 frame-locking requirement and section 7 mobile/RTL/theme requirement are split into two tasks (4 and 5) rather than one — a reviewer can meaningfully approve "the frame state machine and lock/Change behavior work" while still wanting another pass at "the Hebrew translations and RTL layout are right," so they get separate review gates. Task 5 substantially rewrites the file Task 4 wrote; this is expected, not a sign Task 4 was wrong — i18n was always going to touch nearly every user-facing string.

---

## Task 1: Scaffold the `onboarding/` service

**Files:**
- Create: `onboarding/__init__.py`
- Create: `onboarding/CLAUDE.md`
- Create: `onboarding/main.py`
- Test: `tests/test_onboarding_main.py`

**Interfaces:**
- Produces: `onboarding.main.app` (a `FastAPI` instance) — every later task's tests import this.

- [ ] **Step 1: Write the failing test**

Create `tests/test_onboarding_main.py`:

```python
"""Tests for onboarding/main.py — GET/HEAD /healthz on the standalone
onboarding service (a separate FastAPI app from app/main.py)."""
from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from onboarding.main import app


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_healthz_get_returns_ok():
    client = await _client()
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_healthz_head_returns_ok():
    client = await _client()
    resp = await client.head("/healthz")
    assert resp.status_code == 200
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_onboarding_main.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'onboarding'`

- [ ] **Step 3: Create the empty package marker**

Create `onboarding/__init__.py` (empty file — matches `app/__init__.py`'s convention).

- [ ] **Step 4: Write `onboarding/CLAUDE.md`**

Create `onboarding/CLAUDE.md`:

```markdown
# onboarding/ — self-service setup wizard

Loaded when working under `onboarding/`. This is a **separate service** from
the review engine in `app/` — different process, different deploy, different
threat model. Root `CLAUDE.md`'s secret-handling section still applies in
full; the additions below are specific to what makes this service different.

## The invariant this service exists to protect

This backend is a **stateless relay**. It must never gain a database, a
session store, a cache, or any other place a visitor's credential could be
written to disk or held past the lifetime of a single request. If a task
here seems to need persistence, that is a signal to stop and reconsider the
design, not to add a datastore — durable state for this wizard was a
deliberate architectural choice to avoid (see
`docs/superpowers/specs/2026-08-26-onboarding-wizard-render-frame-design.md`
section 3), not an oversight to fix.

## Rules

- **Never log a visitor-supplied credential**, in full or truncated — same
  standard as root `CLAUDE.md` applies to the operator's own secrets, applied
  here to strangers' secrets, which if anything deserves *more* caution
  since these are people who did not choose to trust this codebase with
  their operational hygiene the way the project's own operator has.
- **Every relay endpoint takes a credential in the request body and returns
  a verdict — never the credential itself, never a derived artifact that
  reconstructs it.** A response schema that echoes back anything from the
  request beyond a boolean/enum/short display name (e.g. an account or
  owner name) needs a specific reason, not just convenience.
- **New external-service integrations follow the same relay shape** as the
  Render frame (`render_client.py` / `router.py`'s `/api/render/validate-key`
  pattern): browser holds the token, backend is a stateless pass-through per
  request. Do not special-case a "simple" integration into calling an
  external API directly from browser JS just because it doesn't strictly
  need server-side confidentiality (see design doc section 3 for why).
- **This service and the review engine (`app/`) do not import from each
  other's credential-handling code paths.** Shared *non-secret* utilities
  (HTTP client setup, logging config) may be factored into a common module
  if genuinely duplicated, but never a shared code path that touches both
  the operator's own long-lived credentials (`app/config.py`'s `Settings`)
  and a visitor's transient ones — keeping these separate is what lets each
  service's threat model be reasoned about independently.
```

- [ ] **Step 5: Write `onboarding/main.py`**

```python
"""onboarding/ — self-service setup wizard: a separate service from the
review engine in app/. Stateless relay only — no database, no session
store. See
docs/superpowers/specs/2026-08-26-onboarding-wizard-render-frame-design.md.
"""
from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="onboarding-wizard")


@app.get("/healthz")
@app.head("/healthz")
async def healthz():
    return {"status": "ok"}
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run pytest tests/test_onboarding_main.py -v`
Expected: PASS (2 tests)

- [ ] **Step 7: Commit**

```bash
git add onboarding/__init__.py onboarding/CLAUDE.md onboarding/main.py tests/test_onboarding_main.py
git commit -m "feat: scaffold the onboarding wizard as a standalone service"
```

---

## Task 2: `render_client.py` — validate a Render API key

**Files:**
- Create: `onboarding/render_client.py`
- Test: `tests/test_onboarding_render_client.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `onboarding.render_client.validate_key(api_key: str) -> RenderValidation` (async), `RenderKeyValid(owner_name: str)`, `RenderKeyInvalid(reason: str)` where `reason` is `"invalid_key"` or `"render_unreachable"`, and `RENDER_API_BASE: str`. Task 3's router imports and calls `validate_key` and pattern-matches on `RenderKeyValid`/`RenderKeyInvalid`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_onboarding_render_client.py`:

```python
"""Tests for onboarding/render_client.py — Render key validation never logs
or returns the raw key, and distinguishes an invalid key from Render being
unreachable (design doc sections 6 and 8)."""
from __future__ import annotations

import httpx
import respx

from onboarding import render_client

SENTINEL_KEY = "rnd_SENTINEL_DO_NOT_LOG_9f3a"
OWNERS_URL = f"{render_client.RENDER_API_BASE}/owners"


async def test_valid_key_returns_owner_name():
    with respx.mock:
        respx.get(OWNERS_URL).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "owner": {
                            "id": "usr-1",
                            "name": "Ada Lovelace",
                            "email": "ada@example.com",
                            "type": "user",
                        },
                        "cursor": "abc",
                    }
                ],
            )
        )
        result = await render_client.validate_key(SENTINEL_KEY)
    assert result == render_client.RenderKeyValid(owner_name="Ada Lovelace")


async def test_unauthorized_key_is_invalid():
    with respx.mock:
        respx.get(OWNERS_URL).mock(return_value=httpx.Response(401, json={"message": "nope"}))
        result = await render_client.validate_key(SENTINEL_KEY)
    assert result == render_client.RenderKeyInvalid(reason="invalid_key")


async def test_forbidden_key_is_invalid():
    with respx.mock:
        respx.get(OWNERS_URL).mock(return_value=httpx.Response(403, json={"message": "nope"}))
        result = await render_client.validate_key(SENTINEL_KEY)
    assert result == render_client.RenderKeyInvalid(reason="invalid_key")


async def test_render_5xx_is_unreachable_not_invalid():
    with respx.mock:
        respx.get(OWNERS_URL).mock(return_value=httpx.Response(500))
        result = await render_client.validate_key(SENTINEL_KEY)
    assert result == render_client.RenderKeyInvalid(reason="render_unreachable")


async def test_timeout_is_unreachable():
    with respx.mock:
        respx.get(OWNERS_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
        result = await render_client.validate_key(SENTINEL_KEY)
    assert result == render_client.RenderKeyInvalid(reason="render_unreachable")


async def test_empty_owners_list_is_invalid():
    with respx.mock:
        respx.get(OWNERS_URL).mock(return_value=httpx.Response(200, json=[]))
        result = await render_client.validate_key(SENTINEL_KEY)
    assert result == render_client.RenderKeyInvalid(reason="invalid_key")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_render_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'onboarding.render_client'`

- [ ] **Step 3: Write `onboarding/render_client.py`**

```python
"""Thin async wrapper around Render's REST API — validates that a
visitor-supplied API key is live, without persisting it anywhere. See
docs/superpowers/specs/2026-08-26-onboarding-wizard-render-frame-design.md
sections 5-6.
"""
from __future__ import annotations

import dataclasses

import httpx

RENDER_API_BASE = "https://api.render.com/v1"


@dataclasses.dataclass(frozen=True)
class RenderKeyValid:
    owner_name: str


@dataclasses.dataclass(frozen=True)
class RenderKeyInvalid:
    reason: str  # "invalid_key" | "render_unreachable"


RenderValidation = RenderKeyValid | RenderKeyInvalid


async def validate_key(api_key: str) -> RenderValidation:
    """One cheap read call (GET /owners) to confirm api_key is a live Render
    API key. Never logs or returns the key itself — only a boolean verdict
    and, on success, the display name of the account it belongs to."""
    try:
        async with httpx.AsyncClient(base_url=RENDER_API_BASE, timeout=10.0) as client:
            response = await client.get(
                "/owners",
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            )
    except httpx.HTTPError:
        return RenderKeyInvalid(reason="render_unreachable")

    if response.status_code in (401, 403):
        return RenderKeyInvalid(reason="invalid_key")
    if response.status_code != 200:
        return RenderKeyInvalid(reason="render_unreachable")

    body = response.json()
    if not body:
        return RenderKeyInvalid(reason="invalid_key")
    return RenderKeyValid(owner_name=body[0]["owner"]["name"])
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_render_client.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add onboarding/render_client.py tests/test_onboarding_render_client.py
git commit -m "feat: validate a visitor's Render API key against GET /owners"
```

---

## Task 3: `router.py` — the relay endpoint and the page route

**Files:**
- Create: `onboarding/router.py`
- Create: `onboarding/static/index.html` (minimal placeholder — Task 4 replaces the body)
- Modify: `onboarding/main.py` (mount the router)
- Test: `tests/test_onboarding_router.py`

**Interfaces:**
- Consumes: `onboarding.render_client.validate_key`, `RenderKeyValid`, `RenderKeyInvalid` (Task 2).
- Produces: `onboarding.router.router` (an `APIRouter`) mounted on `onboarding.main.app`, exposing `GET /` and `POST /api/render/validate-key`. Task 4 replaces `onboarding/static/index.html`'s contents but not its path.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_onboarding_router.py`:

```python
"""Tests for onboarding/router.py — the JSON contract for
POST /api/render/validate-key never echoes the submitted key, and GET /
serves the wizard page. See design doc section 5."""
from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from onboarding import render_client
from onboarding.main import app

SENTINEL_KEY = "rnd_SENTINEL_DO_NOT_LOG_9f3a"


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_index_serves_html():
    client = await _client()
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


async def test_valid_key_returns_owner_name(monkeypatch):
    async def fake_validate_key(api_key: str):
        assert api_key == SENTINEL_KEY
        return render_client.RenderKeyValid(owner_name="Ada Lovelace")

    monkeypatch.setattr(render_client, "validate_key", fake_validate_key)
    client = await _client()
    resp = await client.post("/api/render/validate-key", json={"api_key": SENTINEL_KEY})
    assert resp.status_code == 200
    assert resp.json() == {"valid": True, "owner_name": "Ada Lovelace"}


async def test_invalid_key_reports_the_reason(monkeypatch):
    async def fake_validate_key(api_key: str):
        return render_client.RenderKeyInvalid(reason="invalid_key")

    monkeypatch.setattr(render_client, "validate_key", fake_validate_key)
    client = await _client()
    resp = await client.post("/api/render/validate-key", json={"api_key": SENTINEL_KEY})
    assert resp.status_code == 200
    assert resp.json() == {"valid": False, "reason": "invalid_key"}


async def test_unreachable_reports_the_reason(monkeypatch):
    async def fake_validate_key(api_key: str):
        return render_client.RenderKeyInvalid(reason="render_unreachable")

    monkeypatch.setattr(render_client, "validate_key", fake_validate_key)
    client = await _client()
    resp = await client.post("/api/render/validate-key", json={"api_key": SENTINEL_KEY})
    assert resp.status_code == 200
    assert resp.json() == {"valid": False, "reason": "render_unreachable"}


async def test_response_never_echoes_the_submitted_key(monkeypatch):
    async def fake_validate_key(api_key: str):
        return render_client.RenderKeyInvalid(reason="invalid_key")

    monkeypatch.setattr(render_client, "validate_key", fake_validate_key)
    client = await _client()
    resp = await client.post("/api/render/validate-key", json={"api_key": SENTINEL_KEY})
    assert SENTINEL_KEY not in resp.text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_router.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'onboarding.router'`

- [ ] **Step 3: Create the placeholder static page**

Create `onboarding/static/index.html`:

```html
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Set up your own reviewer</title></head>
<body><p>Coming soon.</p></body>
</html>
```

- [ ] **Step 4: Write `onboarding/router.py`**

```python
"""onboarding/router.py — the wizard's only HTTP surface: GET / (the static
page) and one relay endpoint per external service. Every relay endpoint
returns a verdict, never the credential it was given. See
docs/superpowers/specs/2026-08-26-onboarding-wizard-render-frame-design.md
section 5.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from onboarding import render_client

router = APIRouter()

_STATIC_DIR = Path(__file__).parent / "static"
_INDEX_HTML = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")


class RenderKeyRequest(BaseModel):
    api_key: str


@router.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(_INDEX_HTML)


@router.post("/api/render/validate-key")
async def validate_render_key(payload: RenderKeyRequest) -> dict:
    result = await render_client.validate_key(payload.api_key)
    if isinstance(result, render_client.RenderKeyValid):
        return {"valid": True, "owner_name": result.owner_name}
    return {"valid": False, "reason": result.reason}
```

- [ ] **Step 5: Mount the router in `onboarding/main.py`**

Modify `onboarding/main.py` — replace its contents with:

```python
"""onboarding/ — self-service setup wizard: a separate service from the
review engine in app/. Stateless relay only — no database, no session
store. See
docs/superpowers/specs/2026-08-26-onboarding-wizard-render-frame-design.md.
"""
from __future__ import annotations

from fastapi import FastAPI

from onboarding.router import router

app = FastAPI(title="onboarding-wizard")
app.include_router(router)


@app.get("/healthz")
@app.head("/healthz")
async def healthz():
    return {"status": "ok"}
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_main.py tests/test_onboarding_router.py -v`
Expected: PASS (2 + 5 = 7 tests)

- [ ] **Step 7: Commit**

```bash
git add onboarding/router.py onboarding/static/index.html onboarding/main.py tests/test_onboarding_router.py
git commit -m "feat: wire up POST /api/render/validate-key and GET /"
```

---

## Task 4: The wizard page — accordion shell, working Render frame, lock/Change, mobile

**Files:**
- Modify: `onboarding/static/index.html` (replace the Task 3 placeholder with the full page)
- Test: `tests/test_onboarding_page.py`

**Interfaces:**
- Consumes: `POST /api/render/validate-key` (Task 3) via `fetch` from the browser.
- Produces: the frame state machine (`setFrameStatus`, `lockFrame`, `unlockFrame`, `relockDownstreamOf`, `completeFrame`, `beginChange`, `nextFrame`, `FRAME_ORDER`, `STORAGE_KEYS`) that Task 5 builds its i18n layer on top of — same function names and signatures, Task 5 changes their internals to route text through translations, not their call sites.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_onboarding_page.py`:

```python
"""Tests for GET / — the onboarding wizard's static page shell: the frame
state machine, locking, and the "Change" re-edit path. Content-substring
checks against the served HTML/JS (this repo's existing convention for
single-file static pages — see tests/test_dashboard_page.py), not a JS
execution harness."""
from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from onboarding.main import app

FRAME_IDS = [
    "render-key", "github-app", "supabase", "llm-provider",
    "uptime-pinger", "render-deploy",
]


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_index_serves_all_six_frames_in_order():
    client = await _client()
    resp = await client.get("/")
    assert resp.status_code == 200
    body = resp.text
    positions = [body.index(f'id="frame-{fid}"') for fid in FRAME_IDS]
    assert positions == sorted(positions)


async def test_only_the_render_key_frame_starts_unlocked():
    client = await _client()
    body = (await client.get("/")).text
    assert (
        'id="frame-render-key" class="frame" data-status="ready" '
        'data-locked="false" open'
    ) in body
    for fid in FRAME_IDS[1:]:
        assert (
            f'id="frame-{fid}" class="frame" data-status="locked" data-locked="true"'
        ) in body


async def test_locked_frames_cannot_be_toggled_open():
    client = await _client()
    body = (await client.get("/")).text
    assert "function guardLockedFrames" in body
    assert 'el.dataset.locked === "true"' in body


async def test_render_key_leaves_the_page_exactly_once():
    """The key must only ever transit the one relay call — anything else
    would be a second, unaudited exit path for a visitor's credential."""
    client = await _client()
    body = (await client.get("/")).text
    assert 'fetch("/api/render/validate-key"' in body
    assert body.count("fetch(") == 1


async def test_render_key_never_persists_to_local_storage():
    """localStorage persists across browser sessions/tabs; sessionStorage
    does not, and only the latter is acceptable for a visitor credential.
    This checks the credential's own storage key, not a blanket ban on
    localStorage — Task 5 legitimately uses localStorage for non-secret
    theme/language preferences elsewhere on this same page."""
    client = await _client()
    body = (await client.get("/")).text
    assert 'sessionStorage.setItem(STORAGE_KEYS["render-key"], key)' in body
    assert 'localStorage.setItem(STORAGE_KEYS["render-key"]' not in body
    assert 'localStorage.getItem(STORAGE_KEYS["render-key"]' not in body


async def test_completing_a_frame_unlocks_the_next_one():
    client = await _client()
    body = (await client.get("/")).text
    assert "function completeFrame" in body
    assert "if (next) unlockFrame(next);" in body


async def test_done_frames_show_an_explicit_change_control():
    client = await _client()
    body = (await client.get("/")).text
    assert 'class="frame-change"' in body
    assert "function beginChange" in body


async def test_changing_a_frame_relocks_every_later_frame():
    """A resubmission must invalidate whatever later frames already did,
    not just this frame's own value — design doc section 6."""
    client = await _client()
    body = (await client.get("/")).text
    assert "function relockDownstreamOf" in body
    assert "relockDownstreamOf(id)" in body


async def test_submitted_key_is_cleared_from_the_input_after_success():
    client = await _client()
    body = (await client.get("/")).text
    assert 'input.value = "";' in body


async def test_page_has_a_mobile_breakpoint():
    client = await _client()
    body = (await client.get("/")).text
    assert "@media (max-width: 480px)" in body
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_page.py -v`
Expected: FAIL — the placeholder page has none of these markers (`assert 'id="frame-render-key"...' in body` fails, etc.)

- [ ] **Step 3: Write the full `onboarding/static/index.html`**

Replace `onboarding/static/index.html` entirely with:

```html
<!doctype html>
<html lang="en" dir="ltr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Set up your own reviewer</title>
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
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    padding: 2rem 1rem 4rem;
  }
  main { max-width: 640px; margin: 0 auto; }
  h1 { font-size: 1.5rem; }
  p.lede { color: var(--text-muted); }
  details.frame {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    margin-bottom: 0.75rem;
  }
  details.frame > summary {
    list-style: none;
    cursor: pointer;
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: center;
    gap: 0.5rem;
    padding: 0.9rem 1.1rem;
  }
  details.frame[data-locked="true"] > summary { cursor: not-allowed; color: var(--text-muted); }
  details.frame[data-status="done"] > summary { cursor: default; color: var(--text); }
  details.frame > summary::-webkit-details-marker { display: none; }
  .frame-title { font-weight: 600; }
  .frame-badge { font-size: 0.85rem; color: var(--text-muted); }
  details.frame[data-status="done"] .frame-badge { color: var(--ok); }
  details.frame[data-status="error"] .frame-badge { color: var(--fail); }
  .frame-change {
    display: none;
    background: var(--surface-2);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.4rem 0.8rem;
    min-height: 2.5rem;
    cursor: pointer;
  }
  details.frame[data-status="done"] .frame-change { display: inline-block; }
  .frame-body { padding: 0 1.1rem 1.1rem; border-top: 1px solid var(--border); }
  .frame-body input[type="password"] {
    width: 100%;
    padding: 0.5rem;
    margin: 0.5rem 0;
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    min-height: 2.5rem;
  }
  .frame-body button {
    background: var(--accent);
    color: #fff;
    border: none;
    border-radius: 6px;
    padding: 0.5rem 1rem;
    min-height: 2.5rem;
    cursor: pointer;
  }
  .frame-error { color: var(--fail); min-height: 1.2em; }
  @media (max-width: 480px) {
    details.frame > summary { padding: 0.75rem 0.9rem; }
    .frame-change { width: 100%; text-align: center; }
  }
</style>
</head>
<body>
<main>
  <h1>Set up your own PR review bot</h1>
  <p class="lede">Work through each step below. Nothing you enter here is
    stored on this server — it stays in your browser for this session
    only.</p>

  <details id="frame-render-key" class="frame" data-status="ready" data-locked="false" open>
    <summary>
      <span class="frame-title">1. Render API key</span>
      <span class="frame-badge">Not started</span>
      <button class="frame-change" type="button" data-frame="render-key">Change</button>
    </summary>
    <div class="frame-body">
      <p>Get a key from Render's dashboard: Account Settings &rarr; API Keys.</p>
      <input id="render-key-input" type="password" placeholder="rnd_...">
      <button id="render-key-submit" type="button">Validate</button>
      <p id="render-key-error" class="frame-error"></p>
    </div>
  </details>

  <details id="frame-github-app" class="frame" data-status="locked" data-locked="true">
    <summary>
      <span class="frame-title">2. GitHub App</span>
      <span class="frame-badge">Locked</span>
    </summary>
    <div class="frame-body"><p>Coming soon.</p></div>
  </details>

  <details id="frame-supabase" class="frame" data-status="locked" data-locked="true">
    <summary>
      <span class="frame-title">3. Supabase database</span>
      <span class="frame-badge">Locked</span>
    </summary>
    <div class="frame-body"><p>Coming soon.</p></div>
  </details>

  <details id="frame-llm-provider" class="frame" data-status="locked" data-locked="true">
    <summary>
      <span class="frame-title">4. LLM provider</span>
      <span class="frame-badge">Locked</span>
    </summary>
    <div class="frame-body"><p>Coming soon.</p></div>
  </details>

  <details id="frame-uptime-pinger" class="frame" data-status="locked" data-locked="true">
    <summary>
      <span class="frame-title">5. Keep-warm pinger</span>
      <span class="frame-badge">Locked</span>
    </summary>
    <div class="frame-body"><p>Coming soon.</p></div>
  </details>

  <details id="frame-render-deploy" class="frame" data-status="locked" data-locked="true">
    <summary>
      <span class="frame-title">6. Deploy to Render</span>
      <span class="frame-badge">Locked</span>
    </summary>
    <div class="frame-body"><p>Coming soon.</p></div>
  </details>
</main>
<script>
  const STORAGE_KEYS = {
    "render-key": "onboarding.renderApiKey",
  };

  const FRAME_ORDER = [
    "render-key", "github-app", "supabase", "llm-provider",
    "uptime-pinger", "render-deploy",
  ];

  const BADGE_TEXT = {
    locked: "Locked",
    ready: "Not started",
    error: "Error",
    done: "✓ Validated",
  };

  function frameEl(id) {
    return document.getElementById(`frame-${id}`);
  }

  function badgeEl(id) {
    return frameEl(id).querySelector(".frame-badge");
  }

  function setFrameStatus(id, status, detail) {
    const el = frameEl(id);
    el.dataset.status = status;
    badgeEl(id).textContent = detail ? `${BADGE_TEXT[status]} — ${detail}` : BADGE_TEXT[status];
  }

  function nextFrame(id) {
    return FRAME_ORDER[FRAME_ORDER.indexOf(id) + 1];
  }

  function lockFrame(id) {
    const el = frameEl(id);
    el.dataset.locked = "true";
    el.open = false;
    setFrameStatus(id, "locked");
    const key = STORAGE_KEYS[id];
    if (key) sessionStorage.removeItem(key);
  }

  function unlockFrame(id) {
    const el = frameEl(id);
    el.dataset.locked = "false";
    setFrameStatus(id, "ready");
  }

  function relockDownstreamOf(id) {
    FRAME_ORDER.slice(FRAME_ORDER.indexOf(id) + 1).forEach(lockFrame);
  }

  function completeFrame(id, detail) {
    const el = frameEl(id);
    el.dataset.locked = "true";
    el.open = false;
    setFrameStatus(id, "done", detail);
    const next = nextFrame(id);
    if (next) unlockFrame(next);
  }

  function beginChange(id) {
    relockDownstreamOf(id);
    const el = frameEl(id);
    el.dataset.locked = "false";
    el.open = true;
    setFrameStatus(id, "ready");
  }

  async function validateRenderKey() {
    const input = document.getElementById("render-key-input");
    const key = input.value.trim();
    const errorEl = document.getElementById("render-key-error");
    errorEl.textContent = "";
    if (!key) {
      errorEl.textContent = "Paste your Render API key first.";
      return;
    }
    setFrameStatus("render-key", "ready", "checking…");
    let resp;
    try {
      resp = await fetch("/api/render/validate-key", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({api_key: key}),
      });
    } catch (err) {
      setFrameStatus("render-key", "error");
      errorEl.textContent = "Could not reach the wizard's server. Try again.";
      return;
    }
    const body = await resp.json();
    if (body.valid) {
      sessionStorage.setItem(STORAGE_KEYS["render-key"], key);
      input.value = "";
      completeFrame("render-key", `owner: ${body.owner_name}`);
    } else if (body.reason === "invalid_key") {
      setFrameStatus("render-key", "error");
      errorEl.textContent = "That key was rejected by Render. Double-check it and try again.";
    } else {
      setFrameStatus("render-key", "error");
      errorEl.textContent = "Render's API is unreachable right now. Try again in a moment.";
    }
  }

  function restoreFromSession() {
    if (sessionStorage.getItem(STORAGE_KEYS["render-key"])) {
      completeFrame("render-key", "restored from this session");
    }
  }

  function guardLockedFrames() {
    document.querySelectorAll(".frame").forEach((el) => {
      el.addEventListener("toggle", () => {
        if (el.open && el.dataset.locked === "true") {
          el.open = false;
        }
      });
    });
  }

  function attachChangeButtons() {
    document.querySelectorAll(".frame-change").forEach((btn) => {
      btn.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        beginChange(btn.dataset.frame);
      });
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.getElementById("render-key-submit").addEventListener("click", validateRenderKey);
    guardLockedFrames();
    attachChangeButtons();
    restoreFromSession();
  });
</script>
</body>
</html>
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_page.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Manual verification in a real browser**

Automated tests check the HTML/JS source, not that a browser actually
executes it correctly — do one manual pass:

```bash
uv run uvicorn onboarding.main:app --reload --port 8010
```

Open `http://localhost:8010/` and confirm:
- Frame 1 is open and unlocked; frames 2-6 show "Locked" and cannot be
  clicked open.
- Pasting an obviously-invalid string (e.g. `not-a-real-key`) and clicking
  Validate shows the "key was rejected by Render" error, frame 1 stays open.
- If you have a real Render API key handy, pasting it and clicking Validate
  shows "✓ Validated — owner: `<your account name>`", the input clears,
  frame 1 collapses and shows a "Change" button instead of being directly
  clickable, and frame 2's badge changes from "Locked" to "Not started"
  (still non-functional — that's sub-project 2's job).
- Clicking "Change" on frame 1 re-opens it for editing and re-locks frame 2
  back to "Locked".
- Reloading the page after a successful validation restores frame 1 to
  "done" and frame 2 unlocked, without a second network call (open the
  browser's Network tab to confirm no request fires on reload).
- Shrink the browser window to a phone width (~375px) — frame headers and
  the Validate/Change buttons stay usable and don't overflow or overlap.

- [ ] **Step 6: Commit**

```bash
git add onboarding/static/index.html tests/test_onboarding_page.py
git commit -m "feat: build the accordion wizard shell with lock/Change and mobile layout"
```

---

## Task 5: Theme, language, and RTL support

**Files:**
- Modify: `onboarding/static/index.html` (adds the theme/language toggle UI, `STRINGS`/`t()`, and RTL — routes Task 4's frame-state-machine functions' text output through translations without changing their names or call sites)
- Test: `tests/test_onboarding_i18n.py`

**Interfaces:**
- Consumes: Task 4's `setFrameStatus`, `completeFrame`, `beginChange`, `lockFrame`, `unlockFrame`, `relockDownstreamOf`, `nextFrame`, `FRAME_ORDER`, `STORAGE_KEYS` (same names; their internals change to call `t()` instead of using hardcoded English/`BADGE_TEXT`).
- Produces: nothing new for later tasks in this plan — this is the terminal task of this slice.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_onboarding_i18n.py`:

```python
"""Tests for the onboarding wizard's theme/language/RTL controls — mirrors
app/static/dashboard.html's existing implementation (design doc section 7).
Content-substring checks, same convention as tests/test_dashboard_page.py."""
from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from onboarding.main import app

STRINGS_KEYS = [
    "page_title", "heading", "lede", "theme_light", "theme_dark", "theme_system",
    "theme_popup_title", "lang_popup_title", "frame1_title", "frame1_instructions",
    "frame1_placeholder", "frame2_title", "frame3_title", "frame4_title",
    "frame5_title", "frame6_title", "coming_soon", "validate_button",
    "change_button", "badge_locked", "badge_ready", "badge_error", "badge_done",
    "err_empty_key", "err_invalid_key", "err_unreachable", "err_network",
    "checking", "restored", "owner_prefix",
]


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_page_has_theme_and_language_controls():
    client = await _client()
    resp = await client.get("/")
    body = resp.text
    assert 'id="themeToggleBtn"' in body
    assert 'id="langToggleBtn"' in body
    assert 'name="theme"' in body
    assert 'name="lang"' in body
    assert "עברית" in body
    assert 'dir="ltr"' in body


async def test_every_string_key_is_defined_for_both_languages():
    client = await _client()
    body = (await client.get("/")).text
    for key in STRINGS_KEYS:
        assert body.count(f"{key}:") == 2, f"{key} should appear once per language block"


async def test_theme_switch_uses_the_data_theme_attribute():
    client = await _client()
    body = (await client.get("/")).text
    assert 'document.documentElement.setAttribute("data-theme"' in body
    assert ':root[data-theme="dark"]' in body


async def test_language_switch_sets_dir_for_rtl():
    client = await _client()
    body = (await client.get("/")).text
    assert 'document.documentElement.setAttribute("dir", lang === "he" ? "rtl" : "ltr")' in body


async def test_popup_positioning_is_rtl_aware():
    client = await _client()
    body = (await client.get("/")).text
    assert "function positionPopup" in body
    assert 'document.documentElement.getAttribute("dir") === "rtl"' in body


async def test_theme_and_language_preferences_use_local_storage():
    """Unlike the Render key (sessionStorage only, see
    test_onboarding_page.py), these are non-secret per-visitor preferences
    that should reasonably persist across tabs."""
    client = await _client()
    body = (await client.get("/")).text
    assert 'localStorage.getItem("onboarding_lang")' in body
    assert 'localStorage.getItem("onboarding_theme")' in body
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_i18n.py -v`
Expected: FAIL — none of this markup/JS exists yet.

- [ ] **Step 3: Rewrite `onboarding/static/index.html` with theme/language/RTL support**

Replace `onboarding/static/index.html` entirely with:

```html
<!doctype html>
<html lang="en" dir="ltr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Set up your own reviewer</title>
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
    flex-wrap: wrap;
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
    min-height: 2.5rem;
  }
  button.control:hover { border-color: var(--accent); }
  .popup-backdrop { position: fixed; inset: 0; background: transparent; display: none; z-index: 10; }
  .popup-backdrop.open { display: block; }
  .popup {
    position: absolute;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 0.75rem;
    padding: 1rem 1.25rem;
    min-width: 220px;
    max-width: min(90vw, 320px);
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
  main { max-width: 640px; margin: 0 auto; padding: 2rem 1rem 4rem; }
  h1 { font-size: 1.5rem; }
  p.lede { color: var(--text-muted); }
  details.frame {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    margin-bottom: 0.75rem;
  }
  details.frame > summary {
    list-style: none;
    cursor: pointer;
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: center;
    gap: 0.5rem;
    padding: 0.9rem 1.1rem;
  }
  details.frame[data-locked="true"] > summary { cursor: not-allowed; color: var(--text-muted); }
  details.frame[data-status="done"] > summary { cursor: default; color: var(--text); }
  details.frame > summary::-webkit-details-marker { display: none; }
  .frame-title { font-weight: 600; }
  .frame-badge { font-size: 0.85rem; color: var(--text-muted); }
  details.frame[data-status="done"] .frame-badge { color: var(--ok); }
  details.frame[data-status="error"] .frame-badge { color: var(--fail); }
  .frame-change {
    display: none;
    background: var(--surface-2);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.4rem 0.8rem;
    min-height: 2.5rem;
    cursor: pointer;
  }
  details.frame[data-status="done"] .frame-change { display: inline-block; }
  .frame-body { padding: 0 1.1rem 1.1rem; border-top: 1px solid var(--border); }
  .frame-body input[type="password"] {
    width: 100%;
    padding: 0.5rem;
    margin: 0.5rem 0;
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    min-height: 2.5rem;
  }
  .frame-body button {
    background: var(--accent);
    color: #fff;
    border: none;
    border-radius: 6px;
    padding: 0.5rem 1rem;
    min-height: 2.5rem;
    cursor: pointer;
  }
  .frame-error { color: var(--fail); min-height: 1.2em; }
  @media (max-width: 480px) {
    header.topbar { justify-content: center; }
    details.frame > summary { padding: 0.75rem 0.9rem; }
    .frame-change { width: 100%; text-align: center; }
  }
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
    <h1 data-i18n="heading"></h1>
    <p class="lede" data-i18n="lede"></p>

    <details id="frame-render-key" class="frame" data-status="ready" data-locked="false" open>
      <summary>
        <span class="frame-title" data-i18n="frame1_title"></span>
        <span class="frame-badge"></span>
        <button class="frame-change" type="button" data-frame="render-key" data-i18n="change_button"></button>
      </summary>
      <div class="frame-body">
        <p data-i18n="frame1_instructions"></p>
        <input id="render-key-input" type="password">
        <button id="render-key-submit" type="button" data-i18n="validate_button"></button>
        <p id="render-key-error" class="frame-error"></p>
      </div>
    </details>

    <details id="frame-github-app" class="frame" data-status="locked" data-locked="true">
      <summary>
        <span class="frame-title" data-i18n="frame2_title"></span>
        <span class="frame-badge"></span>
      </summary>
      <div class="frame-body"><p data-i18n="coming_soon"></p></div>
    </details>

    <details id="frame-supabase" class="frame" data-status="locked" data-locked="true">
      <summary>
        <span class="frame-title" data-i18n="frame3_title"></span>
        <span class="frame-badge"></span>
      </summary>
      <div class="frame-body"><p data-i18n="coming_soon"></p></div>
    </details>

    <details id="frame-llm-provider" class="frame" data-status="locked" data-locked="true">
      <summary>
        <span class="frame-title" data-i18n="frame4_title"></span>
        <span class="frame-badge"></span>
      </summary>
      <div class="frame-body"><p data-i18n="coming_soon"></p></div>
    </details>

    <details id="frame-uptime-pinger" class="frame" data-status="locked" data-locked="true">
      <summary>
        <span class="frame-title" data-i18n="frame5_title"></span>
        <span class="frame-badge"></span>
      </summary>
      <div class="frame-body"><p data-i18n="coming_soon"></p></div>
    </details>

    <details id="frame-render-deploy" class="frame" data-status="locked" data-locked="true">
      <summary>
        <span class="frame-title" data-i18n="frame6_title"></span>
        <span class="frame-badge"></span>
      </summary>
      <div class="frame-body"><p data-i18n="coming_soon"></p></div>
    </details>
  </main>

<script>
  const STRINGS = {
    en: {
      page_title: "Set up your own reviewer",
      heading: "Set up your own PR review bot",
      lede: "Work through each step below. Nothing you enter here is stored on this server — it stays in your browser for this session only.",
      theme_light: "Light", theme_dark: "Dark", theme_system: "System",
      theme_popup_title: "Theme", lang_popup_title: "Language",
      frame1_title: "1. Render API key",
      frame1_instructions: "Get a key from Render's dashboard: Account Settings → API Keys.",
      frame1_placeholder: "rnd_...",
      frame2_title: "2. GitHub App",
      frame3_title: "3. Supabase database",
      frame4_title: "4. LLM provider",
      frame5_title: "5. Keep-warm pinger",
      frame6_title: "6. Deploy to Render",
      coming_soon: "Coming soon.",
      validate_button: "Validate",
      change_button: "Change",
      badge_locked: "Locked", badge_ready: "Not started",
      badge_error: "Error", badge_done: "✓ Validated",
      err_empty_key: "Paste your Render API key first.",
      err_invalid_key: "That key was rejected by Render. Double-check it and try again.",
      err_unreachable: "Render's API is unreachable right now. Try again in a moment.",
      err_network: "Could not reach the wizard's server. Try again.",
      checking: "checking…",
      restored: "restored from this session",
      owner_prefix: "owner: ",
    },
    he: {
      page_title: "הקמת בודק ה-PR שלך",
      heading: "הקמת בוט בדיקת PR משלך",
      lede: "עברו על כל שלב למטה. שום דבר שתזינו כאן לא נשמר בשרת הזה — הוא נשאר בדפדפן שלכם למשך הפעלה זו בלבד.",
      theme_light: "בהיר", theme_dark: "כהה", theme_system: "מערכת",
      theme_popup_title: "עיצוב", lang_popup_title: "שפה",
      frame1_title: "1. מפתח API של Render",
      frame1_instructions: "קבלו מפתח מלוח הבקרה של Render: Account Settings ← API Keys.",
      frame1_placeholder: "rnd_...",
      frame2_title: "2. אפליקציית GitHub",
      frame3_title: "3. מסד נתונים ב-Supabase",
      frame4_title: "4. ספק LLM",
      frame5_title: "5. פינגר לשמירה על פעילות",
      frame6_title: "6. פריסה ל-Render",
      coming_soon: "בקרוב.",
      validate_button: "אימות",
      change_button: "שינוי",
      badge_locked: "נעול", badge_ready: "טרם התחיל",
      badge_error: "שגיאה", badge_done: "✓ אומת",
      err_empty_key: "הדביקו קודם את מפתח ה-API של Render.",
      err_invalid_key: "המפתח נדחה על ידי Render. בדקו אותו שוב ונסו שנית.",
      err_unreachable: "שירות ה-API של Render אינו זמין כרגע. נסו שוב בעוד רגע.",
      err_network: "לא ניתן להתחבר לשרת האשף. נסו שוב.",
      checking: "בודק…",
      restored: "שוחזר מהפעלה זו",
      owner_prefix: "בעלים: ",
    },
  };

  const THEME_ICON = { light: "☀️", dark: "🌙", system: "🖥️" };
  const LANG_LABEL = { en: "🇺🇸 English", he: "🇮🇱 עברית" };

  let currentLang = localStorage.getItem("onboarding_lang") || "en";
  let currentTheme = localStorage.getItem("onboarding_theme") || "system";

  function t(key) {
    return STRINGS[currentLang][key] || STRINGS.en[key] || key;
  }

  function applyTheme(theme) {
    currentTheme = theme;
    localStorage.setItem("onboarding_theme", theme);
    document.documentElement.setAttribute("data-theme", theme === "system" ? "" : theme);
    document.getElementById("themeToggleBtn").textContent = `${THEME_ICON[theme]} ${t("theme_" + theme)}`;
    document.querySelector(`input[name="theme"][value="${theme}"]`).checked = true;
  }

  function applyLanguage(lang) {
    currentLang = lang;
    localStorage.setItem("onboarding_lang", lang);
    document.documentElement.setAttribute("lang", lang);
    document.documentElement.setAttribute("dir", lang === "he" ? "rtl" : "ltr");
    document.title = t("page_title");
    document.getElementById("langToggleBtn").textContent = LANG_LABEL[lang];
    document.querySelectorAll("[data-i18n]").forEach((el) => {
      el.textContent = t(el.getAttribute("data-i18n"));
    });
    document.getElementById("render-key-input").placeholder = t("frame1_placeholder");
    document.getElementById("themePopupTitle").textContent = t("theme_popup_title");
    document.getElementById("langPopupTitle").textContent = t("lang_popup_title");
    document.querySelector(`input[name="lang"][value="${lang}"]`).checked = true;
    refreshFrameBadges();
    applyTheme(currentTheme);
  }

  const STORAGE_KEYS = {
    "render-key": "onboarding.renderApiKey",
  };

  const FRAME_ORDER = [
    "render-key", "github-app", "supabase", "llm-provider",
    "uptime-pinger", "render-deploy",
  ];

  const frameState = {};
  FRAME_ORDER.forEach((id) => { frameState[id] = {status: "locked", detail: null}; });
  frameState["render-key"].status = "ready";

  function frameEl(id) {
    return document.getElementById(`frame-${id}`);
  }

  function badgeEl(id) {
    return frameEl(id).querySelector(".frame-badge");
  }

  function renderBadge(id) {
    const {status, detail} = frameState[id];
    const label = t("badge_" + status);
    badgeEl(id).textContent = detail ? `${label} — ${detail}` : label;
  }

  function refreshFrameBadges() {
    FRAME_ORDER.forEach(renderBadge);
  }

  function setFrameStatus(id, status, detail) {
    frameState[id] = {status, detail: detail || null};
    frameEl(id).dataset.status = status;
    renderBadge(id);
  }

  function nextFrame(id) {
    return FRAME_ORDER[FRAME_ORDER.indexOf(id) + 1];
  }

  function lockFrame(id) {
    const el = frameEl(id);
    el.dataset.locked = "true";
    el.open = false;
    setFrameStatus(id, "locked");
    const key = STORAGE_KEYS[id];
    if (key) sessionStorage.removeItem(key);
  }

  function unlockFrame(id) {
    const el = frameEl(id);
    el.dataset.locked = "false";
    setFrameStatus(id, "ready");
  }

  function relockDownstreamOf(id) {
    FRAME_ORDER.slice(FRAME_ORDER.indexOf(id) + 1).forEach(lockFrame);
  }

  function completeFrame(id, detail) {
    const el = frameEl(id);
    el.dataset.locked = "true";
    el.open = false;
    setFrameStatus(id, "done", detail);
    const next = nextFrame(id);
    if (next) unlockFrame(next);
  }

  function beginChange(id) {
    relockDownstreamOf(id);
    const el = frameEl(id);
    el.dataset.locked = "false";
    el.open = true;
    setFrameStatus(id, "ready");
  }

  async function validateRenderKey() {
    const input = document.getElementById("render-key-input");
    const key = input.value.trim();
    const errorEl = document.getElementById("render-key-error");
    errorEl.textContent = "";
    if (!key) {
      errorEl.textContent = t("err_empty_key");
      return;
    }
    setFrameStatus("render-key", "ready", t("checking"));
    let resp;
    try {
      resp = await fetch("/api/render/validate-key", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({api_key: key}),
      });
    } catch (err) {
      setFrameStatus("render-key", "error");
      errorEl.textContent = t("err_network");
      return;
    }
    const body = await resp.json();
    if (body.valid) {
      sessionStorage.setItem(STORAGE_KEYS["render-key"], key);
      input.value = "";
      completeFrame("render-key", `${t("owner_prefix")}${body.owner_name}`);
    } else if (body.reason === "invalid_key") {
      setFrameStatus("render-key", "error");
      errorEl.textContent = t("err_invalid_key");
    } else {
      setFrameStatus("render-key", "error");
      errorEl.textContent = t("err_unreachable");
    }
  }

  function restoreFromSession() {
    if (sessionStorage.getItem(STORAGE_KEYS["render-key"])) {
      completeFrame("render-key", t("restored"));
    }
  }

  function guardLockedFrames() {
    document.querySelectorAll(".frame").forEach((el) => {
      el.addEventListener("toggle", () => {
        if (el.open && el.dataset.locked === "true") {
          el.open = false;
        }
      });
    });
  }

  function attachChangeButtons() {
    document.querySelectorAll(".frame-change").forEach((btn) => {
      btn.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        beginChange(btn.dataset.frame);
      });
    });
  }

  function positionPopup(popup, anchorBtn) {
    const margin = 8;
    const rect = anchorBtn.getBoundingClientRect();
    const isRtl = document.documentElement.getAttribute("dir") === "rtl";
    popup.style.top = `${rect.bottom + margin}px`;
    popup.style.left = "auto";
    popup.style.right = "auto";
    const popupWidth = popup.offsetWidth;
    if (isRtl) {
      const maxRight = window.innerWidth - popupWidth - margin;
      const right = Math.min(Math.max(window.innerWidth - rect.right, margin), maxRight);
      popup.style.right = `${right}px`;
    } else {
      const maxLeft = window.innerWidth - popupWidth - margin;
      const left = Math.max(margin, Math.min(rect.left, maxLeft));
      popup.style.left = `${left}px`;
    }
  }

  function openPopup(id, anchorBtn) {
    closeAllPopups();
    const backdrop = document.getElementById(id);
    backdrop.classList.add("open");
    positionPopup(backdrop.querySelector(".popup"), anchorBtn);
  }

  function closeAllPopups() {
    document.querySelectorAll(".popup-backdrop").forEach((el) => el.classList.remove("open"));
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.getElementById("render-key-submit").addEventListener("click", validateRenderKey);
    guardLockedFrames();
    attachChangeButtons();

    document.getElementById("themeToggleBtn").addEventListener("click", (event) => openPopup("themePopupBackdrop", event.currentTarget));
    document.getElementById("langToggleBtn").addEventListener("click", (event) => openPopup("langPopupBackdrop", event.currentTarget));
    document.querySelectorAll(".popup-backdrop").forEach((backdrop) => {
      backdrop.addEventListener("click", (event) => {
        if (event.target === backdrop) closeAllPopups();
      });
    });
    document.querySelectorAll('input[name="theme"]').forEach((radio) => {
      radio.addEventListener("change", (event) => { applyTheme(event.target.value); closeAllPopups(); });
    });
    document.querySelectorAll('input[name="lang"]').forEach((radio) => {
      radio.addEventListener("change", (event) => { applyLanguage(event.target.value); closeAllPopups(); });
    });

    applyLanguage(currentLang);
    restoreFromSession();
  });
</script>
</body>
</html>
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_i18n.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Run the full onboarding test suite to confirm Task 4's tests still pass unmodified**

Run: `uv run pytest tests/test_onboarding_main.py tests/test_onboarding_render_client.py tests/test_onboarding_router.py tests/test_onboarding_page.py tests/test_onboarding_i18n.py -v`
Expected: PASS (all tests — Task 4's `test_onboarding_page.py` checks strings and function names that this rewrite deliberately preserved, e.g. `class="frame" data-status="ready" data-locked="false" open` on frame 1, `function completeFrame`, `function relockDownstreamOf`)

- [ ] **Step 6: Manual verification in a real browser**

```bash
uv run uvicorn onboarding.main:app --reload --port 8010
```

Open `http://localhost:8010/` and confirm:
- Clicking the language toggle and choosing עברית flips the whole page to
  Hebrew text, right-to-left layout (frame badges/buttons now on the left
  side of each header, not the right), and the choice survives a reload.
- Clicking the theme toggle and choosing Dark switches the page to the dark
  palette; choosing System matches your OS's current light/dark setting;
  the choice survives a reload.
- With Hebrew selected, open the theme popup — it should be positioned
  correctly relative to the button (not off-screen) — this exercises
  `positionPopup`'s RTL branch.
- Shrink the browser to a phone width (~375px) in both English/LTR and
  Hebrew/RTL — the top bar's two buttons, frame headers, and popups all
  stay usable and don't overflow.

- [ ] **Step 7: Run the full project test suite to confirm no regressions**

Run: `uv run pytest -q`
Expected: PASS (all existing tests plus every new onboarding test)

- [ ] **Step 8: Commit**

```bash
git add onboarding/static/index.html tests/test_onboarding_i18n.py
git commit -m "feat: add theme, language, and RTL support to the onboarding wizard"
```
