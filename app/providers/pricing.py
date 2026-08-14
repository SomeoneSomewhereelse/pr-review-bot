"""Per-provider/model rate table + cost estimation.

Rates are representative (see ``cost.md``), pinned here as the single source
of truth for cost calculations.
"""

from __future__ import annotations

# USD per 1M tokens: (rate_in, rate_out), keyed by (provider, model).
# gemini/gemini-flash-latest: AI-Studio's per-token rate (see cost.md).
# groq/llama-3.3-70b-versatile: taken from Groq's live /openai/v1/models
# response (`pricing.prompt` / `pricing.completion`, USD per token) on
# 2026-07-23 — $0.59 / $0.79 per 1M tokens. Representative; verify at build
# time against https://groq.com/pricing before relying on it for real spend.
# vertex/gemini-flash-latest: the same model at the same published rate as the
# gemini entry below -- Vertex and AI-Studio differ in the auth path, not in
# what a token costs. Kept as a separate key because estimate_cost_usd is
# called with the ACTIVE provider name, and a missing entry is a hard KeyError.
_RATES: dict[tuple[str, str], tuple[float, float]] = {
    ("gemini", "gemini-flash-latest"): (0.30, 2.50),
    ("vertex", "gemini-flash-latest"): (0.30, 2.50),
    ("groq", "llama-3.3-70b-versatile"): (0.59, 0.79),
}


def estimate_cost_usd(provider: str, model: str, tokens_in: int, tokens_out: int) -> float:
    rates = _RATES.get((provider, model))
    if rates is None:
        raise KeyError(f"No pricing entry for provider={provider!r} model={model!r}")

    rate_in, rate_out = rates
    return (tokens_in / 1_000_000) * rate_in + (tokens_out / 1_000_000) * rate_out
