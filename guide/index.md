# PR Review Engine

A GitHub PR webhook triggers an **Orchestrator** that fetches the diff and
fans out to three parallel LLM specialists — **Security**, **Performance**,
**Code Quality** — each backed by a structured-output LLM call. Findings are
merged into a single Markdown PR comment, edited in place on later pushes.

## What you'll need

- A GitHub account.
- An LLM API key — **Groq is recommended**: free tier, no card required.
- Either a local Postgres or a free [Supabase](https://supabase.com) project.

Budget about **30 minutes** for a first working review.

## Prerequisites

- **Python 3.12** (pinned in `.python-version`).
- [**uv**](https://docs.astral.sh/uv/) — this project's package/venv manager.
- **git**.
- **A Postgres you can reach** — Docker is one convenient way to get one
  locally, not the requirement itself; a hosted Supabase project works
  just as well and needs nothing installed.

!!! note "Why install instructions live here"
    `scripts/doctor.py` — the command below — runs *via* `uv`, so it cannot
    tell you how to install `uv` or Python itself. Get those two in place
    first; everything after that, `doctor` can check for you.

## Two tracks

- **Local** — run the engine on your own machine against a webhook-forwarding
  tool, for development and debugging. See [Local setup](setup/index.md).
- **Hosted** — deploy to Render with a real GitHub webhook, for a durable,
  always-on reviewer. See [Hosted setup](setup/index.md).

## The one command to remember

```bash
uv run python -m scripts.doctor
```

Run it any time, from a fresh clone or mid-setup. It answers three
questions: where am I, what's missing, and what's next — without ever
mutating anything.
