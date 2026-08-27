# Design — Onboarding wizard: Render service creation + env-var sync + deploy (sub-project 6, final)

**Date:** 2026-08-27
**Status:** Approved for planning
**Relates to:** `onboarding/` (sub-projects 1-5, all merged), `scripts/deploy.py`
(the human-run CLI this generalizes — `_render.py`'s helpers, `_wanted_env()`'s
provider-derived var set, `_trigger_and_wait()`'s deploy-trigger/poll shape),
`render.yaml` (the env-var manifest this pushes the same names into),
`guide/setup/hosted/06-render.md`/`07-sync.md` (the manual two-step process
this automates), `docs/superpowers/specs/2026-08-26-onboarding-github-app-
frame-design.md` section 2 (recorded the original "push immediately, reorder
after frame 1" commitment this spec now formally supersedes with a revised
mechanism — see section 2 below).

## 1. Problem and context

This is the **sixth and final sub-project** of the onboarding wizard
(decomposition: `docs/superpowers/specs/2026-08-26-onboarding-wizard-render-
frame-design.md` section 1). Sub-projects 1-5 each capture and validate one
credential, holding it in `sessionStorage` — none of it has ever reached
Render. This sub-project is the payoff: create the visitor's own Render web
service, push everything sub-projects 2-5 collected into its env vars, and
deploy it to a live, working instance.

Two things make this more than "add one more frame":

**Frame 5 (UptimeRobot) is currently non-functional.** Its design
(`2026-08-27-onboarding-uptimerobot-frame-design.md`) reads a `sessionStorage`
key, `onboarding.renderServiceUrl`, that only this sub-project can write —
and in the shipped frame order, frame 5 runs *before* where frame 6 would
land. A visitor reaching frame 5 today sees a permanent "complete your Render
deployment first" blocked message with no way past it. Making frame 5 usable
requires this sub-project to run — and produce a service URL — before frame
5, not after it.

**An earlier spec recorded a specific mechanism for that reorder that was
never fully committed to.** Sub-project 2's design doc (section 2) floated
"sub-project 6 will create the Render service immediately after frame 1
... and every later frame will push its own captured secret into that
service the moment it validates" — but sub-projects 3, 4, and 5 each
subsequently treated "push to Render" as simply out of scope, deferred
whole to this sub-project, without retrofitting anything. This spec
resolves that ambiguity: it adopts the substance of that original
commitment (push-and-clear immediately per frame, service created early)
but implements it as **two separate frames** rather than one relocated
frame, for reasons covered in section 3.

## 2. Confirmed decisions

| Decision | Choice |
|---|---|
| Reorder mechanism | **Split what was "sub-project 6" into two frames.** A new "Render service" frame runs right after frame 1 (creates the service, writes the URL). The existing reserved-but-placeholder `frame-render-deploy` (already last in `FRAME_ORDER` since sub-project 1's shell) becomes "Finish & Deploy" — triggers the real deploy once every credential has been pushed, and polls. |
| Credential residency | **Push-and-clear.** Frames 2 (GitHub), 3 (Supabase), 4 (LLM) each push their own env var(s) into the now-existing Render service the moment they validate, then clear the raw secret value from their own `sessionStorage` record — shrinking the window any one credential sits in the browser from "the rest of the wizard" to "this one frame." Frame 5 (UptimeRobot) is unaffected: its credential never goes to Render (see the env-var manifest, section 7). |
| Repo to deploy | The canonical upstream repo, `https://github.com/TovTechOrg/pr-review-bot`, used unmodified — verified live that Render's create-service API accepts a plain public-repo URL with no OAuth/account-linking step. Editable via a text input the visitor can override, defaulting to the upstream URL. |
| Service naming | Auto-generated client-side (`pr-review-bot-<8 random hex chars>`), editable via a text input the visitor can override. The URL actually used is derived from Render's own returned `slug`, never the submitted name — Render may normalize it server-side. |
| Env vars at creation | **None** — the created service starts exactly like the guide's own Blueprint flow (every var blank, first deploy expected to fail). Frames 2-4 fill it in afterward. |
| Deploy trigger + poll pattern | Mirrors Supabase's existing project-status polling (frame 3): a trigger call returns immediately with a `deploy_id`; the browser then polls a separate stateless status endpoint every 10s for up to 15 minutes, with a manual "Check again" fallback on timeout — never a single long-blocking backend call. Interval/timeout are longer than Supabase's 5s/5min since a Docker build runs longer than Supabase provisioning (`scripts/deploy.py`'s own `_DEPLOY_TIMEOUT_SECONDS = 900` is the same order of magnitude). |
| Resubmit/orphan handling | Same accepted limitation as frame 2's GitHub App creation: resubmitting the "Render service" frame (via "Change") creates a **new** Render service; the old one is orphaned on the visitor's account until they delete it themselves. No dedupe-by-name check — Render's own name-collision error is the only signal, surfaced generically (see section 6). |
| Push failure handling | Pushing a frame's credential to Render is a **best-effort persistence step, never a gate** on that frame's own success. If the Render-service frame was never completed (or the push itself fails), frames 2-4 still complete normally from the visitor's perspective — only the final "Finish & Deploy" frame is blocked without a real service to deploy. |
| `GITHUB_TARGET_REPO` / `GCP_PROJECT` / `GCP_LOCATION` | **Never pushed.** Track-all mode is the correct default for the first two (GitHub's own installation repo-selection already narrows scope when relevant); `GCP_PROJECT` is derivable from the service-account key and `GCP_LOCATION`'s `app/config.py` default (`us-central1`) already matches `onboarding/llm_client.py`'s own fixed constant — verified identical, confirmed no drift risk worth guarding against by pushing a redundant value. |

## 3. Why two frames, not one relocated frame

The original "immediately after frame 1" commitment, read literally, would
mean position 2 holds a single frame that both creates the service *and*
later triggers the final deploy — but frames 2-4 haven't run yet when
position 2 first completes, so there's nothing to deploy yet. The only way
to honor "create early, push incrementally" without inventing a mechanism
for one frame's UI to reappear at the end (breaking the accordion's
sequential-lock model every other frame follows) is to split creation and
finalization into two distinct, single-purpose frames — which also mirrors
the guide's own two-step manual process almost exactly: `06-render.md`
("create with everything blank, expect the first deploy to fail") is now
the "Render service" frame; `07-sync.md` ("push real values, this triggers
the real deploy") is now spread across frames 2-4's push step plus the
"Finish & Deploy" frame's trigger.

## 4. Architecture

```
onboarding/render_client.py   MODIFIED — + create_service, push_env_var,
                                trigger_deploy, poll_deploy_status
onboarding/router.py          MODIFIED — 6 new endpoints (section 6)
onboarding/static/index.html  MODIFIED — FRAME_ORDER gains "render-service";
                                new frame-render-service markup; frame-
                                render-deploy markup built out from its
                                placeholder; frames 2/3/4 each get a push-
                                and-clear step added after their existing
                                completion logic
onboarding/CLAUDE.md          MODIFIED — "what sub-project 6 adds" section
```

No new backend module: this is small enough, and similar enough in shape
(dataclass results, narrow `httpx` exception handling, no persistence), to
extend `render_client.py` rather than create a new one — it already owns
every Render-API concern this wizard has.

## 5. Data flow

### 5a. Frame "Render service" (position 2, right after the Render-key frame)

1. Visitor sees two pre-filled, editable fields: repo URL (default: the
   upstream repo) and service name (default: `pr-review-bot-<8 hex>`,
   generated via `crypto.getRandomValues`, matching frame 3's existing
   `db_pass` generation pattern for client-side randomness).
2. Submits → `POST /api/render/create-service {api_key, repo_url, name}`.
3. Backend calls `GET /owners` (same call `validate_key` already makes) to
   resolve `ownerId`, then `POST /v1/services` with `type: "web_service"`,
   `runtime: docker`, `plan: free`, `autoDeploy: "yes"`, `healthCheckPath:
   /healthz`, `envSpecificDetails.dockerfilePath: ./Dockerfile`,
   `buildFilter.ignoredPaths: ["**/*.md"]` (matching `render.yaml`'s own
   settings exactly, so a visitor's instance behaves identically to the
   project's own production deploy), no `envVars`.
4. On success: `{"valid": true, "service_id": "srv-...", "service_url":
   "https://<slug>.onrender.com"}` — the URL is built from the response's
   own `service.slug` field, never the submitted `name` (Render may
   normalize it). On failure: `{"valid": false, "reason": "invalid_key" |
   "render_unreachable" | "request_rejected", "message"?: "..."}` —
   `request_rejected` (any other 4xx, e.g. a name collision) relays
   Render's own `message` text verbatim, the same pattern
   `create-project`'s `project_creation_rejected` already uses for
   Supabase, rather than guessing a specific reason enum for a business
   rule this project has not verified the exact wording of.
5. Browser stores `{service_id, service_url}` under
   `sessionStorage["onboarding.renderService"]` and **writes
   `sessionStorage["onboarding.renderServiceUrl"] = service_url`** —
   satisfying frame 5's forward contract. Frame locks.

### 5b. Frames 2 (GitHub), 3 (Supabase), 4 (LLM) — the push-and-clear addition

Each frame's *existing* validation/completion logic (unchanged) is followed
by one new step, only if `onboarding.renderService` holds a `service_id`
(if the Render-service frame was skipped or failed, this step is skipped
entirely — see the push-failure-handling decision in section 2):

6. Browser POSTs the frame's own new push endpoint (section 6) with
   `{render_api_key: <from onboarding.renderApiKey>, render_service_id,
   ...this frame's own already-validated values...}`.
7. Backend loops `PUT /v1/services/{id}/env-vars/{key}` once per var this
   frame owns (never the bulk `PUT /v1/services/{id}/env-vars`, which
   replaces the whole list — same reasoning `scripts/deploy.py::sync_env()`
   already documents). Returns `{"valid": true, "pushed": [...key names...]}`
   on full success, or `{"valid": false, "reason": ..., "pushed": [...keys
   that succeeded before the failure...]}` on a partial failure — the
   partial list is never silently dropped, though the frame's own
   completion is not gated on it (section 2).
8. On any outcome (even a push failure), the frame **clears the raw secret
   value(s)** it just pushed from its own `sessionStorage` record, keeping
   only the non-secret display bits already shown (e.g. GitHub's
   `account_login`, Supabase's project ref) — the push attempt, not its
   success, is what triggers the clear, since a value that failed to reach
   Render is not more safely held in the browser either; the fix for a
   failed push is re-running the frame via "Change", which re-collects the
   credential from the visitor, not resuming from a stale browser copy.

### 5c. Frame "Finish & Deploy" (`frame-render-deploy`, last)

9. Unlocks once frame 5 (UptimeRobot) completes. Shows a summary + one
   action button.
10. Click → `POST /api/render/trigger-deploy {api_key, service_id}` →
    backend calls `POST /v1/services/{id}/deploys` (empty body, matching
    `scripts/deploy.py::_trigger_and_wait`) → returns `{"valid": true,
    "deploy_id": "dep-..."}` immediately (never blocks on the deploy
    itself).
11. Browser polls `POST /api/render/deploy-status {api_key, service_id,
    deploy_id}` every 10s, up to 15 minutes, with "Check again" on
    timeout — same shape as frame 3's existing polling loop. Each poll:
    backend calls `GET /v1/services/{id}/deploys/{deploy_id}`, maps
    Render's status into `"in_progress"` (any of `_DEPLOY_IN_FLIGHT_
    STATUSES`), `"live"`, `"canceled"` (a superseding deploy, not a
    failure — same distinction `deploy.py` already draws), or `"failed"`
    (any of `_DEPLOY_FAILED_STATUSES`) — reusing the exact status-bucket
    sets `scripts/deploy.py` already defines, so the two can never
    describe Render's deploy states differently.
12. On `"live"`: frame shows the final service URL and a wizard-complete
    state. On `"failed"`: an error naming that the deploy failed, with a
    pointer to check the Render dashboard (this wizard has no log-reading
    capability, and inventing one is out of scope — section 9).

## 6. API contract

| Endpoint | Request | Success response | Failure `reason` values |
|---|---|---|---|
| `POST /api/render/create-service` | `{api_key, repo_url, name}` | `{valid: true, service_id, service_url}` | `invalid_key`, `render_unreachable`, `request_rejected` (+ `message`) |
| `POST /api/github/push-render-vars` | `{render_api_key, render_service_id, app_id, private_key_b64, webhook_secret, installation_id}` | `{valid: true, pushed: [...]}` | `invalid_key`, `service_not_found`, `render_unreachable` (+ `pushed`: partial list) |
| `POST /api/supabase/push-render-var` | `{render_api_key, render_service_id, database_url}` | `{valid: true, pushed: [...]}` | same as above |
| `POST /api/llm/push-render-vars` | `{render_api_key, render_service_id, provider, credential_value, model}` | `{valid: true, pushed: [...]}` | same as above |
| `POST /api/render/trigger-deploy` | `{api_key, service_id}` | `{valid: true, deploy_id}` | `invalid_key`, `service_not_found`, `render_unreachable` |
| `POST /api/render/deploy-status` | `{api_key, service_id, deploy_id}` | `{valid: true, status}` | `invalid_key`, `service_not_found`, `render_unreachable` |

Six distinct endpoint URLs, each called from exactly one frame's own
`fetch()` call site — keeps the existing `..._leaves_the_page_exactly_once`
per-endpoint audit convention intact with no adaptation needed (unlike a
single shared push endpoint, which would break the page-wide `== 1` count
across three different frames' credentials).

`push-render-vars`'s `render_api_key` field is **not a new credential
capture** — every one of these calls reads the already-validated value out
of `onboarding.renderApiKey` (frame 1) and `onboarding.renderService`
(the new frame), the same way frame 3's `callSupabaseRelay` already reuses
its own stored access token across six endpoints.

## 7. Env var manifest, by frame

| Frame | Vars pushed |
|---|---|
| GitHub (2) | `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY` (the `private_key_b64` value, verbatim — `app/github_app.py::_read_private_key()` already base64-decodes it, confirmed by reading the code, not assumed), `GITHUB_WEBHOOK_SECRET`, `GITHUB_APP_INSTALLATION_ID` |
| Supabase (3) | `DATABASE_URL` (the client-assembled connection string) |
| LLM (4) | `LLM_PROVIDER` + the selected provider's credential var + its model var only — e.g. `GEMINI_API_KEY`+`LLM_MODEL`, `GROQ_API_KEY`+`GROQ_MODEL`, or `GCP_SERVICE_ACCOUNT_KEY`+`VERTEX_MODEL` |
| UptimeRobot (5) | nothing — its key is relay-only for the monitor-creation call, never touches Render (unchanged from sub-project 5) |

This is a strict subset of `render.yaml`'s `envVars` list — every
operational/dispatcher-tuning var (`DISPATCHER_*`, `LLM_REQUEST_TIMEOUT_
SECONDS`, etc.) is left unset on a wizard-created service, same as the
manual guide's own Step 6/7 flow: the app's own field defaults apply, and
`scripts/deploy.py`'s existing `_GENERIC_OPERATIONAL_ENV_ATTRS` mechanism
remains the only way to change them later, unchanged by this sub-project.

## 8. Credential handling summary

| Value | Origin | Custody |
|---|---|---|
| `api_key` (Render) | Visitor-typed (frame 1) | `sessionStorage`, read repeatedly by every later frame's push/trigger/poll calls — never re-typed, never echoed back |
| GitHub App id/private key/webhook secret/installation id | Minted/verified by frame 2 | Held only until frame 2's own push-and-clear step succeeds or fails, then cleared |
| `DATABASE_URL` | Assembled client-side by frame 3 | Same push-and-clear treatment |
| LLM provider credential | Visitor-typed/frame-4-validated | Same push-and-clear treatment |
| `service_id` / `service_url` | Minted by the Render-service frame | `sessionStorage`, non-secret (a service id and public URL are not credentials), held for the rest of the wizard's duration since later frames need it |

## 9. Error handling

- Every new `render_client.py` function follows the existing `validate_key`
  shape: narrow `httpx.HTTPError` catch mapped to `render_unreachable`, no
  bare `except Exception`.
- `push_env_var`'s per-key loop stops at the first failure (matching
  `sync_env()`'s own behavior) and reports which keys succeeded before it,
  rather than attempting to push remaining keys out of order.
- A deploy reported `"failed"` gets no automated diagnosis — this wizard
  cannot read Render's build logs (no such endpoint is used anywhere in
  this codebase today, and adding log-fetching is a new capability, not a
  small addition). The frame's error copy points the visitor at the
  Render dashboard directly (`service_url`'s dashboard is derivable, but
  linking to it is a nice-to-have, not required — see section 10).

## 10. Testing strategy

- `render_client.py`'s four new functions: `respx`-mocked `httpx`
  responses, mirroring the existing `validate_key` tests — valid/invalid
  key, 5xx, timeout, and (for `create_service`) a non-401/5xx 4xx to cover
  `request_rejected`. The `POST /v1/services` request/response shape
  itself was verified live during this design's brainstorm (not
  guessed from documentation alone) — a real free-plan Docker service was
  created against a throwaway public repo and immediately deleted; the
  response's field names (`service.id`, `service.slug`, `service.ownerId`,
  top-level `deployId`) and the *absence* of a `service.url` field are
  taken from that real response, not Render's docs alone. A second live
  call confirmed the *full* intended request body — `healthCheckPath` and
  `buildFilter.ignoredPaths` included, not just the minimal fields from the
  first call — is accepted as-is and echoed back correctly by the raw
  `POST /v1/services` API, not only by Render's separate Blueprint YAML
  parser (`render.yaml` already uses both fields via that different path;
  the two were not assumed to share a field shape without checking).
- `router.py`: endpoint tests against a mocked `render_client`, asserting
  the JSON contracts in section 6 and that no credential value ever
  appears in a response body.
- Frontend: extends the existing content-substring convention. New checks:
  `FRAME_ORDER` contains `"render-service"` immediately after `"render-
  key"` and `"render-deploy"` last; each of the 3 retrofitted frames'
  push-and-clear step has its own `..._leaves_the_page_exactly_once` test
  (6 new endpoints, 6 new tests, per section 6); a test that frame 5's
  blocked-state markup and check function are unchanged (this sub-project
  makes the blocked path unreachable in normal sequential flow, but does
  not remove or need to modify it — no test should assume it can no
  longer be reached, since a corrupted/partial `sessionStorage` state
  could still trigger it).
- No live Render API calls in the test suite itself — same "mocked in CI,
  live-verified once by hand during design" discipline every prior
  sub-project has followed.

## 11. `onboarding/CLAUDE.md` additions (to be appended)

```markdown
## What sub-project 6 (Render service creation + deploy, final) adds to these rules

- **What was originally decomposed as one "sub-project 6" frame is actually
  two frames**: "Render service" (position 2, right after the Render-key
  frame) creates the service; the pre-existing placeholder `frame-render-
  deploy` (reserved since sub-project 1's shell, always last in
  `FRAME_ORDER`) is "Finish & Deploy" — triggers the real deploy once
  frames 2-5 have run. Do not conflate them or try to merge them back into
  one frame; the accordion's sequential-lock model is why they're split
  (see design spec section 3).
- **Frames 2, 3, and 4 each push their own credential into the already-
  created Render service the moment they validate, then clear the raw
  value from their own `sessionStorage` record.** This is a deliberate
  security property (shrinking a credential's browser-residency window),
  not an optional optimization — do not defer a new frame's push to "do it
  all at the end" without a fresh brainstorm justifying the regression.
- **A push failure never blocks the pushing frame's own completion.**
  Pushing to Render is best-effort persistence; only the final "Finish &
  Deploy" frame is genuinely blocked by a missing service. See design spec
  section 2's push-failure-handling decision before changing this.
- **Six new relay endpoints, one frame each** — never route two different
  frames' pushes through one shared endpoint URL, even though the
  underlying `render_client.push_env_var` call is shared. A shared
  endpoint would break the per-endpoint `..._leaves_the_page_exactly_once`
  audit's page-wide `== 1` count across multiple frames (see design spec
  section 6).
- **The created service's public URL is always derived from Render's
  returned `service.slug`, never the submitted `name`.** Render may
  normalize the name server-side; a create-service response was verified
  live to have no `service.url` field at all — trusting the requested
  name instead of the response's slug would silently point
  `onboarding.renderServiceUrl` (frame 5's forward contract) at a URL
  that doesn't exist.
- **`GITHUB_TARGET_REPO`, `GCP_PROJECT`, and `GCP_LOCATION` are
  deliberately never pushed** — track-all mode and this project's own
  matching defaults make an explicit push redundant. Do not add them
  without a concrete reason a default has drifted.
- **Deploy status polling reuses `scripts/deploy.py`'s own
  `_DEPLOY_IN_FLIGHT_STATUSES`/`_DEPLOY_FAILED_STATUSES` status-bucket
  sets** (via a small shared import or a verbatim-kept-in-sync copy — a
  task-level decision, not a design-level one) — so the CLI's operator-
  facing deploy checks and this wizard's visitor-facing ones can never
  describe Render's own deploy states differently.
```

## 12. Out of scope

- Reading Render's build logs to diagnose a failed deploy — the frame
  reports failure and points at the Render dashboard; no in-wizard log
  viewer.
- Letting the visitor choose Render's region, plan tier (beyond free), or
  branch — all fixed (region: Render's default; plan: free; branch: main).
- Any mechanism to resume an abandoned wizard session, or to detect and
  clean up an orphaned Render service from a resubmitted "Render service"
  frame — same accepted limitation as frame 2's GitHub App creation.
- Retrying a failed individual env-var push automatically — the visitor's
  only recourse is re-running that frame via "Change", which is already
  the pattern every other frame uses for a validation failure.
- Removing or simplifying frame 5's now-normally-unreachable blocked-state
  code — it remains a correctness safeguard for a corrupted/partial
  session, not dead code to delete.
- A post-wizard "download your `.env`" affordance — ruled out in
  sub-project 2's design (section 2) and unchanged here: live Render
  provisioning is the wizard's only end state.
