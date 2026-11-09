from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, update
from sqlalchemy.orm import selectinload
import uuid
import json
from datetime import datetime, timezone

from app.database import get_db
from app.api.deps import get_current_user
from app.models.db.Media import Media, MediaType
from app.models.db.Detection import Detection
from app.models.db.VideoDetection import VideoDetection
from app.models.lab.MediaResponse import (
    DetectionInput,
    MediaResponse,
    MediaPatchRequest,
    VideoDetectionResponse,
)
from app.api.medialog_utils.media_log_utils import create_status_change_log
from app.api.upload_utils.metadata_extractor import get_address_from_coords
from app.api.map import invalidate_map_stats_cache
from app.api.stats_utils.cache import invalidate_stats_cache


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
            if detection.deleted_at is None
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
        "timestamp_in_video": video_det.timestamp_in_video,
        "frame_hf_path": video_det.frame_hf_path,
        "created_at": video_det.created_at,
        "area_sqm": video_det.area_sqm,
    }


def _detection_signature(label: str, bbox: dict) -> tuple[str, str]:
    normalized_bbox = {key: float(value) for key, value in bbox.items()}
    return label, json.dumps(normalized_bbox, sort_keys=True)


def _update_media_summary(media: Media, count: int, confidence: float) -> None:
    media.has_trash = count > 0
    media.confidence = round(confidence * 100, 2) if count else 0.0
    metadata = dict(media.technical_metadata or {})
    metadata.update(
        has_trash=media.has_trash,
        confidence=media.confidence,
        detections_count=count,
    )
    media.technical_metadata = metadata


async def _refresh_video_summary(db: AsyncSession, media: Media) -> None:
    # Include inserted, edited and deleted detections in the aggregate query.
    await db.flush()
    result = await db.execute(
        select(
            func.count(VideoDetection.id),
            func.avg(VideoDetection.confidence),
        ).where(VideoDetection.media_id == media.id)
    )
    count, average_confidence = result.one()
    _update_media_summary(media, count, average_confidence or 0.0)


def _merge_image_detections(
    media: Media, detections: list[DetectionInput]
) -> None:
    """Merge the complete detection list submitted for an image."""
    active_detections = [d for d in media.detections if d.deleted_at is None]
    existing_by_id = {detection.id: detection for detection in active_detections}
    for detection in detections:
        original = existing_by_id.get(detection.id)
        if (
            original is not None
            and original.bbox != detection.bbox.model_dump()
        ):
            # A physical area inferred for the old box is no longer reliable.
            # Do not retain it even when an older client resubmits that value.
            detection.area_sqm = None

    new_detection_map = {
        _detection_signature(detection.label, detection.bbox.model_dump()): detection
        for detection in detections
    }
    processed_signatures: set[tuple[str, str]] = set()

    for existing_detection in active_detections:
        signature = _detection_signature(
            existing_detection.label, existing_detection.bbox
        )

        if signature not in new_detection_map:
            existing_detection.deleted_at = datetime.now(timezone.utc)
            continue

        new_detection_data = new_detection_map[signature]
        if (
            new_detection_data.area_sqm is not None
            and new_detection_data.area_sqm != existing_detection.area_sqm
        ):
            existing_detection.area_sqm = new_detection_data.area_sqm
            existing_detection.confidence = 1.0
            existing_detection.is_manual = True

        processed_signatures.add(signature)

    for signature, new_detection_data in new_detection_map.items():
        if signature in processed_signatures:
            continue

        media.detections.append(
            Detection(
                media_id=media.id,
                label=new_detection_data.label,
                confidence=1.0,
                bbox=new_detection_data.bbox.model_dump(),
                area_sqm=new_detection_data.area_sqm,
                is_manual=True,
            )
        )


