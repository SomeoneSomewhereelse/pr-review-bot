# Design — Self-service onboarding wizard: shell + Render key frame

**Date:** 2026-08-26
**Status:** Approved for planning
**Relates to:** new `onboarding/` directory (does not yet exist), `app/dashboard.py`
(the static-page-plus-JSON-API convention this follows), root `CLAUDE.md`'s
secret-handling section (the discipline this extends to visitor-supplied
secrets), `guide/setup/hosted/*.md` (the manual process this eventually
replaces).

## 1. Problem and context

The Hosted track's 8-step setup guide (`guide/setup/*`) works but is entirely
manual: create a GitHub App by hand, install it, copy IDs out of GitHub's UI,
create a Supabase project and copy a pooler string, get a Render API key and
click through a Blueprint form, run a sync script, set up an UptimeRobot
monitor. Every step is a chance for a typo, and several credentials (a
base64-encoded PEM, a Postgres connection string with special characters)
are exactly the shape that's easy to get wrong by hand.

This spec covers the **first of six sub-projects** in a larger plan to
replace that guide with a self-service web wizard: a visitor lands on a page,
authorizes a few things, and ends the flow with a live, deployed instance of
their own — without hand-copying credentials between browser tabs and a
`.env` file. The full decomposition (for context; only #1 is in scope here):

1. **Wizard shell + Render key capture/validation** (this spec)
2. GitHub automation — App Manifest flow + install-redirect capture
3. Supabase provisioning — OAuth app + Management API
4. LLM provider credential UI — file-picker-to-base64 component, plain key
   paste for Gemini/Groq
5. UptimeRobot — paste key, create monitor
6. Render service creation + env-var sync + deploy poll (the frame that
   depends on everything above)

Only the Local track is considered for removal from the *guide*; this wizard
targets the Hosted track exclusively.

## 2. Confirmed decisions

| Decision | Choice |
|---|---|
| Audience | Self-service — any visitor stands up their **own** instance under their own accounts, not just the project operator |
| Automation depth | Full provisioning where a real delegated-auth API exists; plain key-paste accepted where it doesn't (Render, most of the LLM provider step) |
| Backend statefulness | **Stateless relay** (Approach B) — no session DB, no server-side secret storage of any kind |
| Secret custody | Lives in the browser's `sessionStorage` only, for the duration of the tab session |
| Runtime placement | **Separate service** from the review engine — own process, own Render deploy, own `onboarding/CLAUDE.md` |
| UI shape | Single-page accordion, one collapsible frame per external service, status badge in each header |
| Frame navigation | **Sequential unlock** — a frame stays disabled until everything it depends on is validated |
| Frame re-edit | **Locks on completion**; an explicit "Change" action re-opens it. Editing and resubmitting re-locks every later frame until re-validated |
| Accessibility / locale | Mobile-responsive; full RTL support; light/dark/system theme + English/Hebrew language toggle, **mirroring `app/static/dashboard.html`'s existing implementation** rather than inventing a new pattern |
| Sub-project build order | Render key capture first (no prerequisites, cheap to validate), then GitHub, Supabase, LLM UI, UptimeRobot, then the Render service-creation/sync frame last |

## 3. Architecture

`onboarding/` is a new top-level directory, sibling to `app/`, `scripts/`,
`guide/`. It is its own FastAPI application (`onboarding/main.py`) deployed
as its own Render service, sharing this repo's `uv`-managed dependency set
(FastAPI, httpx, pydantic-settings are already present) rather than a
separate `pyproject.toml`.

**No database, no persistent storage, anywhere in this service.** Every
external API call this wizard ever makes — OAuth-mediated or a plain
API-key call — goes through this backend as a stateless relay:

```
browser (holds the credential)
  → POST to an onboarding/ endpoint, credential in the request body
    → backend calls the external API with it
  ← backend returns a normalized result, never the credential itself
← browser stores the credential (if valid) in sessionStorage, updates UI
```

This is a deliberate uniformity choice: Render's API and UptimeRobot's API
don't strictly *need* a server-side relay the way GitHub's manifest exchange
and Supabase's OAuth token exchange do (those require a confidential client
secret). Routing everything through the same relay shape means there is one
security invariant to reason about across all six frames, not two different
patterns depending on which service is involved. It also sidesteps needing
to verify CORS support on each external API individually.

`sessionStorage`, not a JS variable, because several later frames in this
wizard involve a full-page navigation away to another domain and back
(GitHub's manifest flow, GitHub's install flow, Supabase's OAuth authorize
screen) — in-memory state would not survive that round trip. `sessionStorage`
is per-tab and cleared on tab close; nothing here introduces cross-tab or
cross-visitor sharing.

