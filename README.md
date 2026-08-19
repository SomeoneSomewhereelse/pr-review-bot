# Autonomous Code Review Engine

A GitHub PR webhook triggers an **Orchestrator** that fetches the diff and
fans out to three parallel LLM specialists — **Security**, **Performance**,
**Code Quality** — each backed by a structured-output LLM call. Findings are
merged into a single Markdown PR comment, edited in place on later pushes.

**[Deploy your own →](https://someonesomewhereelse.github.io/pr-review-bot/setup/)**
— the full setup guide, from a fresh clone to a first posted review comment,
covering both a local run and a hosted Render + Supabase deployment. The same
pages live in [`guide/`](guide/setup/index.md) if you would rather read them
in the repo.

Full design: [`SPEC.md`](SPEC.md). Stack/conventions: [`CLAUDE.md`](CLAUDE.md).
Cost model: [`cost.md`](cost.md). This project's actual live configuration
differs from `SPEC.md`'s defaults in a few documented ways — see "Known
limitations" below.

## Architecture

```
GitHub PR (opened / reopened / synchronize)
  └─▶ POST /webhook
        (1) read RAW body → verify HMAC-SHA256 (constant-time) → 401 if bad
        (2) dedup on X-GitHub-Delivery → 200 no-op if already seen
        (3) enqueue/update a durable Postgres ticket for this PR → 202
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
cp .env.example .env   # fill in real values — see the guide's setup section
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

**Faster local iteration:** `eval "$(uv run python -m scripts.test_db)"` once per shell session starts a persistent local test Postgres and exports `DATABASE_URL`, so `pytest` skips testcontainers' cold boot; `uv run python -m scripts.test_db down` tears it down.

821 deterministic tests, no real network calls — mocks GitHub's REST API (at
the `requests` transport layer PyGithub uses), all LLM providers' SDK
clients, and the webhook HTTP layer. CI (`.github/workflows/ci.yml`
at the repo root, path-filtered to this directory) runs `ruff` + `pytest` on
every push/PR touching this project.

Run `pytest -m "not db and not xdist_meta"` to skip Postgres-touching tests and this suite's own slow xdist-scheduling meta-test for the fastest inner loop; CI still runs the full suite.

### Live verification scripts

These make real network calls against real accounts/services — not run by
CI. Each is self-contained and prints what it's proving:

| Script | Proves |
|---|---|
| `scripts/manual_verify_step3.py` | GitHub App auth, diff fetch, comment upsert (edit-in-place) against a real PR |
| `scripts/manual_verify_step4.py` | Gemini provider through the validate-repair layer |
| `scripts/manual_verify_groq.py` | Groq provider through the validate-repair layer |
| `scripts/manual_verify_vertex.py` | Vertex AI provider (service-account or ADC) through the validate-repair layer |
| `scripts/seed_demo_pr.py` | Opens a real PR with planted issues (`fixtures/bad_code/`) on the test repo |
| `scripts/demo_provider_swap.py` | `LLM_PROVIDER` is a genuine runtime seam — see below for this script's current expected behavior |

A real, repeated end-to-end rehearsal of this project — through the actual
GitHub webhook path, both locally and hosted — is logged in the guide's
[Live rehearsal history](guide/background/rehearsals.md).

## Known limitations (deviations from `SPEC.md`, all deliberate)

- **Vertex AI**: live and fully verified (`LLM_PROVIDER=vertex`), reinstated
  2026-08-14 — it had been removed while this project's no-card constraint
  made it unrunnable, and came back once GCP billing/ADC access became
  available. Unlike the other two providers its credential is a GCP
  service-account identity: `GCP_SERVICE_ACCOUNT_KEY` (hosted, base64) →
  implicit ADC. Two bugs were found and fixed via real live calls
  before it succeeded (see the guide's [Provider history](guide/background/providers.md)
  for the full history): a missing OAuth scope on the service-account
  credential path, and the shared `LLM_MODEL` default (`gemini-flash-latest`)
  not existing as a Vertex publisher model for this project — Vertex's
  catalog only carries the 2.5 generation here
  (`gemini-2.5-flash`/`gemini-2.5-flash-lite`), not the AI-Studio alias.
  **Confirmed live**: with the model set to `gemini-2.5-flash`, a real call
  through `scripts/manual_verify_vertex.py` against real Vertex AI returned a
  valid structured-output response with non-zero token usage. Vertex now owns
  its own model var, `VERTEX_MODEL` (default `gemini-2.5-flash`, the
  confirmed-working value) — sharing `LLM_MODEL` with gemini made a DB
  provider flip to vertex guaranteed-broken, since gemini's default 404s on
  Vertex's catalog. Operators only need to override `VERTEX_MODEL` if their
  own project's Vertex publisher-model catalog differs.
- **Gemini (AI-Studio)**: live and working (`LLM_PROVIDER=gemini`) — re-verified
  2026-08-10 via `scripts/manual_verify_step4.py` (real structured output,
  non-zero token usage). See the guide's [Provider history](guide/background/providers.md)
  for the account-access history this project worked through earlier.
  `scripts/demo_provider_swap.py`'s description of a graceful Gemini failure
  predates this and hasn't been re-run against the current key.
- **Groq is the primary live provider** (`LLM_PROVIDER=groq`,
  `llama-3.3-70b-versatile`) — pulled forward from a later build step
  specifically to have a working live path.
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
  cap. Groq is documented to send `retry-after`.

See the guide's [Provider history](guide/background/providers.md) for the
full narrative of each deviation, including what was tried and why.

## Cost

Demo runs at **$0** (Groq + Render + Supabase free tiers). Documented production
cost model (~$8-10/mo at brief scale) is in [`cost.md`](cost.md) — cost is
graded as that documented calculation, not actual spend.
