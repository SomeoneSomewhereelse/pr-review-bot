# Design — Self-service onboarding wizard: UptimeRobot keep-warm frame

**Date:** 2026-08-27
**Status:** Approved for planning
**Relates to:** `onboarding/` (shipped in sub-projects 1-4, see
`docs/superpowers/specs/2026-08-26-onboarding-wizard-render-frame-design.md`,
`docs/superpowers/specs/2026-08-26-onboarding-github-app-frame-design.md`,
`docs/superpowers/specs/2026-08-26-onboarding-supabase-provisioning-frame-design.md`,
`docs/superpowers/specs/2026-08-27-onboarding-llm-provider-frame-design.md`
— this frame reuses their stateless-relay architecture, `sessionStorage`
credential custody, and accordion lock/unlock state machine),
`guide/setup/hosted/08-pinger.md` (the manual process this eventually
replaces), `scripts/deploy.py`'s `check_uptime_pinger` (the existing
read-only UptimeRobot v2 integration this frame's write path sits
alongside, not on top of).

## 1. Problem and context

This is the **fifth of six sub-projects** decomposing the self-service
onboarding wizard (full decomposition recorded in the sub-project 1 spec).
Sub-projects 1-4 shipped the accordion shell, Render-key capture, GitHub App
automation, Supabase provisioning, and LLM provider credential capture. This
spec covers frame 5: replacing `guide/setup/hosted/08-pinger.md`'s manual
"go create a monitor by hand" step with an in-wizard credential capture that
creates the keep-warm monitor itself.

**A dependency this sub-project cannot resolve on its own:** the monitor
needs the deployed Render service's URL, which does not exist until
sub-project 6 (Render service creation) runs. In the wizard's current
built-so-far frame order, frame 5 sits *before* frame 6. Per the
brainstorm's ruling, this sub-project builds frame 5 as if that URL is
available in `sessionStorage` under a key sub-project 6 is obligated to
write (section 3) — the ordering mismatch itself is resolved separately,
later, by reordering the accordion so the Render-creation frame runs
immediately after frame 1's Render-key capture, once sub-project 6 exists.
Until then, frame 5 has exactly one reachable state: blocked, waiting on
that key (section 3's "Missing dependency" flow) — this is expected, not a
bug, for the remainder of this build phase.

**A live finding that reversed the frame's original assumption:**
UptimeRobot's **legacy v2 API** (`POST /v2/newMonitor`, the same family
`scripts/deploy.py`'s existing `check_uptime_pinger` uses for its read-only
`getMonitors` call) rejects monitor creation on a free-plan account with
`403 {"error": {"type": "access_denied", "message": "You are not allowed to
use some settings with your current plan."}}` — verified live, reproduced
identically with and without an explicit `interval` param, against an
account confirmed by `getAccountDetails` to be genuinely free-plan
(`active_subscription: null`, `payment_processor: null`) and nowhere near
its `monitor_limit` (50 allowed, 1 in use). This looked like a hard blocker
matching this project's prior free-tier gates (Supabase's 2-project cap,
Vertex's GCP billing-card requirement) — until a live check against
UptimeRobot's **newer v3 REST API** (`Bearer` auth, JSON body,
`https://api.uptimerobot.com/v3/monitors`) showed monitor creation working
normally on the same free-plan account. The v2 restriction is specific to
that legacy endpoint, not a real plan-wide limitation. This frame uses v3
exclusively for creation; `scripts/deploy.py`'s existing v2 `getMonitors`
check is untouched (read-only, already working, no reason to migrate it).

## 2. Confirmed decisions (this sub-project)