The wizard page itself is a single static HTML page with vanilla JS/CSS, no
build step — following `app/dashboard.py`'s existing static-page-plus-
JSON-API convention in this repo, not a new frontend framework.

## 4. Module layout (this slice)

```
onboarding/
  CLAUDE.md            scoped rules for this sub-project (section 8)
  main.py              FastAPI app + lifespan; GET /healthz
  render_client.py     validate_key(key: str) -> RenderAccountInfo | RenderKeyInvalid
  router.py            POST /api/render/validate-key; GET / serves the static page
  static/
    index.html          the whole wizard page — accordion shell markup (all
                         6 frame headers; only frame 1 is functional this
                         slice, 2-6 render as disabled/"coming soon"), inline
                         <style> (incl. mobile/RTL/theme, section 7), and
                         inline <script> (frame state machine, locking,
                         sessionStorage helpers, i18n, the fetch call for
                         frame 1) — one self-contained file, following
                         app/static/dashboard.html's existing convention
                         rather than separate .js/.css assets

tests/                 (repo-root — this project's existing testpaths, not
                        a per-service tests/ directory)
  test_onboarding_main.py         GET/HEAD /healthz
  test_onboarding_render_client.py  mocked httpx responses: valid key,
                                     invalid key (401), 5xx, timeout
  test_onboarding_router.py       endpoint-level tests against a mocked
                                   render_client
  test_onboarding_page.py         content-substring checks on the served
                                   page (frame markup, locking, i18n/RTL/
                                   theme markers) — see section 10
```

## 5. API contract

**`POST /api/render/validate-key`**

Request:
```json
{"api_key": "rnd_..."}
```

Response (valid):
```json
{"valid": true, "owner_name": "..."}
```

Response (invalid or unreachable):
```json
{"valid": false, "reason": "invalid_key" | "render_unreachable"}
```

The two failure reasons are distinguished deliberately: "your key is wrong"
and "Render's API is down right now" call for different visitor reactions,
and collapsing them into one generic failure would misdirect a visitor
hitting a transient outage into re-typing a key that was already correct.

The endpoint never logs or returns the key value itself in any response,
error message, or log line — logs record only the structural outcome
("render key validation: invalid (401)"), matching the shape root
`CLAUDE.md`'s secret-handling section already requires for the operator's
own credentials, extended here to a visitor's.

## 6. Data flow (Render frame)

1. Visitor loads the wizard. Frame 1 ("Render API key") is unlocked by
   default — it has no prerequisites. Frames 2-6 render `locked`.
2. Visitor pastes their key, clicks **Validate**.
3. `wizard.js` POSTs `{"api_key": "..."}` to `/api/render/validate-key`.
4. `router.py` calls `render_client.validate_key()`, which makes one cheap
   read call against Render's API (e.g. `GET /owners`) with that key as a
   Bearer token.
5. On a 2xx: returns `{"valid": true, "owner_name": ...}`.
   On a 401/403: returns `{"valid": false, "reason": "invalid_key"}`.
   On a network error, timeout, or 5xx: returns
   `{"valid": false, "reason": "render_unreachable"}`.
6. Browser, on `valid: true`: stores the key under a namespaced
   `sessionStorage` key (`onboarding.renderApiKey`), flips frame 1's header
   badge to "✓ validated — owner: `<owner_name>`", collapses frame 1, **locks
   it** (see below), and unlocks frame 2 (which remains a disabled
   placeholder until sub-project 2 lands).
   On `valid: false`: frame 1 shows an inline error matching the `reason`
   (distinct copy for each), stays open and expanded, nothing is written to
   `sessionStorage`.

