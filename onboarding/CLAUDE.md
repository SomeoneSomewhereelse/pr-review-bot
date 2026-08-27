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

## What the implementation adds to these rules

- **The app-wide `RequestValidationError` handler lives in `onboarding/main.py`,
  not `router.py`.** It's what turns a malformed request into the generic
  `{"detail": "invalid request"}` 422 instead of FastAPI's default response
  (which echoes the rejected input, including a submitted credential). Every
  relay endpoint lives in `router.py` and inherits this protection only
  because `main.py` mounts `router` on the same `app` the handler is
  registered on — a non-obvious cross-file dependency. A new relay endpoint
  added to `router.py` gets this for free; the same router mounted on a
  *different* app (a test harness building its own `FastAPI()`, a future
  split-out service) would lose it silently.
- **A visitor's credential goes to `sessionStorage`, never `localStorage`,**
  on the browser side too — not just "no database" on the backend. This
  page's own non-secret theme/language preferences legitimately use
  `localStorage` (they should persist across tabs/sessions); a credential
  must not, since `localStorage` persists past the tab closing. Any new
  frame that holds a visitor secret client-side follows the render-key
  frame's `STORAGE_KEYS` / `sessionStorage` pattern, not the theme/lang one.
- **Every credential-carrying `fetch()` on the page has its own
  `..._leaves_the_page_exactly_once` test in
  `tests/test_onboarding_page.py`,** each asserting
  `body.count('fetch("<that endpoint>"') == 1`. This is deliberate: a visitor
  credential should have exactly one, auditable exit path per page load. The
  check was originally a single blanket `body.count("fetch(") == 1`; it was
  narrowed to per-endpoint counts once frame 2 legitimately added a second
  and third relay call, which is the *only* acceptable way to satisfy it —
  loosening a count to `>= 1`, or dropping one, is not. A new frame that
  adds a relay call adds its own such test alongside; a second `fetch()` to
  an endpoint that already has one is the signal to stop and ask why that
  credential now has two exits, not to bump a number.
  For an endpoint called through the shared `callSupabaseRelay(...)` helper
  (sub-project 3's `list-organizations`, `create-project`, `project-status`,
  `connection-info` — see below) rather than a direct `fetch()`, the audit
  target is `body.count('callSupabaseRelay("<that endpoint>"') == 1`
  instead. This is a faithful adaptation of the same one-exit-path
  invariant to the helper's indirection, not a loosening of it: the
  endpoint string must still appear exactly once as the call's own
  argument, wherever in the call chain that argument is spelled.

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
- **`onboarding/config.py`'s `public_base_url` is checked for *presence* in
  `onboarding/main.py`'s `lifespan`, not as a pydantic-required field** —
  same reasoning as `app/config.py`/`app/main.py`'s own pattern: a
  required field would raise at import time, breaking pytest collection
  before a clear error could ever be reported. Frame 2 cannot construct a
  working manifest without it, so the service still refuses to boot without
  it — just via an explicit check, not a schema constraint.
  Its *shape*, in contrast, does live in a pydantic `field_validator`:
  whitespace and a trailing `/` are stripped, and anything that is not a
  plain `http(s)://` base URL is rejected. That split is deliberate, and the
  shape half is not cosmetic — the value is both concatenated into the
  manifest's `redirect_url`/`setup_url` (a trailing slash yields an
  unroutable `//` path, and the 404 lands *after* the visitor has already
  created a real App whose one-time credentials are then unrecoverable) and
  substituted raw into a `<script>` block on a page holding a GitHub App
  private key (a `"` or `<` there is an injection, not a typo). An unset
  value still normalizes to `""` rather than erroring, so the lifespan check
  stays the one thing that reports a missing value.

## What sub-project 3 (Supabase provisioning) adds to these rules

