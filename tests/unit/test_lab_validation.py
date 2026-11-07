import math
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import Column, Float, MetaData, String, Table, Uuid, create_engine, insert

from app.api import lab, map as map_api
from app.api.deps import get_current_user
from app.api.stats_utils import cache as stats_cache
from app.database import get_db
from app.models.db.Detection import Detection
from app.models.db.Media import Media, MediaType
from app.models.db.VideoDetection import VideoDetection
from app.models.lab.MediaResponse import DetectionInput, MediaPatchRequest
from app.models.upload.MediaStatus import MediaStatus


BBOX = {"x": 0, "y": 0, "w": 0.5, "h": 0.5}


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalar_one(self):
        return self.value

    def one(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.value


class LabSession:
    """Emulate flush visibility while exercising the real route and merge code."""

    def __init__(self, media, video_detections=None):
        self.media = media
        self.video_detections = list(video_detections or [])
        self.pending = []
        self.deleted = []
        self.logs = []
        self.events = []
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        columns = statement.column_descriptions
        params = statement.compile().params
        if len(columns) == 2:
            assert self.events[-1] == "flush"
            records = [
                item for item in self.video_detections
                if item.media_id == params["media_id_1"]
            ]
            count = len(records)
            confidence = (
                sum(item.confidence for item in records) / count
                if count else None
            )
            return Result((count, confidence))
        if columns[0]["expr"] is Media:
            return Result(self.media)
        if "frame_hf_path_1" in params:
            return Result([
                item for item in self.video_detections
                if item.media_id == params["media_id_1"]
                and item.frame_hf_path == params["frame_hf_path_1"]
                and item.timestamp_in_video == params["timestamp_in_video_1"]
                and item.id != params["id_1"]
            ])
        selected_id = params["id_1"]
        return Result(next(
            (item for item in self.video_detections if item.id == selected_id),
            None,
        ))

    def add(self, value):
        if isinstance(value, VideoDetection):
            self.pending.append(value)
        else:
            self.logs.append(value)

    async def delete(self, value):
        self.deleted.append(value)

    async def flush(self):
        self.events.append("flush")
        self.video_detections = [
            item for item in self.video_detections if item not in self.deleted
        ]
        self.video_detections.extend(self.pending)
        self.pending.clear()

    async def commit(self):
        self.events.append("commit")

    async def refresh(self, value):
        pass


def make_media():
    now = datetime.now(timezone.utc)
    return Media(
        id=uuid.uuid4(), uploader_id="user-1", status=MediaStatus.READY,
        media_type=MediaType.IMAGE, initial_metadata={},
        technical_metadata={"gps": {"lat": 47, "lng": 19}},
        has_trash=True, confidence=99, created_at=now, updated_at=now,
        lat=47, lng=19, altitude=100,
    )


def make_detection(media, label="Trash", confidence=0.8):
    return Detection(
        id=uuid.uuid4(), media_id=media.id, label=label,
        confidence=confidence, bbox=BBOX.copy(), is_manual=False,
    )


def make_video_detection(media, label="Trash", confidence=0.8):
    return VideoDetection(
        id=uuid.uuid4(), media_id=media.id, media=media, label=label,
        confidence=confidence, bbox=BBOX.copy(), lat=47, lng=19, altitude=100,
        location="SRID=4326;POINT(19 47)", timestamp_in_video=12.75,
        frame_hf_path="media/frame.jpg", is_manual=False,
        created_at=datetime.now(timezone.utc),
    )


def input_detection(label="Trash", bbox=None, **kwargs):
    return {"label": label, "bbox": bbox or BBOX.copy(), **kwargs}


@pytest.mark.asyncio
@pytest.mark.parametrize("item_type", ["image", "video"])
@pytest.mark.parametrize("geometry_changed", [False, True])
async def test_geometry_edit_discards_stale_area_but_label_edit_preserves_it(
    item_type, geometry_changed
):
    media = make_media()
    original = (
        make_detection(media) if item_type == "image"
        else make_video_detection(media)
    )
    original.area_sqm = 8.0
    media.detections = [original] if item_type == "image" else []
    db = LabSession(media, [original] if item_type == "video" else [])
    bbox = {**BBOX, "w": 0.25} if geometry_changed else BBOX
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application(db)), base_url="http://test"
    ) as client:
        response = await client.patch(
            f"/api/lab/{media.id if item_type == 'image' else original.id}",
            json={"item_type": item_type, "detections": [input_detection(
                "Metal", bbox=bbox, id=str(original.id), area_sqm=8.0,
            )]},
        )
    assert response.status_code == 200, response.text
    saved = response.json()["detections"][0] if item_type == "image" else response.json()
    assert saved["bbox"] == bbox
    assert saved["label"] == "Metal"
    assert saved["area_sqm"] == (None if geometry_changed else 8.0)


