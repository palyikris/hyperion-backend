"""Regression checks that execute the aggregation SQL on PostgreSQL/PostGIS."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.api.stats_utils import (
    get_ai_fleet_efficiency, get_environmental_footprint, get_fun_facts,
    get_hotspot_density, get_mean_time_to_process, get_temporal_trends,
    get_trash_composition, generate_manifest_data,
)
from app.models.db.AIWorker import AIWorkerState
from app.models.db.Detection import Detection
from app.models.db.Media import Media, MediaType
from app.models.db.MediaLog import MediaLog
from app.models.db.User import User
from app.models.db.VideoDetection import VideoDetection
from app.models.upload.MediaStatus import MediaStatus

NOW = datetime(2026, 6, 5, 12, tzinfo=timezone.utc)
BBOX = {"x": 0, "y": 0, "w": 0.5, "h": 0.5}


@pytest_asyncio.fixture
async def stats_data(db_session):
    user_id = uuid.uuid4().hex[:20]
    other_id = uuid.uuid4().hex[:20]
    for uid in (user_id, other_id):
        db_session.add(User(id=uid, email=f"{uid}@example.com", hashed_password="test", full_name="Stats"))
    for name in ("Helios", "Eos", "RetiredTitan"):
        await db_session.merge(AIWorkerState(name=name, tasks_processed_today=999,
                                             last_ping=NOW.replace(tzinfo=None)))
    await db_session.flush()

    def media(*, owner=user_id, worker="Helios", status=MediaStatus.READY,
              created=NOW - timedelta(seconds=120), elapsed=None, point=None,
              kind=MediaType.IMAGE, trash=True, completion_worker="Helios"):
        item = Media(
            id=uuid.uuid4(), uploader_id=owner, assigned_worker=worker,
            status=status, created_at=created, media_type=kind,
            has_trash=trash, confidence=99, initial_metadata={"filename": "sample.jpg"},
        )
        if point:
            item.lng, item.lat = point
            item.location = f"SRID=4326;POINT({point[0]} {point[1]})"
        db_session.add(item)
        if elapsed is not None:
            db_session.add(MediaLog(
                media_id=item.id, worker_name=completion_worker,
                action="STATUS_CHANGE", message="Status changed to READY",
                timestamp=created + timedelta(seconds=elapsed),
            ))
        return item

    def detection(item, label, confidence, area):
        db_session.add(Detection(media_id=item.id, label=label, confidence=confidence,
                                 area_sqm=area, bbox=BBOX))

    first = media(point=(10, 0), elapsed=60)
    detection(first, "plastic", 0.8, 1.4)
    detection(first, "metal", 0.6, None)
    second = media(worker="Eos", created=NOW.replace(day=3, hour=0, minute=1),
                   point=(10.0001, 0.0001), elapsed=120, completion_worker="Eos")
    detection(second, "plastic", 0.9, 0.7)
    # A much later validation must not extend processing time or count as a task today.
    db_session.add(MediaLog(media_id=second.id, worker_name="You", action="STATUS_CHANGE",
                            message="Status changed to READY (Detections validated by You)",
                            timestamp=NOW - timedelta(seconds=5)))
    # An unrelated later log must not extend timing either.
    db_session.add(MediaLog(media_id=first.id, worker_name="Helios", action="NOTE",
                            message="Downloaded", timestamp=NOW))
    video = media(worker="RetiredTitan", kind=MediaType.VIDEO, point=(50, 50),
                  created=NOW - timedelta(seconds=600), elapsed=300, completion_worker=None)
    for label, confidence, area, lat, lng in [
        ("glass", 0.8, 2.1, 2, 11),
        ("plastic", 0.95, 0.7, 5, 12),
        ("glass", 0.79, None, 80, 13),
    ]:
        db_session.add(VideoDetection(
            media_id=video.id, label=label, confidence=confidence, area_sqm=area,
            lat=lat, lng=lng, location=f"SRID=4326;POINT({lng} {lat})", bbox=BBOX,
            timestamp_in_video=1.0, frame_hf_path="frame.jpg",
            created_at=NOW.replace(tzinfo=None),
        ))
    media(trash=False)  # No completion log: success, but no measured latency.
    unassigned = media(worker=None, created=NOW - timedelta(seconds=700), elapsed=600,
                       completion_worker=None)
    detection(unassigned, "plastic", 0.7, 2.1)
    for status in (MediaStatus.FAILED, MediaStatus.PROCESSING):
        invalid = media(status=status, point=(20, 89))
        detection(invalid, "metal", 0.99, 1000)
    media(status=MediaStatus.UPLOADED, trash=False)
    foreign = media(owner=other_id, point=(30, 89), elapsed=1)
    detection(foreign, "metal", 1.0, 5000)
    await db_session.flush()
    return SimpleNamespace(user_id=user_id, first=first, second=second, video=video)


@pytest.mark.asyncio
async def test_detection_totals_match_composition_and_footprint(db_session, stats_data):
    composition = await get_trash_composition(db_session, stats_data.user_id)
    assert [(item.label, item.count) for item in composition] == [
        ("plastic", 4), ("glass", 2), ("metal", 1),
    ]
    assert [item.percentage for item in composition] == [57.14, 28.57, 14.29]
    footprint = await get_environmental_footprint(db_session, stats_data.user_id)
    assert footprint.total_detections == sum(item.count for item in composition) == 7
    assert footprint.total_area_sqm == pytest.approx(7.0)


@pytest.mark.asyncio
async def test_fleet_totals_keep_historical_workers_and_scope_daily_counts(db_session, stats_data):
    with patch("app.api.stats_utils.kpi_fleet_efficiency.datetime") as clock:
        clock.now.return_value = NOW
        fleet = await get_ai_fleet_efficiency(db_session, stats_data.user_id)
    workers = {worker.name: worker for worker in fleet.workers}
    assert (fleet.total_successes, fleet.total_failures) == (4, 1)
    assert fleet.fleet_reliability_score == 0.8
    assert workers["Helios"].reliability_score == 0.6667
    assert workers["Helios"].tasks_processed_today == 1
    assert workers["Eos"].tasks_processed_today == 0
    assert workers["RetiredTitan"].tasks_processed_today == 1


@pytest.mark.asyncio
async def test_processing_time_uses_upload_to_first_ready_once_per_media(db_session, stats_data):
    timing = await get_mean_time_to_process(db_session, stats_data.user_id)
    # Includes the unassigned 600-second item in the overall average.
    assert timing.overall_avg_seconds == 270
    workers = {worker.worker_name: worker for worker in timing.by_worker}
    assert {name: worker.avg_processing_seconds for name, worker in workers.items()} == {
        "Helios": 60, "Eos": 120, "RetiredTitan": 300,
    }
    assert all(worker.task_count == 1 for worker in workers.values())


@pytest.mark.asyncio
async def test_temporal_window_uses_utc_calendar_days_and_completed_reports(db_session, stats_data):
    await db_session.execute(text("SET TIME ZONE 'Pacific/Honolulu'"))
    with patch("app.api.stats_utils.kpi_temporal_trends.datetime") as clock:
        clock.now.return_value = NOW
        trends = await get_temporal_trends(db_session, stats_data.user_id, 3)
    assert [str(trend.date) for trend in trends] == ["2026-06-03", "2026-06-04", "2026-06-05"]
    assert [trend.count for trend in trends] == [1, 0, 3]


@pytest.mark.asyncio
async def test_hotspots_use_fractional_confidence_and_deduplicate_media(db_session, stats_data):
    density = await get_hotspot_density(db_session, stats_data.user_id)
    # Two nearby images share one cell; a single video adds two other cells.
    assert density.hotspot_count == 3
    assert density.high_confidence_media_count == 3


@pytest.mark.asyncio
async def test_fun_facts_use_same_completed_data_and_include_video_locations(db_session, stats_data):
    facts = {fact.icon: fact.fact for fact in await get_fun_facts(db_session, stats_data.user_id)}
    assert "3 tasks" in facts["cpu"]
    assert "1000 smartphone" in facts["maximize"]
    assert "plastic hunter" in facts["target"]
    assert "80.0°" in facts["compass"]
    assert "83%" in facts["award"]
    assert "5 successfully" in facts["award"]


@pytest.mark.asyncio
async def test_manifest_uses_same_detection_rows_and_percentage_scale(db_session, stats_data):
    with patch("app.api.stats_utils.report_manifest.datetime") as clock:
        clock.now.return_value = NOW
        rows = await generate_manifest_data(db_session, stats_data.user_id, 3)
    assert len(rows) == 7
    assert sum(row["Area (sqm)"] for row in rows if row["Area (sqm)"] != "N/A") == pytest.approx(7)
    assert sorted(row["Confidence (%)"] for row in rows) == [60, 70, 79, 80, 80, 90, 95]


@pytest.mark.asyncio
async def test_empty_user_stats_are_zero(db_session):
    user_id = "no-such-user"
    assert await get_trash_composition(db_session, user_id) == []
    assert (await get_environmental_footprint(db_session, user_id)).total_detections == 0
    assert (await get_mean_time_to_process(db_session, user_id)).overall_avg_seconds == 0
    assert (await get_hotspot_density(db_session, user_id)).hotspot_count == 0
    assert await get_fun_facts(db_session, user_id) == []


@pytest.mark.asyncio
async def test_map_grid_includes_video_points_without_detection_join_fanout(db_session, stats_data):
    from app.api.map import get_map_stats

    grid = await get_map_stats(min_lat=-90, max_lat=90, min_lng=-180, max_lng=180,
                              resolution=0.001, db=db_session,
                              current_user=SimpleNamespace(id=stats_data.user_id))
    assert grid["total"] == 4
    images = next(item for item in grid["items"] if item["lng"] == 10)
    assert images["count"] == 2
    assert images["confidence"] == 85
    assert images["density"] == 2_000_000
    assert images["label"] == "plastic"
    video = next(item for item in grid["items"] if item["lng"] == 11)
    assert video["count"] == 1
    assert video["confidence"] == 80


@pytest.mark.asyncio
async def test_dashboard_daily_throughput_includes_videos(db_session, stats_data):
    from app.api.dashboard_utils.ai_workers import get_worker_status

    with patch("app.api.dashboard_utils.ai_workers.datetime") as clock:
        clock.now.return_value = NOW
        dashboard = await get_worker_status(db_session, SimpleNamespace(id=stats_data.user_id))
    workers = {worker["name"]: worker for worker in dashboard["nodes"]}
    assert workers["Helios"]["tasks_processed_today"] == 2  # Includes the other user's completion.
    assert workers["Eos"]["tasks_processed_today"] == 0
    assert workers["RetiredTitan"]["tasks_processed_today"] == 1


@pytest.mark.asyncio
async def test_summary_executes_real_queries_on_one_session(db_session, stats_data):
    from app.api.stats import get_stats_summary
    import json

    result = await get_stats_summary(days=3, db=db_session,
                                     current_user=SimpleNamespace(id=stats_data.user_id))
    data = json.loads(result.body)
    assert data["environmental_footprint"]["total_detections"] == 7
    assert data["mean_time_to_process"]["overall_avg_seconds"] == 270
    assert data["hotspot_density"]["hotspot_count"] == 3


@pytest.mark.asyncio
async def test_temporal_window_excludes_future_uploads(db_session, stats_data):
    db_session.add(Media(id=uuid.uuid4(), uploader_id=stats_data.user_id,
                         status=MediaStatus.READY, has_trash=True,
                         created_at=NOW + timedelta(hours=1)))
    await db_session.flush()
    with patch("app.api.stats_utils.kpi_temporal_trends.datetime") as clock:
        clock.now.return_value = NOW
        trends = await get_temporal_trends(db_session, stats_data.user_id, 1)
    assert len(trends) == 1
    assert trends[0].count == 3


@pytest.mark.asyncio
async def test_northernmost_fact_preserves_zero_latitude(db_session, stats_data):
    user_id = uuid.uuid4().hex[:20]
    db_session.add(User(id=user_id, email=f"{user_id}@example.com", hashed_password="test", full_name="Equator"))
    await db_session.flush()
    db_session.add(Media(id=uuid.uuid4(), uploader_id=user_id, status=MediaStatus.READY,
                         has_trash=True, lat=0, lng=10))
    await db_session.flush()
    facts = {fact.icon: fact.fact for fact in await get_fun_facts(db_session, user_id)}
    assert "0.0°" in facts["compass"]


@pytest.mark.asyncio
async def test_success_percentage_avoids_floating_point_truncation(db_session):
    user_id = uuid.uuid4().hex[:20]
    db_session.add(User(id=user_id, email=f"{user_id}@example.com", hashed_password="test", full_name="Percentage"))
    await db_session.flush()
    db_session.add_all([
        Media(id=uuid.uuid4(), uploader_id=user_id,
              status=MediaStatus.READY if index < 29 else MediaStatus.FAILED)
        for index in range(50)
    ])
    await db_session.flush()
    facts = await get_fun_facts(db_session, user_id)
    assert len(facts) == 1
    assert "58%" in facts[0].fact
