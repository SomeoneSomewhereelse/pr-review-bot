# Render access-layer consolidation — design

## 1. Background

`docs/superpowers/specs/2026-08-10-provider-live-credential-verification-design.md`
§9's last bullet and `docs/superpowers/plans/2026-08-10-provider-live-credential-verification.md`'s
"Deferred to a follow-up" section both record the same finding, raised during
review of that work: `scripts/deploy.py` and `scripts/set_provider.py` each
talk to Render's API (service lookup, env-var fetch) and each resolve "which
provider is actually running" — and `set_provider.py` does this today by
importing `scripts.deploy`'s underscore-prefixed (nominally private)
functions (`_find_render_service_id`, `_render_env_vars`, `_PROVIDERS`). That
review explicitly declined to collapse the *data model* — local `.env`,
Render's live env, and the DB override in Postgres encode three genuinely
different facts on purpose (2026-08-08 spec §2.1) — and deferred the *access
code* consolidation to this session.

This spec covers only the access-code consolidation. No behavior, error
message, or exit code changes anywhere; this is a pure refactor.

## 2. Scope decision

Two things were confirmed with the user before writing this spec:

- **Only Render HTTP access moves** (`_render_headers`, `_unwrap`,
  `_find_render_service_id`, `_render_env_vars`). The provider-resolution
  functions (`_resolved_provider`, `_resolved_provider_or_env`) stay in
  `deploy.py` — `set_provider.py` doesn't consume them today, and they're
  tightly coupled to `deploy.py`'s documented DB-timeout-avoidance rationale
  (a one-shot CLI can't afford `store.init_pool()`'s 30s blocking behavior).
  `_PROVIDERS` (the credential/model-var table) also stays in `deploy.py` —
  it's provider config, not Render-access code.
- **No back-compat aliasing.** `deploy.py`'s call sites and the existing
  tests that monkeypatch `deploy._find_render_service_id` etc. are updated to
  the new module path directly, rather than preserving the old names as
  aliases. The user's call: aliasing would leave two names for the same
  thing indefinitely, trading a slightly larger one-time diff for a smaller,
  clearer permanent surface.

## 3. New module: `scripts/_render.py`

A leading underscore, matching the convention that this is support code for
the `scripts/` CLIs, not a CLI entry point of its own. Imported as a peer by
`deploy.py` and `set_provider.py` — neither reaches into the other's
internals anymore.

Moved verbatim from `scripts/deploy.py` (same bodies, same docstrings, same
behavior — only the module changes):

```python
RENDER_API = "https://api.render.com/v1"
HTTP_TIMEOUT = 10.0

def headers() -> dict[str, str]: ...      # was deploy._render_headers
def unwrap(item: dict, key: str) -> dict: ...   # was deploy._unwrap
def find_service_id() -> str | None: ...  # was deploy._find_render_service_id
def env_vars(service_id: str) -> dict[str, str]: ...  # was deploy._render_env_vars
```

`find_service_id()` and `headers()` keep reading `settings.render_service_name`
/ `settings.render_api_key` directly (no parameters) — matching today's style
exactly, and keeping every existing test lambda's signature (e.g.
`lambda: "svc-1"`) unchanged; only the monkeypatch *target* changes.

