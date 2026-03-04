from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
import uuid
import json

from app.database import get_db
from app.api.deps import get_current_user
from app.models.db.Media import Media
from app.models.db.Detection import Detection
from app.models.lab.MediaResponse import MediaResponse, MediaPatchRequest
from app.api.medialog_utils.media_log_utils import create_status_change_log


router = APIRouter()


@router.get(
    "/{media_id}",
    status_code=status.HTTP_200_OK,
    response_model=MediaResponse,
)
async def get_media(
    media_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Get a media item by ID.
    
    Only returns media that belongs to the authenticated user.
    """
    try:
        media_uuid = uuid.UUID(media_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid media ID format",
        )

    query = (
        select(Media)
        .options(selectinload(Media.detections))
        .where(
            Media.id == media_uuid,
            Media.uploader_id == current_user.id,
        )
    )

    result = await db.execute(query)
    media = result.scalar_one_or_none()

    if not media:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Media not found",
        )

    return {
        "id": str(media.id),
        "uploader_id": media.uploader_id,
        "status": media.status.value,
        "hf_path": media.hf_path,
        "initial_metadata": media.initial_metadata,
        "technical_metadata": media.technical_metadata,
        "assigned_worker": media.assigned_worker,
        "created_at": media.created_at,
        "updated_at": media.updated_at,
        "lat": media.lat,
        "lng": media.lng,
        "altitude": media.altitude,
        "address": media.address,
        "has_trash": media.has_trash,
        "confidence": media.confidence,
        "failed_reason": media.failed_reason,
        "original_media_id": str(media.original_media_id) if media.original_media_id else None,
        "detections": [
            {
                "id": str(d.id),
                "label": d.label,
                "confidence": d.confidence,
                "bbox": d.bbox,
                "area_sqm": d.area_sqm,
            }
            for d in media.detections
        ],
    }


@router.patch(
    "/{media_id}",
    status_code=status.HTTP_200_OK,
    response_model=MediaResponse,
)
async def patch_media(
    media_id: str,
    patch_data: MediaPatchRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Update a media item by ID.
    
    Updates location data and intelligently merges detections:
    - Deletes detections not in the new list
    - Adds new detections
    - Updates existing detections
    - Sets confidence to 1.0 (max) for human-validated detections
    """
    try:
        media_uuid = uuid.UUID(media_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid media ID format",
        )

    query = (
        select(Media)
        .options(selectinload(Media.detections))
        .where(
            Media.id == media_uuid,
            Media.uploader_id == current_user.id,
        )
    )

    result = await db.execute(query)
    media = result.scalar_one_or_none()

    if not media:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Media not found",
        )

    if patch_data.lat is not None:
        media.lat = patch_data.lat
    if patch_data.lng is not None:
        media.lng = patch_data.lng
    if patch_data.altitude is not None:
        media.altitude = patch_data.altitude
    if patch_data.address is not None:
        media.address = patch_data.address

    if patch_data.detections is not None:
        # build a set of new detection signatures (label + bbox) for matching
        new_detection_map = {
            (det.label, json.dumps(det.bbox, sort_keys=True)): det
            for det in patch_data.detections
        }

        processed_signatures = set()

        for existing_det in media.detections[:]:  # slice to avoid iteration issues
            det_signature = (existing_det.label, json.dumps(existing_det.bbox, sort_keys=True))

            if det_signature in new_detection_map:
                new_det_data = new_detection_map[det_signature]
                existing_det.confidence = 1.0  # human validation
                if new_det_data.area_sqm is not None:
                    existing_det.area_sqm = new_det_data.area_sqm
                processed_signatures.add(det_signature)
            else:
                media.detections.remove(existing_det)

        for det_signature, new_det_data in new_detection_map.items():
            if det_signature not in processed_signatures:
                new_detection = Detection(
                    media_id=media.id,
                    label=new_det_data.label,
                    confidence=1.0,
                    bbox=new_det_data.bbox,
                    area_sqm=new_det_data.area_sqm,
                    is_manual=True,
                )
                media.detections.append(new_detection)

    await db.commit()

    await db.refresh(media)

    validation_log = create_status_change_log(
        media_id=media.id,
        status=media.status,
        detail="Detections and location validated",
        worker_name="You",
    )
    db.add(validation_log)
    await db.commit()

    return {
        "id": str(media.id),
        "uploader_id": media.uploader_id,
        "status": media.status.value,
        "hf_path": media.hf_path,
        "initial_metadata": media.initial_metadata,
        "technical_metadata": media.technical_metadata,
        "assigned_worker": media.assigned_worker,
        "created_at": media.created_at,
        "updated_at": media.updated_at,
        "lat": media.lat,
        "lng": media.lng,
        "altitude": media.altitude,
        "address": media.address,
        "has_trash": media.has_trash,
        "confidence": media.confidence,
        "failed_reason": media.failed_reason,
        "original_media_id": str(media.original_media_id) if media.original_media_id else None,
        "detections": [
            {
                "id": str(d.id),
                "label": d.label,
                "confidence": d.confidence,
                "bbox": d.bbox,
                "area_sqm": d.area_sqm,
            }
            for d in media.detections
        ],
    }
