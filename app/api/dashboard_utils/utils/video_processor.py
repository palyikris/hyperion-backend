import asyncio
import logging
import math
import os
import tempfile
from datetime import datetime, timezone

import cv2
from huggingface_hub import hf_hub_download
from sqlalchemy import select

from app.api.ai_client import AIInferenceError, get_real_detections
from app.api.medialog_utils.media_log_utils import create_status_change_log
from app.api.upload_utils.conn_manager import manager
from app.api.upload_utils.hf_upload import (
    delete_video_from_hf,
    upload_video_frames_to_hf,
)
from app.api.upload_utils.metadata_extractor import get_address_from_coords
from app.api.upload_utils.telemetry import (
    MissingTelemetryError,
    extract_srt_from_video,
    get_location_at_timestamp,
)
from app.api.upload_utils.temp_files import cleanup_temp_files
from app.database import AsyncSessionLocal
from app.models.db.Media import Media
from app.models.db.VideoDetection import VideoDetection
from app.models.upload.MediaStatus import MediaStatus

from .worker_state import record_completed_task

logger = logging.getLogger(__name__)
IMAGE_CHECK_INTERVAL_SECONDS = 3.0


class VideoProcessingError(RuntimeError):
    """An incomplete analysis must not become a successful clean result."""


def _sampling_parameters(cap) -> tuple[float, int, int]:
    if not cap.isOpened():
        raise VideoProcessingError("Could not decode video file.")
    fps = cap.get(cv2.CAP_PROP_FPS)
    frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    if not math.isfinite(fps) or fps <= 0:
        raise VideoProcessingError("Video frame rate must be finite and positive.")
    if not math.isfinite(frame_count) or frame_count < 1:
        raise VideoProcessingError("Video does not contain a valid frame count.")
    # Even a video below 1/3 FPS must advance on each iteration.
    return fps, int(frame_count), max(1, int(fps * IMAGE_CHECK_INTERVAL_SECONDS))


def _read_frame(cap, index: int):
    if not cap.set(cv2.CAP_PROP_POS_FRAMES, index):
        raise VideoProcessingError("Could not seek to a sampled video frame.")
    success, frame = cap.read()
    if not success or frame is None:
        raise VideoProcessingError("Could not decode a sampled video frame.")
    if len(frame.shape) < 2 or frame.shape[0] <= 0 or frame.shape[1] <= 0:
        raise VideoProcessingError("Sampled video frame has invalid dimensions.")
    return frame


def _save_jpeg(path: str, frame) -> None:
    if not cv2.imwrite(path, frame):
        raise VideoProcessingError("Could not encode a sampled video frame.")


async def _write_frame(path: str, frame) -> None:
    # Wait for an in-flight write before cleanup, including after cancellation.
    write_task = asyncio.create_task(asyncio.to_thread(_save_jpeg, path, frame))
    try:
        await asyncio.shield(write_task)
    except asyncio.CancelledError:
        await asyncio.gather(write_task, return_exceptions=True)
        raise


def _valid_gps(lat, lng) -> bool:
    return (
        isinstance(lat, (int, float))
        and isinstance(lng, (int, float))
        and math.isfinite(lat)
        and math.isfinite(lng)
        and -90 <= lat <= 90
        and -180 <= lng <= 180
    )


