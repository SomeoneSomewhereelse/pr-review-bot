# Design — Onboarding wizard: fully manual GitHub App creation + validation checklist (frame 4)

**Date:** 2026-09-01
**Status:** Approved for planning
**Relates to:** `docs/superpowers/specs/2026-08-26-onboarding-github-app-frame-design.md`
(the manifest-flow design this replaces), `docs/superpowers/specs/2026-08-27-onboarding-render-service-frame-design.md`
(the Render-service frame this depends on for the webhook URL), `bot/scripts/doctor.py`
(the check pattern this generalizes for a visitor-supplied credential),
`guide/setup/02-github-app.md`/`03-install-app.md` (the already-proven-safe
manual process this mirrors), `onboarding/CLAUDE.md`, `ISSUES.md`.

## 1. Problem and context

Frame 4 currently automates GitHub App creation via GitHub's App Manifest
flow: a JS-constructed `<form>` auto-submits a POST to
`https://github.com/settings/apps/new`, navigating the visitor's browser to
GitHub, which redirects back with a one-time code this service exchanges for
the App's credentials. Installation was already made fully manual in an
earlier fix (2026-08-31, see `onboarding/CLAUDE.md`) after five throwaway
GitHub accounts were suspended for a ToS violation triggered at the
*install*-page navigation step specifically.

A GitHub account has since been suspended again while going through frame 4
— this time with the install-page fix already in place, so the manifest
*creation* flow's automated form-POST navigation is the next most likely
remaining source, though **this is not confirmed**: the account's age,
GitHub's App-creation rate during this project's own testing, or some other
factor could equally explain it, and this spec does not treat automation as
a proven cause. Given the uncertainty, the response is to remove the
remaining automated navigation as a precaution — the same posture the
install-page fix already took — rather than to keep debugging a live
suspension risk against a service GitHub gives no diagnostic feedback for.

This sub-project makes App creation fully manual, the same way installation
already is, and compensates for losing the manifest flow's built-in
correctness (a Manifest always creates the App with exactly the permissions/
events/webhook URL requested) with an explicit, doctor.py-style validation
checklist: the visitor pastes back App ID + private key, and the wizard
reads the App's *actual* configuration from GitHub's API and reports
per-item pass/fail, unlocking the frame only once everything matches.

## 2. Confirmed decisions

