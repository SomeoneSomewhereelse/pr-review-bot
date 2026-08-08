# Deploy verification CLI — completion record

**Date:** 2026-08-07
**Status:** **Complete and merged to `master`** (fast-forward, `5addf5d..2786f49`).
This file began as a mid-work checkpoint; it is now the record of what shipped
and what deliberately did not.
**Relates to:** `docs/superpowers/specs/2026-08-05-deploy-command-design.md`
(spec), `docs/superpowers/plans/2026-08-07-deploy-verification-cli.md` (plan),
`docs/2026-08-05-first-hosted-run-findings.md` (the hosted run that shaped
three of the six checks).

## What shipped

`scripts/deploy.py` is now a standalone deploy-verification CLI: six checks
(config, GitHub App install + webhook, `/healthz` via **both** `GET` and `HEAD`,
Postgres reachability **and** schema provisioning, Render deploy status,
UptimeRobot keep-warm), one aligned table, exit codes 0/1/2. It also has an
opt-in `--sync-env` that pushes config to Render, triggers a deploy, and polls
until live. `.claude/commands/deploy.md` wraps it for Claude Code users and
holds no logic — the CLI works identically without it.

**Verified on the merged result:** 259 tests pass
(`TESTCONTAINERS_RYUK_DISABLED=1 uv run pytest -q`), `ruff check .` clean, and
`python -m scripts.deploy` with no config exits 2 with a clean message and no
traceback.

Three checks encode real failures from the first hosted run rather than
hypotheticals: the `HEAD` verb (a `GET`-only `/healthz` returned 405 to the
pinger and let the instance sleep for 71 minutes), the exact-equality monitor
URL match (the outage was a trailing comma that fired on schedule and 404'd
every time), and `to_regclass('public.tickets')` (a bare `SELECT 1` reports a
wrong-project `DATABASE_URL` as success).

## Process notes worth keeping

- **A plan defect surfaced during Task 3 and was resolved rather than forced.**
  PyGithub 2.9.1's `Requester.__postProcess` injects a synthetic `url` (the
  request path) into any GET dict response lacking one, so the planned
  `get_webhook_url()` could never observe an unconfigured webhook as `""`. The
  fix requires an absolute `http(s)` scheme — no PyGithub internals, no
  hard-coded path, and it still behaves correctly if a future PyGithub stops
  injecting, or if GitHub returns `{"url": null}` / `{"url": ""}`.
- **The final whole-branch review caught a Critical that no task-scoped review
  could see** — see the open spec decision below.

## Still open

### 1. A spec contradiction (needs a decision, not a fix)

- §6.1 makes the required provider key a *function* of `LLM_PROVIDER`,
  including `GEMINI_API_KEY`.
- §8 hardcodes an eight-variable push list that **includes** `LLM_PROVIDER` but
  **excludes** `GEMINI_API_KEY`.

Combined, `--sync-env` could overwrite a live `LLM_PROVIDER=groq` with `gemini`,
never push that provider's key, and produce a service that boots, answers
`/healthz`, and fails every review — with all six checks reporting green. The
implementation followed both spec sections faithfully; the contradiction is the
defect.

**Shipped mitigation:** a guard in `sync_env()` that refuses to sync when the
selected provider's key is not in the synced set (exit 2, before any HTTP). That
is a correct stopgap, not a resolution. The real choice is still open:

- **(a)** drop `LLM_PROVIDER` from the synced set, leaving it to `render.yaml`; or
- **(b)** add the selected provider's key to the synced set.

### 2. Parked residual — `_wanted_env()`'s `OSError` is outside `sync_env`'s `try`

An existing-but-unreadable PEM (mode 000, root-owned, bad mount, I/O error)
gives a raw traceback and **exit 1**, which the CLI's own contract defines as "a
check failed" — narrowly re-opening the exit-code-contract finding under a
different input. `check_config`'s `_private_key_available()` only calls
`is_file()`, so the `config` check reports PASS and gives no forewarning. No
secret leaks (the path is printed, not the key). Low frequency; deferred
deliberately.

Note for whoever picks it up: the original reason given for leaving it — that
moving the call inside the `try` would reorder guard evaluation — is **wrong**.
A `return` from inside a `try` is not intercepted by that block's own `except`,
so the guards would still run in the same order.

### 3. Smaller items, recorded and triaged as non-blocking

Six over-length test lines (102–108 chars; ruff's `E501` is unselected, so
nothing catches them), duplicated PEM-path resolution across two helpers, the
health URL built independently in two checks, `test_env_var_names_match_the_docs`
reading CWD-relative paths, unbounded monitor-URL enumeration in
`check_uptime_pinger`, a stale `discover_installation_id` docstring, no
`--help`/unknown-flag handling, and no pagination on the Render list endpoints
(fail-safe for env vars; a `>20`-service account would break
`_find_render_service_id`, but it fails loudly).

Two further spec gaps: §7.2 lists three causes for exit 2 but §10 never required
the docs to carry them (the docs-parity test would be the natural place to pin
that), and §11's detail-length budget is unimplementable as literally written,
since `check_config`'s missing-key enumeration legitimately exceeds it.

## Deploying this

`origin/main` is the Render-connected remote — **pushing to it auto-deploys the
live service.** Local `master` is ahead of `origin/main` by this work plus the
spec/plan docs, so a `git push` is what actually ships it. Nothing has been
pushed.
