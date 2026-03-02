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
    query = (
        select(
            # COALESCE ensures we get 0 instead of NULL if no detections exist
            func.coalesce(func.sum(Detection.area_sqm), 0).label("total_area"),
            func.count(Detection.id).label("total_detections")
        )
        .join(Media, Detection.media_id == Media.id)
        .where(
            Media.uploader_id == user_id,
            Media.status == MediaStatus.READY  # Only fully processed media
        )
    )
    
    result = await db.execute(query)
    row = result.one()
    
    return EnvironmentalFootprint(
        total_area_sqm=float(row.total_area or 0),
        total_detections=row.total_detections
    )
