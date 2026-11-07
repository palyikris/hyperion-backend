import asyncio
import logging
from datetime import date, datetime, timezone

from sqlalchemy import case, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.db.AIWorker import AIWorkerState


logger = logging.getLogger(__name__)
WORKER_HEARTBEAT_INTERVAL_SECONDS = 30


def worker_timestamp() -> datetime:
    # AIWorkerState.last_ping is a TIMESTAMP WITHOUT TIME ZONE, stored as UTC.
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def maintain_worker_heartbeat(name: str) -> None:
    """Keep long-running work visible using independent, short transactions."""
    while True:
        await asyncio.sleep(WORKER_HEARTBEAT_INTERVAL_SECONDS)
        try:
            async with AsyncSessionLocal() as session:
                await session.execute(
                    update(AIWorkerState)
                    .where(AIWorkerState.name == name)
                    .values(last_ping=worker_timestamp())
                )
                await session.commit()
        except Exception:
            logger.exception("Worker heartbeat failed for %s", name)


async def record_completed_task(session: AsyncSession, name: str) -> None:
    """Count a successful image or video in its READY-state transaction."""
    today = date.today()
    await session.execute(
        update(AIWorkerState)
        .where(AIWorkerState.name == name)
        .values(
            status="Active",
            last_ping=worker_timestamp(),
            last_reset_date=today,
            tasks_processed_today=case(
                (
                    AIWorkerState.last_reset_date == today,
                    func.coalesce(AIWorkerState.tasks_processed_today, 0) + 1,
                ),
                else_=1,
            ),
        )
    )
