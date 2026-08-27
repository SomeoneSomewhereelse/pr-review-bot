# Design — Self-service onboarding wizard: LLM provider credential frame

**Date:** 2026-08-27
**Status:** Approved for planning
**Relates to:** `onboarding/` (shipped in sub-projects 1-3, see
`docs/superpowers/specs/2026-08-26-onboarding-wizard-render-frame-design.md`,
`docs/superpowers/specs/2026-08-26-onboarding-github-app-frame-design.md`,
`docs/superpowers/specs/2026-08-26-onboarding-supabase-provisioning-frame-design.md`
— this frame reuses their stateless-relay architecture, `sessionStorage`
credential custody, and accordion lock/unlock state machine),
`guide/setup/04-llm-provider.md` (the manual process this eventually
replaces), `app/config.py` and `app/providers/` (the target shape these
visitor-supplied credentials need to end up in on the deployed service).

## 1. Problem and context

This is the **fourth of six sub-projects** decomposing the self-service
onboarding wizard (full decomposition recorded in the sub-project 1 spec).
Sub-projects 1-3 shipped the accordion shell, Render-key capture, GitHub App
automation, and Supabase provisioning. This spec covers frame 4: replacing
`guide/setup/04-llm-provider.md`'s manual flow (pick one of three providers,
hand-edit `.env`/`.env.config`, run `scripts/doctor.py` to confirm) with an
in-wizard credential capture that validates itself.

Unlike sub-projects 2 and 3, this frame introduces **no new operator-level
secret and no delegated-auth API** — every credential here is visitor-typed
or visitor-uploaded, matching the "plain key-paste accepted where no
delegated-auth API exists" branch of sub-project 1's Automation-depth
decision. The interesting design surface instead is: (a) three genuinely
different credential shapes behind one frame (two plain API-key pastes and
one file-picker-to-base64), and (b) making sure the *model* this deployment
ends up running is one the submitted credential can actually reach — this
project's own history has a live incident on exactly that gap (root
`CLAUDE.md`'s substitutions section: `gemini-flash-latest` 404s against
Vertex's publisher-model catalog, which is why `vertex_model` is pinned
separately from `llm_model`).

## 2. Confirmed decisions (this sub-project)

| Decision | Choice |
|---|---|
| Provider selection | Single-choice 3-way selector: Gemini / Groq / Vertex — matches `app/config.py`'s `llm_provider` being a single string, not a set. |
| Validation depth | Live call required, not client-side shape-checks only. The call is a models-*listing* call, which doubles as both auth validation and model discovery in one round trip — no separate "just check the key" call. |
| Model source | **Fetched live from the provider's own catalog**, not a hardcoded default. Rejected hardcoding the three current `app/config.py` defaults (`gemini-flash-latest` / `llama-3.3-70b-versatile` / `gemini-2.5-flash`) as onboarding-local constants specifically because that list can silently drift from `app/config.py`'s real defaults, and because a hardcoded single target model can't detect the actual documented failure mode (right key, wrong model for *this* project/region). |
| Frame unlock gate | **Both required**: a live-validated credential AND an explicit model pick from the fetched dropdown. Rejected "credential valid is enough, model optional, fall back to `app/config.py`'s baked-in default" — baked-in defaults are exactly the fragile assumption this sub-project exists to stop relying on. |
| Empty-catalog handling | A credential that validates but returns zero eligible models is a genuine dead end under the "both required" gate (no fallback exists). Surfaced as its own distinct message ("this credential is valid, but no usable models are available") rather than folded into a generic validation-failure error, so the visitor knows the credential itself is fine and the problem is elsewhere (wrong region, wrong GCP project, no models enabled). |
| Gemini/Vertex model filtering | Filtered to models whose `supported_actions` includes `generateContent` (the SDK call `app/providers/google_genai.py` actually makes) *when that field is populated*. **Correction, found at final review (2026-08-27):** `google-genai`'s `Model` type carries the field, but only the Gemini Developer API's response converter (`_Model_from_mldev`) populates it — Vertex's converter (`_Model_from_vertex`) never does, verified directly against the installed SDK's source. A Vertex model with `supported_actions=None` is let through rather than dropped; the original implementation filtered strictly on the field and silently emptied Vertex's entire catalog for every credential. |
| Groq model filtering | **Unfiltered** — shows every model Groq's `/v1/models` returns, including non-chat models (Whisper, TTS, guard/moderation) that would break `app/providers/groq.py`'s `chat.completions.create()` call if picked. Groq's `Model` type (`id`, `created`, `object`, `owned_by`) carries no capability field to filter by; a name-pattern heuristic (excluding ids containing "whisper"/"tts"/"guard") was considered and rejected as exactly the kind of hardcoded assumption about a live API's exact behavior this project's testing-hygiene discipline warns against. A wrong pick is a fast fix later via `scripts/set_override.py`, no redeploy. |
| Vertex GCP project | Auto-derived from the uploaded service-account JSON's own `project_id` field — no visitor typing. |
| Vertex GCP location | Fixed to `us-central1`, matching `app/config.py`'s `gcp_location` default — no visitor choice, same fixed-region posture as Supabase's frame. |

