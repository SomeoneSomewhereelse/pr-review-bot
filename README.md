# Autonomous Code Review Engine (Project ד)

A GitHub PR webhook triggers an **Orchestrator** that fetches the diff and
fans out to three parallel LLM specialists — **Security**, **Performance**,
**Code Quality** — each backed by a structured-output LLM call. Findings are
merged into a single Markdown PR comment, edited in place on later pushes.

Full design: [`SPEC.md`](SPEC.md). Stack/conventions: [`CLAUDE.md`](CLAUDE.md).
Cost model: [`cost.md`](cost.md). Guided setup + environment deviations:
[`SETUP.md`](SETUP.md) — **read that before running anything real**; this
project's actual live configuration differs from `SPEC.md`'s defaults in a
few documented ways (see "Known limitations" below).

## Architecture

```
GitHub PR (opened / reopened / synchronize)
  └─▶ POST /webhook
        (1) read RAW body → verify HMAC-SHA256 (constant-time) → 401 if bad
        (2) dedup on X-GitHub-Delivery → 200 no-op if already seen
        (3) enqueue/update a durable SQLite ticket for this PR → 202
              immediately (no LLM work in the request path)

A single background dispatcher (started in the app lifespan) is the only
caller of the review pipeline, and drains the queue serially:
        repeat: claim the next due ticket (FIFO, honors a deferred
                ticket's not_before)
              ├─ GitHub App auth → installation token → fetch PR diff
              ├─ annotate diff with file:line, cap to a token budget
              ├─ asyncio.gather(security, performance, quality,
              │                 return_exceptions=True)
              ├─ rate-limited (429)? → post/keep a placeholder comment,
              │   defer the ticket — retried automatically once the
              │   provider allows it, durable across a restart
              └─ otherwise: merge results (successes AND failures) +
                 timing + cost → find-or-edit the bot's marked PR
                 comment (replacing any placeholder) → mark ticket done
```

