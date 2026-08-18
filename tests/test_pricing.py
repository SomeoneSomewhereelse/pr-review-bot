"""Rate entries carry their own provenance, so a stale rate is detectable
rather than folded into a prose comment (design spec 2026-08-18 section 6f)."""
from __future__ import annotations

from datetime import date

import pytest

from app.providers import pricing


def test_every_rate_carries_a_source_url_and_a_parseable_verified_date():
    assert pricing._RATES, "rate table must not be empty"
    for (provider, model), rate in pricing._RATES.items():
        assert rate.source_url.startswith("https://"), (
            f"{provider}/{model} has no usable source_url"
        )
        date.fromisoformat(rate.verified)  # raises ValueError if malformed


def test_rate_for_returns_the_entry_or_none():
    assert pricing.rate_for("groq", "llama-3.3-70b-versatile") is not None
    assert pricing.rate_for("groq", "no-such-model") is None


def test_estimate_cost_usd_is_unchanged_by_the_provenance_fields():
    # 1M in + 1M out at (0.59, 0.79) == 1.38
    assert pricing.estimate_cost_usd(
        "groq", "llama-3.3-70b-versatile", 1_000_000, 1_000_000
    ) == pytest.approx(1.38)