- **`SUPABASE_OAUTH_CLIENT_ID`/`SUPABASE_OAUTH_CLIENT_SECRET` are this
  service's first operator-level secrets** — every credential before this
  sub-project was either visitor-typed or minted fresh for the visitor who
  requested it. These two are set once by the operator (a one-time, manual
  Supabase OAuth-app registration; Supabase has no self-registration
  mechanism the way GitHub's App Manifest flow does) and never change per
  visitor. `supabase_oauth_client_id` is also templated into the served
  page as `window.SUPABASE_OAUTH_CLIENT_ID` — this is **not** a secret
  exposure: a `client_id` is the public half of OAuth credentials by
  design, and `client_secret` never leaves the backend.
- **`exchange-oauth-code` and `refresh-access-token` are mint-and-return
  exceptions**, same category as GitHub's manifest exchange: they return
  tokens freshly issued to the visitor who just authorized. `create-project`
  is a *different* kind of exception — on a business-rule rejection it
  relays Supabase's own error `message` text verbatim rather than mapping
  to a fixed reason enum, because guessing which specific rule Supabase
  applied (e.g. the free-tier project cap) would require assuming exact
  API wording this project could not verify without a live authenticated
  call — see the design spec section 4 and `ISSUES.md`'s Design Gaps entry.
- **`db_pass` is generated client-side by the browser, never minted by the
  backend** — deliberately different from the GitHub frame's private-key
  pattern. It's a value *we* choose (Supabase doesn't produce it for us the
  way it produces a private key), so keeping it browser-originated avoids
  growing the mint-and-return exception list for a value that doesn't need
  it.
- **`connection-info` never returns Supabase's own `connection_string`/
  `connectionString` fields.** Whether they embed the real password or a
  masked placeholder could not be verified from documentation during this
  sub-project's brainstorm. The endpoint returns only the non-secret shape
  (`db_user`, `db_host`, `db_port`, `db_name`); the browser, which already
  holds `db_pass`, assembles the final connection string itself. A future
  change that trusts Supabase's own connection-string field needs to
  verify its password-masking behavior with a live call first.
- **Token refresh is reactive, not proactive.** `callSupabaseRelay` in
  `onboarding/static/index.html` retries exactly once, only after a real
  `"unauthorized"` response — there is no client-side expiry-timer
  tracking `expires_in`. Any new Supabase relay call should go through this
  same helper rather than calling `fetch()` directly, to inherit the retry
  behavior for free.
- **The OAuth app is a resource shared across every visitor** — unlike
  every other credential in this service. This is a known, deliberately
  deferred risk; see `ISSUES.md`'s Design Gaps section before changing
  anything about how the OAuth app is used or exposed.

## What sub-project 4 (LLM provider credential UI) adds to these rules

