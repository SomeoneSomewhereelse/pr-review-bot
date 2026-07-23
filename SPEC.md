# SPEC — Autonomous Code Review Engine (Project ד)

A server that listens for GitHub PR webhooks. When a PR is opened/reopened or
receives new commits, an **Orchestrator** fetches the diff and runs three
**Specialist Agents** (Security, Performance, Code Quality) in parallel, each
backed by an LLM call with a structured-output schema. The Orchestrator merges
their findings into a single Markdown comment and posts it back to the PR.
Runs in production (public URL via Cloudflare Tunnel), not localhost.

## Confirmed decisions

| Decision | Choice |
|---|---|
| Prod deploy | Docker container + **Cloudflare Tunnel** (portable to CF Container) |
| LLM providers | **Vertex (default) + free-Gemini + Groq** via `google-genai` |
| Model | **`gemini-flash-latest`** (brief's `gemini-2.5-flash` is deprecated; pinnable via env) |
| Structured output | Per-provider native schema + **shared Pydantic validate-repair** |
| PR triggers | **`opened` + `reopened` + `synchronize`** (edit comment in place) |
| GitHub auth | **GitHub App** (JWT → short-lived installation token) |
| Build order | **Security specialist end-to-end first**, then Performance + Quality |
| Webhook processing | Verify HMAC → **202 immediately** → background task runs review |
| Diff handling | Whole annotated diff per specialist + **token cap + visible truncation** |
| Replay defense | Dedup on `X-GitHub-Delivery` UUID (bounded in-memory LRU) |

---

## 1. Architecture overview

```
GitHub PR (opened / reopened / synchronize)
  └─▶ POST /webhook
        (1) read RAW body → verify HMAC-SHA256 (constant-time) → reject 401 if bad
        (2) dedup on X-GitHub-Delivery → 200 no-op if already seen
        (3) return 202 immediately  ← beats GitHub's ~10s ack timeout
        (4) BackgroundTask: run_review()
              ├─ GitHub App auth → installation token
              ├─ fetch PR unified diff (PyGitHub)
              ├─ annotate diff lines with file:line
              ├─ (cap + truncate if over token budget)
              ├─ asyncio.gather(security, performance, quality, return_exceptions=True)
              ├─ merge results (successes AND failures) + timing + token usage
              └─ find-or-create the bot comment (marker) → post/edit Markdown
```

**Why async is mandatory:** three LLM calls can exceed GitHub's ~10s webhook-ack
timeout; a slow synchronous handler causes GitHub to mark the delivery failed and
**redeliver**, triggering a duplicate review. The 15s target is measured to
*comment-appears*, not to HTTP response.

## 2. Module layout

```
app/
  main.py              FastAPI app + lifespan (provider factory, dedup cache); GET /healthz
  config.py            pydantic-settings — all env vars in one typed place
  webhook.py           /webhook route; HMAC dependency; delivery dedup; 202 + BackgroundTask
  hmac_verify.py       verify_signature(raw, header, secret) — hmac.compare_digest
  github_app.py        JWT (RS256) → installation token; fetch diff; find/create/edit comment
  orchestrator.py      prepare diff → fan out → merge into ReviewResult
  diff_utils.py        annotate diff with file:line; token cap + truncation
  formatting.py        ReviewResult → Markdown comment (with bot marker)
  specialists/
    base.py            Specialist protocol + shared run() (calls provider + validate-repair)
    schemas.py         Pydantic finding models + envelopes
    security.py        system prompt + SecurityFinding schema
    performance.py     system prompt + PerformanceFinding schema
    quality.py         system prompt + QualityFinding schema
  providers/
    base.py            LLMProvider protocol: async complete(system, user, schema) -> BaseModel
    google_genai.py    Vertex (vertexai=True) + Gemini (api_key) — one SDK, two clients
    groq.py            OpenAI-compatible client, constrained-decoding structured output
    factory.py         select provider by LLM_PROVIDER env
    validate.py        validate-and-repair (one repair retry → typed empty-with-error)
    pricing.py         per-provider/model rate table → est_cost_usd
tests/                 (section 8)
fixtures/
  bad_code/            planted issues: hardcoded credential, N+1 query, magic number
  webhook_payloads/    signed request fixtures (valid / invalid / replay)
  llm_cassettes/       recorded provider responses for deterministic E2E
scripts/
  seed_demo_pr.py      push fixtures/bad_code branch + open a real PR (the demo)
Dockerfile
pyproject.toml         (uv-managed)
.github/workflows/ci.yml   ruff lint + pytest (deterministic test layers 1–6) on push/PR
SETUP.md                   prerequisites checklist produced by Step 0 (guided setup)
```

**Rule:** each module has one purpose and a narrow interface — the orchestrator
knows nothing about provider internals; specialists know nothing about GitHub;
formatting knows nothing about LLMs.

## 3. Data model (Pydantic)

Field names match the brief exactly, plus a `file` field so the comment can render
`file:line` without a second round trip.

```python
# specialists/schemas.py
Severity = Literal["critical", "high", "medium"]

class SecurityFinding(BaseModel):
    severity: Severity
    file: str
    line: int
    description: str
    fix: str

class PerformanceFinding(BaseModel):
    type: str                 # e.g. "N+1", "missing-cache", "blocking-io"
    estimated_impact: str     # e.g. "high", "~200ms/req"
    file: str
    line: int
    suggestion: str

class QualityFinding(BaseModel):
    category: str             # e.g. "duplication", "naming", "magic-number"
    file: str
    line: int
    issue: str
    refactoring_suggestion: str

class SpecialistResult(BaseModel):
    name: Literal["Security", "Performance", "Code Quality"]
    status: Literal["ok", "failed"]
    findings: list[dict] = []          # serialized findings of the specialist's type
    error: str | None = None
    elapsed_ms: int
    tokens_in: int = 0
    tokens_out: int = 0

class ReviewResult(BaseModel):
    pr_number: int
    provider: str                      # active LLM_PROVIDER
    model: str
    results: list[SpecialistResult]
    total_elapsed_ms: int
    total_tokens_in: int
    total_tokens_out: int
    est_cost_usd: float
```

The `SpecialistResult` envelope is the **minimum each specialist must carry** so
the comment (section 6) renders fully with no extra GitHub/LLM calls.

## 4. Provider abstraction

```python
# providers/base.py
class LLMProvider(Protocol):
    async def complete(self, system: str, user: str, schema: type[BaseModel]) -> BaseModel: ...
```

- **`vertex`** (default): `genai.Client(vertexai=True, project=..., location=...)`,
  model `gemini-flash-latest` (pinnable via env),
  `config={"response_schema": schema, "response_mime_type": "application/json"}`.
  Free on the $300 GCP trial credit.
- **`gemini`**: `genai.Client(api_key=GEMINI_API_KEY)` — otherwise **identical call**.
  AI-Studio permanent free tier (~1,500 req/day). Zero logic difference from vertex.
- **`groq`**: OpenAI-compatible client; structured outputs via constrained decoding.
  Different vendor + model (Llama) → demonstrates true provider-agnosticism.

`factory.py` selects by `LLM_PROVIDER`. `validate.py` sits above all providers:
validate returned JSON against the schema; on failure, one repair retry ("return
ONLY valid JSON matching this schema"); if still bad, return a typed empty result
and mark the specialist failed. This handles the "malformed/off-schema model
output" case centrally.

**SDK substitution:** the brief names the legacy `vertexai.generative_models` SDK;
we use the current unified `google-genai` (same Vertex backend) because it is what
makes the one-env-var swap between Vertex and AI-Studio trivial. The brief's
`gemini-2.5-flash` is deprecated/removed, so the default model is the alias
`gemini-flash-latest` (currently Gemini 3.5 Flash), pinnable to a dated version.

## 5. Orchestrator + specialists

- **Diff prep** (`diff_utils.py`): fetch unified diff, annotate each added/changed
  line with `file:line`, so specialist `line` outputs are trustworthy. Enforce a
  token budget; if exceeded, truncate and set a `truncated` flag surfaced in the comment.
- **Fan-out**: `await asyncio.gather(sec.run(d), perf.run(d), qual.run(d), return_exceptions=True)`.
- **Merge**: for each result, if it's an Exception, wrap into
  `SpecialistResult(status="failed", error=str(e))`; else the specialist's own
  `SpecialistResult(status="ok", ...)`. A single specialist failing **cannot** drop
  the others or blank the comment.
- Each specialist = same `run()` shape, differing only in **system prompt** +
  **schema**. Each records its own `elapsed_ms`, `tokens_in`, `tokens_out`.

## 6. GitHub PR comment format

Posted as **one issue comment**, edited in place on `synchronize` (found via a
hidden marker). Failed specialists render a **visible** row — never silently dropped.

```markdown
<!-- ai-code-review-bot -->
## 🤖 Automated Code Review — PR #42
_3 specialists · gemini-flash-latest (vertex) · 11.4s · ~$0.0021_

### 🔒 Security — 2 findings
| Severity | Line | Issue | Suggested fix |
|----------|------|-------|---------------|
| 🔴 critical | `app.py:14` | Hardcoded API key | Move to env var / secrets manager |
| 🟠 high | `db.py:88` | Unsanitized input → SQL injection | Use parameterized query |

### ⚡ Performance — 1 finding
| Impact | Line | Issue | Suggestion |
|--------|------|-------|------------|
| 🟠 high | `views.py:52` | N+1 query in loop | `select_related()` / batch fetch |

### 🧹 Code Quality — ✅ no findings

### ❌ Performance check failed
> `DeadlineExceeded` — other checks completed normally.

---
<sub>Runtime 11.4s · 4,910 tok in / 780 tok out · est. $0.0021 · provider: vertex</sub>
```

Footer runtime + cost come straight from the `ReviewResult`: providers return usage
metadata (Vertex/Gemini/Groq all expose it); `pricing.py` maps tokens × active-provider
rate → `est_cost_usd`.

## 7. HMAC webhook validation

- Secret in `GITHUB_WEBHOOK_SECRET` (container secret) — never in code, never logged.
- **Read RAW body before any JSON parsing**: `raw = await request.body()`; do NOT
  bind a Pydantic body model (that consumes/reparses first). Compute
  `hmac.new(secret, raw, sha256).hexdigest()`, compare to `X-Hub-Signature-256`
  with **`hmac.compare_digest`** (constant-time), then `json.loads(raw)`.
- **Reject** (missing/malformed/wrong sig): **401**, log a warning with delivery ID, stop.
- **Replay**: dedup on `X-GitHub-Delivery` UUID in a bounded in-memory LRU; seen →
  **200 "already processed"**, no re-review. Limitation: the in-memory cache does
  not survive restart (acceptable at demo scale; a KV/Redis store would harden it).

## 8. Testing strategy (deterministic-first)

Stack: `pytest`, `pytest-asyncio`, `httpx.AsyncClient` + `ASGITransport`, `respx`
(mock outbound HTTP), recorded LLM cassettes.

1. **HMAC unit** — valid, invalid, missing header, **replayed delivery-ID → 200 no-op**.
2. **Provider adapters** — mock backend HTTP; normalized output + usage; **malformed
   JSON → repair path → typed result**.
3. **Specialist schema** — good + off-schema model output through validate-repair.
4. **Orchestrator partial-failure** — mock one specialist raising; assert other two
   survive and merge yields a `status:failed` envelope (resilience checkbox).
5. **Comment formatter (golden file)** — fixed findings incl. a failed specialist →
   exact Markdown.
6. **E2E — offline/CI**: signed payload fixture + mocked GitHub + LLM cassettes →
   deterministic; asserts the comment body contains the seeded issues.
7. **E2E — live dry-run**: `scripts/seed_demo_pr.py` opens a real PR from
   `fixtures/bad_code/` (planted hardcoded credential + N+1 query + magic number);
   assert comment appears within 15s with expected findings. **This is the
   rehearsable demo.**

## 9. Deploy + cost model

- **Dockerfile**: `uvicorn app.main:app`; runs identical locally / tunneled / on CF Container.
- **Public URL**: `cloudflared tunnel` → stable hostname → set as the GitHub App webhook URL.
- **Secrets/env**: `GITHUB_WEBHOOK_SECRET`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`,
  `GITHUB_APP_INSTALLATION_ID`, `LLM_PROVIDER`, plus provider creds
  (`GOOGLE_CLOUD_PROJECT`/`GOOGLE_CLOUD_LOCATION` for vertex, `GEMINI_API_KEY`, `GROQ_API_KEY`).
- **Cost**: see `cost.md`. Documented production total ≈ $8–10/mo at brief scale;
  the demo runs at $0 on free tiers + the $300 GCP trial credit.

## 10. Build order (implementation session)

0. **Guided setup (interactive, BEFORE any code).** Claude walks the user through
   and verifies each prerequisite, and does NOT proceed to step 1 until they're done:
   - **GitHub App**: register the app; set webhook secret + permissions (PR read/write,
     contents read); subscribe to `pull_request` events; download the private-key PEM;
     install on a throwaway test repo; capture App ID + installation ID.
   - **GCP / Vertex**: create project, enable Vertex AI, `! gcloud auth application-default login`
     (run in-session via the `!` prefix); confirm the $300 trial is active.
   - **Cloudflare Tunnel**: `! cloudflared tunnel login`; create a **named** tunnel
     (stable hostname, survives restarts); map it to local `:8000`.
   - **Secrets hygiene**: create `.env` from `.env.example`; add `.env` + the PEM to
     `.gitignore` BEFORE the first commit; decide PEM-as-file-path vs base64 env value.
   Output: a filled `.env` + a `SETUP.md` checklist capturing the values and steps.

1. **Skeleton**: `config.py`, `main.py`, `Dockerfile`, `pyproject.toml`,
   `.github/workflows/ci.yml` (ruff + pytest); `/webhook` returns 202; `/healthz`
   returns 200; local run + CI green on the first PR.
2. **HMAC + dedup** (`hmac_verify.py`, `webhook.py`) + their tests. Reject/replay covered.
3. **GitHub App** (`github_app.py`): auth → fetch diff → find/create/edit comment.
   Verify against a real test PR.
4. **Provider layer** (`providers/*`) with `vertex` first + `validate.py` + tests.
5. **Security specialist END-TO-END** (`specialists/security.py`, `orchestrator.py`
   with a single specialist, `formatting.py`) → run `seed_demo_pr.py`, confirm a
   real comment appears within 15s. **Milestone: full path proven on a real PR.**
6. Add **Performance** + **Code Quality** behind the same interface; enable
   `asyncio.gather` fan-out + partial-failure merge + tests.
7. Add **`gemini`** and **`groq`** providers; live-swap demo.
8. README + final E2E (offline + live dry-run rehearsal).

## 11. Verification

- `pytest` green across all 7 test layers (section 8); CI runs layers 1–6 deterministically.
- `docker build` + local `uvicorn` boots; `/healthz` → 200; `/webhook` rejects an
  unsigned request (401) and no-ops a replayed delivery (200). GitHub Actions CI
  (`ruff` + `pytest` layers 1–6) is green on the PR.
- `cloudflared tunnel` public URL set as the GitHub App webhook; a manual GitHub
  "Redeliver" of a `pull_request` event produces a comment.
- **Live rehearsal**: `python scripts/seed_demo_pr.py` opens a PR with the three
  planted issues; the bot comment appears within 15s naming the hardcoded credential,
  the N+1 query, and the magic number; footer shows runtime + cost; provider swap
  (`LLM_PROVIDER=groq`) still produces a valid comment.
