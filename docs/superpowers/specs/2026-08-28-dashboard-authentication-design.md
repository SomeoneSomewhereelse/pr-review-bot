# Design — Dashboard authentication

**Date:** 2026-08-28
**Status:** Approved for planning
**Relates to:** `app/dashboard.py`, `app/main.py`, root `CLAUDE.md`'s "Secret
handling" section, `guide/operations/config-files.md` (the `.env`/`.env.config`
split this reuses unchanged), `scripts/deploy.py` (`_wanted_env()`/`sync_env()`,
which must push the new credentials), the parked "Dashboard 'Environment'
feature" entry in `ISSUES.md`'s Design Gaps section — this spec is the
prerequisite that entry names before that feature can resume.

## 1. Problem and context

`app/dashboard.py`'s router (`GET /`, `GET /api/dashboard`) has never had any
authentication — it was built as a read-only ops/demo view of ticket and
review stats, low-stakes if public. That stopped being true the moment a
feature was proposed to let the dashboard fetch and modify live Render env
vars (most of this project's real secrets — API keys, `DATABASE_URL`,
`GITHUB_WEBHOOK_SECRET`, `GCP_SERVICE_ACCOUNT_KEY`) and the `runtime_config`
DB table (active provider/model, active key slot, cooldown tuning, usage
caps). Shipping that on top of an unauthenticated public page would let
anyone who can reach the Render URL read and rewrite the running service's
entire configuration and credential set. This spec closes that gap first,
as its own sub-project, before the Environment feature's own design resumes.

Single-operator project (one shared credential is enough — no per-operator
identity or audit trail is needed today).

## 2. Confirmed decisions

