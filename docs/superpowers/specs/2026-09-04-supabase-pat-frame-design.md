# Design — Supabase provisioning frame: OAuth app → Personal Access Token

**Date:** 2026-09-04
**Status:** Approved for planning
**Relates to:** `onboarding/supabase_client.py`, `onboarding/router.py`,
`onboarding/session_store.py`, `onboarding/config.py`,
`onboarding/static/index.html`, `onboarding/CLAUDE.md`'s sub-project 3
section, `onboarding/.env.example`. Supersedes
`docs/superpowers/specs/2026-08-26-onboarding-supabase-provisioning-frame-design.md`'s
auth-mechanism decision and fully replaces
`docs/superpowers/specs/2026-09-03-supabase-oauth-abuse-mitigation-design.md`
(marked superseded, not deleted — see section 6). Closes the "Supabase
OAuth app is a shared resource" and "OAuth App registration's plan-tier
availability" entries in `ISSUES.md`'s Design Gaps section.

## 1. Problem and context

The Supabase frame (`onboarding/CLAUDE.md` sub-project 3) currently
authenticates through one operator-registered OAuth app
(`SUPABASE_OAUTH_CLIENT_ID`/`SUPABASE_OAUTH_CLIENT_SECRET`), shared across
every visitor. That original design (2026-08-26) explicitly weighed this
against a visitor-pasted Personal Access Token, and chose OAuth "for full
automation" — logging the resulting shared-blast-radius risk as a
deliberately deferred `ISSUES.md` Design Gap rather than solving it. A
follow-up session (this one, 2026-09-03) wrote a full rate-limiting +
detection + graceful-degradation mitigation for that risk before
re-examining the original premise.

**The premise doesn't hold.** Supabase's own documentation confirms a
Personal Access Token reaches the identical Management API endpoints an
OAuth token does (org listing, project creation, status polling) — PATs
are Supabase's own documented path for exactly this kind of single-
integration automation; OAuth apps are meant for a service that manages
*many* users' accounts on an ongoing basis, which this one-time
provisioning wizard is not. No other rationale for choosing OAuth is
recorded anywhere in the original spec or its plan. Since "full
automation" was the entire stated reason and a PAT provides that just as
well, this design replaces OAuth with a PAT-based flow instead of
continuing to mitigate a risk that a different, already-viable design
choice removes outright — bringing the Supabase frame in line with every
other frame's model (Render, GitHub App, LLM provider, UptimeRobot: every
credential is visitor-supplied, none is an operator-held shared secret).

## 2. Confirmed decisions

| Decision | Choice |
|---|---|
| Auth mechanism | Visitor-pasted Personal Access Token, validated and used exactly like the Render frame's API key. No operator-level Supabase secret remains. |
| Automation depth | Unchanged from today: the wizard still lists orgs, creates the project, polls status, and assembles the connection string via the Management API — only the credential's origin changes. |
| Project-name capture | Moves from "typed before the OAuth redirect" (no longer applicable — there is no redirect) to "typed alongside the org picker, after the PAT validates" — sent directly in the `create-project` request body instead of pre-stored in the session during a now-nonexistent connect step. |
| OAuth code removal | Full removal — `exchange_oauth_code`, `refresh_access_token`, the `/api/supabase/connect` and `/oauth/supabase/callback` endpoints, PKCE/`state` handling, and the two operator settings are all deleted, not left dormant. |
| Abuse-mitigation spec | Marked `Status: Superseded` in place, not deleted — keeps the record of why it was written and why it's now moot. |

## 3. Backend changes

**`onboarding/supabase_client.py`:**
- Delete `exchange_oauth_code`, `refresh_access_token`, `SupabaseTokens`,
  `SupabaseOAuthFailed`, `SUPABASE_OAUTH_BASE`, and `list_organizations`
  (folded into the new validation call below).
- Add `validate_key(pat: str) -> SupabaseKeyValid | SupabaseKeyInvalid`,
  mirroring `render_client.validate_key`'s shape exactly: one
  `GET /v1/organizations` call under `Authorization: Bearer <pat>`.
  `SupabaseKeyValid` carries `orgs: list[SupabaseOrg]` (the existing
  `SupabaseOrg` dataclass, unchanged) — this doubles as both credential
  validation and the org list the frame needs next, replacing what was
  two separate calls (`exchange_oauth_code` + `list_organizations`) with
  one. `SupabaseKeyInvalid.reason` is `"invalid_key"` (401/403) or
  `"supabase_unreachable"` (network/5xx/malformed body) — same reason
  vocabulary `render_client.py` already uses, for consistency.
