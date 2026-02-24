import asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, update, or_
from app.database import AsyncSessionLocal
from app.api.media_log_utils import create_status_change_log
from app.models.db.Media import Media
from app.models.db.MediaLog import MediaLog
from app.models.db.AIWorker import AIWorkerState
from app.models.upload.MediaStatus import MediaStatus
from app.api.upload_utils.conn_manager import worker_signal, manager


async def _build_pending_timeout_detail(session, media_id):
    latest_log_result = await session.execute(
        select(MediaLog.message)
        .where(MediaLog.media_id == media_id)
        .order_by(MediaLog.timestamp.desc())
        .limit(1)
    )
    latest_log = latest_log_result.scalar_one_or_none()

    inferred_step = "during HF upload transfer"
    if latest_log:
        normalized_log = latest_log.upper()
        if "UPLOADED" in normalized_log:
            inferred_step = "during status persistence after HF upload"
        elif "PENDING" in normalized_log:
            inferred_step = "during upload transfer"
        elif "FAILED" in normalized_log:
            inferred_step = "while recovering from previous failure"
        else:
            inferred_step = "at unknown pipeline step"

    if latest_log:
        return (
            f"reaper pending timeout: failed {inferred_step} (last log: {latest_log})"
        )
    return f"reaper pending timeout: failed {inferred_step}"


async def ai_reaper_process():
    """
    Background overseer that recovers stuck tasks and notifies workers of missed signals.
    """
    while True:
        # run every 10 minutes
        await asyncio.sleep(600)

        if manager.is_hf_rate_limited():
            await asyncio.sleep(300)
            continue

        async with AsyncSessionLocal() as session:
            try:
                now = datetime.now(timezone.utc)
                threshold = now - timedelta(minutes=10)
                pending_threshold = now - timedelta(minutes=15)
                changes_made = False

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

                stale_pending_query = await session.execute(
                    select(Media).where(
                        Media.status == MediaStatus.PENDING,
                        Media.updated_at <= pending_threshold,
                    )
                )
                stale_pending_tasks = stale_pending_query.scalars().all()

                for task in stale_pending_tasks:
                    failure_detail = await _build_pending_timeout_detail(
                        session, task.id
                    )
                    failure_log = create_status_change_log(
                        media_id=task.id,
                        status=MediaStatus.FAILED,
                        detail=failure_detail,
                    )
                    session.add(failure_log)

                    await manager.send_status(
                        user_id=str(task.uploader_id),
                        media_id=str(task.id),
                        status=MediaStatus.FAILED.value,
                        worker=None,
                    )

                    task.status = MediaStatus.FAILED
                    task.assigned_worker = None
                    changes_made = True

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
                        recovery_log = create_status_change_log(
                            media_id=task.id,
                            status=MediaStatus.UPLOADED,
                            detail="reaper recovery",
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
                        changes_made = True

                if changes_made:
                    await session.commit()

            except Exception as e:
                print(f"Reaper Error: {e}")
                await session.rollback()
