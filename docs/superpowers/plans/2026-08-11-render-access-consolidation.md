# Render Access-Layer Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the Render-API-access functions duplicated across
`scripts/deploy.py` and `scripts/set_provider.py` into one shared module,
`scripts/_render.py`, with no behavior change.

**Architecture:** `scripts/_render.py` becomes the sole owner of
`headers()`, `unwrap()`, `find_service_id()`, `env_vars()`, `RENDER_API`, and
`HTTP_TIMEOUT`. `scripts/deploy.py` and `scripts/set_provider.py` import it
as `from scripts import _render` and call these as `_render.<name>()` —
no aliasing, no back-compat shims. `deploy.py` keeps its own
`_resolved_provider()` / `_resolved_provider_or_env()` / `_PROVIDERS`
unmoved.

**Tech Stack:** Python, `httpx`, `respx` (test mocking), `pytest`.

## Global Constraints

- No behavior, error message, or exit-code change anywhere (pure refactor;
  spec §1).
- `_PROVIDERS`, `_resolved_provider()`, `_resolved_provider_or_env()` stay in
  `scripts/deploy.py`, unmoved (spec §2, §7).
- No back-compat aliasing: call sites and tests are updated to the new
  module path directly (spec §2).
- `scripts/_render.py`'s `find_service_id()` and `headers()` take no
  parameters — they read `settings.render_service_name` /
  `settings.render_api_key` directly, exactly like today (spec §3).
- `scripts/_render.py` gets its own `HTTP_TIMEOUT = 10.0`, independent of
  `deploy.py`'s `_HTTP_TIMEOUT` (used by the unrelated UptimeRobot check)
  (spec §3).
- `env_vars()` keeps its existing "never log a fetched value" docstring
  invariant verbatim (spec §3, CLAUDE.md "no secret is ever logged").
- After the change, grepping `scripts/` and `tests/` for `_render_headers`,
  `_unwrap(`, `_find_render_service_id`, `_render_env_vars`, `_RENDER_API`
  must return zero hits (spec §8).

---

## File Structure

- **Create:** `scripts/_render.py` — shared Render API access (headers,
  unwrap, find_service_id, env_vars, RENDER_API, HTTP_TIMEOUT).
- **Create:** `tests/test_render_client.py` — direct unit tests for the new
  module.
- **Modify:** `scripts/deploy.py` — remove the four moved functions and
  `_RENDER_API`; import and call through `_render`.
- **Modify:** `scripts/set_provider.py` — import `_render` instead of
  reaching into `bot.scripts.deploy`'s internals for the Render-fetch functions.
- **Modify:** `tests/test_deploy_script.py` — retarget 10 monkeypatches from
  `deploy` to `deploy._render`; remove the one test that moves to
  `test_render_client.py`.
- **No change:** `tests/test_set_provider_script.py` (confirmed in spec §6 —
  it never monkeypatches the moved functions directly).

---

### Task 1: Create `scripts/_render.py` with its own tests

**Files:**
- Create: `scripts/_render.py`
- Create: `tests/test_render_client.py`

**Interfaces:**
- Produces: `bot.scripts._render.RENDER_API: str`,
  `bot.scripts._render.HTTP_TIMEOUT: float`,
  `bot.scripts._render.headers() -> dict[str, str]`,
  `bot.scripts._render.unwrap(item: dict, key: str) -> dict`,
  `bot.scripts._render.find_service_id() -> str | None`,
  `bot.scripts._render.env_vars(service_id: str) -> dict[str, str]`.
  Tasks 2 and 3 consume all six.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_render_client.py`:

```python
"""Direct unit tests for scripts/_render.py, the shared Render API access
module used by scripts/deploy.py and scripts/set_provider.py."""

from __future__ import annotations

import httpx
import respx

from app.config import settings
from scripts import _render

RENDER_SERVICES = "https://api.render.com/v1/services"


def test_find_service_id_returns_the_matching_service(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "pr-review-engine")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(
            return_value=httpx.Response(
                200,
                json=[{"service": {"id": "srv-1", "name": "pr-review-engine"}}],
            )
        )
        assert _render.find_service_id() == "srv-1"


