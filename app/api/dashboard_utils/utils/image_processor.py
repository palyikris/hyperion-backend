from app.api.dashboard_utils.utils.media_utils import (
    add_status_log,
    fail_media,
    build_metadata_update,
    detect_duplicate,
    is_image_media,
    generate_fake_detections,
    simulation_processing_delay_seconds,
)
from app.api.upload_utils.conn_manager import manager
from app.models.db.Media import Media
from app.models.upload.MediaStatus import MediaStatus
from app.models.db.AIWorker import AIWorkerState
from app.database import AsyncSessionLocal
from sqlalchemy import update, select
from .hf_metadata import extract_metadata_from_hf
import asyncio

DUPLICATE_DISTANCE_METERS = 10

async def process_image_media(media_task, name, uploader_id):
    """
    Process an image media item.
    """
    media_task_id = media_task.id

    try:
        technical_meta = await extract_metadata_from_hf(media_task)
        async with AsyncSessionLocal() as session:
            update_values = build_metadata_update(technical_meta)
            await session.execute(
                update(Media).where(Media.id == media_task_id).values(**update_values)
            )
            add_status_log(
                session,
                media_task_id,
                MediaStatus.PROCESSING,
                name,
                "Extracted EXIF and GPS data",
            )
            await session.commit()
    except Exception as e:
        print(f"Extraction Error for {media_task_id}: {e}")
        extraction_failed_reason = "Unable to read image metadata or GPS data. Please ensure the file is a valid image."
        async with AsyncSessionLocal() as session:
            await fail_media(
                session,
                media_task_id,
                extraction_failed_reason,
                name,
                uploader_id,
                f"Extraction failed: {e}",
            )
        return False

    # second duplicate guard: only runs after EXIF/GPS extraction so can
    # reject spatial duplicates that passed the filename-only upload check.

    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Media).where(Media.id == media_task_id))
        current_task = res.scalar_one_or_none()
        duplicate_found, duplicate, duplicate_reason = await detect_duplicate(
            session, media_task_id, uploader_id, current_task, DUPLICATE_DISTANCE_METERS
        )
        if duplicate_found:

            if not current_task:
                print(
                    f"Duplicate detected for media {media_task_id} but current task not found in DB. This should not happen."
                )
                return False

            if not duplicate:
                print(
                    f"Duplicate detected for media {media_task_id} but duplicate record not found in DB. This should not happen."
                )
                return False

            current_task.status = MediaStatus.FAILED
            current_task.failed_reason = duplicate_reason
            current_task.original_media_id = duplicate.id
            add_status_log(
                session, media_task_id, MediaStatus.FAILED, name, "Duplicate detected"
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
                failed_reason=duplicate_reason,
            )
            return False

    await manager.send_status(
        user_id=str(uploader_id),
        media_id=str(media_task_id),
        status=MediaStatus.PROCESSING.value,
        worker=name,
    )

    await asyncio.sleep(simulation_processing_delay_seconds())

    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Media).where(Media.id == media_task_id))
        task = res.scalar_one()

        fake_detections = []
        if is_image_media(task):
            fake_detections = generate_fake_detections(task.id)
        else:
            await fail_media(
                session,
                media_task_id,
                "Unsupported media type for AI processing",
                name,
                uploader_id,
                "Unsupported media type for AI processing",
            )
            return False

        if fake_detections:
            session.add_all(fake_detections)

            task_meta = task.technical_metadata or {}
            max_confidence = max(d.confidence for d in fake_detections)
            task.has_trash = True
            task.confidence = round(max_confidence * 100, 2)
            task_meta["has_trash"] = True
            task_meta["confidence"] = task.confidence
            task_meta["detections_count"] = len(fake_detections)
            task.technical_metadata = task_meta
        else:
            task_meta = task.technical_metadata or {}
            task.has_trash = False
            task.confidence = 0.0
            task_meta["has_trash"] = False
            task_meta["confidence"] = task.confidence
            task_meta["detections_count"] = 0
            task.technical_metadata = task_meta

        task.status = MediaStatus.READY

        await session.execute(
            update(AIWorkerState)
            .where(AIWorkerState.name == name)
            .values(
                status="Active",
                tasks_processed_today=AIWorkerState.tasks_processed_today + 1,
            )
        )
        add_status_log(session, task.id, MediaStatus.READY, name)
        await session.commit()

    await manager.send_status(
        user_id=str(uploader_id),
        media_id=str(media_task_id),
        status=MediaStatus.READY.value,
        worker=name,
    )
    return True
