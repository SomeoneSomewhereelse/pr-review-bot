# Design — Self-service onboarding wizard: Supabase provisioning frame

**Date:** 2026-08-26
**Status:** Approved for planning
**Relates to:** `onboarding/` (shipped in sub-project 1 and 2, see
`docs/superpowers/specs/2026-08-26-onboarding-wizard-render-frame-design.md`
and `docs/superpowers/specs/2026-08-26-onboarding-github-app-frame-design.md`
— this frame reuses their stateless-relay architecture, `sessionStorage`
credential custody, and CSRF `state` pattern), `guide/setup/hosted/05-supabase.md`
(the manual process this eventually replaces), `ISSUES.md`'s Design Gaps
section (the two open items this sub-project deliberately defers).

## 1. Problem and context

This is the **third of six sub-projects** decomposing the self-service
onboarding wizard (full decomposition recorded in the sub-project 1 spec).
Sub-project 1 shipped the accordion shell and a working Render-API-key
frame; sub-project 2 added GitHub App automation. This spec covers frame 3:
replacing `guide/setup/hosted/05-supabase.md`'s entirely manual flow
(create a project by hand, wait ~2 minutes, copy the Session-mode pooler
connection string, watch out for password percent-encoding) with API-driven
provisioning — the visitor authorizes once, and the wizard creates a real
Supabase project and assembles its connection string for them.

Unlike sub-project 2's GitHub App Manifest flow, Supabase's Management API
has no self-registration mechanism: a third-party integration must
authenticate through an **OAuth app that only the operator can register**,
one time, via Supabase's own dashboard. This is a genuinely new category of
secret for this service — sub-projects 1 and 2 introduced zero operator-held
secrets (every credential was either visitor-typed or minted fresh and
handed back to the visitor who created it). This sub-project's brainstorm
explicitly weighed that tradeoff against a visitor-pasted personal-access-
token fallback (no operator secret, no shared blast radius) and chose the
OAuth app for full automation, accepting the new risk — see section 7.

## 2. Confirmed decisions (this sub-project)