| Decision | Choice |
|---|---|
| API version | **v3 REST API** (`Bearer` token, JSON) for everything this frame does — list (dedupe check) and create. Not v2 — verified live to reject creation on a free-plan account (section 1); v3 verified live to accept it. |
| Render URL source | Read from a new `sessionStorage` key, `onboarding.renderServiceUrl`, that this spec obligates **sub-project 6** to write on its own completion. Frame 5 does not ask the visitor to type a URL — matches this project's "full automation where no delegated-auth API gap forces plain key-paste" posture (sub-project 1's Automation-depth decision), since the URL is a value the wizard itself will have produced, not an external credential. |
| Missing-dependency handling | If `onboarding.renderServiceUrl` is absent when frame 5 is reached, show a distinct blocked state ("complete your Render deployment first") instead of the credential form. No polling, no auto-retry — the visitor re-opens frame 5 after finishing whatever frame produces that key. |
| Monitor-path derivation | Backend strips the submitted URL's trailing slash and appends `/healthz` server-side, mirroring `scripts/deploy.py::check_uptime_pinger`'s existing `f"{base}/healthz"` convention exactly, rather than trusting the browser to have already formed the full monitored path. |
| Dedup-before-create | Before calling `POST /v3/monitors`, call `GET /v3/monitors` and check for an existing monitor whose `url` exactly matches the derived target. If found, treat it as success without creating a duplicate (matches `check_uptime_pinger`'s own exact-URL-match convention). Prevents orphaned duplicates from repeated "Change" resubmits. |
| Read-only-key detection | **Not possible, verified live.** A Monitor-Specific (read-only) API key returns the *identical* `401 {"message": "Invalid token.", "code": "003-005"}` as a garbage/wrong token against both `GET /v3/monitors` and `POST /v3/monitors` — there is no server-side signal to distinguish "wrong key" from "right key, wrong scope." Mitigated at the UI-copy level instead (section 3): the input's help text and the generic `unauthorized` error both name the Main-API-Key requirement explicitly, rather than inventing a `reason` value the backend cannot actually verify. |
| Monitor `friendlyName` | Set to the derived target URL itself — matches this project's own existing production monitor's convention (`getAccountDetails`'s live response showed `friendly_name: "pr-review-engine.onrender.com/healthz"`, i.e. host+path, not a separate custom label). |
| Monitor type / interval / timeout | Fixed `type: "HTTP"`, `interval: 300` (5 minutes, matching `guide/setup/hosted/08-pinger.md`'s existing instruction and `scripts/deploy.py`'s `_MAX_PINGER_INTERVAL_SECONDS = 600` ceiling), `timeout: 30` — no visitor choice, verified live as an accepted combination on a free-plan account. |

## 3. Architecture and data flow

`onboarding/uptimerobot_client.py` (new) — raw `httpx`, no SDK (UptimeRobot
publishes none), same shape as `render_client.py` / `github_client.py` /
`supabase_client.py`'s existing raw-`httpx` integration layer. Exposes one
function: `create_or_reuse_monitor(api_key: str, render_service_url: str) ->
UptimeRobotMonitorResult | UptimeRobotApiFailed`, where
`UptimeRobotMonitorResult` carries only `created: bool` — nothing else from
either UptimeRobot call is consumed downstream, so nothing else is kept.

**Backend logic, inside `create_or_reuse_monitor`:**

1. Strip `render_service_url`'s trailing slash/whitespace, append `/healthz`
   → `target_url`.
2. `GET https://api.uptimerobot.com/v3/monitors` with
   `Authorization: Bearer <api_key>`, `?limit=50` (matching this project's
   own account's live-verified `monitor_limit`, so a single page covers any
   account within that ceiling — documented assumption, not re-verified
   against an account with more than one monitor, since this project's own
   demo account only ever has the one).
3. If any returned monitor's `url` exactly equals `target_url`: return
   `UptimeRobotMonitorResult(created=False)`.
4. Otherwise, `POST https://api.uptimerobot.com/v3/monitors` with JSON body
   `{"friendlyName": target_url, "url": target_url, "type": "HTTP",
   "interval": 300, "timeout": 30}`. On success (`201`), return
   `UptimeRobotMonitorResult(created=True)`.
5. Error mapping (both calls share one handler): `401` → `unauthorized`
   (verified live shape: `{"message": "Invalid token.", "code": "003-005"}`
   — this is also what a valid-but-read-only key produces, section 2);
   `429` → `rate_limited`; any `5xx` or `httpx` network-level exception →
   `provider_unreachable`; any other non-2xx (e.g. a `400` validation
   error) → `request_rejected` — a catch-all, not verified live (this
   frame's fixed request shape was itself verified to succeed, so a `400`
   here would indicate something this design didn't anticipate rather than
   a routine visitor-facing case).

No new `onboarding/config.py` settings, no `onboarding/main.py` lifespan
changes — the credential is visitor-supplied per request, same territory
as the Render and LLM-provider frames.

**Flow:**

1. Visitor's browser reads `sessionStorage["onboarding.renderServiceUrl"]`
   when frame 5 is opened (on unlock, and again each time the frame is
   re-rendered — mirrors how other frames re-check their own preconditions
   on open rather than caching a stale read).
2. **Missing:** frame 5 shows the blocked state (section 2) — a `<p>`
   with a `data-i18n="frame5_blocked_no_render_url"` message, no input
   field, no submit button. This is the frame's only reachable state until
   sub-project 6 exists and writes that key.
3. **Present:** frame 5 shows the normal form — a password-style API-key
   input (help text names the Main-API-Key requirement, section 2) and a
   "Validate & create monitor" button.
4. Visitor pastes their key, clicks submit. Browser calls
   `POST /api/uptimerobot/create-monitor` with
   `{api_key, render_service_url}` — `render_service_url` is the raw
   stored value, unmodified; the backend does all derivation (section 3),
   keeping the browser from needing to duplicate the `/healthz`-appending
   logic `check_uptime_pinger` already owns server-side.
5. Success (`created: true` or `created: false`, both are success from the
   visitor's perspective): browser shows "✓ monitor created" or
   "✓ existing monitor found and reused" respectively, stores `{api_key}`
   under `sessionStorage["onboarding.uptimeRobot"]`, and
   completes/locks the frame via the existing `completeFrame` state
   machine, unchanged.
6. Failure: browser shows the reason-mapped error (section 4) — frame
   stays open, nothing written to `sessionStorage`.

**Note for sub-project 6 (forward contract):** on completing Render service
creation, sub-project 6 must write the deployed service's base URL (no
trailing slash, no path) to `sessionStorage["onboarding.renderServiceUrl"]`
before or as part of completing its own frame. This is the only interface
this sub-project exposes to a sub-project that does not yet exist; if
sub-project 6's actual output shape differs when it's built, that key name
and format are the contract to reconcile against, not something frame 5
should guess further at now.

## 4. API contract

| Endpoint | Request body | Response | Notes |
|---|---|---|---|
| `POST /api/uptimerobot/create-monitor` | `{api_key, render_service_url}` | `{valid: true, created: bool}` or `{valid: false, reason}` | `reason` ∈ `unauthorized`, `rate_limited`, `provider_unreachable`, `request_rejected` (section 3) |

Each new relay call gets its own `..._leaves_the_page_exactly_once` test
per `onboarding/CLAUDE.md`'s existing convention.

## 5. Credential handling summary

| Value | Origin | Custody |
|---|---|---|
| `api_key` | Visitor-typed (must be their Main API Key — read-only keys are indistinguishable from invalid ones at this API, section 2) | `sessionStorage`, relayed per-request, never logged |
| `render_service_url` | Read from `sessionStorage`, written there by sub-project 6 (not yet built) | Non-secret (a public URL); relayed per-request as-is, backend derives the actual monitored path |

No mint-and-return exceptions — every value here is either visitor-supplied
or a non-secret value already present from an earlier step.

## 6. Testing strategy

`respx` mocks `httpx` directly (UptimeRobot's v3 API is pure REST, no SDK,
same shape as `render_client.py`/`github_client.py`/`supabase_client.py`'s
existing tests). Per root `CLAUDE.md`'s LLM-testing-hygiene section this
frame is out of scope for that section's stricter rule (UptimeRobot is not
an LLM provider), but the project-wide "no real network call in the test
suite" convention still applies — every UptimeRobot call in
`tests/test_onboarding_uptimerobot_client.py` is mocked.

Test coverage: create-when-absent (mocked `GET` returns no match, mocked
`POST` returns `201` → `created: true`); reuse-when-present (mocked `GET`
returns a matching `url` → `created: false`, no `POST` call made — assert
the mock's `POST` route was never called, not just that the right value
came back); each error-reason mapping (`401`/`429`/`5xx`/network
exception/other-4xx); the `/healthz`-derivation and trailing-slash-stripping
logic in isolation; and a credential-never-logged test matching the
convention every prior sub-project's client module has.

Browser-side tests cover: the missing-`renderServiceUrl` blocked state
(mocked `sessionStorage` without that key renders the blocked message, no
form); the present-URL normal form path; and the
`..._leaves_the_page_exactly_once` test for the new endpoint.

## 7. Module layout

```
onboarding/uptimerobot_client.py   NEW — create_or_reuse_monitor
onboarding/router.py               MODIFIED — 1 new endpoint (table in section 4)
onboarding/static/index.html       MODIFIED — frame 5 markup + JS (blocked
                                      state, credential form, result handling)
onboarding/CLAUDE.md               MODIFIED — "what sub-project 5 adds" section
```

`pyproject.toml` is unchanged — `httpx` is already a project dependency.

## 8. Out of scope

- Writing `sessionStorage["onboarding.renderServiceUrl"]` — that is sub-
  project 6's job; this sub-project only documents the contract (section 3)
  and consumes the key once it exists.
- Reordering the accordion so frame 6 runs before frame 5 in the visitor-
  facing flow — a separate, later change once sub-project 6 exists, not
  part of this sub-project's build.
- Any UI to let the visitor pick a different interval, timeout, or monitor
  type — all three are fixed (section 2).
- Detecting or specifically messaging "you pasted a read-only key" as a
  distinct error `reason` — verified live to be indistinguishable from an
  invalid key at the API level (section 2); mitigated via UI copy only.
- Deleting or updating a previously created monitor if the visitor changes
  their Render service later — matches the LLM-provider frame's precedent
  (no proactive re-validation; the existing "Change" action already covers
  on-demand resubmission, which itself dedupes via section 3's logic rather
  than ever leaving a stale monitor pointed at an old URL cleaned up
  automatically).
- Verifying `GET /v3/monitors`' pagination behavior beyond a single
  `?limit=50` page (section 3) — not re-tested against an account with
  more than one monitor.
