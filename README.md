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
`pytest`/`pytest-asyncio`/`respx` · Docker · Cloudflare Tunnel.

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

### Exposing a public webhook URL

This project uses a **Cloudflare quick tunnel** (see "Known limitations"):

```bash
cloudflared tunnel --url http://localhost:8000
```

Copy the printed `https://*.trycloudflare.com` hostname into the GitHub
App's webhook URL setting (`.../settings/apps/<slug>` → General → Webhook
URL), appending `/webhook`. **This has to be redone every time the tunnel
restarts** — the hostname is random and doesn't persist.

## Testing

```bash
uv run ruff check .
uv run pytest -v
```

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

1. Start the app (`uv run uvicorn ...`) and a tunnel (`cloudflared tunnel
   --url http://localhost:8000`).
2. Update the GitHub App's webhook URL to the tunnel's hostname + `/webhook`.
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
- **Cloudflare quick tunnel**, not a named tunnel — no domain was available
  to register as a Cloudflare zone. The webhook URL must be updated on every
  tunnel restart (see above); a named tunnel would remove this step but
  needs a domain.
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

Demo runs at **$0** (Groq + Cloudflare free tiers). Documented production
cost model (~$8-10/mo at brief scale) is in [`cost.md`](cost.md) — cost is
graded as that documented calculation, not actual spend.
