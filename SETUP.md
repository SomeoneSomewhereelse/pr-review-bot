# SETUP.md — Step 0 prerequisites (completed)

This documents what was set up and where the values live. No raw secrets are
included here — see the (gitignored) `.env` and `github-app-private-key.pem`.

## 1. GitHub App

- Created via the **App Manifest flow** (a local HTML form POSTed a manifest to
  `https://github.com/settings/apps/new`; GitHub's redirect delivered a one-time
  `code`, exchanged via `POST /app-manifests/{code}/conversions` for the App ID,
  PEM, and webhook secret in one step — no manual "generate private key" click
  needed).
- App: **`<your-app-slug>`** (App ID in `.env` as `GITHUB_APP_ID`).
- Permissions: `pull_requests: write`, `contents: read`, `issues: write`,
  `metadata: read`. Events: `pull_request`.
- Installed on throwaway test repo: `<your-user>/pr-review-bot-testbed`
  (created via `gh repo create --private`). Installation ID captured via
  `GET /app/installations` (signed with a short-lived JWT built from the PEM)
  → stored as `GITHUB_APP_INSTALLATION_ID`.
- **Webhook URL**: currently a placeholder (`https://example.com/webhook`) from
  app creation. **Must be updated** in the app's webhook settings
  (`https://github.com/settings/apps/<your-app-slug>`) once the
  Cloudflare quick tunnel is running and the local server is up (step 1 of the
  build) — see the Tunnel section below for why this happens on every restart.
- Private key: downloaded as part of the manifest exchange, saved to
  `github-app-private-key.pem` at the repo root (gitignored). Referenced by
  path via `GITHUB_APP_PRIVATE_KEY_PATH` in `.env` (chosen over base64-encoding
  the key inline).
- **Obtaining the App ID** (needed as `GITHUB_APP_ID`): open the App's settings
  at `https://github.com/settings/apps/<your-app-slug>` → **General** → **App
  ID**, near the top. The manifest-conversion flow above also returns it
  directly. Three IDs sit close together and only two are used here:
  - **App ID** → `GITHUB_APP_ID`. A short integer. `app/config.py` types it as
    `int`, so a non-numeric paste fails config validation at startup, and
    `app/github_app.py` reports "likely a bad `GITHUB_APP_ID`" on a 401.
  - **Installation ID** → `GITHUB_APP_INSTALLATION_ID`. **Optional** — the app
    auto-discovers it at boot when unset. Capture it manually via
    `GET /app/installations` (signed with a short-lived JWT) if you want it
    pinned.
  - **Client ID** — not used by this project at all. Easy to grab by mistake,
    since it sits on the same page.

## 2. LLM provider — Groq (live), not Vertex, Gemini currently blocked

