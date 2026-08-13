"""Tests for GET /api/dashboard — the dashboard's JSON payload."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app import dashboard
from app.main import app
from app.queue import dispatcher, store
from app.specialists.schemas import ReviewResult, SpecialistResult


@pytest.fixture(autouse=True)
def _isolate(db):
    dispatcher.reset_blocked_until()
    yield
    dispatcher.reset_blocked_until()


async def _client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_empty_state_shape():
    client = await _client()
    resp = await client.get("/api/dashboard")
    assert resp.status_code == 200
    body = resp.json()
    assert body["stats"] == {"total_reviews": 0, "total_cost_usd": 0.0, "avg_elapsed_ms": 0}
    assert body["queue"]["by_status"] == {
        "pending": 0, "running": 0, "deferred": 0, "retrying": 0, "done": 0, "failed": 0,
    }
    assert body["queue"]["backoff"] == {"gemini": None, "groq": None}
    assert body["reviews"] == []


async def test_includes_a_recorded_review_and_active_backoff():
    review = ReviewResult(
        pr_number=42,
        provider="groq",
        model="llama-3.3-70b-versatile",
        results=[
            SpecialistResult(name="Security", status="ok", findings=[{"severity": "high"}],
                              elapsed_ms=10, tokens_in=5, tokens_out=2),
        ],
        total_elapsed_ms=10,
        total_tokens_in=5,
        total_tokens_out=2,
        est_cost_usd=0.001,
    )
    store.record_review("owner/repo", 42, review, comment_id=999, now="2026-08-11T12:00:00+00:00")

    from datetime import datetime, timezone
    dispatcher._blocked_until["groq"] = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)

    client = await _client()
    resp = await client.get("/api/dashboard")
    body = resp.json()

    assert body["stats"]["total_reviews"] == 1
    row = body["reviews"][0]
    assert row["repo"] == "owner/repo"
    assert row["pr_number"] == 42
    assert row["comment_url"] == "https://github.com/owner/repo/pull/42#issuecomment-999"
    assert row["specialists"][0]["name"] == "Security"
    assert body["queue"]["backoff"]["groq"] == "2026-08-11T14:00:00+00:00"
    assert body["queue"]["backoff"]["gemini"] is None


async def test_degrades_a_single_section_on_store_error(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(dashboard.store, "dashboard_stats", boom)

    client = await _client()
    resp = await client.get("/api/dashboard")
    assert resp.status_code == 200
    body = resp.json()
    assert body["stats"] == {"error": "data unavailable"}
    assert body["reviews"] == []  # unaffected sections still populate


async def test_degrades_queue_by_status_independently_of_backoff(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(dashboard.store, "dashboard_queue_counts", boom)
    from datetime import datetime, timezone
    dispatcher._blocked_until["groq"] = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)

    client = await _client()
    resp = await client.get("/api/dashboard")
    body = resp.json()
    assert body["queue"]["by_status"] == {"error": "data unavailable"}
    assert body["queue"]["backoff"]["groq"] == "2026-08-11T14:00:00+00:00"