def test_find_service_id_returns_none_when_no_service_matches(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    monkeypatch.setattr(settings, "render_service_name", "no-such-service")
    with respx.mock:
        respx.get(RENDER_SERVICES).mock(
            return_value=httpx.Response(
                200,
                json=[{"service": {"id": "srv-1", "name": "pr-review-engine"}}],
            )
        )
        assert _render.find_service_id() is None


def test_unwrap_returns_the_inner_dict_when_wrapped():
    assert _render.unwrap({"service": {"id": "srv-1"}}, "service") == {"id": "srv-1"}


def test_unwrap_returns_the_item_itself_when_bare():
    assert _render.unwrap({"id": "srv-1"}, "service") == {"id": "srv-1"}


def _env_var_list(values: dict):
    return [{"envVar": {"key": k, "value": v}} for k, v in values.items()]


def test_env_vars_unwraps_the_service_env_list(monkeypatch):
    monkeypatch.setattr(settings, "render_api_key", "rnd_x")
    with respx.mock:
        respx.get(f"{RENDER_SERVICES}/srv-1/env-vars").mock(
            return_value=httpx.Response(200, json=_env_var_list({"A": "1", "B": "2"}))
        )
        result = _render.env_vars("srv-1")
    assert result == {"A": "1", "B": "2"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_render_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bot.scripts._render'` (or
`ImportError`) for every test, since the module doesn't exist yet.

- [ ] **Step 3: Create `scripts/_render.py`**

Move the four functions out of `scripts/deploy.py` verbatim (same bodies,
same docstrings) into the new module:

```python
"""Shared Render API access for scripts/deploy.py and scripts/set_provider.py.

Not a CLI entry point -- support code for the scripts/ CLIs. Consolidates
what was previously duplicated Render-fetch logic (service lookup, env-var
fetch) across the two scripts; see
docs/superpowers/specs/2026-08-10-render-access-consolidation-design.md.
"""

from __future__ import annotations

import httpx

from app.config import settings

RENDER_API = "https://api.render.com/v1"
HTTP_TIMEOUT = 10.0


def headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.render_api_key}",
        "Accept": "application/json",
    }


def unwrap(item: dict, key: str) -> dict:
    """Render wraps list items as {"service": {...}} / {"deploy": {...}}."""
    return item.get(key) or item


def find_service_id() -> str | None:
    resp = httpx.get(f"{RENDER_API}/services", headers=headers(), timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    for item in resp.json():
        service = unwrap(item, "service")
        if service.get("name") == settings.render_service_name:
            return service.get("id")
    return None


def env_vars(service_id: str) -> dict[str, str]:
    """The service's live env-vars, key -> value.

    Callers must reduce a returned value to a boolean or an equality result
    immediately -- never store it beyond that computation, print it, or pass
    it to anything that might log it. See CLAUDE.md's "no secret is ever
    logged" and docs/superpowers/specs/
    2026-08-10-provider-live-credential-verification-design.md section 6.
    """
    resp = httpx.get(
        f"{RENDER_API}/services/{service_id}/env-vars",
        headers=headers(),
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    current: dict[str, str] = {}
    for item in resp.json():
        env_var = unwrap(item, "envVar")
        current[env_var.get("key")] = env_var.get("value")
    return current
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_render_client.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/_render.py tests/test_render_client.py
git commit -m "feat: extract shared scripts/_render.py Render API access module"
```

---

### Task 2: Point `scripts/deploy.py` at `scripts/_render.py`

**Files:**
- Modify: `scripts/deploy.py`
- Modify: `tests/test_deploy_script.py`

**Interfaces:**
- Consumes: `bot.scripts._render.RENDER_API`, `.headers()`, `.unwrap()`,
  `.find_service_id()`, `.env_vars()` (produced in Task 1).

- [ ] **Step 1: Remove the four moved functions and `_RENDER_API` from `scripts/deploy.py`**

In `scripts/deploy.py`:

1. Delete the `_RENDER_API = "https://api.render.com/v1"` line (currently
   line 41).
2. Add `from scripts import _render` to the import block (after the
   `from app.config import settings` line).
3. Delete the four function bodies: `_render_headers` (lines 378-382),
   `_unwrap` (lines 385-387), `_find_render_service_id` (lines 412-419),
   `_render_env_vars` (lines 658-677).
4. Update every remaining call site in the file:
   - `check_provider_live()`: `_find_render_service_id()` →
     `_render.find_service_id()`; `_render_env_vars(service_id)` →
     `_render.env_vars(service_id)`.
   - `check_render_service()`: `_find_render_service_id()` →
     `_render.find_service_id()`; both `headers=_render_headers()` →
     `headers=_render.headers()`; both `f"{_RENDER_API}/..."` →
     `f"{_render.RENDER_API}/..."`; `_unwrap(deploys[0], "deploy")` →
     `_render.unwrap(deploys[0], "deploy")`.
   - `_wait_for_in_flight()`: `f"{_RENDER_API}/..."` →
     `f"{_render.RENDER_API}/..."`; `headers=_render_headers()` →
     `headers=_render.headers()`; `_unwrap(deploys[0], "deploy")` →
     `_render.unwrap(deploys[0], "deploy")`.
   - `_trigger_and_wait()`: both `f"{_RENDER_API}/..."` →
     `f"{_render.RENDER_API}/..."`; both `headers=_render_headers()` →
     `headers=_render.headers()`; both `_unwrap(..., "deploy")` →
     `_render.unwrap(..., "deploy")`.
   - `sync_env()`: `_find_render_service_id()` → `_render.find_service_id()`;
     `_render_env_vars(service_id)` → `_render.env_vars(service_id)`;
     `headers=_render_headers()` → `headers=_render.headers()`;
     `f"{_RENDER_API}/..."` → `f"{_render.RENDER_API}/..."`.

Use `grep -n "_render_headers\|_unwrap(\|_find_render_service_id\|_render_env_vars\|_RENDER_API" scripts/deploy.py`
to find every remaining occurrence before moving on — there should be none
left referring to the old bare names after this step.

- [ ] **Step 2: Retarget the 10 affected monkeypatches in `tests/test_deploy_script.py`**

Change each of the following (all currently
`monkeypatch.setattr(deploy, "_find_render_service_id", <lambda>)`) to
`monkeypatch.setattr(deploy._render, "find_service_id", <lambda>)`, keeping
each lambda body exactly as-is:

- `test_render_service_reports_the_live_commit`
- `test_render_service_fails_when_local_head_is_not_deployed`
- `test_render_service_fails_on_a_dirty_working_tree`
- `test_render_service_reports_an_image_without_claiming_verification`
- `test_render_service_degrades_when_render_reports_no_artifact`
- `test_render_service_skips_the_comparison_outside_a_git_repo`
- `test_sync_env_does_not_demand_other_providers_keys`
- `test_sync_env_refuses_when_the_selected_credential_is_empty`
- `test_sync_env_exits_2_on_an_unreadable_pem_without_a_traceback`
- `test_sync_env_refuses_when_an_override_would_mask_the_push`

Example (from `test_render_service_reports_the_live_commit`):

```python
# before
monkeypatch.setattr(deploy, "_find_render_service_id", lambda: "svc-1")
# after
monkeypatch.setattr(deploy._render, "find_service_id", lambda: "svc-1")
```

- [ ] **Step 3: Remove the relocated test from `tests/test_deploy_script.py`**

Delete `test_render_env_vars_unwraps_the_service_env_list` (currently lines
865-872, calling `deploy._render_env_vars("srv-1")` directly) — its
equivalent, `test_env_vars_unwraps_the_service_env_list`, was added to
`tests/test_render_client.py` in Task 1.

- [ ] **Step 4: Run the full deploy test suite to verify it still passes**

Run: `uv run pytest tests/test_deploy_script.py -v`
Expected: PASS, same test count as before minus the one relocated test.

- [ ] **Step 5: Grep-verify no old names remain**

Run: `grep -rn "_render_headers\|_unwrap(\|_find_render_service_id\|_render_env_vars\|_RENDER_API" scripts/ tests/`
Expected: no output (zero hits) — confirms the spec §8 exit condition.

- [ ] **Step 6: Commit**

```bash
git add scripts/deploy.py tests/test_deploy_script.py
git commit -m "refactor: point scripts/deploy.py at scripts/_render.py"
```

---

### Task 3: Point `scripts/set_provider.py` at `scripts/_render.py`

**Files:**
- Modify: `scripts/set_provider.py`

**Interfaces:**
- Consumes: `bot.scripts._render.find_service_id()`, `bot.scripts._render.env_vars()`
  (produced in Task 1). No test file changes in this task (spec §6 confirms
  `tests/test_set_provider_script.py` needs none).

- [ ] **Step 1: Update the import and call sites in `scripts/set_provider.py`**

Change:

```python
from bot.scripts.deploy import _PROVIDERS, _find_render_service_id, _render_env_vars
```

to:

```python
from scripts import _render
from bot.scripts.deploy import _PROVIDERS
```

In `_verify_render_credential()`, change:

```python
service_id = _find_render_service_id()
```

to:

```python
service_id = _render.find_service_id()
```

and:

```python
env_vars = _render_env_vars(service_id)
```

to:

```python
env_vars = _render.env_vars(service_id)
```

- [ ] **Step 2: Run the set_provider test suite to verify it still passes unchanged**

Run: `uv run pytest tests/test_set_provider_script.py -v`
Expected: PASS, same test count and names as before (no test file changes
were needed — confirms spec §6's prediction).

- [ ] **Step 3: Grep-verify no old references remain**

Run: `grep -n "_find_render_service_id\|_render_env_vars" scripts/set_provider.py`
Expected: no output.

- [ ] **Step 4: Commit**

```bash
git add scripts/set_provider.py
git commit -m "refactor: point scripts/set_provider.py at scripts/_render.py"
```

---

### Task 4: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Run the complete test suite**

Run: `uv run pytest -q`
Expected: PASS, zero failures, zero errors.

- [ ] **Step 2: Run the repo's lint check**

Run: `uv run ruff check scripts/ tests/`
Expected: no findings (in particular: no unused imports left in
`scripts/deploy.py` or `scripts/set_provider.py` from the old direct
`httpx`/name usage, and no unused `_PROVIDERS`/`_render` imports).

- [ ] **Step 3: Final grep sweep across the whole repo**

Run: `grep -rn "_render_headers\|_find_render_service_id\|_render_env_vars\b" --include="*.py" .`
Expected: no output — confirms nothing outside `scripts/` and `tests/`
(e.g. no stray reference in docs' code fences that would mislead a future
reader) references the removed names. (A hit inside a `.md` file's prose
describing history, e.g. this plan or the design spec itself, is expected
and fine — only `.py` files are in scope for this grep.)

- [ ] **Step 4: Commit** (only if any fixups were needed in Steps 1-2; otherwise skip — Task 3's commit already leaves the tree clean)
