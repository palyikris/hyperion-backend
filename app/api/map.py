from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case, and_, desc, or_, union_all
from sqlalchemy.orm import selectinload
from geoalchemy2.functions import (
    ST_Intersects,
    ST_MakeEnvelope,
    ST_X,
    ST_Y,
    ST_SnapToGrid,
)
from typing import Optional
from time import monotonic

from app.database import get_db
from app.api.deps import get_current_user
from app.models.db.Media import Media, MediaType
from app.models.upload.MediaStatus import MediaStatus
from app.models.db.VideoDetection import VideoDetection
from app.models.db.MediaLog import MediaLog
from app.models.db.Detection import Detection
from app.models.map.MapResponse import MapLogsResponse, MapResponse, MapStatsResponse
from collections import defaultdict


router = APIRouter()

STATS_CACHE_TTL_SECONDS = 60
_map_stats_cache: dict[tuple, tuple[float, dict]] = {}


@router.get(
    "/map",
    status_code=status.HTTP_200_OK,
    response_model=MapResponse,
)
async def get_map_data(
    min_lat: Optional[float] = Query(None),
    max_lat: Optional[float] = Query(None),
    min_lng: Optional[float] = Query(None),
    max_lng: Optional[float] = Query(None),
    has_trash: Optional[bool] = Query(None),
    min_confidence: Optional[float] = Query(None, ge=0, le=100),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    image_query = (
        select(Media)
        .options(selectinload(Media.detections))
        .where(
            Media.uploader_id == current_user.id,
            Media.location.isnot(None),
        )
    )

    video_query = (
        select(VideoDetection)
        .join(Media, VideoDetection.media_id == Media.id)
        .options(selectinload(VideoDetection.media))
        .where(
            Media.uploader_id == current_user.id,
            VideoDetection.location.isnot(None),
        )
    )

    if all(v is not None for v in [min_lat, max_lat, min_lng, max_lng]):
        # Use ST_Intersects with ST_MakeEnvelope for spatial bounding box query
        bbox = ST_MakeEnvelope(min_lng, min_lat, max_lng, max_lat, 4326)
        image_query = image_query.where(ST_Intersects(Media.location, bbox))
        video_query = video_query.where(ST_Intersects(VideoDetection.location, bbox))

    if has_trash is not None:
        if has_trash:
            image_query = image_query.where(
                or_(Media.has_trash.is_(True), Media.detections.any())
            )
        else:
            image_query = image_query.where(
                and_(Media.has_trash.is_(False), ~Media.detections.any())
            )

    if min_confidence is not None and min_confidence > 0:
        image_query = image_query.where(Media.confidence >= min_confidence)
        video_query = video_query.where(VideoDetection.confidence >= min_confidence / 100)

    image_result = await db.execute(image_query)
    image_records = image_result.scalars().all()

    video_result = await db.execute(video_query)
    video_records = video_result.scalars().all()

    def extract_coords(media):
        """Extract lat/lng from location geometry or fall back to legacy columns."""
        if media.lat is not None and media.lng is not None:
            return media.lat, media.lng
        return None, None

    grouped_video_detections = defaultdict(list)
    for v in video_records:
        grouped_video_detections[str(v.media_id)].append(
            {
                "id": str(v.id),
                "media_id": str(v.media_id),
                "filename": v.media.initial_metadata.get("filename"),
                "status": v.media.status.value,
                "worker_name": v.media.assigned_worker,
                "lat": extract_coords(v)[0],
                "lng": extract_coords(v)[1],
                "altitude": v.altitude,
                "address": v.address,
                "image_url": v.frame_hf_path,
                "confidence": v.confidence,
                "label": v.label,
                "bbox": v.bbox,
                "timestamp_in_video": v.timestamp_in_video,
            }
        )
    for detections in grouped_video_detections.values():
        detections.sort(
            key=lambda x: x["timestamp_in_video"] if "timestamp_in_video" in x else 0
        )

    return JSONResponse(
        content={
            "total": len(image_records) + len(video_records),
            "items": [
                {
                    "id": str(m.id),
                    "filename": m.initial_metadata.get("filename"),
                    "status": m.status.value,
                    "worker_name": m.assigned_worker,
                    "lat": extract_coords(m)[0],
                    "lng": extract_coords(m)[1],
                    "altitude": m.altitude,
                    "address": m.address,
                    "image_url": m.hf_path,
                    # Detection rows are authoritative when the cached summary
                    # flag is stale (for example after manual Lab edits).
                    "has_trash": m.has_trash or bool(m.detections),
                    "confidence": m.confidence,
                    "detections": [
                        {
                            "label": d.label,
                            "confidence": d.confidence,
                            "bbox": d.bbox,
                            "area_sqm": d.area_sqm,
                        }
                        for d in m.detections
                    ],
                    "failed_reason": m.failed_reason,
                }
                for m in image_records
            ],
            "video_detections": grouped_video_detections,
        }
    )


@router.get(
    "/map/stats",
    status_code=status.HTTP_200_OK,
    response_model=MapStatsResponse,
)
async def get_map_stats(
    min_lat: float = Query(...),
    max_lat: float = Query(...),
    min_lng: float = Query(...),
    max_lng: float = Query(...),
    resolution: float = Query(0.005, gt=0.0, le=1.0),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if min_lat > max_lat or min_lng > max_lng:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid bounding box: min values must be <= max values.",
        )

    # Cache key is scoped by user + normalized bbox/resolution, so repeated map views
    # within a short interval can reuse the already computed cell stats.
    cache_key = (
        str(current_user.id),
        round(min_lat, 6),
        round(max_lat, 6),
        round(min_lng, 6),
        round(max_lng, 6),
        round(resolution, 6),
    )

    cached = _map_stats_cache.get(cache_key)
    now = monotonic()
    if cached and now - cached[0] <= STATS_CACHE_TTL_SECONDS:
        return cached[1]

    bbox = ST_MakeEnvelope(min_lng, min_lat, max_lng, max_lat, 4326)
    image_filters = and_(
        Media.uploader_id == current_user.id,
        Media.status == MediaStatus.READY,
        Media.media_type == MediaType.IMAGE,
        Media.location.isnot(None),
        or_(Media.has_trash.is_(True), Media.detections.any()),
        ST_Intersects(Media.location, bbox),
    )
    video_filters = and_(
        Media.uploader_id == current_user.id,
        Media.status == MediaStatus.READY,
        Media.media_type == MediaType.VIDEO,
        VideoDetection.location.isnot(None),
        ST_Intersects(VideoDetection.location, bbox),
    )
    # Each image is one map report; each video detection is one located report.
    # A video parent is excluded so its detections are not counted twice.
    image_confidence = select(func.max(Detection.confidence)).where(
        Detection.media_id == Media.id
    ).correlate(Media).scalar_subquery()
    points = union_all(
        select(Media.id.label("report_id"), Media.location.label("location"),
               func.coalesce(image_confidence * 100, Media.confidence).label("confidence"))
        .where(image_filters),
        select(VideoDetection.id, VideoDetection.location, VideoDetection.confidence * 100)
        .join(Media, VideoDetection.media_id == Media.id).where(video_filters),
    ).subquery()
    snapped_point = ST_SnapToGrid(points.c.location, resolution)
    cell_lat_expr = ST_Y(snapped_point).label("cell_lat")
    cell_lng_expr = ST_X(snapped_point).label("cell_lng")
    media_agg_subq = (
        select(cell_lat_expr, cell_lng_expr,
               func.count().label("total_reports"),
               func.avg(func.coalesce(points.c.confidence, 0.0)).label("avg_confidence"))
        .group_by(cell_lat_expr, cell_lng_expr)
        .subquery()
    )
    labels = union_all(
        select(Media.location.label("location"), Detection.label.label("label"))
        .join(Detection, Detection.media_id == Media.id).where(image_filters),
        select(VideoDetection.location, VideoDetection.label)
        .join(Media, VideoDetection.media_id == Media.id).where(video_filters),
    ).subquery()
    label_point = ST_SnapToGrid(labels.c.location, resolution)
    label_lat, label_lng = ST_Y(label_point), ST_X(label_point)
    label_counts_subq = (
        select(label_lat.label("cell_lat"), label_lng.label("cell_lng"),
               labels.c.label, func.count().label("label_count"))
        .group_by(label_lat, label_lng, labels.c.label)
        .subquery()
    )

    # Rank labels per cell by descending count; tie-break alphabetically so the
    # result is deterministic even when counts are equal.
    dominant_labels_ranked_subq = select(
        label_counts_subq.c.cell_lat,
        label_counts_subq.c.cell_lng,
        label_counts_subq.c.label,
        func.row_number()
        .over(
            partition_by=(
                label_counts_subq.c.cell_lat,
                label_counts_subq.c.cell_lng,
            ),
            order_by=(
                desc(label_counts_subq.c.label_count),
                label_counts_subq.c.label.asc(),
            ),
        )
        .label("rn"),
    ).subquery()

    # Keep only the top-ranked (dominant) label for each cell.
    dominant_labels_subq = (
        select(
            dominant_labels_ranked_subq.c.cell_lat,
            dominant_labels_ranked_subq.c.cell_lng,
            dominant_labels_ranked_subq.c.label,
        )
        .where(dominant_labels_ranked_subq.c.rn == 1)
        .subquery()
    )

    # Final result combines cell aggregates with the dominant label (if any).
    # Outer join keeps cells that have media but no detections.
    stats_query = (
        select(
            media_agg_subq.c.cell_lat,
            media_agg_subq.c.cell_lng,
            media_agg_subq.c.total_reports,
            media_agg_subq.c.avg_confidence,
            dominant_labels_subq.c.label,
        )
        .select_from(media_agg_subq)
        .outerjoin(
            dominant_labels_subq,
            and_(
                dominant_labels_subq.c.cell_lat == media_agg_subq.c.cell_lat,
                dominant_labels_subq.c.cell_lng == media_agg_subq.c.cell_lng,
            ),
        )
        .order_by(media_agg_subq.c.cell_lat, media_agg_subq.c.cell_lng)
    )

    result = await db.execute(stats_query)
    rows = result.all()

    # Shape API payload and normalize numeric values for stable frontend rendering.
    # density is report concentration per square-degree cell area:
    # count / (resolution^2). This remains meaningful after excluding non-trash media.
    response_payload = {
        "total": len(rows),
        "items": [
            {
                "lat": float(row.cell_lat),
                "lng": float(row.cell_lng),
                "density": round(
                    float((row.total_reports or 0) / (resolution * resolution)), 2
                ),
                "count": int(row.total_reports or 0),
                "confidence": round(float(row.avg_confidence or 0.0), 2),
                "label": row.label,
            }
            for row in rows
        ],
    }

    # Store in short-lived in-memory cache to reduce repeated aggregate scans.
    _map_stats_cache[cache_key] = (now, response_payload)
    return response_payload


@router.get(
    "/map/{id}/logs",
    status_code=status.HTTP_200_OK,
    response_model=MapLogsResponse,
)
async def get_map_item_logs(
    id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    media_query = select(Media.id).where(
        Media.id == id,
        Media.uploader_id == current_user.id,
    )
    media_result = await db.execute(media_query)
    media_id = media_result.scalar_one_or_none()

    if not media_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Media not found",
        )

    logs_query = (
        select(MediaLog)
        .where(MediaLog.media_id == media_id)
        .order_by(MediaLog.timestamp)
    )
    logs_result = await db.execute(logs_query)
    logs = logs_result.scalars().all()

    return JSONResponse(
        content={
            "media_id": str(media_id),
            "total": len(logs),
            "history": [
                {
                    "action": log.action,
                    "message": log.message,
                    "worker_name": log.worker_name,
                    "timestamp": log.timestamp.isoformat(),
                }
                for log in logs
            ],
        }
    )
