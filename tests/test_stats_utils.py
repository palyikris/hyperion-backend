from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.api.dashboard_utils.utils.init_workers import TITAN_FLEET
from app.api.stats_utils.kpi_environmental_footprint import (
	get_environmental_footprint,
)
from app.api.stats_utils.kpi_fleet_efficiency import get_ai_fleet_efficiency
from app.api.stats_utils.kpi_hotspot_density import get_hotspot_density
from app.api.stats_utils.kpi_processing_time import get_mean_time_to_process
from app.api.stats_utils.kpi_temporal_trends import get_temporal_trends
from app.api.stats_utils.kpi_trash_composition import get_trash_composition


class FakeAllResult:
	def __init__(self, rows):
		self._rows = rows

	def all(self):
		return self._rows


class FakeOneResult:
	def __init__(self, row):
		self._row = row

	def one(self):
		return self._row


class FakeScalarResult:
	def __init__(self, value):
		self._value = value

	def scalar(self):
		return self._value


class FakeScalarsResult:
	def __init__(self, rows):
		self._rows = rows

	def scalars(self):
		return self

	def all(self):
		return self._rows


@pytest.mark.asyncio
async def test_trash_composition_math():
	db = AsyncMock()
	db.execute.side_effect = [
		FakeAllResult(
			[
				("plastic", 4),
				("metal", 1),
			]
		),
		FakeAllResult(
			[
				("plastic", 2),
				("glass", 3),
			]
		),
	]

	result = await get_trash_composition(db, "user-1")

	assert [item.label for item in result] == ["plastic", "glass", "metal"]
	assert [item.count for item in result] == [6, 3, 1]
	assert [item.percentage for item in result] == [60.0, 30.0, 10.0]


@pytest.mark.asyncio
async def test_environmental_footprint_math():
	db = AsyncMock()
	db.execute.side_effect = [
		FakeOneResult(SimpleNamespace(total_area=12.5, total_detections=3)),
		FakeOneResult(SimpleNamespace(total_area=7.25, total_detections=2)),
	]

	result = await get_environmental_footprint(db, "user-1")

	assert result.total_area_sqm == pytest.approx(19.75)
	assert result.total_detections == 5


@pytest.mark.asyncio
async def test_ai_fleet_efficiency_math():
	db = AsyncMock()
	db.execute.side_effect = [
		FakeAllResult(
			[
				SimpleNamespace(assigned_worker="Helios", successes=3, failures=1),
				SimpleNamespace(assigned_worker="Eos", successes=0, failures=0),
			]
		),
		FakeScalarsResult(
			[
				SimpleNamespace(name="Helios", tasks_processed_today=7),
				SimpleNamespace(name="Eos", tasks_processed_today=2),
			]
		),
	]

	result = await get_ai_fleet_efficiency(db, "user-1")
	workers_by_name = {worker.name: worker for worker in result.workers}

	assert len(result.workers) == len(TITAN_FLEET)
	assert result.total_successes == 3
	assert result.total_failures == 1
	assert result.fleet_reliability_score == 0.75
	assert workers_by_name["Helios"].success_count == 3
	assert workers_by_name["Helios"].failure_count == 1
	assert workers_by_name["Helios"].tasks_processed_today == 7
	assert workers_by_name["Helios"].reliability_score == 0.75
	assert workers_by_name["Eos"].success_count == 0
	assert workers_by_name["Eos"].failure_count == 0
	assert workers_by_name["Eos"].tasks_processed_today == 2
	assert workers_by_name["Eos"].reliability_score == 1.0


@pytest.mark.asyncio
async def test_mean_time_to_process_math():
	db = AsyncMock()
	db.execute.side_effect = [
		FakeAllResult(
			[
				SimpleNamespace(worker_name="Helios", avg_seconds=12.3456, task_count=3),
				SimpleNamespace(worker_name="Eos", avg_seconds=8.0, task_count=1),
			]
		),
		FakeScalarResult(10.556),
	]

	result = await get_mean_time_to_process(db, "user-1")
	workers_by_name = {worker.worker_name: worker for worker in result.by_worker}

	assert result.overall_avg_seconds == 10.56
	assert set(workers_by_name) == {"Helios", "Eos"}
	assert workers_by_name["Helios"].avg_processing_seconds == 12.35
	assert workers_by_name["Helios"].task_count == 3
	assert workers_by_name["Eos"].avg_processing_seconds == 8.0
	assert workers_by_name["Eos"].task_count == 1


@pytest.mark.asyncio
async def test_temporal_trends_zero_fill_math():
	db = AsyncMock()
	db.execute.return_value = FakeAllResult(
		[
			(datetime(2026, 6, 3, tzinfo=timezone.utc), 2),
			(datetime(2026, 6, 5, tzinfo=timezone.utc), 5),
		]
	)

	fixed_now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
	with patch("app.api.stats_utils.kpi_temporal_trends.datetime") as mock_datetime:
		mock_datetime.now.return_value = fixed_now

		result = await get_temporal_trends(db, "user-1", days=3)

	assert [trend.date.isoformat() for trend in result] == [
		"2026-06-03",
		"2026-06-04",
		"2026-06-05",
	]
	assert [trend.count for trend in result] == [2, 0, 5]


@pytest.mark.asyncio
async def test_hotspot_density_fallback_math():
	db = AsyncMock()
	db.execute.side_effect = [
		FakeScalarResult(2),
		FakeScalarResult(3),
		Exception("PostGIS unavailable"),
	]

	result = await get_hotspot_density(db, "user-1")

	assert result.high_confidence_media_count == 5
	assert result.hotspot_count == 5
