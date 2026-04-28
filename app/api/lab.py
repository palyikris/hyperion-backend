from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
import uuid
import json

from app.database import get_db
from app.api.deps import get_current_user
from app.models.db.Media import Media
from app.models.db.Detection import Detection
from app.models.db.VideoDetection import VideoDetection
from app.models.lab.MediaResponse import (
    MediaResponse,
    MediaPatchRequest,
    VideoDetectionResponse,
)
from app.api.medialog_utils.media_log_utils import create_status_change_log
from app.api.upload_utils.metadata_extractor import get_address_from_coords


router = APIRouter()

ADDRESS_REFRESH_DISTANCE_METERS = 100


def _serialize_detection(detection: Detection) -> dict:
    return {
        "id": str(detection.id),
        "label": detection.label,
        "confidence": detection.confidence,
        "bbox": detection.bbox,
        "area_sqm": detection.area_sqm,
    }


def _serialize_media(media: Media) -> dict:
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
        "original_media_id": (
            str(media.original_media_id) if media.original_media_id else None
        ),
        "detections": [
            _serialize_detection(detection) for detection in media.detections
        ],
    }


def _serialize_video_detection(video_det: VideoDetection) -> dict:
    return {
        "id": str(video_det.id),
        "media_id": str(video_det.media_id),
        "lat": video_det.lat,
        "lng": video_det.lng,
        "altitude": video_det.altitude,
        "address": video_det.address,
        "label": video_det.label,
        "confidence": video_det.confidence,
        "bbox": video_det.bbox,
        "timestamp_in_video": (
            int(video_det.timestamp_in_video)
            if video_det.timestamp_in_video is not None
            else None
        ),
        "frame_hf_path": video_det.frame_hf_path,
        "created_at": video_det.created_at,
        "area_sqm": video_det.area_sqm,
    }


@router.get(
    "/image/{media_id}",
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

    return _serialize_media(media)


@router.get(
    "/video/{media_id}",
    status_code=status.HTTP_200_OK,
    response_model=VideoDetectionResponse,
)
async def get_video_media(
    media_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Get a video media item by ID.

    Only returns media that belongs to the authenticated user.
    """
    try:
        media_uuid = uuid.UUID(media_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid media ID format",
        )

    query = select(VideoDetection).where(
        VideoDetection.id == media_uuid,
        select(Media)
        .where(
            Media.id == VideoDetection.media_id,
            Media.uploader_id == current_user.id,
        )
        .exists(),
    )

    result = await db.execute(query)
    detections = result.scalar_one_or_none()

    if not detections:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Media not found",
        )

    return _serialize_video_detection(detections)


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

    original_lat = media.lat
    original_lng = media.lng

    if patch_data.lat is not None:
        media.lat = patch_data.lat
    if patch_data.lng is not None:
        media.lng = patch_data.lng
    if patch_data.altitude is not None:
        media.altitude = patch_data.altitude
    if patch_data.address is not None:
        media.address = patch_data.address
    else:
        location_updated = patch_data.lat is not None or patch_data.lng is not None
        new_lat = media.lat
        new_lng = media.lng

        if location_updated and new_lat is not None and new_lng is not None:
            should_refresh_address = False

            if original_lat is None or original_lng is None:
                should_refresh_address = True
            else:
                distance_query = select(
                    func.ST_Distance(
                        func.Geography(
                            func.ST_SetSRID(
                                func.ST_MakePoint(original_lng, original_lat), 4326
                            )
                        ),
                        func.Geography(
                            func.ST_SetSRID(func.ST_MakePoint(new_lng, new_lat), 4326)
                        ),
                    )
                )
                distance_result = await db.execute(distance_query)
                distance_m = distance_result.scalar_one_or_none() or 0
                should_refresh_address = distance_m >= ADDRESS_REFRESH_DISTANCE_METERS

            if should_refresh_address:
                media.address = await get_address_from_coords(new_lat, new_lng)

    # Keep PostGIS geometry in sync even when address is manually provided.
    if (
        (patch_data.lat is not None or patch_data.lng is not None)
        and media.lat is not None
        and media.lng is not None
    ):
        media.location = func.ST_SetSRID(func.ST_MakePoint(media.lng, media.lat), 4326)

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
                was_changed = False

                if (
                    new_det_data.area_sqm is not None
                    and new_det_data.area_sqm != existing_det.area_sqm
                ):
                    existing_det.area_sqm = new_det_data.area_sqm
                    was_changed = True

                if was_changed:
                    existing_det.confidence = 1.0  # human validation

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
        detail="Detections or location validated by You",
        worker_name="You",
    )
    db.add(validation_log)
    await db.commit()

    return _serialize_media(media)
