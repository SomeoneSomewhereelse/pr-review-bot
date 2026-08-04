import hashlib
import hmac
import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.main import app
from app import webhook
from app.queue import store

TEST_SECRET = "test-webhook-secret"


def _sign(body: bytes, secret: str = TEST_SECRET) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


@pytest.fixture(autouse=True)
def _isolate(db, monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", TEST_SECRET)
    monkeypatch.setattr(settings, "llm_provider", "groq")
    webhook.reset_dedup_cache()
    yield
    webhook.reset_dedup_cache()


async def _client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_valid_signature_returns_202():
    body = b'{"action": "opened"}'
    headers = {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "11111111-1111-1111-1111-111111111111",
    }
    async with await _client() as c:
        response = await c.post("/webhook", content=body, headers=headers)
    assert response.status_code == 202


async def test_invalid_signature_returns_401():
    body = b'{"action": "opened"}'
    headers = {
        "X-Hub-Signature-256": _sign(body, secret="wrong-secret"),
        "X-GitHub-Delivery": "22222222-2222-2222-2222-222222222222",
    }
    async with await _client() as c:
        response = await c.post("/webhook", content=body, headers=headers)
    assert response.status_code == 401


async def test_missing_signature_header_returns_401():
    body = b'{"action": "opened"}'
    headers = {"X-GitHub-Delivery": "33333333-3333-3333-3333-333333333333"}
    async with await _client() as c:
        response = await c.post("/webhook", content=body, headers=headers)
    assert response.status_code == 401


async def test_replayed_delivery_id_is_noop(monkeypatch):
    body = b'{"action": "opened"}'
    headers = {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "44444444-4444-4444-4444-444444444444",
    }
    async with await _client() as c:
        first = await c.post("/webhook", content=body, headers=headers)
        second = await c.post("/webhook", content=body, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 200


async def test_opened_action_enqueues_ticket():
    payload = {
        "action": "opened",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 7, "head": {"sha": "abc123"}},
    }
    body = json.dumps(payload).encode()
    headers = {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "55555555-5555-5555-5555-555555555555",
    }
    async with await _client() as c:
        response = await c.post("/webhook", content=body, headers=headers)

    assert response.status_code == 202
    ticket = store.claim_next_due(now="2026-01-01T12:00:00+00:00")
    assert ticket is not None
    assert ticket.repo_full_name == "owner/repo"
    assert ticket.pr_number == 7
    assert ticket.head_sha == "abc123"


async def test_ignored_action_does_not_enqueue():
    payload = {
        "action": "closed",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 7, "head": {"sha": "abc123"}},
    }
    body = json.dumps(payload).encode()
    headers = {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": "66666666-6666-6666-6666-666666666666",
    }
    async with await _client() as c:
        response = await c.post("/webhook", content=body, headers=headers)

    assert response.status_code == 202
    assert store.claim_next_due(now="2026-01-01T12:00:00+00:00") is None
