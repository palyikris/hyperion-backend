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
from app.models.db.Media import Media
from app.models.upload.MediaStatus import MediaStatus
from app.models.db.MediaLog import MediaLog
from app.api.upload_utils.conn_manager import worker_signal, manager
from PIL import Image
import io
import uuid
from fastapi.responses import JSONResponse
from fastapi import HTTPException, status
from app.models.upload.UploadResponse import UploadResponse
from datetime import datetime, timezone
from app.api.upload_utils.hf_upload import process_hf_upload

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

        insert_log = MediaLog(
            media_id=media_id,
            status=MediaStatus.PENDING,
            worker=None,
            timestamp=datetime.now(timezone.utc),
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
        files_to_process.append((file.filename, content))

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


@router.websocket("/ws/updates")
async def websocket_updates(
    websocket: WebSocket,
    token: str = Query(...),  # frontend sends: ws://.../ws/updates?token=JWT_HERE
    db: AsyncSession = Depends(get_db),
):
    try:
        user = await get_current_user_from_token(token)
    except Exception:
        await websocket.close(code=401)
        return

    await manager.connect(user.id, websocket)

    try:
        while True:
            # not expecting any messages from client, but keeping connection alive
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(user.id)
