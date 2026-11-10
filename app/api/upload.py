from fastapi import (
    APIRouter,
    UploadFile,
    File,
    Depends,
    BackgroundTasks,
    WebSocket,
    WebSocketDisconnect,
    Query,
    WebSocketException,
)
from app.database import AsyncSessionLocal
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.api.deps import get_current_user, get_current_user_from_token
from app.api.medialog_utils.media_log_utils import create_status_change_log
from app.models.db.Media import Media
from app.models.upload.MediaStatus import MediaStatus
from app.api.upload_utils.conn_manager import manager
from PIL import Image, UnidentifiedImageError
import io
import uuid
import asyncio
import logging
import tempfile
import os
from fastapi.responses import JSONResponse
from fastapi import HTTPException, status
from app.models.upload.UploadResponse import UploadResponse, RecentsResponse
from app.api.upload_utils.hf_upload import process_hf_upload, process_video_hf_upload
from sqlalchemy import select
from app.api.upload_utils.video_file_helpers import (
    save_video_chunk_to_temp,
    assemble_video_from_chunks,
    detect_video_extension,
)
from app.api.upload_utils.video_thumbnail import extract_video_thumbnail
from huggingface_hub import HfApi
import shutil
from app.models.db.Media import MediaType
from app.models.db.VideoDetection import VideoDetection
from app.api.upload_utils.temp_files import cleanup_temp_files
from app.core.csrf import is_trusted_request_origin

router = APIRouter()
logger = logging.getLogger(__name__)
WEBSOCKET_AUTH_CHECK_INTERVAL_SECONDS = 60

THUMBNAIL_SIZE = (400, 400)
MAX_IMAGE_FILES_PER_REQUEST = 20
MAX_IMAGE_FILE_SIZE_BYTES = 5 * 1024 * 1024
UPLOAD_READ_CHUNK_SIZE = 1024 * 1024
SUPPORTED_IMAGE_CONTENT_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/tiff",
    "image/x-tiff",
}
SUPPORTED_IMAGE_FORMATS = {"JPEG", "PNG", "TIFF"}
VIDEO_CHUNK_SIZE_BYTES = 2 * 1024 * 1024
MAX_VIDEO_FILE_SIZE_BYTES = 1024 * 1024 * 1024
SUPPORTED_VIDEO_CONTENT_TYPES = {
    "video/mp4": {".mp4"},
    "video/webm": {".webm"},
    "video/ogg": {".ogg", ".ogv"},
}


def _normalize_video_filename(filename: str) -> str:
    normalized = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Video filename is required",
        )
    return normalized


def _validate_video_metadata(
    filename: str,
    content_type: str,
    total_size: int,
    total_chunks: int,
) -> tuple[str, str]:
    normalized_filename = _normalize_video_filename(filename)
    content_type = content_type.lower().split(";", 1)[0].strip()
    extension = os.path.splitext(normalized_filename)[1].lower()
    allowed_extensions = SUPPORTED_VIDEO_CONTENT_TYPES.get(content_type)

    if not allowed_extensions or extension not in allowed_extensions:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Supported video formats are MP4, WebM and Ogg",
        )
    if total_size <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Video file is empty",
        )
    if total_size > MAX_VIDEO_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Video exceeds the 1 GB upload limit",
        )

    expected_chunks = (
        total_size + VIDEO_CHUNK_SIZE_BYTES - 1
    ) // VIDEO_CHUNK_SIZE_BYTES
    if total_chunks != expected_chunks:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Video chunk count does not match the declared file size",
        )

    return normalized_filename, extension


def _expected_video_chunk_size(
    chunk_index: int,
    total_size: int,
    total_chunks: int,
) -> int:
    if chunk_index < 0 or chunk_index >= total_chunks:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Video chunk index is out of range",
        )
    if chunk_index < total_chunks - 1:
        return VIDEO_CHUNK_SIZE_BYTES
    return total_size - VIDEO_CHUNK_SIZE_BYTES * (total_chunks - 1)


async def _read_video_chunk(chunk: UploadFile, expected_size: int) -> bytes:
    chunk_bytes = await chunk.read(expected_size + 1)
    if len(chunk_bytes) != expected_size:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Video chunk size does not match its declared position",
        )
    return chunk_bytes


