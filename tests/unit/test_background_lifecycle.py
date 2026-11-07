import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from sqlalchemy.dialects import postgresql

import main
from app.api.dashboard_utils.utils import reaper_process
from app.api.dashboard_utils.utils import init_workers
from app.models.upload.MediaStatus import MediaStatus


@pytest.fixture
def lifecycle_dependencies(monkeypatch):
    @asynccontextmanager
    async def session():
        yield SimpleNamespace(commit=AsyncMock())

    monkeypatch.setattr(main, "AsyncSessionLocal", session)
    monkeypatch.setattr(main, "prune_expired_blacklisted_tokens", AsyncMock())
    dispose = AsyncMock()
    monkeypatch.setattr(main, "engine", SimpleNamespace(dispose=dispose))
    return dispose


@pytest.mark.asyncio
async def test_lifespan_awaits_initialization_and_stops_every_owned_task(monkeypatch, lifecycle_dependencies):
    tasks = []
    stopped = []

    async def background(name):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(name)

    async def initialize():
        tasks.extend(asyncio.create_task(background(name)) for name in ["worker", "reaper"])
        return tasks.copy()

    monkeypatch.setattr(main, "initialize_worker_fleet", initialize)
    monkeypatch.setattr(main, "_blacklist_pruner", lambda: background("blacklist"))
    monkeypatch.setattr(main, "_video_temp_cleaner", lambda: background("video"))
    app = FastAPI()
    async with main.lifespan(app):
        await asyncio.sleep(0)
        assert len(app.state.background_tasks) == 4
        assert not any(task.done() for task in app.state.background_tasks)
        lifecycle_dependencies.assert_not_awaited()
    assert all(task.done() for task in app.state.background_tasks)
    assert set(stopped) == {"worker", "reaper", "blacklist", "video"}
    lifecycle_dependencies.assert_awaited_once()


@pytest.mark.asyncio
async def test_initialization_failure_prevents_serving_and_disposes_pool(monkeypatch, lifecycle_dependencies):
    monkeypatch.setattr(main, "initialize_worker_fleet", AsyncMock(side_effect=RuntimeError("unavailable")))
    with pytest.raises(RuntimeError, match="unavailable"):
        async with main.lifespan(FastAPI()):
            pytest.fail("Traffic must not be served after startup failed")
    lifecycle_dependencies.assert_awaited_once()


@pytest.mark.asyncio
async def test_fleet_initializer_returns_all_workers_and_the_reaper(monkeypatch):
    execute = AsyncMock()

    @asynccontextmanager
    async def transaction():
        yield

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(begin=transaction, execute=execute)

    async def background(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(init_workers, "AsyncSessionLocal", session)
    monkeypatch.setattr(init_workers, "ai_worker_process", background)
    monkeypatch.setattr(init_workers, "ai_reaper_process", background)
    tasks = await init_workers.initialize_worker_fleet()
    try:
        assert len(tasks) == len(init_workers.TITAN_FLEET) + 1
        assert execute.await_count == len(init_workers.TITAN_FLEET)
        assert tasks[-1].get_name() == "ai-reaper"
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_blacklist_pruner_retries_after_a_database_error(monkeypatch, lifecycle_dependencies, caplog):
    prune = AsyncMock(side_effect=[RuntimeError("private-database-error"), asyncio.CancelledError()])
    monkeypatch.setattr(main, "prune_expired_blacklisted_tokens", prune)
    monkeypatch.setattr(main, "BLACKLIST_PRUNE_INTERVAL_SECONDS", 0)
    with pytest.raises(asyncio.CancelledError):
        await main._blacklist_pruner()
    assert prune.await_count == 2
    assert "private-database-error" not in caplog.text


@pytest.mark.asyncio
async def test_video_cleaner_retries_after_a_filesystem_error(monkeypatch):
    work = AsyncMock(side_effect=[OSError("denied"), asyncio.CancelledError()])
    monkeypatch.setattr(main.asyncio, "to_thread", work)
    monkeypatch.setattr(main, "VIDEO_CLEANUP_INTERVAL_SECONDS", 0)
    with pytest.raises(asyncio.CancelledError):
        await main._video_temp_cleaner()
    assert work.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_fails", [False, True])
@pytest.mark.parametrize("kind", ["pending", "zombie"])
async def test_reaper_notifies_only_after_commit_and_unlocks_rows(monkeypatch, commit_fails, kind):
    events = []
    statements = []
    record = SimpleNamespace(id="stale", uploader_id="owner", status=MediaStatus.PENDING,
                             assigned_worker="Helios", failed_reason=None)

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return self

        def all(self):
            return self.rows

        def first(self):
            return self.rows[0] if self.rows else None

    class Session:
        async def execute(self, statement):
            statements.append(statement)
            index = len(statements)
            if kind == "pending" and index == 2 or kind == "zombie" and index == 4:
                return Result([record])
            return Result(["Helios"] if kind == "zombie" and index == 3 else [])

        def add(self, value):
            pass

        async def commit(self):
            events.append("commit")
            if commit_fails:
                raise RuntimeError("commit failed")

        async def rollback(self):
            events.append("rollback")

    @asynccontextmanager
    async def session():
        try:
            yield Session()
        finally:
            events.append("closed")

    sleeps = 0

    async def sleep_once(_):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError()

    async def notify(**values):
        assert events == ["commit", "closed"]
        events.append("notify")

    monkeypatch.setattr(reaper_process.asyncio, "sleep", sleep_once)
    monkeypatch.setattr(reaper_process, "AsyncSessionLocal", session)
    monkeypatch.setattr(reaper_process, "_build_pending_timeout_detail", AsyncMock(return_value=("timeout", "Upload timeout")))
    monkeypatch.setattr(reaper_process, "manager", SimpleNamespace(
        is_hf_rate_limited=lambda: False, send_status=notify,
    ))
    with pytest.raises(asyncio.CancelledError):
        await reaper_process.ai_reaper_process()
    assert ("notify" in events) == (not commit_fails)
    if commit_fails:
        assert "rollback" in events
    query = str(statements[1 if kind == "pending" else 3].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE SKIP LOCKED" in query
