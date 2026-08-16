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
- Provider adapter constructors (`GeminiProvider`, `VertexProvider`,
  `GroqProvider`) take an explicit `model: str` parameter and must never read
  `Settings` for the model internally. `app/providers/active_model.py` is the
  single resolver of "which model is active for this provider" (env default or
  DB override); adapters only ever bake in whatever model value they were
  constructed with. This is what keeps the model reported in the PR comment
  guaranteed equal to the model actually sent to the provider -- a regression
  back to an internal `Settings` read would silently break that guarantee.
