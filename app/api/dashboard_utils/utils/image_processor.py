from app.api.dashboard_utils.utils.media_utils import (
    add_status_log,
    fail_media,
    build_metadata_update,
    is_image_media,
)
from app.api.upload_utils.conn_manager import manager
from app.models.db.Media import Media
from app.models.db.Detection import Detection
from app.models.upload.MediaStatus import MediaStatus
from app.database import AsyncSessionLocal
from sqlalchemy import update, select
from .hf_metadata import extract_metadata_from_hf
import logging
from app.api.ai_client import get_real_detections
from .worker_state import record_completed_task

logger = logging.getLogger(__name__)

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

    await manager.send_status(
        user_id=str(uploader_id),
        media_id=str(media_task_id),
        status=MediaStatus.PROCESSING.value,
        worker=name,
    )

    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Media).where(Media.id == media_task_id))
        task = res.scalar_one()

        if is_image_media(task):
            image_path = task.hf_path
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

    # Inference (including retries) can outlive an idle DB connection. Open the
    # persistence transaction only after the model has returned its result.
    detections = await get_real_detections(image_path)

    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Media).where(Media.id == media_task_id))
        task = res.scalar_one()

        if detections:
            orig_w = task.initial_metadata.get("width")
            orig_h = task.initial_metadata.get("height")

            detection_instances = []
            for d in detections:
                x1, y1, x2, y2 = d["bbox"]

                norm_x = x1 / orig_w
                norm_y = y1 / orig_h
                norm_w = (x2 - x1) / orig_w
                norm_h = (y2 - y1) / orig_h

                detection_instances.append(
                    Detection(
                        media_id=media_task_id,
                        label="Trash" if d["label"] == "rubbish" else d["label"],
                        confidence=d["confidence"],
                        bbox={"x": norm_x, "y": norm_y, "w": norm_w, "h": norm_h},
                    )
                )

            session.add_all(detection_instances)

            task_meta = dict(task.technical_metadata or {})
            max_confidence = max(d["confidence"] for d in detections)

            task.has_trash = True
            task.confidence = round(max_confidence * 100, 2)
            task_meta["has_trash"] = True
            task_meta["confidence"] = task.confidence
            task_meta["detections_count"] = len(detections)
            task.technical_metadata = task_meta
        else:
            task_meta = dict(task.technical_metadata or {})
            task.has_trash = False
            task.confidence = 0.0
            task_meta["has_trash"] = False
            task_meta["confidence"] = task.confidence
            task_meta["detections_count"] = 0
            task.technical_metadata = task_meta

        task.status = MediaStatus.READY

        await record_completed_task(session, name)
        add_status_log(session, task.id, MediaStatus.READY, name)
        await session.commit()
        logger.info("Saved %s detections for image %s", len(detections), media_task_id)

    await manager.send_status(
        user_id=str(uploader_id),
        media_id=str(media_task_id),
        status=MediaStatus.READY.value,
        worker=name,
    )
    return True
