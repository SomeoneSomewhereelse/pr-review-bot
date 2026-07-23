# CLAUDE.md — Autonomous Code Review Engine (Project ד)

## Project

Autonomous code-review engine. A GitHub PR webhook triggers an **Orchestrator**
that fetches the diff and fans out to **three parallel LLM specialists**
(Security, Performance, Code Quality); their findings are merged into a **single
Markdown PR comment**. Runs in production via a Docker container exposed through a
Cloudflare Tunnel (public URL, not localhost).

Full design lives in `SPEC.md`; cost model in `cost.md`.

## Tech stack

- **Backend**: FastAPI (async), managed with `uv`.
- **GitHub**: PyGitHub with **GitHub App** auth (JWT → short-lived installation token).
- **AI**: `google-genai` SDK behind an `LLMProvider` seam — `vertex` (default),
  `gemini` (AI-Studio), `groq` (cross-vendor). Selected via `LLM_PROVIDER` env var.
- **Concurrency**: `asyncio.gather(..., return_exceptions=True)`.
- **Validation**: Pydantic v2 with a shared validate-and-repair layer.
- **Tests**: `pytest`, `pytest-asyncio`, `httpx.AsyncClient`, `respx`.
- **CI**: GitHub Actions — `ruff` lint + `pytest` (deterministic test layers 1–6) on push/PR.
- **Deploy**: Docker + Cloudflare Tunnel (portable to a $5/mo Cloudflare Container).

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

## Cost

Documented production total ≈ **$8–10/mo** at brief scale (20 PRs/day). The demo
runs at **$0** on free tiers + the $300 GCP trial credit. Cost is graded as a
documented calculation, not as actual spend — see `cost.md`.
