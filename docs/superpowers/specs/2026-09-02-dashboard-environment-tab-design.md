# Design — Dashboard Environment tab

**Date:** 2026-09-02
**Status:** Approved for planning
**Relates to:** `dashboard/router.py`, `dashboard/static/dashboard.html`,
`dashboard/CLAUDE.md`, `bot/scripts/_render.py`, `bot/scripts/deploy.py`,
`bot/scripts/set_override.py`, `bot/queue/store.py` (the `get_*`/`set_*`
override functions), `onboarding/render_client.py` (the sibling client this
mirrors), root `CLAUDE.md`'s "Secret handling" section, the "Dashboard
'Environment' feature" entry in `ISSUES.md`'s Design Gaps section — this spec
resumes and completes that parked entry now that dashboard authentication
(`docs/superpowers/specs/2026-08-28-dashboard-authentication-design.md`) is
merged.

## 1. Problem and context

The dashboard today (`dashboard/router.py`) is read-only: `GET /` and
`GET /api/dashboard` show ticket/queue/review stats behind a login-gated
session. Two operational surfaces still require leaving the dashboard
entirely: changing the running service's Render env vars (secrets — API
keys, `DATABASE_URL`, `GITHUB_WEBHOOK_SECRET`, `GCP_SERVICE_ACCOUNT_KEY`,
...) requires editing `.env` and running `deploy.py --sync-env` locally, and
changing `runtime_config` overrides (active provider/model, active key
slot, cooldown tuning, usage caps, `review_draft_prs`) requires
`set_override.py`. Both are CLI-only, local-machine operations.

This design adds an **Environment** tab to the dashboard, alongside the
existing content (renamed **Status**), that does both from the browser:
view and edit live Render env vars, and view and edit `runtime_config`
overrides — the fetch/edit/write flow the original `ISSUES.md` entry left
unresolved.

## 2. Confirmed decisions

| Decision | Choice |
|---|---|
| Nav shell | Single page, client-side toggle between **Status** and **Environment** panels inside the existing `dashboard/static/dashboard.html` — no new page route. |
| Render env-var CRUD scope | Full: add, edit (including numbered keys like `GROQ_API_KEY_1`/`_2`, `GCP_SERVICE_ACCOUNT_KEY_1`/`_2`), and delete any var — mirrors Render's own Environment tab. |
| Secret value visibility | **Exception to `CLAUDE.md`'s "never display a byte of a secret" rule, scoped to this one feature.** Real values are fetched and rendered masked by default, toggle-revealable in the operator's own authenticated browser session — never logged, never persisted client-side beyond the DOM, never sent anywhere but this response. Transport is unchanged HTTPS throughout (browser↔dashboard, dashboard↔Render API) — the exception is about the value reaching the DOM at all, not about weaker encryption. `CLAUDE.md` gets a new subsection documenting this carve-out explicitly, so it isn't read as a blanket loosening. |
| Push mechanism | **Never Render's real bulk endpoint** (`PUT /v1/services/{id}/env-vars`, which silently replaces the whole list). One "Save" action applies every staged edit/delete as a loop of single-key calls (`PUT`/`DELETE /v1/services/{id}/env-vars/{key}`), then fires **one** redeploy trigger — matches `deploy.py::sync_env()`'s and `onboarding/render_client.py::push_env_var()`'s existing rule. |
| Redeploy behavior | Fire-and-forget: `POST /v1/services/{id}/deploys` is called and its response returned immediately — no blocking poll-until-live (the request may itself get cut off mid-response once the old container is torn down). The dashboard UI shows a "redeploying…" banner and polls `GET /api/dashboard` afterward, tolerant of the connection dropping. Relies on Render's existing `healthCheckPath: /healthz` zero-downtime behavior: a new deploy that crashes on boot (e.g. a bad env var breaking `Settings()` construction) never receives traffic and the previous live container — dashboard included — keeps serving. |
| `runtime_config` write | `PATCH /api/environment/config`, one field per existing `set_*` in `bot/queue/store.py` (provider override, per-provider model override, per-provider key-index override, cooldown overrides, usage-cap override, `review_draft_prs`). No redeploy — same as `set_override.py` today, live on next dispatcher read. |
| Auth | Session cookie only (existing `require_session` dependency) — no step-up re-auth for writes. Matches the single-auth-layer posture the rest of the dashboard already has. |
| Audit logging | Every write (Render env var or `runtime_config`) logs one structured INFO line: key name, and for env vars the new value's *length* (never the value) or `deleted`; for `runtime_config`, the new value itself (non-secret). Matches this project's existing strategic-INFO-logging convention on the webhook→enqueue→dispatch path. |
| Error handling | Per-key success/failure reporting on `PATCH /api/environment/render` — matches `push_env_var()`'s existing stop-at-first-failure-report-what-succeeded behavior. Never a single opaque "failed" — this project's standing "partial failure is always visible" convention. |
| Module placement | New `bot/render_client.py`, mirroring `onboarding/render_client.py`'s existing precedent (each deployable package gets its own thin Render API client rather than crossing package boundaries). `bot/scripts/_render.py`'s logic moves there. `deploy.py`/`set_override.py` import from `bot.render_client` instead of `bot.scripts._render`; the new `dashboard/environment.py` does not import from `bot/scripts/` at all — reusable pieces (the per-key push loop, deploy-trigger call, `_looks_like_local_test_db` guard) live in `bot/render_client.py` and are called by both `deploy.py` and the dashboard, not imported one from the other. |
| Onboarding change | Frame 6's existing `push_env_var` loop (`onboarding/render_client.py`) also pushes `RENDER_API_KEY` itself into the newly created service, so the deployed bot service has the credential this feature needs at runtime. Flagged explicitly: this makes the deployed service hold the one credential that can rewrite its own env vars and trigger its own redeploys — previously `RENDER_API_KEY` never left the operator's machine. The dashboard's session-cookie auth is what stands between "authenticated operator" and "anyone who reaches the URL" once this ships. |
| `deploy.py` future | Once this ships, `deploy.py --sync-env` and `set_override.py` are redundant with the dashboard doing the same job live. Not retired by this plan — logged as a follow-up in `ISSUES.md`'s Design Gaps for a later cleanup pass, since some of `deploy.py`'s checks (pricing, provider-live, health, database) are unrelated to env-var/config editing and stay useful regardless. |