## 3. Architecture and data flow

`onboarding/llm_client.py` (new) handles all three providers' listing calls
— via the **official SDKs** (`google-genai`, `groq`), not raw `httpx`,
since both are already project dependencies (`app/providers/`'s own
integration layer) and re-implementing their request/auth/retry handling by
hand in `httpx` would duplicate what the SDKs already do correctly. This is
a deliberate divergence from `render_client.py`/`github_client.py`/
`supabase_client.py`'s raw-`httpx` shape — justified because, unlike those
three services, no raw REST contract needs to be hand-verified here: the
SDKs' own typed `Model`/exception classes already encode it (verified by
reading their source directly — see section 6 on testing implications of
this choice).

No new `onboarding/config.py` settings, no `onboarding/main.py` lifespan
changes — every credential is visitor-supplied per request, the same
territory as the Render frame, not the Supabase frame's operator-level
OAuth secret.

Gemini and Vertex share one internal helper (`_list_generative_models`)
that both public functions call, differing only in how the `genai.Client`
is constructed:

- **Gemini**: `genai.Client(api_key=api_key)`.
- **Vertex**: decode+parse the submitted base64 JSON, extract `project_id`,
  build `google.oauth2.service_account.Credentials.from_service_account_info(data, scopes=["https://www.googleapis.com/auth/cloud-platform"])`
  (the exact pattern `app/providers/google_genai.py`'s `VertexProvider`
  already uses), then `genai.Client(vertexai=True, project=project_id, location="us-central1", credentials=creds)`.

Both then call `await client.aio.models.list()`, filter the returned
`Model` objects to those whose `supported_actions` either contains
`"generateContent"` or is unset (Vertex's converter never populates that
field — a strict filter there emptied the whole catalog; see section 2's
correction), and strip the resource-name prefix (`"models/"` for Gemini,
`"publishers/google/models/"` for Vertex) down to the plain model-id string
that `app/config.py`'s `llm_model`/`vertex_model` fields expect.

**Groq**: `groq.AsyncGroq(api_key=api_key)`, `await client.models.list()` →
`ModelListResponse(data=[Model(id, created, object, owned_by)], object="list")`
— `id` values are already plain strings, no prefix to strip, no filtering
(section 2).

**Flow:**

1. Visitor opens frame 4, picks a provider via the 3-way selector, which
   reveals that provider's own input: a text field (Gemini/Groq) or a file
   picker (Vertex).
2. **Gemini/Groq**: visitor pastes their API key.
   **Vertex**: visitor selects their GCP service-account JSON file; the
   browser reads it via `FileReader`, does a client-side `JSON.parse`
   sanity check (catches "wrong file entirely" before any network call),
   and base64-encodes the raw file content — same shape as the GitHub
   frame's `private_key_b64` handling, never uploaded as a raw file.
3. Visitor clicks "Validate & fetch models". Browser calls the
   provider-specific relay endpoint (section 4).
4. Backend makes the one live listing call, which is simultaneously the
   validation. Success returns `{valid: true, models: [...]}`
   (Vertex also echoes `project_id`, extracted server-side, non-secret,
   needed by the frame to display/store it). Failure returns
   `{valid: false, reason}`.
5. On success with a non-empty list: browser populates a dropdown, visitor
   must pick one model.
   On success with an empty list: browser shows the dedicated
   "valid credential, no usable models" message (section 2) — frame stays
   locked open, no dropdown to interact with.
   On failure: browser shows the reason-mapped error message.
6. Visitor picks a model, clicks "Continue". Browser stores
   `{provider, api_key?, gcp_service_account_key_b64?, gcp_project?, model}`
   in `sessionStorage` (only the fields relevant to the chosen provider are
   present) and completes/locks the frame — reusing sub-project 1's
   existing `completeFrame`/`lockFrame`/`beginChange` state machine
   unmodified; an explicit "Change" action re-opens the frame exactly as it
   does for every other frame.

## 4. API contract

| Endpoint | Request body | Response | Notes |
|---|---|---|---|
| `POST /api/llm/gemini/list-models` | `{api_key}` | `{valid: true, models: [str, ...]}` or `{valid: false, reason}` | Filtered to `generateContent`-capable models, `"models/"` prefix stripped |
| `POST /api/llm/groq/list-models` | `{api_key}` | `{valid: true, models: [str, ...]}` or `{valid: false, reason}` | Unfiltered — section 2 |
| `POST /api/llm/vertex/list-models` | `{service_account_key_b64}` | `{valid: true, project_id, models: [str, ...]}` or `{valid: false, reason}` | `project_id` extracted server-side, non-secret, echoed for the frame's own display/storage use; location fixed `us-central1`; same filtering/prefix-stripping as Gemini |