**Locking a completed frame.** Once a frame reaches `done`, its header no
longer opens on click — a distinct "Change" control (shown only in the
`done` state) is the sole way back in. Clicking it re-opens the frame in its
editable state, pre-filled with nothing (the previous key is never re-shown,
consistent with never surfacing a credential value once it's been entered).
Submitting a revised value **re-locks every later frame** back to `locked`
until this frame is revalidated — an earlier frame's value may be a real
input to a later one (concretely: not yet for frame 1 → frame 2, but this is
the general contract every later frame in sub-projects 2-6 inherits, several
of which perform side-effecting actions like creating a GitHub App or a
Supabase project, where silently allowing a stale resubmission risks
creating a duplicate resource). This is why locking defaults to "on" rather
than being decided per-frame later.

## 7. Mobile, RTL, theme, and language support

The wizard must work well on a phone screen, support right-to-left layout
for Hebrew, and offer the same light/dark/system theme and English/Hebrew
language controls already implemented in `app/static/dashboard.html` —
mirroring that file's exact pattern rather than inventing a new one:

- **Theme** — a `themeToggleBtn` opens a small popup with light/dark/system
  radio options, persisted to `localStorage`, applied via
  `document.documentElement.setAttribute("data-theme", ...)` against the
  same `:root` / `prefers-color-scheme` / `[data-theme]` CSS custom-property
  structure `dashboard.html` already defines (light values on bare `:root`,
  dark values under both the media-query guard and `:root[data-theme="dark"]`).
- **Language** — a `langToggleBtn` opens a popup with English/Hebrew radio
  options, persisted to `localStorage`, applied by setting `lang`/`dir` on
  `<html>` and re-rendering every `[data-i18n]`-tagged element plus every
  dynamically-generated string (frame badges, error messages, the "Change"
  control) through a `STRINGS` lookup table and `t()` helper, exactly like
  `dashboard.html`'s existing implementation.
- **RTL** — `dir="rtl"` plus CSS logical properties (`margin-inline`,
  `padding-inline`, `inset-inline-*`) in place of physical `left`/`right`,
  matching `dashboard.html`'s existing approach — not a separate RTL
  stylesheet.
- **Mobile** — the wizard's natural layout (a stacked list of frames) is
  already single-column, but touch targets (the Validate/Change buttons,
  frame headers) need adequate tap-target sizing, and the theme/language
  popups reuse `dashboard.html`'s existing `positionPopup()` viewport-
  clamping logic so they never overflow a narrow screen.

This reuse is deliberate: `dashboard.html`'s pattern is already proven —
tested, RTL-correct, and visually consistent with the rest of this project's
web surface — so the wizard should read as the same product, not a
differently-themed one-off.

## 8. `onboarding/CLAUDE.md` (to be created verbatim by the implementation)

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

## 9. Error handling

- Backend: `render_client.validate_key()` catches `httpx` timeout/connection
  errors and non-2xx responses explicitly, mapping each to one of the two
  `reason` values — no bare `except Exception` that could accidentally swallow
  a real bug (e.g. a code error) into a misleading "render_unreachable".
- Frontend: a request that fails at the network level (the visitor's own
  connection, not Render's) is treated the same as `render_unreachable` —
  the visitor has no way to distinguish them and the retry action is
  identical either way.
- No retry-with-backoff logic in this slice — a single Validate click is one
  request; if it fails, the visitor clicks again. Automatic retries are not
  warranted for a manually-triggered, low-frequency action.

## 10. Testing strategy

- `render_client.py`: unit tests against mocked `httpx` responses covering
  valid key, invalid key (401), Render 5xx, and a timeout — no live Render
  API calls in CI or in tests, consistent with this project's existing
  "prefer mocked/cassette over live calls" testing hygiene (root `CLAUDE.md`,
  "LLM API testing hygiene" section — the same discipline applies to any
  third-party API this project calls repeatedly during development, not
  only LLM providers).
- `router.py`: endpoint tests against a mocked `render_client`, asserting the
  JSON contract (section 5) and that the key value never appears in the
  response body under any failure mode.
- Frontend: manual click-through verification for this slice — no build step
  exists yet to hang an automated JS test runner off, and a single functional
  frame doesn't yet justify standing one up. Revisit once more frames land.
  Content-substring assertions against the served HTML/JS (this repo's
  existing convention — see `tests/test_dashboard_page.py`) cover the frame
  state machine, the locking/"Change" behavior, and that the i18n/RTL/theme
  markup (`data-i18n` attributes, both `STRINGS.en` and `STRINGS.he` keys,
  the `dir` attribute switch) is actually present, without needing to
  execute JS.

## 11. Out of scope (tracked separately, not part of this spec)

- **`RENDER_API_KEY` becoming required** in the existing review engine's own
  `app/config.py` / `scripts/deploy.py` / `scripts/doctor.py` — a real
  follow-up implied by front-loading key collection in the wizard, but it
  touches the review engine's own config validation, a different module
  boundary than anything in this spec. Track as its own small task.
- Sub-projects 2-6 (GitHub automation, Supabase provisioning, LLM provider
  credential UI including the file-picker-to-base64 component, UptimeRobot,
  and the final Render service-creation/env-sync frame) — each gets its own
  design pass when its turn comes.
- Removing the Local track from `guide/setup/` — mentioned as a possible
  future direction, not decided or scoped here.