## 3. Architecture

```
bot/render_client.py          NEW — headers(), find_service_id(), env_vars(),
                                push_env_var() (per-key loop, stop-at-first-
                                failure), delete_env_var(), trigger_deploy()
                                (fire-and-forget, no poll). Moved/adapted from
                                bot/scripts/_render.py + the push loop in
                                bot/scripts/deploy.py::sync_env().
bot/scripts/_render.py         REMOVED — superseded by bot/render_client.py
bot/scripts/deploy.py          MODIFIED — imports from bot.render_client;
                                keeps its own blocking _trigger_and_wait()
                                for CLI use (dashboard uses the non-blocking
                                trigger_deploy() instead)
bot/scripts/set_override.py    MODIFIED — imports from bot.render_client
                                where it touches Render (if at all)
dashboard/router.py            MODIFIED — Status-only content, still
                                read-only; CLAUDE.md's "never mutates"
                                contract holds for this file specifically
dashboard/environment.py       NEW — GET/PATCH /api/environment/render,
                                GET/PATCH /api/environment/config
dashboard/static/dashboard.html MODIFIED — side nav (Status / Environment),
                                Environment panel: Render vars table
                                (masked value, reveal toggle, edit, delete,
                                add-new-key form, Save button) + runtime_config
                                form (existing override fields)
dashboard/CLAUDE.md            MODIFIED — documents dashboard/environment.py's
                                write access (breaks the current
                                read-only-router statement), notes the
                                secret-visibility exception
root CLAUDE.md                 MODIFIED — new subsection under "Secret
                                handling" documenting the scoped exception
                                (masked-by-default, toggle-reveal, this one
                                authenticated endpoint only)
onboarding/render_client.py    MODIFIED — frame 6 push list gains
                                RENDER_API_KEY
bot/tests/test_render_client.py NEW — mocked httpx, mirrors
                                onboarding/tests/test_onboarding_render_client.py
dashboard/tests/test_environment.py NEW — see section 6
ISSUES.md                      MODIFIED — closes the parked Design Gaps
                                entry; adds a new follow-up note for
                                deploy.py/set_override.py eventual retirement
```

## 4. Data flow

**Viewing Environment tab:**
1. `GET /api/environment/render` (session-gated) → `bot.render_client.env_vars()`
   fetches the live list from Render, response is `{"vars": [{"key": str,
   "value": str}]}` (section 5) — the raw value rides the (TLS-encrypted)
   response but the client renders it masked (`••••••••`) by default.
2. `GET /api/environment/config` (session-gated) → current `runtime_config`
   values via the existing `get_*` functions, rendered directly (non-secret).

