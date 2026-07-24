# SETUP.md — Step 0 prerequisites (completed)

This documents what was set up and where the values live. No raw secrets are
included here — see the (gitignored) `.env` and `github-app-private-key.pem`.

## 1. GitHub App

- Created via the **App Manifest flow** (a local HTML form POSTed a manifest to
  `https://github.com/settings/apps/new`; GitHub's redirect delivered a one-time
  `code`, exchanged via `POST /app-manifests/{code}/conversions` for the App ID,
  PEM, and webhook secret in one step — no manual "generate private key" click
  needed).
- App: **`tov-pr-review-bot-testbed`** (App ID in `.env` as `GITHUB_APP_ID`).
- Permissions: `pull_requests: write`, `contents: read`, `issues: write`,
  `metadata: read`. Events: `pull_request`.
- Installed on throwaway test repo: `SomeoneSomewhereelse/pr-review-bot-testbed`
  (created via `gh repo create --private`). Installation ID captured via
  `GET /app/installations` (signed with a short-lived JWT built from the PEM)
  → stored as `GITHUB_APP_INSTALLATION_ID`.
- **Webhook URL**: currently a placeholder (`https://example.com/webhook`) from
  app creation. **Must be updated** in the app's webhook settings
  (`https://github.com/settings/apps/tov-pr-review-bot-testbed`) once the
  Cloudflare quick tunnel is running and the local server is up (step 1 of the
  build) — see the Tunnel section below for why this happens on every restart.
- Private key: downloaded as part of the manifest exchange, saved to
  `study/final_project/github-app-private-key.pem` (gitignored). Referenced by
  path via `GITHUB_APP_PRIVATE_KEY_PATH` in `.env` (chosen over base64-encoding
  the key inline).

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

## 3. Cloudflare Tunnel — quick tunnel, not named

- **Deviation from the original plan:** a *named* tunnel requires a domain
  added as a Cloudflare zone. The user doesn't own a domain and declined to
  buy one (Cloudflare Registrar, ~$1-9/yr) or claim a free one via GitHub
  Student Pack.
- Using a **quick tunnel** instead: `cloudflared tunnel --url http://localhost:8000`.
  No login, no account, no domain needed — verified working (smoke-tested
  against a temporary local server, got a `*.trycloudflare.com` URL with clean
  connectivity pre-checks).
- **Known limitation:** the hostname is random and changes every time the
  tunnel restarts. **Each time you start the tunnel, you must update the
  GitHub App's webhook URL** to the new hostname + `/webhook`
  (`https://github.com/settings/apps/tov-pr-review-bot-testbed` → General →
  Webhook URL).
- `gcloud` and `cloudflared` were installed via `winget` (`Google.CloudSDK`,
  `Cloudflare.cloudflared`); both are confirmed on PATH after a terminal
  restart.

## 4. Secrets hygiene

- Root `.gitignore` updated (before any secret file existed) to ignore
  `study/final_project/.env`, `*.pem`, `.venv/`, `__pycache__/`.
- `.env.example` committed with placeholders for every var; `.env` itself and
  `github-app-private-key.pem` are real values, gitignored, never committed.
- All work happens on branch `feat/project-d-code-review-engine` — `master`
  untouched.

## Redo-from-scratch notes

If any of this needs to be redone (e.g. rotating the webhook secret, a new PEM):
- GitHub App settings: `https://github.com/settings/apps/tov-pr-review-bot-testbed`
- Test repo: `https://github.com/SomeoneSomewhereelse/pr-review-bot-testbed`
- Gemini key management: `https://aistudio.google.com/app/apikey`
- To start the tunnel: `cloudflared tunnel --url http://localhost:8000`, then
  copy the printed `https://*.trycloudflare.com` URL into the GitHub App's
  webhook URL setting (append `/webhook`).
