import cv2
import os
import tempfile
import asyncio
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
from app.api.dashboard_utils.utils.media_utils import generate_fake_video_detections
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

            frame_step = int(fps * IMAGE_CHECK_INTERVAL_SECONDS)
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

                ret, frame = await asyncio.to_thread(read_frame, cap, current_frame_idx)
                if not ret:
                    break

                timestamp_sec = current_frame_idx / fps

                frame_lat, frame_lng = get_location_at_timestamp(
                    srt_path, timestamp_sec
                )

                logger.info(
                    f"Frame {current_frame_idx} at {timestamp_sec}s: GPS matched as {frame_lat}, {frame_lng}"
                )

                if frame_lat is not None and frame_lng is not None:

                    detections = await get_real_detections(frame)

                    for det in detections:

                        sum_confidence_found += det["confidence"]

                        # save the frame locally to a temp file in thread
                        def save_frame(frame):
                            with tempfile.NamedTemporaryFile(
                                suffix=".jpg", delete=False
                            ) as tmp_img:
                                cv2.imwrite(tmp_img.name, frame)
                                return tmp_img.name

                        tmp_img_path = await asyncio.to_thread(save_frame, frame)

                        filename = f"frame_{det['timestamp_in_video']}.jpg"
                        hf_file_path = (
                            f"media/{user_id}/{date_str}/{media_id}_{filename}"
                        )

                        frames_to_upload.append((tmp_img_path, hf_file_path))

                        address = await get_address_from_coords(det["lat"], det["lng"])

                        db_det = VideoDetection(
                            media_id=media.id,
                            timestamp_in_video=det["timestamp_in_video"],
                            label=det["label"],
                            confidence=det["confidence"],
                            bbox=det["bbox"],
                            frame_hf_path=hf_file_path,
                            lat=det["lat"],
                            lng=det["lng"],
                            location=f"SRID=4326;POINT({det['lng']} {det['lat']})",
                            address=address,
                        )
                        all_detections_for_db.append(db_det)

                current_frame_idx += frame_step

            await asyncio.to_thread(lambda c: c.release(), cap)

            if frames_to_upload:
                upload_success = await upload_video_frames_to_hf(
                    user_id, str(media.id), frames_to_upload
                )
                if upload_success:
                    session.add_all(all_detections_for_db)
                    media.has_trash = True
                    media.confidence = round(
                        sum_confidence_found / len(all_detections_for_db) * 100, 2
                    )
                else:
                    media.has_trash = False
                    media.confidence = 0.0
                    logger.error(
                        f"HF Upload failed for video {media_id}. Detections not saved."
                    )
            else:
                media.has_trash = False
                media.confidence = 0.0

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

            await manager.send_status(user_id, str(media_id), "READY")

    except Exception as e:
        async with AsyncSessionLocal() as session:
            if media_id is not None:
                media_result = await session.execute(
                    select(Media).where(Media.id == media_id)
                )
                media = media_result.scalar_one_or_none()
                if media:
                    media.status = MediaStatus.FAILED
                    media.failed_reason = "Internal processing error."

                    if (
                        media.technical_metadata
                        and "hf_full_video_path" in media.technical_metadata
                    ):
                        tech_meta = media.technical_metadata.copy()
                        tech_meta["hf_full_video_path"] = None
                        media.technical_metadata = tech_meta

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
            if hf_full_video_path:
                await asyncio.to_thread(delete_video_from_hf, hf_full_video_path)
    finally:
        if hf_full_video_path:
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

        try:
            if local_video_path and await asyncio.to_thread(
                os.path.exists, local_video_path
            ):
                await asyncio.to_thread(os.remove, local_video_path)
        except Exception:
            pass

        srt_path = f"{local_video_path}.srt"
        try:
            if local_video_path and await asyncio.to_thread(os.path.exists, srt_path):
                await asyncio.to_thread(os.remove, srt_path)
        except Exception:
            pass
