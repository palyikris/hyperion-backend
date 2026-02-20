import asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, update, or_
from app.database import AsyncSessionLocal
from app.models.db.Media import Media
from app.models.db.AIWorker import AIWorkerState
from app.models.db.MediaLog import MediaLog
from app.models.upload.MediaStatus import MediaStatus
from app.api.upload_utils.conn_manager import worker_signal, manager


async def ai_reaper_process():
    """
    Background overseer that recovers stuck tasks and notifies workers of missed signals.
    """
    while True:
        # run every 10 minutes
        await asyncio.sleep(600)

        async with AsyncSessionLocal() as session:
            try:
                now = datetime.now(timezone.utc)
                threshold = now - timedelta(minutes=10)

                stale_uploaded = await session.execute(
                    select(Media.id).where(
                        Media.status == MediaStatus.UPLOADED,
                        Media.assigned_worker == None,
                        Media.updated_at <= threshold,
                    )
                )

                if stale_uploaded.scalars().first():
                    async with worker_signal:
                        worker_signal.notify_all()

                offline_workers_query = await session.execute(
                    select(AIWorkerState.name).where(
                        or_(
                            AIWorkerState.last_ping <= threshold,
                            AIWorkerState.last_ping == None,
                        )
                    )
                )
                offline_worker_names = offline_workers_query.scalars().all()

                if offline_worker_names:
                    zombie_tasks_query = await session.execute(
                        select(Media).where(
                            Media.status.in_(
                                [MediaStatus.EXTRACTING, MediaStatus.PROCESSING]
                            ),
                            Media.assigned_worker.in_(offline_worker_names),
                        )
                    )
                    zombie_tasks = zombie_tasks_query.scalars().all()

                    for task in zombie_tasks:
                        recovery_log = MediaLog(
                            media_id=task.id,
                            status=MediaStatus.UPLOADED,
                            worker=None,
                            timestamp=now,
                        )
                        session.add(recovery_log)
                        
                        await manager.send_status(
                            user_id=str(task.uploader_id),
                            media_id=str(task.id),
                            status=MediaStatus.UPLOADED.value,
                            worker=None,
                        )

                        task.status = MediaStatus.UPLOADED
                        task.assigned_worker = None

                    await session.commit()

            except Exception as e:
                print(f"Reaper Error: {e}")
                await session.rollback()