**Editing/saving Render env vars:**
1. Operator edits/adds/deletes keys in the UI; edits accumulate client-side
   (unsaved-changes indicator, matching Render's own staged-edit UX).
2. Save → `PATCH /api/environment/render` with
   `{"sets": {key: value, ...}, "deletes": [key, ...]}`.
3. Backend loops: `push_env_var(service_id, key, value)` for each `sets`
   entry, `delete_env_var(service_id, key)` for each `deletes` entry, both
   via `bot.render_client`. Stops at the first failure, same as
   `push_env_var()` today; response reports which keys succeeded, which
   failed and why, before the loop stopped.
4. If at least one write succeeded: `trigger_deploy(service_id)` fires
   `POST .../deploys` and returns its `deploy_id` immediately — no poll.
   Logs one INFO line per successfully-applied key (name + new length, or
   `deleted`).
5. Response: `{"applied": [...], "failed": [...], "deploy_id": str | null}`.
6. Client shows per-key results and, if `deploy_id` is present, a
   "redeploying…" banner; begins polling `GET /api/dashboard` every few
   seconds, tolerant of the connection dropping mid-redeploy, until it
   succeeds again (new container live) or a timeout is reached (operator is
   told to check Render's own dashboard if so — same fallback the CLI's
   `_trigger_and_wait()` already has via its own timeout path).

**Editing `runtime_config`:**
1. Operator edits provider/model/key-index/cooldown/usage-cap/
   `review_draft_prs` fields in the Environment panel's second section.
2. Save → `PATCH /api/environment/config` with only the changed fields.
3. Backend calls the matching `set_*` per field; logs one INFO line per
   field (name + new value — non-secret). No redeploy.
4. Response: `{"applied": [...], "failed": [...]}` — same partial-failure
   shape as the Render section for consistency, even though a `set_*` call
   against a reachable DB is expected to succeed or fail atomically per
   field.

## 5. API contract

| Route | Auth | Request | Response |
|---|---|---|---|
| `GET /api/environment/render` | session | — | `{"vars": [{"key": str, "value": str}]}` |
| `PATCH /api/environment/render` | session | `{"sets": {str: str}, "deletes": [str]}` | `{"applied": [str], "failed": [{"key": str, "error": str}], "deploy_id": str \| null}` |
| `GET /api/environment/config` | session | — | current override values, one field per `get_*` |
| `PATCH /api/environment/config` | session | partial override fields | `{"applied": [str], "failed": [{"key": str, "error": str}]}` |

## 6. Testing strategy

- **`bot/tests/test_render_client.py`** (new): mocked `httpx` for
  `find_service_id`, `env_vars` (pagination, same page-2 scenario
  `_render.py`'s existing docstring documents), `push_env_var` (success,
  stop-at-first-failure), `delete_env_var`, `trigger_deploy` (returns
  immediately, does not poll). No live Render calls, ever — mirrors
  `onboarding/tests/test_onboarding_render_client.py`.
- **`dashboard/tests/test_environment.py`** (new): route-level tests using
  the existing session-cookie test fixture from `dashboard/tests/test_auth.py`.
  Covers: unauthenticated requests rejected; a successful multi-key save
  reports all applied + fires one deploy trigger (mocked); a failing key
  mid-loop is reported in `failed` and does not block already-applied keys
  from being reported as `applied`; `runtime_config` PATCH calls the correct
  `set_*` per field; values are never present in any log line asserted
  against (mirrors how `test_auth.py` never asserts on the raw password).
- **`bot/scripts/`'s existing tests** (`test_deploy_script.py`,
  `test_override_script.py` or equivalent): updated for the
  `bot.render_client` import path, behavior otherwise unchanged.

## 7. Out of scope

- **Retiring `deploy.py`/`set_override.py`** — logged as a follow-up in
  `ISSUES.md`, not part of this plan. Their non-env-var checks (pricing,
  provider-live, health, database, credential-live) stay useful
  independent of this feature.
- **Step-up re-authentication for writes** — deliberately deferred; the
  existing session-cookie gate is judged sufficient for this
  single-operator project, matching the dashboard-authentication spec's own
  posture.
- **Naming the Render-vars vs config sections beyond "Environment" tab's
  two panels** — labels are a UI-copy detail settled during implementation,
  not a design decision.
- **Using Render's real bulk-replace endpoint** — explicitly rejected (see
  section 2); every write is single-key.
- **Multi-operator audit trail (who made a change)** — logging captures
  *what* changed, not *who*, consistent with the single-shared-credential
  model dashboard authentication already established.