async def process_video_media(media_task) -> None:
    """Analyze GPS-matched samples and publish only complete, stored results."""
    user_id = media_task.uploader_id
    media_id = media_task.id
    hf_full_video_path = (media_task.technical_metadata or {}).get(
        "hf_full_video_path"
    )
    local_video_path = None
    frame_directory = None
    cap = None
    repo_id = os.getenv("HF_REPO_ID")
    token = os.getenv("HF_TOKEN")

    try:
        def download_from_hf():
            if not repo_id or not hf_full_video_path:
                raise VideoProcessingError("Video storage configuration is incomplete.")
            return hf_hub_download(
                repo_id=repo_id,
                filename=hf_full_video_path,
                repo_type="dataset",
                token=token,
                local_dir=tempfile.gettempdir(),
            )

        local_video_path = await asyncio.to_thread(download_from_hf)
        async with AsyncSessionLocal() as session:
            media_result = await session.execute(
                select(Media).where(Media.id == media_id)
            )
            media = media_result.scalar_one_or_none()
            if media is None:
                return
            filename = (media.initial_metadata or {}).get("filename")
            if filename:
                duplicate_result = await session.execute(
                    select(Media)
                    .where(
                        Media.uploader_id == user_id,
                        Media.id != media_id,
                        Media.status == MediaStatus.READY,
                        Media.initial_metadata["filename"].as_string() == str(filename),
                    )
                    .limit(1)
                )
                duplicate = duplicate_result.scalars().first()
                if duplicate:
                    media.status = MediaStatus.FAILED
                    media.failed_reason = f"Duplicate of {filename}"
                    media.original_media_id = duplicate.id
                    session.add(create_status_change_log(
                        media.id, MediaStatus.FAILED, detail="Duplicate detected"
                    ))
                    await session.commit()
                    await manager.send_status(
                        user_id, str(media_id), "FAILED",
                        failed_reason="Duplicate video detected.",
                    )
                    return
            media.status = MediaStatus.PROCESSING
            session.add(create_status_change_log(media.id, MediaStatus.PROCESSING))
            await session.commit()
        await manager.send_status(user_id, str(media_id), "PROCESSING")

        # Do not hold a database transaction across inference and file uploads.
        srt_path = f"{local_video_path}.srt"
        await asyncio.to_thread(extract_srt_from_video, local_video_path, srt_path)
        cap = await asyncio.to_thread(cv2.VideoCapture, local_video_path)
        fps, total_frames, frame_step = await asyncio.to_thread(_sampling_parameters, cap)
        frame_directory = tempfile.TemporaryDirectory(prefix="hyperion-video-frames-")
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        frames_to_upload = []
        all_detections = []
        sampled_frames = 0
        analyzed_frames = 0

        for frame_index in range(0, total_frames, frame_step):
            frame = await asyncio.to_thread(_read_frame, cap, frame_index)
            sampled_frames += 1
            timestamp_sec = frame_index / fps
            frame_lat, frame_lng = await asyncio.to_thread(
                get_location_at_timestamp, srt_path, timestamp_sec
            )
            if not _valid_gps(frame_lat, frame_lng):
                continue

            frame_path = os.path.join(frame_directory.name, f"frame_{frame_index}.jpg")
            await _write_frame(frame_path, frame)
            detections = await get_real_detections(frame_path)
            analyzed_frames += 1
            if not detections:
                await asyncio.to_thread(cleanup_temp_files, [frame_path])
                continue

            # All objects on this sample share one evidence image and GPS fix.
            hf_frame_path = (
                f"media/{user_id}/{date_str}/{media_id}_frame_{frame_index}.jpg"
            )
            frames_to_upload.append((frame_path, hf_frame_path))
            address = await get_address_from_coords(frame_lat, frame_lng)
            height, width = frame.shape[:2]
            for detection in detections:
                x1, y1, x2, y2 = detection["bbox"]
                all_detections.append(VideoDetection(
                    media_id=media_id,
                    timestamp_in_video=timestamp_sec,
                    label=detection["label"],
                    confidence=detection["confidence"],
                    bbox={
                        "x": x1 / width,
                        "y": y1 / height,
                        "w": (x2 - x1) / width,
                        "h": (y2 - y1) / height,
                    },
                    frame_hf_path=hf_frame_path,
                    lat=frame_lat,
                    lng=frame_lng,
                    location=f"SRID=4326;POINT({frame_lng} {frame_lat})",
                    address=address,
                ))

        if analyzed_frames == 0:
            raise MissingTelemetryError(
                "No usable GPS telemetry matches the sampled video frames."
            )
        if frames_to_upload and not await upload_video_frames_to_hf(
            user_id, str(media_id), frames_to_upload
        ):
            raise VideoProcessingError(
                "Could not store video evidence images. Processing failed."
            )

        async with AsyncSessionLocal() as session:
            media_result = await session.execute(
                select(Media).where(Media.id == media_id)
            )
            media = media_result.scalar_one_or_none()
            if media is None:
                return
            session.add_all(all_detections)
            media.has_trash = bool(all_detections)
            media.confidence = (
                round(
                    sum(det.confidence for det in all_detections)
                    / len(all_detections) * 100, 2
                )
                if all_detections else 0.0
            )
            media.technical_metadata = {
                **(media.technical_metadata or {}),
                "hf_full_video_path": None,
                "sampled_frames": sampled_frames,
                "analyzed_frames": analyzed_frames,
                "skipped_gps_frames": sampled_frames - analyzed_frames,
            }
            media.failed_reason = None
            media.status = MediaStatus.READY
            session.add(create_status_change_log(media.id, MediaStatus.READY))
            if media.assigned_worker:
                await record_completed_task(session, media.assigned_worker)
            await session.commit()
        await manager.send_status(user_id, str(media_id), "READY")

    except Exception as error:
        async with AsyncSessionLocal() as session:
            media_result = await session.execute(
                select(Media).where(Media.id == media_id)
            )
            media = media_result.scalar_one_or_none()
            if media is not None:
                media.status = MediaStatus.FAILED
                media.failed_reason = (
                    str(error)
                    if isinstance(
                        error,
                        (AIInferenceError, VideoProcessingError, MissingTelemetryError),
                    )
                    else "Internal processing error."
                )
                media.technical_metadata = {
                    **(media.technical_metadata or {}), "hf_full_video_path": None,
                }
                session.add(create_status_change_log(
                    media.id, MediaStatus.FAILED, detail=media.failed_reason
                ))
                await session.commit()
                await manager.send_status(
                    user_id, str(media_id), "FAILED", failed_reason=media.failed_reason
                )
    finally:
        if cap is not None:
            try:
                await asyncio.to_thread(cap.release)
            except Exception:
                logger.warning("Could not release video decoder for %s", media_id)
        if frame_directory is not None:
            try:
                await asyncio.to_thread(frame_directory.cleanup)
            except OSError:
                logger.warning("Could not remove temporary video frames for %s", media_id)
        if local_video_path:
            await asyncio.to_thread(
                cleanup_temp_files, [local_video_path, f"{local_video_path}.srt"]
            )
        if hf_full_video_path:
            try:
                await asyncio.to_thread(delete_video_from_hf, hf_full_video_path)
            except Exception:
                logger.warning("Could not remove temporary stored video %s", media_id)
