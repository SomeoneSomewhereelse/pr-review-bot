# Onboarding wizard: Render service creation + deploy (sub-project 6, final) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the final two frames of the onboarding wizard — "Render
service" (creates the visitor's Render web service) and "Finish & Deploy"
(triggers + polls the real deploy) — and retrofit the three credential
frames between them (GitHub, Supabase, LLM) to push their own env var(s)
into that service the moment they validate, then clear the raw secret from
`sessionStorage`.

**Architecture:** Extends `onboarding/render_client.py` with four new
Render-API functions (create/push/trigger/poll), adds six new relay
endpoints to `onboarding/router.py` (one per credential-pushing frame plus
create-service/trigger-deploy/deploy-status), and modifies
`onboarding/static/index.html`'s frame-order array, two frame markups, and
three existing frames' completion handlers.

**Tech Stack:** FastAPI, httpx, pydantic — same as every prior sub-project.
No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-27-onboarding-render-service-frame-design.md`

## Global Constraints

- Push-and-clear (frames 2/3/4 push their own credential into Render
  immediately on validation, then clear the raw value from their own
  `sessionStorage` record) is load-bearing, not optional — spec section 2.
- A push failure never gates the pushing frame's own completion — spec
  section 2. If the Render-service frame was never completed (no
  `service_id` in `sessionStorage`), the push step is skipped entirely
  (no attempt, no clear) — spec section 5b.
- Six distinct endpoint URLs, one per frame — never a shared push endpoint
  — to preserve the existing per-endpoint `..._leaves_the_page_exactly_once`
  audit convention (spec section 6).
- The created service's public URL is always derived from the response's
  `service.slug` field, never the submitted `name` — verified live that
  Render's create-service response has no `service.url` field at all
  (spec sections 5a, 10).
- `GITHUB_TARGET_REPO`, `GCP_PROJECT`, `GCP_LOCATION` are never pushed
  (spec section 7).
- `onboarding/` never imports from `app/` or `scripts/` (existing
  `onboarding/CLAUDE.md` rule) — the deploy-status bucket sets
  (`_DEPLOY_IN_FLIGHT_STATUSES`/`_DEPLOY_FAILED_STATUSES`) and the
  provider-to-env-var-name mapping are **verbatim copies with a paired
  comment**, not imports, mirroring the existing `MANIFEST_PERMISSIONS`
  precedent in `index.html`.
- No live Render API calls in the test suite — `respx`-mocked only. The
  request/response shapes used below were verified live during this
  sub-project's design (spec section 10); do not "correct" them back
  toward Render's docs without re-verifying live first.
- Resubmit/orphan handling: no dedupe. Resubmitting the "Render service"
  frame via "Change" creates a new service; the old one is orphaned —
  same accepted limitation as frame 2's GitHub App creation.

---

### Task 1: `render_client.py` — create/push/trigger/poll functions

**Files:**
- Modify: `onboarding/render_client.py`
- Test: `tests/test_onboarding_render_client.py`

**Interfaces:**
- Consumes: nothing new (extends the existing module).
- Produces (for Task 2's router endpoints to call):
  - `create_service(api_key: str, repo_url: str, name: str) -> RenderServiceCreated | RenderServiceCreationFailed`
  - `push_env_vars(api_key: str, service_id: str, values: dict[str, str]) -> RenderEnvVarsPushed | RenderEnvVarsPushFailed`
  - `trigger_deploy(api_key: str, service_id: str) -> RenderDeployTriggered | RenderDeployTriggerFailed`
  - `poll_deploy_status(api_key: str, service_id: str, deploy_id: str) -> RenderDeployStatus | RenderDeployStatusFailed`
  - Dataclasses: `RenderServiceCreated(service_id: str, service_url: str)`,
    `RenderServiceCreationFailed(reason: str, message: str = "")`,
    `RenderEnvVarsPushed(pushed: list[str])`,
    `RenderEnvVarsPushFailed(reason: str, pushed: list[str])`,
    `RenderDeployTriggered(deploy_id: str)`,
    `RenderDeployTriggerFailed(reason: str)`,
    `RenderDeployStatus(status: str)` (`status` one of `"in_progress"` /
    `"live"` / `"canceled"` / `"failed"`),
    `RenderDeployStatusFailed(reason: str)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_onboarding_render_client.py`:

```python
CREATE_URL = f"{render_client.RENDER_API_BASE}/services"


def _service_created_response(service_id="srv-abc123", slug="pr-review-bot-a1b2c3d4"):
    return httpx.Response(
        201,
        json={
            "deployId": "dep-initial",
            "service": {
                "id": service_id,
                "name": slug,
                "slug": slug,
                "ownerId": "usr-1",
                "type": "web_service",
                "autoDeploy": "yes",
            },
        },
    )


async def test_create_service_returns_id_and_slug_derived_url():
    with respx.mock:
        respx.get(OWNERS_URL).mock(
            return_value=httpx.Response(200, json=[{"owner": {"id": "usr-1", "name": "Ada"}}])
        )
        respx.post(CREATE_URL).mock(return_value=_service_created_response())
        result = await render_client.create_service(SENTINEL_KEY, "https://github.com/x/y", "my-name")
    assert result == render_client.RenderServiceCreated(
        service_id="srv-abc123", service_url="https://pr-review-bot-a1b2c3d4.onrender.com"
    )


async def test_create_service_ignores_submitted_name_uses_response_slug():
    # Render may normalize the submitted name -- the returned slug differs
    # from what was submitted, and the URL must follow the slug.
    with respx.mock:
        respx.get(OWNERS_URL).mock(
            return_value=httpx.Response(200, json=[{"owner": {"id": "usr-1", "name": "Ada"}}])
        )
        respx.post(CREATE_URL).mock(
            return_value=_service_created_response(slug="my-name-normalized")
        )
        result = await render_client.create_service(SENTINEL_KEY, "https://github.com/x/y", "My Name!")
    assert result.service_url == "https://my-name-normalized.onrender.com"


async def test_create_service_invalid_owners_call_is_invalid_key():
    with respx.mock:
        respx.get(OWNERS_URL).mock(return_value=httpx.Response(401, json={"message": "nope"}))
        result = await render_client.create_service(SENTINEL_KEY, "https://github.com/x/y", "n")
    assert result == render_client.RenderServiceCreationFailed(reason="invalid_key")


async def test_create_service_401_is_invalid_key():
    with respx.mock:
        respx.get(OWNERS_URL).mock(
            return_value=httpx.Response(200, json=[{"owner": {"id": "usr-1", "name": "Ada"}}])
        )
        respx.post(CREATE_URL).mock(return_value=httpx.Response(401, json={"message": "nope"}))
        result = await render_client.create_service(SENTINEL_KEY, "https://github.com/x/y", "n")
    assert result == render_client.RenderServiceCreationFailed(reason="invalid_key")


async def test_create_service_5xx_is_unreachable():
    with respx.mock:
        respx.get(OWNERS_URL).mock(
            return_value=httpx.Response(200, json=[{"owner": {"id": "usr-1", "name": "Ada"}}])
        )
        respx.post(CREATE_URL).mock(return_value=httpx.Response(503))
        result = await render_client.create_service(SENTINEL_KEY, "https://github.com/x/y", "n")
    assert result == render_client.RenderServiceCreationFailed(reason="render_unreachable")


async def test_create_service_name_collision_is_request_rejected_with_message():
    with respx.mock:
        respx.get(OWNERS_URL).mock(
            return_value=httpx.Response(200, json=[{"owner": {"id": "usr-1", "name": "Ada"}}])
        )
        respx.post(CREATE_URL).mock(
            return_value=httpx.Response(422, json={"message": "a service with that name already exists"})
        )
        result = await render_client.create_service(SENTINEL_KEY, "https://github.com/x/y", "taken")
    assert result == render_client.RenderServiceCreationFailed(
        reason="request_rejected", message="a service with that name already exists"
    )


