from fastapi import APIRouter, Depends, Query, status, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, asc, update, func
from typing import Optional, List

from app.database import get_db
from app.api.deps import get_current_user
from app.models.db.Media import Media
from app.models.db.VideoDetection import VideoDetection
from app.models.upload.MediaStatus import MediaStatus
from app.models.vault.VaultResponse import VaultResponse
from app.api.upload_utils.hf_upload import delete_from_hf
from app.api.vault_utils.temp_file_finder import find_temp_video_file

router = APIRouter()


@router.get(
    "/vault",
    status_code=status.HTTP_200_OK,
    response_model=VaultResponse,
)
async def get_media_vault(
    search: Optional[str] = Query(
        None, description="Search by filename within metadata"
    ),
    status_filter: Optional[MediaStatus] = Query(None, alias="status"),
    order_by: str = Query(
        "created_at", description="Sort field: created_at, filename, or status"
    ),
    direction: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(20, ge=1, le=100, description="Number of items per page"),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Retrieves the user's personal media library with advanced search and filtering.
    """
    query = select(Media).where(Media.uploader_id == current_user.id)
    video_query = select(VideoDetection).where(
        VideoDetection.media_id.in_(
            select(Media.id).where(Media.uploader_id == current_user.id)
        )
    )

    if status_filter:
        query = query.where(Media.status == status_filter)
        video_query = video_query.where(
            VideoDetection.media_id.in_(
                select(Media.id).where(
                    Media.uploader_id == current_user.id, Media.status == status_filter
                )
            )
        )

    if search:
        query = query.where(
            Media.initial_metadata["filename"].as_string().ilike(f"%{search}%")
        )
        video_query = video_query.where(
            VideoDetection.media_id.in_(
                select(Media.id).where(
                    Media.uploader_id == current_user.id,
                    Media.initial_metadata["filename"].as_string().ilike(f"%{search}%"),
                )
            )
        )

    column_map = {
        "created_at": Media.created_at,
        "status": Media.status,
        "filename": Media.initial_metadata["filename"].as_string(),
    }

    sort_column = column_map.get(order_by, Media.created_at)
    if direction == "desc":
        query = query.order_by(desc(sort_column))
        video_query = video_query.order_by(desc(VideoDetection.created_at))
    else:
        query = query.order_by(asc(sort_column))
        video_query = video_query.order_by(asc(VideoDetection.created_at))

    offset = (page - 1) * page_size

    count_query = (
        select(func.count())
        .select_from(Media)
        .where(Media.uploader_id == current_user.id)
    )
    count_video_query = (
        select(func.count())
        .select_from(VideoDetection)
        .where(
            VideoDetection.media_id.in_(
                select(Media.id).where(Media.uploader_id == current_user.id)
            )
        )
    )

    if status_filter:
        count_query = count_query.where(Media.status == status_filter)
        count_video_query = count_video_query.where(
            VideoDetection.media_id.in_(
                select(Media.id).where(
                    Media.uploader_id == current_user.id, Media.status == status_filter
                )
            )
        )
    if search:
        count_query = count_query.where(
            Media.initial_metadata["filename"].as_string().ilike(f"%{search}%")
        )
        count_video_query = count_video_query.where(
            VideoDetection.media_id.in_(
                select(Media.id).where(
                    Media.uploader_id == current_user.id,
                    Media.initial_metadata["filename"].as_string().ilike(f"%{search}%"),
                )
            )
        )

    count_result = await db.execute(count_query)
    count_video_result = await db.execute(count_video_query)
    total = count_result.scalar_one()
    total_video_detections = count_video_result.scalar_one()

    query = query.offset(offset).limit(page_size)
    video_query = video_query.offset(offset).limit(page_size)

    result = await db.execute(query)
    video_result = await db.execute(video_query)

    records = result.scalars().all()
    video_detections = video_result.scalars().all()

    return JSONResponse(
        content={
            "total": total + total_video_detections,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + total_video_detections + page_size - 1)
            // page_size,
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
                for media in records
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
                for video_det in video_detections
            ],
        }
    )


@router.delete("/vault/all", status_code=status.HTTP_200_OK)
async def delete_all_media(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Delete all media items from the user's vault, including images from HF dataset.
    """
    query = select(Media).where(Media.uploader_id == current_user.id)
    result = await db.execute(query)
    media_items = result.scalars().all()

    if not media_items:
        return JSONResponse(
            content={"detail": "No media found to delete", "deleted_count": 0}
        )

    deleted_count = 0
    media_ids = [media.id for media in media_items]

    await db.execute(
        update(Media)
        .where(Media.original_media_id.in_(media_ids))
        .values(original_media_id=None)
    )

    for media in media_items:
        # Delete from HF dataset if hf_path exists
        if media.hf_path:
            await delete_from_hf(media.hf_path, media.id)

        await db.delete(media)
        deleted_count += 1

    await db.commit()

    return JSONResponse(
        content={
            "detail": "All media deleted successfully",
            "deleted_count": deleted_count,
        }
    )


@router.delete("/vault/{id}", status_code=status.HTTP_200_OK)
async def delete_media(
    id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Delete a media item from the user's vault, including the image from HF dataset.
    If the media is a video, also check for its temp file.
    """
    query = select(Media).where(Media.id == id, Media.uploader_id == current_user.id)
    result = await db.execute(query)
    media = result.scalar_one_or_none()
    video_detections_query = select(VideoDetection).where(VideoDetection.media_id == id)
    video_detections_result = await db.execute(video_detections_query)
    video_detections = video_detections_result.scalars().all()

    if not media:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Media not found or you don't have permission to delete it",
        )

    temp_file_path = None
    if (
        hasattr(media, "media_type")
        and getattr(media, "media_type", None)
        and str(media.media_type).lower() == "video"
    ):
        local_video_path = None
        if media.technical_metadata and isinstance(media.technical_metadata, dict):
            local_video_path = media.technical_metadata.get("local_video_path")
        if local_video_path:
            import os

            if os.path.exists(local_video_path):
                temp_file_path = local_video_path
        else:
            filename = (
                media.initial_metadata.get("filename")
                if media.initial_metadata
                else None
            )
            if filename:
                temp_file_path = find_temp_video_file(filename)

    # remove the temp file if found
    if temp_file_path:
        import os

        try:
            os.remove(temp_file_path)
            temp_file_status = f"Temp video file deleted: {temp_file_path}"
        except Exception as e:
            temp_file_status = f"Temp video file found but could not be deleted: {temp_file_path} ({e})"
    else:
        temp_file_status = None

    if video_detections:
        for detection in video_detections:
            await delete_from_hf(detection.frame_hf_path, detection.id)

    if media.hf_path:
        await delete_from_hf(media.hf_path, media.id)

    await db.execute(
        update(Media)
        .where(Media.original_media_id == media.id)
        .values(original_media_id=None)
    )

    await db.delete(media)
    await db.commit()

    detail_msg = "Media deleted successfully"
    if temp_file_status:
        detail_msg += f"; {temp_file_status}"
    return JSONResponse(content={"detail": detail_msg})