def _video_metadata_values(media: Media) -> tuple[str, str, int, int]:
    metadata = media.initial_metadata or {}
    filename = str(metadata.get("filename") or "")
    content_type = str(metadata.get("content_type") or "")
    try:
        total_size = int(metadata["size"])
        total_chunks = int(metadata["total_chunks"])
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Stored video upload metadata is incomplete",
        ) from error

    normalized_filename, extension = _validate_video_metadata(
        filename,
        content_type,
        total_size,
        total_chunks,
    )
    return normalized_filename, extension, total_size, total_chunks


async def _read_image_upload(file: UploadFile) -> bytes:
    if file.content_type not in SUPPORTED_IMAGE_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported image type for {file.filename or 'unnamed file'}",
        )

    chunks = []
    total_size = 0
    while chunk := await file.read(UPLOAD_READ_CHUNK_SIZE):
        total_size += len(chunk)
        if total_size > MAX_IMAGE_FILE_SIZE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=(
                    f"Image {file.filename or 'unnamed file'} exceeds the "
                    "5 MB upload limit"
                ),
            )
        chunks.append(chunk)

    if not chunks:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Image {file.filename or 'unnamed file'} is empty",
        )
    return b"".join(chunks)


def _process_image_to_temp(
    content: bytes, media_id: uuid.UUID
) -> tuple[int, int, str, str]:
    """
    CPU-bound image processing: extract dimensions, create thumbnail, and save both to temp files.
    This function runs in a thread pool to avoid blocking the event loop.
    Returns: (width, height, content_temp_path, thumbnail_temp_path)
    """
    content_temp_path = os.path.join(tempfile.gettempdir(), f"{media_id}_content")
    thumbnail_temp_path = os.path.join(tempfile.gettempdir(), f"{media_id}_thumbnail")

    try:
        with Image.open(io.BytesIO(content)) as img:
            if img.format not in SUPPORTED_IMAGE_FORMATS:
                raise UnidentifiedImageError(
                    f"Unsupported image format: {img.format or 'unknown'}"
                )
            img.load()
            width, height = img.size
            thumbnail_img = img.copy()

        try:
            thumbnail_img.thumbnail(THUMBNAIL_SIZE)
            if thumbnail_img.mode != "RGB":
                rgb_thumbnail = thumbnail_img.convert("RGB")
                thumbnail_img.close()
                thumbnail_img = rgb_thumbnail

            with open(content_temp_path, "wb") as file_handle:
                file_handle.write(content)
            thumbnail_img.save(
                thumbnail_temp_path, format="JPEG", quality=85, optimize=True
            )
        finally:
            thumbnail_img.close()

        return width, height, content_temp_path, thumbnail_temp_path
    except Exception:
        cleanup_temp_files([content_temp_path, thumbnail_temp_path])
        raise


