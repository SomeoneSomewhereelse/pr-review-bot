import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_healthz_returns_200(client):
    response = await client.get("/healthz")
    assert response.status_code == 200


async def test_webhook_returns_202(client):
    response = await client.post("/webhook", json={"action": "opened"})
    assert response.status_code == 202
