"""Per-provider/model rate table + cost estimation.

Rates are representative (see ``cost.md``), pinned here as the single source
of truth for cost calculations.
"""

from __future__ import annotations

from typing import NamedTuple


class Rate(NamedTuple):
    """One (provider, model) price, with the provenance needed to tell whether
    it is still true. ``verified`` is an ISO date; ``source_url`` is where the
    number came from and where to re-check it. bot/scripts/pricing_check.py reads
    both."""

    rate_in: float   # USD per 1M input tokens
    rate_out: float  # USD per 1M output tokens
    source_url: str
    verified: str    # ISO date, YYYY-MM-DD
    # Set when `verified` does NOT record an independent check against
    # source_url -- e.g. a rate inherited from another provider's entry on a
    # same-price rationale. An empty note means `verified` means what it says.
    note: str = ""


_GROQ_PRICING = "https://groq.com/pricing"
_GEMINI_PRICING = "https://ai.google.dev/gemini-api/docs/pricing"
_VERTEX_PRICING = "https://cloud.google.com/vertex-ai/generative-ai/pricing"

# Rates are representative (see cost.md). Vertex and AI-Studio differ in the
# auth path, not in what a token costs, which is why the same model appears
# under both provider keys -- estimate_cost_usd is called with the ACTIVE
# provider name. vertex/gemini-2.5-flash exists because gemini-flash-latest is
# not a Vertex publisher model for this project (confirmed live 2026-08-14,
# see ISSUES.md).
_RATES: dict[tuple[str, str], Rate] = {
    ("gemini", "gemini-flash-latest"): Rate(0.30, 2.50, _GEMINI_PRICING, "2026-07-23"),
    ("vertex", "gemini-flash-latest"): Rate(
        0.30, 2.50, _VERTEX_PRICING, "2026-07-23",
        note="inherited from the gemini (AI-Studio) entry on a same-token-price "
        "rationale; not independently checked against Vertex's own pricing page",
    ),
    ("vertex", "gemini-2.5-flash"): Rate(0.30, 2.50, _VERTEX_PRICING, "2026-08-14"),
    ("groq", "llama-3.3-70b-versatile"): Rate(0.59, 0.79, _GROQ_PRICING, "2026-07-23"),
}


def rate_for(provider: str, model: str) -> Rate | None:
    """The rate entry for (provider, model), or None when unpriced."""
    return _RATES.get((provider, model))


def estimate_cost_usd(
    provider: str, model: str, tokens_in: int, tokens_out: int
) -> float | None:
    """Estimated USD cost, or None when (provider, model) has no rate entry.

    Returning None rather than raising is deliberate. This is called from
    bot/orchestrator.py AFTER all three specialists have already made real,
    paid calls, so a KeyError here threw away completed, paid work. Pricing is
    a nice-to-have on the comment, not a gate on which models may run --
    bot/scripts/deploy.py reports an unpriced model as a WARN row instead
    (design spec 2026-08-18 sections 6a and 6b).
    """
    rates = _RATES.get((provider, model))
    if rates is None:
        return None

    return (tokens_in / 1_000_000) * rates.rate_in + (tokens_out / 1_000_000) * rates.rate_out


def is_known(provider: str, model: str) -> bool:
    """Whether (provider, model) has a rate entry -- i.e. whether
    estimate_cost_usd would return a real number instead of None for it.

    Backs the advisory pricing warnings -- bot/scripts/set_override.py's
    --model and bot/scripts/deploy.py's check_pricing()/sync_env(). None of them
    blocks on the answer: an unpriced model runs fine and simply produces no
    cost estimate on the PR comment (design spec 2026-08-18 sections 6a/6b).
    It exists so those warnings can be specific about WHICH model is
    unpriced, rather than leaving an operator to discover a blank cost field.
    """
    return (provider, model) in _RATES


def models_for(provider: str) -> tuple[str, ...]:
    """Every model this rate table knows for `provider`, sorted -- lets a
    refusal message name the valid options instead of just saying "unknown"."""
    return tuple(sorted(model for (p, model) in _RATES if p == provider))
