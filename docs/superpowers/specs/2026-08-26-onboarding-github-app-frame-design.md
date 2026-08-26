# Design — Onboarding wizard: GitHub App automation (frame 2)

**Date:** 2026-08-26
**Status:** Approved for planning
**Relates to:** `onboarding/` (shipped in sub-project 1, see
`docs/superpowers/specs/2026-08-26-onboarding-wizard-render-frame-design.md`),
`scripts/create_github_app.py` (the human-run CLI equivalent this generalizes
for self-service visitors), `scripts/deploy.py::sync_env()` (the per-key
Render env-var push this wizard's later sub-project will reuse), `guide/setup/
02-github-app.md` and `03-install-app.md` (the manual process this replaces).

## 1. Problem and context

This is the **second of six sub-projects** decomposing the self-service
onboarding wizard (full decomposition recorded in the sub-project 1 spec).
Sub-project 1 shipped the accordion shell and a working Render-API-key frame.
Frame 2 automates what `guide/setup/02-github-app.md` and
`03-install-app.md` otherwise require a human to do by hand in GitHub's UI:
create a GitHub App (App Manifest flow) and install it on an account/repo
selection, capturing the resulting App ID, private key, webhook secret, and
installation ID — the exact four values `scripts/deploy.py::sync_env()`
already knows how to push into a Render service's env vars.

`scripts/create_github_app.py` already automates the Manifest flow for a
single human operator running a local script (local callback server, or a
manual copy-paste fallback for headless/SSH sessions — both needed because
the script has no public URL to redirect to). This wizard is different in
one structural way that simplifies things: `onboarding/` is itself a
deployed service with a real, stable public URL by the time any visitor uses
it, so GitHub's redirects can land directly on the wizard's own domain — no
`example.invalid` placeholder-redirect trick needed.

## 2. Confirmed decisions (this sub-project)

| Decision | Choice |
|---|---|
| App naming | Visitor types the App name (required field); a name collision surfaces as GitHub's own error on its approval page — no special handling needed here |
| Step sequencing | Explicit two-step: "App created ✓" is its own visible state with its own **Install** action, not an automatic chained redirect |
| Post-install confirmation | Verify the `installation_id` via a JWT-signed relay call to GitHub's API (account login + repo scope), not just trusting the redirect's query string |
| Account-switch risk | GitHub offers no forced-reauth mechanism for these pages (not an OAuth `authorize` endpoint, and `github.com/logout` is CSRF-protected, not a plain redirect target) — handled with explicit instructional copy only, matching `guide/setup/03-install-app.md`'s existing warning |
| End-state of the whole wizard | Confirmed (again) as **live Render provisioning only** — no `.env` file ever leaves the browser as a download. A future standalone CLI script/flag to reconstruct `.env` files from an already-deployed service is a separate, later idea, not part of this wizard |
| Credential-residency direction for sub-project 6 | **Firm commitment, recorded here for that sub-project to pick up:** sub-project 6 will create the Render service immediately after frame 1 (not at the end), and every later frame (2, 3, 4, 5) will push its own captured secret into that service the moment it validates, via the same per-key `PUT /v1/services/{id}/env-vars/{key}` `sync_env()` already uses. Frame 6 then shrinks to "trigger deploy + poll". This was investigated during this sub-project's brainstorm: nothing blocks it mechanically (Render's env-var API is already incremental, never a bulk replace), but *programmatic Render service creation* is unresearched — nothing in this codebase creates a Render service via API today, only against one a human already created via the dashboard's Blueprint flow — so it is out of scope for this sub-project and left as sub-project 6's problem to solve, not solved here as a side effect. Frame 2's design below stores its captured values under clean, single-purpose `sessionStorage` keys specifically so that a later "push this frame's secret to Render immediately" step is a small addition, not a rework. |

## 3. Data flow

Two sequential GitHub round-trips, each a full top-level navigation away and
back — `sessionStorage` (not a JS variable) is what survives the trip, same
reasoning as frame 1 and the rest of sub-project 1's design.

### Phase 1 — Create the App

1. Visitor types an App name into frame 2, clicks **Create App**.
2. Browser JS builds the manifest object entirely client-side — no backend
   call needed for this step, since it involves no secret and no external
   call. Fixed fields (`public: false`, `default_permissions`,
   `default_events`) are a JS-side copy of `scripts/create_github_app.py`'s
   `MANIFEST_PERMISSIONS`/`MANIFEST_EVENTS`/`build_manifest()` — kept in
   sync by a paired comment in both files, not a shared code artifact
   (there is no shared JS/Python module boundary to put one in). `url` is
   the wizard's own real public URL (embedded server-side at request time,
   see section 5); `hook_attributes.url` stays the same
   `https://example.invalid/webhook` placeholder the CLI script uses, since
   the review engine's own eventual webhook URL doesn't exist until
   sub-project 6 deploys it — corrected then, mirroring
   `scripts/deploy.py`'s existing "points here — set only if wrong" pattern
   for the exact same field.
3. Browser generates a random `state` (`crypto.randomUUID()`), stores it in
   `sessionStorage`, and submits a hidden auto-submitting `<form>` (POST) to
   `https://github.com/settings/apps/new?state=...` — same technique as
   `scripts/create_github_app.py::_render_manifest_form()`.
4. GitHub shows its own login screen if the visitor isn't already
   authenticated, then its manifest-approval screen, then redirects to
   `{public_base_url}/?gh_step=manifest&code=...&state=...`.
5. Page reloads. JS sees `gh_step=manifest`, checks the returned `state`
   against what's stored — mismatch or missing `code` shows a generic error
   and stops here, nothing is exchanged. On a match, JS POSTs
   `{"code": "..."}` to `/api/github/exchange-manifest-code`.
6. Backend calls GitHub's conversion endpoint
   (`POST https://api.github.com/app-manifests/{code}/conversions`, no auth
   header needed — same as `scripts/create_github_app.py::exchange_code()`)
   and returns `{valid: true, app_id, slug, private_key_b64, webhook_secret}`
   on success, or `{valid: false, reason: "exchange_failed" |
   "github_unreachable"}` on failure (`exchange_failed` = GitHub 4xx — the
   code is single-use and expires quickly; `github_unreachable` = network
   error or 5xx).
7. Browser stores the returned object under `sessionStorage`'s
   `onboarding.githubApp` key, strips the query string
   (`history.replaceState`), and frame 2 shows "App created ✓ — click to
   install it on your account" — an intermediate sub-state, not yet `done`.

### Phase 2 — Install the App

8. Visitor clicks **Install**. Browser generates a fresh `state`, stores it,
   and navigates (`location.href =`, a plain top-level navigation — not a
   form POST, so no CSP `form-action` concern here) to
   `https://github.com/apps/{slug}/installations/new?state=...`.
9. GitHub shows its repo-picker (own login screen first if needed), then
   redirects to `{public_base_url}/?gh_step=install&installation_id=...
   &setup_action=...&state=...` — `setup_url` was set to this same address
   at manifest-creation time (a real, documented App Manifest field).
10. Page reloads. JS checks `state`; on a match, reads
    `app_id`/`private_key_b64` back out of `sessionStorage` and POSTs
    `{app_id, private_key_b64, installation_id}` to
    `/api/github/verify-installation`.
11. Backend builds a short-lived JWT from the decoded PEM (via PyGithub —
    see section 6) and calls `GET /app/installations/{id}`, returning
    `{valid: true, account_login, repo_scope: "all" | "selected"}` on
    success or `{valid: false, reason: "installation_not_found" |
    "invalid_credentials" | "github_unreachable"}` on failure. Scoped to
    account login + `repository_selection` only — an exact repo count would
    need a second token-mint-then-list round trip for marginal extra
    confirmation value, not worth the added complexity here.
12. Browser merges `installation_id`/`account_login` into
    `onboarding.githubApp`, strips the query string, and calls
    `completeFrame("github-app", "account_prefix", account_login)` —
    frame 2 shows "✓ installed on `<account_login>`" and unlocks frame 3.

**Accepted limitation:** if the visitor closes the tab between phase 1 and
phase 2, `sessionStorage` is gone (deliberate, from sub-project 1's
architecture) and restarting frame 2 creates a *new* GitHub App rather than
resuming — the abandoned one is orphaned on their account until they delete
it themselves in GitHub's settings. Nothing server-side can fix this without
breaking the stateless-relay invariant, so this is accepted rather than
solved.

**Restoring on reload:** `restoreFromSession()` (already present for frame
1) gets a matching branch: if `onboarding.githubApp` holds a complete
record (app data + installation), call `completeFrame` immediately; if it
holds only phase-1 data (visitor refreshed between phases with no active
`gh_step`), render the "ready to install" sub-state directly — no wasted
round trip back through GitHub.

## 4. API contract

**`POST /api/github/exchange-manifest-code`**

Request:
```json
{"code": "..."}
```
Response (success):
```json
{"valid": true, "app_id": 123, "slug": "my-app-abc", "private_key_b64": "...", "webhook_secret": "..."}
```
Response (failure):
```json
{"valid": false, "reason": "exchange_failed" | "github_unreachable"}
```

This is the one endpoint in the whole wizard that *mints and returns* a
credential rather than only verifying one submitted to it — a deliberate,
narrow exception to the "never echo a credential" rule in
`onboarding/CLAUDE.md`, since the returned material belongs to the visitor
who just created it and must reach their browser for sub-project 6 to later
push it into their own Render service. Never logged, same standard as every
other credential this service handles.

**`POST /api/github/verify-installation`**

Request:
```json
{"app_id": 123, "private_key_b64": "...", "installation_id": 456}
```
Response (success):
```json
{"valid": true, "account_login": "...", "repo_scope": "all" | "selected"}
```
Response (failure):
```json
{"valid": false, "reason": "installation_not_found" | "invalid_credentials" | "github_unreachable"}
```

This request body carries the App's full private key — the single most
sensitive artifact anywhere in this wizard, the same tier as the operator's
own `GITHUB_APP_PRIVATE_KEY` in root `CLAUDE.md`. Never logged in full or
truncated, and any GitHub/PyGithub exception surfaced to the caller is
described structurally ("installation not found", "auth failed"), never as
a raw exception message that could echo request data.

## 5. New module: `onboarding/config.py`

Doesn't exist yet. A required (fail-fast-at-boot, no default) setting:

```python
class Settings(BaseSettings):
    public_base_url: str
```

Needed to build `redirect_url`/`setup_url` for the manifest, and to hand the
wizard's own real URL to client-side JS for constructing the manifest object
in phase 1 step 2. Delivered into the page via a single placeholder-token
substitution in `router.py`'s `index()` handler:

```python
_INDEX_HTML.replace("__ONBOARDING_BASE_URL__", settings.public_base_url)
```

— one dynamic value, no templating engine, keeps the "one self-contained
static file, no build step" convention from sub-project 1 intact.

## 6. New module: `onboarding/github_client.py`

Same shape as `render_client.py` (dataclass results, no persistence, narrow
exception handling — no bare `except Exception`):

```python
async def exchange_manifest_code(code: str) -> GithubAppCreated | GithubAppExchangeFailed
async def verify_installation(app_id: int, private_key_b64: str, installation_id: int) -> InstallationVerified | InstallationInvalid
```

`exchange_manifest_code` is a plain `httpx` POST (no auth), same style as
`render_client.validate_key`. `verify_installation` uses **PyGithub**
(`pygithub>=2.4`, already an approved dependency — the review engine's own
`app/github_app.py` already does App JWT auth this way, delegating RS256
signing to PyGithub internally via PyJWT) — but builds its own independent
`Auth.AppAuth`/`GithubIntegration` instance from the *visitor's* just-minted
`app_id`/`private_key`, never importing `app/github_app.py`'s helpers, which
are wired to the operator's own long-lived env-based credentials (per
`onboarding/CLAUDE.md`'s no-shared-credential-path rule). PyGithub's calls
are synchronous; `verify_installation` needs the same async-blocking
handling `app/github_app.py` already uses for calling it from async code —
left as a task-level detail, not a design-level one.

## 7. CSP fix (security header correction)

`router.py`'s existing CSP on `GET /`
(`default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline';
connect-src 'self'; frame-ancestors 'none'`) has no `form-action` directive,
so it inherits `default-src 'none'` — **this blocks phase 1's manifest form
POST to `github.com` outright.** Fix: add
`form-action 'self' https://github.com;`, narrowly scoped to exactly the one
external origin this page ever needs to form-POST to. The phase 2
install-navigation step (`location.href = ...`) is unaffected — CSP's
`default-src`/`connect-src` do not govern top-level navigation, only
`form-action`/`frame-src` do, and this is a plain navigation, not a form
submission.

## 8. `onboarding/CLAUDE.md` additions

To be appended (not replacing existing content) once implementation lands:

```markdown
## What sub-project 2 (GitHub App automation) adds to these rules

- **`exchange-manifest-code` is a deliberate exception to "never echo a
  credential."** It mints and returns App credentials that belong to the
  visitor who just created them — this is different from every other
  endpoint in this service, which only ever verifies a credential already
  submitted to it. A new endpoint that returns secret material needs the
  same justification this one has (the value is freshly created for, and
  belongs to, the caller) before following this exception rather than the
  default rule.
- **`verify-installation`'s request body carries a GitHub App's full
  private key** — the same sensitivity tier as this project's own
  `GITHUB_APP_PRIVATE_KEY`. Treat it accordingly: never logged, never in an
  unhandled exception's message, narrow `except` clauses only.
- **Multi-navigation flows use a `sessionStorage`-held random `state`,
  generated client-side before each redirect and checked on return before
  any exchange happens.** This is the CSRF pattern for any frame that
  involves a full-page round trip to an external site (this one, and
  Supabase's OAuth in sub-project 3) — reuse it rather than inventing a
  per-frame variant.
- **`GET /`'s CSP carries `form-action 'self' https://github.com;`**
  specifically for this frame's manifest-creation form POST. A future frame
  that needs to form-POST to a *different* external origin adds that origin
  to this same directive rather than loosening `default-src`.
- **`onboarding/config.py`'s `public_base_url` is required, not optional.**
  Frame 2 cannot construct a working manifest without it — same "fail loud
  at boot, not silently drift" posture root `CLAUDE.md` already applies to
  the review engine's own required settings.
```

## 9. Error handling

- Backend: both `github_client.py` functions catch `httpx`/PyGithub errors
  explicitly and map each to one of the documented `reason` values — no
  bare `except Exception`, matching `render_client.py`'s existing
  convention.
- Frontend: a `state` mismatch or a missing `code`/`installation_id` on
  either callback shows a generic frame-2 error and does **not** attempt
  the exchange — protects against a replayed or tampered callback URL. A
  network-level failure on either `fetch()` is treated the same as
  `github_unreachable` — the visitor has no way to distinguish them.
- No retry-with-backoff — each action (Create App, Install) is one
  manually-triggered click; a failure means the visitor clicks again,
  consistent with sub-project 1's frame 1 precedent.

## 10. Testing strategy

- `tests/test_onboarding_config.py` (new): `public_base_url` required,
  service fails fast without it.
- `tests/test_onboarding_github_client.py` (new): mocked `httpx`/PyGithub
  covering valid code exchange, expired/bad code (4xx), GitHub unreachable
  (timeout/5xx); valid installation verification, installation not found
  (404), bad credentials, unreachable — no live GitHub API calls in tests,
  consistent with this project's existing testing hygiene.
- `tests/test_onboarding_router.py` (extended): endpoint tests for both new
  routes against a mocked `github_client`, asserting the JSON contract and
  that request/response bodies never leak into an unrelated error path.
- `tests/test_onboarding_page.py` (extended): frame 2 markup (name input,
  Create/Install buttons), both language's i18n keys, `handleGithubCallback`
  present, CSP now includes `form-action`. The existing
  `test_render_key_leaves_the_page_exactly_once` (currently a blanket
  `body.count("fetch(") == 1`) is narrowed to just the render-key call, per
  `onboarding/CLAUDE.md`'s own anticipated evolution — paired with two new
  tests, one per new GitHub `fetch()` call, each asserting it appears
  exactly once.

## 11. Out of scope (tracked separately, not part of this spec)

- **Programmatic Render service creation and the incremental-secret-push
  restructuring of sub-project 6** — the direction is firmly decided
  (section 2), but the actual API investigation and implementation belong
  to that sub-project.
- **Sub-projects 3-5** (Supabase provisioning, LLM provider credential UI,
  UptimeRobot) — each gets its own design pass, as already noted in the
  sub-project 1 spec.
- **A future standalone CLI script/flag to reconstruct `.env` files from an
  already-deployed Render service** — a distinct, later idea, not part of
  this wizard's own flow.
- **`GITHUB_TARGET_REPO`** — which installed repos the bot actually acts on
  is a separate, further-narrowing setting on top of installation (see
  `guide/setup/03-install-app.md`). Repo *selection* happens on GitHub's own
  install screen in phase 2; setting `GITHUB_TARGET_REPO` itself is an
  env-var-sync concern that belongs with sub-project 6, not this frame.
