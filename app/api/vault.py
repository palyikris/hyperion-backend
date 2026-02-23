from fastapi import APIRouter, Depends, Query, status, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, asc
from typing import Optional, List

from app.database import get_db
from app.api.deps import get_current_user
from app.models.db.Media import Media
from app.models.upload.MediaStatus import MediaStatus

router = APIRouter()


@router.get("/vault", status_code=status.HTTP_200_OK)
async def get_media_vault(
    search: Optional[str] = Query(
        None, description="Search by filename within metadata"
    ),
    status_filter: Optional[MediaStatus] = Query(None, alias="status"),
    order_by: str = Query(
        "created_at", description="Sort field: created_at, filename, or status"
    ),
    direction: str = Query("desc", regex="^(asc|desc)$"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Retrieves the user's personal media library with advanced search and filtering.
    """
    query = select(Media).where(Media.uploader_id == current_user.id)

    if status_filter:
        query = query.where(Media.status == status_filter)

    if search:
        query = query.where(
            Media.initial_metadata["filename"].as_string().ilike(f"%{search}%")
        )

    column_map = {
        "created_at": Media.created_at,
        "status": Media.status,
        "filename": Media.initial_metadata["filename"].as_string(),
    }

    sort_column = column_map.get(order_by, Media.created_at)
    if direction == "desc":
        query = query.order_by(desc(sort_column))
    else:
        query = query.order_by(asc(sort_column))

    query = query.offset(offset).limit(limit)

    result = await db.execute(query)
    records = result.scalars().all()

    return JSONResponse(
        content={
            "total": len(records),
            "items": [
                {
                    "id": str(media.id),
                    "filename": media.initial_metadata.get("filename"),
                    "status": media.status.value,
                    "timestamp": media.created_at.isoformat(),
                    "metadata": media.initial_metadata,
                    "assigned_worker": media.assigned_worker,
                    "image_url": media.hf_path,
                    "technical_metadata": media.technical_metadata,
                    "updated_at": media.updated_at.isoformat(),
                }
                for media in records
            ],
        }
    )


@router.delete("/vault/{id}", status_code=status.HTTP_200_OK)
async def delete_media(
    id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Delete a media item from the user's vault.
    """
    query = select(Media).where(Media.id == id, Media.uploader_id == current_user.id)
    result = await db.execute(query)
    media = result.scalar_one_or_none()

    if not media:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Media not found or you don't have permission to delete it",
        )

    await db.delete(media)
    await db.commit()

    return JSONResponse(content={"detail": "Media deleted successfully"})
