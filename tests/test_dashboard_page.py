"""Tests for GET /dashboard — the static HTML page shell."""
from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from app.main import app


async def _client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_dashboard_page_serves_html_with_theme_and_language_controls():
    client = await _client()
    resp = await client.get("/dashboard")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert 'id="themeToggleBtn"' in body
    assert 'id="langToggleBtn"' in body
    assert 'name="theme"' in body
    assert 'name="lang"' in body
    assert "עברית" in body
    assert 'dir="ltr"' in body


async def test_dashboard_page_includes_polling_and_rendering_hooks():
    client = await _client()
    resp = await client.get("/dashboard")
    body = resp.text
    assert "/api/dashboard" in body
    assert "setInterval" in body
    assert "renderReviews" in body
    assert "renderStats" in body


async def test_render_stats_guards_on_queue_by_status_error_not_bare_queue_error():
    """renderStats's degrade-guard must check queue.by_status.error.

    /api/dashboard's queue payload is always {"by_status": ..., "backoff": ...} —
    there is no top-level "error" key on queue itself. build_dashboard_payload()
    (app/dashboard.py) degrades queue.by_status to {"error": "data unavailable"}
    on a store failure, not queue as a whole. A guard written as `queue.error`
    is permanently undefined and never trips, so a degraded queue would render
    a garbled stat tile (e.g. "q_error: data unavailable") instead of clearing
    the stats section. The guard must dereference queue.by_status?.error.
    """
    client = await _client()
    resp = await client.get("/dashboard")
    body = resp.text
    assert "queue.by_status?.error" in body
    assert "queue.error" not in body


async def test_dashboard_page_escapes_llm_text_before_innerhtml():
    """finding.*/specialist.error are attacker-influenced LLM text; the page
    must run them through esc() before interpolating into innerHTML, not
    just LLM-controlled-looking closed enums like severity/status."""
    client = await _client()
    resp = await client.get("/dashboard")
    body = resp.text
    assert "function esc(value)" in body
    assert "esc(specialist.error" in body
    assert "esc(text)" in body


async def test_dashboard_page_has_translated_specialist_names():
    """Specialist display names must come from STRINGS, not the raw literal
    'Security'/'Performance'/'Code Quality' field, so Hebrew mode doesn't
    half-translate ('Security: תקין')."""
    client = await _client()
    resp = await client.get("/dashboard")
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
    resp = await client.get("/dashboard")
    body = resp.text
    assert "tile-chip-list" in body
    assert '<span class="tile-chip">' in body
    assert "queueChips" in body
    assert "backoffChips" in body
    # the old bug joined every status/provider into one run-on string with
    # this separator; the chip-based rendering must not reintroduce it
    assert '.join(", ")' not in body


async def test_dashboard_page_anchors_popups_to_their_button():
    """Popups must be positioned near the button that opened them (an
    absolutely-positioned popup placed via getBoundingClientRect), not
    centered on the screen regardless of which button was clicked."""
    client = await _client()
    resp = await client.get("/dashboard")
    body = resp.text
    assert "function positionPopup" in body
    assert "getBoundingClientRect" in body
    assert "openPopup(\"themePopupBackdrop\", event.currentTarget)" in body
    assert "openPopup(\"langPopupBackdrop\", event.currentTarget)" in body


async def test_dashboard_page_has_how_it_works_section():
    """The explainer section: heading, all step copy in both languages, the
    parallel-group container and its mini-cards, and the arrow connector's
    (unconditional) downward rotation."""
    client = await _client()
    resp = await client.get("/dashboard")
    body = resp.text
    assert 'id="howItWorks"' in body
    assert "hiw_heading" in body
    assert "How it works" in body
    assert "איך זה עובד" in body
    assert "hiw_parallel_label" in body
    assert "3 specialists review in parallel" in body
    assert "3 מומחים בודקים במקביל" in body
    assert "hiw-parallel-group" in body
    assert "hiw-mini-card" in body
    assert "hiw-arrow" in body
    assert "rotate(90deg)" in body


async def test_dashboard_how_it_works_flow_is_always_vertical():
    """The flow is a fixed vertical stack at every screen size (both desktop
    and phone) — no row layout, no breakpoint, and therefore no RTL-mirror
    rule for the arrow (a downward arrow doesn't flip with reading
    direction). This was a deliberate simplification after the horizontal
    row layout kept causing width/wrapping bugs (mini-card overflow, the
    flow's own min-width exceeding its breakpoint, etc.)."""
    client = await _client()
    resp = await client.get("/dashboard")
    body = resp.text
    assert ".hiw-flow {\n    display: flex;\n    flex-direction: column;" in body
    assert ".hiw-parallel-cards {\n    display: flex;\n    flex-direction: column;" in body
    # these were the how-it-works section's own breakpoints; none should
    # remain now that the flow is unconditionally vertical
    assert "max-width: 1000px" not in body
    assert "max-width: 760px" not in body
    assert "min-width: 1001px" not in body
    assert "scaleX(-1)" not in body


async def test_dashboard_page_uses_a_thicker_svg_arrow_not_a_unicode_glyph():
    """The connector was a thin Unicode "→" glyph; it's now a bolder inline
    SVG (stroke-based, colored via currentColor from .hiw-arrow's own
    `color`), so the rotation transform must target the svg element rather
    than a ::before pseudo-element's text content."""
    client = await _client()
    resp = await client.get("/dashboard")
    body = resp.text
    assert '<svg viewBox="0 0 24 24"' in body
    assert 'stroke="currentColor"' in body
    assert 'stroke-width="3"' in body
    assert ".hiw-arrow svg" in body
    assert '.hiw-arrow::before { content: "→"' not in body


async def test_dashboard_page_mini_cards_have_min_width_zero():
    """.hiw-mini-card keeps min-width: 0 as defensive flex hygiene -- the
    original failure mode (flexbox's automatic content-based minimum
    stopping the cards from shrinking to fit .hiw-parallel-group) no longer
    applies now that the flow is always vertical, but the property is
    harmless to keep and cheap to guard against a future regression back to
    a row layout."""
    client = await _client()
    resp = await client.get("/dashboard")
    body = resp.text
    assert ".hiw-mini-card {\n    min-width: 0;" in body
