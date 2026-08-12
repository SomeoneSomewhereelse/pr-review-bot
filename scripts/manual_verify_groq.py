"""Manual live verification for the Groq provider (app/providers/groq.py).

Not part of the pytest suite (CI never runs this) — it depends on a real,
live call to the Groq API using the real GROQ_API_KEY from `.env`. Groq is
the actually-live cross-vendor provider in this environment (Gemini's API
key is blocked at the account level — unrelated issue — so Groq was pulled
forward from the original build order to have a working live LLM path).

Run it directly:

    uv run python -m scripts.manual_verify_groq

It proves, against the real Groq API, through the real validate-repair
layer:
  1. A structured-output call succeeds and returns a validated instance of
     a tiny test schema (not a bare string).
  2. Real, non-zero token usage (tokens_in/tokens_out) comes back.

Never prints the API key or any other secret.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from app.config import settings
from app.providers.groq import GroqProvider
from app.providers.pricing import estimate_cost_usd
from app.providers.validate import validate_and_repair


class Greeting(BaseModel):
    message: str


def main() -> None:
    print(f"Provider: groq   Model: {settings.groq_model}")
    print("(never printing the API key)")

    provider = GroqProvider(api_key=settings.groq_api_key)

    system = "Respond in the given JSON schema."
    user = "Say hello in one short sentence."

    print("\nMaking a real, live call through validate_and_repair() ...")
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

    cost = estimate_cost_usd("groq", settings.groq_model, result.tokens_in, result.tokens_out)
    print(f"estimated cost: ${cost:.6f}")

    print(
        "\nSUCCESS: live Groq structured-output call verified "
        "end-to-end through validate_and_repair()."
    )


if __name__ == "__main__":
    main()
