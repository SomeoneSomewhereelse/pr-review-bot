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
- **Webhook URL**: set to the deployed service's `<public-url>/webhook`. This is
  stable and set once by `uv run python -m scripts.deploy` (§3.4) — it does not
  need re-editing between runs.
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

## 2. LLM provider — Groq (live); Vertex reinstated 2026-08-14; Gemini blocked, then resolved

- **Deviation from the original plan (GCP/Vertex):** Vertex AI requires a
  billing account (card) to enable. The user declined to add one, so no GCP
  project was created and Vertex is **not configured** (the adapter was
  implemented, then removed — see `CLAUDE.md`'s "Substitutions from the
  brief").
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
  **Resolved, 2026-08-10:** the API key was updated (new key, same or a
  different Google account — not investigated further) and
  `scripts/manual_verify_step4.py` now succeeds live: real structured output,
  non-zero token usage, no `403`. Whatever specifically tripped the flag on
  the earlier key isn't reproduced here, and wasn't chased further — per
  `CLAUDE.md`'s testing-hygiene rule, this was one deliberate call, not a
  root-cause investigation. Groq remains the provider used for the live demo
  regardless of this; Gemini being usable again doesn't change that choice.
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
  env var — kept separate from `LLM_MODEL`, since one shared var stopped
  making sense across two unrelated provider families; vertex later got the
  same treatment via `VERTEX_MODEL`, see below). Structured output uses
  `json_object` mode + a schema-instructing system prompt (this model doesn't
  support Groq's `json_schema` constrained decoding — verified live).
- The model-choice question for Gemini/Vertex (which flash generation, given
  free-tier rate caps) is explicitly deferred — `LLM_MODEL` stays at
  `gemini-flash-latest` for now. The account-access issue is resolved (above),
  but this question hasn't been revisited since; still open.
- **Vertex reinstated, 2026-08-14:** the no-card constraint that ruled Vertex
  out no longer applies — GCP billing/ADC access became available, so
  `LLM_PROVIDER=vertex` is now a real, code-complete provider.
  `scripts/manual_verify_vertex.py` was run once against a real GCP
  service-account credential, per this project's one-deliberate-call testing
  hygiene rule (see `CLAUDE.md`): credential resolution and project-id
  derivation both worked correctly (a real project id was resolved, and no
  credential material was ever printed), and the call reached Google's real
  OAuth token endpoint — a genuine network round-trip, not a mock — proving
  the vertex code path (credential resolution → `VertexProvider` construction
  → the `google-genai` `vertexai=True` client → an actual HTTPS call) is wired
  correctly end-to-end. The call itself then failed with
  `google.auth.exceptions.RefreshError: invalid_scope: Invalid OAuth scope or
  ID token audience provided`. Root cause identified and fixed: `VertexProvider`
  was constructing its service-account credentials without the required
  `cloud-platform` OAuth scope (`app/providers/google_genai.py`) — the
  implicit-ADC path already had the correct scope via `google-genai`'s own
  SDK, but the explicit service-account path (the hosted/Render production
  configuration) did not. Per the testing-hygiene rule, this was **not**
  retried with a different scope/key while diagnosing it; instead, one
  deliberate follow-up call was made once the fix had landed.

  **Follow-up run, same day: the OAuth-scope fix is confirmed working.**
  Re-running `scripts/manual_verify_vertex.py` once against a real GCP
  service-account credential no longer hits `invalid_scope` — credential
  resolution, project derivation, and OAuth token refresh all succeeded, a
  genuine network round-trip against Google's real infrastructure, not a
  mock. The call then reached Vertex AI's real `generateContent` endpoint and
  failed with a different, unrelated error: `404 NOT_FOUND: Publisher model
  'projects/tovtech-vertex-imagen/locations/us-central1/publishers/google/models/gemini-flash-latest'
  was not found or your project does not have access to it.` This is not a
  new bug — it's the model-choice question for Gemini/Vertex noted above
  ("`LLM_MODEL` stays at `gemini-flash-latest` for now... still open"), now
  with concrete evidence: Vertex's publisher-model catalog uses its own model
  ids (often dated, e.g. `gemini-2.0-flash-001`-style) that don't necessarily
  mirror AI-Studio's aliases, so `gemini-flash-latest` doesn't resolve as a
  Vertex publisher model for this project/region. Per the one-deliberate-call
  rule, this was **not** retried with a different model name.

  **Model-choice question resolved for this project, same day: full live
  success.** Rather than guessing model IDs via repeated `generateContent`
  calls, candidate model IDs were checked via lightweight, no-cost
  `GET https://us-central1-aiplatform.googleapis.com/v1/publishers/google/models/{model}`
  catalog-existence requests first (metadata reads, not generation calls —
  checking several of these in one pass is not the "bursting live calls"
  pattern the testing-hygiene rule targets, since there's no token cost and
  no completion request involved). Result: `gemini-2.0-flash-001`,
  `gemini-2.0-flash-lite-001`, `gemini-1.5-flash-002`, and
  `gemini-flash-latest` all 404; `gemini-2.5-flash` and
  `gemini-2.5-flash-lite` both exist — this project's Vertex catalog only
  carries the 2.5 generation. One deliberate `generateContent` call was then
  made with `LLM_MODEL=gemini-2.5-flash`: **full success** — a valid
  structured-output response with non-zero token usage
  (`Greeting(message='Hello there!')`, 20 tokens in / 8 out), the first
  genuinely complete end-to-end live verification of this provider. A
  `("vertex", "gemini-2.5-flash")` pricing entry was added
  (`app/providers/pricing.py`) to match, since the call otherwise succeeds
  and then hits a `KeyError` at cost-estimation time.

  **Operational note:** `LLM_MODEL`'s shared default (`gemini-flash-latest`)
  did not resolve for vertex on this project — vertex has since been split
  onto its own `VERTEX_MODEL` var (default `gemini-2.5-flash`, the
  confirmed-working value), so an operator enabling vertex gets a working
  model with no override needed, and only has to set `VERTEX_MODEL` if their
  own project's Vertex publisher-model catalog differs. Its credential is a GCP
  service-account identity, not an API key, resolved in three layers by
  `app/providers/vertex_credentials.py`:
  1. `GCP_SERVICE_ACCOUNT_KEY_B64` (+ numbered `_1`/`_2` siblings) — the
     hosted/Render path, selected by the same `vertex_key_index` override
     gemini/groq use.
  2. `GCP_SERVICE_ACCOUNT_KEY_PATH` (default `./gcp-service-account-key.json`,
     gitignored; + numbered siblings) — local-dev only, for testing several
     service accounts without touching Render or Supabase.
  3. Implicit ADC — with neither of the above, `google-auth` discovers
     `gcloud auth application-default login`'s local credentials on its own.

  `GCP_PROJECT` is an **optional** override: unset, the project is read from
  the service-account key's own `project_id`, so an operator handed nothing
  but a JSON key needs no separate project lookup. `GCP_LOCATION` defaults to
  `us-central1`.

  **Deploying vertex to Render requires the base64 form.** Render has neither
  a local key file nor a `gcloud` login, so `scripts/deploy.py`'s `config` and
  `provider` checks FAIL for `LLM_PROVIDER=vertex` unless
  `GCP_SERVICE_ACCOUNT_KEY_B64` is set locally — that is the value `--sync-env`
  pushes. A file-only local setup is fine for running the app locally, but is
  deliberately not considered deployable. `--sync-env` does not push
  `GCP_PROJECT`/`GCP_LOCATION` — if you rely on non-default values for
  either, set them manually in the Render dashboard.

## 2b. Third provider — GitHub Models (added post-step-8, real cross-vendor demo)

The user wanted a second genuinely-live cross-vendor provider (beyond Groq)
to demonstrate at showcase time — at the point this decision was made,
Gemini was not expected to come back (see above; it has since been
re-verified working, but that finding came later and didn't change this
provider's rationale). Researched free-tier options with a hard constraint:
RPM low enough to risk
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
   - (Other provider creds as needed: `GEMINI_API_KEY`, `GCP_SERVICE_ACCOUNT_KEY_B64`, etc.)
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

### 3.4 GitHub App installation, webhook registration, and verification

1. **Install the GitHub App** on your test repo:
   - Go to https://github.com/settings/apps/<your-app-slug>
   - Click **Install App** (if not already installed)
   - Select the test repo (e.g., `<your-user>/pr-review-bot-testbed`)

2. **Verify the deployment and register the webhook** (one-time, after Render
   deployment): Once Render finishes deploying, you'll have a public URL (e.g.,
   `https://pr-review-bot.onrender.com`). Run `scripts/deploy.py`
   **locally, from your own machine — not inside the Render container**
   (`scripts/` is intentionally not copied into the Docker image; see
   `Dockerfile`, which only `COPY`s `app/`). Since `RENDER_EXTERNAL_URL` is an
   env var Render injects only *inside* its own container, it will not be set
   on your laptop, so pass the public URL explicitly via `PUBLIC_BASE_URL`:
   ```bash
   PUBLIC_BASE_URL=https://pr-review-bot.onrender.com uv run python -m scripts.deploy
   ```
   It authenticates as the GitHub App (using the PEM), confirms the
   installation ID, and — since it reads the current webhook URL before
   writing — either reports it already correct or posts the Render URL +
   `/webhook` idempotently. It prints one line per check and always runs all
   eight, so a single run surfaces every problem rather than only the first:

   | Check | Verifies | Required? |
   |---|---|---|
   | `config` | Every setting the service needs is resolvable locally | yes |
   | `github-app` | The App is installed, and its webhook points here (set only if wrong) | yes |
   | `health` | `/healthz` answers **both** `GET` and `HEAD` — UptimeRobot's free tier sends `HEAD`, so a `GET`-only endpoint lets the instance sleep | yes |
   | `database` | Postgres is reachable **and** the app has provisioned its `tickets` table there | optional |
   | `provider` | The provider that will actually run — `LLM_PROVIDER`, or an active **DB override** — has its credential set | optional |
   | `provider-live` | The actively-resolved provider's credential (env or DB override) is present on the deployed Render service — not just locally | optional |
   | `render-service` | The latest Render deploy is `live`, and (when a commit is comparable) matches local `HEAD` | optional |
   | `uptime-pinger` | A monitor targets `/healthz` exactly, is active, and polls at most every 10 minutes | optional |

   A **DB override** is a runtime provider swap: `scripts/set_override.py`
   (§3.6 below) writes a provider name straight into the `runtime_config`
   table, and it wins over the `LLM_PROVIDER` env var starting at the
   dispatcher's next claimed ticket — no restart, no redeploy. `provider`
   resolves the override exactly the way the dispatcher does, so it never
   reports on a provider that isn't actually the one running.

   | Exit | Meaning |
   | --- | --- |
   | 0 | every check passed (skipped checks do not fail the run) |
   | 1 | at least one check failed |
   | 2 | the run could not proceed: `GITHUB_TARGET_REPO` or a public base URL is unset; `--sync-env` without `RENDER_API_KEY`; or a sync refused before any request (empty values, an unsupported `LLM_PROVIDER`, or an active DB override that would mask the push) |

   So: exit 0 means every check is green (or intentionally skipped), exit 1
   means the printed table has at least one `FAIL` row to act on, and exit 2
   means the run itself never got far enough to produce a trustworthy table.

   Five checks are skipped with a hint unless you set the matching
   operator-local key. None of these keys is ever set on the Render service
   itself:
   - `RENDER_API_KEY` (Render → Account Settings → API Keys) enables
     `render-service`, `provider-live`, and `--sync-env`.
   - `UPTIMEROBOT_API_KEY` (a read-only key) enables `uptime-pinger`.
   - `DATABASE_URL` enables both `database` and `provider` (the override
     lives in the same database). It is normally a Render dashboard secret;
     export it locally, temporarily, to check either.

3. **Verify**: The GitHub App webhook URL setting
   (https://github.com/settings/apps/<your-app-slug> → General →
   Webhook URL) should now show your stable Render URL.

4. **Deploying** (repeatable, once `RENDER_API_KEY` is set):
   ```bash
   PUBLIC_BASE_URL=https://pr-review-bot.onrender.com uv run python -m scripts.deploy --sync-env
   ```
   The set of vars pushed is **provider-derived**, not a fixed list: it
   always pushes `DATABASE_URL`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_B64`,
   `GITHUB_TARGET_REPO`, `GITHUB_WEBHOOK_SECRET`, `LLM_PROVIDER`, and **every
   provider's model var** — `LLM_MODEL`, `GROQ_MODEL`, and `VERTEX_MODEL` are
   all pushed regardless of which provider is selected, because a DB override
   (§3.6) can activate any provider with no redeploy, and a provider whose
   model var was never pushed would read a missing or stale value on the
   service the moment it became active. (This used to only push the
   selected provider's model var — a local `LLM_MODEL` sitting next to
   `LLM_PROVIDER=groq` used to not be enough to have it pushed; that gap is
   exactly what this change closes.) The **selected provider's** credential
   is always pushed too — for example `LLM_PROVIDER=groq` pushes
   `GROQ_API_KEY`. Beyond that, only a **credential** belonging to a
   different provider is ever pushed opportunistically — if `GEMINI_API_KEY`
   happens to have a local value, it goes along even though `groq` is
   selected, so a later dashboard-side provider switch has something to work
   with. It refuses to start (exit 2) if any wanted value is empty locally,
   so a blank `.env` entry can never overwrite a working secret on the
   service; only changed variables are pushed, and if nothing differs no
   deploy is triggered.

   If a DB override is active and disagrees with the `LLM_PROVIDER` you're
   about to push, `--sync-env` refuses (exit 2) — pushing would be pointless,
   since the override wins at runtime anyway. Clear it first:
   `uv run python -m scripts.set_override --clear` (§3.6).

   Before it triggers anything, it waits for any deploy already in progress
   to settle (it never stacks a second deploy on top of one still building)
   — worst case that's up to 900s waiting for the in-flight one, plus up to
   900s for the one it triggers itself, so **budget up to ~30 minutes** in
   the rare worst case. Measured warm redeploys with nothing already in
   flight have taken well under a minute (see the live-rehearsal history
   below for real numbers).

   Claude Code users can run `/deploy` instead, which wraps the same CLI.

### 3.5 Keep-warm pinger (free)

Both Render (free tier) and Supabase (free tier) spin down after inactivity.
To keep them warm and ensure fast responses:

1. Go to https://uptimerobot.com (free) — cron-job.org also works, but
   UptimeRobot is what `scripts/deploy.py`'s `uptime-pinger` check verifies
   against.
2. Create a new monitor that pings your Render URL's `/healthz` endpoint.
3. This keeps Render warm and also keeps Supabase un-paused (the dispatcher
   polls the queue continuously, so pinging `/healthz` guarantees activity).

The monitor's URL must be exactly `https://<your-service>.onrender.com/healthz`
— a stray trailing character (a comma pasted from prose, for instance) returns
404 on every check while the dashboard still shows the monitor firing on
schedule. Use an interval of **5 minutes**; anything above 10 lets Render's
~15-minute spin-down win. UptimeRobot's free tier sends `HEAD` rather than
`GET`, which is why `/healthz` answers both verbs.

### 3.6 Switching providers and API keys live: `scripts/set_override.py`

```bash
uv run python -m scripts.set_override groq                   # activate a provider
uv run python -m scripts.set_override --clear                 # remove the provider override
uv run python -m scripts.set_override groq --index 2          # activate groq AND its index-2 slot, together
uv run python -m scripts.set_override groq --index 2 --no-activate   # index only, leave the active provider alone
uv run python -m scripts.set_override groq --clear-index --no-activate  # clear index only
```

This writes a provider override and/or a provider's key-index override to
the `runtime_config` table in whatever database `DATABASE_URL` currently
resolves to — **not** necessarily production. Run it with your local `.env`
and you get a purely local override; it reaches the production service only
if your local `DATABASE_URL` happens to be the production one. The override
takes effect on the **next ticket the dispatcher claims** — no restart, no
redeploy, which is what makes it useful for a live provider-swap demo (build
step 7's `demo_provider_swap.py` predates this and used two `uvicorn`
restarts instead; a follow-up spec rewrite is tracked but deferred).

Each provider's credential env var can have numbered siblings (`GROQ_API_KEY`,
`GROQ_API_KEY_1`, `GROQ_API_KEY_2`, ...), provisioned like any other env var
(one redeploy, via `--sync-env` or the Render dashboard, to add a slot). Each
provider tracks its own key-index independently, and no secret value is ever
written to, read from, or logged by the database — only the slot's integer
index is.

