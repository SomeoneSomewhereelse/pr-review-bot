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
  build step 4. Live verification then failed: the key's underlying GCP
  project returns `403 PERMISSION_DENIED — Your project has been denied
  access` on newer flash models and `429` quota-exhausted on older ones,
  confirmed across multiple model names — an **account-level restriction**,
  not a model-naming or code issue. Unresolved as of 2026-07-23; revisit later
  (check https://aistudio.google.com/app/apikey and the linked GCP project's
  status, or generate a key under a different Google account).
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
