"""Per-provider/model rate table + cost estimation.

Rates are representative (see ``cost.md``), pinned here as the single source
of truth for cost calculations. ``groq``'s model/pricing isn't chosen yet
(a later build step) — ``estimate_cost_usd`` raises ``NotImplementedError``
for it rather than silently returning ``$0`` and masking an un-costed
provider.
"""

from __future__ import annotations

# USD per 1M tokens: (rate_in, rate_out), keyed by (provider, model).
# Vertex and Gemini share the same underlying model + rate (see cost.md).
_RATES: dict[tuple[str, str], tuple[float, float]] = {
    ("vertex", "gemini-flash-latest"): (0.30, 2.50),
    ("gemini", "gemini-flash-latest"): (0.30, 2.50),
}

_PLACEHOLDER_PROVIDERS = {"groq"}


def estimate_cost_usd(provider: str, model: str, tokens_in: int, tokens_out: int) -> float:
    if provider in _PLACEHOLDER_PROVIDERS:
        raise NotImplementedError(
            f"No pricing entry for provider={provider!r} yet — its model/rate "
            "isn't chosen (a later build step)."
        )

    rates = _RATES.get((provider, model))
    if rates is None:
        raise KeyError(f"No pricing entry for provider={provider!r} model={model!r}")

    rate_in, rate_out = rates
    return (tokens_in / 1_000_000) * rate_in + (tokens_out / 1_000_000) * rate_out
