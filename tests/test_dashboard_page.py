"""Tests for GET /dashboard — the static HTML page shell."""
from __future__ import annotations

import re

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
    RTL-mirror (wide screens only) / rotation (narrow screens, unconditional)
    rules."""
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
    assert "scaleX(-1)" in body
    assert "rotate(90deg)" in body


async def test_dashboard_how_it_works_flow_breakpoint_matches_arrow_breakpoints():
    """The .hiw-flow row/column breakpoint and the arrow RTL-mirror/rotation
    breakpoints must all agree on the same boundary, on opposite sides of it
    (e.g. max-width: 1000px paired with min-width: 1001px) — otherwise the
    row layout can turn on at a viewport narrower than the flow's own
    min-width floors require, breaking mid-flow (see design spec, Layout)."""
    client = await _client()
    resp = await client.get("/dashboard")
    body = resp.text

    flow_re = r"@media \(max-width: (\d+)px\) \{\s*\.hiw-flow"
    arrow_wide_re = r"@media \(min-width: (\d+)px\) \{\s*\[dir=\"rtl\"\] \.hiw-arrow"
    arrow_narrow_re = r"@media \(max-width: (\d+)px\) \{\s*\.hiw-arrow svg"

    flow_break = int(re.search(flow_re, body).group(1))
    arrow_wide = int(re.search(arrow_wide_re, body).group(1))
    arrow_narrow = int(re.search(arrow_narrow_re, body).group(1))

    assert arrow_wide == flow_break + 1
    assert arrow_narrow == flow_break


async def test_dashboard_page_uses_a_thicker_svg_arrow_not_a_unicode_glyph():
    """The connector was a thin Unicode "→" glyph; it's now a bolder inline
    SVG (stroke-based, colored via currentColor from .hiw-arrow's own
    `color`), so the RTL-mirror/rotation transforms must target the svg
    element rather than a ::before pseudo-element's text content."""
    client = await _client()
    resp = await client.get("/dashboard")
    body = resp.text
    assert '<svg viewBox="0 0 24 24"' in body
    assert 'stroke="currentColor"' in body
    assert 'stroke-width="3"' in body
    assert ".hiw-arrow svg" in body
    assert '.hiw-arrow::before { content: "→"' not in body


async def test_dashboard_page_mini_cards_can_shrink_to_fit_their_group():
    """.hiw-mini-card must set min-width: 0 -- without it, flexbox's
    automatic content-based minimum stops the three specialist mini-cards
    from shrinking to fit .hiw-parallel-group on desktop, so they overflow
    to the right and get covered by the next card in the flow."""
    client = await _client()
    resp = await client.get("/dashboard")
    body = resp.text
    assert ".hiw-mini-card {\n    flex: 1;\n    min-width: 0;" in body
