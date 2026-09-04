# Design — Supabase OAuth app abuse mitigation

**Date:** 2026-09-03
**Status:** Superseded — see `2026-09-04-supabase-pat-frame-design.md`. A
follow-up session re-examined the OAuth-app auth mechanism this design was
built to mitigate the risk of, and replaced it with a visitor-pasted
Personal Access Token instead — which removes the shared-resource risk
outright rather than mitigating it. Left below unchanged as a record of
the mitigation design that turned out to be unnecessary.
**Relates to:** `onboarding/supabase_client.py`, `onboarding/router.py`,
`onboarding/session_store.py`, `onboarding/config.py`,
`onboarding/static/index.html`, `onboarding/CLAUDE.md`'s sub-project 3
section, the "Supabase OAuth app is a shared resource across all visitors"
entry in `ISSUES.md`'s Design Gaps section — this spec closes that entry.

## 1. Problem and context

Every visitor's Supabase frame (`onboarding/CLAUDE.md` sub-project 3)
authorizes through one operator-registered OAuth app
(`SUPABASE_OAUTH_CLIENT_ID`/`SUPABASE_OAUTH_CLIENT_SECRET`). Unlike the
Render frame (each visitor's own key) or the GitHub frame (each visitor
mints their own App), this credential is a single shared identity across
the whole visitor population. A burst of activity from one visitor, or
Supabase's own abuse heuristics, could throttle or revoke the shared app's
token-exchange/Management API access — breaking the Supabase frame for
every future visitor, not just the one who triggered it. This is the same
risk category as the documented Gemini AI-Studio Trust & Safety block (root
`CLAUDE.md`'s "LLM API testing hygiene" section).

This design closes the gap in three parts: reduce the odds it happens
(rate limiting), notice promptly if it happens anyway (detection), and
fail cleanly for visitors while it's happening (graceful degradation).

## 2. Confirmed decisions

| Decision | Choice |
|---|---|
| Rate-limit key | Per-IP cap **and** a global ceiling, both checked. Per-IP stops one visitor from single-handedly exhausting the budget; the global ceiling is the actual protection for the shared app regardless of how many distinct IPs are involved. |
| Rate-limit storage | New table in onboarding's existing session-store Postgres (`onboarding/session_store.py`'s database) — not in-memory, since a per-process counter would reset on every deploy/restart and not reason about distributed instances. |
| Rate-limited endpoints | Both `POST /api/supabase/connect` and `GET /oauth/supabase/callback` — the callback is reachable directly with a forged/garbage `code` without ever calling `connect`, so limiting only `connect` would leave a bypass. |
| Detection — passive | Classify token-exchange failures by whether Supabase's error body says the app itself is rejected (`invalid_client`/`unauthorized_client`) vs. an individual visitor's code being bad (`invalid_grant` etc.) — log the former under a distinct, greppable tag. |
| Detection — active | A new `GET /api/supabase/oauth-health` endpoint performs a real canary token refresh against Supabase's own token endpoint on a **cooldown** (default 6h) — not on every poll — and returns 200/503 from a cached verdict in between. A second UptimeRobot monitor (reusing the existing free integration) polls it, so the operator gets an email alert without checking the wizard. |
| Canary credential storage | The canary's `refresh_token` is stored Fernet-encrypted using `session_store.py`'s existing encryption key, in a dedicated single-row table separate from `wizard_sessions` (it is an operator-level credential, not visitor session data). Updated in place each time the endpoint performs a real check, since Supabase is expected to rotate the refresh token on use. |
| Graceful degradation | Both the rate limiter and an app-level auth failure return a distinct `reason` (`rate_limited` / `oauth_app_unavailable`) instead of falling into the generic failure path, so the frontend shows "temporarily unavailable, try again later" instead of a confusing generic connection error. |

## 3. Rate limiting

### 3.1 Storage

```sql
CREATE TABLE IF NOT EXISTS oauth_connect_attempts (
    id BIGSERIAL PRIMARY KEY,
    ip TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS oauth_connect_attempts_ts_idx ON oauth_connect_attempts (ts);
```

Created by the same `init_pool()`-time idempotent `CREATE TABLE IF NOT
EXISTS` pattern `session_store.py` already uses for `wizard_sessions` — one
more statement in the same module, not a new migration mechanism.

### 3.2 Check-and-record

