"""Tests for the onboarding wizard's theme/language/RTL controls — mirrors
app/static/dashboard.html's existing implementation (design doc section 7).
Content-substring checks, same convention as tests/test_dashboard_page.py."""
from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from onboarding.main import app

STRINGS_KEYS = [
    "page_title", "heading", "lede", "theme_light", "theme_dark", "theme_system",
    "theme_popup_title", "lang_popup_title", "frame1_title", "frame1_instructions",
    "frame1_placeholder", "frame2_title", "frame3_title", "frame4_title",
    "frame5_title", "frame6_title", "coming_soon", "validate_button",
    "change_button", "badge_locked", "badge_ready", "badge_error", "badge_done",
    "err_empty_key", "err_invalid_key", "err_unreachable", "err_network",
    "checking", "restored", "owner_prefix",
]


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_page_has_theme_and_language_controls():
    client = await _client()
    resp = await client.get("/")
    body = resp.text
    assert 'id="themeToggleBtn"' in body
    assert 'id="langToggleBtn"' in body
    assert 'name="theme"' in body
    assert 'name="lang"' in body
    assert "עברית" in body
    assert 'dir="ltr"' in body


async def test_every_string_key_is_defined_for_both_languages():
    client = await _client()
    body = (await client.get("/")).text
    for key in STRINGS_KEYS:
        assert body.count(f"{key}:") == 2, f"{key} should appear once per language block"


async def test_theme_switch_uses_the_data_theme_attribute():
    client = await _client()
    body = (await client.get("/")).text
    assert 'document.documentElement.setAttribute("data-theme"' in body
    assert ':root[data-theme="dark"]' in body


async def test_language_switch_sets_dir_for_rtl():
    client = await _client()
    body = (await client.get("/")).text
    assert 'document.documentElement.setAttribute("dir", lang === "he" ? "rtl" : "ltr")' in body


async def test_popup_positioning_is_rtl_aware():
    client = await _client()
    body = (await client.get("/")).text
    assert "function positionPopup" in body
    assert 'document.documentElement.getAttribute("dir") === "rtl"' in body


async def test_theme_and_language_preferences_use_local_storage():
    """Unlike the Render key (sessionStorage only, see
    test_onboarding_page.py), these are non-secret per-visitor preferences
    that should reasonably persist across tabs."""
    client = await _client()
    body = (await client.get("/")).text
    assert 'localStorage.getItem("onboarding_lang")' in body
    assert 'localStorage.getItem("onboarding_theme")' in body


async def test_stored_lang_and_theme_are_validated_against_known_values():
    """An unrecognized localStorage value (a stale value from a future
    version, a manual edit, corruption) must not be trusted verbatim: an
    unguarded `localStorage.getItem("onboarding_lang") || "en"` lets a value
    like "fr" through, and STRINGS["fr"] is undefined — t()'s
    STRINGS[currentLang][key] then throws inside the DOMContentLoaded
    handler, aborting it before any event listeners (Validate, Change,
    theme/lang toggles, popups) get attached. The fix must clamp both
    values to their known-good sets at read time, not just trust-or-default."""
    client = await _client()
    body = (await client.get("/")).text
    assert 'localStorage.getItem("onboarding_lang") || "en"' not in body
    assert 'localStorage.getItem("onboarding_theme") || "system"' not in body
    assert 'KNOWN_LANGS' in body
    assert 'KNOWN_THEMES' in body
    assert '["en", "he"]' in body
    assert '["light", "dark", "system"]' in body
    assert "KNOWN_LANGS.includes(stored) ? stored : \"en\"" in body
    assert "KNOWN_THEMES.includes(stored) ? stored : \"system\"" in body