- `create_project`, `get_project_status`, `get_connection_info` are
  unchanged — they already take a bearer token string as a parameter and
  have no idea whether it came from OAuth or a PAT.
- `create_project` gains a `name: str` parameter (currently reads it from
  the caller's own `supabase_frame["name"]` at the `router.py` call site;
  the caller now passes it straight through since it arrives in the
  request body instead of the session — see below).

**`onboarding/router.py`:**
- Delete `SupabaseConnectRequest`, `connect_supabase`,
  `supabase_oauth_callback`, `SUPABASE_OAUTH_CALLBACK_PATH`, and all
  `_pending_oauth` read/write handling.
- Add `POST /api/supabase/validate-key`, request `{"key": str}` (`Field
  (min_length=1, max_length=512)`, matching the LLM-provider frame's key
  field constraints). Calls `supabase_client.validate_key`; on success,
  writes `{"api_key": pat}` into the session's `supabase` frame via
  `_update_frame(..., replace=True)` (a fresh key submission is a
  "restart this frame" action, same reasoning already documented for the
  Render/GitHub validate-key endpoints) and returns `{"valid": true,
  "orgs": [...]}`. On failure, returns `{"valid": false, "reason":
  ...}` — never the submitted key.
- `SupabaseCreateProjectRequest` gains `name: str = Field(min_length=1,
  max_length=64)` alongside the existing `organization_slug`.
  `create_supabase_project` reads `api_key` from the session (as
  `access_token` did before, just renamed) and passes `payload.name`
  straight to `supabase_client.create_project` instead of reading a
  session-stored `name`.
- `get_supabase_project_status` and `get_supabase_connection_info` change
  only in reading `supabase_frame["api_key"]` instead of
  `supabase_frame["access_token"]` — no other change.
- `GET /api/session`'s Supabase completeness mapping loses its "OAuth
  done, project not yet created" branch (`"access_token" in supabase`
  with no `ref`) — that in-between state no longer exists once there's no
  redirect round-trip. It collapses into the existing "credential valid,
  no project yet" shape the same completeness check already gives the
  Render frame between key-validation and service-creation.

**`onboarding/config.py` / `onboarding/main.py`:** delete
`supabase_oauth_client_id`, `supabase_oauth_client_secret`, their
validators, and the corresponding `lifespan` presence check. No
replacement setting — the PAT is entirely visitor-supplied.

**`onboarding/session_store.py`:** no schema change. The `supabase` frame's
`access_token` field is renamed `api_key` throughout (session field only —
`session_store.py` itself is schema-agnostic JSONB, so this is a call-site
rename in `router.py`, not a migration).

**`onboarding/.env.example`:** delete the `SUPABASE_OAUTH_CLIENT_ID=` /
`SUPABASE_OAUTH_CLIENT_SECRET=` lines (confirmed: `bot/.env.config.example`
doesn't reference this credential at all — unaffected, operational-
config-only template).

**`onboarding/.env.config.example`:** deleted outright, separately from
this credential swap — a 2026-09-04 audit found its only real setting
(`PUBLIC_BASE_URL`) was already stale before this design (the setting was
removed from `onboarding/config.py` on 2026-08-31; the file's own comment
still cited GitHub's Manifest flow and Supabase's OAuth authorize screen,
both gone), and `onboarding/config.py`'s `Settings` has no `env_file`
configured at all, unlike `bot/config.py`'s — so the file was never
actually loaded by anything. With that block removed nothing but header
comments remained, so the file was deleted rather than left empty.

## 4. Frontend changes (`onboarding/static/index.html`)

- Replace the "Connect to Supabase" OAuth-redirect button with a text
  input + submit, same shape as the Render-key frame: paste PAT →
  `POST /api/supabase/validate-key` via `callSupabaseRelay` (satisfies
  the existing one-exit-path test convention unchanged). On success, show
  the org picker (auto-selected if the account has exactly one org,
  otherwise a dropdown — unchanged) **together with** a project-name
  input (default `pr-review-bot`, editable — same default and editability
  as today, just relocated to this step). Submitting that step calls
  `create-project` with both `organization_slug` and `name`.
