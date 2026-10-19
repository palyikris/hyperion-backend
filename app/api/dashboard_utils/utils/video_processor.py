import cv2
import os
import tempfile
import asyncio
import math
from datetime import datetime, timezone
from sqlalchemy import select, update
from app.database import AsyncSessionLocal
from app.models.db.Media import Media
from app.models.db.VideoDetection import VideoDetection
from app.models.upload.MediaStatus import MediaStatus

from app.api.upload_utils.hf_upload import (
    upload_video_frames_to_hf,
    delete_video_from_hf,
)
from app.api.upload_utils.telemetry import (
    extract_srt_from_video,
    get_location_at_timestamp,
    MissingTelemetryError,
)
from app.api.upload_utils.conn_manager import manager
from app.api.medialog_utils.media_log_utils import create_status_change_log
from huggingface_hub import hf_hub_download
from app.api.upload_utils.metadata_extractor import get_address_from_coords
from app.api.ai_client import get_real_detections


import logging

logger = logging.getLogger(__name__)

IMAGE_CHECK_INTERVAL_SECONDS = 3.0  # (secs)


async def process_video_media(
    media_task,
) -> None:
    """
    Main async pipeline for processing drone video files.
    """
    user_id = media_task.uploader_id
    media_id = media_task.id
    hf_full_video_path = (
        media_task.technical_metadata.get("hf_full_video_path")
        if media_task.technical_metadata
        else ""
    )
    local_video_path = None
    cap = None
    temporary_frames = set()
    delete_source_video = False
    HF_REPO_ID = os.getenv("HF_REPO_ID")
    HF_TOKEN = os.getenv("HF_TOKEN")

    try:

        def download_from_hf():
            if HF_REPO_ID is None:
                raise ValueError("HF_REPO_ID environment variable is not set.")
            if not hf_full_video_path:
                raise ValueError("hf_full_video_path is missing in tech_metadata.")
            return hf_hub_download(
                repo_id=HF_REPO_ID,
                filename=hf_full_video_path,
                repo_type="dataset",
                token=HF_TOKEN,
                local_dir=tempfile.gettempdir(),  # Saves to /tmp/media/user_id/...
            )

        logger.info(f"Worker downloading video from HF: {hf_full_video_path}")
        local_video_path = await asyncio.to_thread(download_from_hf)

        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        async with AsyncSessionLocal() as session:
            media_result = await session.execute(
                select(Media).where(Media.id == media_id)
            )
            media = media_result.scalar_one_or_none()
            if not media:
                logger.error(f"Media {media_id} not found.")
                return

            filename = media.initial_metadata.get("filename")

            if filename:
                duplicate_query = (
                    select(Media)
                    .where(
                        Media.uploader_id == user_id,
                        Media.id != media_id,
                        Media.status == MediaStatus.READY,
                        Media.initial_metadata["filename"].as_string() == str(filename),
                    )
                    .limit(1)
                )

                duplicate_result = await session.execute(duplicate_query)
                duplicate_media = duplicate_result.scalars().first()

                if duplicate_media:
                    logger.info(
                        f"Duplicate video found for {media_id}. Cancelling processing."
                    )
                    media.status = MediaStatus.FAILED
                    media.failed_reason = f"Duplicate of {filename}"

                    # Optional: link to original if your DB supports it
                    if hasattr(media, "original_media_id"):
                        media.original_media_id = duplicate_media.id

                    session.add(
                        create_status_change_log(
                            media.id, MediaStatus.FAILED, detail="Duplicate detected"
                        )
                    )
                    await session.commit()
                    delete_source_video = True
                    await manager.send_status(
                        user_id,
                        str(media_id),
                        "FAILED",
                        failed_reason="Duplicate video detected.",
                    )
                    return

            media.status = MediaStatus.PROCESSING
            session.add(create_status_change_log(media.id, MediaStatus.PROCESSING))
            await session.commit()
            await manager.send_status(user_id, str(media_id), "PROCESSING")

            srt_path = f"{local_video_path}.srt"
            try:
                await asyncio.to_thread(
                    extract_srt_from_video, local_video_path, srt_path
                )
            except MissingTelemetryError as e:
                media.status = MediaStatus.FAILED
                media.failed_reason = str(
                    "No embedded GPS telemetry track found in video."
                )
                session.add(
                    create_status_change_log(
                        media.id, MediaStatus.FAILED, detail=str(e)
                    )
                )
                await session.commit()
                await manager.send_status(
                    user_id, str(media_id), "FAILED", failed_reason=str(e)
                )

                if await asyncio.to_thread(os.path.exists, local_video_path):
                    await asyncio.to_thread(os.remove, local_video_path)
                return

            def open_video_capture(path):
                cap = cv2.VideoCapture(path)
                opened = cap.isOpened()
                return cap if opened else None

            cap = await asyncio.to_thread(open_video_capture, local_video_path)
            if cap is None:
                media.status = MediaStatus.FAILED
                media.failed_reason = "Could not decode video file."
                session.add(
                    create_status_change_log(
                        media.id, MediaStatus.FAILED, detail="Decoding error"
                    )
                )
                await session.commit()
                await manager.send_status(
                    user_id,
                    str(media_id),
                    "FAILED",
                    failed_reason="Could not decode video file.",
                )
                return

            def get_fps_and_frames(cap):
                fps = cap.get(cv2.CAP_PROP_FPS)
                total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                return fps, total_frames

            fps, total_frames = await asyncio.to_thread(get_fps_and_frames, cap)

            if not math.isfinite(fps) or fps <= 0 or total_frames <= 0:
                raise ValueError("Video has an invalid frame rate or frame count.")
            frame_step = max(1, int(fps * IMAGE_CHECK_INTERVAL_SECONDS))
            current_frame_idx = 0

            all_detections_for_db = []
            frames_to_upload = []  # list of (local_temp_path, hf_target_path)
            sum_confidence_found = 0.0

            while current_frame_idx < total_frames:
                # jump directly to the target frame and read frame in thread
                def read_frame(cap, idx):
                    cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                    ret, frame = cap.read()
                    return ret, frame

                def save_temp_frame(f):
                    with tempfile.NamedTemporaryFile(
                        suffix=".jpg", delete=False
                    ) as tmp:
                        if not cv2.imwrite(tmp.name, f):
                            os.remove(tmp.name)
                            raise RuntimeError("Could not save video frame for inference.")
                        return tmp.name

                ret, frame = await asyncio.to_thread(read_frame, cap, current_frame_idx)
                if not ret:
                    break

                tmp_frame_path = await asyncio.to_thread(save_temp_frame, frame)
                temporary_frames.add(tmp_frame_path)

                timestamp_sec = current_frame_idx / fps

                frame_lat, frame_lng = get_location_at_timestamp(
                    srt_path, timestamp_sec
                )

                logger.info(
                    f"Frame {current_frame_idx} at {timestamp_sec}s: GPS matched as {frame_lat}, {frame_lng}"
                )

                detections = []
                if frame_lat is not None and frame_lng is not None:

                    detections = await get_real_detections(tmp_frame_path)

                    if detections:
                        # The model sees a still image; GPS and time come from the video.
                        filename = f"frame_{timestamp_sec}.jpg"
                        hf_file_path = (
                            f"media/{user_id}/{date_str}/{media_id}_{filename}"
                        )
                        frames_to_upload.append((tmp_frame_path, hf_file_path))
                        address = await get_address_from_coords(frame_lat, frame_lng)
                        vid_h, vid_w = frame.shape[:2]

                    for det in detections:

                        sum_confidence_found += det["confidence"]
                        x1, y1, x2, y2 = det["bbox"]

                        db_det = VideoDetection(
                            media_id=media.id,
                            timestamp_in_video=timestamp_sec,
                            label="Trash" if det["label"] == "rubbish" else det["label"],
                            confidence=det["confidence"],
                            bbox={
                                "x": x1 / vid_w,
                                "y": y1 / vid_h,
                                "w": (x2 - x1) / vid_w,
                                "h": (y2 - y1) / vid_h,
                            },
                            frame_hf_path=hf_file_path,
                            lat=frame_lat,
                            lng=frame_lng,
                            location=f"SRID=4326;POINT({frame_lng} {frame_lat})",
                            address=address,
                        )
                        all_detections_for_db.append(db_det)

                if not detections:
                    os.remove(tmp_frame_path)
                    temporary_frames.discard(tmp_frame_path)

                current_frame_idx += frame_step

            await asyncio.to_thread(lambda c: c.release(), cap)
            cap = None

            if frames_to_upload:
                upload_success = await upload_video_frames_to_hf(
                    user_id, str(media.id), frames_to_upload
                )
                if not upload_success:
                    raise RuntimeError("Could not upload video detection evidence frames.")
                session.add_all(all_detections_for_db)
                media.has_trash = True
                media.confidence = round(
                    sum_confidence_found / len(all_detections_for_db) * 100, 2
                )
            else:
                media.has_trash = False
                media.confidence = 0.0

            tech_meta = dict(media.technical_metadata or {})
            tech_meta.update(
                has_trash=media.has_trash,
                confidence=media.confidence,
                detections_count=len(all_detections_for_db),
            )
            media.technical_metadata = tech_meta

            if (
                media.technical_metadata
                and "hf_full_video_path" in media.technical_metadata
            ):
                tech_meta = media.technical_metadata.copy()
                tech_meta["hf_full_video_path"] = None
                media.technical_metadata = tech_meta

            media.status = MediaStatus.READY
            session.add(create_status_change_log(media.id, MediaStatus.READY))
            await session.commit()
            delete_source_video = True
            logger.info(
                "Saved %s detections for video %s", len(all_detections_for_db), media_id
            )

            await manager.send_status(user_id, str(media_id), "READY")

    except Exception as e:
        logger.exception("Video processing failed for %s", media_id)
        async with AsyncSessionLocal() as session:
            if media_id is not None:
                media_result = await session.execute(
                    select(Media).where(Media.id == media_id)
                )
                media = media_result.scalar_one_or_none()
                if media:
                    media.status = MediaStatus.FAILED
                    media.failed_reason = "Internal processing error."

                    session.add(
                        create_status_change_log(
                            media.id, MediaStatus.FAILED, detail=str(e)
                        )
                    )
                    await session.commit()
                    await manager.send_status(
                        user_id,
                        str(media_id),
                        "FAILED",
                        failed_reason="Internal processing error.",
                    )
    finally:
        if cap is not None:
            try:
                await asyncio.to_thread(cap.release)
            except Exception:
                logger.warning("Could not release video capture for %s", media_id, exc_info=True)
        for frame_path in temporary_frames:
            try:
                if os.path.exists(frame_path):
                    os.remove(frame_path)
            except OSError:
                logger.warning("Could not remove temporary video frame %s", frame_path, exc_info=True)

        # Keep the uploaded source available for recovery when inference or saving fails.
        if delete_source_video and hf_full_video_path:
            await asyncio.to_thread(delete_video_from_hf, hf_full_video_path)

        try:
            if await asyncio.to_thread(
                os.path.exists, local_video_path if local_video_path else ""
            ):
                await asyncio.to_thread(
                    os.remove, local_video_path if local_video_path else ""
                )
        except Exception:
            pass

        srt_path = f"{local_video_path}.srt"
        try:
            if local_video_path and await asyncio.to_thread(os.path.exists, srt_path):
                await asyncio.to_thread(os.remove, srt_path)
        except Exception:
            pass
