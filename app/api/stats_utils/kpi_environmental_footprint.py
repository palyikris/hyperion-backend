"""
KPI 2: Environmental Footprint

Calculates the cumulative physical area (in square meters) of all detected trash,
along with a total detection count. This metric quantifies the real-world impact
of the environmental monitoring effort.
"""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.models.db.Media import Media
from app.models.db.Detection import Detection
from app.models.db.VideoDetection import VideoDetection
from app.models.upload.MediaStatus import MediaStatus
from app.models.stats import EnvironmentalFootprint


async def get_environmental_footprint(
    db: AsyncSession, user_id: str
) -> EnvironmentalFootprint:
    """
    Calculates the cumulative total area of detected trash and detection count.
    
    SQL Logic:
    - SUM(Detection.area_sqm) to get total contaminated area
    - COUNT(Detection.id) for total number of individual detections
    - Only includes READY (successfully processed) media to ensure data quality
    - COALESCE handles NULL values from empty result sets
    
    Why filter by READY status:
        We only count detections from fully processed media to avoid counting
        partial or failed processing attempts that may have incorrect data.
    
    Returns:
        EnvironmentalFootprint with total_area_sqm and total_detections
    """
    # Query for Detection
    detection_query = (
        select(
            func.coalesce(func.sum(Detection.area_sqm), 0).label("total_area"),
            func.count(Detection.id).label("total_detections"),
        )
        .join(Media, Detection.media_id == Media.id)
        .where(Media.uploader_id == user_id, Media.status == MediaStatus.READY)
    )

    # Query for VideoDetection
    video_detection_query = (
        select(
            func.coalesce(func.sum(VideoDetection.area_sqm), 0).label("total_area"),
            func.count(VideoDetection.id).label("total_detections"),
        )
        .join(Media, VideoDetection.media_id == Media.id)
        .where(Media.uploader_id == user_id, Media.status == MediaStatus.READY)
    )

    detection_result = await db.execute(detection_query)
    detection_row = detection_result.one()

    video_detection_result = await db.execute(video_detection_query)
    video_detection_row = video_detection_result.one()

    total_area = float(detection_row.total_area or 0) + float(
        video_detection_row.total_area or 0
    )
    total_detections = (detection_row.total_detections or 0) + (
        video_detection_row.total_detections or 0
    )

    return EnvironmentalFootprint(
        total_area_sqm=total_area, total_detections=total_detections
    )
