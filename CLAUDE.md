# CLAUDE.md — Autonomous Code Review Engine (Project ד)

## Project

Autonomous code-review engine. A GitHub PR webhook triggers an **Orchestrator**
that fetches the diff and fans out to **three parallel LLM specialists**
(Security, Performance, Code Quality); their findings are merged into a **single
Markdown PR comment**. Runs in production via a Docker container exposed through a
Render (stable public URL, not localhost), with the queue in Supabase Postgres.

Full design lives in `SPEC.md`; cost model in `cost.md`.

## Tech stack

- **Backend**: FastAPI (async), managed with `uv`.
- **GitHub**: PyGitHub with **GitHub App** auth (JWT → short-lived installation token).
- **AI**: `LLMProvider` seam with three adapters — `gemini`
  (`google-genai` SDK), `groq` (OpenAI-compatible, live primary), `github_models`
  (OpenAI-compatible via the user's GitHub account, live cross-vendor demo).
  Selected via `LLM_PROVIDER` env var.
- **Concurrency**: `asyncio.gather(..., return_exceptions=True)`.
- **Validation**: Pydantic v2 with a shared validate-and-repair layer.
- **Tests**: `pytest`, `pytest-asyncio`, `httpx.AsyncClient`, `respx`.
- **CI**: GitHub Actions — `ruff` lint + `pytest` (deterministic test layers 1–6) on push/PR.
- **Deploy**: Docker on Render (free tier) + Supabase Postgres, kept warm by a
  free external pinger. See `cost.md` for the alternatives that were weighed.

## Architecture of the agents

- **Orchestrator** owns: diff prep (line annotation, token cap), fan-out, merge,
  formatting. Knows nothing about provider internals.
- **Specialists** are uniform (`run()`), differing only by **system prompt** +
  **Pydantic schema**. Each records its own timing + token usage. Know nothing about GitHub.
- **Providers** are swappable via `LLM_PROVIDER`; a shared validate-repair layer
  guarantees structured output regardless of provider.
- **Formatting** turns a `ReviewResult` into Markdown. Knows nothing about LLMs.

## Conventions

- Async throughout; one-purpose modules with narrow interfaces.
- Secrets only via env vars; **no secret is ever logged**.
- **Partial failure is always visible** in the PR comment (a failed specialist
  renders a real row) — never silently dropped.
- Webhook handler: verify HMAC on the **raw body** → return **202 immediately** →
  run the review in a background task.
- Provider adapters normalize usage metadata (`tokens_in`/`tokens_out`) so cost
  can be computed from a single rate table (`pricing.py`).

## Substitutions from the brief (and why)

- **`google-genai`** instead of the legacy `vertexai.generative_models` SDK —
  same Vertex backend, and it is what makes the one-env-var provider swap trivial.
- **`gemini-flash-latest`** instead of `gemini-2.5-flash` — the brief's model is
  deprecated/removed. The alias is pinnable to a dated version via env for demo
  reproducibility.
- **`vertex` adapter removed** — Vertex AI requires an attached payment card,
  which the no-card constraint rules out (see SETUP.md §2), so it was never
  live-runnable and its tests could only ever be mocked. `SPEC.md` still records
  it as the brief's default; `cost.md` keeps the $300-trial-credit costing as
  the evaluation record.

## Cost

Documented production total ≈ **$8–10/mo** at brief scale (20 PRs/day). The demo
runs at **$0** on free tiers + the $300 GCP trial credit. Cost is graded as a
documented calculation, not as actual spend — see `cost.md`.

## LLM API testing hygiene (avoid Trust & Safety flags)

Gemini AI-Studio access got **account-level blocked** (`403 PERMISSION_DENIED:
Your project has been denied access`) during this build, confirmed across
multiple models, multiple projects, and multiple separate Google accounts —
per Google's own AI Developer Forum, this is an automated Trust & Safety flag,
and one documented trigger is **hitting repeated 429s / testing many models
back-to-back without backoff**, which is exactly what happened during
troubleshooting here. The only documented fix is attaching GCP billing, which
this project's setup deliberately avoids (see SETUP.md) — so once flagged, a
provider is effectively lost for the rest of the demo. **Rules to avoid
repeating this:**

- **Never loop/burst live calls across many models or keys** to "see what
  sticks." One deliberate, single live call per real verification need.
- **Prefer mocked/cassette tests for exploration.** Reserve real network calls
  for the one live-verification step a build step actually requires (per
  SPEC.md section 8's testing strategy) — not for debugging or model-shopping.
- **If a provider starts returning 403/429, stop calling it immediately** and
  investigate via docs/support channels rather than retrying with different
  models/keys in quick succession — retrying does not help and each attempt
  is one more data point that can reinforce an abuse-pattern flag.
- This applies to **any** LLM provider's free tier, not just Gemini — Groq and
  future alternatives should get the same restraint.