@router.post(
    "/files",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def batch_upload(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if len(files) > MAX_IMAGE_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"At most {MAX_IMAGE_FILES_PER_REQUEST} images can be uploaded at once",
        )

    media_records = []
    files_to_process = []
    prepared_temp_files = []

    try:
        for file in files:
            content = await _read_image_upload(file)
            media_id = uuid.uuid4()

            try:
                # Track output paths before starting the thread, including cancellation.
                content_path = os.path.join(tempfile.gettempdir(), f"{media_id}_content")
                thumbnail_path = os.path.join(tempfile.gettempdir(), f"{media_id}_thumbnail")
                prepared_temp_files.extend([content_path, thumbnail_path])
                processing = asyncio.create_task(asyncio.to_thread(
                    _process_image_to_temp, content, media_id
                ))
                try:
                    width, height, content_path, thumbnail_path = await asyncio.shield(processing)
                except asyncio.CancelledError:
                    await asyncio.gather(processing, return_exceptions=True)
                    raise
            except UnidentifiedImageError as error:
                raise HTTPException(
                    status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    detail=f"Unsupported or invalid image: {file.filename}",
                ) from error
            except OSError as error:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Unable to read image: {file.filename}",
                ) from error
            finally:
                del content

            filename = (file.filename or f"{media_id}.jpg").replace("\\", "/").rsplit("/", 1)[-1]
            new_media = Media(
                id=media_id,
                uploader_id=current_user.id,
                status=MediaStatus.PENDING,
                initial_metadata={
                    "filename": filename,
                    "size": os.path.getsize(content_path),
                    "width": width,
                    "height": height,
                },
            )

            insert_log = create_status_change_log(
                media_id=media_id,
                status=MediaStatus.PENDING,
            )
            db.add(insert_log)

            media_records.append(new_media)
            files_to_process.append(
                (media_id, filename, content_path, thumbnail_path)
            )

        db.add_all(media_records)
        await db.commit()

        for media in media_records:
            await manager.send_status(
                user_id=str(current_user.id),
                media_id=str(media.id),
                status=MediaStatus.PENDING.value,
                worker=None,
            )
    except BaseException:
        await db.rollback()
        cleanup_temp_files(prepared_temp_files)
        raise

    background_tasks.add_task(
        process_hf_upload,
        files_to_process,
        current_user.id,
    )

    return {
        "message": f"{len(media_records)} files uploaded successfully",
        "media_ids": [str(m.id) for m in media_records],
    }


@router.get(
    "/recents",
    status_code=status.HTTP_200_OK,
    response_model=RecentsResponse,
)
async def get_recents(
    db: AsyncSession = Depends(get_db), current_user=Depends(get_current_user)
):
    # 1. Fetch 4 recent media items
    media_query = (
        select(Media)
        .where(Media.uploader_id == current_user.id)
        .order_by(Media.created_at.desc())
        .limit(4)
    )
    media_result = await db.execute(media_query)
    recent_media = media_result.scalars().all()

    # 2. Fetch 4 recent video detections
    video_query = (
        select(VideoDetection)
        .where(
            VideoDetection.media_id.in_(
                select(Media.id).where(Media.uploader_id == current_user.id)
            )
        )
        .order_by(VideoDetection.created_at.desc())
        .limit(4)
    )
    video_result = await db.execute(video_query)
    recent_videos = video_result.scalars().all()

    # 3. Return the exact same structure as the Vault endpoint
    return {
        "total": len(recent_media) + len(recent_videos),
        "image_items": [
            {
                "id": str(media.id),
                "uploader_id": str(media.uploader_id),
                "status": (
                    media.status.value
                    if hasattr(media.status, "value")
                    else media.status
                ),
                "hf_path": media.hf_path,
                "initial_metadata": media.initial_metadata,
                "technical_metadata": media.technical_metadata,
                "assigned_worker": media.assigned_worker,
                "created_at": media.created_at.isoformat(),
                "updated_at": media.updated_at.isoformat(),
                "lat": media.lat,
                "lng": media.lng,
                "altitude": media.altitude,
                "address": media.address,
                "has_trash": media.has_trash,
                "confidence": media.confidence,
                "failed_reason": media.failed_reason,
            }
            for media in recent_media
        ],
        "video_items": [
            {
                "id": str(video_det.id),
                "media_id": str(video_det.media_id),
                "lat": video_det.lat,
                "lng": video_det.lng,
                "altitude": video_det.altitude,
                "address": video_det.address,
                "label": video_det.label,
                "confidence": video_det.confidence,
                "bbox": video_det.bbox,
                "timestamp_in_video": video_det.timestamp_in_video,
                "frame_hf_path": video_det.frame_hf_path,
                "created_at": video_det.created_at.isoformat(),
                "area_sqm": video_det.area_sqm,
            }
            for video_det in recent_videos
        ],
    }


from fastapi import Form


def _cleanup_video_temp_artifacts(
    media_id: uuid.UUID,
    extra_paths: list[str] | None = None,
) -> None:
    temp_dir = os.path.join(tempfile.gettempdir(), f"video_upload_{media_id}")
    if os.path.isdir(temp_dir):
        shutil.rmtree(temp_dir, ignore_errors=True)

    file_paths = [
        os.path.join(tempfile.gettempdir(), f"{media_id}{extension}")
        for extension in (".mp4", ".webm", ".ogg", ".ogv")
    ]
    file_paths.append(
        os.path.join(tempfile.gettempdir(), f"{media_id}_video_thumbnail.jpg")
    )
    cleanup_temp_files([*file_paths, *(extra_paths or [])])


