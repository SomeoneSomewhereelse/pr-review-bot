# Design — Dashboard Environment credential guardrails & model selection

**Date:** 2026-09-03
**Status:** Approved for planning
**Relates to:** `dashboard/environment.py`, `dashboard/static/dashboard.html`,
`bot/providers/registry.py`, `bot/providers/active_model.py`,
`bot/providers/credentials.py`, `bot/providers/vertex_credentials.py`,
`bot/queue/store.py`, `bot/config.py`, `bot/github_app.py`,
`docs/superpowers/specs/2026-09-02-dashboard-environment-tab-design.md` (the
tab this extends), `docs/superpowers/specs/2026-08-27-onboarding-llm-provider-frame-design.md`
and `docs/superpowers/specs/2026-08-26-onboarding-github-app-frame-design.md`
(the onboarding flows this reimplements independently — no code is imported
from `onboarding/`, since `bot/` may become its own repo).

## 1. Problem and context

The Environment tab (shipped 2026-09-02) deliberately mirrors Render's own
Environment tab: any key can be added, edited, or deleted as free text.
Render has no notion that some of `bot`'s env vars are related — the bot
does. Two concrete failure shapes exist today, both reachable through the
tab as it stands:

- **Silent drift**: `GCP_PROJECT` is an independent override of the project
  embedded in `GCP_SERVICE_ACCOUNT_KEY` (`bot/factory.py`:
  `settings.gcp_project or info.get("project_id", "")`). Replacing the
  Vertex key with a different project's service account leaves the old
  `GCP_PROJECT` in place, silently pointed at the wrong project — no error
  until (or unless) a live call happens to fail downstream.
- **Dangling references**: deleting a numbered credential slot
  (`GEMINI_API_KEY_2`, etc.) while `runtime_config`'s `key_index` override
  still names that slot leaves the bot resolving an empty credential at
  call time (`ValueError`, not caught proactively).

The onboarding wizard already avoids both failure shapes for GitHub App and
LLM provider credentials, by construction — it validates a credential
live before accepting it, and it derives dependent identifiers (a Vertex
key's `project_id`, a GitHub App's installation ID) instead of asking the
operator to type them by hand. This design brings that same discipline to
the dashboard's Environment tab, for the vars where it actually matters,
while leaving the tab's free-form behavior intact everywhere else.

Two feature threads are combined here because they share the same
mechanism: exposing the LLM providers' per-provider `model`/`key_index`
config (already accepted by `EnvironmentConfigPatch` but not yet surfaced
in the UI) is naturally the same guided flow that fixes the credential
drift problem — both need "validate this credential live, then let the
operator pick a model from what it actually supports."

## 2. Confirmed decisions

