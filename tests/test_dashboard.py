"""Tests for dashboard.html markup content validation."""
from pathlib import Path

_DASHBOARD = Path(__file__).resolve().parent.parent / "app" / "static" / "dashboard.html"


def test_how_it_works_section_is_gone():
    """Presentation-only scaffolding; a polished version lives on the Pages
    landing page instead (design spec 2026-08-18 section 6g)."""
    html = _DASHBOARD.read_text(encoding="utf-8")
    for token in ("howItWorks", "hiwJumpBtn", "how-it-works", "hiw-", "hiw_"):
        assert token not in html, f"leftover How-it-works markup: {token}"


def test_specialist_name_strings_survive():
    """They are also used by the reviews table's name mapping, not only by the
    removed flow diagram."""
    html = _DASHBOARD.read_text(encoding="utf-8")
    for key in ("sp_name_security", "sp_name_performance", "sp_name_quality"):
        assert key in html