| Decision | Choice |
|---|---|
| App creation | Fully manual — visitor follows on-page breadcrumb instructions (`Settings → Developer settings → GitHub Apps → New GitHub App`), no URL of any kind rendered on the page, no automated navigation or form-POST |
| Credentials collected | App ID (numeric input) + private key (file picker → base64 client-side, reusing `readFileAsBase64`) |
| Webhook secret | Generated client-side by the wizard (`crypto.getRandomValues`, same pattern as the dashboard frame's session secret) and displayed once with a copy affordance — never asked to be pasted back, since GitHub's API never returns it for comparison anyway |
| Installation | Auto-discovered server-side (`GET /app/installations` under the visitor's App JWT), not typed by the visitor — mirrors `bot/github_app.py::discover_installation_id_for_app`'s exactly-one-installation expectation |
| Validation trigger | Explicit "Validate" button, matching the LLM-provider frame's pattern — no live/auto-validation on keystroke |
| Checklist granularity | One line per individual requirement (each permission, the event, installation, webhook URL) — not one rolled-up line per category the way `doctor.py`'s CLI table does |
| Check-run semantics | All checks run every time, none short-circuits another (mirrors `doctor._safe`'s "a complete table is the deliverable") — the one exception is a hard credentials failure (bad key / `GET /app` itself failing), which has nothing to diff against and skips the rest |
| Scope | This wizard only — `bot/scripts/create_github_app.py`, its CLI flag, and `guide/setup/02-github-app.md` are untouched; a human operator knowingly running a script once is a different threat model than an anonymous visitor's browser |

## 3. Frame layout and flow

The frame keeps its existing two-part shape but the parts change meaning:
"create" becomes **instructions + credential entry**, "install" is folded
into the same validation pass (installation is just another checked item,
not a separate step with its own button).

### Section A — Instructions (always visible once unlocked)

Rendered from the frame's known values (the Render service's URL is already
known — the Render-service frame completes two frames earlier) and a
client-side-generated webhook secret:

- Breadcrumb: "Go to **Settings → Developer settings → GitHub Apps → New
  GitHub App**" — text only, no `<a>`, no URL string anywhere in the DOM.
- **Homepage URL**: suggest `window.ONBOARDING_BASE_URL` (not validated —
  cosmetic only, matches the old manifest's `url` field).
- **Webhook → Active**: check it.
- **Webhook → Webhook URL**: the real value, `${renderService.service_url}/webhook`,
  shown for copying (validated later).
- **Webhook → Webhook secret**: the wizard-generated value, shown once with
  a copy button (not independently validated — see decisions table).
- **Repository permissions** table, generated from `REQUIRED_PERMISSIONS`
  (see section 4): Contents Read-only, Issues Read and write, Pull requests
  Read and write, Metadata Read-only (automatic).
- **Subscribe to events**: Pull request only.
- **Where can this GitHub App be installed?**: Only on this account.
- A second instruction block, below, for installation: "Once created,
  install the App on your account/repos from the App's own settings page in
  GitHub's UI" — again breadcrumb-only, no URL, preserving the existing
  install-page policy (`test_page_offers_no_route_to_the_install_page_at_all`)
  and extending it to also cover the App's own creation-page URL, enforced
  by a new sibling test.

### Section B — Credential entry + validation

- App ID input (numeric).
- Private key file picker (`accept=".pem"`), converted to base64 via
  `readFileAsBase64` on validate, same as the Vertex credential's JSON file
  today.
- **Validate** button → `POST /api/github/validate-app`.
- Result: a list of checklist rows, red/green, one per item (see section 5
  for the exact shape). Re-clicking Validate after fixing something in
  GitHub's UI re-runs the same request with the same locally-held
  credentials — no re-upload needed unless the visitor picks a different
  key file.
- When every row passes, the frame pushes the four env vars to the already-
  created Render service (`POST /api/github/push-render-vars`, endpoint
  unchanged) and completes — same push-and-clear pattern every other frame
  uses (private key/base64 cleared from the in-memory record before
  re-persisting to `sessionStorage`, per `onboarding/CLAUDE.md`'s existing
  convention).

## 4. `REQUIRED_PERMISSIONS` / `REQUIRED_EVENTS`

Renamed from today's `MANIFEST_PERMISSIONS`/`MANIFEST_EVENTS` in both the JS
and (new) Python side — there is no manifest anymore, only a requirement to
check against. Values are unchanged and still duplicated by hand between
`bot/scripts/create_github_app.py` (source of truth, since the CLI script
still builds a real manifest) and this wizard's two sides (Python backend
that runs the diff, JS frontend that renders the instructions table) —
kept in sync by a renamed test,
`test_required_permissions_match_the_cli_script` (was
`test_manifest_permissions_match_the_cli_script`), which now checks both the
Python and JS copies against `create_github_app.py`'s originals.

```python
REQUIRED_PERMISSIONS = {
    "pull_requests": "write",
    "contents": "read",
    "issues": "write",
    "metadata": "read",
}
REQUIRED_EVENTS = ("pull_request",)
```

## 5. Backend: `onboarding/github_client.py`

Removed: `exchange_manifest_code`, `GithubAppCreated`, `GithubAppExchangeFailed`,
`verify_installation`, `InstallationVerified`, `InstallationInvalid`,
`_fetch_installation`.

Added, each built independently against the visitor's own App JWT (per
`onboarding/CLAUDE.md`'s rule that `onboarding/` never imports from `bot/`'s
credential-handling code, so these are fresh implementations of the same
idea `bot/github_app.py` already has for the operator-side doctor checks,
not a shared import):

```python
def _app_jwt_headers(app_id: int, private_key_pem: str) -> dict[str, str]:
    """Mints a short-lived App JWT (PyJWT, RS256) and returns the Authorization
    header for raw httpx calls -- no PyGithub client construction needed for
    the three read-only calls this module makes."""

async def get_app_permissions(app_id: int, private_key_pem: str) -> AppPermissionsResult:
    """GET /app under the App JWT. Returns AppPermissionsFetched(permissions,
    events) or AppCredentialsInvalid(reason) where reason is
    "invalid_key" | "unauthorized" | "github_unreachable" -- a 401/403/404
    here means the App ID/private key pair itself doesn't check out, which
    is a harder failure than any individual permission mismatch and skips
    the rest of validate_app's checks (nothing to diff without it)."""

def diff_required_permissions(
    actual_permissions: dict[str, str], actual_events: list[str]
) -> tuple[list[PermissionCheck], list[EventCheck]]:
    """Structured per-item results against REQUIRED_PERMISSIONS/REQUIRED_EVENTS
    -- one entry per required permission/event (not the human-readable
    under/over string lists bot/github_app.py's own diff_app_permissions
    returns, since the UI renders one colored line per item). Rank-based
    comparison (read < write < admin), same as bot/github_app.py's, so a
    broader-than-needed permission still passes (least-privilege nit, not a
    blocker here -- there's no operator to WARN, just a visitor whose App
    already works)."""

async def discover_installation(app_id: int, private_key_pem: str) -> InstallationResult:
    """GET /app/installations under the App JWT. Returns exactly one of:
    InstallationFound(account_login, repo_scope), InstallationNotFound(),
    or MultipleInstallationsFound(account_logins) -- mirrors
    bot/github_app.py::discover_installation_id_for_app's exactly-one
    expectation, adapted to return a result type instead of raising."""

async def get_webhook_url(app_id: int, private_key_pem: str) -> WebhookUrlResult:
    """GET /app/hook/config under the App JWT. Returns WebhookUrlFetched(url)
    or WebhookConfigUnavailable(reason)."""

async def validate_app(
    app_id: int, private_key_b64: str, expected_webhook_url: str
) -> AppValidationResult:
    """Orchestrates the above. Decodes the private key first (invalid base64
    -> AppCredentialsInvalid("invalid_key"), no network call attempted).
    On AppCredentialsInvalid from get_app_permissions, returns immediately
    with only that one failure -- no permissions/events/installation/webhook
    data to report. Otherwise computes diff_required_permissions
    synchronously from the already-fetched permissions/events, and runs
    discover_installation and get_webhook_url concurrently (asyncio.gather)
    -- one check's failure never hides another's result, same principle as
    doctor.py's _safe wrapper ("no check may abort the run"). Returns
    AppValidated(permissions, events,
    installation, webhook) bundling every sub-result; the caller (router)
    decides whether "all passed" from the bundle, not this function --
    keeps the pass/fail policy in one place (the router's response
    construction) rather than duplicated between this function and its
    caller."""
```

Never logs the private key (same sensitivity tier as `GITHUB_APP_PRIVATE_KEY`,
per root and `onboarding/CLAUDE.md`); narrow `except` clauses mapping each
failure mode to a named reason, no bare `except Exception`.

## 6. Backend: `onboarding/router.py`

Removed: `/api/github/exchange-manifest-code`, `/api/github/verify-installation`,
`GithubManifestCodeRequest`, `GithubInstallVerifyRequest`.

Added:

```python
class GithubValidateAppRequest(BaseModel):
    app_id: int = Field(gt=0)
    private_key_b64: str = Field(max_length=16384)
    expected_webhook_url: str = Field(max_length=2048)

@router.post("/api/github/validate-app")
async def validate_github_app(payload: GithubValidateAppRequest) -> dict:
    result = await github_client.validate_app(
        payload.app_id, payload.private_key_b64, payload.expected_webhook_url
    )
    ...  # serializes AppCredentialsInvalid -> {"valid": False, "reason": ...}
         # or AppValidated -> {"valid": True, "permissions": [...], "events": [...],
         #                      "installation": {...}, "webhook": {...}}
```

`expected_webhook_url` is computed client-side from the already-known
`renderService.service_url` and sent up rather than recomputed server-side —
this service holds no state to recompute it from, consistent with every
other relay endpoint's stateless design.

`/api/github/push-render-vars` is unchanged (same four env vars; the values
just arrive from manual entry + discovery instead of a manifest exchange).

## 7. Frontend: `onboarding/static/index.html`

Removed: `buildManifest`, `submitManifestForm`, `createGithubApp`,
`handleGithubManifestCallback`, `showGithubAppReadyToInstall`,
`handleGithubInstallReturn`, `markGithubAppInstalled`, `GITHUB_MANIFEST_STATE_KEY`,
`MANIFEST_PERMISSIONS`/`MANIFEST_EVENTS` (renamed, see section 4), all
`?gh_step=...` query-param handling, the `github-app-install-section` markup
(folded into the single validation section).

Added: instruction-rendering (permissions table, webhook URL/secret display,
breadcrumb text), the App-ID + file-picker inputs, `validateGithubApp()`
(mirrors `validateLlmProviderCredential()`'s file-read-then-fetch shape),
checklist-rendering from the structured response, and
`finishGithubAppSetup`'s push-and-clear call site updated to read from the
new validated-state shape instead of `installation_id`/`account_login`
fields set by the old `markGithubAppInstalled`.

New i18n error keys replace the removed manifest/install ones:
`err_github_no_file` (no private key file chosen), `err_github_invalid_key_file`
(file doesn't decode/parse as a key), plus the existing `err_github_name_empty`→
repurposed validation copy and `err_github_no_render_service` (unchanged,
still a hard precondition — now checked before rendering the webhook URL
instruction, not before a manifest POST).

CSP: the `form-action 'self' https://github.com;` directive added for the
old manifest form-POST is removed — there is no cross-origin form
submission left in this frame.

## 8. Error handling

- Credentials-level failures (`invalid_key`, `unauthorized`, `github_unreachable`)
  short-circuit `validate_app` — no checklist has any data to render besides
  "Credentials accepted: ✗ <reason>".
- Once credentials are accepted, every other check runs regardless of the
  others' outcomes, and the response always includes all of them — the
  visitor never needs more than one round of "fix everything GitHub-side,
  click Validate once more" to reach all-green from any starting point.
- Multiple installations (`MultipleInstallationsFound`) is a distinct,
  named failure state on the "Installation" row, not folded into
  "not found" — same distinction `discover_installation_id_for_app` already
  draws, surfaced to the visitor as "multiple installations found — this
  wizard expects exactly one" rather than a generic error.
- No retry-with-backoff on the Validate action itself — one manually
  triggered click, consistent with every other frame's credential-check
  buttons (Render key, Supabase org picker, LLM provider).

## 9. Testing strategy

- `onboarding/tests/test_onboarding_github_client.py`: remove
  `exchange_manifest_code`/`verify_installation` tests; add respx-mocked
  coverage for `get_app_permissions` (success, 401/403/404, malformed body),
  `diff_required_permissions` (exact match, under, over, missing event,
  extra event), `discover_installation` (zero/one/multiple), `get_webhook_url`
  (match, mismatch, unreachable), and `validate_app`'s orchestration
  (credentials-failure short-circuit vs. full-bundle-on-success, confirming
  it runs concurrently and that one failing sub-check doesn't suppress the
  others' results in the bundle).
- `onboarding/tests/test_onboarding_router.py`: replace the two removed
  endpoint tests with `/api/github/validate-app` coverage (valid, each
  failure reason, response-shape assertions for the nested
  permissions/events/installation/webhook structures).
- `onboarding/tests/test_onboarding_page.py`: remove
  `test_setup_url_return_only_prefills_the_id_it_never_bypasses_verification`
  and the `buildManifest`/`hook_attributes` assertions; add tests for the
  new instructions markup, file-picker input, checklist rendering, and the
  renamed `test_required_permissions_match_the_cli_script`. Add a new
  sibling to `test_page_offers_no_route_to_the_install_page_at_all`
  asserting no `github.com/settings/apps` (or any bare `github.com` URL)
  string appears anywhere in the served page, extending the existing
  install-page policy to also cover App creation.
- No live GitHub API calls in any test, consistent with this project's
  existing testing hygiene.

## 10. Out of scope

- `bot/scripts/create_github_app.py`, its CLI flag, and
  `guide/setup/02-github-app.md`/`03-install-app.md` — untouched, different
  threat model (see section 2).
- Confirming the actual root cause of the suspension — not knowable from
  inside this project without support access to the affected GitHub
  account; this spec treats automation removal as a precaution, not a
  verified fix, and says so in `ISSUES.md`.
- `GITHUB_TARGET_REPO` — still never pushed by this wizard (track-all mode),
  unchanged from today.
- Any change to how `doctor.py`/`bot/github_app.py` do their own
  operator-side checks — this spec only adds an independent, visitor-facing
  equivalent; the two are not unified into shared code, per
  `onboarding/CLAUDE.md`'s existing rule.
