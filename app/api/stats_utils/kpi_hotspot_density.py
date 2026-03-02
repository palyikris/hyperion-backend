"""
KPI 6: Hotspot Discovered Density

Counts the number of geographical "clusters" where trash confidence is >= 80%.
This helps users identify high-priority zones that require immediate attention.
"""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.models.db.Media import Media
from app.models.stats import HotspotDensity


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
    # Query 1: Count media with high confidence (>= 80%) and valid location
    high_confidence_query = (
        select(func.count(Media.id))
        .where(
            Media.uploader_id == user_id,
            Media.confidence >= 80,  # High confidence threshold
            Media.location.isnot(None)  # Must have geolocation
        )
    )
    
    high_confidence_count = await db.execute(high_confidence_query)
    media_count = high_confidence_count.scalar() or 0
    
    # Query 2: Spatial clustering using PostGIS ST_SnapToGrid
    # This groups nearby points into grid cells for hotspot detection
    try:
        from geoalchemy2.functions import ST_SnapToGrid
        
        # Snap all points to a 0.001 degree grid (~100m resolution)
        # Points within the same grid cell will have identical snapped coordinates
        snapped = ST_SnapToGrid(Media.location, 0.001)
        
        # Count distinct grid cells (each represents a unique hotspot area)
        cluster_query = (
            select(func.count(func.distinct(snapped)))
            .where(
                Media.uploader_id == user_id,
                Media.confidence >= 80,
                Media.location.isnot(None)
            )
        )
        
        cluster_result = await db.execute(cluster_query)
        hotspot_count = cluster_result.scalar() or 0
    except Exception:
        # Fallback: If PostGIS fails, use raw media count as hotspot count
        # This is less accurate but ensures the endpoint doesn't break
        hotspot_count = media_count
    
    return HotspotDensity(
        hotspot_count=hotspot_count,
        high_confidence_media_count=media_count
    )
