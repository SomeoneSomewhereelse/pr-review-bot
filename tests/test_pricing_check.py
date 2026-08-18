"""Rate drift is detectable without a live call: compare() is pure, so the
network fetch stays in main() and the logic is fully testable."""
from __future__ import annotations

import httpx
import respx

from app.config import settings
from scripts import pricing_check

_GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"


def test_compare_reports_nothing_when_the_catalog_matches():
    result = pricing_check.compare({"llama-3.3-70b-versatile": (0.59, 0.79)})
    assert result.drift == []
    assert result.missing == []


def test_compare_reports_a_drifted_rate():
    """The drifted INPUT rate must appear, not merely a substring of it. The
    earlier `"0.7" in line` assertion passed on the untouched 0.79 that sits
    on the same line, so it never actually proved the drift was reported."""
    lines = pricing_check.compare({"llama-3.3-70b-versatile": (0.70, 0.79)}).drift
    assert len(lines) == 1
    assert "llama-3.3-70b-versatile" in lines[0]
    assert "(0.59, 0.79)" in lines[0], "must show what the table says"
    assert "(0.7, 0.79)" in lines[0], "must show what the catalog says"


def test_compare_offers_a_paste_ready_line_for_an_unpriced_model():
    result = pricing_check.compare({"llama-3.1-8b-instant": (0.05, 0.08)})
    assert result.drift == [], "a model the table lacks is not drift"
    assert any(
        '("groq", "llama-3.1-8b-instant"): Rate(0.05, 0.08' in ln
        for ln in result.missing
    )


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
    assert pricing_check.compare(catalog).drift == []


def _mock_catalog(models: dict[str, tuple[float, float]]):
    """Mock Groq's listing endpoint with per-TOKEN prices, as it really reports."""
    return respx.get(_GROQ_MODELS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": name, "pricing": {"prompt": pin / 1e6, "completion": pout / 1e6}}
                    for name, (pin, pout) in models.items()
                ]
            },
        )
    )


def test_main_exits_zero_when_the_catalog_merely_has_extra_models(monkeypatch, capsys):
    """Groq ships dozens of models this project never runs. Exiting non-zero
    on those trains an operator to ignore the tool, so only real drift fails."""
    monkeypatch.setattr(settings, "groq_api_key", "gsk_test")
    with respx.mock:
        _mock_catalog(
            {"llama-3.3-70b-versatile": (0.59, 0.79), "whisper-large-v3": (0.11, 0.0)}
        )
        assert pricing_check.main([]) == 0
    out = capsys.readouterr().out
    assert "1 catalog model(s) not in the rate table" in out
    assert "whisper-large-v3" not in out, "missing models stay collapsed by default"


def test_main_lists_missing_models_on_request(monkeypatch, capsys):
    monkeypatch.setattr(settings, "groq_api_key", "gsk_test")
    with respx.mock:
        _mock_catalog({"whisper-large-v3": (0.11, 0.0)})
        assert pricing_check.main(["--show-missing"]) == 0
    assert "whisper-large-v3" in capsys.readouterr().out


def test_main_exits_one_on_real_drift(monkeypatch, capsys):
    monkeypatch.setattr(settings, "groq_api_key", "gsk_test")
    with respx.mock:
        _mock_catalog({"llama-3.3-70b-versatile": (0.70, 0.79)})
        assert pricing_check.main([]) == 1
    assert "DRIFT" in capsys.readouterr().out
