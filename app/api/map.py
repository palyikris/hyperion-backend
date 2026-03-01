from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional

from app.database import get_db
from app.api.deps import get_current_user
from app.models.db.Media import Media
from app.models.db.MediaLog import MediaLog
from app.models.map.MapResponse import MapLogsResponse, MapResponse

router = APIRouter()


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
    query = select(Media).where(
        Media.uploader_id == current_user.id,
        Media.lat.isnot(None),
        Media.lng.isnot(None),
    )

    if all(v is not None for v in [min_lat, max_lat, min_lng, max_lng]):
        query = query.where(
            Media.lat.between(min_lat, max_lat), Media.lng.between(min_lng, max_lng)
        )

    if has_trash is not None:
        query = query.where(
            Media.technical_metadata["has_trash"].as_boolean() == has_trash
        )

    if min_confidence is not None and min_confidence > 0:
        query = query.where(
            Media.technical_metadata["confidence"].as_float() >= min_confidence
        )

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
                }
                for m in records
            ],
        }
    )


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
