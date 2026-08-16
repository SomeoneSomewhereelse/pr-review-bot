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
# vertex/gemini-2.5-flash: confirmed live 2026-08-14 (see ISSUES.md) that
# `gemini-flash-latest` does not exist as a Vertex publisher model for this
# project/region -- only the 2.5 generation is available there, so a real
# vertex deployment needs VERTEX_MODEL=gemini-2.5-flash, not the shared default.
# Rate is representative (Gemini 2.5 Flash's published per-token price at
# launch); verify at build time against current Vertex AI pricing before
# relying on it for real spend, same caveat as the groq entry below.
_RATES: dict[tuple[str, str], tuple[float, float]] = {
    ("gemini", "gemini-flash-latest"): (0.30, 2.50),
    ("vertex", "gemini-flash-latest"): (0.30, 2.50),
    ("vertex", "gemini-2.5-flash"): (0.30, 2.50),
    ("groq", "llama-3.3-70b-versatile"): (0.59, 0.79),
}


def estimate_cost_usd(provider: str, model: str, tokens_in: int, tokens_out: int) -> float:
    rates = _RATES.get((provider, model))
    if rates is None:
        raise KeyError(f"No pricing entry for provider={provider!r} model={model!r}")

    rate_in, rate_out = rates
    return (tokens_in / 1_000_000) * rate_in + (tokens_out / 1_000_000) * rate_out


def is_known(provider: str, model: str) -> bool:
    """Whether (provider, model) has a rate entry -- i.e. whether
    estimate_cost_usd would succeed instead of raising KeyError for it.

    scripts/set_override.py's --model validation is the reason this exists:
    without it, an operator/agent could set a model with no pricing entry,
    and the KeyError would only surface in app/orchestrator.py's cost
    estimation -- AFTER all three specialists already made real, paid calls.
    """
    return (provider, model) in _RATES


def models_for(provider: str) -> tuple[str, ...]:
    """Every model this rate table knows for `provider`, sorted -- lets a
    refusal message name the valid options instead of just saying "unknown"."""
    return tuple(sorted(model for (p, model) in _RATES if p == provider))
