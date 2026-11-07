import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy import Column, DateTime, Enum, MetaData, String, Table, Uuid
import uuid

from app.api.ai_client import AIInferenceError
from app.api.dashboard_utils.utils import worker_process, worker_state
from app.models.db.AIWorker import AIWorkerState
from app.models.db.Media import MediaType
from app.models.upload.MediaStatus import MediaStatus


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeSession:
    def __init__(self, media=None):
        self.media = media
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, statement):
        self.statements.append(statement)
        return ScalarResult(self.media)

    def add(self, value):
        pass

    async def commit(self):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("reset_day", [date.today(), date.today() - timedelta(days=1)])
async def test_completion_counter_handles_day_change(reset_day):
    engine = create_engine("sqlite://")
    table = AIWorkerState.__table__
    table.create(engine)
    try:
        with engine.begin() as connection:
            connection.execute(
                insert(table).values(
                    name="Helios",
                    status="Working",
                    tasks_processed_today=7,
                    last_reset_date=reset_day,
                )
            )

            class CounterSession:
                async def execute(self, statement):
                    return connection.execute(statement)

            await worker_state.record_completed_task(CounterSession(), "Helios")
            record = connection.execute(select(table)).mappings().one()

        assert record["tasks_processed_today"] == (8 if reset_day == date.today() else 1)
        assert record["last_reset_date"] == date.today()
        assert record["status"] == "Active"
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_heartbeat_continues_while_processor_is_busy(monkeypatch):
    task_record = SimpleNamespace(
        id="media-1",
        uploader_id="user-1",
        media_type=MediaType.VIDEO,
        status=MediaStatus.UPLOADED,
        assigned_worker=None,
        address=None,
    )
    claim_session = FakeSession(task_record)
    heartbeat_session = FakeSession()
    heartbeat_received = asyncio.Event()
    processing_started = asyncio.Event()
    timestamps = []

    async def heartbeat_commit():
        statement = heartbeat_session.statements[-1]
        values = statement.compile().params
        timestamps.append(values["last_ping"])
        assert "status" not in values
        if len(timestamps) >= 2:
            heartbeat_received.set()

    heartbeat_session.commit = heartbeat_commit

    async def long_video_processing(media):
        processing_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(worker_process, "AsyncSessionLocal", lambda: claim_session)
    monkeypatch.setattr(worker_state, "AsyncSessionLocal", lambda: heartbeat_session)
    monkeypatch.setattr(worker_state, "WORKER_HEARTBEAT_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(worker_process, "process_video_media", long_video_processing)
    monkeypatch.setattr(worker_process.manager, "send_status", AsyncMock())
    task = asyncio.create_task(worker_process.ai_worker_process("Helios"))
    try:
        await asyncio.wait_for(processing_started.wait(), timeout=2)
        await asyncio.wait_for(heartbeat_received.wait(), timeout=2)
        assert timestamps[-1] > timestamps[0]
        assert timestamps[-1].tzinfo is None
        assert (
            datetime.now(timezone.utc).replace(tzinfo=None) - timestamps[-1]
            < timedelta(seconds=1)
        )
        assert task_record.status == MediaStatus.EXTRACTING
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert not any(
        task.get_name() == "worker-heartbeat-Helios"
        for task in asyncio.all_tasks()
    )


@pytest.mark.asyncio
async def test_inference_failure_marks_claimed_image_failed(monkeypatch):
    record = SimpleNamespace(
        id="media-1",
        uploader_id="user-1",
        media_type=MediaType.IMAGE,
        status=MediaStatus.UPLOADED,
        assigned_worker=None,
        address=None,
    )
    session = FakeSession(record)
    failed = asyncio.Event()
    reason = "AI analysis service is unavailable. Please try again later."

    async def send_status(**values):
        if values["status"] == "FAILED":
            assert values["failed_reason"] == reason
            failed.set()

    monkeypatch.setattr(worker_process, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(worker_process.manager, "send_status", send_status)
    monkeypatch.setattr(
        worker_process,
        "process_image_media",
        AsyncMock(side_effect=AIInferenceError(reason)),
    )
    task = asyncio.create_task(worker_process.ai_worker_process("Helios"))
    try:
        await asyncio.wait_for(failed.wait(), timeout=2)
        updates = [
            statement.compile().params
            for statement in session.statements
            if getattr(statement, "is_update", False)
        ]
        assert any(values.get("status") == MediaStatus.FAILED for values in updates)
        assert not any(values.get("status") == MediaStatus.READY for values in updates)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
@pytest.mark.parametrize("status,assigned_worker,should_fail", [
    (MediaStatus.EXTRACTING, "Helios", True),
    (MediaStatus.PROCESSING, "Helios", True),
    (MediaStatus.READY, "Helios", False),
    (MediaStatus.PROCESSING, "Eos", False),
])
async def test_shutdown_closes_only_its_own_unfinished_claim(monkeypatch, status, assigned_worker, should_fail):
    engine = create_engine("sqlite://")
    table = Table("media", MetaData(), Column("id", Uuid, primary_key=True),
                  Column("status", Enum(MediaStatus)), Column("assigned_worker", String),
                  Column("failed_reason", String), Column("updated_at", DateTime))
    table.create(engine)
    media_id = uuid.uuid4()
    events = []
    monkeypatch.setattr(worker_process.manager, "send_status", AsyncMock())
    try:
        with engine.begin() as connection:
            connection.execute(insert(table), {"id": media_id, "status": status, "assigned_worker": assigned_worker})

            class Session:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    events.append("closed")

                async def execute(self, statement):
                    return connection.execute(statement)

                def add(self, value):
                    events.append("log")

                async def commit(self):
                    events.append("commit")

            monkeypatch.setattr(worker_process, "AsyncSessionLocal", Session)
            await worker_process.fail_interrupted_task(media_id, "owner", "Helios")
            saved = connection.execute(select(table)).mappings().one()
        assert saved["status"] == (MediaStatus.FAILED if should_fail else status)
        assert ("commit" in events) == should_fail
        assert worker_process.manager.send_status.await_count == int(should_fail)
        assert events[-1] == "closed"
    finally:
        engine.dispose()