def test_unrelated_detection_id_does_not_read_another_medias_area():
    media = make_media()
    original = make_detection(media)
    original.area_sqm = 8.0
    media.detections = [original]
    lab._merge_image_detections(media, [DetectionInput.model_validate(
        input_detection("Metal", id=str(uuid.uuid4()), bbox={**BBOX, "w": 0.25})
    )])
    assert len(media.detections) == 1
    assert media.detections[0].area_sqm is None


def application(db):
    app = FastAPI()
    app.include_router(lab.router, prefix="/api/lab")

    async def session():
        yield db

    async def current_user():
        return SimpleNamespace(id="user-1")

    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = current_user
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("item_type", ["image", "video"])
@pytest.mark.parametrize("old_area,new_area", [(8.0, 10.0), (None, 0.0)])
async def test_area_only_edit_marks_detection_as_manual(item_type, old_area, new_area):
    media = make_media()
    original = (
        make_detection(media) if item_type == "image"
        else make_video_detection(media)
    )
    original.area_sqm = old_area
    media.detections = [original] if item_type == "image" else []
    db = LabSession(media, [original] if item_type == "video" else [])
    selected_id = media.id if item_type == "image" else original.id

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application(db)), base_url="http://test"
    ) as client:
        response = await client.patch(
            f"/api/lab/{selected_id}",
            json={"item_type": item_type, "detections": [input_detection(
                id=str(original.id), area_sqm=new_area,
            )]},
        )

    assert response.status_code == 200, response.text
    assert original.area_sqm == new_area
    assert original.confidence == 1.0
    assert original.is_manual is True
    assert media.confidence == 100.0
    assert len(db.logs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("item_type", ["image", "video"])
@pytest.mark.parametrize("area", [None, 8.0])
async def test_unchanged_detection_preserves_automatic_origin(item_type, area):
    media = make_media()
    original = (
        make_detection(media) if item_type == "image"
        else make_video_detection(media)
    )
    original.area_sqm = area
    media.detections = [original] if item_type == "image" else []
    db = LabSession(media, [original] if item_type == "video" else [])
    selected_id = media.id if item_type == "image" else original.id

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application(db)), base_url="http://test"
    ) as client:
        response = await client.patch(
            f"/api/lab/{selected_id}",
            json={"item_type": item_type, "detections": [input_detection(
                id=str(original.id), area_sqm=area,
            )]},
        )

    assert response.status_code == 200, response.text
    assert original.area_sqm == area
    assert original.confidence == 0.8
    assert original.is_manual is False


@pytest.mark.asyncio
async def test_video_addition_matching_existing_sibling_is_idempotent():
    media = make_media()
    selected = make_video_detection(media)
    sibling = make_video_detection(media, "paper", 0.4)
    sibling.area_sqm = 8.0
    db = LabSession(media, [selected, sibling])
    payload = {"item_type": "video", "detections": [
        input_detection(id=str(selected.id)),
        input_detection("paper", bbox={"h": 0.5, "w": 0.5, "y": 0.0, "x": 0.0}),
        input_detection("paper"),
    ]}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application(db)), base_url="http://test"
    ) as client:
        for _ in range(2):
            response = await client.patch(f"/api/lab/{selected.id}", json=payload)
            assert response.status_code == 200, response.text
            assert db.video_detections == [selected, sibling]
            assert media.technical_metadata["detections_count"] == 2
            assert media.confidence == 60.0

    assert sibling.area_sqm == 8.0
    assert sibling.confidence == 0.4
    assert sibling.is_manual is False


