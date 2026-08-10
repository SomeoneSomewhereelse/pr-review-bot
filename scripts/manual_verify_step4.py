"""Manual live verification for Step 4 (app/providers/*).

Not part of the pytest suite (CI never runs this) — it depends on a real,
live call to the Gemini AI-Studio API using the real GEMINI_API_KEY from
`.env`. This is the ACTUALLY LIVE provider in this environment — Vertex was
evaluated and removed (it requires an attached payment card, which this
project's no-card constraint rules out; see CLAUDE.md's "Substitutions from
the brief").

Run it directly:

    uv run python -m scripts.manual_verify_step4

It proves, against the real Gemini API, through the real validate-repair
layer:
  1. A structured-output call succeeds and returns a validated instance of
     a tiny test schema (not a bare string).
  2. Real, non-zero token usage (tokens_in/tokens_out) comes back.

Never prints the API key or any other secret.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.config import settings
from app.providers.google_genai import GeminiProvider
from app.providers.pricing import estimate_cost_usd
from app.providers.validate import validate_and_repair


class Greeting(BaseModel):
    message: str


def main() -> None:
    print(f"Provider: gemini   Model: {settings.llm_model}")
    print("(never printing the API key)")

    provider = GeminiProvider()

    system = "Respond in the given JSON schema."
    user = "Say hello in one short sentence."

    print("\nMaking a real, live call through validate_and_repair() ...")
    import asyncio

    result = asyncio.run(validate_and_repair(provider, system, user, Greeting))

    print(f"\nok: {result.ok}")
    assert result.ok, f"live call failed: {result.error}"
    assert result.parsed is not None
    assert isinstance(result.parsed, Greeting)

    print(f"parsed: {result.parsed!r}")
    print(f"tokens_in: {result.tokens_in}")
    print(f"tokens_out: {result.tokens_out}")

    assert result.tokens_in > 0, "expected non-zero real prompt token usage"
    assert result.tokens_out > 0, "expected non-zero real completion token usage"

    cost = estimate_cost_usd("gemini", settings.llm_model, result.tokens_in, result.tokens_out)
    print(f"estimated cost: ${cost:.6f}")

    print(
        "\nSUCCESS: live Gemini structured-output call verified "
        "end-to-end through validate_and_repair()."
    )


if __name__ == "__main__":
    main()
