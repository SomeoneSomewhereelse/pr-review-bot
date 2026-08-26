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
