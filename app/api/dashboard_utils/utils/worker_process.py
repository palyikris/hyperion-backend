import asyncio
from datetime import datetime, timezone, date
from sqlalchemy import select, update
from app.database import AsyncSessionLocal
from app.models.db.Media import Media
from app.models.db.AIWorker import AIWorkerState
from app.models.upload.MediaStatus import MediaStatus


worker_signal = asyncio.Condition()

async def ai_worker_process(name: str):
    """
    Persistent background loop for a specific Titan worker.
    """

    async with worker_signal:
        await worker_signal.wait()

    while True:
        async with AsyncSessionLocal() as session:
            try:
                await session.execute(
                    update(AIWorkerState)
                    .where(AIWorkerState.name == name)
                    .values(last_ping=datetime.now(timezone.utc), status="Active")
                )
                await session.commit()

                task_query = (
                    select(Media)
                    .where(Media.status == MediaStatus.UPLOADED)
                    .where(Media.assigned_worker == None)
                    .order_by(Media.created_at.asc())
                    .limit(1)
                    .with_for_update(skip_locked=True)
                ) # find oldest uploaded that isnt assigned

                result = await session.execute(task_query)
                media_task = result.scalar_one_or_none()

                if not media_task:
                    await asyncio.sleep(10) 
                    continue

                media_task.status = MediaStatus.EXTRACTING
                media_task.assigned_worker = name

                await session.execute(
                    update(AIWorkerState)
                    .where(AIWorkerState.name == name)
                    .values(status="Working")
                )
                await session.commit()

                # --- PHASE 1: EXTRACTION (Simulation) ---
                await asyncio.sleep(5)

                media_task.status = MediaStatus.PROCESSING
                await session.commit()

                # --- PHASE 2: AI PROCESSING (Simulation) ---
                await asyncio.sleep(20)

                media_task.status = MediaStatus.READY

                await session.execute(
                    update(AIWorkerState)
                    .where(AIWorkerState.name == name)
                    .values(
                        status="Active",
                        tasks_processed_today=AIWorkerState.tasks_processed_today + 1,
                    )
                )
                await session.commit()

            except Exception as e:
                print(f"Worker {name} encountered an error: {e}")
                await session.rollback()
                await asyncio.sleep(10)
