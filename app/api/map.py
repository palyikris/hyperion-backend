from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case, and_, desc
from sqlalchemy.orm import selectinload
from typing import Optional
from time import monotonic

from app.database import get_db
from app.api.deps import get_current_user
from app.models.db.Media import Media
from app.models.db.MediaLog import MediaLog
from app.models.db.Detection import Detection
from app.models.map.MapResponse import MapLogsResponse, MapResponse, MapStatsResponse

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
    query = (
        select(Media)
        .options(selectinload(Media.detections))
        .where(
            Media.uploader_id == current_user.id,
            Media.lat.isnot(None),
            Media.lng.isnot(None),
        )
    )

    if all(v is not None for v in [min_lat, max_lat, min_lng, max_lng]):
        query = query.where(
            Media.lat.between(min_lat, max_lat), Media.lng.between(min_lng, max_lng)
        )

    if has_trash is not None:
        query = query.where(Media.has_trash == has_trash)

    if min_confidence is not None and min_confidence > 0:
        query = query.where(Media.confidence >= min_confidence)

    result = await db.execute(query)
    records = result.scalars().all()

    return JSONResponse(
        content={
            "total": len(records),
            "items": [
                {
                    "id": str(m.id),
                    "filename": m.initial_metadata.get("filename"),
                    "status": m.status.value,
                    "worker_name": m.assigned_worker,
                    "lat": m.lat,
                    "lng": m.lng,
                    "altitude": m.altitude,
                    "address": m.address,
                    "image_url": m.hf_path,
                    "has_trash": m.has_trash,
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
                }
                for m in records
            ],
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

    # Bucket each media point into a grid cell by flooring lat/lng to the given
    # resolution. Example: resolution 0.005 groups nearby points together.
    cell_lat_expr = (func.floor(Media.lat / resolution) * resolution).label("cell_lat")
    cell_lng_expr = (func.floor(Media.lng / resolution) * resolution).label("cell_lng")
    confidence_expr = Media.confidence

    # Base filters shared across all map stats queries so every metric uses the
    # same uploader + bounded geographic window.
    media_filters = and_(
        Media.uploader_id == current_user.id,
        Media.lat.isnot(None),
        Media.lng.isnot(None),
        Media.has_trash.is_(True),
        Media.lat.between(min_lat, max_lat),
        Media.lng.between(min_lng, max_lng),
    )

    # Per-cell aggregate over Media:
    # - total_reports: number of media points in the cell
    # - avg_confidence: mean confidence value (missing confidence treated as 0)
    media_agg_subq = (
        select(
            cell_lat_expr,
            cell_lng_expr,
            func.count(Media.id).label("total_reports"),
            func.avg(func.coalesce(confidence_expr, 0.0)).label("avg_confidence"),
        )
        .where(media_filters)
        .group_by(cell_lat_expr, cell_lng_expr)
        .subquery()
    )

    # Count detections by label for each cell. This is used to determine a single
    # dominant label per cell.
    label_counts_subq = (
        select(
            cell_lat_expr,
            cell_lng_expr,
            Detection.label.label("label"),
            func.count(Detection.id).label("label_count"),
        )
        .select_from(Media)
        .join(Detection, Detection.media_id == Media.id)
        .where(media_filters)
        .group_by(cell_lat_expr, cell_lng_expr, Detection.label)
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
