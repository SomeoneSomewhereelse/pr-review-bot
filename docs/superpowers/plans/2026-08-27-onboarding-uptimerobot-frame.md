# Onboarding wizard — UptimeRobot keep-warm frame Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `guide/setup/hosted/08-pinger.md`'s manual "create a monitor
by hand" step with an in-wizard frame 5 that creates (or reuses) the
keep-warm UptimeRobot monitor for the visitor's deployed Render service.

**Architecture:** A new `onboarding/uptimerobot_client.py` (raw `httpx`, no
SDK) talks to UptimeRobot's v3 REST API to list then create-or-reuse a
monitor; a new `POST /api/uptimerobot/create-monitor` relay endpoint in
`onboarding/router.py` exposes it; frame 5's markup and JS in
`onboarding/static/index.html` gain a credential-paste form gated behind a
"waiting on your Render deployment" blocked state, since the Render URL
this frame needs doesn't exist until sub-project 6 (not yet built) writes
it to `sessionStorage`.

**Tech Stack:** FastAPI, httpx (async), pydantic, respx (test mocking) —
all already project dependencies. Vanilla JS on the frontend, no framework,
matching every prior onboarding frame.

**Spec:**
`docs/superpowers/specs/2026-08-27-onboarding-uptimerobot-frame-design.md`

## Global Constraints

- **v3 REST API only** (`Bearer` auth, JSON body,
  `https://api.uptimerobot.com/v3/monitors`) for every UptimeRobot call this
  frame makes — never the legacy v2 form-API `scripts/deploy.py`'s existing
  `check_uptime_pinger` uses, which was verified live to reject monitor
  creation on a free-plan account (spec section 1).
