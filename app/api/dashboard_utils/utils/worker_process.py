import asyncio
from datetime import datetime, timezone, date
from sqlalchemy import select, update, or_
from app.database import AsyncSessionLocal
from app.api.media_log_utils import create_status_change_log
from app.models.db.Media import Media
from app.models.db.AIWorker import AIWorkerState
from app.models.upload.MediaStatus import MediaStatus
from app.api.upload_utils.conn_manager import worker_signal, manager
from huggingface_hub import hf_hub_download
import os
from app.api.upload_utils.metadata_extractor import extract_media_metadata
import random
from app.models.db.Detection import Detection


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic"}
FAKE_DETECTION_LABELS = ["plastic", "metal", "glass", "paper", "trash"]


def _is_image_media(media: Media) -> bool:
    metadata = media.initial_metadata or {}
    mimetype = str(metadata.get("mime_type") or metadata.get("mimetype") or "").lower()
    if mimetype.startswith("image/"):
        return True

    hf_path = (media.hf_path or "").lower()
    return any(hf_path.endswith(ext) for ext in IMAGE_EXTENSIONS)


def _generate_fake_detections(media_id):
    has_trash = random.random() < 0.70
    if not has_trash:
        return []

    detection_count = random.randint(1, 5)
    detections: list[Detection] = []

    for _ in range(detection_count):
        x = round(random.uniform(0.0, 0.75), 4)
        y = round(random.uniform(0.0, 0.75), 4)
        w = round(random.uniform(0.1, 0.35), 4)
        h = round(random.uniform(0.1, 0.35), 4)

        detections.append(
            Detection(
                media_id=media_id,
                label=random.choice(FAKE_DETECTION_LABELS),
                confidence=round(random.uniform(0.55, 0.99), 4),
                bbox={"x": x, "y": y, "w": w, "h": h},
                is_manual=False,
                area_sqm=round(random.uniform(0.1, 4.0), 3),
            )
        )

    return detections


def _simulation_processing_delay_seconds() -> float:
    return round(random.uniform(1.5, 5.0), 2)


async def ai_worker_process(name: str):
    """
    Persistent background loop for a specific Titan worker.
    """

    while True:
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
                await asyncio.sleep(300)
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
                        await asyncio.wait_for(worker_signal.wait(), timeout=60)
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

            extraction_ok = False
            local_file_path = None
            try:
                hf_repo_id = os.getenv("HF_REPO_ID")
                hf_token = os.getenv("HF_TOKEN")

                if not hf_repo_id or not hf_token:
                    raise ValueError(
                        "HF_REPO_ID or HF_TOKEN environment variables are not set"
                    )

                if not media_task or not media_task.hf_path:
                    raise ValueError(f"Media task {media_task_id} is missing hf_path")

                original_path = media_task.hf_path.replace("_thumbnail_", "_")

                local_file_path = await asyncio.to_thread(
                    hf_hub_download,
                    repo_id=hf_repo_id,
                    filename=original_path,
                    repo_type="dataset",
                    token=hf_token,
                )

                with open(local_file_path, "rb") as f:
                    file_bytes = f.read()

                technical_meta = await asyncio.to_thread(extract_media_metadata, file_bytes)

                async with AsyncSessionLocal() as session:
                    update_values = {
                        "status": MediaStatus.PROCESSING,
                        "technical_metadata": technical_meta,
                    }

                    if "error" not in technical_meta and technical_meta.get("gps"):
                        gps_data = technical_meta["gps"]
                        if isinstance(gps_data, dict):
                            update_values["lat"] = gps_data.get("lat")
                            update_values["lng"] = gps_data.get("lng")
                            update_values["altitude"] = gps_data.get("altitude")
                            update_values["address"] = gps_data.get("address")

                    await session.execute(
                        update(Media)
                        .where(Media.id == media_task_id)
                        .values(**update_values)
                    )
                    session.add(
                        create_status_change_log(
                            media_id=media_task_id,
                            status=MediaStatus.PROCESSING,
                            worker_name=name,
                            detail="Extracted EXIF and GPS data",
                        )
                    )
                    await session.commit()

                extraction_ok = True
            except Exception as e:
                print(f"Extraction Error for {media_task_id}: {e}")
                async with AsyncSessionLocal() as session:
                    await session.execute(
                        update(Media)
                        .where(Media.id == media_task_id)
                        .values(status=MediaStatus.FAILED)
                    )
                    session.add(
                        create_status_change_log(
                            media_id=media_task_id,
                            status=MediaStatus.FAILED,
                            worker_name=name,
                            detail=f"Extraction failed: {e}",
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
                )
            finally:
                if local_file_path and os.path.exists(local_file_path):
                    os.remove(local_file_path)

            if not extraction_ok:
                await asyncio.sleep(5)
                continue

            await manager.send_status(
                user_id=str(uploader_id),
                media_id=str(media_task_id),
                status=MediaStatus.PROCESSING.value,
                worker=name,
            )

            await asyncio.sleep(_simulation_processing_delay_seconds())

            async with AsyncSessionLocal() as session:
                res = await session.execute(
                    select(Media).where(Media.id == media_task_id)
                )
                task = res.scalar_one()

                fake_detections = []
                if _is_image_media(task):
                    fake_detections = _generate_fake_detections(task.id)

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
                session.add(
                    create_status_change_log(
                        media_id=task.id,
                        status=MediaStatus.READY,
                        worker_name=name,
                    )
                )
                await session.commit()

            await manager.send_status(
                user_id=str(uploader_id),
                media_id=str(media_task_id),
                status=MediaStatus.READY.value,
                worker=name,
            )

        except Exception as e:
            print(f"Worker {name} encountered an error: {e}")
            await asyncio.sleep(10)
