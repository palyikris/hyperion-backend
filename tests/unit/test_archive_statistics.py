import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import (
    Boolean, Column, DateTime, Enum, Float, JSON, MetaData, String, Table, Uuid,
    create_engine, insert, select,
)

from app.api import map as map_api, stats, vault
from app.api.stats_utils import cache
from app.api.stats_utils.kpi_hotspot_density import get_hotspot_density
from app.api.stats_utils.kpi_processing_time import get_mean_time_to_process
from app.models.db.Media import Media, MediaType
from app.models.db.VideoDetection import VideoDetection
from app.models.upload.MediaStatus import MediaStatus
from app.models.vault.VaultResponse import VaultResponse


class Result:
    def __init__(self, value):
        self.value = value

    def scalar(self):
        return self.value

    scalar_one = scalar
    scalar_one_or_none = scalar

    def scalars(self):
        return self

    def all(self):
        return self.value


USER = SimpleNamespace(id="user-1")
NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)


def media_record(number, filename, status=MediaStatus.READY, owner="user-1"):
    return SimpleNamespace(
        id=uuid.UUID(int=number), uploader_id=owner, status=status,
        media_type=MediaType.IMAGE, initial_metadata={"filename": filename},
        technical_metadata={}, created_at=NOW + timedelta(seconds=number),
        updated_at=NOW, hf_path=f"media/{filename}", assigned_worker=None,
        lat=47.0, lng=19.0, altitude=100, address=None, has_trash=True,
        confidence=90, failed_reason=None, detections=[],
    )


def video_record(number, parent, confidence=0.9):
    return SimpleNamespace(
        id=uuid.UUID(int=number), media_id=parent.id, media=parent,
        label="plastic", bbox={"x": 0, "y": 0, "w": 1, "h": 1},
        confidence=confidence, lat=47.0, lng=19.0, altitude=100,
        address=None, timestamp_in_video=number / 10, area_sqm=None,
        created_at=NOW + timedelta(seconds=number), frame_hf_path="media/frame.jpg",
    )


class CoreSession:
    """Execute real filters/pagination in SQLite without PostGIS or live data.

    The final entity hydration is replaced with fixture objects; the SQL that
    selects the combined page, applies ownership and filters is unchanged.
    """

    def __init__(self, connection, media, videos):
        self.connection = connection
        self.media = {item.id: item for item in media}
        self.videos = {item.id: item for item in videos}

    async def execute(self, statement):
        descriptions = statement.column_descriptions
        entity = descriptions[0].get("expr")
        if entity is Media or entity is VideoDetection:
            query = select(entity.id)
            if entity is VideoDetection:
                query = query.join(Media, VideoDetection.media_id == Media.id)
            ids = self.connection.execute(query.where(statement.whereclause)).scalars()
            records = self.media if entity is Media else self.videos
            return Result([records[item_id] for item_id in ids])
        return self.connection.execute(statement)


@pytest.fixture
def archive_db():
    engine = create_engine("sqlite://")
    metadata = MetaData()
    media_table = Table(
        "media", metadata, Column("id", Uuid, primary_key=True),
        Column("uploader_id", String), Column("status", Enum(MediaStatus)),
        Column("initial_metadata", JSON), Column("created_at", DateTime),
        Column("location", String), Column("has_trash", Boolean),
        Column("confidence", Float),
    )
    video_table = Table(
        "video_detections", metadata, Column("id", Uuid, primary_key=True),
        Column("media_id", Uuid), Column("created_at", DateTime),
        Column("location", String), Column("confidence", Float),
    )
    metadata.create_all(engine)
    media = [media_record(1, "zeta.jpg"),
             media_record(2, "alpha.jpg", MediaStatus.PENDING),
             media_record(3, "beta.mp4"), media_record(4, "other.mp4", owner="other")]
    videos = [video_record(11, media[2], 0.8), video_record(12, media[2], 0.79),
              video_record(13, media[2], 1.0), video_record(14, media[3], 1.0)]
    try:
        with engine.begin() as connection:
            connection.execute(insert(media_table), [{
                "id": item.id, "uploader_id": item.uploader_id, "status": item.status,
                "initial_metadata": item.initial_metadata, "created_at": item.created_at,
                "location": "point", "has_trash": True, "confidence": item.confidence,
            } for item in media])
            connection.execute(insert(video_table), [{
                "id": item.id, "media_id": item.media_id, "created_at": item.created_at,
                "location": "point", "confidence": item.confidence,
            } for item in videos])
            yield CoreSession(connection, media, videos)
    finally:
        engine.dispose()