A new `onboarding/session_store.py` function,
`check_and_record_oauth_attempt(ip: str) -> RateLimitResult`, called via
the same `_get_session`-style `asyncio.to_thread` wrapper convention
`router.py` already uses for every other session-store call (never called
directly from an `async def` endpoint):

1. Delete rows older than the window (e.g. 15 minutes) — self-pruning, no
   separate cleanup job.
2. Count remaining rows where `ip = <caller ip>`. If `>= PER_IP_LIMIT`
   (default 5), return `rate_limited` without inserting.
3. Count remaining rows in total. If `>= GLOBAL_LIMIT` (default 50), return
   `rate_limited` without inserting.
4. Otherwise insert `(ip, now())` and return `ok`.

Both limits and the window are module-level constants in
`session_store.py`, next to `SESSION_TTL`, not settings — no operator
configuration surface needed for a first pass; revisit only if the
defaults prove wrong in practice.

`ip` is `request.client.host` — this project's existing deploy has no
already-terminated-proxy header convention to trust, so no `X-Forwarded-
For` handling is added here; a future proxy layer that needs it gets its
own change.

### 3.3 Call sites

`router.py`'s `connect_supabase` and `supabase_oauth_callback` both call
`check_and_record_oauth_attempt` first, before touching
`supabase_client.py` at all. On `rate_limited`:

- `connect_supabase` returns `{"valid": false, "reason": "rate_limited"}`
  (same response shape its other failure branches already use).
- `supabase_oauth_callback` redirects to `/` with a `?supabase_error=
  rate_limited` query param, mirroring how it already reports other
  callback-time failures back to the page (`index.html` reads this on
  load — see section 5).

## 4. Detection

### 4.1 Passive: app-level vs visitor-level failure classification

`supabase_client.py`'s `_parse_token_response` currently collapses every
4xx into one `invalid_reason` string (`"invalid_code"` or `"unauthorized"`
depending on caller). It gains one more check before that fallback: if the
response body's `error` field is `"invalid_client"` or
`"unauthorized_client"` — Supabase's own signal that the *app itself* was
rejected, not the visitor's code/token — return
`SupabaseOAuthFailed(reason="oauth_app_unavailable")` instead. Every
caller (`exchange_oauth_code`, `refresh_access_token`) already returns
`SupabaseTokenResult`, so this is a new enum-like value on an existing
type, not a new return shape.

`router.py`'s two callers log this specific reason at `ERROR` with a fixed
prefix (`"[SUPABASE_OAUTH_APP_DEGRADED]"`) so it's `grep`-able and stands
out from routine per-visitor `invalid_code` noise in the logs — this
project has no `logging.basicConfig` call yet (a separate, already-logged
gap — see `ISSUES.md`), so this uses the same `print(...)`-based
convention every other diagnostic line in this codebase currently uses,
not a new logging setup.

### 4.2 Active: canary health-check endpoint

**New table**, single row, holding the canary credential:

```sql
CREATE TABLE IF NOT EXISTS oauth_canary (
    id TEXT PRIMARY KEY DEFAULT 'supabase',
    refresh_token_encrypted BYTEA NOT NULL,
    last_checked_at TIMESTAMPTZ,
    last_status TEXT NOT NULL DEFAULT 'unknown'  -- 'healthy' | 'degraded' | 'unknown'
);
```

`refresh_token_encrypted` uses the same `Fernet` key `session_store.py`
already loads from settings — no new secret setting needed for the
encryption key itself, only for seeding the row's initial value.