Before writing, `set_override.py` verifies against the **effective** index —
whichever index will actually be active for that provider after the write —
against the live Render service, when `RENDER_API_KEY` is set and only when
the local `DATABASE_URL` is actually the one Render reads (if it isn't, e.g.
you're testing against a local database, the write cannot affect production
and verification is skipped automatically). It refuses by default (exit 2) if
the target credential is missing on Render or, for index 0, differs from your
local `.env` value; pass `--force` to write the override anyway. Clearing the
index override with `--no-activate` never verifies at all — a key rotation
must not be blockable by a Render/local mismatch. `scripts/deploy.py`'s
`provider-live` and `api-key-live` checks are the read-only counterparts to
this guard, and both are built on the same `GET /v1/services/{id}/env-vars`
call.

### 3.7 Tuning the re-review cooldown live: `scripts/set_cooldown.py`

```bash
uv run python -m scripts.set_cooldown --base 30 --factor 1.5   # tune for a demo
uv run python -m scripts.set_cooldown --cap 600
uv run python -m scripts.set_cooldown --clear                  # remove the override
```

Same pattern as `set_override.py` above: this writes the base/cap/factor
override to the `runtime_config` table in whatever database `DATABASE_URL`
currently resolves to — a local `.env` run sets a purely local override.
It takes effect on the **next ticket the dispatcher claims** — no restart,
no redeploy — which is what makes it useful for showing the escalating
cooldown speed up on stage instead of waiting out the 300s/3600s production
defaults.

