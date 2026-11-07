import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import Column, DateTime, Enum, MetaData, String, Table, Uuid, create_engine, insert

from app.api.dashboard_utils import ai_workers
from app.models.db.AIWorker import AIWorkerState
from app.models.upload.MediaStatus import MediaStatus


@pytest.mark.asyncio
@pytest.mark.parametrize("fleet_size", [1, 10])
async def test_worker_tasks_are_owner_scoped_and_loaded_in_one_batch(monkeypatch, fleet_size):
    engine = create_engine("sqlite://")
    table = Table("media", MetaData(), Column("id", Uuid, primary_key=True),
                  Column("uploader_id", String), Column("assigned_worker", String),
                  Column("status", Enum(MediaStatus)), Column("created_at", DateTime))
    table.create(engine)
    now = datetime.now(timezone.utc)
    own_id = uuid.uuid4()
    workers = [SimpleNamespace(name=f"worker-{index}", last_ping=now, status="Working",
                               tasks_processed_today=2) for index in range(fleet_size)]
    statements = []
    monkeypatch.setattr(ai_workers, "get_queue_depth", AsyncMock(return_value=3))
    try:
        with engine.begin() as connection:
            connection.execute(insert(table), [
                {"id": own_id, "uploader_id": "owner", "assigned_worker": "worker-0",
                 "status": MediaStatus.PROCESSING, "created_at": now},
                {"id": uuid.uuid4(), "uploader_id": "other", "assigned_worker": "worker-0",
                 "status": MediaStatus.PROCESSING, "created_at": now - timedelta(seconds=2)},
                {"id": uuid.uuid4(), "uploader_id": "owner", "assigned_worker": "worker-0",
                 "status": MediaStatus.READY, "created_at": now - timedelta(seconds=1)},
            ])

            class Session:
                async def execute(self, statement):
                    statements.append(statement)
                    if statement.column_descriptions[0]["expr"] is AIWorkerState:
                        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: workers))
                    return connection.execute(statement)

            result = await ai_workers.get_worker_status(db=Session(), current_user=SimpleNamespace(id="owner"))
        assert len(statements) == 2
        assert result["nodes"][0]["current_task_id"] == str(own_id)
        assert result["nodes"][0]["current_task_status"] == "PROCESSING"
        assert all(node["current_task_id"] is None for node in result["nodes"][1:])
    finally:
        engine.dispose()
