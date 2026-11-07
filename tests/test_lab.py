import pytest
import uuid
from fastapi import status
from sqlalchemy import select
from app.models.db.Media import Media, MediaType
from app.models.db.Detection import Detection
from app.models.db.MediaLog import MediaLog
from app.models.db.VideoDetection import VideoDetection
from unittest.mock import patch


@pytest.mark.asyncio
async def test_patch_media_detection_update(auth_client, db_session):
    client = auth_client["client"]
    user = auth_client["user"]

    media_id = uuid.uuid4()
    det_id = uuid.uuid4()
    media = Media(
        id=media_id,
        uploader_id=user.id,
        location="POINT(20.0 47.0)",
        lat=47.0,
        lng=20.0,
    )
    det = Detection(
        id=det_id,
        media_id=media_id,
        label="plastic",
        confidence=0.5,
        bbox={"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5},
        area_sqm=100.0,
    )
    db_session.add(media)
    db_session.add(det)
    await db_session.commit()

    # Update detection area, which should trigger human validation (confidence -> 1.0)
    patch_payload = {
        "item_type": "image",
        "lat": 47.0,
        "lng": 20.0,
        "detections": [
            {
                "label": "plastic",
                "bbox": {"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5},
                "area_sqm": 200.0,
                "confidence": 0.9,
            }
        ],
    }

    response = await client.patch(f"/api/lab/{media_id}", json=patch_payload)
    if response.status_code == 422:
        print(response.json())

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["detections"][0]["area_sqm"] == 200.0
    assert data["detections"][0]["confidence"] == 1.0 


@pytest.mark.asyncio
async def test_patch_media_address_refresh(auth_client, db_session):
    client = auth_client["client"]
    user = auth_client["user"]

    media_id = uuid.uuid4()
    media = Media(
        id=media_id, uploader_id=user.id, lat=47.0, lng=20.0, address="Original Address"
    )
    db_session.add(media)
    await db_session.commit()

    # Patch: Move location by > 100m (approx 0.01 degrees)
    # Patching address to None triggers logic to re-fetch via get_address_from_coords
    with patch("app.api.lab.get_address_from_coords", return_value="New Address"):
        response = await client.patch(
            f"/api/lab/{media_id}",
            json={"item_type": "image", "lat": 47.1, "lng": 20.1},
        )

        assert response.json()["address"] == "New Address"


@pytest.mark.asyncio
async def test_get_media_security(auth_client, db_session):
    client = auth_client["client"]

    # Create a dummy user that the foreign key expects
    from app.models.db.User import User 

    wrong_user = User(id="wrong_user_id", email="test@test.com", hashed_password="pw", full_name="Test User", language="en")
    db_session.add(wrong_user)

    other_user_media = Media(id=uuid.uuid4(), uploader_id="wrong_user_id")
    db_session.add(other_user_media)
    await db_session.commit()

    response = await client.get(f"/api/lab/image/{other_user_media.id}")
    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_patch_video_detection_updates_and_adds_detections(
    auth_client, db_session
):
    client = auth_client["client"]
    user = auth_client["user"]

    media = Media(
        id=uuid.uuid4(),
        uploader_id=user.id,
        media_type=MediaType.VIDEO,
    )
    video_detection = VideoDetection(
        id=uuid.uuid4(),
        media_id=media.id,
        lat=47.0,
        lng=20.0,
        altitude=120.0,
        location="SRID=4326;POINT(20.0 47.0)",
        address="Original address",
        label="plastic",
        confidence=0.65,
        bbox={"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
        timestamp_in_video=12.0,
        frame_hf_path="media/frame_12.jpg",
    )
    db_session.add_all([media, video_detection])
    await db_session.commit()

    response = await client.patch(
        f"/api/lab/{video_detection.id}",
        json={
            "item_type": "video",
            "lat": 47.1,
            "lng": 20.1,
            "address": "Updated address",
            "detections": [
                {
                    "label": "glass",
                    "bbox": {"x": 0.15, "y": 0.2, "w": 0.3, "h": 0.4},
                    "area_sqm": 1.25,
                },
                {
                    "label": "paper",
                    "bbox": {"x": 0.5, "y": 0.5, "w": 0.2, "h": 0.2},
                },
            ],
        },
    )

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["id"] == str(video_detection.id)
    assert data["label"] == "glass"
    assert data["confidence"] == 1.0
    assert data["lat"] == 47.1
    assert data["lng"] == 20.1

    detections_result = await db_session.execute(
        select(VideoDetection)
        .where(VideoDetection.media_id == media.id)
        .order_by(VideoDetection.label)
    )
    detections = detections_result.scalars().all()
    assert [detection.label for detection in detections] == ["glass", "paper"]
    assert all(detection.confidence == 1.0 for detection in detections)
    assert all(detection.is_manual for detection in detections)
    assert all(detection.timestamp_in_video == 12.0 for detection in detections)
    assert all(
        detection.frame_hf_path == "media/frame_12.jpg"
        for detection in detections
    )

    log_result = await db_session.execute(
        select(MediaLog).where(MediaLog.media_id == media.id)
    )
    assert log_result.scalars().first() is not None


@pytest.mark.asyncio
async def test_patch_video_detection_can_delete_selected_detection(
    auth_client, db_session
):
    client = auth_client["client"]
    user = auth_client["user"]

    media = Media(
        id=uuid.uuid4(),
        uploader_id=user.id,
        media_type=MediaType.VIDEO,
    )
    video_detection = VideoDetection(
        id=uuid.uuid4(),
        media_id=media.id,
        lat=47.0,
        lng=20.0,
        altitude=120.0,
        location="SRID=4326;POINT(20.0 47.0)",
        label="plastic",
        confidence=0.65,
        bbox={"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
        timestamp_in_video=12.0,
        frame_hf_path="media/frame_12.jpg",
    )
    db_session.add_all([media, video_detection])
    await db_session.commit()

    response = await client.patch(
        f"/api/lab/{video_detection.id}",
        json={"item_type": "video", "detections": []},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    deleted_result = await db_session.execute(
        select(VideoDetection).where(VideoDetection.id == video_detection.id)
    )
    assert deleted_result.scalar_one_or_none() is None

    log_result = await db_session.execute(
        select(MediaLog).where(MediaLog.media_id == media.id)
    )
    assert log_result.scalars().first() is not None
