from fastapi import APIRouter, UploadFile, File, Depends, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.api.deps import get_current_user
from app.models.db.Media import Media
from app.models.upload.MediaStatus import MediaStatus
from app.api.dashboard_utils.conn_manager import worker_signal, manager
from PIL import Image
import io
import uuid

router = APIRouter()


async def upload_to_hf_task(media_ids: list[uuid.UUID], user_id: str):
    """Background task to stream files to HF and wake workers."""
    # 1. Implementation for huggingface_hub.upload_file goes here
    # 2. Update status to UPLOADED
    # 3. Wake workers
    async with worker_signal:
        worker_signal.notify_all()


@router.post("/files")
async def batch_upload(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    media_records = []

    for file in files:
        # Phase 1: Guardrail Extraction (Fast)
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
        media_records.append(new_media)
        await file.seek(0)  # Reset stream for background upload

    db.add_all(media_records)
    await db.commit()

    # Trigger background HF pipeline
    background_tasks.add_task(
        upload_to_hf_task, [m.id for m in media_records], current_user.id
    )

    return {"message": "Batch accepted", "ids": [str(m.id) for m in media_records]}
