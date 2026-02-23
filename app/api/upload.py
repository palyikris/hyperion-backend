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
from app.api.media_log_utils import create_status_change_log
from app.models.db.Media import Media
from app.models.upload.MediaStatus import MediaStatus
from app.api.upload_utils.conn_manager import worker_signal, manager
from PIL import Image
import io
import uuid
from fastapi.responses import JSONResponse
from fastapi import HTTPException, status
from app.models.upload.UploadResponse import UploadResponse, RecentsResponse
from app.api.upload_utils.hf_upload import process_hf_upload
from sqlalchemy import select


router = APIRouter()


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
        img = Image.open(io.BytesIO(content))
        width, height = img.size

        media_id = uuid.uuid4()
        new_media = Media(
            id=media_id,
            uploader_id=current_user.id,
            status=MediaStatus.PENDING,
            initial_metadata={
                "filename": file.filename,
                "size": len(content),
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
        await file.seek(0)  # reset file pointer
        files_to_process.append((media_id, file.filename, content))

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
                }
                for media in recent_media
            ],
        }
    )


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