- **The model this deployment runs is always fetched live from the
  provider's own catalog, never hardcoded.** `onboarding/llm_client.py`
  makes exactly one models-listing call per credential submission, which
  doubles as validation. No provider's default/fallback model string may
  be hardcoded anywhere in this service — that is exactly the drift this
  sub-project exists to avoid (root `CLAUDE.md`'s substitutions section
  documents the real incident this generalizes from: `gemini-flash-latest`
  404s against Vertex's publisher-model catalog).
- **Gemini and Vertex share one internal helper**
  (`_list_generative_models`) since both go through the same `google-genai`
  SDK and differ only in how `genai.Client` is constructed. A change to the
  filtering/prefix-stripping logic belongs in that shared helper, not
  duplicated per provider. **The `generateContent` filter only applies
  when `supported_actions` is populated** — Vertex's response converter
  (`google/genai/models.py`'s `_Model_from_vertex`) never sets that field
  at all, unlike the Gemini Developer API's converter, so a Vertex model
  is let through rather than dropped when the field is `None`. Filtering
  Vertex strictly on that field (the original implementation) silently
  emptied its entire model catalog for every credential — verify against
  the installed SDK's actual converter functions before changing this
  again, not just against the `Model` type's field list.
- **Groq's model list is deliberately unfiltered** — its `Model` type
  carries no capability field to distinguish chat-completion models from
  Whisper/TTS/moderation ones, and a name-pattern heuristic was
  deliberately rejected as guessing at API behavior this project's
  testing-hygiene discipline warns against. Do not add one without a new
  brainstorm.
- **The frame's unlock gate requires both a live-validated credential AND
  an explicit model pick** — there is no fallback to any baked-in default
  if the visitor skips picking a model. A credential that validates but
  returns zero eligible models is a genuine dead end under this gate; it
  gets its own distinct error message (`err_llm_no_models_available`)
  rather than folding into a generic validation failure.
- **No operator-level settings were added for this sub-project** — unlike
  Supabase's OAuth app, every credential here is visitor-supplied per
  request. `onboarding/config.py` and `onboarding/main.py`'s `lifespan` are
  untouched by it.
- **Gemini/Vertex tests mock at the SDK client boundary
  (`google.genai.Client` itself is monkeypatched), not `respx`** — the
  async listing call itself is `httpx`-based for both providers (verified:
  this environment has no `aiohttp` installed, so `google-genai`'s async
  path falls back to `httpx` regardless of auth type). What `respx` alone
  can't cover is Vertex's separate credential step: a service-account
  refreshes its access token via `google.auth`'s synchronous,
  `requests`-based transport before the `httpx` listing call ever happens,
  and `respx` only intercepts `httpx`. SDK-boundary mocking sidesteps
  needing two different mocking libraries for one code path, and covers
  Gemini and Vertex with the same test shape despite their different
  `Client`-construction inputs (`api_key` vs a service-account credential
  object). Groq's tests use `respx` as normal, since its SDK transport is
  pure `httpx` end-to-end with no separate credential-refresh step.
- **All three credentials share one `fetch(endpoint, ...)` call site**
  in `validateLlmProviderCredential()`, with `endpoint` set to a literal
  per-provider URL string in each branch — a second adaptation of the
  one-exit-path convention (alongside `callSupabaseRelay`'s): audited by
  checking each `endpoint = "/api/llm/<provider>/list-models"` assignment
  appears exactly once, plus the shared call site itself appears exactly
  once. A new provider added to this frame follows the same shape, not a
  new dedicated `fetch()` call.
- **The Vertex credential's storage field name
  (`gcp_service_account_key_b64`) deliberately differs from its wire field
  name (`service_account_key_b64`)** — the frame maps between them in
  `showLlmProviderModels`'s caller. Keep this distinction if either name
  changes: the wire name matches the relay endpoint's pydantic field, the
  storage name matches this service's `GCP_SERVICE_ACCOUNT_KEY`-adjacent
  naming convention for sub-project 6 to read later.

## What sub-project 5 (UptimeRobot keep-warm frame) adds to these rules

- **UptimeRobot's v3 REST API (`Bearer` auth, JSON,
  `https://api.uptimerobot.com/v3/monitors`) is used for every call this
  frame makes — never the legacy v2 form-API.** `scripts/deploy.py`'s
  existing `check_uptime_pinger` still uses v2 for its own read-only
  `getMonitors` check, and that is intentionally untouched — no reason to
  migrate a working read-only check. But v2's `POST /newMonitor` was
  verified live to reject monitor creation on a free-plan account (`403
  "You are not allowed to use some settings with your current plan"`),
  while v3 was verified live to accept the identical creation on the same
  account. Do not "simplify" this frame's client onto v2 without
  re-verifying that live behavior first.
- **This frame reads a `sessionStorage` key it does not write:
  `onboarding.renderServiceUrl`.** Sub-project 6 (Render service creation,
  not yet built as of this frame) is obligated to write the deployed
  service's base URL there on its own completion — see
  `docs/superpowers/specs/2026-08-27-onboarding-uptimerobot-frame-design.md`
  section 3's forward contract. Until sub-project 6 exists, frame 5's only
  reachable state is the blocked message (`frame5_blocked_no_render_url`)
  — this is expected, not a bug. If sub-project 6's actual output shape
  ends up different from a bare base URL when it's built, reconcile against
  that key name and format, not against a guess made here.
- **Dedupe-before-create is load-bearing, not an optimization.** Every
  credential submission to this frame (including a "Change" resubmit)
  calls `GET /v3/monitors` before ever calling `POST /v3/monitors` — a
  monitor is only created if none already watches the derived
  `<render_service_url>/healthz` target. Removing this check reintroduces
  orphaned duplicate monitors on every resubmit.
- **There is no way to detect a read-only (Monitor-Specific) API key
  server-side — verified live.** `POST /v3/monitors` and `GET
  /v3/monitors` both return the identical `401 {"message": "Invalid
  token.", "code": "003-005"}` for a valid-but-read-only key as for a
  wholly invalid one. Do not add a `reason` value implying this frame can
  tell the two apart; the only mitigation is UI copy (the input's help
  text and the `unauthorized` error both name the Main-API-Key requirement
  explicitly).
- **The monitor's `friendlyName`/`type`/`interval`/`timeout` are fixed, not
  visitor-configurable** — `friendlyName` is always the derived target URL
  itself (matching this project's own production monitor's existing
  naming), `type: "HTTP"`, `interval: 300`, `timeout: 30`. A future change
  that lets the visitor choose these needs its own brainstorm, not a quiet
  addition here.
- **`onboarding/uptimerobot_client.py` follows the same raw-`httpx`, no-SDK
  shape as `render_client.py`/`github_client.py`/`supabase_client.py`** —
  UptimeRobot has no official SDK. Tests mock via `respx`, same as those
  three modules, not the SDK-boundary mocking `llm_client.py`'s tests
  needed for `google-genai`.

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
  regression. One path is exempt by design, not by oversight: if the
  Render-service frame was never completed, the push-and-clear step for
  frames 2/3/4 is skipped entirely (no attempt, no clear — see the
  push-failure-handling decision above), which means those credentials
  sit in `sessionStorage` for the rest of the session in that specific
  case. This is the documented, accepted tradeoff of "never gate a
  frame's completion on Render being reachable," not a gap to close.
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

## The test suite looks hung on a fresh worktree — it isn't

The **first** `uv run pytest` (or any `uv run ...`) invocation in a newly
created worktree can take 5-6+ minutes before any test output appears, with
no progress indication in between. This is not a real hang and nothing to
debug in this project's code — it is `uv` building that worktree's `.venv`
from scratch (this repo lives on a Windows-mounted drive under WSL2 —
`/mnt/c/...` — while `uv`'s package cache lives on a different filesystem,
so `uv` cannot hardlink packages into the new `.venv` and falls back to a
full byte-for-byte copy of every package instead; confirmed directly: a
fresh-worktree install of this project's 79 dependencies took 5m37s,
preceded by `uv`'s own `warning: Failed to hardlink files; falling back to
full copy` — the exact fallback this is). Every *new* SDD/git worktree pays
this cost again, since each starts with an empty `.venv`.

**Practical consequence:** give the first test/`uv run` command in a fresh
worktree a long timeout (10+ minutes) or run it in the background and wait
for completion, rather than killing it or concluding something is broken
partway through. Subsequent runs in the *same* worktree reuse the now-built
`.venv` and run at normal speed (this project's own suite: ~20-45s, per
`pyproject.toml`'s `[tool.pytest.ini_options]` xdist tuning comments).
`export UV_LINK_MODE=copy`
suppresses the warning text (it does not speed up the copy — the fallback
already happens either way) if the noise itself is what's confusing; the
real fix (giving `uv`'s cache and the worktree the same filesystem) is a
workstation-level environment decision, out of scope for this project's own
code or config to make on its own.
