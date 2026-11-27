import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [
    ("delete", "/api/vault/not-a-uuid"),
    ("get", "/api/map/not-a-uuid/logs"),
])
async def test_malformed_media_ids_return_validation_errors(auth_client, method, path):
    response = await getattr(auth_client["client"], method)(path)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["path", "id"]
