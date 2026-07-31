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

**Updated in section 12:** step (4) above (`BackgroundTask: run_review()`) is
now a durable SQLite ticket enqueue; a single serial dispatcher — not the
webhook request — is the one caller of the review pipeline. This absorbs
per-minute/daily rate limits from the live providers without changing the
steps *inside* a review (diff prep → fan-out → merge → comment).

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
    base.py            LLMProvider protocol: async complete(system, user, schema) -> BaseModel;
                       RateLimited(retry_after) — raised on a 429 (section 12)
    google_genai.py    Vertex (vertexai=True) + Gemini (api_key) — one SDK, two clients
    groq.py            OpenAI-compatible client, constrained-decoding structured output
    github_models.py   OpenAI-compatible client via the user's GitHub PAT
    factory.py         select provider by LLM_PROVIDER env
    validate.py        validate-and-repair (one repair retry → typed empty-with-error)
    pricing.py         per-provider/model rate table → est_cost_usd
  queue/
    store.py           durable SQLite ticket store: enqueue_or_update, claim_next_due,
                       defer, mark_done, recover_on_startup, get_ticket (section 12)
    dispatcher.py      single serial consumer: process_next_due, run_forever,
                       in-memory blocked_until gate (section 12)
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

## 12. Review queue (RPM + daily-quota handling)

Full design rationale (problem statement, alternatives considered, accepted
costs): `docs/superpowers/specs/2026-07-27-queue-features-design.md`. This
section documents what was actually built.

**Problem.** The live providers' free tiers have real caps — Groq ≈ 30 RPM /
14.4K per day, GitHub Models ≈ single-digit RPM / ~150 requests per day — and
the original design fired 3 concurrent LLM calls per review straight from a
per-request `BackgroundTask` with zero coordination across PRs.

**Producer/consumer split.** `webhook.py` no longer runs any LLM work: it
verifies HMAC, dedups the delivery, and calls
`store.enqueue_or_update(...)` to upsert a durable ticket, then returns `202`
immediately. A single serial dispatcher (`app/queue/dispatcher.py`,
`run_forever`) is started as an `asyncio` task from the app lifespan
(`app/main.py`) and is the **only** caller of the review pipeline — this
serializes every pacing/quota decision, and serial dispatch is anti-burst by
construction.

**Durable SQLite ticket, one per PR.** `app/queue/store.py` keeps one row per
`(repo_full_name, pr_number)` (`UNIQUE` constraint). `enqueue_or_update`
applies a single per-state re-review policy (full design rationale:
`docs/superpowers/specs/2026-07-28-dispatcher-followups-design.md` §6):
a push to a **`pending`** ticket updates `head_sha` and stays `pending`
(unreviewed, so no cooldown applies); a push to a **`deferred`** ticket
**rides out** — `head_sha` is updated but `status`/`not_before` are left
untouched, so a push can never shorten a provider's rate-limit wait or an
in-progress cooldown; a push to a **`running`** ticket updates `head_sha`
and sets a `rereview_requested` dirty flag (no task cancellation), so
`store.finalize_review` re-arms that ticket for exactly one coalesced
follow-up review of the latest commit once the in-flight run completes; a
push to a **`done`/`failed`** ticket re-arms via the `_due_after_cooldown`
helper — `attempts` resets to 0, and the ticket lands on `pending`
immediately or `deferred` until the per-PR cooldown
(`DISPATCHER_REREVIEW_COOLDOWN_SECONDS`, default 300s, keyed on
`last_reviewed_at`, the timestamp of the last *completed* review) elapses.
The cooldown is silent by design — the previous review's comment stays on
the PR while a re-review waits it out, so there is nothing to notify.
This cooldown now **escalates** per PR — a `cooldown_level` raises the
effective wait geometrically (`effective_cooldown(level) = min(base·2^level, cap)`,
`DISPATCHER_REREVIEW_COOLDOWN_MAX_SECONDS` default 3600s) for a PR that keeps
being pushed inside each window, resetting to 0 once the PR stays quiet for a
full window. Level 0 equals the base cooldown, so normal PRs are unchanged;
escalation is silent (only lengthens `not_before`); it bounds a churning PR from
~288 to ~26 reviews/day without ever abandoning it. The two escalation sites are:
(1) `enqueue_or_update` done/failed re-arm, and (2) `finalize_review`'s
dirty-flag branch.
`claim_next_due` claims the oldest due ticket (`pending`, or `deferred`
whose `not_before` has passed) with an atomic
`UPDATE ... WHERE status IN ('pending', 'deferred')`, so a claimed ticket
cannot be re-claimed. `enqueue_or_update`'s own SELECT → branch → INSERT/
UPDATE runs inside an explicit `BEGIN IMMEDIATE` transaction (manual
begin/commit/rollback, connection closed in a `finally`) on its single
connection, so the whole read-branch-write is atomic against
`claim_next_due`/`finalize_review`/`defer_*` even if a future change moves
the call off the event loop (e.g. `asyncio.to_thread`) — not merely safe by
virtue of today's synchronous, no-`await`-in-between execution. This is
deadlock-free: one lockable resource (`queue.db`) and one connection per
transaction rules out a circular wait, the transaction body opens no
second connection and calls no other `store` function (`_due_after_cooldown`
is pure Python), and no lock is ever held across an `await` (dispatcher
store calls are synchronous; the only `await`s in the review path — network
I/O in `attempt_review` — touch no DB). `BEGIN IMMEDIATE` also removes the
classic SQLite footgun it replaces (two connections each holding a SHARED
read lock, then both trying to upgrade to write, deadlocking each other):
it takes the write lock up front, so a losing concurrent writer blocks
before touching anything, waits up to the default 5s busy-timeout, and
raises `OperationalError("database is locked")` on contention rather than
deadlocking — and these transactions are sub-millisecond, so real
contention is negligible.

