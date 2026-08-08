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

Run it from your own machine, not inside the Render container — `scripts/` is not
copied into the Docker image, and `RENDER_EXTERNAL_URL` only exists inside
Render's own container, which is why `PUBLIC_BASE_URL` is passed explicitly here.

It prints one line per check and always runs all six, so a single run surfaces
every problem rather than only the first:

| Check | Verifies | Required? |
|---|---|---|
| `config` | Every setting the service needs is resolvable locally | yes |
| `github-app` | The App is installed, and its webhook points here (set only if wrong) | yes |
| `health` | `/healthz` answers **both** `GET` and `HEAD` — UptimeRobot's free tier sends `HEAD`, so a `GET`-only endpoint lets the instance sleep | yes |
| `database` | Postgres is reachable **and** the app has provisioned its `tickets` table there | optional |
| `render-service` | The latest Render deploy is `live` | optional |
| `uptime-pinger` | A monitor targets `/healthz` exactly, is active, and polls at most every 10 minutes | optional |

Exit codes: `0` everything passed or was skipped, `1` at least one check failed,
`2` the CLI could not run at all (no `GITHUB_TARGET_REPO` or public base URL).

The two optional checks are skipped with a hint unless you set the matching
operator-local key. Neither is ever set on the Render service:

- `RENDER_API_KEY` (Render → Account Settings → API Keys) enables
  `render-service` and `--sync-env`.
- `UPTIMEROBOT_API_KEY` (a read-only key) enables `uptime-pinger`.
- `DATABASE_URL` enables `database`. It is normally a Render dashboard secret;
  export it locally, temporarily, to check it.

#### Deploying

With `RENDER_API_KEY` set, this is a complete, repeatable deploy:

```bash
PUBLIC_BASE_URL=https://<your-service>.onrender.com uv run python -m scripts.deploy --sync-env
```

It pushes any of `DATABASE_URL`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_B64`,
`GITHUB_TARGET_REPO`, `GITHUB_WEBHOOK_SECRET`, `LLM_PROVIDER`, `GROQ_API_KEY`
and `GITHUB_MODELS_TOKEN` that differ from your local `.env`, triggers a deploy,
waits for it to go live (~60s), and then runs the checklist above.

It refuses to start if any of those values is empty locally, so a blank `.env`
entry can never overwrite a working secret on the service. Only changed
variables are pushed, and if nothing differs no deploy is triggered. It
currently expects the Groq/GitHub-Models pair to be set regardless of
`LLM_PROVIDER` — a Gemini-only setup will exit 2 here until **both**
`GROQ_API_KEY` and `GITHUB_MODELS_TOKEN` are filled in too.

Claude Code users can run `/deploy` instead, which wraps the same CLI.

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

99 deterministic tests, no real network calls — mocks GitHub's REST API (at
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
| `scripts/manual_verify_step4.py` | Gemini provider through the validate-repair layer (⚠️ currently fails — see below) |
| `scripts/manual_verify_groq.py` | Groq provider through the validate-repair layer |
| `scripts/manual_verify_github_models.py` | GitHub Models provider through the validate-repair layer |
| `scripts/seed_demo_pr.py` | Opens a real PR with planted issues (`fixtures/bad_code/`) on the test repo |
| `scripts/demo_provider_swap.py` | `LLM_PROVIDER` is a genuine runtime seam: Groq succeeds, Gemini fails gracefully, comment renders either way |

### Live end-to-end rehearsal

1. Ensure the Render service is deployed and `/healthz` returns 200.
2. The GitHub App's webhook URL is already the stable Render URL — no per-run
   update needed (`uv run python -m scripts.deploy` sets it once).
3. `uv run python scripts/seed_demo_pr.py` — opens a real PR with a
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

- **Vertex AI**: implemented per spec, covered by mocked tests only — **never
  live-verified**. Requires GCP billing, which this project's setup
  deliberately avoids.
- **Gemini (AI-Studio)**: implemented and was live-verified initially, but the
  API key's underlying Google account got an automated **Trust & Safety
  access flag** (`403 PERMISSION_DENIED`) partway through development —
  confirmed, via Google's own AI Developer Forum, to be account-level and
  fixable only by attaching billing. Tried fresh keys under multiple
  different Google accounts; all blocked. **Gemini is not expected to work
  live in this environment without that trade-off.**
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