| Decision | Choice |
|---|---|
| Operator model | Single shared credential — no user table, no registration flow. |
| Auth mechanism | Login page + signed session cookie (not HTTP Basic Auth) — real logout, styled to match the dashboard, a "remember me" option. |
| Credential shape | Username + password (`DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD`), not a single passphrase. |
| Session token | A signed JWT (`pyjwt`, already a dependency — no new library), signed with its own independent secret (`DASHBOARD_SESSION_SECRET`), not derived from the password. Rotating this secret invalidates every active session at once — the deliberate "revoke everything" lever if a session is ever suspected compromised. |
| Session duration | `exp` 12h out by default; 30d out if "remember me" was checked at login. |
| Cookie flags | `HttpOnly`, `Secure`, `SameSite=Strict`. `SameSite=Strict` is the CSRF mitigation for this and future write endpoints — no separate CSRF-token scheme (YAGNI: there is no legitimate cross-site link into this dashboard). |
| Brute-force guard | Fixed ~1s delay on every failed login, one generic `invalid_credentials` reason regardless of which field was wrong. No IP-based lockout/counter — no new state, no way to lock the operator out of their own IP. |
| Where the gate lives | A `require_session` FastAPI dependency, attached once in `app/main.py` at `app.include_router(dashboard_router, dependencies=[Depends(require_session)])` — not a global middleware. Every current and future route on `dashboard_router` (including the Environment feature's routes, once that resumes) is covered automatically with no per-route boilerplate. `/healthz` and `/webhook` are on separate routers and are untouched. |
| Unauthenticated response shape | Path-prefix branch, not header-sniffing: anything under `/api/` gets `401 {"valid": false, "reason": "unauthenticated"}` JSON; anything else (`GET /`) gets a `303` redirect to `/login`. Mirrors the `/api/`-prefix convention already used throughout this codebase (onboarding router, dashboard). |
| Startup safety | `app/main.py`'s `lifespan()` refuses to boot if any of the three new vars is empty — same shape as the existing `GITHUB_WEBHOOK_SECRET` check ("an empty secret would accept any webhook signature"). An empty `DASHBOARD_PASSWORD` would make `hmac.compare_digest("", "")` return `True`, i.e. accept any login; this closes that off at boot rather than relying on the operator to notice. |

## 3. Architecture

```
app/auth.py                 NEW — credential check, JWT issue/verify,
                              require_session dependency, SessionRequired
                              exception, cookie helpers (set/clear)
app/main.py                  MODIFIED — SessionRequired exception handler
                              (path-prefix branch: 401 JSON vs 303 redirect);
                              auth_router included (unprotected); dashboard_router
                              included with dependencies=[Depends(require_session)];
                              lifespan() gains the three-var empty-value guard
app/static/login.html        NEW — same theming/toggle pattern as dashboard.html;
                              username/password/remember-me form; posts to
                              /api/login via fetch, same idiom dashboard.html
                              already uses against /api/dashboard
app/config.py                MODIFIED — dashboard_username, dashboard_password,
                              dashboard_session_secret fields (plain str,
                              default "", NOT added to OPERATIONAL_KEYS)
scripts/deploy.py             MODIFIED — _wanted_env() gains the three keys
.env.example                  MODIFIED — three new blank/commented keys
tests/test_auth.py             NEW — see section 6
tests/test_dashboard*.py       MODIFIED — shared login fixture (section 6)
tests/test_deploy_script.py    MODIFIED — _wanted_env() expectations
docs/reference/config.md       regenerated (scripts/gen_docs.py), not hand-edited
```

`app/auth.py` follows this codebase's existing module-boundary convention
(`app/dashboard.py`'s own docstring: "Knows nothing about LLM providers or
GitHub") — it knows nothing about tickets, reviews, or providers either; its
only dependency is `app.config.settings` for the three credential fields.

## 4. Data flow

**Login:**
1. Browser: `GET /login` → `login.html` (unauthenticated, always reachable).
2. Form submit → `fetch POST /api/login` with `{username, password, remember}`.
3. `app/auth.py` compares both fields via `hmac.compare_digest` against
   `settings.dashboard_username` / `settings.dashboard_password`. Any
   mismatch: sleep ~1s, return `{"valid": false, "reason": "invalid_credentials"}`
   (200, not 401 — this is a normal form-validation response, not an
   auth-gate rejection).
4. Match: issue a JWT with `exp` = now + 12h (default) or now + 30d
   (`remember=true`), `Set-Cookie` with `HttpOnly; Secure; SameSite=Strict`,
   return `{"valid": true}`. Client JS redirects to `/`.

**Protected request (page or API):**
1. `require_session` reads the cookie, verifies signature + `exp` via `pyjwt`.
2. Valid → request proceeds untouched.
3. Missing/invalid/expired → raises `SessionRequired`. The app-level handler
   checks `request.url.path.startswith("/api/")`: JSON 401 if so, else a
   `303` redirect to `/login`.

**Logout:** `POST /api/logout` clears the cookie (`Set-Cookie` with an
already-expired `Max-Age=0`); client JS redirects to `/login`. No
server-side token invalidation — the JWT itself remains cryptographically
valid until its `exp`, but the browser no longer holds it. This is an
accepted limitation given the stateless design (see section 7).

## 5. API contract

| Route | Auth | Request | Response |
|---|---|---|---|
| `GET /login` | none | — | `login.html` |
| `POST /api/login` | none | `{"username": str, "password": str, "remember": bool}` | `{"valid": true}` or `{"valid": false, "reason": "invalid_credentials"}` |
| `POST /api/logout` | none (clearing an absent/invalid cookie is a harmless no-op) | — | `{"valid": true}` |
| `GET /` | session | — | `dashboard.html` (unchanged), or `303 → /login` |
| `GET /api/dashboard` | session | — | unchanged JSON, or `401 {"valid": false, "reason": "unauthenticated"}` |

## 6. Testing strategy

- **`tests/test_auth.py`** (new): correct credentials issue a cookie with
  the right `exp` delta for both `remember` values; wrong username, wrong
  password, and both wrong all return the identical generic reason; a
  tampered/expired/missing cookie is rejected by `require_session`;
  `/api/logout` clears the cookie; `/healthz` and `/webhook` remain
  reachable with no cookie at all. The fixed delay is monkeypatched
  (`time.sleep`) so the suite doesn't pay it for real — same pattern
  `tests/test_onboarding_github_client.py`'s `_no_pygithub_rate_limit_sleep`
  fixture already uses for an analogous reason.
- **`tests/test_dashboard.py` / `test_dashboard_api.py` / `test_dashboard_page.py`**
  (modified): gain a shared fixture that mints a valid session JWT (via
  `app.auth`'s own signing function, not by re-deriving the logic) and
  attaches it to the test client's cookies, so these tests keep verifying
  dashboard *content* rather than re-testing the auth gate on every case.
- **`tests/test_deploy_script.py`** (modified): `_wanted_env()` includes the
  three new keys; the existing empty-value-refusal test extends to cover
  them.
- **`tests/test_config.py`**: no change expected — the three new fields are
  simply absent from `OPERATIONAL_KEYS`, which the existing exhaustive-list
  test already enforces in both directions.

## 7. Out of scope

- **The Environment feature itself** (fetching/modifying Render env vars and
  the `runtime_config` table) — this spec only unblocks it; its own design
  resumes separately per the parked `ISSUES.md` entry.
- **Multi-operator support** (named accounts, per-operator revocation/audit)
  — explicitly deferred; would need a real operator table and is not
  justified by a single-operator project today.
- **IP-based lockout/rate limiting** — the fixed-delay + generic-error guard
  was chosen specifically to avoid this; revisit only if brute-forcing is
  ever observed in practice.
- **Server-side session revocation short of rotating `DASHBOARD_SESSION_SECRET`**
  — a stateless JWT can't be individually invalidated before its `exp`
  without a blocklist (new state this design deliberately avoids). Logout
  removes the browser's copy but not the token's validity.
- **A separate CSRF-token mechanism** — `SameSite=Strict` is judged
  sufficient for this dashboard's actual threat model (no legitimate
  cross-site entry point exists); revisit only if that assumption changes.