- Instructions are breadcrumb text ("Account → Access Tokens → Generate
  new token"), no `supabase.com` URL rendered or linked — reusing the
  GitHub App frame's established "breadcrumb text, no clickable/copyable
  URL" pattern for UI consistency (that frame's specific reason — GitHub
  ToS suspension risk — doesn't apply here; the pattern is reused for
  consistency, not because the same risk exists).
- Delete `handleSupabaseOauthCallback`, PKCE/`state` generation, and the
  `/oauth/supabase/callback` path-routing branch in the page's own
  router.
- `restoreFromSession()`'s Supabase branch drops the "OAuth done, project
  not yet created" resume case (matches the `GET /api/session` change
  above) — resumes into the same "credential valid, org+name step shown"
  state the Render frame's equivalent in-between state already resumes
  into.

## 5. Testing

- `supabase_client.py`: `validate_key` returns `SupabaseKeyValid` with
  the org list on 200, `invalid_key` on 401/403, `supabase_unreachable`
  on network error/5xx/malformed body — mirrors `render_client.py`'s
  existing `validate_key` test shape exactly, `respx`-mocked.
- `router.py`: `validate-key` persists `api_key` into the session on
  success (`replace=True`) and returns the org list, never the key
  itself; `create-project` passes through the request's `name` field
  (mocking `supabase_client.create_project` and asserting the call args)
  rather than reading a session-stored one; `project-status` and
  `connection-info` read `api_key` (not `access_token`) from the session.
- `test_onboarding_page.py`: update the one-exit-path assertion for the
  new `validate-key` endpoint; remove the assertions that referenced
  `connect`/`exchange-oauth-code`/the callback path, since those no
  longer exist on the page at all.
- Delete every test exercising `exchange_oauth_code`,
  `refresh_access_token`, `connect_supabase`, `supabase_oauth_callback`,
  and PKCE/`state` handling — dead code has no test to keep green.
- No test exercises a real Supabase call — same `respx`-mocked convention
  as before.

## 6. Documentation and disposition

- `onboarding/CLAUDE.md`'s sub-project 3 section is **rewritten**, not
  appended — every OAuth-specific bullet (the operator-secret framing,
  the same-tab-redirect/PKCE/cookie-session mechanics, `db_pass`
  generation timing relative to the redirect) either no longer describes
  real code or needs restating in PAT terms. The new section documents:
  PAT as the credential, `validate-key`'s combined validate+list-orgs
  call, `name` traveling via `create-project`'s request body, and that
  `db_pass` is still generated server-side in `create-project` (that part
  is genuinely unchanged, just no longer tied to redirect timing).
- `docs/superpowers/specs/2026-08-26-onboarding-supabase-provisioning-frame-design.md`
  is left as-is — historical record of what shipped then. This new spec
  is the record of what replaces it; no retroactive edit to the old one.
- `docs/superpowers/specs/2026-09-03-supabase-oauth-abuse-mitigation-design.md`'s
  header gets `Status: Superseded — see
  2026-09-04-supabase-pat-frame-design.md` (its body is left otherwise
  unchanged, as a record of the mitigation design that turned out to be
  unnecessary).
- `ISSUES.md`'s Design Gaps section: both the "Supabase OAuth app is a
  shared resource across all visitors" and "Supabase OAuth App
  registration's plan-tier availability is undocumented" entries get
  `Status: closed — superseded by PAT-based redesign,
  2026-09-04-supabase-pat-frame-design.md` — the risk and the open
  question both stop applying once there's no OAuth app to register or
  share.

## 7. Out of scope

- No change to `create_project`, `get_project_status`,
  `get_connection_info`'s Management API call shapes — this is an
  auth-mechanism swap, not a re-verification of the provisioning flow
  itself.
- No change to how `db_pass`/`database_url` are generated, stored, or
  assembled — unaffected by the credential swap.
- The manual pooler-connection-string fallback track
  (`guide/setup/hosted/05-supabase.md`) is untouched; this spec doesn't
  add or remove that manual path, only changes the wizard's own automated
  one.
