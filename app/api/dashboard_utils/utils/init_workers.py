# app/api/dashboard_utils/worker_init.py
from sqlalchemy.dialects.postgresql import insert
from app.database import AsyncSessionLocal
from app.models.db.AIWorker import AIWorkerState
from datetime import datetime, timezone
from app.api.dashboard_utils.utils.worker_process import ai_worker_process
from app.api.dashboard_utils.utils.reaper_process import ai_reaper_process
import asyncio

TITAN_FLEET = [
    "Helios",
    "Eos",
    "Aethon",
    "Crius",
    "Iapetus",
    "Perses",
    "Phlegon",
    "Phoebe",
    "Theia",
    "Cronus",
]


async def initialize_worker_fleet():
    """Ensures all workers exist in the DB without overwriting current progress."""
    async with AsyncSessionLocal() as session:
        async with session.begin():
            for name in TITAN_FLEET:
                stmt = (
                    insert(AIWorkerState)
                    .values(
                        name=name, status="Active", last_ping=datetime.now(timezone.utc)
                    )
                    .on_conflict_do_nothing() # keeps existing records intact
                ) 
                await session.execute(stmt)

    for name in TITAN_FLEET:
        asyncio.create_task(ai_worker_process(name))

    asyncio.create_task(ai_reaper_process())
