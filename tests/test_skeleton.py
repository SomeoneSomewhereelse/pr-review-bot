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


async def test_healthz_supports_head(client):
    """Uptime monitors (e.g. UptimeRobot's free tier) send HEAD by default to
    avoid transferring a response body; a GET-only route 405s every check."""
    response = await client.head("/healthz")
    assert response.status_code == 200
