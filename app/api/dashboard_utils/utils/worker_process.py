import asyncio
from datetime import datetime, timezone, date
from sqlalchemy import select, update, or_, func, and_
from sqlalchemy.sql import text
from geoalchemy2.functions import ST_DWithin, ST_SetSRID, ST_MakePoint
from datetime import timedelta
from app.database import AsyncSessionLocal
from app.api.medialog_utils.media_log_utils import create_status_change_log
from app.models.db.Media import Media
from app.models.db.AIWorker import AIWorkerState
from app.models.upload.MediaStatus import MediaStatus
from app.api.upload_utils.conn_manager import worker_signal, manager
from app.models.db.Detection import Detection
from app.models.db.Media import MediaType

from .image_processor import process_image_media
from .video_processor import process_video_media
from .hf_metadata import extract_metadata_from_hf


WORKER_IDLE_WAIT_SECONDS = 60
WORKER_RATE_LIMIT_BACKOFF_SECONDS = 300
WORKER_ERROR_RETRY_SECONDS = 10
EXTRACTION_RETRY_DELAY_SECONDS = 5
DUPLICATE_DISTANCE_METERS = 10


async def ai_worker_process(name: str):
    """
    Persistent background loop for a specific Titan worker.
    """
    while True:
        media_task_id = None
        uploader_id = None
        try:
            if manager.is_hf_rate_limited():
                async with AsyncSessionLocal() as session:
                    await session.execute(
                        update(AIWorkerState)
                        .where(AIWorkerState.name == name)
                        .values(
                            last_ping=datetime.now(timezone.utc),
                            status="Paused (Rate Limited)",
                        )
                    )
                    await session.commit()
                await asyncio.sleep(WORKER_RATE_LIMIT_BACKOFF_SECONDS)
                continue

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
                    .with_for_update(
                        skip_locked=True
                    )  # ensures two workers don't pick the same task
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
                        await asyncio.wait_for(
                            worker_signal.wait(), timeout=WORKER_IDLE_WAIT_SECONDS
                        )
                    except asyncio.TimeoutError:
                        pass
                continue

            await manager.send_status(
                user_id=str(uploader_id),
                media_id=str(media_task_id),
                status=MediaStatus.EXTRACTING.value,
                worker=name,
                address=media_task.address if media_task else None,
            )

            if not media_task:
                continue

            if media_task.media_type == MediaType.IMAGE:
                result = await process_image_media(media_task, name, uploader_id)
                if not result:
                    await asyncio.sleep(EXTRACTION_RETRY_DELAY_SECONDS)
                    continue
            elif media_task.media_type == MediaType.VIDEO:
                await process_video_media(media_task, name, uploader_id)
                # For now, just continue after placeholder
                continue
            else:
                # Unknown or unsupported media type
                async with AsyncSessionLocal() as session:
                    await session.execute(
                        update(Media)
                        .where(Media.id == media_task_id)
                        .values(
                            status=MediaStatus.FAILED,
                            failed_reason="Unsupported media type for AI processing",
                        )
                    )
                    session.add(
                        create_status_change_log(
                            media_id=media_task_id,
                            status=MediaStatus.FAILED,
                            worker_name=name,
                            detail="Unsupported media type for AI processing",
                        )
                    )
                    await session.execute(
                        update(AIWorkerState)
                        .where(AIWorkerState.name == name)
                        .values(status="Active")
                    )
                    await session.commit()

                await manager.send_status(
                    user_id=str(uploader_id),
                    media_id=str(media_task_id),
                    status=MediaStatus.FAILED.value,
                    worker=name,
                    failed_reason="Unsupported media type for AI processing",
                )
                continue

        except Exception as e:
            print(f"Worker {name} encountered an error: {e}")
            # Handle AI processing failures
            if media_task_id and uploader_id:
                processing_failed_reason = "The AI analysis engine failed to process this image. Our team has been notified."
                try:
                    async with AsyncSessionLocal() as session:
                        await session.execute(
                            update(Media)
                            .where(Media.id == media_task_id)
                            .values(
                                status=MediaStatus.FAILED,
                                failed_reason=processing_failed_reason,
                            )
                        )
                        session.add(
                            create_status_change_log(
                                media_id=media_task_id,
                                status=MediaStatus.FAILED,
                                worker_name=name,
                                detail=f"Processing failed: {e}",
                            )
                        )
                        await session.commit()

                    await manager.send_status(
                        user_id=str(uploader_id),
                        media_id=str(media_task_id),
                        status=MediaStatus.FAILED.value,
                        worker=name,
                        failed_reason=processing_failed_reason,
                    )
                except Exception as inner_e:
                    print(f"Worker {name} failed to update failure status: {inner_e}")
            await asyncio.sleep(WORKER_ERROR_RETRY_SECONDS)
