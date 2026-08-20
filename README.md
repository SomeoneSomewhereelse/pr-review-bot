# Autonomous Code Review Engine

[![CI](https://github.com/SomeoneSomewhereelse/pr-review-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/SomeoneSomewhereelse/pr-review-bot/actions/workflows/ci.yml)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)
![uv](https://img.shields.io/badge/package%20manager-uv-de5fe9.svg)

Open a pull request, and this bot reviews it automatically — checking for
security risks, performance issues, and code-quality problems — then posts
the results as a single comment on the PR itself. Three specialists run in
parallel, and later pushes edit that same comment in place rather than
piling up new ones.

**[Deploy your own →](https://someonesomewhereelse.github.io/pr-review-bot/setup/)**
— the full setup guide, from a fresh clone to a first posted review comment,
covering both a local run and a hosted Render + Supabase deployment. The same
pages live in [`guide/`](guide/setup/index.md) if you would rather read them
in the repo.

Full design: [`SPEC.md`](SPEC.md). Stack/conventions: [`CLAUDE.md`](CLAUDE.md).
Cost model: [`cost.md`](cost.md).

## Architecture

```mermaid
flowchart LR
    PR(["Pull request opened<br/>or updated"]) --> Hook(["Webhook received<br/>&amp; verified"])
    Hook --> Queue(["Queued for review"])
    Queue --> Sec(["Security Review"])
    Queue --> Perf(["Performance Review"])
    Queue --> Qual(["Code Quality Review"])
    Sec --> Merge(["Findings combined"])
    Perf --> Merge
    Qual --> Merge
    Merge --> Comment(["Posted as a PR comment"])

    classDef stage fill:#e8eefc,stroke:#5b7cd6,stroke-width:1.5px,color:#1a1a2e;
    classDef specialist fill:#eaf7ef,stroke:#3fa66d,stroke-width:1.5px,color:#0f2f1c;
    class PR,Hook,Queue,Merge,Comment stage;
    class Sec,Perf,Qual specialist;
```

<details>
<summary><strong>Implementation detail</strong></summary>

- Webhook reads the **raw body**, verifies the HMAC-SHA256 signature in
  constant time, and returns `401` if it doesn't match.
- Each delivery is deduplicated on `X-GitHub-Delivery` — a replay gets a
  `200` no-op instead of a second review.
- A valid delivery becomes a durable Postgres ticket; the webhook returns
  `202` immediately — no LLM work happens in the request path.
- A single background dispatcher drains the queue FIFO, one ticket at a
  time, honoring a deferred ticket's `not_before`.
- A `429` from the provider posts/keeps a placeholder comment and reschedules
  the ticket — durable across a restart.
- Results (successes *and* failures) are merged with timing and cost data
  into one Markdown comment, edited in place on every later push.

See [`SPEC.md` §12](SPEC.md#12-review-queue-rpm--daily-quota-handling) for
the full queue design.
</details>

## What a review looks like

A real posted comment, condensed to one finding per specialist:

```markdown
## 🤖 Automated Code Review — PR #42
_3 specialists · llama-3.3-70b-versatile (groq) · 4.2s · ~$0.0021_

### 🔒 Security — 1 finding
| Severity | Line | Issue | Suggested fix |
| --- | --- | --- | --- |
| 🔴 critical | `app/auth.py:88` | API key is logged in plaintext when the request fails | Log only the key's length/hash, never the raw value |

### ⚡ Performance — 1 finding
| Impact | Line | Issue | Suggestion |
| --- | --- | --- | --- |
| 🟡 medium | `app/api/users.py:145` | N+1 | Batch these lookups into a single query |

### 🧹 Code Quality — 1 finding
| Category | Line | Issue | Refactoring suggestion |
| --- | --- | --- | --- |
| duplication | `app/utils/format.py:22` | Date-formatting logic is duplicated across three modules | Extract a shared helper |

---
<sub>Runtime 4.2s · 1,842 tok in / 612 tok out · est. $0.0021 · provider: groq</sub>
```

If a specialist's own check fails outright, its section says so plainly
instead of vanishing — partial failure is always visible, never silent.

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

856 deterministic tests, no real network calls — every GitHub, LLM-provider,
and webhook interaction is mocked. CI runs the same checks on every push.

A handful of scripts under `scripts/manual_verify_*.py` make real calls
against real accounts instead, each proving one specific integration (GitHub
App auth, or one LLM provider's structured-output path) — see the guide's
[Live rehearsal history](guide/background/rehearsals.md) for a real,
repeated end-to-end run through the actual GitHub webhook path.

## Known limitations

This project deliberately deviates from `SPEC.md`'s defaults in a few
documented ways — most notably, **Groq is the primary live provider**
(pulled forward for a reliable live path), with Gemini and Vertex AI both
also live and verified. The review queue is single-process only (no
horizontal scaling yet). See the guide's
[Provider history](guide/background/providers.md) for the full narrative of
each deviation, including what was tried and why.

## Cost

Demo runs at **$0** (Groq + Render + Supabase free tiers). Documented production
cost model (~$8-10/mo at brief scale) is in [`cost.md`](cost.md) — cost is
graded as that documented calculation, not actual spend.