async def _merge_video_detections(
    db: AsyncSession,
    video_detection: VideoDetection,
    detections: list[DetectionInput],
) -> bool:
    """Merge edits made on one video evidence frame.

    Returns True when the selected detection was deleted without a replacement.
    Additional submitted detections inherit the frame, timestamp and location of
    the selected detection. Exact matches already stored on that frame are not
    inserted again; a replacement keeps the selected ID if it matches a sibling.
    """
    unique_detections: dict[tuple[str, str], DetectionInput] = {}
    for detection in detections:
        signature = _detection_signature(detection.label, detection.bbox.model_dump())
        unique_detections.setdefault(signature, detection)

    if not unique_detections:
        video_detection.deleted_at = datetime.now(timezone.utc)
        return True

    # The caller holds the parent media lock, serializing sibling edits. Scope
    # deduplication to this evidence frame, never to other frames or videos.
    frame_result = await db.execute(
        select(VideoDetection).where(
            VideoDetection.media_id == video_detection.media_id,
            VideoDetection.frame_hf_path == video_detection.frame_hf_path,
            VideoDetection.timestamp_in_video == video_detection.timestamp_in_video,
            VideoDetection.id != video_detection.id,
        )
    )
    frame_detections = frame_result.scalars().all()
    active_ids = {video_detection.id, *(item.id for item in frame_detections)}
    if any(item.id is not None and item.id not in active_ids for item in detections):
        raise HTTPException(status_code=409, detail="Detections changed. Reload the media and try again.")

    existing_signature = _detection_signature(
        video_detection.label, video_detection.bbox
    )

    if existing_signature in unique_detections:
        current_data = unique_detections.pop(existing_signature)
        if (
            current_data.area_sqm is not None
            and current_data.area_sqm != video_detection.area_sqm
        ):
            video_detection.area_sqm = current_data.area_sqm
            video_detection.confidence = 1.0
            video_detection.is_manual = True
    else:
        replacement_signature, replacement = next(iter(unique_detections.items()))
        unique_detections.pop(replacement_signature)
        geometry_changed = video_detection.bbox != replacement.bbox.model_dump()
        video_detection.label = replacement.label
        video_detection.bbox = replacement.bbox.model_dump()
        video_detection.area_sqm = (
            None if geometry_changed else replacement.area_sqm
        )
        video_detection.confidence = 1.0
        video_detection.is_manual = True

    selected_signature = _detection_signature(
        video_detection.label, video_detection.bbox
    )
    sibling_signatures: set[tuple[str, str]] = set()
    for sibling in frame_detections:
        signature = _detection_signature(sibling.label, sibling.bbox)
        if signature == selected_signature:
            # Preserve the ID returned by this endpoint, but do not leave an
            # identical sibling behind when a replacement collides with it.
            sibling.deleted_at = datetime.now(timezone.utc)
        else:
            sibling_signatures.add(signature)

    for signature, detection in unique_detections.items():
        if signature in sibling_signatures:
            continue
        db.add(
            VideoDetection(
                media_id=video_detection.media_id,
                lat=video_detection.lat,
                lng=video_detection.lng,
                altitude=video_detection.altitude,
                location=(
                    f"SRID=4326;POINT({video_detection.lng} {video_detection.lat})"
                ),
                address=video_detection.address,
                label=detection.label,
                confidence=1.0,
                bbox=detection.bbox.model_dump(),
                timestamp_in_video=video_detection.timestamp_in_video,
                frame_hf_path=video_detection.frame_hf_path,
                is_manual=True,
                area_sqm=detection.area_sqm,
            )
        )

    return False