async def get_archive(db, **kwargs):
    params = dict(search=None, status_filter=None, order_by="created_at",
                  direction="asc", page=1, page_size=2, db=db, current_user=USER)
    params.update(kwargs)
    result = await vault.get_media_vault(**params)
    VaultResponse.model_validate(result)
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("order_by", ["created_at", "filename", "status"])
@pytest.mark.parametrize("direction", ["asc", "desc"])
async def test_archive_uses_one_combined_page(archive_db, order_by, direction):
    seen = set()
    for page in range(1, 4):
        result = await get_archive(archive_db, page=page, order_by=order_by, direction=direction)
        assert result["total"] == 6
        assert result["total_pages"] == 3
        items = result["image_items"] + result["video_items"]
        assert len(items) == 2
        ids = {item["id"] for item in items}
        assert not ids & seen
        seen |= ids
    assert len(seen) == 6
    result = await get_archive(archive_db, page=4)
    assert result["image_items"] == result["video_items"] == []


@pytest.mark.asyncio
async def test_video_only_page_keeps_filename_and_sort_order(archive_db):
    result = await get_archive(archive_db, order_by="filename", page=2)
    assert result["image_items"] == []
    assert [item["id"] for item in result["video_items"]] == [str(uuid.UUID(int=n)) for n in (11, 12)]
    assert all(item["filename"] == "beta.mp4" for item in result["video_items"])


@pytest.mark.asyncio
@pytest.mark.parametrize("order_by", ["filename", "status"])
@pytest.mark.parametrize("direction", ["asc", "desc"])
async def test_video_order_uses_parent_filename_and_status(archive_db, order_by, direction):
    parent = media_record(5, "aardvark.mp4", MediaStatus.PENDING)
    detection = video_record(15, parent)
    media_table = Table("media", MetaData(), autoload_with=archive_db.connection)
    video_table = Table("video_detections", MetaData(), autoload_with=archive_db.connection)
    archive_db.connection.execute(insert(media_table), {
        "id": parent.id.hex, "uploader_id": parent.uploader_id,
        "status": parent.status.name, "initial_metadata": parent.initial_metadata,
        "created_at": parent.created_at,
    })
    archive_db.connection.execute(insert(video_table), {
        "id": detection.id.hex, "media_id": parent.id.hex, "created_at": detection.created_at,
    })
    archive_db.media[parent.id] = parent
    archive_db.videos[detection.id] = detection
    result = await get_archive(archive_db, page_size=100, order_by=order_by, direction=direction)
    expected = [15, 11, 12, 13] if direction == "asc" else [11, 12, 13, 15]
    assert [item["id"] for item in result["video_items"]] == [str(uuid.UUID(int=n)) for n in expected]


@pytest.mark.asyncio
@pytest.mark.parametrize("params,total", [
    ({"search": "BETA"}, 4), ({"status_filter": MediaStatus.PENDING}, 1),
    ({"search": "other"}, 0), ({"search": "missing"}, 0),
])
async def test_archive_filters_both_types_and_owner(archive_db, params, total):
    result = await get_archive(archive_db, page_size=100, **params)
    assert result["total"] == total
    assert len(result["image_items"]) + len(result["video_items"]) == total


@pytest.mark.asyncio
@pytest.mark.parametrize("threshold,expected", [(0, 3), (80, 2), (100, 1)])
async def test_video_map_confidence_filter_uses_percent_scale(archive_db, threshold, expected):
    result = await map_api.get_map_data(
        min_lat=None, max_lat=None, min_lng=None, max_lng=None,
        has_trash=None, min_confidence=threshold, db=archive_db, current_user=USER,
    )
    videos = [item for items in result["video_detections"].values() for item in items]
    assert len(videos) == expected


@pytest.mark.asyncio
async def test_clean_only_map_excludes_video_detections(archive_db):
    result = await map_api.get_map_data(
        min_lat=None, max_lat=None, min_lng=None, max_lng=None,
        has_trash=False, min_confidence=0, db=archive_db, current_user=USER,
    )
    assert result["video_detections"] == {}


@pytest.mark.asyncio
async def test_hotspot_video_threshold_is_normalized():
    statements = []

    async def execute(statement):
        statements.append(statement)
        return Result(0)

    await get_hotspot_density(SimpleNamespace(execute=execute), USER.id)
    assert 0.8 in statements[1].compile().params.values()
    assert 0.8 in statements[2].compile().params.values()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["summary", "pdf"])