**One-time operator setup:** the operator authorizes the OAuth app once,
manually, the same way a visitor would (this is the existing sub-project 3
flow — no new UI for it), and a small one-off CLI script (in the shape of
`onboarding/scripts/` alongside this project's existing operator-CLI
convention — root `CLAUDE.md`'s "reach for this project's CLI" rule)
encrypts the resulting refresh token and writes the single canary row.
This is a manual, deliberate action, not something the wizard or any
automation triggers — same shape as the existing "operator confirms this
directly" pattern for the Supabase OAuth App registration itself
(`ISSUES.md`'s "plan-tier availability" entry).

**`GET /api/supabase/oauth-health`** (new `router.py` endpoint, no auth
required — it reveals only a boolean-ish health status, never a
credential, matching this project's existing "verdict, not credential"
relay contract):

1. Read the canary row.
2. If `last_checked_at` is within the cooldown window (`OAUTH_HEALTH_
   CHECK_COOLDOWN`, default 6 hours), return the cached `last_status`
   immediately — no Supabase call.
3. Otherwise call `supabase_client.refresh_access_token(canary_refresh_
   token)`:
   - On success: encrypt and store the (possibly rotated) new refresh
     token, set `last_status = 'healthy'`, `last_checked_at = now()`,
     return 200.
   - On `SupabaseOAuthFailed(reason="oauth_app_unavailable")`: set
     `last_status = 'degraded'`, `last_checked_at = now()`, return 503.
   - On any other failure (e.g. `supabase_unreachable`, or the canary's
     own token having separately expired for an unrelated reason): return
     the cached `last_status` unchanged rather than flipping to
     `degraded` on a transient/ambiguous signal, but still update
     `last_checked_at` so the cooldown doesn't retry-storm a transient
     blip. Log this ambiguous case distinctly (not the same
     `[SUPABASE_OAUTH_APP_DEGRADED]` tag) since it doesn't confirm the
     app-level failure the tag means.

The 6-hour cooldown is deliberately much coarser than the existing
UptimeRobot keep-warm monitor's 5-minute interval — this endpoint is
built to tolerate being polled that often without ever calling Supabase's
token endpoint more than a few times a day, in the same spirit as root
`CLAUDE.md`'s LLM-provider testing-hygiene rule (avoid bursty/looping live
calls that themselves risk an abuse flag) even though that rule is written
for a different provider.

**Wiring the monitor:** documented as a manual, one-time operator step
(README/guide update) — register a second UptimeRobot monitor against
`<service_url>/api/supabase/oauth-health`, reusing the same account the
existing keep-warm monitor already uses. Not part of the onboarding
wizard's automated frame (`onboarding/uptimerobot_client.py`'s
dedupe-before-create logic is specific to the keep-warm monitor's fixed
shape) — out of scope to extend that automation for a second, differently-
purposed monitor.

## 5. Graceful degradation

`index.html`'s existing per-reason error-message pattern for the Supabase
frame (it already branches on reasons like `unauthorized`,
`supabase_unreachable`) gains two more:

- `rate_limited` → "Too many connection attempts right now — please wait a
  few minutes and try again."
- `oauth_app_unavailable` → "Supabase sign-in is temporarily unavailable.
  Please try again later." (This is also the message the callback-path
  `?supabase_error=` redirect renders on page load, same as its existing
  error-query handling.)

Both are visitor-facing "this is not your fault, retry later" copy, kept
deliberately distinct from validation-style errors (bad code, wrong org
permissions) that already tell the visitor to fix something on their end.

## 6. Testing

- `session_store.py`: unit tests for `check_and_record_oauth_attempt` —
  under both limits allows and records; per-IP limit blocks without
  touching the global count; global limit blocks a fresh IP too; pruning
  drops rows outside the window.
- `router.py`: `connect_supabase` and `supabase_oauth_callback` return/
  redirect the rate-limited response once the limit is hit, mocking the
  session-store call rather than actually inserting hundreds of rows.
- `supabase_client.py`: `_parse_token_response` returns
  `oauth_app_unavailable` for an `invalid_client`/`unauthorized_client`
  body, distinct from the existing `invalid_code`/`unauthorized` cases.
- `oauth-health` endpoint: cooldown-cache-hit path (no Supabase call
  mocked/asserted-not-called), real-check success path (updates row,
  returns 200), real-check degraded path (updates row, returns 503),
  ambiguous-failure path (cached status preserved, `last_checked_at`
  still bumped).
- No test exercises a real Supabase call — same `respx`-mocked convention
  every other `supabase_client.py` test already uses.

## 7. Out of scope

- CAPTCHA/friction step before the OAuth redirect — rate limiting is
  judged sufficient for a first pass; revisit if abuse is observed despite
  it.
- Per-IP/global limits are not visitor-configurable and have no admin UI —
  same "constants, not settings" reasoning as section 3.2.
- Automatically re-registering or rotating the OAuth app itself if
  revoked — that remains a manual operator action (Supabase has no
  self-registration API, per `ISSUES.md`'s existing note on this).
- Extending `onboarding/uptimerobot_client.py`'s automated monitor-creation
  to the health-check monitor — documented as a manual setup step instead
  (section 4.2).
