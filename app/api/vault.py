import logging
from fastapi import APIRouter, Depends, Query, status, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, asc, update, func, literal, union_all
from sqlalchemy.orm import selectinload
from typing import Optional

from app.database import get_db
from app.api.deps import get_current_user
from app.models.db.Media import Media
from app.models.db.VideoDetection import VideoDetection
from app.models.upload.MediaStatus import MediaStatus
from app.models.vault.VaultResponse import VaultResponse
from app.api.upload_utils.hf_upload import delete_from_hf
from app.api.vault_utils.temp_file_finder import find_temp_video_file
from app.api.map import invalidate_map_stats_cache
from app.api.stats_utils.cache import invalidate_stats_cache

router = APIRouter()

# Configure logger for this module
logger = logging.getLogger("vault_api")
if not logger.hasHandlers():
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s in %(module)s: %(message)s"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
logger.setLevel(logging.INFO)


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

    filename = func.coalesce(Media.initial_metadata["filename"].as_string(), "")
    column_map = {
        "created_at": Media.created_at,
        "status": Media.status,
        "filename": filename,
    }
    sort_column = column_map.get(order_by, Media.created_at)
    video_sort_column = (
        VideoDetection.created_at
        if order_by not in ("filename", "status")
        else sort_column
    )
    filters = [Media.uploader_id == current_user.id]
    if status_filter:
        filters.append(Media.status == status_filter)
    if search:
        filters.append(filename.ilike(f"%{search}%"))

    # Page one combined sequence of lightweight references. Applying LIMIT to
    # each entity separately would return up to twice the requested page size.
    entries = union_all(
        select(
            Media.id.label("item_id"),
            literal("media").label("item_type"),
            sort_column.label("sort_value"),
        ).where(*filters),
        select(
            VideoDetection.id.label("item_id"),
            literal("video").label("item_type"),
            video_sort_column.label("sort_value"),
        )
        .join(Media, VideoDetection.media_id == Media.id)
        .where(*filters),
    ).subquery()
    count_result = await db.execute(select(func.count()).select_from(entries))
    total = count_result.scalar_one()
    ordering = desc if direction == "desc" else asc
    page_result = await db.execute(
        select(entries.c.item_id, entries.c.item_type)
        .order_by(
            ordering(entries.c.sort_value).nulls_last(),
            entries.c.item_type,
            entries.c.item_id,
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    page_entries = page_result.all()
    positions = {
        (row.item_type, row.item_id): index
        for index, row in enumerate(page_entries)
    }
    media_ids = [row.item_id for row in page_entries if row.item_type == "media"]
    video_ids = [row.item_id for row in page_entries if row.item_type == "video"]
    records = []
    video_detections = []
    if media_ids:
        result = await db.execute(
            select(Media).where(
                Media.id.in_(media_ids), Media.uploader_id == current_user.id,
            )
        )
        records = sorted(
            result.scalars().all(),
            key=lambda item: positions[("media", item.id)],
        )
    if video_ids:
        result = await db.execute(
            select(VideoDetection)
            .join(Media, VideoDetection.media_id == Media.id)
            .options(selectinload(VideoDetection.media))
            .where(
                VideoDetection.id.in_(video_ids),
                Media.uploader_id == current_user.id,
            )
        )
        video_detections = sorted(
            result.scalars().all(),
            key=lambda item: positions[("video", item.id)],
        )

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
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
                "filename": (video_det.media.initial_metadata or {}).get("filename"),
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
        invalidate_map_stats_cache(str(current_user.id))
        invalidate_stats_cache(str(current_user.id))
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
    logger.info(f"Set original_media_id=None for media_ids: {media_ids}")

    for media in media_items:
        # Delete from HF dataset if hf_path exists
        if media.hf_path:
            logger.info(f"Deleting from HF: hf_path={media.hf_path}, id={media.id}")
            await delete_from_hf(media.hf_path, media.id)

        await db.delete(media)
        deleted_count += 1
        logger.info(f"Deleted media id={media.id}")

    await db.commit()
    invalidate_map_stats_cache(str(current_user.id))
    invalidate_stats_cache(str(current_user.id))

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
    logger.info(f"Delete single media query: {query}")
    result = await db.execute(query)
    media = result.scalar_one_or_none()
    video_detections_query = (
        select(VideoDetection).where(VideoDetection.media_id == id)
        .execution_options(include_cleaned_detections=True)
    )
    logger.info(f"Delete single media video detections query: {video_detections_query}")
    video_detections_result = await db.execute(video_detections_query)
    video_detections = video_detections_result.scalars().all()
    logger.info(f"Found {len(video_detections)} video detections for media id={id}")

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
                logger.info(f"Found local video path for deletion: {temp_file_path}")
        else:
            filename = (
                media.initial_metadata.get("filename")
                if media.initial_metadata
                else None
            )
            if filename:
                temp_file_path = find_temp_video_file(filename)
                logger.info(f"Found temp video file by filename: {temp_file_path}")

    # remove the temp file if found

    if temp_file_path:
        import os

        try:
            os.remove(temp_file_path)
            temp_file_status = f"Temp video file deleted: {temp_file_path}"
            logger.info(temp_file_status)
        except Exception as e:
            temp_file_status = f"Temp video file found but could not be deleted: {temp_file_path} ({e})"
            logger.warning(temp_file_status)
    else:
        temp_file_status = None

    if video_detections:
        for detection in video_detections:
            logger.info(
                f"Deleting video detection frame from HF: frame_hf_path={detection.frame_hf_path}, id={detection.id}"
            )
            await delete_from_hf(detection.frame_hf_path, detection.id)

    if media.hf_path:
        logger.info(f"Deleting media from HF: hf_path={media.hf_path}, id={media.id}")
        await delete_from_hf(media.hf_path, media.id)

    await db.execute(
        update(Media)
        .where(Media.original_media_id == media.id)
        .values(original_media_id=None)
    )
    logger.info(f"Set original_media_id=None for media.id={media.id}")

    await db.delete(media)
    logger.info(f"Deleted media id={media.id}")
    await db.commit()
    invalidate_map_stats_cache(str(current_user.id))
    invalidate_stats_cache(str(current_user.id))

    detail_msg = "Media deleted successfully"
    if temp_file_status:
        detail_msg += f"; {temp_file_status}"
    logger.info(detail_msg)
    return JSONResponse(content={"detail": detail_msg})