@pytest.mark.asyncio
@pytest.mark.parametrize("geometry_changed", [False, True])
async def test_video_replacement_matching_sibling_preserves_selected_id(geometry_changed):
    media = make_media()
    selected = make_video_detection(media)
    sibling = make_video_detection(media, "paper", 0.4)
    bbox = {**BBOX, "w": 0.25} if geometry_changed else BBOX.copy()
    sibling.bbox = bbox
    selected.area_sqm = 8.0
    db = LabSession(media, [selected, sibling])

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application(db)), base_url="http://test"
    ) as client:
        response = await client.patch(
            f"/api/lab/{selected.id}",
            json={"item_type": "video", "detections": [input_detection(
                "paper", bbox=bbox, id=str(selected.id), area_sqm=8.0,
            )]},
        )

    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(selected.id)
    assert db.video_detections == [selected]
    assert db.deleted == [sibling]
    assert selected.bbox == bbox
    assert selected.area_sqm == (None if geometry_changed else 8.0)
    assert selected.confidence == 1.0
    assert selected.is_manual is True
    assert media.technical_metadata["detections_count"] == 1
    assert media.confidence == 100.0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["addition", "replacement"])
@pytest.mark.parametrize("different_field", ["media_id", "frame_hf_path", "timestamp_in_video"])
async def test_video_deduplication_does_not_touch_other_frames(operation, different_field):
    media = make_media()
    selected = make_video_detection(media)
    unrelated = make_video_detection(media, "paper", 0.4)
    different_values = {
        "media_id": uuid.uuid4(),
        "frame_hf_path": "media/another-frame.jpg",
        "timestamp_in_video": 13.0,
    }
    setattr(unrelated, different_field, different_values[different_field])
    db = LabSession(media, [selected, unrelated])
    submitted = [input_detection("paper")]
    if operation == "addition":
        submitted.insert(0, input_detection(id=str(selected.id)))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application(db)), base_url="http://test"
    ) as client:
        response = await client.patch(
            f"/api/lab/{selected.id}",
            json={"item_type": "video", "detections": submitted},
        )

    assert response.status_code == 200, response.text
    assert unrelated in db.video_detections
    assert unrelated.confidence == 0.4
    assert unrelated.is_manual is False
    assert db.deleted == []
    assert len(db.video_detections) == (3 if operation == "addition" else 2)
    matching_frame = [
        item for item in db.video_detections
        if item.media_id == selected.media_id
        and item.frame_hf_path == selected.frame_hf_path
        and item.timestamp_in_video == selected.timestamp_in_video
    ]
    assert sum(item.label == "paper" for item in matching_frame) == 1


@pytest.mark.asyncio
async def test_video_frame_lookup_filters_using_real_sql():
    engine = create_engine("sqlite://")
    table = Table(
        "video_detections", MetaData(),
        Column("id", Uuid, primary_key=True), Column("media_id", Uuid),
        Column("frame_hf_path", String), Column("timestamp_in_video", Float),
    )
    table.create(engine)
    media = make_media()
    selected = make_video_detection(media)
    sibling = make_video_detection(media, "paper")
    records = [selected, sibling]
    for field, value in (
        ("media_id", uuid.uuid4()),
        ("frame_hf_path", "another-frame.jpg"),
        ("timestamp_in_video", 13.0),
    ):
        unrelated = make_video_detection(media, "paper")
        setattr(unrelated, field, value)
        records.append(unrelated)
    try:
        with engine.begin() as connection:
            connection.execute(insert(table), [
                {field: getattr(item, field) for field in (
                    "id", "media_id", "frame_hf_path", "timestamp_in_video",
                )}
                for item in records
            ])
            matched_ids = []

            class FrameSession:
                async def execute(self, statement):
                    # Use the production WHERE clause against an isolated table,
                    # without needing PostGIS to deserialize complete ORM rows.
                    matched_ids.extend(connection.execute(
                        statement.with_only_columns(VideoDetection.id)
                    ).scalars().all())
                    return Result([
                        item for item in records if item.id in matched_ids
                    ])

                def add(self, value):
                    pytest.fail("An existing sibling must not be inserted again")

            await lab._merge_video_detections(FrameSession(), selected, [
                DetectionInput.model_validate(input_detection()),
                DetectionInput.model_validate(input_detection("paper")),
            ])

        assert matched_ids == [sibling.id]
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_video_repeated_addition_inserts_only_one_new_detection():
    media = make_media()
    selected = make_video_detection(media)
    db = LabSession(media, [selected])
    payload = {"item_type": "video", "detections": [
        input_detection(), input_detection("paper"), input_detection("paper"),
    ]}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application(db)), base_url="http://test"
    ) as client:
        for _ in range(2):
            response = await client.patch(f"/api/lab/{selected.id}", json=payload)
            assert response.status_code == 200, response.text
            assert len(db.video_detections) == 2
            assert media.confidence == 90.0

    added = db.video_detections[1]
    assert added.label == "paper"
    assert added.confidence == 1.0
    assert added.is_manual is True
    assert added.frame_hf_path == selected.frame_hf_path
    assert added.timestamp_in_video == selected.timestamp_in_video


