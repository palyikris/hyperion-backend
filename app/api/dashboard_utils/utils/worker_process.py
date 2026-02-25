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

                local_file_path = await asyncio.to_thread(
                    hf_hub_download,
                    repo_id=hf_repo_id,
                    filename=media_task.hf_path,
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

            await asyncio.sleep(20)

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

            await manager.send_status(
                user_id=str(uploader_id),
                media_id=str(media_task_id),
                status=MediaStatus.READY.value,
                worker=name,
            )

        except Exception as e:
            print(f"Worker {name} encountered an error: {e}")
            await asyncio.sleep(10)