**Reactive detection, no caps.** Adapters (`app/providers/base.py` +
`google_genai.py`/`groq.py`/`github_models.py`) raise `RateLimited(retry_after)`
only on an actual `429`, parsing `Retry-After` (seconds or HTTP-date) via
`parse_retry_after`, falling back to `DEFAULT_RETRY_AFTER_SECONDS` (default
`60`) when the header is missing or unparseable. No per-provider RPM/RPD
number is hardcoded anywhere — a short `retry_after` behaves like a
per-minute limit, a long one like a daily wall; the code does not
distinguish them.

**Atomic reviews.** `orchestrator.attempt_review()` returns
`ReviewCompleted(review)` or `ReviewRateLimited(retry_after)`: if any of the
three specialist calls raises `RateLimited`, all partial results are
discarded and no comment is posted — the max `retry_after` across the
rate-limited calls is returned. `run_review()` remains as a
backward-compatible wrapper (raises `RateLimited` on the rate-limited case)
so existing scripts/tests are unaffected.

**Failure backoff + hard stop.** Two waits are kept separate end-to-end so a
provider-wide throttle and a single poisoned ticket never share a clock. A
`RateLimited` outcome (or the pre-flight `blocked_until` gate firing) defers
the ticket via `store.defer_rate_limited` — per-provider, floored at
`DISPATCHER_MIN_RETRY_AFTER_SECONDS` (default 1.0s) so a degenerate
`Retry-After: 0` or already-past HTTP-date can't tight-loop — and does
**not** count toward the hard stop, since a provider eventually frees up on
its own. Any other exception from `attempt_review` is a hard failure:
`store.defer_failed` increments the ticket's per-ticket `attempts` and the
dispatcher computes the next wait with the pure, unit-tested
`compute_backoff(attempts, jitter) = min(BASE * 2**(attempts-1), CAP) +
jitter()` (`DISPATCHER_FAILURE_BASE_BACKOFF_SECONDS` default 2.0,
`DISPATCHER_FAILURE_MAX_BACKOFF_SECONDS` default 300.0). `jitter()` comes
through an injectable module-level seam, `dispatcher._jitter()`, returning a
value in `[0, DISPATCHER_BACKOFF_JITTER_SECONDS]` (default 0.0 —
deterministic/off in tests and single-instance operation; a future
multi-instance deployment can set it above 0 to spread retries without a
code change). Once a ticket's hard-failure count reaches
`DISPATCHER_MAX_FAILURE_ATTEMPTS` (default 5), the dispatcher calls
`store.mark_failed` instead of deferring again and posts a marker-prefixed
`formatting.format_failure(pr_number, attempts)` comment naming the attempt
count — no raw exception text, per this project's secrets-hygiene rule —
satisfying "partial failure is always visible" for the terminal case. A plain
successful completion (no mid-run push) leaves `attempts` unchanged on the
now-`done` row — harmless, since that count is never read again unless the
ticket re-arms. `attempts` is explicitly reset to 0 in the two cases that
actually re-arm a ticket: `finalize_review`'s dirty-flag branch, when a
mid-run push coalesces into an immediate re-review, and a fresh push to a
`done`/`failed` ticket handled by `enqueue_or_update`'s terminal-state
branch.

**Never downgrade a good visible review.** A ticket's own
`last_reviewed_at` (set only by `finalize_review` on a genuinely successful
completion) is the signal that a real review is currently on the PR — a
tiny guard, `dispatcher._has_visible_review(ticket)`, checks
`last_reviewed_at is not None`. Two places used to overwrite that good
review unconditionally with something strictly worse; both now check the
guard first. At the terminal hard-stop (once `attempts` reaches
`DISPATCHER_MAX_FAILURE_ATTEMPTS`): if no good review is present, the
dispatcher overwrites the marker comment with
`formatting.format_failure(pr_number, attempts)` as before; if a good
review **is** present, it instead calls `github_app.append_review_footnote`
to append a sub-marker-delimited (`FAIL_NOTE_START`/`FAIL_NOTE_END`)
footnote below the preserved review, via
`formatting.format_failure_footnote(attempts)` — a repeated terminal
failure replaces the prior footnote in place (no stacking), and the next
successful review's `upsert_comment` overwrites the whole comment body, so
the footnote disappears on its own with no separate cleanup. This also
closes a silent-double-failure gap: the notice (overwrite or footnote) is
now posted **before** `store.mark_failed`, and if posting itself raises,
the ticket is **not** stranded as terminal — it goes through
`store.defer_failed` with the usual `compute_backoff` instead, so it keeps
retrying (and reattempts the notice) until visibility is actually restored.
Both `format_failure` and `format_failure_footnote` pluralize correctly
("1 attempt" / "N attempts").