This durable review queue absorbs the live providers' real per-minute and
daily rate limits — see [`SPEC.md` §12](SPEC.md#12-review-queue-rpm--daily-quota-handling)
for the full design (ticket store, the in-memory `blocked_until` gate,
restart recovery).

## Tech stack

FastAPI (async) · `uv` · PyGitHub (GitHub App auth) · `google-genai` +
`groq` behind a swappable `LLMProvider` seam · Pydantic v2 validate-repair ·
`pytest`/`pytest-asyncio`/`respx` · Docker · Render + Supabase Postgres.

## Running locally

```bash
uv sync
cp .env.example .env   # fill in real values — see SETUP.md
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- `GET /healthz` → `200`
- `POST /webhook` → `401` (bad/missing signature), `200` (replayed delivery),
  or `202` (accepted, review runs in the background)
- `GET /` → the live ops/demo dashboard — light/dark/system theme,
  English/Hebrew with RTL, auto-refreshing review history and queue stats
- `GET /api/dashboard` → JSON backing endpoint for the dashboard above

### Docker

```bash
docker build -t pr-review-engine .
docker run -p 8000:8000 --env-file .env pr-review-engine
```

### Deploying to production (Render + Supabase)

The bot runs as a Docker container on Render's free tier with its durable queue
in Supabase Postgres, kept awake by a free UptimeRobot monitor. `scripts/deploy.py`
is the tool for both verifying and performing a deploy; it is a plain CLI and
needs no editor, assistant, or Claude Code.

#### One-time setup

These four steps need a browser and cannot be automated — the first is
*structurally* impossible, since GitHub does not permit an App to install itself.

1. **Install the GitHub App on the target repo** — repo Settings → GitHub Apps.
   A repo admin authorizes it once.
2. **Create the Supabase project**, wait until it reports ready, and copy the
   **Session-mode pooler** connection string (port 5432, not 6543) as
   `DATABASE_URL`.
3. **Create the Render service** from `render.yaml` (New + → Blueprint).
4. **Create an UptimeRobot monitor** on `https://<your-service>.onrender.com/healthz`
   with a **5-minute interval**. The URL must match exactly — a stray trailing
   character 404s on every check while looking perfectly healthy in the dashboard.

Full click-by-click detail for each: [`SETUP.md`](SETUP.md) §3.

#### Verifying a deployment

```bash
PUBLIC_BASE_URL=https://<your-service>.onrender.com uv run python -m scripts.deploy
```

PowerShell:

```powershell
$env:PUBLIC_BASE_URL = "https://<your-service>.onrender.com"
uv run python -m scripts.deploy
```

Run it from your own machine, not inside the Render container — `scripts/` is not
copied into the Docker image, and `RENDER_EXTERNAL_URL` only exists inside
Render's own container, which is why `PUBLIC_BASE_URL` is passed explicitly here.

It prints one line per check and always runs all nine, so a single run
surfaces every problem rather than only the first:

| Check | Verifies | Required? |
|---|---|---|
| `config` | Every setting the service needs is resolvable locally | yes |
| `github-app` | The App is installed, and its webhook points here (set only if wrong) | yes |
| `health` | `/healthz` answers **both** `GET` and `HEAD` — UptimeRobot's free tier sends `HEAD`, so a `GET`-only endpoint lets the instance sleep | yes |
| `database` | Postgres is reachable **and** the app has provisioned its `tickets` table there | optional |
| `provider` | The provider that will actually run — `LLM_PROVIDER`, or an active **DB override** — has its credential set | optional |
| `provider-live` | The actively-resolved provider's credential (env or DB override) is present on the deployed Render service — not just locally | optional |
| `api-key-live` | The actively-resolved provider's actively-resolved key slot (base env var, or an `_N` slot set by a DB override) is present on the deployed Render service | optional |
| `render-service` | The latest Render deploy is `live`, and (when a commit is comparable) matches local `HEAD` | optional |
| `uptime-pinger` | A monitor targets `/healthz` exactly, is active, and polls at most every 10 minutes | optional |

A **DB override** is a provider swap written straight into the
`runtime_config` table by `scripts/set_provider.py` (see "Switching providers
without a redeploy" below) — it wins over `LLM_PROVIDER` at runtime with no
restart and no redeploy, so `provider` resolves and checks whichever one is
actually active, not just the env var.

| Exit | Meaning |
| --- | --- |
| 0 | every check passed (skipped checks do not fail the run) |
| 1 | at least one check failed |
| 2 | the run could not proceed: `GITHUB_TARGET_REPO` or a public base URL is unset; `--sync-env` without `RENDER_API_KEY`; or a sync refused before any request (empty values, an unsupported `LLM_PROVIDER`, or an active DB override that would mask the push) |

In short: exit 0 means trust the table as-is, exit 1 means read the table for
what to fix, exit 2 means the run never really started.

Six checks are skipped with a hint unless you set the matching
operator-local key. None of these keys is ever set on the Render service
itself:

- `RENDER_API_KEY` (Render → Account Settings → API Keys) enables
  `render-service`, `provider-live`, `api-key-live`, and `--sync-env`.
- `UPTIMEROBOT_API_KEY` (a read-only key) enables `uptime-pinger`.
- `DATABASE_URL` enables both `database` and `provider` (the override lives
  in the same database). It is normally a Render dashboard secret; export it
  locally, temporarily, to check either.

#### Deploying

With `RENDER_API_KEY` set, this is a complete, repeatable deploy:

```bash
PUBLIC_BASE_URL=https://<your-service>.onrender.com uv run python -m scripts.deploy --sync-env
```

PowerShell:

```powershell
$env:PUBLIC_BASE_URL = "https://<your-service>.onrender.com"
uv run python -m scripts.deploy --sync-env
```

The push set is **provider-derived**, not a fixed list: it always pushes
`DATABASE_URL`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_B64`,
`GITHUB_TARGET_REPO`, `GITHUB_WEBHOOK_SECRET`, and `LLM_PROVIDER`, plus the
**selected provider's** credential and model var — e.g. `LLM_PROVIDER=groq`
pushes `GROQ_API_KEY` and `GROQ_MODEL`, not `GEMINI_API_KEY`/`LLM_MODEL` or
`GITHUB_MODELS_TOKEN`/`GITHUB_MODELS_MODEL`. Any *other* provider's
credential is pushed too, but only if you happen to have it set locally —
an unselected provider's key is never demanded. It refuses to start (exit 2)
if any wanted value is empty locally, so a blank `.env` entry can never
overwrite a working secret on the service; only changed variables are
pushed, and if nothing differs no deploy is triggered.

If a **DB override** (see below) is active and disagrees with the
`LLM_PROVIDER` being pushed, `--sync-env` refuses (exit 2) rather than push a
value the override would silently ignore at runtime — clear it first with
`uv run python -m scripts.set_provider --clear`.

Before triggering anything, it waits for any deploy already in progress to
settle (it never stacks a second deploy on top of one still building) —
worst case that's up to 900s waiting for the in-flight one, plus up to 900s
for the one it triggers itself, so **budget up to ~30 minutes** in the rare
worst case; a warm redeploy with nothing already in flight has taken well
under a minute in practice.

Claude Code users can run `/deploy` instead, which wraps the same CLI.

#### Switching providers without a redeploy

> Superseded by `scripts/set_override.py` below, which can do everything this script does
> plus set a key-index override in the same write. Kept working and documented here until a
> follow-up cleanup removes it.

```bash
uv run python -m scripts.set_provider groq       # set the override
uv run python -m scripts.set_provider --clear     # remove it
```

This writes a provider override to the `runtime_config` table and takes
effect on the **next ticket the dispatcher claims** — no restart, no
redeploy. It writes to whatever `DATABASE_URL` currently resolves to, so
running it against a local `.env` sets a **local** override only; nothing
reaches production unless your local `DATABASE_URL` happens to be the
production one. `scripts/deploy.py`'s `provider` check resolves the override
the same way the dispatcher does and confirms the resulting provider's
credential is set — but only in **your local `.env`**, not on the deployed
service. `scripts/deploy.py`'s `provider-live` check is the read-only
counterpart to this guard: it verifies the resolved provider's credential
against the live Render service itself, not just your local `.env`.
`scripts/set_provider.py` itself now verifies the target provider's
credential against Render before writing the override (when `RENDER_API_KEY`
is set), and refuses by default if it's missing or differs from your local
`.env` — pass `--force` to write anyway. If your local `DATABASE_URL` isn't
the one Render's service actually reads (e.g. you're testing against a local
database), this verification is skipped automatically, since the write
cannot affect production either way.

#### Tuning the re-review cooldown without a redeploy

```bash
uv run python -m scripts.set_cooldown --base 30 --factor 1.5   # tune for a demo
uv run python -m scripts.set_cooldown --clear                  # remove the override
```

Same shape as `set_provider.py` above: it writes a base/cap/factor override
to the `runtime_config` table and takes effect on the **next ticket the
dispatcher claims** — no restart, no redeploy — so the escalating cooldown
can be sped up on stage instead of waiting out the 300s/3600s production
defaults. It writes to whatever `DATABASE_URL` currently resolves to, so a
local `.env` run sets a local override only. Unlike `set_provider.py` there's
no credential at stake, so a non-cleared write is never refused for a
Render-verification reason — only refused if the resulting base/cap/factor
would resolve to something invalid (`factor < 1.0`, `base > cap`, or a
non-positive base/cap), since that combination would write successfully but
be silently discarded on every read. **A DB override in force also masks env
var changes** — editing `DISPATCHER_REREVIEW_COOLDOWN_SECONDS`/`_MAX_SECONDS`/
`_FACTOR` in the Render dashboard will appear to do nothing until you run
`--clear`.

#### Swapping API keys without a redeploy

> Superseded by `scripts/set_override.py` below, which can do everything this script does
> plus activate a provider in the same write. Kept working and documented here until a
> follow-up cleanup removes it.

```bash
uv run python -m scripts.set_api_key groq 2       # activate GROQ_API_KEY_2
uv run python -m scripts.set_api_key groq --clear  # back to GROQ_API_KEY (index 0)
```

Each provider's credential env var can have numbered siblings —
`GROQ_API_KEY`, `GROQ_API_KEY_1`, `GROQ_API_KEY_2`, ... — provisioned ahead
of time exactly like any other env var (one redeploy, via `--sync-env` or
the Render dashboard, to add a new slot). This writes which slot is
**active** to the `runtime_config` table and takes effect on the **next
ticket the dispatcher claims** — no restart, no redeploy, and no secret
value is ever written to, read from, or logged by the database: only the
slot's integer index is. Each provider tracks its own index independently,
so switching providers never disturbs the slot chosen for the other two.
Same presence-only Render verification as `set_provider.py` — refuses by
default (pass `--force` to override) if the target slot's env var is
missing on the live service, but never compares against a local `.env`
value, since a numbered slot typically has no local counterpart at all.
`scripts/deploy.py`'s `api-key-live` check is the read-only counterpart: it
confirms the actively-resolved provider's actively-resolved slot is
genuinely present on Render, catching the exact gap a redeploy-free index
flip can introduce — the DB says index 2, but nobody ever pushed
`GROQ_API_KEY_2` to Render.

#### Setting the provider and key-index override together

```bash
uv run python -m scripts.set_override groq --index 1        # activate groq AND its index-1 slot, together
uv run python -m scripts.set_override groq --index 1 --no-activate   # index only, same as set_api_key.py above
uv run python -m scripts.set_override groq --clear-index --no-activate  # clear index only, same as set_api_key.py above
uv run python -m scripts.set_override groq                  # activate only, same as set_provider.py above
uv run python -m scripts.set_override --clear                # clear the provider override, same as set_provider.py above
uv run python -m scripts.set_override groq --index 1 --force  # write despite a failed live check
```

`scripts/set_override.py` is a full, standalone replacement for both `set_provider.py` and
`set_api_key.py` below — every operation either of them supports is reachable through this
one script (see `docs/superpowers/specs/2026-08-12-override-cli-unification-design.md`
section 5 for the complete mapping), plus the new capability of setting both overrides in one
write and one Render-verification pass instead of two round trips. It verifies against the
**effective** index — whichever index will actually be active for that provider after the
write, not always index 0 — except when clearing the index override with `--no-activate`,
which never verifies (matching the old `set_api_key.py --clear` behavior, since nothing is
being activated and the target is the documented default). Activating a provider that already
has a non-default key-index override verifies the correct slot, not the base credential.

`scripts/set_provider.py` and `scripts/set_api_key.py` (documented in the two sections above)
are **superseded by this script** and will be removed in a follow-up cleanup; nothing below
needs to change before then.

#### Deploying an image, when the Render service has no connected repo

Render **always builds on Render** — from a connected GitHub repo, or by
pulling a pre-built image from a container registry. It never uploads your
local working tree. For a service configured against a registry image rather
than a repo:

1. Build locally: `docker build -t ghcr.io/<you>/pr-review-engine:<tag> .`
2. Push it: `docker push ghcr.io/<you>/pr-review-engine:<tag>`
3. Point the Render service at that image and tag (dashboard → Settings).
4. Run `--sync-env` as above to push config and trigger a deploy against it.

`render-service` reports whichever artifact is actually live — a commit sha
for a repo-connected service, or the image ref for an image-backed one — and
only compares against local `HEAD` when a commit is present; an image-backed
deploy reports `PASS` with "no local comparison possible" rather than a
guessed-at mismatch.

## Testing

```bash
uv run ruff check .
uv run pytest -v
```

**Prerequisite:** the queue store runs on Postgres, so DB-touching tests need
either Docker installed locally (`tests/conftest.py`'s `db_url` fixture spins
up a throwaway Postgres 16 via `testcontainers` automatically) or a
`DATABASE_URL` env var pointing at a reachable local Postgres. Without either,
those tests fail with an opaque testcontainers error. CI provides this
automatically via a `services: postgres` container — no action needed there.

508 deterministic tests, no real network calls — mocks GitHub's REST API (at
the `requests` transport layer PyGithub uses), all LLM providers' SDK
clients, and the webhook HTTP layer. CI (`.github/workflows/project-d-ci.yml`
at the repo root, path-filtered to this directory) runs `ruff` + `pytest` on
every push/PR touching this project.

### Live verification scripts

These make real network calls against real accounts/services — not run by
CI. Each is self-contained and prints what it's proving:

| Script | Proves |
|---|---|
| `scripts/manual_verify_step3.py` | GitHub App auth, diff fetch, comment upsert (edit-in-place) against a real PR |
| `scripts/manual_verify_step4.py` | Gemini provider through the validate-repair layer |
| `scripts/manual_verify_groq.py` | Groq provider through the validate-repair layer |
| `scripts/manual_verify_github_models.py` | GitHub Models provider through the validate-repair layer |
| `scripts/seed_demo_pr.py` | Opens a real PR with planted issues (`fixtures/bad_code/`) on the test repo |
| `scripts/demo_provider_swap.py` | `LLM_PROVIDER` is a genuine runtime seam — see below for this script's current expected behavior |

### Live end-to-end rehearsal

1. Ensure the Render service is deployed and `/healthz` returns 200.
2. The GitHub App's webhook URL is already the stable Render URL — no per-run
   update needed (`uv run python -m scripts.deploy` sets it once).
3. `uv run python -m scripts.seed_demo_pr` — opens a real PR with a
   hardcoded credential, an N+1 query, and a magic number planted in
   `fixtures/bad_code/`.
4. The bot comment appears on the PR within the 15-second target, naming all
   three planted issues across the Security/Performance/Code Quality
   sections, with a footer showing runtime, token usage, and estimated cost.

This has been run for real multiple times during development (not just
described), through the actual GitHub webhook delivery path (not a direct
function call) — most recently: PR #3, comment appeared **8 seconds** after
PR creation, all three specialists found real issues. See `SETUP.md` for
the full history of runs and timings.

## Known limitations (deviations from `SPEC.md`, all deliberate)

- **Vertex AI**: evaluated and **removed**. It requires an attached payment
  card, which this project's no-card constraint rules out, so it was never
  live-runnable here. The adapter existed under mocked tests only and was
  deleted rather than carried as a fourth code path no test could exercise for
  real. `SPEC.md` still records it as the brief's default provider.
- **Gemini (AI-Studio)**: live and working (`LLM_PROVIDER=gemini`) — re-verified
  2026-08-10 via `scripts/manual_verify_step4.py` (real structured output,
  non-zero token usage). See `SETUP.md` for the account-access history this
  project worked through earlier. `scripts/demo_provider_swap.py`'s
  description of a graceful Gemini failure predates this and hasn't been
  re-run against the current key.
- **Groq is the primary live provider** (`LLM_PROVIDER=groq`,
  `llama-3.3-70b-versatile`) — pulled forward from a later build step
  specifically to have a working live path.
- **GitHub Models is a second, genuinely live cross-vendor provider**
  (`LLM_PROVIDER=github_models`, `openai/gpt-4o-mini`) — rides the user's
  existing GitHub account (a fine-grained PAT with the "Models" permission),
  no separate signup/account-flagging risk. Real OpenAI models, a different
  vendor and model family from both Gemini (Google) and Groq (Llama) —
  live-verified end-to-end (all three specialists, real PR comment, 7.5s).
  Free tier caps are modest (single-digit RPM, ~150 requests/day on the
  low-access-tier models) — fine for demonstration, a real constraint at any
  meaningful sustained volume.
- **Docker**: fully verified (`docker build` + container boot + endpoint
  checks) — installed partway through development, not from the start.
- **Durable review queue is single-process** (see `SPEC.md` §12) — one
  dispatcher, no horizontal scaling. The atomic ticket claim would make a
  multi-instance deployment possible later, but it is neither built for nor
  tested.
- **`blocked_until` is in-memory, not durable.** It's re-learned from the
  first honest `429` after a restart; only a deferred ticket's own persisted
  `not_before` is what actually prevents an early run.
- **"Never partial" wastes a little quota at the daily boundary.** If the
  real remaining daily budget is 1-2 calls, the atomic review still fires 3;
  the 1-2 that succeed are discarded and the whole review defers to reset.
  Accepted cost of a simple, atomic review pipeline.
- **`DEFAULT_RETRY_AFTER_SECONDS` (default 60) is a backoff fallback**, used
  only when a `429` omits a usable `Retry-After` header — not a per-provider
  cap. Groq is documented to send `retry-after`; **GitHub Models sending a
  usable `Retry-After` is still an open assumption**, to be confirmed with a
  single live call per `CLAUDE.md`'s hygiene rules (not yet performed as of
  this writing) — until then, that provider's backoff runs on the fallback.

See `SETUP.md` for the full narrative of each deviation, including what was
tried and why.

## Cost

Demo runs at **$0** (Groq + Render + Supabase free tiers). Documented production
cost model (~$8-10/mo at brief scale) is in [`cost.md`](cost.md) — cost is
graded as that documented calculation, not actual spend.
