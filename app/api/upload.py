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
from app.api.upload_utils.hf_upload import process_hf_upload
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
    response_model=RecentsResponse,
)
async def get_recents(
    db: AsyncSession = Depends(get_db), current_user=Depends(get_current_user)
):
    result = await db.execute(
        select(Media)
        .where(Media.uploader_id == current_user.id)
        .order_by(Media.created_at.desc())
        .limit(4)
    )
    recent_media = result.scalars().all()

    return JSONResponse(
        content={
            "total": len(recent_media),
            "items": [
                {
                    "id": str(media.id),
                    "filename": media.initial_metadata.get("filename"),
                    "status": media.status.value,
                    "timestamp": media.created_at.isoformat(),
                    "image_url": media.hf_path,
                    "metadata": media.initial_metadata,
                    "address": media.address,
                    "failed_reason": media.failed_reason,
                }
                for media in recent_media
            ],
        }
    )


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
    # Security: Only allow temp dirs in system temp, and check for path traversal
    # NOTE: A path traversal attack is already impossible here, because media_id is strictly validated as a UUID above.
    # This check is extra caution, but UUID validation alone is 100% protection.
    if not os.path.abspath(temp_dir).startswith(os.path.abspath(tempfile.gettempdir())):
        raise HTTPException(status_code=400, detail="Invalid temp dir path")
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
):
    try:
        media_uuid = uuid.UUID(media_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid media_id format")
    temp_dir = os.path.join(tempfile.gettempdir(), f"video_upload_{media_uuid}")
    # Security: Only allow temp dirs in system temp, and check for path traversal
    # NOTE: A path traversal attack is already impossible here, because media_id is strictly validated as a UUID above.
    # This check is extra caution, but UUID validation alone is 100% protection.
    if not os.path.abspath(temp_dir).startswith(os.path.abspath(tempfile.gettempdir())):
        raise HTTPException(status_code=400, detail="Invalid temp dir path")
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
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    try:
        media_uuid = uuid.UUID(media_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid media_id format")
    temp_dir = os.path.join(tempfile.gettempdir(), f"video_upload_{media_uuid}")
    # Security: Only allow temp dirs in system temp, and check for path traversal
    # NOTE: A path traversal attack is already impossible here, because media_id is strictly validated as a UUID above.
    # This check is extra caution, but UUID validation alone is 100% protection.
    if not os.path.abspath(temp_dir).startswith(os.path.abspath(tempfile.gettempdir())):
        raise HTTPException(status_code=400, detail="Invalid temp dir path")
    # Check all chunks exist before assembly
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

    # Upload thumbnail to HF
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

    result = await db.execute(select(Media).where(Media.id == media_uuid))
    media = result.scalar_one_or_none()
    if not media:
        raise HTTPException(status_code=404, detail="Media not found")
    media.status = MediaStatus.UPLOADED
    # Add thumbnail HF link and local video path to initial_metadata
    if not media.initial_metadata:
        media.initial_metadata = {}
    media.initial_metadata["thumbnail_hf_url"] = thumb_hf_path
    media.initial_metadata["local_video_path"] = (
        video_path  # So the AI worker can find the file
    )
    db.add(media)

    insert_log = create_status_change_log(
        media_id=media_uuid,
        status=MediaStatus.UPLOADED,
    )
    db.add(insert_log)
    await db.commit()

    await manager.send_status(
        user_id=str(current_user.id),
        media_id=str(media_uuid),
        status=MediaStatus.UPLOADED.value,
        worker=None,
        img_url=thumb_hf_path,
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
    # Security: Only allow temp dirs in system temp, and check for path traversal
    # NOTE: A path traversal attack is already impossible here, because media_id is strictly validated as a UUID above.
    # This check is extra caution, but UUID validation alone is 100% protection.
    if os.path.abspath(temp_dir).startswith(os.path.abspath(tempfile.gettempdir())):
        shutil.rmtree(temp_dir, ignore_errors=True)

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
    db: AsyncSession = Depends(get_db),
):
    auth_token = websocket.cookies.get("access_token") or token

    if not auth_token:
        await websocket.close(code=1008)
        return

    try:
        user = await get_current_user_from_token(token=auth_token, db=db)
    except Exception:
        await websocket.close(code=1008)
        return

    await manager.connect(user.id, websocket)

    try:
        while True:
            # not expecting any messages from client, but keeping connection alive
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(user.id)
