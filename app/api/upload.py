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
from app.api.upload_utils.conn_manager import worker_signal, manager
from PIL import Image
import io
import uuid
import asyncio
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
)
from app.api.upload_utils.video_thumbnail import extract_video_thumbnail
from app.api.upload_utils.hf_upload import process_hf_upload
from huggingface_hub import HfApi, CommitOperationAdd
import shutil
from app.models.db.Media import MediaType
from app.models.db.VideoDetection import VideoDetection

router = APIRouter()

THUMBNAIL_SIZE = (400, 400)


def _process_image_to_temp(
    content: bytes, media_id: uuid.UUID
) -> tuple[int, int, str, str]:
    """
    CPU-bound image processing: extract dimensions, create thumbnail, and save both to temp files.
    This function runs in a thread pool to avoid blocking the event loop.
    Returns: (width, height, content_temp_path, thumbnail_temp_path)
    """
    img = Image.open(io.BytesIO(content))
    width, height = img.size

    thumbnail_img = img.copy()
    thumbnail_img.thumbnail(THUMBNAIL_SIZE)
    if thumbnail_img.mode != "RGB":
        thumbnail_img = thumbnail_img.convert("RGB")

    # Save original content to temp file
    content_temp_path = os.path.join(tempfile.gettempdir(), f"{media_id}_content")
    with open(content_temp_path, "wb") as f:
        f.write(content)

    # Save thumbnail to temp file
    thumbnail_temp_path = os.path.join(tempfile.gettempdir(), f"{media_id}_thumbnail")
    thumbnail_img.save(thumbnail_temp_path, format="JPEG", quality=85, optimize=True)

    return width, height, content_temp_path, thumbnail_temp_path


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
    media_records = []
    files_to_process = []

    for file in files:
        content = await file.read()
        media_id = uuid.uuid4()

        # Run CPU-bound image processing in a thread and save to temp files
        # This frees RAM immediately after processing each file
        width, height, content_path, thumbnail_path = await asyncio.to_thread(
            _process_image_to_temp, content, media_id
        )
        # Release the in-memory content immediately
        del content

        new_media = Media(
            id=media_id,
            uploader_id=current_user.id,
            status=MediaStatus.PENDING,
            initial_metadata={
                "filename": file.filename,
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

        await manager.send_status(
            user_id=str(current_user.id),
            media_id=str(media_id),
            status=MediaStatus.PENDING.value,
            worker=None,
        )

        media_records.append(new_media)
        # Pass file paths instead of raw bytes
        files_to_process.append((media_id, file.filename, content_path, thumbnail_path))

    db.add_all(media_records)
    await db.commit()

    background_tasks.add_task(
        process_hf_upload,
        files_to_process,
        current_user.id,
    )

    return JSONResponse(
        content={
            "message": f"{len(media_records)} files uploaded successfully",
            "media_ids": [str(m.id) for m in media_records],
        },
        status_code=status.HTTP_201_CREATED,
    )


@router.get(
    "/recents",
    status_code=status.HTTP_200_OK,
    # response_model=RecentsResponse, # You can comment this out or update the Pydantic model
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


@router.post("/video/init")
async def video_init(
    filename: str = Form(...),
    total_size: int = Form(None),
    total_chunks: int = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    media_id = uuid.uuid4()  # Use UUID type throughout
    temp_dir = os.path.join(tempfile.gettempdir(), f"video_upload_{media_id}")
    os.makedirs(temp_dir, exist_ok=True)

    new_media = Media(
        id=media_id,
        uploader_id=current_user.id,
        status=MediaStatus.PENDING,
        initial_metadata={
            "filename": filename,
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
    await db.commit()

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

    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Media not found or access denied")

    temp_dir = os.path.join(tempfile.gettempdir(), f"video_upload_{media_uuid}")
    if not os.path.isdir(temp_dir):
        raise HTTPException(status_code=404, detail="Temp dir for media_id not found")
    chunk_path = os.path.join(temp_dir, f"chunk_{chunk_index}")
    if os.path.exists(chunk_path):
        return JSONResponse(
            content={
                "status": "duplicate",
                "detail": f"Chunk {chunk_index} already uploaded",
            },
            status_code=200,
        )
    chunk_bytes = await chunk.read()
    await asyncio.to_thread(
        save_video_chunk_to_temp, str(media_uuid), chunk_index, chunk_bytes
    )
    del chunk_bytes
    return JSONResponse(content={"status": "ok"})


@router.post("/video/complete/{media_id}")
async def video_complete(
    media_id: str,
    total_chunks: int = Form(...),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    try:
        media_uuid = uuid.UUID(media_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid media_id format")
    temp_dir = os.path.join(tempfile.gettempdir(), f"video_upload_{media_uuid}")

    missing_chunks = [
        i
        for i in range(total_chunks)
        if not os.path.exists(os.path.join(temp_dir, f"chunk_{i}"))
    ]
    if missing_chunks:
        return JSONResponse(
            content={"status": "incomplete", "missing_chunks": missing_chunks},
            status_code=400,
        )
    try:
        video_path = await asyncio.to_thread(
            assemble_video_from_chunks, str(media_uuid), total_chunks
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Assembly failed: {e}")

    try:
        thumbnail_path, width, height = await asyncio.to_thread(
            extract_video_thumbnail, video_path, str(media_uuid)
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Thumbnail generation failed: {e}")

    HF_TOKEN = os.getenv("HF_TOKEN")
    HF_REPO_ID = os.getenv("HF_REPO_ID")
    if not HF_REPO_ID or not HF_TOKEN:
        raise HTTPException(status_code=500, detail="HF_REPO_ID or HF_TOKEN not set")
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"HF upload failed: {e}")

    result = await db.execute(
        select(Media)
        .where(Media.id == media_uuid)
        .where(Media.uploader_id == current_user.id)
    )
    media = result.scalar_one_or_none()
    if not media:
        raise HTTPException(status_code=404, detail="Media not found")
    if not media.initial_metadata:
        media.initial_metadata = {}
    media.initial_metadata["thumbnail_hf_url"] = thumb_hf_path
    media.hf_path = thumb_hf_path
    if not media.technical_metadata:
        media.technical_metadata = {}
    media.technical_metadata["local_video_path"] = video_path
    media.status = MediaStatus.PENDING
    db.add(media)
    await db.commit()

    video_hf_path = f"media/{current_user.id}/{date_str}/{media_uuid}_full_video.mp4"

    background_tasks.add_task(
        process_video_hf_upload,
        media_id=media_id,
        user_id=current_user.id,
        local_video_path=video_path,
        video_hf_path=video_hf_path,
    )

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
    result = await db.execute(select(Media).where(Media.id == media_uuid))
    media = result.scalar_one_or_none()
    if not media:
        raise HTTPException(status_code=404, detail="Media not found")
    media.status = MediaStatus.FAILED
    db.add(media)

    insert_log = create_status_change_log(
        media_id=media_uuid,
        status=MediaStatus.FAILED,
    )
    db.add(insert_log)
    await db.commit()

    temp_dir = os.path.join(tempfile.gettempdir(), f"video_upload_{media_uuid}")
    if os.path.isdir(temp_dir):
        shutil.rmtree(temp_dir)

    await manager.send_status(
        user_id=str(current_user.id),
        media_id=str(media_uuid),
        status=MediaStatus.FAILED.value,
        worker=None,
    )

    return JSONResponse(content={"status": "cancelled"})


@router.websocket("/ws/updates")
async def websocket_updates(
    websocket: WebSocket,
    token: str | None = Query(None),
    # REMOVE db = Depends(get_db)
):
    auth_token = websocket.cookies.get("access_token") or token

    if not auth_token:
        await websocket.close(code=1008)
        return

    # 1. Open a short-lived DB session JUST to authenticate the user
    try:
        async with AsyncSessionLocal() as db:
            user = await get_current_user_from_token(token=auth_token, db=db)
    except Exception as e:
        print(f"WebSocket auth error: {e}")
        await websocket.close(code=1008)
        return

    # The 'async with' block ends here. The DB connection is now safely returned
    # to the pool, but we still have the `user` object in memory!

    await manager.connect(user.id, websocket)

    # 2. Enter the infinite loop safely
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(user.id)
