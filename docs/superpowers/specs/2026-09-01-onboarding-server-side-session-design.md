# Design — Onboarding wizard: server-side session persistence

**Date:** 2026-09-01
**Status:** Approved for planning
**Relates to:** `onboarding/CLAUDE.md` (the stateless-relay invariant this
supersedes), `docs/superpowers/specs/2026-08-26-onboarding-wizard-render-frame-design.md`
section 3 (the original decision this reverses), `ISSUES.md` (the mobile
`sessionStorage`-loss incidents that motivated this), root `CLAUDE.md`'s
secret-handling section (still applies in full to every credential this
design newly persists).

## 1. Problem and context

The onboarding wizard was built as a deliberately stateless relay: every
visitor credential lived only in the browser's `sessionStorage`, and the
backend never held anything past the lifetime of a single request. That
choice was made specifically to avoid a server-side datastore — but it
assumed `sessionStorage` itself was durable enough to survive the wizard's
several full-page redirects to other domains (GitHub's manifest/install
flow, Supabase's OAuth authorize screen).

In practice it isn't. Mobile Firefox has been observed destroying the
tab's entire browsing context — and with it, all of `sessionStorage`, not
just whatever a given step was using — during these redirect round trips,
even without the user manually switching apps. A same-tab redirect resets
the whole wizard on return. A follow-up attempt to route the OAuth leg
through a popup (first via `window.opener`, then via `BroadcastChannel`)
avoided the redirect but didn't fix the underlying fragility: `window.close()`
is blocked by mobile Firefox once a popup has navigated cross-origin and
back, and the original tab can itself be discarded while backgrounded,
losing any live listener before a cross-tab message ever arrives. See
`ISSUES.md` for the three incidents this traces through.

The common thread: **anything that depends on a specific browsing context
staying alive for the duration of the flow is unsafe on mobile.** The only
things that reliably survive are the server and cookies (part of the
browser's cookie jar, not tied to a specific tab's JS heap). This design
moves the wizard's durable state to the server, behind a cookie-identified
session, and keeps `sessionStorage` only for non-durable UI state.

This is a deliberate reversal of `onboarding/CLAUDE.md`'s stated invariant
("this backend is a stateless relay... must never gain a database, a
session store, a cache"). That invariant was explored extensively and
found to not hold up against real mobile behavior; this design replaces it
rather than working around it.

## 2. Confirmed decisions

| Decision | Choice |
|---|---|
| Persistence scope | Full session state, server-side — every frame's validated data, not just the OAuth-crossing frames |
| Backend | A new, dedicated Supabase Postgres project for `onboarding/` (separate from `bot/`'s queue DB and from any visitor's own provisioned project — matches the existing "own threat model, don't share infra" rule) |
| Access pattern | `psycopg[binary]` + `psycopg_pool.ConnectionPool`, sync functions run via `asyncio.to_thread` — mirrors `bot/queue/store.py`'s existing pattern rather than introducing a new DB access style |
| Encryption | Application-level (Fernet) encryption of every credential value before it's written, decrypted only in-process when a server action needs the raw value |
| Session identity | A random id (`secrets.token_urlsafe(32)`) in an `HttpOnly`, `Secure`, `SameSite=Lax` cookie — the cookie is the only pointer to a session; never a body/query param |
| Session creation | Exactly one entry point: the frame-1 endpoint's first successful validation. No other code path may create a session (see section 5) |
| Credential re-use | Once submitted, a credential's raw value is read back from the server-side session when a later step needs it — the browser never re-sends it and the server never echoes it back |
| Restore-on-load | The browser calls `GET /api/session` on every load; the server returns which frames are complete plus non-secret display data only, never a raw credential |
| TTL | A few hours (see section 4), swept lazily — no cron/background job |
| Reset | One global "Start over" control in the page header, with a confirm dialog, calling `POST /api/session/reset` |

## 3. Architecture

```
browser                              onboarding/ backend                 Postgres
--------                             -------------------                 --------
GET /                    ─────────►  serves static page
GET /api/session         ─────────►  reads cookie, get_session()   ────► SELECT
                         ◄─────────  {frames: {...display-only...}}
POST /api/<frame>/validate ───────►  validates credential against
  (credential in body)               external API (unchanged relay
                                      shape) then, on success:
                                        - frame 1: create_session()  ───► INSERT
                                        - frame 2+: get_session()    ───► SELECT
                                          must already exist
                                        - update_frame(session, ...) ───► UPDATE
                         ◄─────────  verdict + Set-Cookie (frame 1 only)
...later step needing earlier
credentials (e.g. deploy)───────────► read_frame() for each          ──► SELECT
  (no credentials in body)            needed frame, decrypt, call
                                      external API
POST /api/session/reset ─────────►  delete_session()              ───► DELETE
                         ◄─────────  204, Set-Cookie clears
```

### 3.1 Data model

One table in the new dedicated Postgres project:

```sql
CREATE TABLE IF NOT EXISTS wizard_sessions (
    id          TEXT PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    frame_data  JSONB NOT NULL DEFAULT '{}'::jsonb
);
```

`frame_data` is one JSONB blob, one key per frame (`github_app`, `supabase`,
`render`, `llm_provider`, `uptimerobot`, ...) rather than one column per
frame — a new frame later adds a new key, not a migration. Every credential
value nested inside a frame's data is Fernet-encrypted (as an opaque base64
string) before the row is written; non-secret display fields (account
login, org name) are stored in plaintext since they're already permitted to
round-trip to the browser today.

Declared schema, `CREATE TABLE IF NOT EXISTS`, no migrations — same
convention `bot/queue/store.py` already uses and documents the reasoning
for.

### 3.2 `onboarding/session_store.py` (new module)

```python
def create_session() -> str:
    """Sweeps expired rows, then inserts a new row and returns its id.
    The ONLY function in this module allowed to mint a new session id."""

def get_session(session_id: str) -> SessionData | None:
    """None for a missing OR expired row (expired is deleted here, not
    just skipped) -- callers never distinguish the two cases."""

def update_frame(session_id: str, frame: str, data: dict) -> UpdateResult:
    """Requires an existing, non-expired session (calls get_session
    first). Returns SessionNotFound rather than upserting a session
    into existence if the id doesn't resolve to a real row. Encrypts
    every value in `data` before merging into `frame_data[frame]`."""

def read_frame(session_id: str, frame: str) -> dict | None:
    """Decrypts and returns one frame's stored data, or None if the
    session or that frame's data doesn't exist."""

def delete_session(session_id: str) -> None:
    """No-op if the id doesn't exist."""
```

`update_frame`'s existence check is the fix for the fork risk raised during
design review: if every endpoint could silently create-or-update, a
dropped cookie, a race between two near-simultaneous requests, or a stale
cookie pointing at an expired row would each silently fork off (or
resurrect) a session containing only one frame's data, while the browser
believes earlier frames are still done. Restricting creation to
`create_session()` — called only from frame 1's own endpoint — means there
is exactly one place a session id is ever minted; every other write path
fails closed with a structured "complete earlier steps first" result
instead of fabricating disconnected state.

### 3.3 Router changes

- **`GET /api/session`** — no cookie, or `get_session` returns `None` →
  `{"frames": {}}` (identical shape to a genuinely new visitor — expired
  and missing are indistinguishable on purpose, so there's no separate
  "your session expired" UI state to build). Otherwise, for each frame
  present in `frame_data`: `{"complete": true, "display": {...}}` using
  only the same non-secret display fields already allowed back today.
- **`POST /api/session/reset`** — `delete_session` if a cookie is present,
  clears the cookie via the response, `204`.
- **Existing per-frame endpoints** (`/api/render/validate-key`,
  `/api/github/validate-app`, etc.) — unchanged input shape (credential in
  the request body). On success: frame 1's endpoint calls `create_session()`
  if no cookie was sent, and sets the `Set-Cookie` header on its response.
  Every other frame's endpoint requires an existing session (via
  `update_frame`'s existence check) — if none exists, it returns the same
  "complete earlier steps first" structured error used today for corrupted
  client-side state, not a generic 500.
- **New final-step endpoint** (part of sub-project 6, not yet built) — the
  one genuinely new shape: takes no credentials in its body at all, reads
  the session cookie, and calls `read_frame` for `github_app`, `supabase`,
  and `render` before deploying.

### 3.4 Redirect-and-resume pattern (Supabase OAuth)

This is the concrete replacement for the popup + `BroadcastChannel`
machinery, and the reason this design exists in the first place. It works
because a cookie — unlike `sessionStorage` — is part of the browser's
persistent cookie jar, not tied to a specific browsing-context instance,
and because `SameSite=Lax` cookies are sent on exactly the request shape
this flow produces: a top-level GET navigation landing back on our origin
after a cross-site redirect.

1. Frame 5's connect endpoint generates the PKCE `state` and verifier,
   writes them into the session via `update_frame(session_id, "supabase",
   {"_pending_oauth": {"state": ..., "verifier": ...}})` (encrypted like any
   other stored value), and returns the authorize URL. The frontend does a
   plain `location.href = authorizeUrl` — same tab, no popup.
2. Supabase's authorize page redirects back to our own
   `/oauth/supabase/callback?code=...&state=...` via GET. The session
   cookie rides along automatically.
3. That route reads the cookie, calls `get_session`, reads
   `frame_data["supabase"]["_pending_oauth"]`, and rejects (falls back to
   the same "no session" shape) if it's missing or `state` doesn't match —
   this is the CSRF-binding property discussed earlier, now free: an
   attacker who merely captures the redirect URL doesn't have this
   browser's session cookie.
4. It exchanges `code` + the stored verifier with Supabase, then calls
   `update_frame` with the real result (the access/refresh tokens),
   clearing `_pending_oauth` in the same write.
5. It issues a server-side `302` to `/`. No client-side JS is involved in
   the callback at all — the page that loads calls `GET /api/session`
   exactly as it would on any other load, and sees `supabase` as complete.
   "Resuming" isn't a special case; it's just the normal restore path.
6. Every subsequent Supabase call that needs the access token
   (`/api/supabase/create-project`, `/api/supabase/project-status`,
   `/api/supabase/connection-info`) stops taking `access_token` in its
   request body and instead calls `read_frame(session_id, "supabase")` to
   get it server-side — consistent with the "credential re-use" decision
   in section 2.

**Carry forward the outstanding connection-info diagnostic.**
`supabase_client.py::get_connection_info`'s pooler-config lookup currently
has a still-unresolved `TEMPORARY diagnostic` (a `print()` logging the
`pool_mode`/`database_type` shapes seen when no session/PRIMARY entry
matches — see the function's docstring and `ISSUES.md`). This refactor
touches the endpoint that calls it (`/api/supabase/connection-info`), so
the diagnostic must be carried forward as-is, not dropped as a side effect
of moving `access_token` out of the request body — the mismatch it's
chasing is still open. While touching this code, tag the diagnostic's log
line with the session id (`session_id`, not any credential) so a future
occurrence can be correlated to a specific wizard run instead of being an
anonymous one-off in the logs. Actually root-causing the mismatch remains
out of scope for this design (see section 6).

### 3.5 Frontend changes

- `sessionStorage` stops holding credentials entirely. It's reduced to
  pure UI/display state (theme, language, which frame is expanded) — the
  same category of thing this page's theme/lang toggle already legitimately
  uses `localStorage` for elsewhere, just kept in `sessionStorage` since it
  still shouldn't outlive the tab.
- On page load, the wizard calls `GET /api/session` and uses the returned
  `frames` map to lock/unlock frames and populate badges — the restore
  path is now server-driven, not a `sessionStorage` read.
- A "Start over" control in the page header (visible regardless of active
  frame) opens a confirm dialog; on confirm, calls
  `POST /api/session/reset`, then reloads the page.
- The GitHub-App-manifest-era `STORAGE_KEYS` pattern per frame goes away
  for credential values; frames still track their own local form/validation
  state in memory the way they always have during a single page view, they
  just no longer persist a raw credential to browser storage at all.

### 3.6 Error handling

- Missing/unknown/expired cookie anywhere → always "no session," never a
  distinct error. `GET /api/session` returns the fresh-visitor shape; an
  action requiring a session that finds none returns the structured
  "complete step X first" shape.
- A DB write failing after a successful external validation → the frame
  reports failure to the visitor, even though the external check passed.
  Source of truth is the DB now, so an unpersisted "success" isn't real —
  and every external check here is cheap/idempotent to retry.
- Fernet decrypt failure (rotated key, corrupted ciphertext) → treated
  identically to session-not-found. Never surfaces a raw exception.
- `update_frame` against a session that doesn't exist (see 3.2) → the
  calling endpoint's structured "complete earlier steps first" response,
  never a silently-created orphan session.

### 3.7 Secrets

Two new operator secrets, documented in `onboarding/.env.example`
(already added):
- `DATABASE_URL` — the new dedicated Postgres project's connection string.
  **Must be the Session-mode pooler string** (`postgres.<ref>@aws-0-<region>.pooler.supabase.com:5432`), not the direct `db.<ref>.supabase.co:5432` host — Supabase's direct host is IPv6-only and Render's outbound networking is IPv4-only, so a direct-host `DATABASE_URL` fails startup with a `psycopg_pool` timeout. Same requirement `guide/setup/hosted/05-supabase.md` already documents for `bot`'s own `DATABASE_URL`; see `ISSUES.md`'s 2026-09-02 "Two real deploy blockers" entry for how this was found.
- `ONBOARDING_SESSION_ENCRYPTION_KEY` — a Fernet key used to encrypt every
  credential value before it's written, and decrypt it in-process when
  needed. Root `CLAUDE.md`'s secret-handling rules apply to both exactly as
  they do to any other operator secret in this project.

## 4. Session TTL

A session expires `created_at + TTL` where TTL is a few hours (an
operational constant in `session_store.py`, not an env var — no need to
make this configurable). Expiry is checked lazily: `get_session` treats a
past-`expires_at` row as not-found and deletes it on that read;
`create_session` additionally sweeps `DELETE FROM wizard_sessions WHERE
expires_at < now()` before inserting. No cron, no background task —
matches this project's existing preference against new background infra.

## 5. Testing strategy

- `session_store.py` unit tests mock at the `psycopg` connection boundary,
  following `bot/queue/store.py`'s existing test fixture shape (no real
  Postgres needed for the suite). Cover: `create_session` mints a fresh id
  each call; `get_session` returns `None` for both missing and expired
  rows and deletes the expired one; `update_frame` returns
  `SessionNotFound` (not an upsert) against a missing/expired id;
  `read_frame` round-trips an encrypted value correctly; `delete_session`
  is a no-op against a missing id.
- Router tests get a fake `session_store` module, same shape as the
  existing `render_client`/`github_client` fakes — no real DB in router
  tests either.
- A dedicated test exercises the fork-risk scenario directly: two
  successive `update_frame` calls against a session id that was deleted
  between them must not resurrect a session — the second call gets
  `SessionNotFound`.
- Encryption round-trip test: a value written via `update_frame` and read
  back via `read_frame` is byte-for-byte identical, and the raw row content
  read directly from the DB fixture never equals the plaintext (guards
  against `update_frame` accidentally skipping encryption for some field).
- `/oauth/supabase/callback` tests (fake `session_store` + fake
  `supabase_client`): a matching `state` completes the exchange and clears
  `_pending_oauth`; a mismatched or missing `state` falls back to the
  "no session" shape rather than completing the exchange; a valid
  completion redirects to `/` with no credential anywhere in the redirect
  response.

## 6. Out of scope

- The new final-step (sub-project 6) endpoint's own deploy logic — this
  design only specifies that it reads from the session store, not what it
  does with the data. That includes Postgres schema creation for the
  visitor's freshly-provisioned Supabase project: `bot/main.py`'s lifespan
  already calls `store.init_pool()`, which runs `bot/queue/store.py`'s
  `_SCHEMA` (`CREATE TABLE IF NOT EXISTS` for `tickets`, `runtime_config`,
  `reviews`) unconditionally against whatever `DATABASE_URL` it's given, on
  first boot. The wizard's job is only to wire the visitor's connection
  string into their Render service's `DATABASE_URL` — bot self-provisions
  its own schema the first time that instance starts. The wizard never
  creates tables itself.
- Any change to how the GitHub App validation checklist itself works —
  this design only changes where its *results* get persisted, not the flow
  that produces them (it already has no cross-domain redirect to worry
  about, per the earlier manual-validation redesign). The Supabase OAuth
  leg's redirect handling is addressed directly in section 3.4, not deferred.
- Root-causing the `pool_mode`/`database_type` mismatch behind
  `get_connection_info`'s outstanding diagnostic (section 3.4) — this
  design only ensures the diagnostic survives the refactor and gains a
  session id for correlation; actually finding and fixing the mismatch is
  separate, already-tracked work (`ISSUES.md`).
- Rotating `ONBOARDING_SESSION_ENCRYPTION_KEY` without invalidating every
  live session — out of scope; a rotation simply orphans in-flight
  sessions (acceptable given the short TTL), not a supported live-rotation
  path.