async def _mark_video_upload_failed(
    db: AsyncSession,
    media: Media,
    reason: str,
) -> None:
    media.status = MediaStatus.FAILED
    media.failed_reason = reason
    db.add(media)
    db.add(
        create_status_change_log(
            media_id=media.id,
            status=MediaStatus.FAILED,
            detail=reason,
        )
    )
    await db.commit()
    await manager.send_status(
        user_id=str(media.uploader_id),
        media_id=str(media.id),
        status=MediaStatus.FAILED.value,
        worker=None,
        failed_reason=reason,
    )


@router.post("/video/init")
async def video_init(
    filename: str = Form(...),
    content_type: str = Form(...),
    total_size: int = Form(...),
    total_chunks: int = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    normalized_content_type = content_type.lower().split(";", 1)[0].strip()
    normalized_filename, extension = _validate_video_metadata(
        filename,
        normalized_content_type,
        total_size,
        total_chunks,
    )

    media_id = uuid.uuid4()
    temp_dir = os.path.join(tempfile.gettempdir(), f"video_upload_{media_id}")
    os.makedirs(temp_dir, exist_ok=True)

    new_media = Media(
        id=media_id,
        uploader_id=current_user.id,
        status=MediaStatus.PENDING,
        initial_metadata={
            "filename": normalized_filename,
            "content_type": normalized_content_type,
            "extension": extension,
            "size": total_size,
            "total_chunks": total_chunks,
        },
        media_type=MediaType.VIDEO,
    )
    db.add(new_media)

    insert_log = create_status_change_log(
        media_id=media_id,
        status=MediaStatus.PENDING,
    )
    db.add(insert_log)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    await manager.send_status(
        user_id=str(current_user.id),
        media_id=str(media_id),
        status=MediaStatus.PENDING.value,
        worker=None,
    )

    return JSONResponse(content={"media_id": str(media_id)})


@router.post("/video/chunk/{media_id}")
async def video_chunk(
    media_id: str,
    chunk_index: int = Form(...),
    chunk: UploadFile = File(...),
    db=Depends(get_db),
    current_user=Depends(get_current_user),
):

    try:
        media_uuid = uuid.UUID(media_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid media_id format")

    user_id = current_user.id

    result = await db.execute(
        select(Media).where(Media.id == media_uuid).where(Media.uploader_id == user_id)
    )
    media = result.scalar_one_or_none()
    if not media or media.media_type != MediaType.VIDEO:
        raise HTTPException(status_code=404, detail="Media not found or access denied")
    if media.status != MediaStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Video upload is no longer accepting chunks",
        )

    _, _, total_size, total_chunks = _video_metadata_values(media)
    expected_size = _expected_video_chunk_size(
        chunk_index,
        total_size,
        total_chunks,
    )

    temp_dir = os.path.join(tempfile.gettempdir(), f"video_upload_{media_uuid}")
    if not os.path.isdir(temp_dir):
        raise HTTPException(status_code=404, detail="Temp dir for media_id not found")
    chunk_path = os.path.join(temp_dir, f"chunk_{chunk_index}")
    if os.path.exists(chunk_path):
        if os.path.getsize(chunk_path) == expected_size:
            return JSONResponse(
                content={
                    "status": "duplicate",
                    "detail": f"Chunk {chunk_index} already uploaded",
                },
                status_code=200,
            )
        os.remove(chunk_path)

    chunk_bytes = await _read_video_chunk(chunk, expected_size)
    await asyncio.to_thread(
        save_video_chunk_to_temp, str(media_uuid), chunk_index, chunk_bytes
    )
    del chunk_bytes
    return JSONResponse(content={"status": "ok"})


@router.post("/video/complete/{media_id}")
async def video_complete(
    media_id: str,
    background_tasks: BackgroundTasks,
    total_chunks: int = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    try:
        media_uuid = uuid.UUID(media_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid media_id format")

    result = await db.execute(
        select(Media).where(
            Media.id == media_uuid,
            Media.uploader_id == current_user.id,
        )
    )
    media = result.scalar_one_or_none()
    if not media or media.media_type != MediaType.VIDEO:
        raise HTTPException(status_code=404, detail="Media not found")
    if media.status != MediaStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Video upload is no longer pending",
        )

    _, extension, total_size, stored_total_chunks = _video_metadata_values(media)
    if total_chunks != stored_total_chunks:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Video chunk count does not match the initialized upload",
        )

    temp_dir = os.path.join(tempfile.gettempdir(), f"video_upload_{media_uuid}")
    if not os.path.isdir(temp_dir):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Temporary video upload was not found",
        )

    missing_chunks = [
        i
        for i in range(stored_total_chunks)
        if not os.path.exists(os.path.join(temp_dir, f"chunk_{i}"))
    ]
    if missing_chunks:
        return JSONResponse(
            content={"status": "incomplete", "missing_chunks": missing_chunks},
            status_code=400,
        )

    invalid_chunks = [
        i
        for i in range(stored_total_chunks)
        if os.path.getsize(os.path.join(temp_dir, f"chunk_{i}"))
        != _expected_video_chunk_size(i, total_size, stored_total_chunks)
    ]
    if invalid_chunks:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "One or more video chunks have an invalid size",
                "invalid_chunks": invalid_chunks,
            },
        )

    HF_TOKEN = os.getenv("HF_TOKEN")
    HF_REPO_ID = os.getenv("HF_REPO_ID")
    if not HF_REPO_ID or not HF_TOKEN:
        reason = "Video storage is not configured."
        await _mark_video_upload_failed(db, media, reason)
        _cleanup_video_temp_artifacts(media_uuid)
        raise HTTPException(status_code=500, detail=reason)

    video_path = os.path.join(tempfile.gettempdir(), f"{media_uuid}{extension}")
    thumbnail_path = os.path.join(
        tempfile.gettempdir(), f"{media_uuid}_video_thumbnail.jpg"
    )

    try:
        video_path = await asyncio.to_thread(
            assemble_video_from_chunks,
            str(media_uuid),
            stored_total_chunks,
            video_path,
        )
    except Exception as error:
        reason = "Video assembly failed."
        await _mark_video_upload_failed(db, media, reason)
        _cleanup_video_temp_artifacts(media_uuid, [video_path])
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=reason,
        ) from error

    try:
        detected_extension = await asyncio.to_thread(
            detect_video_extension, video_path
        )
        expected_container = ".ogg" if extension == ".ogv" else extension
        if detected_extension != expected_container:
            raise ValueError("Video content does not match its declared format")

        thumbnail_path, width, height = await asyncio.to_thread(
            extract_video_thumbnail, video_path, str(media_uuid)
        )
    except Exception as error:
        reason = "Unsupported, invalid or unreadable video file."
        await _mark_video_upload_failed(db, media, reason)
        _cleanup_video_temp_artifacts(media_uuid, [video_path, thumbnail_path])
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=reason,
        ) from error

    api = HfApi(token=HF_TOKEN)
    from datetime import datetime, timezone

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    thumb_hf_path = (
        f"media/{current_user.id}/{date_str}/{media_uuid}_video_thumbnail.jpg"
    )
    try:
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: api.upload_file(
                path_or_fileobj=thumbnail_path,
                path_in_repo=thumb_hf_path,
                repo_id=HF_REPO_ID,
                repo_type="dataset",
                commit_message=f"Upload video thumbnail for {media_uuid}",
            ),
        )
    except Exception as error:
        reason = "Video thumbnail could not be saved to storage."
        await _mark_video_upload_failed(db, media, reason)
        _cleanup_video_temp_artifacts(media_uuid, [video_path, thumbnail_path])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=reason,
        ) from error
    finally:
        cleanup_temp_files([thumbnail_path])

    initial_metadata = media.initial_metadata.copy() if media.initial_metadata else {}
    initial_metadata.update(
        {
            "thumbnail_hf_url": thumb_hf_path,
            "width": width,
            "height": height,
        }
    )
    media.initial_metadata = initial_metadata
    media.hf_path = thumb_hf_path
    technical_metadata = (
        media.technical_metadata.copy() if media.technical_metadata else {}
    )
    technical_metadata["local_video_path"] = video_path
    media.technical_metadata = technical_metadata
    media.status = MediaStatus.PENDING
    db.add(media)
    await db.commit()

    video_hf_path = (
        f"media/{current_user.id}/{date_str}/{media_uuid}_full_video{extension}"
    )

    background_tasks.add_task(
        process_video_hf_upload,
        media_id=media_id,
        user_id=current_user.id,
        local_video_path=video_path,
        video_hf_path=video_hf_path,
    )

    shutil.rmtree(temp_dir, ignore_errors=True)

    return JSONResponse(
        content={
            "status": "complete",
            "video_path": video_path,
            "thumbnail_url": thumb_hf_path,
        }
    )