async def _mark_clean(
    media_id: uuid.UUID,
    detection_id: uuid.UUID | None,
    db: AsyncSession,
    user_id: str,
) -> Response:
    # Use the same parent lock as lab editing to keep summaries consistent when
    # multiple frames or detections are reviewed concurrently.
    result = await db.execute(
        select(Media).where(Media.id == media_id, Media.uploader_id == user_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    media = result.scalar_one_or_none()
    if media is None:
        raise HTTPException(status_code=404, detail="Media not found")

    now = datetime.now(timezone.utc)
    if detection_id is None:
        # Clear the whole media item, including every evidence frame of a video.
        for model in (Detection, VideoDetection):
            await db.execute(
                update(model).where(
                    model.media_id == media.id, model.deleted_at.is_(None)
                ).values(deleted_at=now)
            )
    else:
        model = VideoDetection if media.media_type == MediaType.VIDEO else Detection
        result = await db.execute(
            select(model).where(model.id == detection_id, model.media_id == media.id)
        )
        detection = result.scalar_one_or_none()
        if detection is None:
            raise HTTPException(status_code=404, detail="Detection not found")
        detection.deleted_at = now

    if media.media_type == MediaType.VIDEO:
        await _refresh_video_summary(db, media)
    else:
        await db.flush()
        result = await db.execute(
            select(func.count(Detection.id), func.max(Detection.confidence))
            .where(Detection.media_id == media.id)
        )
        count, confidence = result.one()
        _update_media_summary(media, count, confidence or 0.0)

    db.add(create_status_change_log(
        media_id=media.id, status=media.status,
        detail="All detections marked clean by You" if detection_id is None
        else "Detection marked clean by You",
        worker_name="You",
    ))
    await db.commit()
    db.expire(media, ["detections", "video_detections"])
    invalidate_map_stats_cache(str(user_id))
    invalidate_stats_cache(str(user_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{media_id}/detections", status_code=status.HTTP_204_NO_CONTENT)
async def mark_all_clean(
    media_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
) -> Response:
    return await _mark_clean(media_id, None, db, current_user.id)


@router.delete("/{media_id}/detections/{detection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def mark_detection_clean(
    media_id: uuid.UUID,
    detection_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
) -> Response:
    return await _mark_clean(media_id, detection_id, db, current_user.id)


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
    response_model=MediaResponse | VideoDetectionResponse,
    responses={status.HTTP_204_NO_CONTENT: {"description": "Detection deleted"}},
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

    if patch_data.item_type == "image":
        query = (
            select(Media)
            .options(selectinload(Media.detections))
            .where(
                Media.id == media_uuid,
                Media.uploader_id == current_user.id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    else:
        query = (
            select(VideoDetection)
            .options(selectinload(VideoDetection.media))
            .where(
                VideoDetection.id == media_uuid,
                select(Media)
                .where(
                    Media.id == VideoDetection.media_id,
                    Media.uploader_id == current_user.id,
                )
                .exists(),
            )
        )

    result = await db.execute(query)
    media = result.scalar_one_or_none()

    if not media:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Media not found",
        )

    if patch_data.item_type == "video":
        # Serialize edits to sibling detections so their parent summary cannot
        # be overwritten by an aggregate calculated before another edit commits.
        parent_result = await db.execute(
            select(Media)
            .where(
                Media.id == media.media_id,
                Media.uploader_id == current_user.id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        parent_media = parent_result.scalar_one()
        # A concurrent clean action may have completed while awaiting the lock.
        selected_result = await db.execute(
            select(VideoDetection).where(VideoDetection.id == media_uuid)
            .execution_options(populate_existing=True)
        )
        media = selected_result.scalar_one_or_none()
        if media is None:
            raise HTTPException(status_code=404, detail="Media not found")
    else:
        parent_media = media

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

    video_detection_deleted = False
    if patch_data.detections is not None:
        if patch_data.item_type == "image":
            active_ids = {d.id for d in media.detections if d.deleted_at is None}
            if any(d.id is not None and d.id not in active_ids for d in patch_data.detections):
                raise HTTPException(status_code=409, detail="Detections changed. Reload the media and try again.")
            _merge_image_detections(media, patch_data.detections)
            active_detections = [d for d in media.detections if d.deleted_at is None]
            _update_media_summary(
                media,
                len(active_detections),
                max((detection.confidence for detection in active_detections), default=0),
            )
        else:
            video_detection_deleted = await _merge_video_detections(
                db, media, patch_data.detections
            )
            await _refresh_video_summary(db, parent_media)

    validation_log = create_status_change_log(
        media_id=parent_media.id,
        status=parent_media.status,
        detail="Detections or location validated by You",
        worker_name="You",
    )
    db.add(validation_log)
    await db.commit()
    invalidate_map_stats_cache(str(current_user.id))
    invalidate_stats_cache(str(current_user.id))

    if video_detection_deleted:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    await db.refresh(media)

    if patch_data.item_type == "image":
        return _serialize_media(media)
    else:
        return _serialize_video_detection(media)