| Decision | Choice |
|---|---|
| Scope of guardrails | Free-form add/edit/delete of **any** env var stays available, unchanged, everywhere. Guardrails (validation, dependency tracking, cascade-delete) apply only to a fixed set of **credential families** — this does not become a closed-set/schema-only editor. |
| Where guardrails apply | The Environment tab's own frontend/backend (`dashboard/environment.py` + `dashboard.html`) only. The bot's runtime (`bot/providers/*`, the dispatcher, `bot/main.py`'s `lifespan()`) is untouched — it keeps its own existing call-time/boot-time fallback behavior as the last-resort safety net. This design is entirely about catching problems before they reach that runtime path. |
| Trigger, not entry path | Whether a request goes through the new guided modal or is typed directly into the existing free-form add/edit form, if the **var name** matches a `CREDENTIAL_FAMILIES` member, the same validation/dependency logic applies. The guardrail is keyed off the var name, not the UI path used to reach it. |
| Credential families (v1) | `gemini`, `groq`, `vertex` (LLM providers — this design's original scope) and `github_app` (added during brainstorming as a second, structurally different family). See section 3. |
| Reimplementation, not reuse | All new validation/listing logic (`bot/providers/catalog.py`) is written from scratch in `bot/`, independent of `onboarding/llm_client.py`, even though the logic is structurally similar — `bot/` may be split into its own repo later. The one exception: GitHub App validation reuses `bot/github_app.py` directly, since that module already lives in `bot/` and already implements the needed checks — no onboarding code is touched either way. |
| Live validation calls | One deliberate metadata/listing call per validate action (`.models.list()`, or `GET /app` + `GET /app/installations`) — no retries, no burst — per root `CLAUDE.md`'s LLM API testing hygiene and this project's general credential-testing discipline. |
| Vertex/.pem upload | Browser uploads the raw file (JSON key or `.pem`); the backend does the base64-encode, decode, and validation. Client-side JS never touches credential structure, only relays bytes over the already-authenticated session. |
| `GCP_PROJECT` conflict | If replacing the Vertex credential and the new key's embedded `project_id` differs from an explicitly-set `GCP_PROJECT`, the modal shows both values and asks the operator to keep or clear it (clearing falls back to the embedded id) — never silently overwritten or silently left stale. `GCP_LOCATION` has no embedded value to check against, so it's left untouched with a non-blocking note to double check it. |
| `GITHUB_APP_INSTALLATION_ID` | Never a manually-editable field, in the guided modal or otherwise — it is fully derived. Validating an App ID + private key pair always runs live discovery (`bot/github_app.py::discover_and_verify_installation_id`) and the result is shown as a **read-only label**, not an input. The modal blocks only on discovery failure (`InstallationNotFound`, `MultipleInstallationsFound`, or a credential-level failure). |
| GitHub App delete | No new delete logic needed. `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, and `GITHUB_APP_INSTALLATION_ID` are already in `PROTECTED_ENV_KEYS` (2026-09-02 spec) — deletion is already refused by the existing mechanism. |
| LLM provider slot delete | No LLM provider credential var (`GEMINI_API_KEY`/`GROQ_API_KEY`/`GCP_SERVICE_ACCOUNT_KEY`, any slot, active or not) is in `PROTECTED_ENV_KEYS` today — all remain deletable, unlike the GitHub App family. Deleting one that other state still points at (a `key_index` override naming that slot, or a `provider` override naming that provider) shows a confirmation listing the dependent `runtime_config` entries, then **cascade-deletes** both the env var and those entries on confirm — this applies whether or not the slot/provider being deleted is currently active. Vars are never reset to "blank" as an alternative to deletion — Render rejects empty values — so cascade always means removing the entry outright, never blanking it. |
| Model var on credential delete | Not touched by cascade-delete. A model var (`LLM_MODEL`/`GROQ_MODEL`/`VERTEX_MODEL`) isn't credential-slot-specific, so deleting one credential slot doesn't cascade to it. |
| Model picker data source | Live catalog fetch (not a static/known-models list) — reflects what the credential can actually use right now, matches the onboarding precedent, and doubles as part of credential validation. |
| Model picker scope | All three LLM providers get a model field in the config UI simultaneously (not gated behind "whichever is active"), matching that `runtime_config` already stores a model per provider independently of which is active. |
| Key-slot picker | Included in this design (not deferred) — the operator can see and pick which credential slot is active per provider, discovered from which `slot_env_name(provider, i)` keys exist in the fetched Render var list (key names only, no values touched). |
| Changing model on an already-set provider | A dedicated "refresh models" action per LLM provider (not routed through the credential add/replace modal) — resolves the already-stored credential server-side via the existing `bot/providers/credentials.py::resolve`/`vertex_credentials.resolve_service_account_info`, and calls the same `catalog.list_*_models`. No upload/paste needed; keeps the guided modal's meaning as strictly "add or replace a credential." |
| Validating `GCP_PROJECT`/`GCP_LOCATION`/model vars edited directly | Automatic, not a manual button — fires on blur (inline pass/fail, informational only) and again, authoritatively, server-side on every `PATCH` touching one of these vars (rejects that var into `failed`, other keys in the same request still apply). Client-side Save is additionally blocked with no network call if a field is currently flagged invalid from its last blur-check. See section 6a. |

## 3. Credential families

```python
# bot/config_deps.py (new)
CREDENTIAL_FAMILIES = {
    "gemini":     {"credential": ["GEMINI_API_KEY"], "model": "LLM_MODEL"},
    "groq":       {"credential": ["GROQ_API_KEY"],   "model": "GROQ_MODEL"},
    "vertex":     {"credential": ["GCP_SERVICE_ACCOUNT_KEY"], "model": "VERTEX_MODEL",
                   "soft_deps": ["GCP_PROJECT", "GCP_LOCATION"]},
    "github_app": {"credential": ["GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY"],
                   "derived": ["GITHUB_APP_INSTALLATION_ID"]},
}
MAX_CREDENTIAL_SLOTS = 5  # numbered slots per LLM provider, e.g. GEMINI_API_KEY_1..._4
```

`gemini`/`groq`/`vertex`'s `credential` entry expands to its numbered-slot
variants via the existing `bot/providers/registry.py::slot_env_name`.
`github_app`'s credential is two independent vars together (App ID + key),
not slotted — there is exactly one GitHub App, unlike LLM providers which
support multiple key slots per provider.

`soft_deps` (Vertex) means "an independent var whose value can become
*stale*, not absent, when the credential changes" — handled with a
keep/clear prompt. `derived` (GitHub App) means "a var whose value is
never operator-authored, always computed from the credential" — handled by
recomputing and displaying it, no prompt.

## 4. Architecture

```
bot/providers/catalog.py       NEW — list_gemini_models(api_key),
                                list_groq_models(api_key),
                                list_vertex_models(service_account_info | None,
                                project_override=None, location_override=None)
                                -> CatalogResult{ok, models, error}. One
                                deliberate live listing call each. The
                                override params exist solely to support
                                section 6a's substitution-based validation
                                without writing anything. Written from
                                scratch (structurally similar to
                                onboarding/llm_client.py, not imported from
                                it).
bot/config_deps.py             NEW — CREDENTIAL_FAMILIES, MAX_CREDENTIAL_SLOTS,
                                and pure functions: dependents_of(var) ->
                                list of runtime_config entries that would
                                dangle, conflicts_for(family, new_value) ->
                                list of soft_dep mismatches (Vertex only).
                                No I/O — takes already-fetched state as
                                input, testable without mocks.
bot/github_app.py              UNCHANGED — existing discover_installation_id_for_app /
                                discover_and_verify_installation_id reused
                                directly by the new validate endpoint.
dashboard/environment.py       MODIFIED — four new routes (section 5): the
                                guided validate/apply pair, a
                                GET .../models refresh route for an
                                already-set provider, and
                                POST /api/environment/validate/{var} for
                                the five directly-editable soft-dep/model
                                vars (section 6a); plus the existing
                                DELETE /api/environment/render/{key} gains
                                dependent-computation for LLM credential
                                slots (none of which are in
                                PROTECTED_ENV_KEYS today), and
                                GET /api/environment/render response gains
                                an available_key_slots map.
dashboard/static/dashboard.html MODIFIED — Add button gains a "guided setup"
                                option per credential family (opens a modal:
                                file/text credential input, live-validate,
                                model dropdown for LLM providers or a
                                read-only installation-id label for GitHub
                                App, conflict prompt if applicable, apply);
                                editing an existing family credential var
                                opens the same modal pre-filled to "replace";
                                per-provider model + key-slot selects added
                                to the runtime_config panel, fed by the
                                guided modal's picks; the five directly-
                                editable soft-dep/model fields wire a
                                blur handler to POST /api/environment/validate/{var}
                                (inline pass/fail indicator) and track
                                per-field valid/invalid state to disable
                                Save while any is flagged invalid.
bot/tests/test_catalog.py      NEW — mocked SDK clients per provider, no
                                live calls.
bot/tests/test_config_deps.py  NEW — pure-function tests for dependents_of
                                and conflicts_for.
dashboard/tests/test_environment.py MODIFIED — new tests for the two
                                guided-flow routes and the extended delete
                                route (section 6).
```

## 5. API contract

| Route | Auth | Request | Response |
|---|---|---|---|
| `POST /api/environment/credential/{family}/validate` | session | multipart: credential file (Vertex JSON / GitHub App `.pem`) or text fields (Gemini/Groq API key, GitHub App ID) | `{"ok": bool, "error": str \| null, "models": [str] \| null, "project_id": str \| null, "installation_id": int \| null, "conflicts": [{"var": str, "current": str, "new": str}]}` — a dry run, nothing written |
| `POST /api/environment/credential/{family}/apply` | session | the validated payload plus the operator's model pick and any `conflicts` keep/clear choices | `{"applied": [str], "failed": [{"key": str, "error": str}]}` |
| `DELETE /api/environment/render/{key}` | session | `?confirm=true` optional | Unchanged shape; for an LLM provider credential slot (the only `CREDENTIAL_FAMILIES` members not in `PROTECTED_ENV_KEYS`) with dependents and no `confirm=true`, returns `409 {"dependents": [str]}` instead of deleting; with `confirm=true` (or no dependents), deletes the var and cascade-deletes the listed `runtime_config` entries |
| `GET /api/environment/render` | session | — | Unchanged `{"vars": [...]}`, plus `"available_key_slots": {provider: [int]}` computed from which `slot_env_name` keys exist |
| `GET /api/environment/credential/{family}/models?slot=N` | session | `slot` optional, defaults to that provider's current `key_index` override | `{"ok": bool, "models": [str] \| null, "error": str \| null}` — LLM providers only (`gemini`/`groq`/`vertex`); resolves the already-stored credential for that slot and re-runs the same live catalog call, no upload needed |
| `POST /api/environment/validate/{var}` | session | `{"value": str}` | `{"ok": bool, "error": str \| null, "models": [str] \| null}` — `var` is one of `GCP_PROJECT`/`GCP_LOCATION`/`LLM_MODEL`/`GROQ_MODEL`/`VERTEX_MODEL`; resolves the matching provider's stored credential and substitutes `value` into the same live-call path, writes nothing (section 6a) |

`error` values on `/validate` failure are structural only —
`unauthorized`/`forbidden`/`rate_limited`/`provider_unreachable`/`invalid_service_account_json`
for LLM providers, `invalid_key`/`unauthorized`/`github_unreachable`/`installation_not_found`/`multiple_installations`
for GitHub App — never a raw exception or credential value, per root
`CLAUDE.md`'s rule on secret-bearing validation errors.

## 6. Data flow

**Guided add/replace (LLM provider example — Vertex):**
1. Operator picks "Vertex" from the Add button's guided-setup dropdown (or
   clicks edit on an existing `GCP_SERVICE_ACCOUNT_KEY` row).
2. Modal opens: file picker for the service-account JSON.
3. Submit → `POST /api/environment/credential/vertex/validate` (multipart
   file). Backend decodes, calls `catalog.list_vertex_models(info)`.
4. Failure → structural error shown inline, submission blocked.
5. Success → modal shows the model dropdown (from `models`) and, if
   `GCP_PROJECT` is explicitly set and differs from `project_id`, the
   keep/clear prompt.
6. Operator picks a model (and a keep/clear choice if prompted), submits →
   `POST /api/environment/credential/vertex/apply` writes
   `GCP_SERVICE_ACCOUNT_KEY` (or the chosen slot), `VERTEX_MODEL`, and
   `GCP_PROJECT` (cleared or left) in one call, same per-key
   applied/failed reporting as the existing `PATCH /api/environment/render`.

**Guided add/replace (GitHub App):**
1. Operator picks "GitHub App" from the guided-setup dropdown, or edits
   `GITHUB_APP_ID`/`GITHUB_APP_PRIVATE_KEY`.
2. Modal: file picker for `.pem`, text field for App ID.
3. Submit → `POST /api/environment/credential/github_app/validate`.
   Backend calls `bot/github_app.py`'s checks, including
   `discover_and_verify_installation_id`.
4. Failure (credential invalid, no installation, multiple installations)
   → structural error, blocked.
5. Success → modal shows the discovered installation ID as a read-only
   label. No further input needed.
6. Submit → `POST /api/environment/credential/github_app/apply` writes
   `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, and
   `GITHUB_APP_INSTALLATION_ID` together.

**Deleting an LLM provider credential slot with dependents:**
1. Operator clicks delete on `GEMINI_API_KEY_2` (or on the active
   provider's own credential, e.g. `GEMINI_API_KEY` while
   `runtime_config.provider == "gemini"` — neither case is blocked today).
2. `DELETE /api/environment/render/GEMINI_API_KEY_2` (no `confirm`) →
   backend finds `runtime_config.gemini_key_index == 2` (and, if
   applicable, `runtime_config.provider == "gemini"`) → `409
   {"dependents": ["gemini_key_index override", "active provider override"]}`.
3. UI shows a confirmation dialog listing the dependents.
4. Operator confirms → same request with `?confirm=true` → var deleted,
   the listed `runtime_config` entries cleared, one `applied` response.

**Changing model on an already-set provider (no credential change):**
1. Operator clicks "refresh models" next to a provider's model `<select>`
   in the runtime_config panel.
2. `GET /api/environment/credential/{provider}/models?slot=N` (slot
   defaults to that provider's current `key_index` override) → backend
   resolves the stored credential for that slot and calls the matching
   `catalog.list_*_models`.
3. Response populates the model `<select>` with live options; operator
   picks one.
4. Existing `PATCH /api/environment/config`'s `model` field (unchanged)
   writes the pick — no credential involved, so this never touches the
   guided add/replace modal.

**Key-slot selection outside the guided flow:**
The runtime_config panel's key-slot `<select>` (populated from
`available_key_slots`) calls the existing `PATCH /api/environment/config`'s
`key_index` field directly for a plain switch between already-configured
slots — again no credential change, no guided modal.

## 6a. Validating soft-dep/model vars edited outside the guided flow

`GCP_PROJECT`, `GCP_LOCATION`, `LLM_MODEL`, `GROQ_MODEL`, and `VERTEX_MODEL`
can still be edited as plain free-form fields (unchanged from today), but
every edit to one of these five is now validated automatically at two
points, reusing the same underlying check — no separate "Validate" button:

1. **On blur** (the field loses focus): the frontend fires
   `POST /api/environment/validate/{var}` with `{"value": <candidate>}`.
   - For a model var: resolves that provider's currently-stored credential
     (same resolution as the `.../models` refresh route), re-runs
     `catalog.list_*_models`, checks `value` is in the result.
   - For `GCP_PROJECT`/`GCP_LOCATION`: resolves the current Vertex
     credential and builds the same client the real adapter would
     (`bot/factory.py`'s construction path), substituting `value` for
     whichever of project/location is being checked, then makes the one
     live listing call. `bot/providers/catalog.py::list_vertex_models`
     gains two optional params, `project_override`/`location_override`, to
     support this substitution without writing anything.
   - Response: `{"ok": bool, "error": str | null}` (model vars also
     include `"models": [str]` for potential inline suggestions). Shown
     inline as a pass/fail indicator next to the field. Purely
     informational at this point — nothing is written or blocked yet.
2. **On Save**: the frontend refuses to submit `PATCH /api/environment/render`
   (or `/config` for model vars) at all if any of these five fields is
   currently flagged invalid from its last blur-check — no network call is
   made, the operator is pointed at the failing field(s) instead. This is
   a client-side convenience, not the enforcement boundary.
3. **Server-side, on the same PATCH**: authoritative regardless of client
   state (covers a paste-then-save without ever blurring the field, stale
   client-side validation state, or a direct API call bypassing the UI) —
   the backend re-runs the identical check on every one of these five vars
   present in the request body before applying it. A failing var is
   reported in `failed` with the same structural error shape as elsewhere
   in this design; other, unrelated keys in the same request still apply,
   matching this project's standing "partial failure is always visible"
   convention.

This closes the "Out of scope" gap from the original draft (see section
8) without introducing a separate manual validation action, and without
making the plain edit form request a credential upload — only vars already
covered by an existing family's stored credential can be validated this
way; a var with no resolvable credential yet (e.g. the provider was never
set up) simply reports `{"ok": false, "error": "no_credential_configured"}`.

## 7. Testing strategy

- **`bot/tests/test_catalog.py`**: mocked `genai`/`groq` SDK clients for
  each provider's success and every structural error variant; asserts no
  real network call is made.
- **`bot/tests/test_config_deps.py`**: pure-function tests for
  `dependents_of`/`conflicts_for` against constructed `runtime_config`
  state — no DB, no mocks needed.
- **`dashboard/tests/test_environment.py`** (extended): validate/apply
  routes with `catalog`/`github_app` mocked out — success, each structural
  failure, the `GCP_PROJECT` conflict prompt path, the GitHub App
  installation-mismatch path; the `.../models` refresh route (resolves the
  stored credential for the given/defaulted slot, returns live models,
  never accepts an uploaded credential); the `.../validate/{var}` route
  for each of the five vars — success, failure, and
  `no_credential_configured` when the provider was never set up; a `PATCH`
  containing an invalid value for one of these vars reports it in `failed`
  while unrelated keys in the same request still apply; delete route
  returns `409` with dependents when unconfirmed and cascades correctly
  when confirmed; protected-key delete still refused exactly as before
  (regression check); no secret value ever asserted against in a log line.
- **Manual pass**: guided add/replace for at least one LLM provider and
  GitHub App, one cascade-delete confirmation, and the blur-validate →
  Save-blocked-while-invalid → server-side-rejects-if-forced sequence for
  at least one of the five directly-editable vars, driven through the
  actual dashboard UI before calling this done — UI flows aren't caught by
  type-checking or route tests alone.

## 8. Out of scope

- **Multi-installation GitHub Apps** — `MultipleInstallationsFound` is a
  validation failure, not a picker; this project's model is one App, one
  installation, matching the onboarding wizard's existing assumption.
- **Retiring `bot/scripts/set_override.py`** — still valid for
  provider/model/key-index switches that don't involve a credential
  change; not made redundant by this design.
- **Raising `MAX_CREDENTIAL_SLOTS` dynamically** — fixed at 5, a config
  change if ever needed, not a runtime setting.