@pytest.mark.asyncio
async def test_video_same_label_with_different_box_remains_a_distinct_detection():
    media = make_media()
    selected = make_video_detection(media)
    sibling = make_video_detection(media, "paper", 0.4)
    db = LabSession(media, [selected, sibling])
    other_bbox = {**BBOX, "x": 0.5}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application(db)), base_url="http://test"
    ) as client:
        response = await client.patch(
            f"/api/lab/{selected.id}",
            json={"item_type": "video", "detections": [
                input_detection(), input_detection("paper", bbox=other_bbox),
            ]},
        )

    assert response.status_code == 200, response.text
    assert len(db.video_detections) == 3
    assert db.video_detections[-1].bbox == other_bbox
    assert db.deleted == []


@pytest.mark.asyncio
async def test_deleting_selected_video_detection_keeps_its_sibling():
    media = make_media()
    selected = make_video_detection(media)
    sibling = make_video_detection(media)
    db = LabSession(media, [selected, sibling])

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application(db)), base_url="http://test"
    ) as client:
        response = await client.patch(
            f"/api/lab/{selected.id}",
            json={"item_type": "video", "detections": []},
        )

    assert response.status_code == 204, response.text
    assert db.video_detections == [sibling]
    assert media.technical_metadata["detections_count"] == 1
    assert media.confidence == 80.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"lat": 91}, {"lat": -91}, {"lng": 181}, {"lng": -181},
        {"altitude": -1}, {"altitude": 3001},
        {"detections": [input_detection(area_sqm=-0.1)]},
        {"detections": [input_detection(bbox={"x": -0.1, "y": 0, "w": 0.5, "h": 0.5})]},
        {"detections": [input_detection(bbox={"x": 0, "y": 0, "w": 0, "h": 0.5})]},
        {"detections": [input_detection(bbox={"x": 0.8, "y": 0, "w": 0.5, "h": 0.5})]},
        {"detections": [input_detection(bbox={"x": 0, "y": 0, "w": 0.5})]},
        {"detections": [input_detection(bbox={**BBOX, "extra": 1})]},
        {"detections": [{"label": "Trash", "bbox": {}}]},
        {"detections": [input_detection(bbox={**BBOX, "w": True})]},
    ],
)
async def test_invalid_patch_rejected_before_database_access(payload):
    db = LabSession(make_media())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application(db)), base_url="http://test"
    ) as client:
        response = await client.patch(f"/api/lab/{db.media.id}", json=payload)
    assert response.status_code == 422
    assert db.statements == []


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
@pytest.mark.parametrize("field", ["lat", "lng", "altitude"])
def test_non_finite_location_is_rejected(field, value):
    with pytest.raises(ValidationError):
        MediaPatchRequest.model_validate({field: value})


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
@pytest.mark.parametrize("field", ["area_sqm", "bbox"])
def test_non_finite_detection_values_are_rejected(field, value):
    payload = input_detection()
    if field == "bbox":
        payload["bbox"]["w"] = value
    else:
        payload[field] = value
    with pytest.raises(ValidationError):
        DetectionInput.model_validate(payload)


