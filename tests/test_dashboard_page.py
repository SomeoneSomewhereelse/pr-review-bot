"""Tests for GET / — the static HTML dashboard page shell."""
from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from bot.main import app
from dashboard import auth


async def _client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={auth.SESSION_COOKIE_NAME: auth.create_session_token(remember=False)},
    )


async def test_dashboard_page_serves_html_with_theme_and_language_controls():
    client = await _client()
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert 'id="themeToggleBtn"' in body
    assert 'id="langToggleBtn"' in body
    assert 'name="theme"' in body
    assert 'name="lang"' in body
    assert "עברית" in body
    assert 'dir="ltr"' in body


async def test_dashboard_no_longer_served_at_slash_dashboard():
    """The page moved from /dashboard to / (no redirect, no duplicate route)
    — /dashboard should be gone, not just an alias."""
    client = await _client()
    resp = await client.get("/dashboard")
    assert resp.status_code == 404


async def test_dashboard_page_includes_polling_and_rendering_hooks():
    client = await _client()
    resp = await client.get("/")
    body = resp.text
    assert "/api/dashboard" in body
    assert "setInterval" in body
    assert "renderReviews" in body
    assert "renderStats" in body


async def test_render_stats_guards_on_queue_by_status_error_not_bare_queue_error():
    """renderStats's degrade-guard must check queue.by_status.error.

    /api/dashboard's queue payload is always {"by_status": ..., "backoff": ...} —
    there is no top-level "error" key on queue itself. build_dashboard_payload()
    (dashboard/router.py) degrades queue.by_status to {"error": "data unavailable"}
    on a store failure, not queue as a whole. A guard written as `queue.error`
    is permanently undefined and never trips, so a degraded queue would render
    a garbled stat tile (e.g. "q_error: data unavailable") instead of clearing
    the stats section. The guard must dereference queue.by_status?.error.
    """
    client = await _client()
    resp = await client.get("/")
    body = resp.text
    assert "queue.by_status?.error" in body
    assert "queue.error" not in body


async def test_dashboard_page_guards_null_est_cost_usd_before_tofixed():
    """est_cost_usd is nullable (Task 3 made unpriced reviews serialize as
    JSON null). Calling .toFixed(4) directly on it throws inside
    renderReviews, which is called from refreshDashboard's try block -- so
    one unpriced review among the most-recent 50 blanks the entire reviews
    table on every poll. The renderer must null-guard before .toFixed,
    following the same `?? "?"` idiom used for finding.line."""
    client = await _client()
    resp = await client.get("/")
    body = resp.text
    assert 'review.est_cost_usd === null ? "—" : `$${review.est_cost_usd.toFixed(4)}`' in body


async def test_dashboard_page_escapes_llm_text_before_innerhtml():
    """finding.*/specialist.error are attacker-influenced LLM text; the page
    must run them through esc() before interpolating into innerHTML, not
    just LLM-controlled-looking closed enums like severity/status."""
    client = await _client()
    resp = await client.get("/")
    body = resp.text
    assert "function esc(value)" in body
    assert "esc(specialist.error" in body
    assert "esc(text)" in body


async def test_dashboard_page_has_translated_specialist_names():
    """Specialist display names must come from STRINGS, not the raw literal
    'Security'/'Performance'/'Code Quality' field, so Hebrew mode doesn't
    half-translate ('Security: תקין')."""
    client = await _client()
    resp = await client.get("/")
    body = resp.text
    assert "sp_name_security" in body
    assert "אבטחה" in body
    assert "SPECIALIST_KEY" in body


async def test_dashboard_page_renders_queue_and_backoff_as_chips_not_one_string():
    """The Queue and Provider-backoff stat tiles must render each status/
    provider as its own chip element, not one run-on string joined with
    ' · ' — a single string wraps unpredictably at narrow widths (e.g. two
    unrelated statuses landing on the same visual line), which is exactly
    the mobile bug this pins against a regression."""
    client = await _client()
    resp = await client.get("/")
    body = resp.text
    assert "tile-chip-list" in body
    assert '<span class="tile-chip">' in body
    assert "queueChips" in body
    assert "backoffChips" in body
    # the old bug joined every status/provider into one run-on string with
    # this separator; the chip-based rendering must not reintroduce it
    assert '.join(", ")' not in body


async def test_stored_lang_and_theme_are_parsed_defensively():
    """An unrecognized stored value (not "en"/"he", not "light"/"dark"/
    "system") must not throw inside applyLanguage (STRINGS[currentLang][key]
    would throw for an unknown currentLang), which would abort
    DOMContentLoaded before any event listener attaches. Same defensive
    shape as onboarding/static/index.html's readStoredLang/readStoredTheme."""
    client = await _client()
    body = (await client.get("/")).text
    assert "function readStoredLang" in body
    assert "function readStoredTheme" in body
    assert "KNOWN_LANGS.includes(stored)" in body
    assert "KNOWN_THEMES.includes(stored)" in body
    assert 'localStorage.getItem("dashboard_lang") || "en"' not in body
    assert 'localStorage.getItem("dashboard_theme") || "system"' not in body


async def test_dashboard_page_has_a_logout_control_that_posts_to_api_logout():
    """The dashboard must expose a reachable way to log out -- POST /api/logout
    exists and is tested at the API level, but was unreachable from any page
    before this: no button anywhere called it."""
    client = await _client()
    resp = await client.get("/")
    body = resp.text
    assert 'id="logoutBtn"' in body
    assert '"/api/logout"' in body
    assert 'method: "POST"' in body


async def test_dashboard_page_refresh_redirects_to_login_on_401():
    """An expired/invalid session must not render as a permanent generic
    error banner -- refreshDashboard must check response.status for 401 and
    redirect to /login, rather than trying (and failing) to parse the body
    as the normal payload shape."""
    client = await _client()
    resp = await client.get("/")
    body = resp.text
    assert "response.status === 401" in body
    assert 'window.location.href = "/login"' in body


async def test_dashboard_page_anchors_popups_to_their_button():
    """Popups must be positioned near the button that opened them (an
    absolutely-positioned popup placed via getBoundingClientRect), not
    centered on the screen regardless of which button was clicked."""
    client = await _client()
    resp = await client.get("/")
    body = resp.text
    assert "function positionPopup" in body
    assert "getBoundingClientRect" in body
    assert "openPopup(\"themePopupBackdrop\", event.currentTarget)" in body
    assert "openPopup(\"langPopupBackdrop\", event.currentTarget)" in body
