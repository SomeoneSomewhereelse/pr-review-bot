# dashboard/ — module boundaries and contracts

Loaded when working with files under `dashboard/`. Project-wide conventions
live in the root `CLAUDE.md`.

## What this package is

The ops/demo dashboard: `GET /` (static page) + `GET /api/dashboard` (JSON),
plus the session-cookie login flow that gates both. Deployed in the same
process as `bot/` (one Render service, one Dockerfile — see
`bot/Dockerfile`), not as its own service.

## Layering

- `dashboard/router.py` reads `bot.queue.store`, `bot.queue.dispatcher`, and
  `bot.providers.base.KNOWN_PROVIDERS` directly — this is the one place
  `dashboard` depends on `bot`'s internals, and it's read-only (never
  enqueues, never mutates provider state).
- `dashboard/auth.py` reads only `bot.config.settings` (the three
  `DASHBOARD_*` credential fields) — no queue/provider access.
- `bot/main.py` mounts `dashboard.router.router` and `dashboard.auth.router`
  — the one place `bot` depends on `dashboard`. Neither package declares
  the other in its own `pyproject.toml` `dependencies` (see the
  2026-08-29 project-restructure design spec, section A) — they coexist as
  workspace members sharing one venv.

## Contracts

- Every dashboard/auth route assumes it runs behind `bot/main.py`'s
  `SessionRequired` exception handler and `require_session` dependency —
  this package does not re-implement that wiring itself.
- `dashboard/static/dashboard.html` and `dashboard/static/login.html` are
  read once at import time (`_STATIC_DIR = Path(__file__).parent /
  "static"`) — moving either file requires keeping them siblings of
  `router.py`/`auth.py`.