@router.delete("/video/cancel/{media_id}")
async def video_cancel(
    media_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    try:
        media_uuid = uuid.UUID(media_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid media_id format")
    result = await db.execute(
        select(Media).where(
            Media.id == media_uuid,
            Media.uploader_id == current_user.id,
        )
    )
    media = result.scalar_one_or_none()
    if not media or media.media_type != MediaType.VIDEO:
        raise HTTPException(status_code=404, detail="Media not found")

    local_video_path = (
        media.technical_metadata.get("local_video_path")
        if media.technical_metadata
        else None
    )
    if media.technical_metadata:
        technical_metadata = media.technical_metadata.copy()
        technical_metadata["local_video_path"] = None
        media.technical_metadata = technical_metadata

    media.status = MediaStatus.FAILED
    media.failed_reason = "Video upload cancelled by user."
    db.add(media)

    insert_log = create_status_change_log(
        media_id=media_uuid,
        status=MediaStatus.FAILED,
        detail="Video upload cancelled by user.",
    )
    db.add(insert_log)
    await db.commit()

    _cleanup_video_temp_artifacts(
        media_uuid,
        [local_video_path] if local_video_path else None,
    )

    await manager.send_status(
        user_id=str(current_user.id),
        media_id=str(media_uuid),
        status=MediaStatus.FAILED.value,
        worker=None,
        failed_reason=media.failed_reason,
    )

    return JSONResponse(content={"status": "cancelled"})


@router.websocket("/ws/updates")
async def websocket_updates(
    websocket: WebSocket,
    token: str | None = Query(None),
):
    cookie_token = websocket.cookies.get("access_token")
    origin = websocket.headers.get("origin")
    # CORS middleware does not cover WebSocket handshakes. Ambient cookies may
    # only be used by trusted browser origins; explicit non-browser tokens can
    # omit Origin, but a supplied untrusted Origin is never accepted.
    if (cookie_token or origin) and not is_trusted_request_origin(websocket, origin):
        await websocket.close(code=1008)
        return
    auth_token = cookie_token or token

    if not auth_token:
        await websocket.close(code=1008)
        return

    # 1. Open a short-lived DB session JUST to authenticate the user
    try:
        async with AsyncSessionLocal() as db:
            user = await get_current_user_from_token(token=auth_token, db=db)
    except Exception as exc:
        logger.warning("WebSocket authentication failed (%s)", type(exc).__name__)
        await websocket.close(code=1008)
        return

    # The 'async with' block ends here. The DB connection is now safely returned
    # to the pool, but we still have the `user` object in memory!

    await manager.connect(user.id, websocket)

    async def recheck_authentication():
        while True:
            await asyncio.sleep(WEBSOCKET_AUTH_CHECK_INTERVAL_SECONDS)
            try:
                async with AsyncSessionLocal() as db:
                    await get_current_user_from_token(token=auth_token, db=db)
            except HTTPException:
                await websocket.close(code=1008)
                return
            except Exception:
                await websocket.close(code=1011)
                return

    auth_task = asyncio.create_task(recheck_authentication())
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(user.id, websocket)
        auth_task.cancel()
        await asyncio.gather(auth_task, return_exceptions=True)