`env_vars()`'s docstring keeps its existing invariant verbatim: callers must
reduce a returned value to a boolean or an equality result immediately —
never store it beyond that computation, print it, or pass it to anything
that might log it (CLAUDE.md's "no secret is ever logged"; this spec's
predecessor's §6).

`HTTP_TIMEOUT` here is a separate constant from `deploy.py`'s own
`_HTTP_TIMEOUT` (used by the unrelated UptimeRobot check) — duplicating one
float literal across two vendor-facing concerns beats cross-importing a
"generic timeout" from a Render-named module for an unrelated vendor's call.
`RENDER_API` **is** imported by `deploy.py`, since deploy.py's own
deploy-triggering and deploy-status-polling code (not moving — it's not
shared with `set_provider.py`) hits the same base URL.

## 4. `scripts/deploy.py` changes

Remove the four function bodies. Add `from scripts import _render`. Update
every call site from the bare name to the module-qualified one:

| Before | After |
|---|---|
| `_render_headers()` | `_render.headers()` |
| `_unwrap(item, key)` | `_render.unwrap(item, key)` |
| `_find_render_service_id()` | `_render.find_service_id()` |
| `_render_env_vars(service_id)` | `_render.env_vars(service_id)` |
| `_RENDER_API` | `_render.RENDER_API` |

Call sites affected: `check_provider_live`, `check_render_service`,
`sync_env`, `_wait_for_in_flight`, `_trigger_and_wait`.

## 5. `scripts/set_provider.py` changes

```python
from scripts import _render
from scripts.deploy import _PROVIDERS
```

(Previously: `from scripts.deploy import _PROVIDERS, _find_render_service_id,
_render_env_vars`.) `_verify_render_credential()`'s two call sites become
`_render.find_service_id()` and `_render.env_vars(service_id)`.

## 6. Test changes

All in `tests/test_deploy_script.py` — `tests/test_set_provider_script.py`
needs no changes (confirmed: it only ever monkeypatches
`_verify_render_credential` directly, and mocks the Render HTTP layer via
`respx`, never `deploy`'s or `set_provider`'s internal function names).

**Mechanical retarget** (10 call sites, same lambda bodies, target changes
from `deploy` to `deploy._render`):

```python
monkeypatch.setattr(deploy, "_find_render_service_id", lambda: "svc-1")
```
becomes
```python
monkeypatch.setattr(deploy._render, "find_service_id", lambda: "svc-1")
```

Affected tests: `test_render_service_reports_the_live_commit`,
`test_render_service_fails_when_local_head_is_not_deployed`,
`test_render_service_fails_on_a_dirty_working_tree`,
`test_render_service_reports_an_image_without_claiming_verification`,
`test_render_service_degrades_when_render_reports_no_artifact`,
`test_render_service_skips_the_comparison_outside_a_git_repo`,
`test_sync_env_does_not_demand_other_providers_keys`,
`test_sync_env_refuses_when_the_selected_credential_is_empty`,
`test_sync_env_exits_2_on_an_unreadable_pem_without_a_traceback`,
`test_sync_env_refuses_when_an_override_would_mask_the_push`.

**Relocation:** `test_render_env_vars_unwraps_the_service_env_list` currently
calls `deploy._render_env_vars("srv-1")` directly to test unwrap behavior in
isolation — this moves to a new `tests/test_render_client.py`, calling
`_render.env_vars("srv-1")`, since that module is now the real owner of the
behavior under test.

**New file `tests/test_render_client.py`:** since `scripts/_render.py` is now
a standalone module worth testing directly rather than only transitively
through `deploy.py`/`set_provider.py`, add focused unit coverage alongside
the relocated test:

- `find_service_id()`: matching service name → its id; no match → `None`
  (both via `respx`-mocked `GET /services`, mirroring the existing pattern in
  `test_deploy_script.py`).
- `unwrap()`: wrapped (`{"service": {...}}`) and bare-dict input both return
  the inner dict.
- `env_vars()`: the relocated unwrap test, unchanged in substance.

No other test files change. No new test infrastructure (fixtures, mocks) is
needed beyond what `test_deploy_script.py` already uses — `test_render_client.py`
reuses the same `respx` + `httpx` patterns.

## 7. Non-goals (carried forward from the 2026-08-10 spec's own §9)

- No change to the three-plane data model (local `.env` / Render live env /
  DB override).
- No change to `_resolved_provider()` / `_resolved_provider_or_env()` —
  these stay in `deploy.py`, unmoved.
- No change to `_PROVIDERS` — stays in `deploy.py`, still imported by
  `set_provider.py` as today.
- No behavior, message, or exit-code changes in either script.

## 8. Testing strategy for this change itself

Since this is a pure refactor with pre-existing coverage, verification is:
run the full existing suite (`test_deploy_script.py`,
`test_set_provider_script.py`, new `test_render_client.py`) and confirm green,
plus a manual read-through confirming no call site was missed (grep for
`_render_headers`, `_unwrap(`, `_find_render_service_id`, `_render_env_vars`,
`_RENDER_API` across `scripts/` and `tests/` should return zero hits after
the change).
