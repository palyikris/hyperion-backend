import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_system_health_check(client: AsyncClient):
    """Ensure the system health endpoint always returns 200 OK and DB status."""
    response = await client.get("/api/system/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "database" in data