| Decision | Choice |
|---|---|
| Auth mechanism | **Operator-registered OAuth app** (full Management API automation), not visitor-pasted personal access tokens. Accepts a new shared-abuse-risk exposure — see section 7 and `ISSUES.md`'s Design Gaps entry. |
| Automation depth | Full provisioning: the wizard actually creates a new Supabase project via the Management API, not just validation of one the visitor made by hand. |
| Organization selection | Auto-pick if the visitor's account has exactly one organization; otherwise show a simple dropdown of org names/slugs fetched via `GET /v1/organizations`. |
| Region | Fixed default (`us-east-1`), no visitor choice — consistent with this frame's YAGNI-for-self-service-demo posture. |
| Project naming | Visitor types it (default `pr-review-bot`, editable) — mirrors sub-project 2's GitHub App naming pattern. |
| Free-tier project cap | Surfaced as Supabase's actual rejection *message*, relayed verbatim (see section 4's `create-project` contract) rather than a generic failure — but not as a fixed, guessed reason enum: this project's own testing-hygiene discipline (root `CLAUDE.md`) counsels against hardcoding assumptions about a live API's exact behavior without verifying it. **Verified live 2026-08-27** against a real account already at its 2-project cap: `POST /v1/projects` returns `HTTP 400` with a JSON body whose `message` field is a plain string (Supabase's own free-tier-cap wording) — no other structure, matching `create_project()`'s `response.json().get("message")` extraction exactly. No code change needed. |
| Readiness polling | Browser polls a lightweight relay endpoint every 5 seconds, times out after 5 minutes (≈2.5× the guide's documented ~2-minute typical wait) with a manual "Check again" button on timeout — keeps the backend a stateless per-request relay rather than a long-held blocking connection. |
| Orphaned project on partial failure | Nothing automatic. If project creation succeeds but a later step fails or the tab closes mid-flow, the wizard does not attempt to delete it — error copy tells the visitor a project may exist and links to their Supabase dashboard. Mirrors the GitHub frame's identical precedent for an orphaned App. |
| DB password custody | **Generated client-side** by the browser (Web Crypto API, alphanumeric-only character set), not minted by the backend. This keeps it in the same "browser already holds the credential" category as every other frame's visitor-originated secret, rather than growing the mint-and-return exception list — see section 5. |
| Cost verification | The $0 demo-cost commitment (`cost.md`) covers project usage (falls on the *visitor's* own account/org, not the operator's — see section 3), but whether OAuth App *registration itself* is free-plan-available is undocumented anywhere in Supabase's docs. Logged as a Design Gap in `ISSUES.md`, verified live by the operator at the one-time manual registration step rather than blocking this spec — no purchase happens automatically since that step is manual and operator-initiated. |

## 3. Architecture and data flow

`onboarding/supabase_client.py` (new, mirrors `render_client.py`'s and
`github_client.py`'s shape) handles all Supabase Management API and OAuth
calls via `httpx.AsyncClient` — nothing here needs PyGithub's `requests`
transport, so `respx` can mock all of it directly in tests, unlike
`github_client.py`'s `verify_installation`.

Two new operator-level settings join `public_base_url` in
`onboarding/config.py`: `supabase_oauth_client_id` and
`supabase_oauth_client_secret`. Both are checked for presence in
`onboarding/main.py`'s `lifespan`, same pattern as the existing base-URL
check — no pydantic-required field, for the same pytest-collection reason
documented there.

**Cost model:** project creation happens via the *visitor's own* OAuth
token, against one of *their* organizations (`organization_slug` comes from
`GET /v1/organizations` under their token). Any usage cost (their free-tier
project cap, any overage) lands on the visitor's account, not the
operator's — consistent with sub-project 1's "self-service — any visitor
stands up their own instance under their own accounts" decision. The
operator's only cost exposure is the one-time OAuth app registration itself
(see section 2's Cost verification row).

**Flow** (mirrors sub-project 2's two-redirect structure):

1. Visitor opens frame 3, types a project name (default `pr-review-bot`,
   editable).
2. Browser generates a random `state` (CSRF) and a PKCE `code_verifier` /
   `code_challenge` pair (SHA-256, `code_challenge_method=S256`), stores
   both in `sessionStorage`, and full-page-redirects to
   `https://api.supabase.com/v1/oauth/authorize` with `client_id`,
   `redirect_uri` (`{PUBLIC_BASE_URL}/?supabase_step=oauth_callback`),
   `response_type=code`, `state`, `code_challenge`, `code_challenge_method`.
3. Visitor authorizes on Supabase's own consent screen (their existing
   account, or signs up in the moment) — no forced-reauth mechanism exists
   here either, same conclusion sub-project 2 reached for GitHub.
4. Supabase redirects back with `code` + `state`. Browser verifies `state`
   against its stored value, then `POST /api/supabase/exchange-oauth-code`
   with `{code, code_verifier}`.
5. Backend exchanges the code at `POST https://api.supabase.com/v1/oauth/token`
   (form-encoded body: `grant_type=authorization_code`, the operator's
   `client_id`/`client_secret`, `code`, `code_verifier`, `redirect_uri`) and
   returns `{valid, access_token, refresh_token, expires_in}` to the
   browser — the frame's mint-and-return exception (see section 5): all
   three values are freshly issued and belong to the visitor who just
   authorized. Backend never persists any of them. `refresh_token` may be
   absent per Supabase's own schema (it is not a required response field)
   — the browser must tolerate that.
6. Browser calls `POST /api/supabase/list-organizations` with
   `{access_token}`. Backend relays `GET /v1/organizations`, returns
   `{orgs: [{slug, name}]}`. Auto-select if one; show a dropdown if more.
7. Browser generates a random 32-character alphanumeric DB password
   client-side (`crypto.getRandomValues`, mapped into `[A-Za-z0-9]`) — long
   enough to be a strong password, and the Management API's `db_pass` field
   carries no documented length ceiling to stay under. Browser calls
   `POST /api/supabase/create-project` with
   `{access_token, organization_slug, name, db_pass}`. Backend relays
   `POST /v1/projects` with `{organization_slug, name, db_pass,
   region_selection: {type: "specific", code: "us-east-1"}}` — omitting the
   deprecated `region` and `plan` fields, and omitting `desired_instance_size`
   so it defaults to the smallest tier. Returns `{ref, status}` to the
   browser (`status` is `INACTIVE` immediately after creation, per the
   Management API's own response — provisioning is asynchronous).
8. Browser polls `POST /api/supabase/project-status` with
   `{access_token, ref}` every 5 seconds (5-minute timeout, manual
   "Check again" on timeout), showing "Provisioning… this takes about 2
   minutes." Backend relays `GET /v1/projects/{ref}`, returns `{status}`.
   Target is `ACTIVE_HEALTHY`; `INIT_FAILED` is treated as an immediate
   terminal failure (stop polling, surface an error) rather than waiting
   out the full timeout; every other status keeps polling.
9. Once healthy, browser calls `POST /api/supabase/connection-info` with
   `{access_token, ref}`. Backend relays
   `GET /v1/projects/{ref}/config/database/pooler`, which returns an array
   of pooler configs (one per `pool_mode` × `database_type` combination).
   Backend selects the entry where `pool_mode == "session"` and
   `database_type == "PRIMARY"` (matching the guide's existing "Session-mode
   pooler, port 5432, not 6543" requirement). If no entry matches — the
   array is empty, or contains only `"transaction"`-mode entries — the
   backend returns `{valid: false, reason: "pooler_config_unavailable"}`
   rather than guessing at a substitute; this is expected to be rare (every
   free-tier project observed in this guide's manual flow has always had a
   session-mode pooler) but the endpoint must not crash or return a
   malformed connection string if it happens. Otherwise it returns only the
   matched entry's non-secret shape: `{db_user, db_host, db_port, db_name}`
   — deliberately
   **not** Supabase's own `connection_string`/`connectionString` fields,
   since it cannot be verified from documentation whether those embed the
   real password or a masked placeholder, and there is no way to check
   without a live authenticated call this session cannot make. The browser
   (which already holds `db_pass` from step 7) assembles the final
   `postgresql://{db_user}:{db_pass}@{db_host}:{db_port}/{db_name}` string
   itself, client-side — the password never round-trips through the
   backend a second time, and since the generated password is
   alphanumeric-only, no percent-encoding is ever needed (sidesteps the
   manual guide's existing footgun entirely).
10. Frame locks, showing the project name and organization; the connection
    string sits in `sessionStorage` under its own key, ready for
    sub-project 6 to push as `DATABASE_URL`.

**Token refresh:** any Supabase relay call that comes back with an
`unauthorized` reason triggers one reactive refresh attempt — browser calls
`POST /api/supabase/refresh-access-token` with `{refresh_token}` (if one was
issued; skip straight to a "please re-authorize" error if not), backend
relays the `grant_type=refresh_token` exchange, browser retries the
original call once with the new `access_token`. A second failure surfaces
the error normally. Chosen over a proactive expiry-timer approach (tracking
`expires_in` client-side) for simplicity — no clock-skew bookkeeping, and
this wizard's flow is normally well under typical OAuth token lifetimes.

## 4. API contract

| Endpoint | Request body | Response | Notes |
|---|---|---|---|
| `POST /api/supabase/exchange-oauth-code` | `{code, code_verifier}` | `{valid, access_token, refresh_token, expires_in}` or `{valid: false, reason}` | Mint-and-return exception (alongside GitHub's manifest exchange) |
| `POST /api/supabase/refresh-access-token` | `{refresh_token}` | `{valid, access_token, refresh_token, expires_in}` or `{valid: false, reason}` | Same exception category |
| `POST /api/supabase/list-organizations` | `{access_token}` | `{orgs: [{slug, name}]}` or `{valid: false, reason}` | |
| `POST /api/supabase/create-project` | `{access_token, organization_slug, name, db_pass}` | `{ref, status}` or `{valid: false, reason, message}` | `db_pass` is browser-generated, not visitor-typed — same "already the browser's own value" category as the Render frame's `api_key`. On a 4xx business-rule rejection (free-tier cap included), `reason` is `"project_creation_rejected"` and `message` carries Supabase's own error text verbatim — this endpoint is a documented second exception to "never echo more than a reason enum," justified the same way `exchange-oauth-code` is: relaying Supabase's own wording is the only way to satisfy "surface the actual rejection" without guessing at API behavior this session cannot verify live |
| `POST /api/supabase/project-status` | `{access_token, ref}` | `{status}` or `{valid: false, reason}` | Polled by the browser |
| `POST /api/supabase/connection-info` | `{access_token, ref}` | `{db_user, db_host, db_port, db_name}` or `{valid: false, reason}` | Structurally safe to echo — no password field, ever |

Error `reason` vocabulary: `unauthorized`, `invalid_code`, `forbidden`,
`rate_limited`, `project_creation_rejected` (`create-project` only, paired
with a `message` field — see below), `supabase_unreachable`,
`pooler_config_unavailable` (`connection-info` only — see step 9).

`create-project`'s reason is derived defensively — Supabase's own 4xx
responses have no guaranteed structured body (401/403/429 are documented as
bare descriptions, no schema). The backend distinguishes only what the
status code itself reliably means (`401` → `unauthorized`, `429` →
`rate_limited`) from everything else 4xx, which is treated as a business-
rule rejection: attempt to read a JSON `message` field from the error body
and return `{reason: "project_creation_rejected", message: <that text>}`;
fall back to `{reason: "supabase_unreachable"}` if the body doesn't parse or
has no `message` field. This deliberately does not attempt to distinguish
*which* business rule was violated (free-tier cap vs. anything else
Supabase might reject) — that would require guessing at exact message
wording this session cannot verify live; relaying the message verbatim
lets the visitor read Supabase's own explanation instead.

CSP's `form-action` directive does **not** need a new entry: this flow's
only full-page navigation (`GET /v1/oauth/authorize`) is a plain redirect,
not a form POST, unlike GitHub's manifest step.

Each new relay call gets its own `..._leaves_the_page_exactly_once` test
per `onboarding/CLAUDE.md`'s existing convention.

## 5. Credential handling summary

| Value | Origin | Custody |
|---|---|---|
| `access_token`, `refresh_token` | Minted by Supabase, freshly issued to the visitor who just authorized | Mint-and-return exception, `sessionStorage` |
| `db_pass` | Generated by the browser itself (Web Crypto API, alphanumeric-only) | Browser-originated, same category as the Render frame's `api_key` — never minted server-side |
| Final connection string | Assembled client-side from `db_pass` (already browser-held) + `{db_user, db_host, db_port, db_name}` (structurally non-secret, safe to relay) | Never constructed or held server-side at any point |
| `SUPABASE_OAUTH_CLIENT_ID` / `SUPABASE_OAUTH_CLIENT_SECRET` | Operator-registered once, manually, via Supabase org settings | Onboarding service env vars — the service's first operator-level secret |

## 6. Testing strategy

`supabase_client.py`'s functions are tested with `respx`-mocked `httpx`
responses, mirroring `render_client.py`'s and `github_client.py`'s existing
test structure — no transport-patching workaround needed since nothing here
uses PyGithub. New browser-side tests cover: PKCE verifier/challenge
generation, `state` verification on the OAuth callback, org-picker
show/hide logic (single org vs. multiple), the polling loop's 5s/5min
timeout and its "Check again" fallback, and client-side connection-string
assembly — including that the generated `db_pass` is alphanumeric-only and
the assembled string never needs percent-encoding.

## 7. Known risk, deliberately deferred

The OAuth app is a resource shared across every visitor, unlike every
credential in sub-projects 1 and 2 (each visitor-scoped). One visitor's
abuse, or Supabase's own abuse heuristics, could throttle or revoke the
shared app's Management API access — breaking this frame for every future
visitor, not just the one who triggered it. Same risk category as the
documented Gemini AI-Studio Trust & Safety block in root `CLAUDE.md`'s "LLM
API testing hygiene" section. No mitigation is designed or built in this
sub-project — logged in `ISSUES.md`'s Design Gaps section with candidate
follow-ups (per-visitor rate limiting, a CAPTCHA/friction step, active
revocation monitoring), the same treatment sub-project 2 gave its
credential-residency commitment for sub-project 6.

## 8. Module layout

```
onboarding/supabase_client.py   NEW — exchange_oauth_code, refresh_access_token,
                                 list_organizations, create_project,
                                 get_project_status, get_connection_info
onboarding/config.py            MODIFIED — + supabase_oauth_client_id,
                                 supabase_oauth_client_secret
onboarding/main.py              MODIFIED — lifespan checks the two new settings
onboarding/router.py            MODIFIED — 6 new endpoints (table in section 4)
onboarding/static/index.html    MODIFIED — frame 3 markup + JS
onboarding/CLAUDE.md            MODIFIED — "what sub-project 3 adds" section
```

**Operator setup (one-time, manual, outside this wizard's automation):**
register an OAuth app via Supabase org settings → OAuth Apps → Add
application, redirect URI `{PUBLIC_BASE_URL}/?supabase_step=oauth_callback`,
copy the resulting `client_id`/`client_secret` into the onboarding service's
env vars. This does not compromise the wizard's self-service nature from
any visitor's perspective — it's a one-time cost paid once by the operator,
not per visitor.

## 9. Out of scope

- Region choice for the visitor (fixed default only).
- Resuming/reusing an existing Supabase project if one with the chosen name
  already exists (the orphaned-project decision in section 2 covers this
  case instead).
- The abuse-countermeasure design itself (section 7, deferred to
  `ISSUES.md`).
- Any push of the captured connection string into Render's env vars —
  sub-project 6's job, same boundary sub-project 2 drew for GitHub
  credentials.
- OAuth token revocation on frame re-edit/"Change" (`POST /v1/oauth/revoke`
  exists but isn't wired in this slice).