Error `reason` vocabulary, derived from each SDK's own typed exceptions
(verified against SDK source in section 6 of this brainstorm, not
guessed): `unauthorized` (`google.genai.errors.ClientError` code 401 /
`groq.AuthenticationError`), `forbidden` (`ClientError` code 403 /
`groq.PermissionDeniedError` — Vertex's most likely real-world case:
service account missing the Vertex AI IAM role), `rate_limited`
(`ClientError`/`groq.RateLimitError` code 429), `provider_unreachable`
(`google.genai.errors.ServerError` / `groq.InternalServerError` /
`groq.APIConnectionError` / any network-level failure), and
`invalid_service_account_json` (Vertex only — the submitted base64 doesn't
decode to valid JSON, or lacks a `project_id` field; caught before any live
call is attempted, so it never reaches the SDK at all).

Each new relay call gets its own `..._leaves_the_page_exactly_once` test
per `onboarding/CLAUDE.md`'s existing convention. No CSP change needed —
this frame has no full-page navigation or form POST, only `fetch()` calls.

## 5. Credential handling summary

| Value | Origin | Custody |
|---|---|---|
| `api_key` (Gemini/Groq) | Visitor-typed | `sessionStorage`, relayed per-request, never logged |
| `gcp_service_account_key_b64` (Vertex) | Visitor-uploaded file, base64'd client-side | `sessionStorage`, relayed per-request, never logged |
| `gcp_project` | Extracted server-side from the decoded key's own `project_id` field | Non-secret, echoed back, stored alongside the credential in `sessionStorage` |
| `model` | Selected by the visitor from a live-fetched, per-credential list | Stored in `sessionStorage` |

No mint-and-return exceptions in this sub-project — unlike sub-projects 2
and 3, nothing here is freshly issued by the backend; every value is either
visitor-supplied or a non-secret field extracted from what the visitor
already submitted.

## 6. Testing strategy

**Groq**: the `groq` SDK's transport is pure `httpx` (verified by reading
`groq/_base_client.py`'s imports directly) — `respx` can mock it the same
way `render_client.py`/`supabase_client.py`'s tests already do.

**Gemini/Vertex**: the async listing call itself is `httpx`-based for both
providers (verified: this environment has no `aiohttp` installed, so
`google-genai`'s async path falls back to `httpx` regardless of auth type).
What a single `respx` mock can't cover is Vertex's separate credential
step: a service-account refreshes its access token via `google.auth`'s
synchronous, `requests`-based transport
(`google.auth.transport.requests.AuthorizedSession`) before the `httpx`
listing call ever happens, and `respx` only intercepts `httpx`. Tests mock
at the SDK client boundary instead (`unittest.mock` patching
`genai.Client`/its `.aio.models.list` — or the thin
`onboarding/llm_client.py` wrapper functions directly), the same category
of workaround `github_client.py`'s `verify_installation` already needed
for PyGithub's `requests`-based transport, for the same underlying reason
(an SDK that isn't pure `httpx` under the hood).

Per root `CLAUDE.md`'s LLM-API-testing-hygiene section, no test in this
suite makes a real network call to Gemini, Groq, or Vertex — every provider
call is mocked. Browser-side tests cover: provider-selector show/hide
logic, the Vertex file-picker's client-side JSON sanity check and
base64 encoding, the model dropdown's populate/empty-state/disabled-until-
picked behavior, and the "both required" unlock gate (credential alone
does not unlock; model alone is impossible since the dropdown only exists
after a successful credential validation).

## 7. Module layout

```
onboarding/llm_client.py       NEW — list_gemini_models, list_groq_models,
                                 list_vertex_models
onboarding/router.py           MODIFIED — 3 new endpoints (table in section 4)
onboarding/static/index.html   MODIFIED — frame 4 markup + JS (provider
                                 selector, file picker, model dropdown)
onboarding/CLAUDE.md           MODIFIED — "what sub-project 4 adds" section
```

`pyproject.toml` is unchanged — `google-genai` and `groq` are already
project dependencies (`app/providers/`'s own integration layer).

## 8. Out of scope

- Pushing the captured provider/credential/model into Render's env vars —
  sub-project 6's job, same boundary sub-projects 2 and 3 drew for their
  own credentials.
- Model pricing display (`guide/setup/04-llm-provider.md`'s existing
  "Model pricing is optional" framing carries over unchanged — no cost
  estimate shown in this frame).
- Best-effort name-pattern filtering of Groq's non-chat models (section 2 —
  deliberately rejected).
- Re-validating a previously-picked model if the provider deprecates it
  after the frame is already complete — the existing "Change" action
  already covers on-demand re-validation, no proactive check is added.
- GCP IAM/permissions troubleshooting guidance beyond the `forbidden`
  reason surface — matches the Supabase frame's precedent of surfacing a
  structured reason rather than diagnosing the visitor's GCP project for
  them.
