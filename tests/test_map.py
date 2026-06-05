import pytest
from fastapi import status
from app.models.upload.MediaStatus import MediaStatus
from app.models.db.Media import Media, MediaType


@pytest.mark.asyncio
async def test_get_map_data(auth_client, db_session):
    client = auth_client["client"]
    user = auth_client["user"]

    test_media = Media(
        id=uuid.uuid4(),
        uploader_id=user.id,
        status=MediaStatus.READY,
        location="POINT(20.0 47.0)",
        lat=47.0,
        lng=20.0,
        initial_metadata={"filename": "map_test.jpg"},
    )
    db_session.add(test_media)
    await db_session.commit()

    response = await client.get(
        "/api/map",
        params={"min_lat": 46.0, "max_lat": 48.0, "min_lng": 19.0, "max_lng": 21.0},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 1
    assert data["items"][0]["lat"] == 47.0


@pytest.mark.asyncio
async def test_get_map_stats_aggregation(auth_client, db_session):
    client = auth_client["client"]
    user = auth_client["user"]

    m1 = Media(
        id=uuid.uuid4(),
        uploader_id=user.id,
        location="POINT(20.001 47.001)",
        has_trash=True,
        confidence=0.9,
    )
    m2 = Media(
        id=uuid.uuid4(),
        uploader_id=user.id,
        location="POINT(20.002 47.002)",
        has_trash=True,
        confidence=0.8,
    )
    db_session.add_all([m1, m2])
    await db_session.commit()

    response = await client.get(
        "/api/map/stats",
        params={
            "min_lat": 46.0,
            "max_lat": 48.0,
            "min_lng": 19.0,
            "max_lng": 21.0,
            "resolution": 0.05,
        },
    )

    assert response.status_code == 200
    stats = response.json()
    assert stats["total"] >= 1
    assert stats["items"][0]["count"] == 2
    assert stats["items"][0]["confidence"] == 0.85  # Avg of 0.9 and 0.8

import uuid

@pytest.mark.asyncio
async def test_get_map_item_logs_404(auth_client):
    client = auth_client["client"]
    response = await client.get(f"/api/map/{uuid.uuid4()}/logs")
    assert response.status_code == 404