async def test_create_service_timeout_is_unreachable():
    with respx.mock:
        respx.get(OWNERS_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
        result = await render_client.create_service(SENTINEL_KEY, "https://github.com/x/y", "n")
    assert result == render_client.RenderServiceCreationFailed(reason="render_unreachable")


ENV_VAR_URL_A = f"{render_client.RENDER_API_BASE}/services/srv-1/env-vars/A_KEY"
ENV_VAR_URL_B = f"{render_client.RENDER_API_BASE}/services/srv-1/env-vars/B_KEY"


async def test_push_env_vars_all_succeed():
    with respx.mock:
        respx.put(ENV_VAR_URL_A).mock(return_value=httpx.Response(200))
        respx.put(ENV_VAR_URL_B).mock(return_value=httpx.Response(200))
        result = await render_client.push_env_vars(
            SENTINEL_KEY, "srv-1", {"A_KEY": "a-value", "B_KEY": "b-value"}
        )
    assert result == render_client.RenderEnvVarsPushed(pushed=["A_KEY", "B_KEY"])


async def test_push_env_vars_stops_at_first_failure_reports_partial():
    with respx.mock:
        respx.put(ENV_VAR_URL_A).mock(return_value=httpx.Response(200))
        respx.put(ENV_VAR_URL_B).mock(return_value=httpx.Response(401, json={"message": "nope"}))
        result = await render_client.push_env_vars(
            SENTINEL_KEY, "srv-1", {"A_KEY": "a-value", "B_KEY": "b-value"}
        )
    assert result == render_client.RenderEnvVarsPushFailed(reason="invalid_key", pushed=["A_KEY"])


async def test_push_env_vars_404_is_service_not_found():
    with respx.mock:
        respx.put(ENV_VAR_URL_A).mock(return_value=httpx.Response(404))
        result = await render_client.push_env_vars(SENTINEL_KEY, "srv-1", {"A_KEY": "a-value"})
    assert result == render_client.RenderEnvVarsPushFailed(reason="service_not_found", pushed=[])


async def test_push_env_vars_timeout_is_unreachable():
    with respx.mock:
        respx.put(ENV_VAR_URL_A).mock(side_effect=httpx.ConnectTimeout("timed out"))
        result = await render_client.push_env_vars(SENTINEL_KEY, "srv-1", {"A_KEY": "a-value"})
    assert result == render_client.RenderEnvVarsPushFailed(reason="render_unreachable", pushed=[])


DEPLOYS_URL = f"{render_client.RENDER_API_BASE}/services/srv-1/deploys"
DEPLOY_STATUS_URL = f"{render_client.RENDER_API_BASE}/services/srv-1/deploys/dep-1"


async def test_trigger_deploy_returns_deploy_id():
    with respx.mock:
        respx.post(DEPLOYS_URL).mock(return_value=httpx.Response(201, json={"deploy": {"id": "dep-1"}}))
        result = await render_client.trigger_deploy(SENTINEL_KEY, "srv-1")
    assert result == render_client.RenderDeployTriggered(deploy_id="dep-1")


async def test_trigger_deploy_401_is_invalid_key():
    with respx.mock:
        respx.post(DEPLOYS_URL).mock(return_value=httpx.Response(401, json={"message": "nope"}))
        result = await render_client.trigger_deploy(SENTINEL_KEY, "srv-1")
    assert result == render_client.RenderDeployTriggerFailed(reason="invalid_key")


async def test_trigger_deploy_404_is_service_not_found():
    with respx.mock:
        respx.post(DEPLOYS_URL).mock(return_value=httpx.Response(404))
        result = await render_client.trigger_deploy(SENTINEL_KEY, "srv-1")
    assert result == render_client.RenderDeployTriggerFailed(reason="service_not_found")


async def test_poll_deploy_status_live():
    with respx.mock:
        respx.get(DEPLOY_STATUS_URL).mock(
            return_value=httpx.Response(200, json={"deploy": {"id": "dep-1", "status": "live"}})
        )
        result = await render_client.poll_deploy_status(SENTINEL_KEY, "srv-1", "dep-1")
    assert result == render_client.RenderDeployStatus(status="live")


async def test_poll_deploy_status_in_flight_bucket():
    with respx.mock:
        respx.get(DEPLOY_STATUS_URL).mock(
            return_value=httpx.Response(200, json={"deploy": {"id": "dep-1", "status": "build_in_progress"}})
        )
        result = await render_client.poll_deploy_status(SENTINEL_KEY, "srv-1", "dep-1")
    assert result == render_client.RenderDeployStatus(status="in_progress")


async def test_poll_deploy_status_failed_bucket():
    with respx.mock:
        respx.get(DEPLOY_STATUS_URL).mock(
            return_value=httpx.Response(200, json={"deploy": {"id": "dep-1", "status": "build_failed"}})
        )
        result = await render_client.poll_deploy_status(SENTINEL_KEY, "srv-1", "dep-1")
    assert result == render_client.RenderDeployStatus(status="failed")


async def test_poll_deploy_status_canceled_is_not_failed():
    with respx.mock:
        respx.get(DEPLOY_STATUS_URL).mock(
            return_value=httpx.Response(200, json={"deploy": {"id": "dep-1", "status": "canceled"}})
        )
        result = await render_client.poll_deploy_status(SENTINEL_KEY, "srv-1", "dep-1")
    assert result == render_client.RenderDeployStatus(status="canceled")


async def test_poll_deploy_status_401_is_invalid_key():
    with respx.mock:
        respx.get(DEPLOY_STATUS_URL).mock(return_value=httpx.Response(401, json={"message": "nope"}))
        result = await render_client.poll_deploy_status(SENTINEL_KEY, "srv-1", "dep-1")
    assert result == render_client.RenderDeployStatusFailed(reason="invalid_key")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_render_client.py -v`
Expected: FAIL — `create_service`, `push_env_vars`, `trigger_deploy`,
`poll_deploy_status` not defined.

- [ ] **Step 3: Implement**

Add to `onboarding/render_client.py`, after the existing `validate_key`
function:

```python
# Mirrors scripts/deploy.py's own status-bucket sets VERBATIM -- onboarding/
# never imports from scripts/ or app/ (onboarding/CLAUDE.md's no-shared-
# credential-path rule), so this is a deliberate, paired-comment copy, not
# a shared import. Keep in sync with scripts/deploy.py's
# _DEPLOY_IN_FLIGHT_STATUSES / _DEPLOY_FAILED_STATUSES if either changes.
_DEPLOY_IN_FLIGHT_STATUSES = {
    "created",
    "queued",
    "build_in_progress",
    "update_in_progress",
    "pre_deploy_in_progress",
}
_DEPLOY_FAILED_STATUSES = {
    "build_failed",
    "update_failed",
    "pre_deploy_failed",
    "deactivated",
}


@dataclasses.dataclass(frozen=True)
class RenderServiceCreated:
    service_id: str
    service_url: str


@dataclasses.dataclass(frozen=True)
class RenderServiceCreationFailed:
    reason: str  # "invalid_key" | "render_unreachable" | "request_rejected"
    message: str = ""


RenderServiceCreation = RenderServiceCreated | RenderServiceCreationFailed


async def _resolve_owner_id(client: httpx.AsyncClient, api_key: str) -> str | None:
    """GET /owners, the same call validate_key() already makes -- returns
    the first owner's id, or None if the response is empty/malformed/
    unauthorized (any of which the caller maps to invalid_key)."""
    response = await client.get(
        "/owners",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    if response.status_code != 200:
        return None
    try:
        body = response.json()
        if not body:
            return None
        return body[0]["owner"]["id"]
    except (ValueError, KeyError, IndexError, TypeError):
        return None


async def create_service(api_key: str, repo_url: str, name: str) -> RenderServiceCreation:
    """Create a free-plan Docker web service from repo_url, with every env
    var left blank -- the visitor's later frames fill them in via
    push_env_vars(). Mirrors render.yaml's own buildFilter/healthCheckPath/
    dockerfilePath exactly, so a wizard-created service behaves identically
    to this project's own production deploy. Never derives the returned
    URL from `name`: Render may normalize it server-side, and a live call
    confirmed the create response carries no `service.url` field at all --
    the URL is built from the response's own `service.slug`.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(base_url=RENDER_API_BASE, timeout=30.0) as client:
            owner_id = await _resolve_owner_id(client, api_key)
            if owner_id is None:
                return RenderServiceCreationFailed(reason="invalid_key")
            response = await client.post(
                "/services",
                headers=headers,
                json={
                    "type": "web_service",
                    "name": name,
                    "ownerId": owner_id,
                    "repo": repo_url,
                    "autoDeploy": "yes",
                    "buildFilter": {"ignoredPaths": ["**/*.md"]},
                    "serviceDetails": {
                        "runtime": "docker",
                        "plan": "free",
                        "healthCheckPath": "/healthz",
                        "envSpecificDetails": {"dockerfilePath": "./Dockerfile"},
                    },
                },
            )
    except httpx.HTTPError:
        return RenderServiceCreationFailed(reason="render_unreachable")

    if response.status_code in (401, 403):
        return RenderServiceCreationFailed(reason="invalid_key")
    if response.status_code >= 500:
        return RenderServiceCreationFailed(reason="render_unreachable")
    if response.status_code not in (200, 201):
        message = ""
        try:
            message = str(response.json().get("message", ""))
        except (ValueError, AttributeError):
            pass
        return RenderServiceCreationFailed(reason="request_rejected", message=message)

    try:
        body = response.json()
        service = body.get("service") or body
        service_id = service["id"]
        slug = service["slug"]
    except (ValueError, KeyError, TypeError):
        return RenderServiceCreationFailed(reason="render_unreachable")
    return RenderServiceCreated(service_id=service_id, service_url=f"https://{slug}.onrender.com")


@dataclasses.dataclass(frozen=True)
class RenderEnvVarsPushed:
    pushed: list[str]


@dataclasses.dataclass(frozen=True)
class RenderEnvVarsPushFailed:
    reason: str  # "invalid_key" | "service_not_found" | "render_unreachable"
    pushed: list[str]


RenderEnvVarsPush = RenderEnvVarsPushed | RenderEnvVarsPushFailed


async def push_env_vars(api_key: str, service_id: str, values: dict[str, str]) -> RenderEnvVarsPush:
    """Push every (key, value) in `values`, one PUT per key -- never the
    bulk PUT /env-vars endpoint, which replaces the service's whole env-var
    list (same reasoning scripts/deploy.py::sync_env() already documents).
    Stops at the first failure and reports which keys succeeded before it;
    dict iteration order controls push order.
    """
    pushed: list[str] = []
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(base_url=RENDER_API_BASE, timeout=15.0) as client:
            for key, value in values.items():
                response = await client.put(
                    f"/services/{service_id}/env-vars/{key}",
                    headers=headers,
                    json={"value": value},
                )
                if response.status_code in (401, 403):
                    return RenderEnvVarsPushFailed(reason="invalid_key", pushed=pushed)
                if response.status_code == 404:
                    return RenderEnvVarsPushFailed(reason="service_not_found", pushed=pushed)
                if response.status_code not in (200, 201):
                    return RenderEnvVarsPushFailed(reason="render_unreachable", pushed=pushed)
                pushed.append(key)
    except httpx.HTTPError:
        return RenderEnvVarsPushFailed(reason="render_unreachable", pushed=pushed)
    return RenderEnvVarsPushed(pushed=pushed)


@dataclasses.dataclass(frozen=True)
class RenderDeployTriggered:
    deploy_id: str


@dataclasses.dataclass(frozen=True)
class RenderDeployTriggerFailed:
    reason: str  # "invalid_key" | "service_not_found" | "render_unreachable"


RenderDeployTrigger = RenderDeployTriggered | RenderDeployTriggerFailed


async def trigger_deploy(api_key: str, service_id: str) -> RenderDeployTrigger:
    """POST an empty-body deploy trigger. Pushing env vars does not
    auto-deploy (scripts/deploy.py::_trigger_and_wait's own docstring), so
    this is what actually makes the pushed values take effect."""
    try:
        async with httpx.AsyncClient(base_url=RENDER_API_BASE, timeout=15.0) as client:
            response = await client.post(
                f"/services/{service_id}/deploys",
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                json={},
            )
    except httpx.HTTPError:
        return RenderDeployTriggerFailed(reason="render_unreachable")
    if response.status_code in (401, 403):
        return RenderDeployTriggerFailed(reason="invalid_key")
    if response.status_code == 404:
        return RenderDeployTriggerFailed(reason="service_not_found")
    if response.status_code not in (200, 201):
        return RenderDeployTriggerFailed(reason="render_unreachable")
    try:
        body = response.json()
        deploy = body.get("deploy") or body
        deploy_id = deploy["id"]
    except (ValueError, KeyError, TypeError):
        return RenderDeployTriggerFailed(reason="render_unreachable")
    return RenderDeployTriggered(deploy_id=deploy_id)


@dataclasses.dataclass(frozen=True)
class RenderDeployStatus:
    status: str  # "in_progress" | "live" | "canceled" | "failed"


@dataclasses.dataclass(frozen=True)
class RenderDeployStatusFailed:
    reason: str  # "invalid_key" | "service_not_found" | "render_unreachable"


RenderDeployStatusResult = RenderDeployStatus | RenderDeployStatusFailed


async def poll_deploy_status(api_key: str, service_id: str, deploy_id: str) -> RenderDeployStatusResult:
    """One status check -- never loops or blocks itself. The browser's own
    polling loop (onboarding/static/index.html) calls the router endpoint
    wrapping this repeatedly; see design spec section 5c step 11."""
    try:
        async with httpx.AsyncClient(base_url=RENDER_API_BASE, timeout=15.0) as client:
            response = await client.get(
                f"/services/{service_id}/deploys/{deploy_id}",
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            )
    except httpx.HTTPError:
        return RenderDeployStatusFailed(reason="render_unreachable")
    if response.status_code in (401, 403):
        return RenderDeployStatusFailed(reason="invalid_key")
    if response.status_code == 404:
        return RenderDeployStatusFailed(reason="service_not_found")
    if response.status_code != 200:
        return RenderDeployStatusFailed(reason="render_unreachable")
    try:
        body = response.json()
        deploy = body.get("deploy") or body
        raw_status = deploy["status"]
    except (ValueError, KeyError, TypeError):
        return RenderDeployStatusFailed(reason="render_unreachable")
    if raw_status == "live":
        return RenderDeployStatus(status="live")
    if raw_status == "canceled":
        return RenderDeployStatus(status="canceled")
    if raw_status in _DEPLOY_FAILED_STATUSES:
        return RenderDeployStatus(status="failed")
    # Any in-flight status, and any status this project has not catalogued,
    # is reported as still in progress -- never guess "failed" for an
    # unrecognized value and stop the visitor's poll loop early.
    return RenderDeployStatus(status="in_progress")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_render_client.py -v`
Expected: PASS (all tests, old and new).

- [ ] **Step 5: Commit**

```bash
git add onboarding/render_client.py tests/test_onboarding_render_client.py
git commit -m "feat: add Render service create/push/deploy functions"
```

---

### Task 2: `router.py` — six new relay endpoints

**Files:**
- Modify: `onboarding/router.py`
- Test: `tests/test_onboarding_router.py`

**Interfaces:**
- Consumes: Task 1's `render_client.create_service`, `push_env_vars`,
  `trigger_deploy`, `poll_deploy_status` and their dataclasses.
- Produces (for Task 3/4's frontend to call):
  - `POST /api/render/create-service` — `{api_key, repo_url, name}` →
    `{valid: true, service_id, service_url}` or `{valid: false, reason,
    message?}`
  - `POST /api/github/push-render-vars` — `{render_api_key,
    render_service_id, app_id, private_key_b64, webhook_secret,
    installation_id}` → `{valid: true, pushed: [...]}` or `{valid: false,
    reason, pushed: [...]}`
  - `POST /api/supabase/push-render-var` — `{render_api_key,
    render_service_id, database_url}` → same shape
  - `POST /api/llm/push-render-vars` — `{render_api_key,
    render_service_id, provider, credential_value, model}` → same shape
  - `POST /api/render/trigger-deploy` — `{api_key, service_id}` →
    `{valid: true, deploy_id}` or `{valid: false, reason}`
  - `POST /api/render/deploy-status` — `{api_key, service_id, deploy_id}`
    → `{valid: true, status}` or `{valid: false, reason}`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_onboarding_router.py` (mirror the file's existing
`unittest.mock.patch`/`TestClient` conventions used for
`/api/render/validate-key`'s tests — check the top of the file for the
exact `client`/`monkeypatch` fixture names already in use and match them):

```python
def test_create_service_endpoint_returns_id_and_url(client, monkeypatch):
    async def fake_create_service(api_key, repo_url, name):
        return render_client.RenderServiceCreated(service_id="srv-1", service_url="https://x.onrender.com")

    monkeypatch.setattr(render_client, "create_service", fake_create_service)
    resp = client.post(
        "/api/render/create-service",
        json={"api_key": "rnd_x", "repo_url": "https://github.com/a/b", "name": "n"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"valid": True, "service_id": "srv-1", "service_url": "https://x.onrender.com"}


def test_create_service_endpoint_relays_rejection_message(client, monkeypatch):
    async def fake_create_service(api_key, repo_url, name):
        return render_client.RenderServiceCreationFailed(reason="request_rejected", message="name taken")

    monkeypatch.setattr(render_client, "create_service", fake_create_service)
    resp = client.post(
        "/api/render/create-service",
        json={"api_key": "rnd_x", "repo_url": "https://github.com/a/b", "name": "n"},
    )
    assert resp.json() == {"valid": False, "reason": "request_rejected", "message": "name taken"}


def test_github_push_render_vars_endpoint(client, monkeypatch):
    captured = {}

    async def fake_push_env_vars(api_key, service_id, values):
        captured["values"] = values
        return render_client.RenderEnvVarsPushed(pushed=list(values.keys()))

    monkeypatch.setattr(render_client, "push_env_vars", fake_push_env_vars)
    resp = client.post(
        "/api/github/push-render-vars",
        json={
            "render_api_key": "rnd_x",
            "render_service_id": "srv-1",
            "app_id": 123,
            "private_key_b64": "cGVt",
            "webhook_secret": "whsec",
            "installation_id": 456,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["valid"] is True
    assert captured["values"] == {
        "GITHUB_APP_ID": "123",
        "GITHUB_APP_PRIVATE_KEY": "cGVt",
        "GITHUB_WEBHOOK_SECRET": "whsec",
        "GITHUB_APP_INSTALLATION_ID": "456",
    }


def test_supabase_push_render_var_endpoint(client, monkeypatch):
    captured = {}

    async def fake_push_env_vars(api_key, service_id, values):
        captured["values"] = values
        return render_client.RenderEnvVarsPushed(pushed=list(values.keys()))

    monkeypatch.setattr(render_client, "push_env_vars", fake_push_env_vars)
    resp = client.post(
        "/api/supabase/push-render-var",
        json={
            "render_api_key": "rnd_x",
            "render_service_id": "srv-1",
            "database_url": "postgresql://u:p@h:5432/d",
        },
    )
    assert resp.status_code == 200
    assert captured["values"] == {"DATABASE_URL": "postgresql://u:p@h:5432/d"}


def test_llm_push_render_vars_endpoint_gemini(client, monkeypatch):
    captured = {}

    async def fake_push_env_vars(api_key, service_id, values):
        captured["values"] = values
        return render_client.RenderEnvVarsPushed(pushed=list(values.keys()))

    monkeypatch.setattr(render_client, "push_env_vars", fake_push_env_vars)
    resp = client.post(
        "/api/llm/push-render-vars",
        json={
            "render_api_key": "rnd_x",
            "render_service_id": "srv-1",
            "provider": "gemini",
            "credential_value": "AIzaSy...",
            "model": "gemini-flash-latest",
        },
    )
    assert resp.status_code == 200
    assert captured["values"] == {
        "LLM_PROVIDER": "gemini",
        "GEMINI_API_KEY": "AIzaSy...",
        "LLM_MODEL": "gemini-flash-latest",
    }


def test_llm_push_render_vars_endpoint_vertex_uses_gcp_key_name(client, monkeypatch):
    captured = {}

    async def fake_push_env_vars(api_key, service_id, values):
        captured["values"] = values
        return render_client.RenderEnvVarsPushed(pushed=list(values.keys()))

    monkeypatch.setattr(render_client, "push_env_vars", fake_push_env_vars)
    resp = client.post(
        "/api/llm/push-render-vars",
        json={
            "render_api_key": "rnd_x",
            "render_service_id": "srv-1",
            "provider": "vertex",
            "credential_value": "eyJ0eXBlIjoi...",
            "model": "gemini-2.5-pro",
        },
    )
    assert resp.status_code == 200
    assert captured["values"] == {
        "LLM_PROVIDER": "vertex",
        "GCP_SERVICE_ACCOUNT_KEY": "eyJ0eXBlIjoi...",
        "VERTEX_MODEL": "gemini-2.5-pro",
    }


def test_llm_push_render_vars_endpoint_rejects_unknown_provider(client, monkeypatch):
    resp = client.post(
        "/api/llm/push-render-vars",
        json={
            "render_api_key": "rnd_x",
            "render_service_id": "srv-1",
            "provider": "openai",
            "credential_value": "x",
            "model": "y",
        },
    )
    assert resp.status_code == 422


def test_push_render_vars_partial_failure_reports_pushed_keys(client, monkeypatch):
    async def fake_push_env_vars(api_key, service_id, values):
        return render_client.RenderEnvVarsPushFailed(reason="invalid_key", pushed=["GITHUB_APP_ID"])

    monkeypatch.setattr(render_client, "push_env_vars", fake_push_env_vars)
    resp = client.post(
        "/api/github/push-render-vars",
        json={
            "render_api_key": "rnd_x",
            "render_service_id": "srv-1",
            "app_id": 123,
            "private_key_b64": "cGVt",
            "webhook_secret": "whsec",
            "installation_id": 456,
        },
    )
    assert resp.json() == {"valid": False, "reason": "invalid_key", "pushed": ["GITHUB_APP_ID"]}


def test_trigger_deploy_endpoint(client, monkeypatch):
    async def fake_trigger_deploy(api_key, service_id):
        return render_client.RenderDeployTriggered(deploy_id="dep-1")

    monkeypatch.setattr(render_client, "trigger_deploy", fake_trigger_deploy)
    resp = client.post("/api/render/trigger-deploy", json={"api_key": "rnd_x", "service_id": "srv-1"})
    assert resp.json() == {"valid": True, "deploy_id": "dep-1"}


def test_deploy_status_endpoint(client, monkeypatch):
    async def fake_poll_deploy_status(api_key, service_id, deploy_id):
        return render_client.RenderDeployStatus(status="live")

    monkeypatch.setattr(render_client, "poll_deploy_status", fake_poll_deploy_status)
    resp = client.post(
        "/api/render/deploy-status",
        json={"api_key": "rnd_x", "service_id": "srv-1", "deploy_id": "dep-1"},
    )
    assert resp.json() == {"valid": True, "status": "live"}
```

Check the existing test file's top for its `render_client` import and
`client`/fixture setup, and add `from onboarding import render_client` if
not already imported at module scope.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_router.py -v -k "render or push_render or trigger_deploy or deploy_status"`
Expected: FAIL — 404s, the new routes don't exist yet.

- [ ] **Step 3: Implement**

Add to `onboarding/router.py`. First, new pydantic models (insert after the
existing `UptimeRobotCreateMonitorRequest` class):

```python
class RenderServiceCreateRequest(BaseModel):
    api_key: str = Field(max_length=512)
    repo_url: str = Field(min_length=1, max_length=512)
    name: str = Field(min_length=1, max_length=64)


class RenderPushEnvVarsRequest(BaseModel):
    render_api_key: str = Field(max_length=512)
    render_service_id: str = Field(min_length=1, max_length=64)


class GithubPushRenderVarsRequest(RenderPushEnvVarsRequest):
    app_id: int = Field(gt=0)
    private_key_b64: str = Field(max_length=16384)
    webhook_secret: str = Field(max_length=512)
    installation_id: int = Field(gt=0)


class SupabasePushRenderVarRequest(RenderPushEnvVarsRequest):
    database_url: str = Field(min_length=1, max_length=2048)


class LlmPushRenderVarsRequest(RenderPushEnvVarsRequest):
    provider: str = Field(pattern=r"^(gemini|groq|vertex)$")
    credential_value: str = Field(min_length=1, max_length=16384)
    model: str = Field(min_length=1, max_length=256)


class RenderTriggerDeployRequest(BaseModel):
    api_key: str = Field(max_length=512)
    service_id: str = Field(min_length=1, max_length=64)


class RenderDeployStatusRequest(BaseModel):
    api_key: str = Field(max_length=512)
    service_id: str = Field(min_length=1, max_length=64)
    deploy_id: str = Field(min_length=1, max_length=64)


# Paired comment with app/providers/registry.py::PROVIDERS -- onboarding/
# never imports from app/ (onboarding/CLAUDE.md's no-shared-credential-
# path rule), so this 3-entry mapping is a deliberate copy, not a shared
# import. Keep in sync if a provider's env var names ever change there.
_LLM_ENV_VAR_NAMES = {
    "gemini": ("GEMINI_API_KEY", "LLM_MODEL"),
    "groq": ("GROQ_API_KEY", "GROQ_MODEL"),
    "vertex": ("GCP_SERVICE_ACCOUNT_KEY", "VERTEX_MODEL"),
}
```

Then, new endpoints (append at the end of the file, after the existing
`create_uptimerobot_monitor`):

```python
@router.post("/api/render/create-service")
async def create_render_service(payload: RenderServiceCreateRequest) -> dict:
    result = await render_client.create_service(payload.api_key, payload.repo_url, payload.name)
    if isinstance(result, render_client.RenderServiceCreated):
        return {"valid": True, "service_id": result.service_id, "service_url": result.service_url}
    if result.message:
        return {"valid": False, "reason": result.reason, "message": result.message}
    return {"valid": False, "reason": result.reason}


def _push_result(result) -> dict:
    if isinstance(result, render_client.RenderEnvVarsPushed):
        return {"valid": True, "pushed": result.pushed}
    return {"valid": False, "reason": result.reason, "pushed": result.pushed}


@router.post("/api/github/push-render-vars")
async def push_github_render_vars(payload: GithubPushRenderVarsRequest) -> dict:
    result = await render_client.push_env_vars(
        payload.render_api_key,
        payload.render_service_id,
        {
            "GITHUB_APP_ID": str(payload.app_id),
            "GITHUB_APP_PRIVATE_KEY": payload.private_key_b64,
            "GITHUB_WEBHOOK_SECRET": payload.webhook_secret,
            "GITHUB_APP_INSTALLATION_ID": str(payload.installation_id),
        },
    )
    return _push_result(result)


@router.post("/api/supabase/push-render-var")
async def push_supabase_render_var(payload: SupabasePushRenderVarRequest) -> dict:
    result = await render_client.push_env_vars(
        payload.render_api_key, payload.render_service_id, {"DATABASE_URL": payload.database_url}
    )
    return _push_result(result)


@router.post("/api/llm/push-render-vars")
async def push_llm_render_vars(payload: LlmPushRenderVarsRequest) -> dict:
    credential_var, model_var = _LLM_ENV_VAR_NAMES[payload.provider]
    result = await render_client.push_env_vars(
        payload.render_api_key,
        payload.render_service_id,
        {
            "LLM_PROVIDER": payload.provider,
            credential_var: payload.credential_value,
            model_var: payload.model,
        },
    )
    return _push_result(result)


@router.post("/api/render/trigger-deploy")
async def trigger_render_deploy(payload: RenderTriggerDeployRequest) -> dict:
    result = await render_client.trigger_deploy(payload.api_key, payload.service_id)
    if isinstance(result, render_client.RenderDeployTriggered):
        return {"valid": True, "deploy_id": result.deploy_id}
    return {"valid": False, "reason": result.reason}


@router.post("/api/render/deploy-status")
async def get_render_deploy_status(payload: RenderDeployStatusRequest) -> dict:
    result = await render_client.poll_deploy_status(payload.api_key, payload.service_id, payload.deploy_id)
    if isinstance(result, render_client.RenderDeployStatus):
        return {"valid": True, "status": result.status}
    return {"valid": False, "reason": result.reason}
```

The `provider` field's `Field(pattern=r"^(gemini|groq|vertex)$")` already
guarantees `payload.provider` is a key of `_LLM_ENV_VAR_NAMES` — no
`.get()`/`None` check needed before the dict-unpacking line.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_router.py -v`
Expected: PASS (all tests, old and new).

- [ ] **Step 5: Commit**

```bash
git add onboarding/router.py tests/test_onboarding_router.py
git commit -m "feat: add Render service create/push/deploy relay endpoints"
```

---

### Task 3: `index.html` — the two new frames

**Files:**
- Modify: `onboarding/static/index.html`
- Test: `tests/test_onboarding_page.py`

**Interfaces:**
- Consumes: Task 2's 3 new Render endpoints (`create-service`,
  `trigger-deploy`, `deploy-status`); existing `STORAGE_KEYS["render-key"]`,
  `completeFrame`/`lockFrame`/`unlockFrame`/`beginChange`/`setFrameStatus`/
  `t()`/`FRAME_ORDER`/`readStoredGithubApp`-style helpers.
- Produces (for Task 4 to consume):
  - `STORAGE_KEYS["render-service"] = "onboarding.renderService"` holding
    `{service_id, service_url, pending_deploy_id?, deployed?}`.
  - `RENDER_SERVICE_URL_KEY` (already exists) now actually gets written.
  - `function readStoredRenderService()` — used by Task 4's push helpers.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_onboarding_page.py` (match the file's existing
`body = ...` fixture / content-substring style used for frame 1's markup
and JS tests):

```python
def test_frame_order_includes_render_service_after_render_key():
    assert '"render-key", "render-service", "github-app"' in body


def test_frame_order_ends_with_render_deploy():
    assert '"uptime-pinger", "render-deploy"' in body


def test_render_service_frame_markup_present():
    assert 'id="frame-render-service"' in body
    assert 'id="render-service-repo-input"' in body
    assert 'id="render-service-name-input"' in body
    assert 'id="render-service-submit"' in body


def test_render_service_storage_key_present():
    assert '"render-service": "onboarding.renderService"' in body


def test_create_service_fetch_leaves_the_page_exactly_once():
    assert body.count('fetch("/api/render/create-service"') == 1


def test_render_service_url_written_on_success():
    assert "sessionStorage.setItem(RENDER_SERVICE_URL_KEY, body.service_url)" in body


def test_render_deploy_frame_markup_present():
    assert 'id="render-deploy-trigger-section"' in body
    assert 'id="render-deploy-polling-section"' in body
    assert 'id="render-deploy-done-section"' in body
    assert 'id="render-deploy-trigger-submit"' in body
    assert 'id="render-deploy-check-again-submit"' in body


def test_trigger_deploy_fetch_leaves_the_page_exactly_once():
    assert body.count('fetch("/api/render/trigger-deploy"') == 1


def test_deploy_status_fetch_leaves_the_page_exactly_once():
    assert body.count('fetch("/api/render/deploy-status"') == 1


def test_render_service_frame_i18n_strings_present_in_both_languages():
    keys = [
        "frame_render_service_title", "frame_render_service_instructions",
        "frame_render_service_repo_label", "frame_render_service_name_label",
        "create_service_button", "url_prefix", "err_render_service_no_key",
        "err_render_service_empty", "err_render_service_invalid_key",
        "err_render_service_unreachable", "err_render_service_rejected",
    ]
    for key in keys:
        assert body.count(f'{key}:') == 2, f"{key} should appear once in en and once in he"


def test_render_deploy_frame_i18n_strings_present_in_both_languages():
    keys = [
        "frame6_instructions", "frame6_polling", "frame6_done", "deploy_button",
        "err_render_deploy_no_service", "err_render_deploy_invalid_key",
        "err_render_deploy_service_not_found", "err_render_deploy_unreachable",
        "err_render_deploy_failed", "err_render_deploy_timeout",
    ]
    for key in keys:
        assert body.count(f'{key}:') == 2, f"{key} should appear once in en and once in he"


def test_frame_titles_renumbered_after_render_service_insertion():
    assert 'frame_render_service_title: "2. Render service"' in body
    assert 'frame2_title: "3. GitHub App"' in body
    assert 'frame3_title: "4. Supabase database"' in body
    assert 'frame4_title: "5. LLM provider"' in body
    assert 'frame5_title: "6. Keep-warm pinger"' in body
    assert 'frame6_title: "7. Finish & Deploy"' in body


def test_begin_change_render_service_clears_its_own_stale_state():
    assert 'if (id === "render-service")' in body
    assert 'sessionStorage.removeItem(STORAGE_KEYS["render-service"])' in body
    assert 'sessionStorage.removeItem(RENDER_SERVICE_URL_KEY)' in body


def test_lock_frame_resets_render_deploy_section():
    assert 'if (id === "render-deploy") resetRenderDeploySection();' in body
```

`body` here is the fixture already used throughout this test file (the
served page's HTML/JS as one string) — do not redefine it; if the file
uses a different fixture name, match it exactly.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_page.py -v -k "render_service or render_deploy or frame_order or renumbered or begin_change_render or lock_frame_resets"`
Expected: FAIL — none of this markup/JS exists yet.

- [ ] **Step 3: Implement**

**3a. `FRAME_ORDER` and `STORAGE_KEYS`** (`onboarding/static/index.html`,
existing `FRAME_ORDER`/`STORAGE_KEYS` declarations found earlier in this
task's investigation, around the frame-state-machine section):

```javascript
  const STORAGE_KEYS = {
    "render-key": "onboarding.renderApiKey",
    "render-service": "onboarding.renderService",
    "github-app": "onboarding.githubApp",
    "supabase": "onboarding.supabase",
    "llm-provider": "onboarding.llmProvider",
    "uptime-pinger": "onboarding.uptimeRobot",
  };
```

```javascript
  const FRAME_ORDER = [
    "render-key", "render-service", "github-app", "supabase", "llm-provider",
    "uptime-pinger", "render-deploy",
  ];
```

Add a `readStoredRenderService()` helper next to the existing
`readStoredGithubApp()`/`readStoredSupabase()` functions:

```javascript
  function readStoredRenderService() {
    try {
      return JSON.parse(sessionStorage.getItem(STORAGE_KEYS["render-service"]) || "null");
    } catch (err) {
      return null;
    }
  }
```

**3b. `lockFrame` and `beginChange` additions:**

In `lockFrame(id)`, add a matching branch alongside the existing
`if (id === "github-app") ...` lines:

```javascript
    if (id === "render-deploy") resetRenderDeploySection();
```

In `beginChange(id)`, add a matching branch alongside the existing
`if (id === "github-app") { ... }` block (same file location, same
reasoning as that block's own comment — a frame changing itself keeps its
own stale value otherwise):

```javascript
    if (id === "render-service") {
      sessionStorage.removeItem(STORAGE_KEYS["render-service"]);
      sessionStorage.removeItem(RENDER_SERVICE_URL_KEY);
    }
```

**3c. Frame "Render service" markup** — insert immediately after
`</details>` closing `frame-render-key` and before `<details
id="frame-github-app"...>`:

```html
    <details id="frame-render-service" class="frame" data-status="locked" data-locked="true">
      <summary>
        <span class="frame-title" data-i18n="frame_render_service_title"></span>
        <span class="frame-badge"></span>
        <button class="frame-change" type="button" data-frame="render-service" data-i18n="change_button"></button>
      </summary>
      <div class="frame-body">
        <p data-i18n="frame_render_service_instructions"></p>
        <label data-i18n="frame_render_service_repo_label"></label>
        <input id="render-service-repo-input" type="text">
        <label data-i18n="frame_render_service_name_label"></label>
        <input id="render-service-name-input" type="text">
        <button id="render-service-submit" type="button" data-i18n="create_service_button"></button>
        <p id="render-service-error" class="frame-error"></p>
      </div>
    </details>
```

**3d. Frame "Render service" JS** — add near `validateRenderKey` (same
section of the file):

```javascript
  let currentRenderServiceErrorKey = null;

  function generateRenderServiceName() {
    const bytes = crypto.getRandomValues(new Uint8Array(4));
    const hex = Array.from(bytes).map((b) => b.toString(16).padStart(2, "0")).join("");
    return `pr-review-bot-${hex}`;
  }

  const RENDER_SERVICE_DEFAULT_REPO = "https://github.com/TovTechOrg/pr-review-bot";

  function prefillRenderServiceDefaults() {
    const repoInput = document.getElementById("render-service-repo-input");
    const nameInput = document.getElementById("render-service-name-input");
    if (!repoInput.value) repoInput.value = RENDER_SERVICE_DEFAULT_REPO;
    if (!nameInput.value) nameInput.value = generateRenderServiceName();
  }

  async function createRenderService() {
    const repoInput = document.getElementById("render-service-repo-input");
    const nameInput = document.getElementById("render-service-name-input");
    const errorEl = document.getElementById("render-service-error");
    errorEl.textContent = "";
    currentRenderServiceErrorKey = null;
    const repoUrl = repoInput.value.trim();
    const name = nameInput.value.trim();
    const apiKey = sessionStorage.getItem(STORAGE_KEYS["render-key"]);
    if (!apiKey) {
      currentRenderServiceErrorKey = "err_render_service_no_key";
      errorEl.textContent = t("err_render_service_no_key");
      return;
    }
    if (!repoUrl || !name) {
      currentRenderServiceErrorKey = "err_render_service_empty";
      errorEl.textContent = t("err_render_service_empty");
      return;
    }
    setFrameStatus("render-service", "ready", "checking");
    let resp;
    try {
      resp = await fetch("/api/render/create-service", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({api_key: apiKey, repo_url: repoUrl, name: name}),
      });
    } catch (err) {
      setFrameStatus("render-service", "error");
      currentRenderServiceErrorKey = "err_network";
      errorEl.textContent = t("err_network");
      return;
    }
    let body;
    try {
      if (!resp.ok) throw new Error("bad status");
      body = await resp.json();
    } catch (err) {
      setFrameStatus("render-service", "error");
      currentRenderServiceErrorKey = "err_render_service_unreachable";
      errorEl.textContent = t("err_render_service_unreachable");
      return;
    }
    if (body.valid) {
      sessionStorage.setItem(STORAGE_KEYS["render-service"], JSON.stringify({
        service_id: body.service_id, service_url: body.service_url,
      }));
      sessionStorage.setItem(RENDER_SERVICE_URL_KEY, body.service_url);
      completeFrame("render-service", "url_prefix", body.service_url);
    } else if (body.reason === "invalid_key") {
      setFrameStatus("render-service", "error");
      currentRenderServiceErrorKey = "err_render_service_invalid_key";
      errorEl.textContent = t("err_render_service_invalid_key");
    } else if (body.reason === "request_rejected") {
      setFrameStatus("render-service", "error");
      currentRenderServiceErrorKey = "err_render_service_rejected";
      errorEl.textContent = body.message
        ? `${t("err_render_service_rejected")} ${body.message}`
        : t("err_render_service_rejected");
    } else {
      setFrameStatus("render-service", "error");
      currentRenderServiceErrorKey = "err_render_service_unreachable";
      errorEl.textContent = t("err_render_service_unreachable");
    }
  }
```

In `unlockFrame(id)`, add a matching branch alongside the existing
`if (id === "uptime-pinger") refreshUptimePingerBlockedState();`:

```javascript
    if (id === "render-service") prefillRenderServiceDefaults();
```

**3e. Frame "Finish & Deploy" markup** — replace the existing placeholder
`<details id="frame-render-deploy" ...>...</details>` block entirely with:

```html
    <details id="frame-render-deploy" class="frame" data-status="locked" data-locked="true">
      <summary>
        <span class="frame-title" data-i18n="frame6_title"></span>
        <span class="frame-badge"></span>
      </summary>
      <div class="frame-body">
        <div id="render-deploy-trigger-section">
          <p data-i18n="frame6_instructions"></p>
          <button id="render-deploy-trigger-submit" type="button" data-i18n="deploy_button"></button>
        </div>
        <div id="render-deploy-polling-section" style="display: none;">
          <p data-i18n="frame6_polling"></p>
          <button id="render-deploy-check-again-submit" type="button" data-i18n="check_again_button" style="display: none;"></button>
        </div>
        <div id="render-deploy-done-section" style="display: none;">
          <p data-i18n="frame6_done"></p>
          <a id="render-deploy-service-link" href="#" target="_blank" rel="noopener"></a>
        </div>
        <p id="render-deploy-error" class="frame-error"></p>
      </div>
    </details>
```

**3f. Frame "Finish & Deploy" JS** — add near frame 3's polling functions
(`fetchProjectStatus`/`handleProjectStatusResult`/`pollUntilReady`), which
this deliberately mirrors:

```javascript
  let currentRenderDeployErrorKey = null;
  const RENDER_DEPLOY_POLL_INTERVAL_MS = 10000;
  const RENDER_DEPLOY_POLL_TIMEOUT_MS = 900000;

  function resetRenderDeploySection() {
    document.getElementById("render-deploy-trigger-section").style.display = "block";
    document.getElementById("render-deploy-polling-section").style.display = "none";
    document.getElementById("render-deploy-done-section").style.display = "none";
    document.getElementById("render-deploy-check-again-submit").style.display = "none";
    document.getElementById("render-deploy-error").textContent = "";
    currentRenderDeployErrorKey = null;
  }

  function renderDeployError(key) {
    setFrameStatus("render-deploy", "error");
    currentRenderDeployErrorKey = key;
    document.getElementById("render-deploy-error").textContent = t(key);
  }

  function renderDeployErrorForReason(reason) {
    const map = {
      invalid_key: "err_render_deploy_invalid_key",
      service_not_found: "err_render_deploy_service_not_found",
      render_unreachable: "err_render_deploy_unreachable",
    };
    renderDeployError(map[reason] || "err_render_deploy_unreachable");
  }

  async function fetchRenderDeployStatus(serviceId, deployId) {
    const apiKey = sessionStorage.getItem(STORAGE_KEYS["render-key"]);
    if (!apiKey) return {valid: false, reason: "invalid_key"};
    let resp;
    try {
      resp = await fetch("/api/render/deploy-status", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({api_key: apiKey, service_id: serviceId, deploy_id: deployId}),
      });
    } catch (err) {
      return {networkError: true};
    }
    try {
      if (!resp.ok) throw new Error("bad status");
      return await resp.json();
    } catch (err) {
      return {valid: false, reason: "render_unreachable"};
    }
  }

  function finishRenderDeploy() {
    const stored = readStoredRenderService();
    document.getElementById("render-deploy-polling-section").style.display = "none";
    document.getElementById("render-deploy-done-section").style.display = "block";
    const link = document.getElementById("render-deploy-service-link");
    link.href = stored ? stored.service_url : "";
    link.textContent = stored ? stored.service_url : "";
    if (stored) {
      stored.deployed = true;
      sessionStorage.setItem(STORAGE_KEYS["render-service"], JSON.stringify(stored));
    }
    completeFrame("render-deploy", null, null);
  }

  function handleRenderDeployStatusResult(body) {
    if (body.networkError) {
      renderDeployError("err_network");
      return "error";
    }
    if (!body.valid) {
      renderDeployErrorForReason(body.reason);
      return "error";
    }
    if (body.status === "live") {
      finishRenderDeploy();
      return "live";
    }
    if (body.status === "failed") {
      document.getElementById("render-deploy-polling-section").style.display = "none";
      renderDeployError("err_render_deploy_failed");
      return "error";
    }
    // "in_progress" and "canceled" both keep polling: canceled means a
    // newer deploy superseded this one, not a failure (same distinction
    // scripts/deploy.py draws), so the right response is to keep
    // watching, not to stop.
    return "pending";
  }

  async function pollRenderDeployStatus(serviceId, deployId, startedAt) {
    const body = await fetchRenderDeployStatus(serviceId, deployId);
    const outcome = handleRenderDeployStatusResult(body);
    if (outcome !== "pending") return;
    if (Date.now() - startedAt >= RENDER_DEPLOY_POLL_TIMEOUT_MS) {
      renderDeployError("err_render_deploy_timeout");
      document.getElementById("render-deploy-check-again-submit").style.display = "inline-block";
      return;
    }
    setTimeout(() => { pollRenderDeployStatus(serviceId, deployId, startedAt); }, RENDER_DEPLOY_POLL_INTERVAL_MS);
  }

  async function checkRenderDeployStatusOnce() {
    const stored = readStoredRenderService();
    if (!stored || !stored.service_id || !stored.pending_deploy_id) return;
    document.getElementById("render-deploy-error").textContent = "";
    currentRenderDeployErrorKey = null;
    const body = await fetchRenderDeployStatus(stored.service_id, stored.pending_deploy_id);
    const outcome = handleRenderDeployStatusResult(body);
    if (outcome === "pending") {
      renderDeployError("err_render_deploy_timeout");
      document.getElementById("render-deploy-check-again-submit").style.display = "inline-block";
    }
  }

  async function triggerRenderDeploy() {
    document.getElementById("render-deploy-error").textContent = "";
    currentRenderDeployErrorKey = null;
    const apiKey = sessionStorage.getItem(STORAGE_KEYS["render-key"]);
    const stored = readStoredRenderService();
    if (!apiKey || !stored || !stored.service_id) {
      renderDeployError("err_render_deploy_no_service");
      return;
    }
    let resp;
    try {
      resp = await fetch("/api/render/trigger-deploy", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({api_key: apiKey, service_id: stored.service_id}),
      });
    } catch (err) {
      renderDeployError("err_network");
      return;
    }
    let body;
    try {
      if (!resp.ok) throw new Error("bad status");
      body = await resp.json();
    } catch (err) {
      renderDeployError("err_render_deploy_unreachable");
      return;
    }
    if (!body.valid) {
      renderDeployErrorForReason(body.reason);
      return;
    }
    stored.pending_deploy_id = body.deploy_id;
    sessionStorage.setItem(STORAGE_KEYS["render-service"], JSON.stringify(stored));
    document.getElementById("render-deploy-trigger-section").style.display = "none";
    document.getElementById("render-deploy-polling-section").style.display = "block";
    pollRenderDeployStatus(stored.service_id, body.deploy_id, Date.now());
  }
```

**3g. Event listeners** — in the `DOMContentLoaded` handler, add alongside
the existing `.addEventListener("click", ...)` lines:

```javascript
    document.getElementById("render-service-submit").addEventListener("click", createRenderService);
    document.getElementById("render-deploy-trigger-submit").addEventListener("click", triggerRenderDeploy);
    document.getElementById("render-deploy-check-again-submit").addEventListener("click", checkRenderDeployStatusOnce);
```

**3h. `restoreFromSession()` additions** — add after the existing
`render-key` restore block and before the `github-app` restore block:

```javascript
    if (sessionStorage.getItem(STORAGE_KEYS["render-service"])) {
      completeFrame("render-service", "url_prefix", (readStoredRenderService() || {}).service_url);
    }
```

And at the very end of the function (after the existing `uptime-pinger`
restore block), add the render-deploy restore/resume logic:

```javascript
    const renderServiceState = readStoredRenderService();
    if (renderServiceState && renderServiceState.deployed) {
      completeFrame("render-deploy", null, null);
    } else if (renderServiceState && renderServiceState.pending_deploy_id) {
      document.getElementById("render-deploy-trigger-section").style.display = "none";
      document.getElementById("render-deploy-polling-section").style.display = "block";
      pollRenderDeployStatus(renderServiceState.service_id, renderServiceState.pending_deploy_id, Date.now());
    }
```

**3i. `applyLanguage()` addition** — add alongside the existing
`if (currentRenderKeyErrorKey) { ... }` lines:

```javascript
    if (currentRenderServiceErrorKey) {
      document.getElementById("render-service-error").textContent = t(currentRenderServiceErrorKey);
    }
    if (currentRenderDeployErrorKey) {
      document.getElementById("render-deploy-error").textContent = t(currentRenderDeployErrorKey);
    }
```

**3j. `STRINGS` additions** — in the `en:` block, change the existing
title lines (frame numbers shift by one, since the new frame becomes
"2."):

```javascript
      frame2_title: "3. GitHub App",
      frame3_title: "4. Supabase database",
      frame4_title: "5. LLM provider",
      frame5_title: "6. Keep-warm pinger",
      frame6_title: "7. Finish & Deploy",
```

And add these new keys anywhere in the `en:` block:

```javascript
      frame_render_service_title: "2. Render service",
      frame_render_service_instructions: "This creates your own Render web service from the project's repository. You can point it at a different repo or change the name below if you want.",
      frame_render_service_repo_label: "Repository",
      frame_render_service_name_label: "Service name",
      create_service_button: "Create service",
      url_prefix: "",
      err_render_service_no_key: "Complete step 1 (Render API key) first.",
      err_render_service_empty: "Fill in a repository URL and a service name.",
      err_render_service_invalid_key: "That key was rejected by Render. Double-check it and try again.",
      err_render_service_unreachable: "Render's API is unreachable right now. Try again in a moment.",
      err_render_service_rejected: "Render rejected this request:",
      frame6_instructions: "Everything is captured. Click Deploy to push it live.",
      frame6_polling: "Deploying… this can take a few minutes.",
      frame6_done: "Your instance is live at:",
      deploy_button: "Deploy",
      err_render_deploy_no_service: "Complete step 2 (Render service) first.",
      err_render_deploy_invalid_key: "That key was rejected by Render. Double-check it and try again.",
      err_render_deploy_service_not_found: "Your Render service could not be found. Try step 2 again.",
      err_render_deploy_unreachable: "Render's API is unreachable right now. Try again in a moment.",
      err_render_deploy_failed: "The deploy failed. Check your service on Render's dashboard for details.",
      err_render_deploy_timeout: "Still deploying after 15 minutes. It may still finish — check again.",
```

In the `he:` block, change the existing title lines:

```javascript
      frame2_title: "3. אפליקציית GitHub",
      frame3_title: "4. מסד נתונים ב-Supabase",
      frame4_title: "5. ספק LLM",
      frame5_title: "6. פינגר לשמירה על פעילות",
      frame6_title: "7. סיום ופריסה",
```

And add these new keys anywhere in the `he:` block:

```javascript
      frame_render_service_title: "2. שירות Render",
      frame_render_service_instructions: "פעולה זו יוצרת שירות Render משלך מתוך מאגר הקוד של הפרויקט. ניתן להצביע על מאגר אחר או לשנות את השם למטה אם רוצים.",
      frame_render_service_repo_label: "מאגר קוד",
      frame_render_service_name_label: "שם השירות",
      create_service_button: "צור שירות",
      url_prefix: "",
      err_render_service_no_key: "השלם קודם את שלב 1 (מפתח API של Render).",
      err_render_service_empty: "מלא כתובת מאגר קוד ושם שירות.",
      err_render_service_invalid_key: "המפתח נדחה על ידי Render. בדוק אותו ונסה שוב.",
      err_render_service_unreachable: "ה-API של Render אינו זמין כרגע. נסה שוב בעוד רגע.",
      err_render_service_rejected: "Render דחה את הבקשה:",
      frame6_instructions: "הכול נאסף. לחץ על פרוס כדי להעלות לאוויר.",
      frame6_polling: "מבצע פריסה… זה עשוי לקחת כמה דקות.",
      frame6_done: "המערכת שלך פעילה בכתובת:",
      deploy_button: "פרוס",
      err_render_deploy_no_service: "השלם קודם את שלב 2 (שירות Render).",
      err_render_deploy_invalid_key: "המפתח נדחה על ידי Render. בדוק אותו ונסה שוב.",
      err_render_deploy_service_not_found: "לא נמצא שירות Render. נסה שוב את שלב 2.",
      err_render_deploy_unreachable: "ה-API של Render אינו זמין כרגע. נסה שוב בעוד רגע.",
      err_render_deploy_failed: "הפריסה נכשלה. בדוק את השירות בלוח הבקרה של Render לפרטים.",
      err_render_deploy_timeout: "עדיין בפריסה אחרי 15 דקות. ייתכן שתסתיים בהמשך — בדוק שוב.",
```

`owner_prefix`'s existing `en`/`he` entries already demonstrate the
"`<label>: value`" badge-detail convention `url_prefix` follows (an empty
string, so the badge reads simply as the URL itself with no extra label
text — check `owner_prefix`'s actual value first; if it is non-empty
prose like `"owner: "`, match that shape instead of an empty string, e.g.
`url_prefix: "live at "` / he equivalent).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_page.py -v`
Expected: PASS (all tests, old and new — including every pre-existing
`frame2_title`/`frame3_title`/etc. substring test; check for any existing
test asserting the OLD numbered title text, e.g. `"2. GitHub App"`, and
update it to the new numbering in the same commit).

- [ ] **Step 5: Commit**

```bash
git add onboarding/static/index.html tests/test_onboarding_page.py
git commit -m "feat: add Render-service-creation and Finish-and-Deploy frames"
```

---

### Task 4: Push-and-clear retrofit into frames 2/3/4

**Files:**
- Modify: `onboarding/static/index.html`
- Test: `tests/test_onboarding_page.py`

**Interfaces:**
- Consumes: Task 2's `/api/github/push-render-vars`,
  `/api/supabase/push-render-var`, `/api/llm/push-render-vars`; Task 3's
  `readStoredRenderService()`.
- Produces: nothing new for later tasks — this is the last code task.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_onboarding_page.py`:

```python
def test_github_push_render_vars_fetch_leaves_the_page_exactly_once():
    assert body.count('fetch("/api/github/push-render-vars"') == 1


def test_supabase_push_render_var_fetch_leaves_the_page_exactly_once():
    assert body.count('fetch("/api/supabase/push-render-var"') == 1


def test_llm_push_render_vars_fetch_leaves_the_page_exactly_once():
    assert body.count('fetch("/api/llm/push-render-vars"') == 1


def test_github_push_helper_clears_secret_fields_not_account_login():
    assert "delete stored.private_key_b64;" in body
    assert "delete stored.webhook_secret;" in body


def test_supabase_push_helper_clears_connection_string():
    assert "delete stored.connection_string;" in body


def test_supabase_restore_uses_completed_flag_not_connection_string():
    assert "supabaseState.completed" in body
    assert "supabaseState.connection_string" not in body


def test_llm_push_helper_clears_credential_fields():
    assert "delete stored.api_key;" in body
    assert "delete stored.gcp_service_account_key_b64;" in body


def test_push_helpers_skip_entirely_without_a_render_service():
    assert "if (!renderService || !renderService.service_id || !renderApiKey) return;" in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_page.py -v -k "push_render or push_helper or supabase_restore"`
Expected: FAIL — none of this exists yet.

- [ ] **Step 3: Implement**

**4a. GitHub push helper** — add near `handleGithubInstallCallback`:

```javascript
  async function pushGithubAppToRenderService(stored) {
    const renderService = readStoredRenderService();
    const renderApiKey = sessionStorage.getItem(STORAGE_KEYS["render-key"]);
    if (!renderService || !renderService.service_id || !renderApiKey) return;
    try {
      await fetch("/api/github/push-render-vars", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          render_api_key: renderApiKey,
          render_service_id: renderService.service_id,
          app_id: stored.app_id,
          private_key_b64: stored.private_key_b64,
          webhook_secret: stored.webhook_secret,
          installation_id: stored.installation_id,
        }),
      });
    } catch (err) {
      // best-effort -- a network failure still counts as "attempted"
      // (design spec section 5b step 8); the fields below are cleared
      // either way, and this frame's completion is never gated on it.
    }
    delete stored.private_key_b64;
    delete stored.webhook_secret;
  }
```

Modify `handleGithubInstallCallback`'s success branch (currently):

```javascript
    if (body.valid) {
      stored.installation_id = parseInt(installationId, 10);
      stored.account_login = body.account_login;
      sessionStorage.setItem(STORAGE_KEYS["github-app"], JSON.stringify(stored));
      completeFrame("github-app", "account_prefix", body.account_login);
    } else if (body.reason === "installation_not_found") {
```

to:

```javascript
    if (body.valid) {
      stored.installation_id = parseInt(installationId, 10);
      stored.account_login = body.account_login;
      await pushGithubAppToRenderService(stored);
      sessionStorage.setItem(STORAGE_KEYS["github-app"], JSON.stringify(stored));
      completeFrame("github-app", "account_prefix", body.account_login);
    } else if (body.reason === "installation_not_found") {
```

(`handleGithubInstallCallback` is already declared `async`; no signature
change needed.)

**4b. Supabase push helper** — add near `fetchSupabaseConnectionInfo`:

```javascript
  async function pushSupabaseToRenderService(stored) {
    const renderService = readStoredRenderService();
    const renderApiKey = sessionStorage.getItem(STORAGE_KEYS["render-key"]);
    if (!renderService || !renderService.service_id || !renderApiKey) return;
    try {
      await fetch("/api/supabase/push-render-var", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          render_api_key: renderApiKey,
          render_service_id: renderService.service_id,
          database_url: stored.connection_string,
        }),
      });
    } catch (err) {
      // best-effort -- see pushGithubAppToRenderService's comment above.
    }
    delete stored.connection_string;
  }
```

Modify `fetchSupabaseConnectionInfo`'s tail (currently):

```javascript
    stored.connection_string =
      `postgresql://${body.db_user}:${stored.db_pass}@${body.db_host}:${body.db_port}/${body.db_name}`;
    delete stored.db_pass;
    sessionStorage.setItem(STORAGE_KEYS["supabase"], JSON.stringify(stored));
    completeFrame("supabase", "project_prefix", stored.name);
```

to:

```javascript
    stored.connection_string =
      `postgresql://${body.db_user}:${stored.db_pass}@${body.db_host}:${body.db_port}/${body.db_name}`;
    delete stored.db_pass;
    await pushSupabaseToRenderService(stored);
    stored.completed = true;
    sessionStorage.setItem(STORAGE_KEYS["supabase"], JSON.stringify(stored));
    completeFrame("supabase", "project_prefix", stored.name);
```

`stored.completed` is a new, non-secret marker: since
`pushSupabaseToRenderService` deletes `connection_string` (the value
`restoreFromSession`'s Supabase branch previously used to detect
completion), that branch must switch to checking `completed` instead —
update `restoreFromSession`'s existing:

```javascript
    const supabaseState = readStoredSupabase();
    if (supabaseState && supabaseState.connection_string) {
      completeFrame("supabase", "restored", null);
    } else if (supabaseState && supabaseState.ref) {
```

to:

```javascript
    const supabaseState = readStoredSupabase();
    if (supabaseState && supabaseState.completed) {
      completeFrame("supabase", "restored", null);
    } else if (supabaseState && supabaseState.ref) {
```

**4c. LLM provider push helper** — add near `confirmLlmProviderModel`:

```javascript
  async function pushLlmProviderToRenderService(stored) {
    const renderService = readStoredRenderService();
    const renderApiKey = sessionStorage.getItem(STORAGE_KEYS["render-key"]);
    if (!renderService || !renderService.service_id || !renderApiKey) return;
    const credentialValue = stored.provider === "vertex"
      ? stored.gcp_service_account_key_b64
      : stored.api_key;
    try {
      await fetch("/api/llm/push-render-vars", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          render_api_key: renderApiKey,
          render_service_id: renderService.service_id,
          provider: stored.provider,
          credential_value: credentialValue,
          model: stored.model,
        }),
      });
    } catch (err) {
      // best-effort -- see pushGithubAppToRenderService's comment above.
    }
    delete stored.api_key;
    delete stored.gcp_service_account_key_b64;
  }
```

Modify `confirmLlmProviderModel` (currently):

```javascript
  function confirmLlmProviderModel() {
    const select = document.getElementById("llm-provider-model-select");
    const model = select.value;
    if (!model || !pendingLlmProviderCredential) {
      llmProviderError("err_llm_no_model_selected");
      return;
    }
    const stored = {...pendingLlmProviderCredential, model};
    try {
      sessionStorage.setItem(STORAGE_KEYS["llm-provider"], JSON.stringify(stored));
    } catch (err) {
      llmProviderError("err_llm_storage_failed");
      return;
    }
    completeFrame("llm-provider", "provider_prefix", stored.provider);
  }
```

to:

```javascript
  async function confirmLlmProviderModel() {
    const select = document.getElementById("llm-provider-model-select");
    const model = select.value;
    if (!model || !pendingLlmProviderCredential) {
      llmProviderError("err_llm_no_model_selected");
      return;
    }
    const stored = {...pendingLlmProviderCredential, model};
    await pushLlmProviderToRenderService(stored);
    try {
      sessionStorage.setItem(STORAGE_KEYS["llm-provider"], JSON.stringify(stored));
    } catch (err) {
      llmProviderError("err_llm_storage_failed");
      return;
    }
    completeFrame("llm-provider", "provider_prefix", stored.provider);
  }
```

(Note the `function` → `async function` change — the click-listener
registration in `DOMContentLoaded` already just passes the function
reference, so no change needed there.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_page.py -v`
Expected: PASS (all tests, old and new).

- [ ] **Step 5: Run the FULL test suite**

Run: `uv run pytest`
Expected: PASS, 0 failures.

- [ ] **Step 6: Commit**

```bash
git add onboarding/static/index.html tests/test_onboarding_page.py
git commit -m "feat: push-and-clear GitHub/Supabase/LLM credentials into the Render service"
```

---

### Task 5: `onboarding/CLAUDE.md` documentation

**Files:**
- Modify: `onboarding/CLAUDE.md`

**Interfaces:**
- Consumes: nothing (documentation only).
- Produces: nothing (last task).

- [ ] **Step 1: Append the new section**

Append to the end of `onboarding/CLAUDE.md` (after the existing "What
sub-project 5 ... adds" section and before "The test suite looks hung on a
fresh worktree" section — insert immediately above that section, not
after it, since that section is itself a trailing environment note, not
part of the numbered sub-project history):

```markdown
## What sub-project 6 (Render service creation + deploy, final) adds to these rules

- **What was originally decomposed as one "sub-project 6" frame is
  actually two frames**: "Render service" (position 2, right after the
  Render-key frame) creates the service; the pre-existing placeholder
  `frame-render-deploy` (reserved since sub-project 1's shell, always
  last in `FRAME_ORDER`) is "Finish & Deploy" — triggers the real deploy
  once frames 2-5 have run. Do not conflate them or try to merge them
  back into one frame; the accordion's sequential-lock model is why
  they're split (see
  `docs/superpowers/specs/2026-08-27-onboarding-render-service-frame-design.md`
  section 3).
- **Frames 2, 3, and 4 each push their own credential into the
  already-created Render service the moment they validate, then clear the
  raw value from their own `sessionStorage` record.** This is a
  deliberate security property (shrinking a credential's browser-residency
  window), not an optional optimization — do not defer a new frame's push
  to "do it all at the end" without a fresh brainstorm justifying the
  regression.
- **A push failure never blocks the pushing frame's own completion.**
  Pushing to Render is best-effort persistence; only the final "Finish &
  Deploy" frame is genuinely blocked by a missing service. See design
  spec section 2's push-failure-handling decision before changing this.
- **Six new relay endpoints, one frame each** — never route two different
  frames' pushes through one shared endpoint URL, even though the
  underlying `render_client.push_env_vars` call is shared. A shared
  endpoint would break the per-endpoint
  `..._leaves_the_page_exactly_once` audit's page-wide `== 1` count
  across multiple frames.
- **The created service's public URL is always derived from Render's
  returned `service.slug`, never the submitted `name`.** Render may
  normalize the name server-side; a create-service response was verified
  live to have no `service.url` field at all — trusting the requested
  name instead of the response's slug would silently point
  `onboarding.renderServiceUrl` (frame 5's forward contract) at a URL
  that doesn't exist.
- **`GITHUB_TARGET_REPO`, `GCP_PROJECT`, and `GCP_LOCATION` are
  deliberately never pushed** — track-all mode and this project's own
  matching defaults (`app/config.py`'s `gcp_location` default already
  equals `onboarding/llm_client.py`'s fixed `_VERTEX_LOCATION` constant,
  verified) make an explicit push redundant. Do not add them without a
  concrete reason a default has drifted.
- **Deploy status polling reuses `scripts/deploy.py`'s own
  `_DEPLOY_IN_FLIGHT_STATUSES`/`_DEPLOY_FAILED_STATUSES` status-bucket
  sets as a verbatim, paired-comment copy in `render_client.py`** — never
  an import (`onboarding/` never imports from `scripts/` or `app/`, per
  this file's own no-shared-credential-path rule). Keep the two in sync
  by hand if either changes; `router.py`'s `_LLM_ENV_VAR_NAMES` mapping
  is the same pattern, paired with `app/providers/registry.py::PROVIDERS`.
- **Frame 5 (UptimeRobot)'s "blocked, no Render URL" state is no longer
  reachable in normal sequential flow** — the "Render service" frame now
  writes `onboarding.renderServiceUrl` two frames before UptimeRobot
  unlocks. The blocked-state markup and its check function are
  unchanged and NOT dead code: they remain a correctness safeguard for a
  corrupted or manually-manipulated `sessionStorage` state, not something
  this sub-project needed or was asked to remove.
```

- [ ] **Step 2: Commit**

```bash
git add onboarding/CLAUDE.md
git commit -m "docs: document sub-project 6's additions to onboarding/CLAUDE.md"
```

---

### Task 6: Correct the GitHub App's webhook URL (post-final-review addition)

**Why this task exists:** the final whole-branch review of Tasks 1-5 found
a Critical gap this plan's original scope silently dropped. Sub-project 2's
design spec (`docs/superpowers/specs/2026-08-26-onboarding-github-app-frame-design.md`
section 3, step 2) deliberately left the GitHub App's webhook pointed at a
placeholder (`https://example.invalid/webhook`) and explicitly assigned the
correction to this sub-project: *"corrected then, mirroring
`scripts/deploy.py`'s existing 'points here — set only if wrong' pattern for
the exact same field."* Without this task, a wizard-deployed instance
reaches "live" but never receives a single GitHub webhook event — the
wizard's whole purpose (a working reviewer bot) silently fails to be
delivered. This task closes that gap using the exact API call
`app/github_app.py::set_webhook_url` already makes for the operator's own
deploy, built independently (never imported) for the visitor's own App
credentials, mirroring `onboarding/github_client.py::verify_installation`'s
existing App-JWT client-construction pattern in the same file.

**Files:**
- Modify: `onboarding/github_client.py`
- Modify: `onboarding/router.py`
- Modify: `onboarding/static/index.html`
- Test: `tests/test_onboarding_github_client.py`
- Test: `tests/test_onboarding_router.py`
- Test: `tests/test_onboarding_page.py`

**Interfaces:**
- Consumes: `onboarding/render_client.py`'s existing `readStoredRenderService()`
  (Task 3) for the deployed service's `service_url`; the existing
  `handleGithubInstallCallback` function and `stored`/`readStoredGithubApp()`
  shape (`{app_id, slug, private_key_b64, webhook_secret, installation_id,
  account_login}`) frame 2 already builds.
- Produces: `github_client.set_webhook_url(app_id, private_key_b64, url) ->
  WebhookUrlSet | WebhookUrlSetFailed`; `POST /api/github/set-webhook-url`;
  nothing later in this plan depends on this task (it is the last one).

- [ ] **Step 1: Write the failing backend tests**

Append to `tests/test_onboarding_github_client.py` (reuses the file's
existing `fake_transport`/`_throwaway_key_material`/
`_no_pygithub_rate_limit_sleep` fixtures — do not redefine them):

```python
async def test_set_webhook_url_succeeds(fake_transport, _throwaway_key_material):
    fake_transport.route("PATCH", "/app/hook/config", {"url": "https://x.onrender.com/webhook"})
    result = await github_client.set_webhook_url(
        app_id=999, private_key_b64=_throwaway_key_material, url="https://x.onrender.com/webhook"
    )
    assert result == github_client.WebhookUrlSet()


async def test_set_webhook_url_unauthorized_is_invalid_credentials(fake_transport, _throwaway_key_material):
    fake_transport.route("PATCH", "/app/hook/config", {"message": "Bad credentials"}, 401)
    result = await github_client.set_webhook_url(
        app_id=999, private_key_b64=_throwaway_key_material, url="https://x.onrender.com/webhook"
    )
    assert result == github_client.WebhookUrlSetFailed(reason="invalid_credentials")


async def test_set_webhook_url_not_found_is_invalid_credentials(fake_transport, _throwaway_key_material):
    fake_transport.route("PATCH", "/app/hook/config", {"message": "Not Found"}, 404)
    result = await github_client.set_webhook_url(
        app_id=999, private_key_b64=_throwaway_key_material, url="https://x.onrender.com/webhook"
    )
    assert result == github_client.WebhookUrlSetFailed(reason="invalid_credentials")


async def test_set_webhook_url_server_error_is_unreachable(fake_transport, _throwaway_key_material):
    fake_transport.route("PATCH", "/app/hook/config", {}, 500)
    result = await github_client.set_webhook_url(
        app_id=999, private_key_b64=_throwaway_key_material, url="https://x.onrender.com/webhook"
    )
    assert result == github_client.WebhookUrlSetFailed(reason="github_unreachable")


async def test_set_webhook_url_malformed_base64_private_key_is_invalid_credentials():
    result = await github_client.set_webhook_url(
        app_id=999, private_key_b64="not-valid-base64!!", url="https://x.onrender.com/webhook"
    )
    assert result == github_client.WebhookUrlSetFailed(reason="invalid_credentials")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_github_client.py -v -k set_webhook_url`
Expected: FAIL — `set_webhook_url`/`WebhookUrlSet`/`WebhookUrlSetFailed` not
defined.

- [ ] **Step 3: Implement `github_client.py`**

Add to `onboarding/github_client.py`, after the existing `verify_installation`
function:

```python
@dataclasses.dataclass(frozen=True)
class WebhookUrlSet:
    pass


@dataclasses.dataclass(frozen=True)
class WebhookUrlSetFailed:
    reason: str  # "invalid_credentials" | "github_unreachable"


WebhookUrlResult = WebhookUrlSet | WebhookUrlSetFailed


def _set_webhook_url(app_id: int, private_key_pem: str, url: str) -> None:
    """Blocking PyGithub call — run via asyncio.to_thread by the caller.
    Same App-JWT-only client construction as _fetch_installation above
    (never app/github_app.py's operator-tied helpers — onboarding/CLAUDE.md's
    no-shared-credential-path rule). Mirrors app/github_app.py::
    set_webhook_url's own PATCH /app/hook/config call shape exactly — this
    is the visitor-credential equivalent of the same operation."""
    gh = Github(auth=Auth.AppAuth(app_id, private_key_pem))
    gh.requester.requestJsonAndCheck("PATCH", "/app/hook/config", input={"url": url})


async def set_webhook_url(app_id: int, private_key_b64: str, url: str) -> WebhookUrlResult:
    """Point the visitor's App's webhook at `url` (their just-deployed
    Render service's own /webhook path). Never logs the private key, in
    full or truncated — same sensitivity tier as verify_installation's."""
    try:
        private_key_pem = base64.b64decode(private_key_b64, validate=True).decode()
    except (binascii.Error, ValueError):
        return WebhookUrlSetFailed(reason="invalid_credentials")

    try:
        await asyncio.to_thread(_set_webhook_url, app_id, private_key_pem, url)
    except GithubException as exc:
        if exc.status in (401, 403, 404):
            return WebhookUrlSetFailed(reason="invalid_credentials")
        return WebhookUrlSetFailed(reason="github_unreachable")
    except (ValueError, jwt.exceptions.InvalidKeyError):
        # Same base64-valid-but-non-PEM case verify_installation's own
        # docstring explains: PyJWT re-raises cryptography's ValueError as
        # InvalidKeyError while signing the App JWT.
        return WebhookUrlSetFailed(reason="invalid_credentials")
    except requests.exceptions.RequestException:
        return WebhookUrlSetFailed(reason="github_unreachable")

    return WebhookUrlSet()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_github_client.py -v`
Expected: PASS (all tests, old and new).

- [ ] **Step 5: Write the failing router test**

Append to `tests/test_onboarding_router.py`:

```python
def test_set_github_webhook_url_endpoint(client, monkeypatch):
    async def fake_set_webhook_url(app_id, private_key_b64, url):
        return github_client.WebhookUrlSet()

    monkeypatch.setattr(github_client, "set_webhook_url", fake_set_webhook_url)
    resp = client.post(
        "/api/github/set-webhook-url",
        json={"app_id": 123, "private_key_b64": "cGVt", "url": "https://x.onrender.com/webhook"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"valid": True}


def test_set_github_webhook_url_endpoint_failure(client, monkeypatch):
    async def fake_set_webhook_url(app_id, private_key_b64, url):
        return github_client.WebhookUrlSetFailed(reason="invalid_credentials")

    monkeypatch.setattr(github_client, "set_webhook_url", fake_set_webhook_url)
    resp = client.post(
        "/api/github/set-webhook-url",
        json={"app_id": 123, "private_key_b64": "cGVt", "url": "https://x.onrender.com/webhook"},
    )
    assert resp.json() == {"valid": False, "reason": "invalid_credentials"}
```

`github_client` is already imported at module scope in this test file
(check the existing imports before adding a duplicate).

- [ ] **Step 6: Implement the router endpoint**

Add to `onboarding/router.py`, a new pydantic model alongside
`GithubInstallVerifyRequest`:

```python
class GithubSetWebhookUrlRequest(BaseModel):
    app_id: int = Field(gt=0)
    private_key_b64: str = Field(max_length=16384)
    url: str = Field(min_length=1, max_length=2048)
```

And a new endpoint, alongside `verify_github_installation`:

```python
@router.post("/api/github/set-webhook-url")
async def set_github_webhook_url(payload: GithubSetWebhookUrlRequest) -> dict:
    result = await github_client.set_webhook_url(payload.app_id, payload.private_key_b64, payload.url)
    if isinstance(result, github_client.WebhookUrlSet):
        return {"valid": True}
    return {"valid": False, "reason": result.reason}
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_router.py -v`
Expected: PASS.

- [ ] **Step 8: Commit the backend**

```bash
git add onboarding/github_client.py onboarding/router.py tests/test_onboarding_github_client.py tests/test_onboarding_router.py
git commit -m "feat: add GitHub App webhook-URL-correction relay endpoint"
```

- [ ] **Step 9: Write the failing frontend tests**

Append to `tests/test_onboarding_page.py`:

```python
def test_set_webhook_url_fetch_leaves_the_page_exactly_once():
    assert body.count('fetch("/api/github/set-webhook-url"') == 1


def test_webhook_retry_section_markup_present():
    assert 'id="github-app-webhook-retry-section"' in body
    assert 'id="github-app-webhook-retry-submit"' in body


def test_webhook_set_gates_frame_completion_before_push_and_clear():
    # setGithubWebhookUrl must be awaited, and its failure path must return
    # before pushGithubAppToRenderService/completeFrame ever run -- this is
    # the ordering that keeps the private key available for a retry.
    assert "await finishGithubAppSetup(stored, body.account_login);" in body
    assert "if (!webhookResult.ok) {" in body


def test_webhook_retry_i18n_strings_present_in_both_languages():
    keys = [
        "frame2_webhook_retry_instructions", "retry_button",
        "err_github_webhook_invalid_credentials", "err_github_webhook_unreachable",
    ]
    for key in keys:
        assert body.count(f'{key}:') == 2, f"{key} should appear once in en and once in he"
```

- [ ] **Step 10: Run tests to verify they fail**

Run: `uv run pytest tests/test_onboarding_page.py -v -k webhook`
Expected: FAIL — none of this exists yet.

- [ ] **Step 11: Implement the frontend**

**11a. Markup** — inside `frame-github-app`'s `frame-body` div, add a third
sibling section after the existing `github-app-install-section` div and
before the closing `<p id="github-app-error" ...>`:

```html
        <div id="github-app-webhook-retry-section" style="display: none;">
          <p data-i18n="frame2_webhook_retry_instructions"></p>
          <button id="github-app-webhook-retry-submit" type="button" data-i18n="retry_button"></button>
        </div>
```

**11b. `resetGithubAppCreateSection`** — add the new section to the reset
(currently hides only `github-app-install-section`):

```javascript
  function resetGithubAppCreateSection() {
    document.getElementById("github-app-install-section").style.display = "none";
    document.getElementById("github-app-webhook-retry-section").style.display = "none";
    document.getElementById("github-app-create-section").style.display = "block";
    document.getElementById("github-app-error").textContent = "";
    currentGithubAppErrorKey = null;
  }
```

**11c. New JS functions** — add near `pushGithubAppToRenderService` (Task
4's addition):

```javascript
  async function setGithubWebhookUrl(stored) {
    const renderService = readStoredRenderService();
    if (!renderService || !renderService.service_url) {
      return {ok: false, reason: "github_unreachable"};
    }
    let resp;
    try {
      resp = await fetch("/api/github/set-webhook-url", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          app_id: stored.app_id,
          private_key_b64: stored.private_key_b64,
          url: `${renderService.service_url}/webhook`,
        }),
      });
    } catch (err) {
      return {ok: false, reason: "github_unreachable"};
    }
    let body;
    try {
      if (!resp.ok) throw new Error("bad status");
      body = await resp.json();
    } catch (err) {
      return {ok: false, reason: "github_unreachable"};
    }
    if (!body.valid) return {ok: false, reason: body.reason};
    return {ok: true};
  }

  function showGithubWebhookRetry(reason) {
    document.getElementById("github-app-install-section").style.display = "none";
    document.getElementById("github-app-webhook-retry-section").style.display = "block";
    githubAppError(
      reason === "invalid_credentials"
        ? "err_github_webhook_invalid_credentials"
        : "err_github_webhook_unreachable"
    );
  }

  async function finishGithubAppSetup(stored, accountLogin) {
    const webhookResult = await setGithubWebhookUrl(stored);
    if (!webhookResult.ok) {
      showGithubWebhookRetry(webhookResult.reason);
      return;
    }
    await pushGithubAppToRenderService(stored);
    sessionStorage.setItem(STORAGE_KEYS["github-app"], JSON.stringify(stored));
    completeFrame("github-app", "account_prefix", accountLogin);
  }

  async function retryGithubWebhookSetup() {
    const stored = readStoredGithubApp();
    if (!stored || !stored.account_login) return;
    document.getElementById("github-app-error").textContent = "";
    currentGithubAppErrorKey = null;
    await finishGithubAppSetup(stored, stored.account_login);
  }
```

**11d. Rewire `handleGithubInstallCallback`'s success branch** — this is
Task 4's version (already on this branch); change it from:

```javascript
    if (body.valid) {
      stored.installation_id = parseInt(installationId, 10);
      stored.account_login = body.account_login;
      await pushGithubAppToRenderService(stored);
      sessionStorage.setItem(STORAGE_KEYS["github-app"], JSON.stringify(stored));
      completeFrame("github-app", "account_prefix", body.account_login);
    } else if (body.reason === "installation_not_found") {
```

to:

```javascript
    if (body.valid) {
      stored.installation_id = parseInt(installationId, 10);
      stored.account_login = body.account_login;
      sessionStorage.setItem(STORAGE_KEYS["github-app"], JSON.stringify(stored));
      await finishGithubAppSetup(stored, body.account_login);
    } else if (body.reason === "installation_not_found") {
```

(The private key is deliberately still in `stored`/`sessionStorage` at this
point — `finishGithubAppSetup` needs it for the webhook call, and only
`pushGithubAppToRenderService`, called from inside `finishGithubAppSetup`
after a successful webhook-set, clears it.)

**11e. Event listener** — in `DOMContentLoaded`, add:

```javascript
    document.getElementById("github-app-webhook-retry-submit").addEventListener("click", retryGithubWebhookSetup);
```

**11f. STRINGS additions** — `en:` block:

```javascript
      frame2_webhook_retry_instructions: "Your App and installation are set up, but pointing its webhook at your new instance failed. Click Retry to try again.",
      retry_button: "Retry",
      err_github_webhook_invalid_credentials: "GitHub rejected the webhook update. Your App's credentials may have changed — try the Change action above.",
      err_github_webhook_unreachable: "GitHub's API is unreachable right now. Try again in a moment.",
```

`he:` block:

```javascript
      frame2_webhook_retry_instructions: "האפליקציה וההתקנה הוגדרו בהצלחה, אך הפניית ה-webhook למופע החדש נכשלה. לחץ על נסה שוב.",
      retry_button: "נסה שוב",
      err_github_webhook_invalid_credentials: "GitHub דחה את עדכון ה-webhook. ייתכן שפרטי האפליקציה השתנו — נסה את פעולת השינוי למעלה.",
      err_github_webhook_unreachable: "ה-API של GitHub אינו זמין כרגע. נסה שוב בעוד רגע.",
```

- [ ] **Step 12: Run tests to verify they pass**

Run: `uv run pytest tests/test_onboarding_page.py -v`
Expected: PASS (all tests, old and new).

- [ ] **Step 13: Run the FULL test suite**

Run: `uv run pytest`
Expected: PASS, 0 failures.

- [ ] **Step 14: Commit the frontend**

```bash
git add onboarding/static/index.html tests/test_onboarding_page.py
git commit -m "feat: correct GitHub App webhook URL after Render deploy, with a retry path"
```

- [ ] **Step 15: Append a short note to `onboarding/CLAUDE.md`**

Add one bullet to the end of the existing "What sub-project 6 ... adds to
these rules" section (Task 5's addition):

```markdown
- **The GitHub App's webhook URL is corrected in frame 3 (GitHub), not
  in the "Render service" or "Finish & Deploy" frames**, even though the
  correction logically depends on the Render service already existing.
  This is the one point in the whole flow where the private key (needed to
  sign the webhook-update's App JWT) and the deployed service URL are both
  available at once — `pushGithubAppToRenderService`'s later push-and-clear
  step deletes the private key, so the webhook correction must happen
  first. A failed webhook-set does NOT push-and-clear or complete the
  frame; it shows a retry affordance instead, since retrying the whole
  GitHub install flow is not otherwise reachable from that state.
```

```bash
git add onboarding/CLAUDE.md
git commit -m "docs: note webhook-URL-correction ordering in onboarding/CLAUDE.md"
```

---

## Self-Review Notes (for the controller running this plan)

- **Spec coverage:** sections 4-9 of the design spec map to Tasks 1-4;
  section 11 maps to Task 5. Section 12 (out of scope) has no
  corresponding task, correctly.
- **Type consistency check performed:** `RenderServiceCreated.service_id`
  (Task 1) matches `service_id` used in Task 2's endpoint, Task 3's
  `sessionStorage` write, and Task 4's push-helper reads throughout.
  `STORAGE_KEYS["render-service"]` (Task 3) is the single write/read point
  Task 4's three push helpers and Task 3's own restore/poll logic all
  share via `readStoredRenderService()` — no second, differently-named
  key was introduced anywhere.
- **Verified during plan-writing:** `tests/test_onboarding_page.py` has no
  existing assertion on the old frame-title numbering (searched for
  `frame2_title`/`frame3_title`/etc. and the literal old strings — none
  found), so Task 3's renumbering cannot break a pre-existing test.
- **Placeholder scan:** no `TBD`/`TODO`/"fill in details"/"similar to
  Task N" patterns found in this plan.