Unlike `set_override.py` there's no credential at stake, only numbers, so a
non-cleared write is never refused for a credential reason; before writing,
it only checks (when `RENDER_API_KEY` is set) whether the local
`DATABASE_URL` matches the live Render service's, purely as an informational
signal, and proceeds regardless. It *does* refuse the write (exit 2) if the
resulting base/cap/factor would resolve to something `cooldown_config.
effective_config()` discards at read time (`factor < 1.0`, `base > cap`, or
a non-positive base/cap) — a single `--cap` below the env-configured base
would otherwise write successfully but be silently inert on every read.

**A DB override in force masks env-var changes.** If you change
`DISPATCHER_REREVIEW_COOLDOWN_SECONDS`/`_MAX_SECONDS`/`_FACTOR` in the Render
dashboard while a DB override is still set, the redeploy will appear to do
nothing — the override still wins at read time. Run
`uv run python -m scripts.set_cooldown --clear` first if you want the
env-var values to take effect again.

### 3.8 Deploying an image, for a service with no connected repo

Render **always builds on Render** — either from a connected GitHub repo, or
by pulling a pre-built image from a container registry. It never accepts or
uploads a local working tree. If your Render service is configured against a
registry image rather than a repo:

1. Build locally: `docker build -t ghcr.io/<you>/pr-review-engine:<tag> .`
2. Push it to the registry: `docker push ghcr.io/<you>/pr-review-engine:<tag>`
3. In the Render dashboard, point the service at that image and tag.
4. Run `--sync-env` (§3.4 step 4) to push config and trigger a deploy against
   the new image.