- **Deviation from the original plan (GCP/Vertex):** Vertex AI requires a
  billing account (card) to enable. The user declined to add one, so no GCP
  project was created and Vertex is **not configured** (code path exists per
  SPEC.md's architecture, covered by mocked tests only, untested live).
- **Deviation from the original plan (Gemini):** initially set up with a free
  AI-Studio key (`GEMINI_API_KEY`) and `LLM_PROVIDER=gemini`, implemented at
  build step 4. Live verification then failed: `403 PERMISSION_DENIED — Your
  project has been denied access` on newer flash models, persistent `429` on
  older ones. Per Google's own AI Developer Forum, this is an **automated
  Trust & Safety account flag** (Google staff confirmed "a flag has been
  placed on your account" in one thread) — not a model-naming, project, or
  code issue. One documented trigger: hitting repeated 429s / testing many
  models back-to-back without backoff, which is exactly what happened here
  during troubleshooting (see CLAUDE.md's "LLM API testing hygiene" section,
  added as a direct result).
  **Confirmed exhausted, as of 2026-07-23:** tried a second API key under a
  different Google project — same 403. Tried keys under multiple genuinely
  different Google accounts — all blocked too. Per the forum, the only
  documented fix is attaching GCP billing (which this project deliberately
  avoids). **Gemini is not expected to become usable without that trade-off**;
  cross-vendor provider-agnosticism is demonstrated via Groq instead, and
  other AI providers may be evaluated later to strengthen that story.
- **Build step 7 (provider-swap demo, `scripts/demo_provider_swap.py`):**
  since Gemini genuinely fails live, the swap demo turned this into a real
  (not simulated) proof of two things at once: (1) `LLM_PROVIDER` is a true
  runtime seam — swapping it changes provider behavior with no server
  restart; (2) the resilience guarantee holds even under **total** failure —
  every specialist's own never-raise contract catches the real Gemini error
  and the orchestrator still posts a coherent comment with 3 visible failed
  rows, no crash. One oddity noted during this: an isolated direct call to
  `GeminiProvider` consistently reproduces the documented `403`, but running
  through the full `orchestrator.run_review()` pipeline (real PR diff content
  as the prompt) once produced a `401 ACCESS_TOKEN_TYPE_UNSUPPORTED` instead —
  not reproduced after several isolated retries (concurrency, sequencing
  after Groq, matching model/config all ruled out). Likely just another
  inconsistent error shape from the same flagged-account block depending on
  request specifics, not chased further to avoid more burst-testing against
  the blocked provider (see CLAUDE.md's testing-hygiene section).
- **Current live provider: Groq** (`LLM_PROVIDER=groq`), pulled forward from
  build step 7 to have a working live path. Free tier, no card
  (https://console.groq.com/keys). Model: `llama-3.3-70b-versatile` (`GROQ_MODEL`
  env var — kept separate from `LLM_MODEL`, which stays scoped to the
  google-genai family/vertex-gemini, since one shared var stopped making sense
  across two unrelated provider families). Structured output uses
  `json_object` mode + a schema-instructing system prompt (this model doesn't
  support Groq's `json_schema` constrained decoding — verified live).
- The model-choice question for Gemini/Vertex (which flash generation, given
  free-tier rate caps) is explicitly deferred — `LLM_MODEL` stays at
  `gemini-flash-latest` for now, to be revisited once the account access issue
  is resolved.

## 2b. Third provider — GitHub Models (added post-step-8, real cross-vendor demo)

The user wanted a second genuinely-live cross-vendor provider (beyond Groq)
to demonstrate at showcase time, since Gemini isn't expected to come back.
Researched free-tier options with a hard constraint: RPM low enough to risk
the 15s target given 3 concurrent specialist calls per review, or a card
requirement, both disqualify a candidate.

- **Cerebras — tried, ruled out.** Despite older blog posts describing a
  perpetual free RPM quota, the account's actual current policy (confirmed
  live) is a "$5 free credit" that still requires billing info attached —
  every available model (`gpt-oss-120b`, `zai-glm-4.7`, `gemma-4-31b`)
  returned `402 Payment Required`. Same dealbreaker as Vertex.
- **Mistral — not attempted.** Reported free-tier RPM as low as 1 request/min
  (unconfirmed exact number — Mistral stopped publishing free-tier limits
  publicly), which would seriously risk the 15s target given our 3-concurrent-
  calls-per-review pattern. Cohere was also considered (20 RPM, no card) but
  needs a new separate account/signup, explicitly trial-only.
- **GitHub Models — chosen.** Rides the user's existing GitHub account (a
  fine-grained PAT with the "Models: read" permission, created at
  github.com/settings/personal-access-tokens/new) — no new account/signup,
  no new account-flagging risk. OpenAI-compatible API
  (`https://models.github.ai/inference`), real OpenAI models
  (`openai/gpt-4o-mini` — `GITHUB_MODELS_MODEL` env var). Genuinely different
  vendor AND model family from both Gemini (Google) and Groq (Llama) — the
  strongest cross-vendor story among providers actually usable here.
  **Known caveat (flagged by the user, not yet addressed):** free-tier rate
  limits are modest (single-digit RPM / ~150 requests per day on low-access
  models) — fine for a demo, a real constraint at any sustained volume or
  possibly for automated grading that fires many reviews quickly. Revisit if
  that becomes a problem.
- **Real bug caught by live testing** (`app/providers/github_models.py`):
  OpenAI's strict `json_schema` mode requires `"additionalProperties": false`
  explicitly present on **every** object schema, including nested `$defs`
  entries — Pydantic's `model_json_schema()` doesn't set this anywhere by
  default. A flat test schema surfaced the top-level case first (live `400`);
  the real nested container schemas we actually use (e.g. `SecurityFindings`
  wrapping `SecurityFinding` via `$defs`) surfaced that a top-level-only fix
  wasn't enough (another live `400`, different nested path). Fixed with a
  generic recursive walker (`_add_additional_properties_false`) rather than
  special-casing `$defs`, so any nesting shape Pydantic produces is covered.
  Both cases are locked in by tests, not just fixed ad hoc.
- Live-verified end-to-end: single-schema call via
  `scripts/manual_verify_github_models.py`, then the real nested
  `SecurityFindings` schema directly, then a full 3-specialist
  `orchestrator.run_review()` run against PR #3 — **7.5 seconds**, all three
  specialists succeeded with real findings, comment posted and independently
  confirmed via `gh api`.
- Added `openai` (for the OpenAI-compatible client) and cleaned up
  `cerebras-cloud-sdk` from `pyproject.toml`/`uv.lock` once Cerebras was
  ruled out — don't leave unused deps behind after an abandoned attempt.

## 2a. Docker

Installed via `winget install Docker.DockerDesktop` (build step 8, deferred
from step 1 since it wasn't installed initially). Docker Desktop's daemon
started cleanly with no interactive setup needed on this machine (WSL2
backend already available). `docker build .` succeeds; the container boots
with `/healthz` → 200 and an unsigned `/webhook` → 401, confirmed. **Fixed a
real bug found during verification**: the Dockerfile's `CMD` used `uv run
uvicorn ...` without `--no-dev`, so every container start silently re-synced
and reinstalled dev dependencies (ruff, etc.) at runtime — the build-time
`uv sync --frozen --no-dev` didn't carry over to `uv run`'s own implicit sync.
Fixed by adding `--no-dev` to the `CMD`'s `uv run` invocation too.

## 2c. Running tests locally (Postgres prerequisite)

Since the queue store moved from local SQLite to Postgres, `uv run pytest`
needs a real Postgres reachable one of two ways:

- **Docker installed** (see 2a above) — `tests/conftest.py`'s `db_url`
  fixture spins up a throwaway Postgres 16 container via `testcontainers`
  automatically; nothing to configure.
- **No Docker** — set `DATABASE_URL` to point at a local/CI Postgres
  instance yourself, e.g. `DATABASE_URL=postgresql://postgres@127.0.0.1:5433/test
  uv run pytest`. Without either Docker or `DATABASE_URL`, the DB-touching
  tests fail with an opaque testcontainers error.

CI (`.github/workflows/project-d-ci.yml`) provides this automatically via a
`services: postgres` container — no action needed there.

## 3. Deploying to Render + Supabase (production)

The production deployment uses:
- **Supabase** for the durable review queue (Postgres), replacing local SQLite
- **Render** for the web service, replacing local machine + Cloudflare Tunnel
- **Free cron pinger** (cron-job.org or UptimeRobot) to keep both services warm

### 3.1 Supabase setup

1. Create a Supabase project at https://supabase.com
2. **Wait until the dashboard reports the project ready** (~2 minutes). A
   connection attempt against a still-provisioning project fails, and Render
   does not retry a failed deploy — see §3.2's troubleshooting note.
3. Open **Connect** (or Project Settings → Database) and copy the
   **Session-mode pooler** connection string — port **5432**, not 6543.
   - Shape: `postgresql://postgres.<project-ref>:<password>@aws-<region>.pooler.supabase.com:5432/postgres`
   - **Copy it verbatim; do not retype or reconstruct it.** Both the
     `postgres.<project-ref>` username and the region-varying subdomain are
     project-specific, and either one wrong yields
     `FATAL: Tenant or user not found`.
   - If the password contains `@ # / ?`, percent-encode it — those characters
     terminate fields in a URI.
4. Set it as the `DATABASE_URL` env var (locally in `.env`, and in the Render
   dashboard per §3.2).
5. Optional hardening: libpq's default `sslmode=prefer` gets an encrypted
   connection but performs no certificate verification. For MITM protection use
   `sslmode=verify-full` together with Supabase's CA certificate. The app does
   not enforce this.

### 3.2 Render web service setup

1. Push the `feat/supabase-hosting` branch to GitHub (if testing locally)
2. Go to https://render.com/dashboard
3. Click **"New +"** → **Blueprint** → connect your GitHub repo and point it at
   `render.yaml` at the repo root. `render.yaml` declares `runtime: docker`
   with a `dockerfilePath`, so Render builds and runs this project's
   `Dockerfile` as-is — there is no separate Build/Start command to configure;
   the container's entrypoint is the Dockerfile's own `CMD`
   (`uv run --no-dev uvicorn app.main:app --host 0.0.0.0 --port 8000`).
4. In the **Environment** tab, set these variables:
   - `DATABASE_URL`: the Supabase Session-mode pooler string (from above)
   - `GITHUB_APP_ID`: the numeric App ID — see §1 for where to find it
   - `GITHUB_APP_PRIVATE_KEY_B64`: base64-encoded PEM (see "Secrets encoding" below)
   - `GITHUB_TARGET_REPO`: e.g., `<your-user>/pr-review-bot-testbed`
   - `GITHUB_WEBHOOK_SECRET`: (from `.env`)
   - `LLM_PROVIDER`: `groq` (or your chosen provider)
   - `GROQ_API_KEY`: (if using Groq)
   - (Other provider creds as needed: `GEMINI_API_KEY`, etc.)
   - Do **not** set `GITHUB_APP_INSTALLATION_ID`. Leaving it unset is
     deliberate: the app auto-discovers it at boot from the App JWT.
   - `RENDER_API_KEY` is **not** a service env var. It is optional
     operator-local tooling (Account Settings → API Keys) that lets deploy
     scripts set env vars and read logs from your machine. Never add it to
     `render.yaml` and never give it to the service.
5. Click **Deploy**
6. **Verify before considering this step done:**
   - The deploy's logs end with uvicorn's `Application startup complete.`
   - `curl https://<your-service>.onrender.com/healthz` returns `{"status":"ok"}`.

**Troubleshooting the first deploy.** If it fails with
`error connecting in 'pool-1'` or a `RuntimeError` about the connection not
opening, the usual cause is a Supabase project that was not ready yet, or a
mistyped pooler string (§3.1). Render does **not** retry failed deploys
automatically, and a first deploy leaves no previous instance running — fix the
value and click **Manual Deploy**.

### 3.3 Secrets encoding

The PEM file must be base64-encoded for the `GITHUB_APP_PRIVATE_KEY_B64` env var:

```bash
base64 -w0 < github-app-private-key.pem
```

Copy the output and paste it into the Render dashboard's `GITHUB_APP_PRIVATE_KEY_B64`
field (the app code will decode it at startup).

### 3.4 GitHub App installation and webhook registration

1. **Install the GitHub App** on your test repo:
   - Go to https://github.com/settings/apps/<your-app-slug>
   - Click **Install App** (if not already installed)
   - Select the test repo (e.g., `<your-user>/pr-review-bot-testbed`)

2. **Register the webhook URL** (one-time, after Render deployment):
   Once Render finishes deploying, you'll have a public URL (e.g.,
   `https://pr-review-bot.onrender.com`). Run the registration script
   **locally, from your own machine — not inside the Render container**
   (`scripts/` is intentionally not copied into the Docker image; see
   `Dockerfile`, which only `COPY`s `app/`). Since `RENDER_EXTERNAL_URL` is an
   env var Render injects only *inside* its own container, it will not be set
   on your laptop, so pass the public URL explicitly via `PUBLIC_BASE_URL`:
   ```bash
   PUBLIC_BASE_URL=https://pr-review-bot.onrender.com uv run python -m scripts.deploy
   ```
   This script will:
   - Authenticate as the GitHub App (using the PEM)
   - Confirm the installation ID
   - Post the Render URL + `/webhook` to the GitHub App's webhook settings
   - Return success if everything is set up correctly

3. **Verify**: The GitHub App webhook URL setting
   (https://github.com/settings/apps/<your-app-slug> → General →
   Webhook URL) should now show your stable Render URL.

### 3.5 Keep-warm pinger (free)

Both Render (free tier) and Supabase (free tier) spin down after inactivity.
To keep them warm and ensure fast responses:

1. Go to https://cron-job.org or https://uptimerobot.com (both free)
2. Create a new job/monitor that GETs your Render URL's `/healthz` endpoint
   every 10 minutes
3. This keeps Render warm and also keeps Supabase un-paused (the dispatcher
   polls the queue continuously, so pinging `/healthz` guarantees activity)

## 3.6 Cloudflare Tunnel (local testing only, optional)

For **local development only**, a quick tunnel can still be used:
- `cloudflared tunnel --url http://localhost:8000`
- No login, no account, no domain needed
- **Known limitation:** the hostname is random and changes every restart
- Each restart requires manually updating the GitHub App's webhook URL
- `gcloud` and `cloudflared` were installed via `winget`

**Note:** production uses the stable Render URL instead; the tunnel is purely
optional for local manual testing if needed.

## 4. Secrets hygiene

- Root `.gitignore` updated (before any secret file existed) to ignore
  `.env`, `*.pem`, `.venv/`, `__pycache__/`.
- `.env.example` committed with placeholders for every var; `.env` itself and
  `github-app-private-key.pem` are real values, gitignored, never committed.

## Repo history note

This repo was extracted from a course repository (Tov-learn), where it lived
at `study/final_project/` on branch `feat/project-d-code-review-engine`, via
`git subtree split` — full commit history preserved, paths rewritten relative
to this repo's root. The course repo's copy (and that branch) still exist
independently; this is now the standalone home going forward.

## Live rehearsal history (build step 8, final E2E)

| PR | Path exercised | Result |
|---|---|---|
| #2 | Direct `orchestrator.run_review()` call (step 5/6 milestone) | 8s, all 3 specialists ok, comment correct |
| #2 | `demo_provider_swap.py` (step 7) | groq ok → gemini fails gracefully (real error) → groq restored |
| **#3** | **Real GitHub webhook delivery** (quick tunnel + `PATCH /app/hook/config` JWT-updated URL + `seed_demo_pr.py`) | **8s** PR-created → comment-appeared, all 3 specialists found real issues |

PR #3 is the definitive rehearsal for step 8/11's verification — it's the
first run of the *actual* webhook path end-to-end (GitHub → tunnel → HMAC
verify → dedup → background task → orchestrator → comment), not a direct
function call or synthetic payload. Comment appeared well under the 15s
target.

## Redo-from-scratch notes

If any of this needs to be redone (e.g. rotating the webhook secret, a new PEM):
- GitHub App settings: `https://github.com/settings/apps/<your-app-slug>`
- Test repo: `https://github.com/<your-user>/pr-review-bot-testbed`
- Gemini key management: `https://aistudio.google.com/app/apikey`
- To start the tunnel: `cloudflared tunnel --url http://localhost:8000`, then
  copy the printed `https://*.trycloudflare.com` URL into the GitHub App's
  webhook URL setting (append `/webhook`).