- **The Render service URL comes from `sessionStorage["onboarding.renderServiceUrl"]`**
  — a key this sub-project only *reads*; sub-project 6 (not yet built) is
  obligated to *write* it (spec section 3's forward contract). Never ask
  the visitor to type this URL.
- **Missing-dependency handling:** if that key is absent, frame 5 shows a
  blocked message instead of the credential form — no polling, no
  auto-retry (spec section 2). This is expected to be frame 5's only
  reachable state until sub-project 6 exists.
- **`/healthz` path derivation happens server-side**: strip the submitted
  URL's trailing slash, append `/healthz` — mirrors
  `scripts/deploy.py::check_uptime_pinger`'s existing `f"{base}/healthz"`
  convention exactly (spec section 3).
- **Dedupe-before-create**: `GET /v3/monitors` first; only `POST
  /v3/monitors` if no existing monitor's `url` exactly matches the derived
  target (spec section 2).
- **No read-only-key detection is possible** — verified live, a
  Monitor-Specific key returns the identical `401` as an invalid key.
  Mitigate only via UI copy (input help text + the `unauthorized` error
  message both name the Main-API-Key requirement) — never invent a `reason`
  value the backend can't actually verify (spec section 2).
- **Monitor fields are fixed, no visitor choice**: `friendlyName` = the
  derived target URL, `type: "HTTP"`, `interval: 300`, `timeout: 30` (spec
  section 2).
- **Error `reason` vocabulary**: `unauthorized` (401), `rate_limited`
  (429), `provider_unreachable` (5xx / network exception),
  `request_rejected` (any other non-2xx, catch-all) (spec section 3).
- **Never log or echo the submitted `api_key`**, in full or truncated —
  root `CLAUDE.md`'s secret-handling section, applied here per
  `onboarding/CLAUDE.md`'s existing rule for visitor-supplied credentials.
- **`sessionStorage` only, never `localStorage`**, for the API key —
  `onboarding/CLAUDE.md`'s existing browser-side rule.
- **No real network calls in the test suite** — every UptimeRobot call is
  mocked via `respx` (spec section 6).
- **The new credential-carrying `fetch()` gets its own
  `..._leaves_the_page_exactly_once` test** — `onboarding/CLAUDE.md`'s
  existing one-exit-path convention.

---

### Task 1: `onboarding/uptimerobot_client.py`

**Files:**
- Create: `onboarding/uptimerobot_client.py`
- Test: `tests/test_onboarding_uptimerobot_client.py`

**Interfaces:**
- Produces: `UPTIMEROBOT_API_BASE = "https://api.uptimerobot.com/v3"`;
  `UptimeRobotMonitorResult(created: bool)` (frozen dataclass);
  `UptimeRobotApiFailed(reason: str)` (frozen dataclass, `reason` ∈
  `"unauthorized"`, `"rate_limited"`, `"provider_unreachable"`,
  `"request_rejected"`); `UptimeRobotOutcome = UptimeRobotMonitorResult |
  UptimeRobotApiFailed`; `async def create_or_reuse_monitor(api_key: str,
  render_service_url: str) -> UptimeRobotOutcome`.
- Consumes: nothing from earlier tasks (this is the first task).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_onboarding_uptimerobot_client.py`:

```python
"""Tests for onboarding/uptimerobot_client.py — monitor creation checks for
an existing monitor before creating a duplicate, the /healthz path is
derived server-side (never trusted from the caller), and every error maps
to a fixed reason vocabulary, never a raw exception message. See
docs/superpowers/specs/2026-08-27-onboarding-uptimerobot-frame-design.md
sections 2, 3, 6."""
from __future__ import annotations

import httpx
import respx

from onboarding import uptimerobot_client

SENTINEL_KEY = "ur_SENTINEL_DO_NOT_LOG_9f3a"
MONITORS_URL = f"{uptimerobot_client.UPTIMEROBOT_API_BASE}/monitors"
RENDER_URL = "https://sentinel-service.onrender.com"
TARGET_URL = "https://sentinel-service.onrender.com/healthz"


def _monitor(url, monitor_id=1):
    return {
        "id": monitor_id, "friendlyName": url, "url": url, "type": "HTTP",
        "interval": 300, "timeout": 30, "status": "STARTED",
    }


async def test_creates_monitor_when_none_exists():
    with respx.mock:
        respx.get(MONITORS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
        create_route = respx.post(MONITORS_URL).mock(
            return_value=httpx.Response(201, json=_monitor(TARGET_URL))
        )
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert result == uptimerobot_client.UptimeRobotMonitorResult(created=True)
    sent_body = create_route.calls.last.request.content
    assert TARGET_URL.encode() in sent_body


async def test_reuses_existing_monitor_without_creating():
    with respx.mock:
        respx.get(MONITORS_URL).mock(
            return_value=httpx.Response(200, json={"data": [_monitor(TARGET_URL)]})
        )
        # No POST route registered on purpose: respx.mock raises if an
        # unmocked call is attempted, so this also proves create was never
        # called, not just that the right value came back.
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert result == uptimerobot_client.UptimeRobotMonitorResult(created=False)


async def test_dedupe_check_ignores_a_monitor_on_a_different_url():
    with respx.mock:
        respx.get(MONITORS_URL).mock(
            return_value=httpx.Response(200, json={"data": [_monitor("https://someone-elses-service.onrender.com/healthz")]})
        )
        create_route = respx.post(MONITORS_URL).mock(
            return_value=httpx.Response(201, json=_monitor(TARGET_URL))
        )
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert result == uptimerobot_client.UptimeRobotMonitorResult(created=True)
    assert create_route.called


async def test_strips_trailing_slash_before_appending_healthz():
    with respx.mock:
        respx.get(MONITORS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
        create_route = respx.post(MONITORS_URL).mock(
            return_value=httpx.Response(201, json=_monitor(TARGET_URL))
        )
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL + "/")
    assert result == uptimerobot_client.UptimeRobotMonitorResult(created=True)
    sent_body = create_route.calls.last.request.content
    assert TARGET_URL.encode() in sent_body
    assert b"//healthz" not in sent_body


async def test_unauthorized_key_is_reported():
    with respx.mock:
        respx.get(MONITORS_URL).mock(
            return_value=httpx.Response(401, json={"message": "Invalid token.", "code": "003-005"})
        )
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert result == uptimerobot_client.UptimeRobotApiFailed(reason="unauthorized")


async def test_rate_limited_is_reported():
    with respx.mock:
        respx.get(MONITORS_URL).mock(return_value=httpx.Response(429, json={"message": "rate limited"}))
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert result == uptimerobot_client.UptimeRobotApiFailed(reason="rate_limited")


async def test_server_error_is_unreachable_not_invalid():
    with respx.mock:
        respx.get(MONITORS_URL).mock(return_value=httpx.Response(500))
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert result == uptimerobot_client.UptimeRobotApiFailed(reason="provider_unreachable")


async def test_network_error_is_unreachable():
    with respx.mock:
        respx.get(MONITORS_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert result == uptimerobot_client.UptimeRobotApiFailed(reason="provider_unreachable")


async def test_unexpected_client_error_on_create_is_request_rejected():
    with respx.mock:
        respx.get(MONITORS_URL).mock(return_value=httpx.Response(200, json={"data": []}))
        respx.post(MONITORS_URL).mock(return_value=httpx.Response(400, json={"message": "bad"}))
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert result == uptimerobot_client.UptimeRobotApiFailed(reason="request_rejected")


async def test_malformed_list_body_is_unreachable_not_a_crash():
    """A non-JSON 200 body from the list call must not let a traceback
    (which could carry request/response context) escape the function."""
    with respx.mock:
        respx.get(MONITORS_URL).mock(return_value=httpx.Response(200, text="not json"))
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert result == uptimerobot_client.UptimeRobotApiFailed(reason="provider_unreachable")


async def test_api_key_never_appears_in_the_returned_result():
    with respx.mock:
        respx.get(MONITORS_URL).mock(
            return_value=httpx.Response(401, json={"message": "Invalid token.", "code": "003-005"})
        )
        result = await uptimerobot_client.create_or_reuse_monitor(SENTINEL_KEY, RENDER_URL)
    assert SENTINEL_KEY not in repr(result)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_uptimerobot_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'onboarding.uptimerobot_client'`

- [ ] **Step 3: Write the implementation**

Create `onboarding/uptimerobot_client.py`:

```python
"""Thin async wrapper around UptimeRobot's v3 REST API — creates (or
reuses) the keep-warm monitor for a deployed Render service. See
docs/superpowers/specs/2026-08-27-onboarding-uptimerobot-frame-design.md
sections 2-3.

Uses v3 (Bearer auth, JSON), not the legacy v2 form-API
scripts/deploy.py's existing read-only check_uptime_pinger already uses:
v2's POST /newMonitor was verified live to reject monitor creation on a
free-plan account (403 "not allowed to use some settings with your
current plan"); v3 was verified live to accept it on the same account
(spec section 1)."""
from __future__ import annotations

import dataclasses

import httpx

UPTIMEROBOT_API_BASE = "https://api.uptimerobot.com/v3"
_MONITOR_INTERVAL_SECONDS = 300
_MONITOR_TIMEOUT_SECONDS = 30


@dataclasses.dataclass(frozen=True)
class UptimeRobotMonitorResult:
    created: bool


@dataclasses.dataclass(frozen=True)
class UptimeRobotApiFailed:
    reason: str  # "unauthorized" | "rate_limited" | "provider_unreachable" | "request_rejected"


UptimeRobotOutcome = UptimeRobotMonitorResult | UptimeRobotApiFailed


def _target_url(render_service_url: str) -> str:
    return f"{render_service_url.rstrip('/')}/healthz"


def _failure_for_status(status_code: int) -> UptimeRobotApiFailed | None:
    if status_code in (200, 201):
        return None
    if status_code == 401:
        return UptimeRobotApiFailed(reason="unauthorized")
    if status_code == 429:
        return UptimeRobotApiFailed(reason="rate_limited")
    if status_code >= 500:
        return UptimeRobotApiFailed(reason="provider_unreachable")
    return UptimeRobotApiFailed(reason="request_rejected")


async def create_or_reuse_monitor(api_key: str, render_service_url: str) -> UptimeRobotOutcome:
    """Never logs or returns api_key. Lists existing monitors first (GET)
    and only creates one (POST) if none already watches the derived target
    URL -- prevents a duplicate monitor from a repeated "Change" resubmit
    (spec section 2)."""
    target_url = _target_url(render_service_url)
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(base_url=UPTIMEROBOT_API_BASE, timeout=10.0) as client:
            list_resp = await client.get("/monitors", headers=headers, params={"limit": 50})
            list_failure = _failure_for_status(list_resp.status_code)
            if list_failure is not None:
                return list_failure
            try:
                monitors = list_resp.json().get("data") or []
            except (ValueError, AttributeError, TypeError):
                return UptimeRobotApiFailed(reason="provider_unreachable")
            if any(m.get("url") == target_url for m in monitors):
                return UptimeRobotMonitorResult(created=False)

            create_resp = await client.post(
                "/monitors",
                headers=headers,
                json={
                    "friendlyName": target_url,
                    "url": target_url,
                    "type": "HTTP",
                    "interval": _MONITOR_INTERVAL_SECONDS,
                    "timeout": _MONITOR_TIMEOUT_SECONDS,
                },
            )
    except httpx.HTTPError:
        return UptimeRobotApiFailed(reason="provider_unreachable")

    create_failure = _failure_for_status(create_resp.status_code)
    if create_failure is not None:
        return create_failure
    return UptimeRobotMonitorResult(created=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_uptimerobot_client.py -v`
Expected: PASS (12 tests)

- [ ] **Step 5: Commit**

```bash
git add onboarding/uptimerobot_client.py tests/test_onboarding_uptimerobot_client.py
git commit -m "feat: add UptimeRobot v3 client with dedupe-before-create"
```

---

### Task 2: Router endpoint

**Files:**
- Modify: `onboarding/router.py`
- Modify: `tests/test_onboarding_router.py`

**Interfaces:**
- Consumes: `onboarding.uptimerobot_client.create_or_reuse_monitor(api_key: str, render_service_url: str) -> UptimeRobotOutcome`, `UptimeRobotMonitorResult(created: bool)`, `UptimeRobotApiFailed(reason: str)` from Task 1.
- Produces: `POST /api/uptimerobot/create-monitor` — request `{api_key, render_service_url}`, response `{valid: true, created: bool}` or `{valid: false, reason}`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_onboarding_router.py` (add `uptimerobot_client` to the
existing `from onboarding import ...` line at the top so it reads
`from onboarding import github_client, llm_client, render_client, supabase_client, uptimerobot_client`):

```python
async def test_uptimerobot_monitor_created_reports_created_true(monkeypatch):
    async def fake_create(api_key, render_service_url):
        assert api_key == SENTINEL_KEY
        assert render_service_url == "https://sentinel-service.onrender.com"
        return uptimerobot_client.UptimeRobotMonitorResult(created=True)

    monkeypatch.setattr(uptimerobot_client, "create_or_reuse_monitor", fake_create)
    client = await _client()
    resp = await client.post(
        "/api/uptimerobot/create-monitor",
        json={"api_key": SENTINEL_KEY, "render_service_url": "https://sentinel-service.onrender.com"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"valid": True, "created": True}


async def test_uptimerobot_monitor_reused_reports_created_false(monkeypatch):
    async def fake_create(api_key, render_service_url):
        return uptimerobot_client.UptimeRobotMonitorResult(created=False)

    monkeypatch.setattr(uptimerobot_client, "create_or_reuse_monitor", fake_create)
    client = await _client()
    resp = await client.post(
        "/api/uptimerobot/create-monitor",
        json={"api_key": SENTINEL_KEY, "render_service_url": "https://sentinel-service.onrender.com"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"valid": True, "created": False}


async def test_uptimerobot_failure_reports_the_reason(monkeypatch):
    async def fake_create(api_key, render_service_url):
        return uptimerobot_client.UptimeRobotApiFailed(reason="unauthorized")

    monkeypatch.setattr(uptimerobot_client, "create_or_reuse_monitor", fake_create)
    client = await _client()
    resp = await client.post(
        "/api/uptimerobot/create-monitor",
        json={"api_key": SENTINEL_KEY, "render_service_url": "https://sentinel-service.onrender.com"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"valid": False, "reason": "unauthorized"}


async def test_uptimerobot_response_never_echoes_the_submitted_key(monkeypatch):
    async def fake_create(api_key, render_service_url):
        return uptimerobot_client.UptimeRobotApiFailed(reason="unauthorized")

    monkeypatch.setattr(uptimerobot_client, "create_or_reuse_monitor", fake_create)
    client = await _client()
    resp = await client.post(
        "/api/uptimerobot/create-monitor",
        json={"api_key": SENTINEL_KEY, "render_service_url": "https://sentinel-service.onrender.com"},
    )
    assert SENTINEL_KEY not in resp.text


async def test_uptimerobot_validation_error_never_echoes_the_submitted_key():
    client = await _client()
    resp = await client.post(
        "/api/uptimerobot/create-monitor",
        json={"key": SENTINEL_KEY, "render_service_url": "https://sentinel-service.onrender.com"},
    )
    assert resp.status_code == 422
    assert SENTINEL_KEY not in resp.text
    assert "input" not in resp.text


async def test_uptimerobot_empty_api_key_is_rejected():
    client = await _client()
    resp = await client.post(
        "/api/uptimerobot/create-monitor",
        json={"api_key": "", "render_service_url": "https://sentinel-service.onrender.com"},
    )
    assert resp.status_code == 422


async def test_uptimerobot_empty_render_url_is_rejected():
    client = await _client()
    resp = await client.post(
        "/api/uptimerobot/create-monitor",
        json={"api_key": SENTINEL_KEY, "render_service_url": ""},
    )
    assert resp.status_code == 422
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_router.py -k uptimerobot -v`
Expected: FAIL — `404` (no such route) on every request, and a `NameError`/
`AttributeError` for `uptimerobot_client` not being imported yet.

- [ ] **Step 3: Write the implementation**

In `onboarding/router.py`, change the import line:

```python
from onboarding import github_client, llm_client, render_client, supabase_client, uptimerobot_client
```

Add this pydantic model next to the other `Llm...Request` models:

```python
class UptimeRobotCreateMonitorRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=512)
    render_service_url: str = Field(min_length=1, max_length=2048)
```

Add this endpoint at the end of the file, after `list_vertex_models`:

```python
@router.post("/api/uptimerobot/create-monitor")
async def create_uptimerobot_monitor(payload: UptimeRobotCreateMonitorRequest) -> dict:
    result = await uptimerobot_client.create_or_reuse_monitor(
        payload.api_key, payload.render_service_url
    )
    if isinstance(result, uptimerobot_client.UptimeRobotMonitorResult):
        return {"valid": True, "created": result.created}
    return {"valid": False, "reason": result.reason}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_router.py -v`
Expected: PASS (all router tests, including the new ones)

- [ ] **Step 5: Commit**

```bash
git add onboarding/router.py tests/test_onboarding_router.py
git commit -m "feat: add POST /api/uptimerobot/create-monitor relay endpoint"
```

---

### Task 3: Frame 5 markup and JS

**Files:**
- Modify: `onboarding/static/index.html`
- Modify: `tests/test_onboarding_page.py`

**Interfaces:**
- Consumes: `POST /api/uptimerobot/create-monitor` (Task 2) — request
  `{api_key, render_service_url}`, response `{valid: true, created: bool}`
  or `{valid: false, reason}`.
- Produces: `sessionStorage["onboarding.uptimeRobot"]` holding
  `{api_key}` once frame 5 completes; reads (does not write)
  `sessionStorage["onboarding.renderServiceUrl"]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_onboarding_page.py`:

```python
async def test_frame5_has_blocked_and_form_sections():
    client = await _client()
    body = (await client.get("/")).text
    assert 'id="uptime-pinger-blocked-section"' in body
    assert 'id="uptime-pinger-form-section"' in body


async def test_frame5_has_credential_input_and_submit():
    client = await _client()
    body = (await client.get("/")).text
    assert 'id="uptime-pinger-api-key-input"' in body
    assert 'id="uptime-pinger-submit"' in body


async def test_frame5_locked_by_default():
    client = await _client()
    body = (await client.get("/")).text
    assert (
        'id="frame-uptime-pinger" class="frame" data-status="locked" '
        'data-locked="true"'
    ) in body


async def test_uptimerobot_endpoint_leaves_the_page_exactly_once():
    client = await _client()
    body = (await client.get("/")).text
    assert body.count('fetch("/api/uptimerobot/create-monitor"') == 1


async def test_frame5_blocked_state_reads_the_forward_contract_key():
    """sub-project 6 (not yet built) is obligated to write this key on its
    own completion -- see design doc section 3's forward contract. Frame 5
    only ever reads it."""
    client = await _client()
    body = (await client.get("/")).text
    assert 'const RENDER_SERVICE_URL_KEY = "onboarding.renderServiceUrl";' in body
    assert "function refreshUptimePingerBlockedState" in body
    assert "sessionStorage.getItem(RENDER_SERVICE_URL_KEY)" in body


async def test_frame5_never_persists_to_local_storage():
    client = await _client()
    body = (await client.get("/")).text
    assert 'sessionStorage.setItem(STORAGE_KEYS["uptime-pinger"]' in body
    assert 'localStorage.setItem(STORAGE_KEYS["uptime-pinger"]' not in body


async def test_uptimerobot_error_sets_frame_status_to_error():
    """Structural sibling of llmProviderError()/githubAppError() -- all
    three call setFrameStatus(id, "error") before writing the error text."""
    client = await _client()
    body = (await client.get("/")).text
    fn_start = body.index("async function submitUptimeRobotKey")
    fn_body = body[fn_start:body.index("function uptimePingerErrorKeyForReason")]
    assert fn_body.count('setFrameStatus("uptime-pinger", "error")') >= 1


async def test_unauthorized_error_mentions_the_main_api_key_requirement():
    """No server-side signal distinguishes a read-only key from an invalid
    one (design doc section 2) -- mitigated at the UI-copy level instead."""
    client = await _client()
    body = (await client.get("/")).text
    assert "Main API Key" in body


async def test_frame5_strings_present_in_both_languages():
    client = await _client()
    body = (await client.get("/")).text
    for key in (
        "frame5_instructions", "frame5_blocked_no_render_url",
        "err_uptime_empty_key", "err_uptime_unauthorized",
        "err_uptime_rate_limited", "err_uptime_unreachable",
        "err_uptime_request_rejected",
    ):
        assert f"{key}:" in body
    assert body.count("frame5_instructions:") == 2  # STRINGS.en + STRINGS.he


async def test_language_switch_retranslates_uptime_pinger_error():
    client = await _client()
    body = (await client.get("/")).text
    assert 'document.getElementById("uptime-pinger-error").textContent = t(currentUptimePingerErrorKey);' in body


async def test_frame5_has_a_reset_path_wired_into_lock_and_change():
    client = await _client()
    body = (await client.get("/")).text
    assert "function resetUptimePingerSection" in body
    assert 'if (id === "uptime-pinger") resetUptimePingerSection();' in body
    assert 'if (id === "uptime-pinger") {' in body  # beginChange's storage-clear branch


async def test_restore_from_session_completes_uptime_pinger_frame():
    client = await _client()
    body = (await client.get("/")).text
    fn_start = body.index("function restoreFromSession")
    fn_body = body[fn_start:body.index("function guardLockedFrames")]
    assert 'sessionStorage.getItem(STORAGE_KEYS["uptime-pinger"])' in fn_body
    assert 'completeFrame("uptime-pinger"' in fn_body


async def test_frame5_reevaluates_blocked_state_when_reopened():
    """Spec section 3: the frame re-checks its precondition each time it's
    reopened, not only once at unlock -- sub-project 6 may not have run yet
    the first time frame 5 unlocks, but could have by a later reopen."""
    client = await _client()
    body = (await client.get("/")).text
    assert (
        'document.getElementById("frame-uptime-pinger").addEventListener("toggle"'
        in body
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_page.py -k frame5 -v` and
`uv run pytest tests/test_onboarding_page.py -k uptime -v`
Expected: FAIL — none of the new markup/JS exists yet.

- [ ] **Step 3: Write the implementation**

In `onboarding/static/index.html`, replace the frame 5 placeholder:

```html
    <details id="frame-uptime-pinger" class="frame" data-status="locked" data-locked="true">
      <summary>
        <span class="frame-title" data-i18n="frame5_title"></span>
        <span class="frame-badge"></span>
      </summary>
      <div class="frame-body"><p data-i18n="coming_soon"></p></div>
    </details>
```

with:

```html
    <details id="frame-uptime-pinger" class="frame" data-status="locked" data-locked="true">
      <summary>
        <span class="frame-title" data-i18n="frame5_title"></span>
        <span class="frame-badge"></span>
        <button class="frame-change" type="button" data-frame="uptime-pinger" data-i18n="change_button"></button>
      </summary>
      <div class="frame-body">
        <div id="uptime-pinger-blocked-section">
          <p data-i18n="frame5_blocked_no_render_url"></p>
        </div>
        <div id="uptime-pinger-form-section" style="display: none;">
          <p data-i18n="frame5_instructions"></p>
          <input id="uptime-pinger-api-key-input" type="password">
          <button id="uptime-pinger-submit" type="button" data-i18n="validate_button"></button>
        </div>
        <p id="uptime-pinger-error" class="frame-error"></p>
      </div>
    </details>
```

Add these keys to `STRINGS.en` (next to the `frame4_*`/`err_llm_*` block):

```javascript
      frame5_instructions: "Paste your UptimeRobot Main API Key (My Settings → API Keys → Main API Key — not a Monitor-Specific key, which won't work here) to create the keep-warm monitor for your deployed service.",
      frame5_blocked_no_render_url: "Complete your Render deployment first — this step needs its URL.",
      err_uptime_empty_key: "Paste your UptimeRobot API key first.",
      err_uptime_unauthorized: "That key was rejected. Note: a Monitor-Specific key won't work here — use your Main API Key from UptimeRobot's My Settings → API Keys.",
      err_uptime_rate_limited: "Too many requests to UptimeRobot right now. Try again in a moment.",
      err_uptime_unreachable: "UptimeRobot is unreachable right now. Try again in a moment.",
      err_uptime_request_rejected: "UptimeRobot rejected that request. Try again in a moment.",
      monitor_created_detail: "monitor created",
      monitor_reused_detail: "existing monitor reused",
```

Add the matching keys to `STRINGS.he` (next to the `frame4_*`/`err_llm_*`
Hebrew block):

```javascript
      frame5_instructions: "הדביקו את מפתח ה-API הראשי שלכם ב-UptimeRobot (My Settings ← API Keys ← Main API Key — לא מפתח Monitor-Specific, שלא יעבוד כאן) כדי ליצור את הפינגר לשמירה על פעילות עבור השירות שפרסתם.",
      frame5_blocked_no_render_url: "השלימו קודם את פריסת ה-Render שלכם — שלב זה זקוק לכתובת ה-URL שלה.",
      err_uptime_empty_key: "הדביקו קודם את מפתח ה-API של UptimeRobot.",
      err_uptime_unauthorized: "המפתח נדחה. שימו לב: מפתח Monitor-Specific לא יעבוד כאן — השתמשו במפתח הראשי (Main API Key) מתוך My Settings ← API Keys ב-UptimeRobot.",
      err_uptime_rate_limited: "יותר מדי בקשות ל-UptimeRobot כרגע. נסו שוב בעוד רגע.",
      err_uptime_unreachable: "UptimeRobot אינו זמין כרגע. נסו שוב בעוד רגע.",
      err_uptime_request_rejected: "UptimeRobot דחה את הבקשה. נסו שוב בעוד רגע.",
      monitor_created_detail: "הפינגר נוצר",
      monitor_reused_detail: "נעשה שימוש בפינגר קיים",
```

Add `"uptime-pinger": "onboarding.uptimeRobot"` to `STORAGE_KEYS`:

```javascript
  const STORAGE_KEYS = {
    "render-key": "onboarding.renderApiKey",
    "github-app": "onboarding.githubApp",
    "supabase": "onboarding.supabase",
    "llm-provider": "onboarding.llmProvider",
    "uptime-pinger": "onboarding.uptimeRobot",
  };

  // Written by sub-project 6 on its own completion (not yet built) --
  // frame 5 only ever reads this key, never writes it. See
  // docs/superpowers/specs/2026-08-27-onboarding-uptimerobot-frame-design.md
  // section 3's forward contract.
  const RENDER_SERVICE_URL_KEY = "onboarding.renderServiceUrl";
```

Add `currentUptimePingerErrorKey` alongside the other `current*ErrorKey`
declarations:

```javascript
  let currentUptimePingerErrorKey = null;
```

Add the section-reset and blocked-state functions (near
`resetLlmProviderSection`):

```javascript
  function refreshUptimePingerBlockedState() {
    const hasRenderUrl = !!sessionStorage.getItem(RENDER_SERVICE_URL_KEY);
    document.getElementById("uptime-pinger-blocked-section").style.display = hasRenderUrl ? "none" : "block";
    document.getElementById("uptime-pinger-form-section").style.display = hasRenderUrl ? "block" : "none";
  }

  function resetUptimePingerSection() {
    document.getElementById("uptime-pinger-api-key-input").value = "";
    document.getElementById("uptime-pinger-error").textContent = "";
    currentUptimePingerErrorKey = null;
    refreshUptimePingerBlockedState();
  }

  async function submitUptimeRobotKey() {
    const input = document.getElementById("uptime-pinger-api-key-input");
    const key = input.value.trim();
    const errorEl = document.getElementById("uptime-pinger-error");
    errorEl.textContent = "";
    currentUptimePingerErrorKey = null;
    if (!key) {
      currentUptimePingerErrorKey = "err_uptime_empty_key";
      errorEl.textContent = t("err_uptime_empty_key");
      return;
    }
    const renderServiceUrl = sessionStorage.getItem(RENDER_SERVICE_URL_KEY);
    if (!renderServiceUrl) {
      refreshUptimePingerBlockedState();
      return;
    }
    setFrameStatus("uptime-pinger", "ready", "checking");
    let resp;
    try {
      resp = await fetch("/api/uptimerobot/create-monitor", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({api_key: key, render_service_url: renderServiceUrl}),
      });
    } catch (err) {
      setFrameStatus("uptime-pinger", "error");
      currentUptimePingerErrorKey = "err_network";
      errorEl.textContent = t("err_network");
      return;
    }
    let body;
    try {
      if (!resp.ok) throw new Error("bad status");
      body = await resp.json();
    } catch (err) {
      setFrameStatus("uptime-pinger", "error");
      currentUptimePingerErrorKey = "err_uptime_unreachable";
      errorEl.textContent = t("err_uptime_unreachable");
      return;
    }
    if (body.valid) {
      sessionStorage.setItem(STORAGE_KEYS["uptime-pinger"], JSON.stringify({api_key: key}));
      input.value = "";
      completeFrame("uptime-pinger", body.created ? "monitor_created_detail" : "monitor_reused_detail", null);
    } else {
      setFrameStatus("uptime-pinger", "error");
      currentUptimePingerErrorKey = uptimePingerErrorKeyForReason(body.reason);
      errorEl.textContent = t(currentUptimePingerErrorKey);
    }
  }

  function uptimePingerErrorKeyForReason(reason) {
    const map = {
      unauthorized: "err_uptime_unauthorized",
      rate_limited: "err_uptime_rate_limited",
      provider_unreachable: "err_uptime_unreachable",
      request_rejected: "err_uptime_request_rejected",
    };
    return map[reason] || "err_uptime_unreachable";
  }
```

Wire the reset function into `lockFrame` and `beginChange` (matching the
existing `llm-provider`/`supabase`/`github-app` hooks):

```javascript
    if (id === "llm-provider") resetLlmProviderSection();
    if (id === "uptime-pinger") resetUptimePingerSection();
```

(in `lockFrame`), and:

```javascript
    if (id === "llm-provider") {
      sessionStorage.removeItem(STORAGE_KEYS["llm-provider"]);
      resetLlmProviderSection();
    }
    if (id === "uptime-pinger") {
      sessionStorage.removeItem(STORAGE_KEYS["uptime-pinger"]);
      resetUptimePingerSection();
    }
```

(in `beginChange`).

Also add `if (id === "uptime-pinger") refreshUptimePingerBlockedState();`
right after `unlockFrame`'s existing `setFrameStatus(id, "ready");` line,
so the blocked/form choice is (re-)evaluated the moment the frame unlocks:

```javascript
  function unlockFrame(id) {
    const el = frameEl(id);
    el.dataset.locked = "false";
    setFrameStatus(id, "ready");
    if (id === "uptime-pinger") refreshUptimePingerBlockedState();
  }
```

Add the error re-render to `applyLanguage` (next to the other
`current*ErrorKey` checks):

```javascript
    if (currentUptimePingerErrorKey) {
      document.getElementById("uptime-pinger-error").textContent = t(currentUptimePingerErrorKey);
    }
```

Add the restore-on-reload check to `restoreFromSession` (next to the
`llm-provider` check):

```javascript
    if (sessionStorage.getItem(STORAGE_KEYS["llm-provider"])) {
      completeFrame("llm-provider", "restored", null);
    }
    if (sessionStorage.getItem(STORAGE_KEYS["uptime-pinger"])) {
      completeFrame("uptime-pinger", "restored", null);
    }
```

Wire the submit button and the frame's own reopen-reevaluation listener in
the `DOMContentLoaded` block:

```javascript
    document.getElementById("llm-provider-continue-submit").addEventListener("click", confirmLlmProviderModel);
    document.getElementById("uptime-pinger-submit").addEventListener("click", submitUptimeRobotKey);
    document.getElementById("frame-uptime-pinger").addEventListener("toggle", () => {
      if (document.getElementById("frame-uptime-pinger").open) {
        refreshUptimePingerBlockedState();
      }
    });
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_page.py -v`
Expected: PASS (all page tests, including every new one from Step 1)

- [ ] **Step 5: Run the full onboarding test suite**

Run: `uv run pytest tests/test_onboarding_uptimerobot_client.py tests/test_onboarding_router.py tests/test_onboarding_page.py -v`
Expected: PASS, no regressions in the frames built by earlier sub-projects.

- [ ] **Step 6: Commit**

```bash
git add onboarding/static/index.html tests/test_onboarding_page.py
git commit -m "feat: add frame 5 markup and UptimeRobot credential flow"
```

---

### Task 4: Documentation

**Files:**
- Modify: `onboarding/CLAUDE.md`

**Interfaces:**
- Consumes: nothing new — documents the module/endpoint/frame from Tasks 1-3.
- Produces: nothing consumed by later tasks; this is a leaf documentation
  update.

- [ ] **Step 1: Add "What sub-project 5 adds" section**

In `onboarding/CLAUDE.md`, add this section immediately after "## What
sub-project 4 (LLM provider credential UI) adds to these rules" and before
"## The test suite looks hung on a fresh worktree — it isn't":

```markdown
## What sub-project 5 (UptimeRobot keep-warm frame) adds to these rules

- **UptimeRobot's v3 REST API (`Bearer` auth, JSON,
  `https://api.uptimerobot.com/v3/monitors`) is used for every call this
  frame makes — never the legacy v2 form-API.** `scripts/deploy.py`'s
  existing `check_uptime_pinger` still uses v2 for its own read-only
  `getMonitors` check, and that is intentionally untouched — no reason to
  migrate a working read-only check. But v2's `POST /newMonitor` was
  verified live to reject monitor creation on a free-plan account (`403
  "You are not allowed to use some settings with your current plan"`),
  while v3 was verified live to accept the identical creation on the same
  account. Do not "simplify" this frame's client onto v2 without
  re-verifying that live behavior first.
- **This frame reads a `sessionStorage` key it does not write:
  `onboarding.renderServiceUrl`.** Sub-project 6 (Render service creation,
  not yet built as of this frame) is obligated to write the deployed
  service's base URL there on its own completion — see
  `docs/superpowers/specs/2026-08-27-onboarding-uptimerobot-frame-design.md`
  section 3's forward contract. Until sub-project 6 exists, frame 5's only
  reachable state is the blocked message (`frame5_blocked_no_render_url`)
  — this is expected, not a bug. If sub-project 6's actual output shape
  ends up different from a bare base URL when it's built, reconcile against
  that key name and format, not against a guess made here.
- **Dedupe-before-create is load-bearing, not an optimization.** Every
  credential submission to this frame (including a "Change" resubmit)
  calls `GET /v3/monitors` before ever calling `POST /v3/monitors` — a
  monitor is only created if none already watches the derived
  `<render_service_url>/healthz` target. Removing this check reintroduces
  orphaned duplicate monitors on every resubmit.
- **There is no way to detect a read-only (Monitor-Specific) API key
  server-side — verified live.** `POST /v3/monitors` and `GET
  /v3/monitors` both return the identical `401 {"message": "Invalid
  token.", "code": "003-005"}` for a valid-but-read-only key as for a
  wholly invalid one. Do not add a `reason` value implying this frame can
  tell the two apart; the only mitigation is UI copy (the input's help
  text and the `unauthorized` error both name the Main-API-Key requirement
  explicitly).
- **The monitor's `friendlyName`/`type`/`interval`/`timeout` are fixed, not
  visitor-configurable** — `friendlyName` is always the derived target URL
  itself (matching this project's own production monitor's existing
  naming), `type: "HTTP"`, `interval: 300`, `timeout: 30`. A future change
  that lets the visitor choose these needs its own brainstorm, not a quiet
  addition here.
- **`onboarding/uptimerobot_client.py` follows the same raw-`httpx`, no-SDK
  shape as `render_client.py`/`github_client.py`/`supabase_client.py`** —
  UptimeRobot has no official SDK. Tests mock via `respx`, same as those
  three modules, not the SDK-boundary mocking `llm_client.py`'s tests
  needed for `google-genai`.
```

- [ ] **Step 2: Verify the doc renders correctly**

Run: `git diff onboarding/CLAUDE.md` and read it through once — confirm the
new section sits between the Task-4 header and the "test suite looks hung"
section with no broken Markdown heading levels.

- [ ] **Step 3: Commit**

```bash
git add onboarding/CLAUDE.md
git commit -m "docs: document sub-project 5's UptimeRobot frame in onboarding/CLAUDE.md"
```