async def test_statistics_endpoints_never_share_session_concurrently(monkeypatch, endpoint):
    busy = False
    events = []
    values = [[], {"total_area_sqm": 0, "total_detections": 0},
              {"workers": [], "fleet_reliability_score": 1, "total_successes": 0, "total_failures": 0},
              [], {"overall_avg_seconds": 0, "by_worker": []},
              {"hotspot_count": 0, "high_confidence_media_count": 0}]
    names = ["get_trash_composition", "get_environmental_footprint", "get_ai_fleet_efficiency",
             "get_temporal_trends", "get_mean_time_to_process", "get_hotspot_density"]

    def query(index):
        async def run(*args):
            nonlocal busy
            assert not busy, "Concurrent queries on the request session"
            busy = True
            await asyncio.sleep(0)
            events.append(index)
            busy = False
            return values[index]
        return run

    for index, name in enumerate(names):
        monkeypatch.setattr(stats, name, query(index))
    monkeypatch.setattr(stats, "get_cached_stats", lambda *_: None)
    monkeypatch.setattr(stats, "cache_stats", lambda *_: None)
    if endpoint == "summary":
        await stats.get_stats_summary(days=30, db=object(), current_user=USER)
    else:
        from io import BytesIO
        renderer = AsyncMock(return_value=BytesIO(b"pdf"))
        monkeypatch.setattr(stats, "create_pdf_report", renderer)
        await stats.get_statistics_pdf_report(days=30, language="hu", db=object(), current_user=USER)
        assert len(renderer.call_args.args[0]) == 7
    assert events == list(range(6))


@pytest.mark.asyncio
async def test_processing_time_ends_at_first_ready_not_later_validation():
    engine = create_engine("sqlite://")
    metadata = MetaData()
    media = Table("media", metadata, Column("id", Uuid, primary_key=True),
                  Column("uploader_id", String), Column("status", Enum(MediaStatus)))
    logs = Table("media_task_logs", metadata, Column("media_id", Uuid),
                 Column("worker_name", String), Column("action", String),
                 Column("message", String), Column("timestamp", DateTime))
    metadata.create_all(engine)
    own_id, other_id, incomplete_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        with engine.begin() as connection:
            connection.execute(insert(media), [
                {"id": own_id, "uploader_id": USER.id, "status": MediaStatus.READY},
                {"id": other_id, "uploader_id": "other", "status": MediaStatus.READY},
                {"id": incomplete_id, "uploader_id": USER.id, "status": MediaStatus.READY},
            ])
            rows = []
            for worker, status, seconds in [(None, "PENDING", 0), ("Helios", "EXTRACTING", 2),
                                             ("Helios", "READY", 10), ("Helios", "READY", 20),
                                             ("You", "READY", 86400)]:
                rows.append({"media_id": own_id, "worker_name": worker, "action": "STATUS_CHANGE",
                             "message": f"Status changed to {status}", "timestamp": NOW + timedelta(seconds=seconds)})
            rows += [{"media_id": other_id, "worker_name": "Helios", "action": "STATUS_CHANGE",
                      "message": "Status changed to READY", "timestamp": NOW},
                     {"media_id": incomplete_id, "worker_name": "Helios", "action": "STATUS_CHANGE",
                      "message": "Status changed to EXTRACTING", "timestamp": NOW}]
            connection.execute(insert(logs), rows)

            class Session:
                async def execute(self, statement):
                    # Execute the real interval subquery; SQLite cannot subtract
                    # timestamps like PostgreSQL, so aggregate seconds in Python.
                    intervals = connection.execute(select(statement.get_final_froms()[0])).all()
                    assert len(intervals) == 1
                    duration = (intervals[0].end_time - intervals[0].start_time).total_seconds()
                    if "worker_name" in intervals[0]._mapping:
                        return Result([SimpleNamespace(worker_name="Helios", avg_seconds=duration, task_count=1)])
                    return Result(duration)

            result = await get_mean_time_to_process(Session(), USER.id)
            assert result.overall_avg_seconds == 10
            assert result.by_worker[0].avg_processing_seconds == 8
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("all_media", [False, True])
async def test_deletion_invalidates_only_owners_caches(monkeypatch, all_media):
    item = media_record(1, "image.jpg")
    db = AsyncMock()
    db.execute.side_effect = ([Result([item]), Result(None)] if all_media
                              else [Result(item), Result([]), Result(None)])
    monkeypatch.setattr(vault, "delete_from_hf", AsyncMock())
    map_cache = {(USER.id, 1): (0, {}), ("other", 1): (0, {})}
    stats_cache = {(USER.id, 7): (0, {}), ("other", 7): (0, {})}
    monkeypatch.setattr(map_api, "_map_stats_cache", map_cache)
    monkeypatch.setattr(cache, "_stats_cache", stats_cache)
    if all_media:
        await vault.delete_all_media(db=db, current_user=USER)
    else:
        await vault.delete_media(id=str(item.id), db=db, current_user=USER)
    db.commit.assert_awaited_once()
    assert set(map_cache) == {("other", 1)}
    assert set(stats_cache) == {("other", 7)}
