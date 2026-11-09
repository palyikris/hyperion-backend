"""Soft-deletion regressions using the real PostgreSQL/PostGIS queries."""
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from app.api.stats_utils import (
    generate_manifest_data, get_environmental_footprint, get_hotspot_density,
    get_temporal_trends, get_trash_composition,
)
from app.models.db.Detection import Detection
from app.models.db.Media import Media, MediaType
from app.models.db.User import User
from app.models.db.VideoDetection import VideoDetection
from app.models.upload.MediaStatus import MediaStatus

BBOX = {"x": 0, "y": 0, "w": 0.5, "h": 0.5}
BOUNDS = {"min_lat": 46, "max_lat": 48, "min_lng": 18, "max_lng": 21}


async def seed(db, user_id, kind=MediaType.IMAGE):
    media = Media(
        id=uuid.uuid4(), uploader_id=user_id, status=MediaStatus.READY,
        media_type=kind, hf_path="sample.jpg", lat=47, lng=19,
        location="SRID=4326;POINT(19 47)", has_trash=True, confidence=90,
        technical_metadata={"detections_count": 2, "gps": {"lat": 47}},
        initial_metadata={"filename": "sample.jpg"},
    )
    records = []
    for index, confidence in enumerate((0.9, 0.8)):
        fields = dict(id=uuid.uuid4(), media_id=media.id, label="plastic",
                      confidence=confidence, bbox=BBOX, area_sqm=2)
        if kind == MediaType.VIDEO:
            fields.update(lat=47, lng=19, location="SRID=4326;POINT(19 47)",
                          timestamp_in_video=index * 10, frame_hf_path=f"frame{index}.jpg")
        records.append((VideoDetection if kind == MediaType.VIDEO else Detection)(**fields))
    db.add_all([media, *records])
    await db.commit()
    return media, records


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [MediaType.IMAGE, MediaType.VIDEO])
async def test_mark_clean_is_absent_everywhere_and_preserves_rows(auth_client, db_session, kind):
    client, user = auth_client["client"], auth_client["user"]
    media, records = await seed(db_session, user.id, kind)
    model = VideoDetection if kind == MediaType.VIDEO else Detection

    # Warm server caches before cleaning and check the original totals.
    summary = (await client.get("/api/stats/summary")).json()
    assert summary["environmental_footprint"]["total_detections"] == 2
    assert (await client.get("/api/map/stats", params=BOUNDS)).json()["total"] > 0

    response = await client.delete(f"/api/lab/{media.id}/detections/{records[0].id}")
    assert response.status_code == 204, response.text
    assert media.has_trash is True
    assert media.confidence == 80
    assert media.technical_metadata["detections_count"] == 1
    assert (await db_session.execute(select(model.id))).scalars().all() == [records[1].id]
    alias = aliased(model)
    assert (await db_session.execute(select(func.count(alias.id)))).scalar_one() == 1

    summary = (await client.get("/api/stats/summary")).json()
    assert summary["environmental_footprint"]["total_detections"] == 1
    footprint = await get_environmental_footprint(db_session, user.id)
    assert footprint.total_area_sqm == 2
    assert footprint.total_detections == 1
    manifest = await generate_manifest_data(db_session, user.id)
    assert len(manifest) == 1
    assert manifest[0]["Detection ID"] == str(records[1].id)

    if kind == MediaType.VIDEO:
        assert (await client.get(f"/api/lab/video/{records[0].id}")).status_code == 404
        assert (await client.patch(f"/api/lab/{records[0].id}", json={"item_type": "video", "lat": 48})).status_code == 404
    else:
        lab = (await client.get(f"/api/lab/image/{media.id}")).json()
        assert [item["id"] for item in lab["detections"]] == [str(records[1].id)]
        # A stale open tab cannot restore a cleaned ID by resubmitting its draft.
        stale = await client.patch(f"/api/lab/{media.id}", json={
            "item_type": "image", "detections": [
                {"id": str(item.id), "label": item.label, "bbox": BBOX} for item in records
            ],
        })
        assert stale.status_code == 409

    # A whole video must clear every frame, not just the selected evidence frame.
    response = await client.delete(f"/api/lab/{media.id}/detections")
    assert response.status_code == 204, response.text
    assert media.has_trash is False
    assert media.confidence == 0
    assert media.technical_metadata == {
        "detections_count": 0, "has_trash": False, "confidence": 0, "gps": {"lat": 47},
    }
    assert (await db_session.execute(select(func.count(model.id)))).scalar_one() == 0
    retained = (await db_session.execute(
        select(model).execution_options(include_cleaned_detections=True)
    )).scalars().all()
    assert len(retained) == 2
    assert all(item.deleted_at is not None for item in retained)
    assert all(item.bbox == BBOX and item.area_sqm == 2 for item in retained)
    assert await get_trash_composition(db_session, user.id) == []
    footprint = await get_environmental_footprint(db_session, user.id)
    assert footprint.total_area_sqm == 0
    assert footprint.total_detections == 0
    assert (await get_hotspot_density(db_session, user.id)).hotspot_count == 0
    assert all(item.count == 0 for item in await get_temporal_trends(db_session, user.id, 7))
    assert await generate_manifest_data(db_session, user.id) == []
    assert (await client.get("/api/stats/summary")).json()["environmental_footprint"]["total_detections"] == 0
    assert (await client.get("/api/map/stats", params=BOUNDS)).json()["total"] == 0
    assert (await client.get("/api/map", params={"has_trash": True})).json()["total"] == 0
    clean_map = (await client.get("/api/map", params={"has_trash": False})).json()
    assert clean_map["total"] == 1
    assert clean_map["items"][0]["has_trash"] is False
    assert clean_map["items"][0]["detections"] == []
    vault = (await client.get("/api/vault")).json()
    assert vault["total"] == 1  # Pagination/counts must exclude cleaned video rows too.
    assert vault["video_items"] == []
    assert vault["image_items"][0]["has_trash"] is False
    assert (await client.get(f"/api/lab/image/{media.id}")).json()["detections"] == []
    # Repeating Mark all clean is harmless.
    assert (await client.delete(f"/api/lab/{media.id}/detections")).status_code == 204


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [MediaType.IMAGE, MediaType.VIDEO])
async def test_cleaning_is_scoped_to_owner_media_and_detection(auth_client, db_session, kind):
    client, user = auth_client["client"], auth_client["user"]
    own, own_detections = await seed(db_session, user.id, kind)
    db_session.add(User(id="other-clean-user", email="other-clean@example.com", hashed_password="test", full_name="Other"))
    await db_session.commit()
    other, other_detections = await seed(db_session, "other-clean-user", kind)

    for path in (
        f"{other.id}/detections", f"{other.id}/detections/{other_detections[0].id}",
        f"{own.id}/detections/{other_detections[0].id}",
        f"{own.id}/detections/{uuid.uuid4()}", f"{uuid.uuid4()}/detections",
    ):
        response = await client.delete(f"/api/lab/{path}")
        assert response.status_code == 404, response.text
    assert all(item.deleted_at is None for item in [*own_detections, *other_detections])
    assert (await client.delete(f"/api/lab/{own.id}/detections")).status_code == 204
    assert all(item.deleted_at is None for item in other_detections)
    assert other.has_trash is True


@pytest.mark.asyncio
async def test_clean_actions_require_authentication(client):
    assert (await client.delete(f"/api/lab/{uuid.uuid4()}/detections")).status_code == 401