**Placeholder → result, edited in place.** A ticket that can't run now (soft
`blocked_until` gate, or a fresh `RateLimited`) gets a placeholder comment —
`formatting.format_placeholder()` — posted through the same marker-based
`upsert_comment` used for real results, **unless** a good review is already
present (`_has_visible_review`), in which case the placeholder is
suppressed and the ticket still defers silently — the existing good review
stays up untouched until a later successful re-review overwrites it in
place. (A first-ever review, with `last_reviewed_at` still `None`, always
gets the placeholder — it is the only signal available at that point.) The
real comment later overwrites the placeholder in place, found via the
existing bot marker (no separate tracking needed for this). Wording varies
by wait magnitude: short waits say a rate limit was hit and the review will
appear shortly; waits at or above `PLACEHOLDER_DAILY_THRESHOLD_SECONDS`
(300s) name a daily quota and show an ETA computed from `now + retry_after`.

**In-memory `blocked_until` gate.** The dispatcher keeps a per-provider
`blocked_until` timestamp, learned only from the most recent
`RateLimited.retry_after`, so it doesn't fire calls it already knows will
fail. It is a soft optimization only — it is not persisted, and after a
restart it starts empty. What actually prevents an early run, restart or
not, is each deferred ticket's own durable `not_before`.

**Restart recovery.** At lifespan startup (`app/main.py`), before the
dispatcher starts: `store.recover_on_startup()` resets any `running` ticket
(interrupted mid-review by a crash) back to `pending`, also clearing a
`rereview_requested` flag if one was set (the fresh `pending` review already
covers the latest commit, so the flag is moot); `deferred` tickets are left
as-is, gated by their persisted `not_before`. The dispatcher then simply
drains whatever is due.

**Config** (`app/config.py`; none are per-provider caps): `QUEUE_DB_PATH`
(default `./queue.db`, gitignored along with its `-wal`/`-shm` sidecars),
`DEFAULT_RETRY_AFTER_SECONDS` (default `60`), `DISPATCHER_IDLE_SLEEP_SECONDS`
(default `1`), `DISPATCHER_FAILURE_BASE_BACKOFF_SECONDS` (default `2.0`),
`DISPATCHER_FAILURE_MAX_BACKOFF_SECONDS` (default `300.0`),
`DISPATCHER_MAX_FAILURE_ATTEMPTS` (default `5`),
`DISPATCHER_MIN_RETRY_AFTER_SECONDS` (default `1.0`),
`DISPATCHER_BACKOFF_JITTER_SECONDS` (default `0.0`, off),
`DISPATCHER_REREVIEW_COOLDOWN_SECONDS` (default `300.0`),
`DISPATCHER_REREVIEW_COOLDOWN_MAX_SECONDS` (default `3600.0`).

**Deliberate simplification vs. the design doc.** The design doc's §6.1/§9
describe storing the posted comment's `comment_id` on the ticket so a future
feature could reference "the review comment" directly. The `tickets` table
does have a `comment_id` column, and `store.finalize_review()` (the method
that replaced the earlier `mark_done()` when the dirty-flag re-review policy
was added) accepts an optional `comment_id` argument — but nothing in the
current dispatcher/orchestrator path populates it (`finalize_review` is
always called with no `comment_id`), so it is presently unused.
Placeholder→result replacement works purely off the existing comment
marker, not off a stored `comment_id`. The column is kept
available for the design doc's §13 "ping comment" future feature, which
remains out of scope.

**Out of scope** (unchanged from the design doc, all deliberate): provider
failover on a daily wall, proactive quota accounting (no `x-ratelimit-*`
tracking, no hardcoded caps), a priority scheme (FIFO is sufficient), and
horizontal scaling (single process, single dispatcher; the atomic ticket
claim would make multi-instance possible later but it is neither built for
nor tested).

**Testing.** Extends section 8's deterministic-first strategy with new
layers, all using an injected clock (no real sleeps): ticket store
(`tests/test_queue_store.py`), provider `RateLimited` parsing
(`tests/test_provider_rate_limited.py`), atomic rate-limit propagation
(`tests/test_orchestrator_rate_limited.py`), placeholder rendering
(`tests/test_placeholder_formatting.py`), the dispatcher's burst/daily-wall/
restart-recovery behavior (`tests/test_dispatcher.py`), and the webhook's
enqueue path (`tests/test_webhook.py`). One live-verification item remains
per `CLAUDE.md`'s hygiene rules: confirming GitHub Models actually sends a
usable `Retry-After` header on a `429` (one deliberate call) — not yet
performed; until it is, the `DEFAULT_RETRY_AFTER_SECONDS` fallback is what
governs that provider's backoff.