`render-service` reports whichever artifact is actually live — a git commit
sha for a repo-connected service, or the image ref for an image-backed one —
and only attempts the local-`HEAD` comparison when a commit is present; an
image-backed deploy reports `PASS` with "no local comparison possible"
rather than inventing a mismatch it has no way to check.

## 4. Secrets hygiene

- Root `.gitignore` updated (before any secret file existed) to ignore
  `.env`, `*.pem`, `.venv/`, `__pycache__/`.
- `.env.example` committed with placeholders for every var; `.env` itself and
  `github-app-private-key.pem` are real values, gitignored, never committed.

### 4.1 Two config files: `.env` (secrets) and `.env.config` (operational)

`app/config.py::Settings` reads both, in the order `env_file=(".env",
".env.config")` — the **last** file wins on a key present in both, so
`.env.config` is the designated home and always outranks a stale duplicate
left behind in `.env`. `OPERATIONAL_KEYS` (`app/config.py`) is the exhaustive,
hand-maintained list of which env-var names are operational (provider, model,
usage caps, cooldown tuning, etc.) rather than credentials — everything not on
that list is a secret by default.

`.env.config` is safe to open and edit directly (by a human or an agent) —
unlike `.env`, it never mixes in credential material, so none of CLAUDE.md's
"never open a file that mixes secrets" restrictions apply to it.

