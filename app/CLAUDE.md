# app/ — module boundaries and contracts

Loaded when working with files under `app/`. Project-wide conventions,
substitutions from the brief, and process rules live in the root `CLAUDE.md`.

## Layering constraints

- **Orchestrator** owns: diff prep (line annotation, token cap), fan-out, merge,
  formatting. Knows nothing about provider internals.
- **Specialists** are uniform (`run()`), differing only by **system prompt** +
  **Pydantic schema**. Each records its own timing + token usage. Know nothing about GitHub.
- **Providers** are swappable via `LLM_PROVIDER`; a shared validate-repair layer
  guarantees structured output regardless of provider.
- **Formatting** turns a `ReviewResult` into Markdown. Knows nothing about LLMs.

## Contracts

- Webhook handler: verify HMAC on the **raw body** → return **202 immediately** →
  run the review in a background task.
- Provider adapters normalize usage metadata (`tokens_in`/`tokens_out`) so cost
  can be computed from a single rate table (`pricing.py`).
