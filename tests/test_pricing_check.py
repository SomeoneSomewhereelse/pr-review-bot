"""Rate drift is detectable without a live call: compare() is pure, so the
network fetch stays in main() and the logic is fully testable."""
from __future__ import annotations

from scripts import pricing_check


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
