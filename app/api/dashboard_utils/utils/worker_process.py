import asyncio
from datetime import datetime, timezone, date
from sqlalchemy import select, update, or_
from app.database import AsyncSessionLocal
from app.api.media_log_utils import create_status_change_log
from app.models.db.Media import Media
from app.models.db.AIWorker import AIWorkerState
from app.models.upload.MediaStatus import MediaStatus
from app.api.upload_utils.conn_manager import worker_signal, manager


async def ai_worker_process(name: str):
    """
    Persistent background loop for a specific Titan worker.
    Javított verzió: kiküszöböli a holtpontokat és a szálak leállását.
    """

    while True:
        try:
            async with AsyncSessionLocal() as session:
                today = date.today()

                await session.execute(
                    update(AIWorkerState)
                    .where(AIWorkerState.name == name)
                    .where(
                        or_(
                            AIWorkerState.last_reset_date.is_(None),
                            AIWorkerState.last_reset_date != today,
                        )
                    )
                    .values(tasks_processed_today=0, last_reset_date=today)
                )

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
                )

                result = await session.execute(task_query)
                media_task = result.scalar_one_or_none()

                if not media_task:
                    media_task_id = None
                    uploader_id = None
                else:
                    media_task_id = media_task.id
                    uploader_id = media_task.uploader_id

                    media_task.status = MediaStatus.EXTRACTING
                    media_task.assigned_worker = name

                    session.add(
                        create_status_change_log(
                            media_id=media_task.id,
                            status=MediaStatus.EXTRACTING,
                            worker_name=name,
                        )
                    )

                    await session.execute(
                        update(AIWorkerState)
                        .where(AIWorkerState.name == name)
                        .values(status="Working")
                    )
                    await session.commit()

            if not media_task_id:
                async with worker_signal:
                    try:
                        # 60 sec timeout: if no new task, update worker status
                        await asyncio.wait_for(worker_signal.wait(), timeout=60)
                    except asyncio.TimeoutError:
                        pass
                continue

            await manager.send_status(
                user_id=str(uploader_id),
                media_id=str(media_task_id),
                status=MediaStatus.EXTRACTING.value,
                worker=name,
            )

            await asyncio.sleep(5)

            async with AsyncSessionLocal() as session:
                res = await session.execute(
                    select(Media).where(Media.id == media_task_id)
                )
                task = res.scalar_one()
                task.status = MediaStatus.PROCESSING

                session.add(
                    create_status_change_log(
                        media_id=task.id,
                        status=MediaStatus.PROCESSING,
                        worker_name=name,
                    )
                )
                await session.commit()

            await manager.send_status(
                user_id=str(uploader_id),
                media_id=str(media_task_id),
                status=MediaStatus.PROCESSING.value,
                worker=name,
            )

            await asyncio.sleep(20)

            # --- FINALIZE: READY ---
            async with AsyncSessionLocal() as session:
                res = await session.execute(
                    select(Media).where(Media.id == media_task_id)
                )
                task = res.scalar_one()
                task.status = MediaStatus.READY

                await session.execute(
                    update(AIWorkerState)
                    .where(AIWorkerState.name == name)
                    .values(
                        status="Active",
                        tasks_processed_today=AIWorkerState.tasks_processed_today + 1,
                    )
                )
                session.add(
                    create_status_change_log(
                        media_id=task.id,
                        status=MediaStatus.READY,
                        worker_name=name,
                    )
                )
                await session.commit()

        except Exception as e:
            print(f"Worker {name} encountered an error: {e}")
            await asyncio.sleep(10)