def test_valid_boundary_values_are_accepted():
    patch = MediaPatchRequest.model_validate({
        "lat": -90, "lng": 180, "altitude": 3000,
        "detections": [input_detection(
            bbox={"x": 0, "y": 0, "w": 1, "h": 1}, area_sqm=0,
        )],
    })
    assert patch.detections[0].bbox.model_dump() == {"x": 0, "y": 0, "w": 1, "h": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "detections,expected_count,expected_confidence",
    [
        ([], 0, 0),
        ([input_detection("plastic")], 1, 40),
        ([input_detection("glass")], 1, 100),
        ([input_detection("plastic"), input_detection("glass")], 2, 100),
        ([input_detection("plastic", area_sqm=2)], 1, 100),
    ],
)
async def test_image_edit_refreshes_summary(
    detections, expected_count, expected_confidence
):
    media = make_media()
    original = make_detection(media, "plastic", 0.4)
    media.detections = [original, make_detection(media, "paper", 0.9)]
    db = LabSession(media)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application(db)), base_url="http://test"
    ) as client:
        response = await client.patch(
            f"/api/lab/{media.id}",
            json={"item_type": "image", "detections": detections},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["has_trash"] == bool(expected_count)
    assert body["confidence"] == expected_confidence
    assert body["technical_metadata"]["detections_count"] == expected_count
    assert body["technical_metadata"]["has_trash"] == bool(expected_count)
    assert body["technical_metadata"]["confidence"] == expected_confidence
    assert body["technical_metadata"]["gps"] == {"lat": 47, "lng": 19}
    assert media.status == MediaStatus.READY
    assert db.events[-1] == "commit"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "detections,sibling,expected_count,expected_confidence",
    [
        ([], False, 0, 0),
        ([], True, 1, 40),
        ([input_detection()], True, 2, 60),
        ([input_detection("glass")], True, 2, 70),
        ([input_detection(), input_detection("glass")], True, 3, 73.33),
    ],
)
async def test_video_edit_aggregates_all_remaining_detections(
    detections, sibling, expected_count, expected_confidence
):
    media = make_media()
    media.media_type = MediaType.VIDEO
    selected = make_video_detection(media)
    records = [selected]
    if sibling:
        records.append(make_video_detection(media, "paper", 0.4))
    db = LabSession(media, records)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application(db)), base_url="http://test"
    ) as client:
        response = await client.patch(
            f"/api/lab/{selected.id}",
            json={"item_type": "video", "detections": detections},
        )

    assert response.status_code == (200 if detections else 204), response.text
    assert media.has_trash == bool(expected_count)
    assert media.confidence == expected_confidence
    assert media.technical_metadata["detections_count"] == expected_count
    assert media.technical_metadata["confidence"] == expected_confidence
    assert len(db.video_detections) == expected_count
    assert db.events == ["flush", "commit"]
    assert all(item.timestamp_in_video == 12.75 for item in db.video_detections)
    if detections:
        assert response.json()["timestamp_in_video"] == 12.75


@pytest.mark.asyncio
async def test_video_get_preserves_fractional_timestamp():
    media = make_media()
    detection = make_video_detection(media)
    db = LabSession(media, [detection])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application(db)), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/lab/video/{detection.id}")
    assert response.status_code == 200, response.text
    assert response.json()["timestamp_in_video"] == 12.75


@pytest.mark.asyncio
async def test_video_summary_query_is_scoped_to_its_parent():
    engine = create_engine("sqlite://")
    table = Table(
        "video_detections", MetaData(),
        Column("id", Uuid, primary_key=True),
        Column("media_id", Uuid), Column("confidence", Float),
    )
    table.create(engine)
    media = make_media()
    flushed = False
    try:
        with engine.begin() as connection:
            connection.execute(insert(table), [
                {"id": uuid.uuid4(), "media_id": media.id, "confidence": 0.8},
                {"id": uuid.uuid4(), "media_id": media.id, "confidence": 0.4},
                {"id": uuid.uuid4(), "media_id": uuid.uuid4(), "confidence": 1.0},
            ])

            class SummarySession:
                async def flush(self):
                    nonlocal flushed
                    flushed = True

                async def execute(self, statement):
                    assert flushed
                    return connection.execute(statement)

            await lab._refresh_video_summary(SummarySession(), media)

        assert media.confidence == 60.0
        assert media.technical_metadata["detections_count"] == 2
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_edit_invalidates_only_owners_server_caches(monkeypatch):
    media = make_media()
    media.detections = []
    map_cache = {("user-1", 1): (0, {}), ("other-user", 1): (0, {})}
    summary_cache = {("user-1", 7): (0, {}), ("other-user", 7): (0, {})}
    monkeypatch.setattr(map_api, "_map_stats_cache", map_cache)
    monkeypatch.setattr(stats_cache, "_stats_cache", summary_cache)
    db = LabSession(media)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application(db)), base_url="http://test"
    ) as client:
        response = await client.patch(
            f"/api/lab/{media.id}", json={"detections": []}
        )
    assert response.status_code == 200
    assert set(map_cache) == {("other-user", 1)}
    assert set(summary_cache) == {("other-user", 7)}