**Migrating an existing `.env`:**

1. Copy `.env.config.example` to `.env.config` and fill in the values
   currently sitting in `.env` for every name on `OPERATIONAL_KEYS`.
2. If you previously set `LLM_MODEL` to a Vertex-specific model while testing
   with `LLM_PROVIDER=vertex` (see section 2 above — a prior live Vertex
   verification ran with `LLM_MODEL=gemini-2.5-flash`, before `VERTEX_MODEL`
   existed as its own var) that value belongs in `VERTEX_MODEL`, not
   `LLM_MODEL` — check which one you're carrying forward, and restore
   `LLM_MODEL` to a gemini model (e.g. `gemini-flash-latest`) if needed.
   Carrying a Vertex model into gemini's `LLM_MODEL` would make gemini call
   AI-Studio with a Vertex-specific model ID (which costs money if that ID
   happens to also exist in AI-Studio's catalog, then hits a pricing
   `KeyError` once gemini+that model turns out not to be in the rate table),
   and `--sync-env` pushes `LLM_MODEL` unconditionally, so the wrong value
   would propagate to the deployed service too.
3. Remove those same keys from `.env`.

**This order matters**: `.env.config` wins by precedence, so creating it
first and removing the old keys second means there is never a window where a
setting reads as unset. Doing it in the other order would.

