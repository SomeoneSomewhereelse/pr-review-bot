"""Rate drift is detectable without a live call: compare() is pure, so the
network fetch stays in main() and the logic is fully testable."""
from __future__ import annotations

import httpx
import respx

from app.config import settings
from scripts import pricing_check

_GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"


def test_compare_reports_nothing_when_the_catalog_matches():
    assert pricing_check.compare({"llama-3.3-70b-versatile": (0.59, 0.79)}) == []


def test_compare_reports_a_drifted_rate():
    lines = pricing_check.compare({"llama-3.3-70b-versatile": (0.70, 0.79)})
    assert len(lines) == 1
    assert "llama-3.3-70b-versatile" in lines[0]
    assert "0.59" in lines[0] and "0.7" in lines[0]


def test_compare_offers_a_paste_ready_line_for_an_unpriced_model():
    lines = pricing_check.compare({"llama-3.1-8b-instant": (0.05, 0.08)})
    assert any('("groq", "llama-3.1-8b-instant"): Rate(0.05, 0.08' in ln for ln in lines)


def test_fetch_groq_catalog_rounds_the_per_token_conversion(monkeypatch):
    """Groq's real per-token prices (USD/token) don't survive a raw `* 1e6`
    conversion without float noise: 7.9e-7 * 1e6 == 0.7899999999999999, not
    0.79. _fetch_groq_catalog must round the conversion so the result
    compares equal to the _RATES table's (0.59, 0.79) -- otherwise compare()
    reports false drift on a catalog that actually matches."""
    monkeypatch.setattr(settings, "groq_api_key", "gsk_test")
    with respx.mock:
        respx.get(_GROQ_MODELS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "llama-3.3-70b-versatile",
                            "pricing": {"prompt": 5.9e-7, "completion": 7.9e-7},
                        }
                    ]
                },
            )
        )
        catalog = pricing_check._fetch_groq_catalog()
    assert catalog == {"llama-3.3-70b-versatile": (0.59, 0.79)}
    assert pricing_check.compare(catalog) == []
