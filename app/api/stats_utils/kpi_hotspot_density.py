"""
KPI 6: Hotspot Discovered Density

Counts the number of geographical "clusters" where trash confidence is >= 80%.
This helps users identify high-priority zones that require immediate attention.
"""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.models.db.VideoDetection import VideoDetection
from app.models.db.Media import Media
from app.models.stats import HotspotDensity

HOTSPOT_CONFIDENCE_THRESHOLD = 80
HOTSPOT_GRID_RESOLUTION_DEGREES = 0.001


async def get_hotspot_density(
    db: AsyncSession, user_id: str
) -> HotspotDensity:
    """
    Counts the number of geographical "clusters" with high-confidence trash detections.
    
    SQL Logic:
    
    Query 1 (High-confidence count):
    - Simple COUNT of media with confidence >= 80 and valid location
    - Provides raw count for "High-Priority Zones Found" metric
    
    Query 2 (Spatial clustering with PostGIS):
    - Uses ST_SnapToGrid(location, 0.001) to snap points to a grid
    - Grid resolution of 0.001 degrees ≈ ~100m at the equator
    - COUNT(DISTINCT snapped_point) gives number of unique grid cells
    - This approximates the number of distinct "hotspots"
    
    Why 80% confidence threshold:
        High-confidence detections (80%+) represent areas where the AI is
        very certain about trash presence. These are prioritized for cleanup.
    
    Clustering Approach:
        Full PostGIS ST_ClusterWithin would provide true spatial clustering,
        but requires additional setup. This simplified approach using ST_SnapToGrid:
        - Divides the map into ~100m x ~100m grid cells
        - Counts unique cells with high-confidence media
        - Close-by points snap to the same cell, reducing duplicate counts
        - Provides a reasonable approximation without complex clustering
    
    Fallback:
        If PostGIS functions fail (e.g., not installed), falls back to
        using raw media count as the hotspot count.
    
    Returns:
        HotspotDensity containing:
        - hotspot_count: Number of distinct geographic clusters
        - high_confidence_media_count: Total high-confidence media items
    """
    # Query 1: Count high-confidence items in both Media and VideoDetection
    media_query = select(func.count(Media.id)).where(
        Media.uploader_id == user_id,
        Media.confidence >= HOTSPOT_CONFIDENCE_THRESHOLD,
        Media.location.isnot(None),
    )
    video_query = (
        select(func.count(VideoDetection.id))
        .where(
            VideoDetection.confidence >= HOTSPOT_CONFIDENCE_THRESHOLD,
            VideoDetection.location.isnot(None),
            Media.uploader_id == user_id,
        )
        .join(Media, VideoDetection.media_id == Media.id)
    )

    media_count_result, video_count_result = await db.execute(
        media_query
    ), await db.execute(video_query)
    media_count = media_count_result.scalar() or 0
    video_count = video_count_result.scalar() or 0
    total_high_confidence = media_count + video_count

    # Query 2: Spatial clustering using PostGIS ST_SnapToGrid for both tables
    try:
        from geoalchemy2.functions import ST_SnapToGrid
        from sqlalchemy import union_all

        snapped_media = select(
            ST_SnapToGrid(Media.location, HOTSPOT_GRID_RESOLUTION_DEGREES).label(
                "snapped"
            )
        ).where(
            Media.uploader_id == user_id,
            Media.confidence >= HOTSPOT_CONFIDENCE_THRESHOLD,
            Media.location.isnot(None),
        )

        snapped_video = (
            select(
                ST_SnapToGrid(
                    VideoDetection.location, HOTSPOT_GRID_RESOLUTION_DEGREES
                ).label("snapped")
            )
            .where(
                VideoDetection.confidence >= HOTSPOT_CONFIDENCE_THRESHOLD,
                VideoDetection.location.isnot(None),
                Media.uploader_id == user_id,
            )
            .join(Media, VideoDetection.media_id == Media.id)
        )

        unioned = union_all(snapped_media, snapped_video).subquery()
        cluster_query = select(func.count(func.distinct(unioned.c.snapped)))
        cluster_result = await db.execute(cluster_query)
        hotspot_count = cluster_result.scalar() or 0
    except Exception:
        # Fallback: If PostGIS fails, use total high-confidence count as hotspot count
        hotspot_count = total_high_confidence

    return HotspotDensity(
        hotspot_count=hotspot_count, high_confidence_media_count=total_high_confidence
    )