`tests/test_config.py::test_no_operational_key_lives_in_the_secrets_file`
enforces the split going forward — it fails, naming every misplaced key, if
an `OPERATIONAL_KEYS` name is still found in `.env` (or if a key that is
*not* on `OPERATIONAL_KEYS` is found in `.env.config`, catching drift in the
other direction). Green on that test is how you confirm the migration above
is complete.

`KEY_USAGE_TOKEN_CAP`, `KEY_USAGE_COST_CAP_USD`, and `KEY_USAGE_RESET_TIME_UTC`
are declared in `render.yaml` (a dashboard-set baseline) but are **never**
pushed by `uv run python -m scripts.deploy --sync-env` — exactly like the
`DISPATCHER_REREVIEW_COOLDOWN_*` cooldown vars, the live-change path for these
is `uv run python -m scripts.set_usage_cap` (see README's "Changing
operational config"), not a redeploy.

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
| **#4** | **First hosted run** (Render + Supabase, 2026-08-07) — happy path, `seed_demo_pr.py` against the deployed service | **~9.2s** PR-created → comment-appeared, real findings via groq; `tickets` created by the app's own first boot against a real Supabase project (see `docs/2026-08-05-first-hosted-run-findings.md`) |
| **#5** | **Hosted Segment B** — `LLM_PROVIDER=github_models` redeploy (real 2026-07-30 retirement) → all 3 specialists fail visibly → `groq` redeploy → follow-up commit → same comment updates in place | Redeploys **65.5s** / **56.7s** (not a 2s local restart); ticket survived both restarts intact |
| **#6-#9** | **Hosted Segment C** — 4 new PRs + a follow-up commit fired in quick succession under `groq` | **No 429 observed** (see findings doc — current Groq headroom exceeds the 2026-08-03 token-math measurement; not retried, per CLAUDE.md hygiene rules) |

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
