"""High-confidence image/video locations, grouped into geographic grid cells."""

from sqlalchemy import func, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db.Detection import Detection
from app.models.db.Media import Media, MediaType
from app.models.db.VideoDetection import VideoDetection
from app.models.stats import HotspotDensity
from app.models.upload.MediaStatus import MediaStatus

HOTSPOT_CONFIDENCE_THRESHOLD = 0.8
HOTSPOT_GRID_RESOLUTION_DEGREES = 0.001


async def get_hotspot_density(db: AsyncSession, user_id: str) -> HotspotDensity:
    # Detection confidences are fractions. Media.confidence is a denormalized
    # percentage that can be stale after Lab edits, so use the detection rows.
    images = select(Media.id.label("media_id"), Media.location.label("location")).where(
        Media.uploader_id == user_id,
        Media.status == MediaStatus.READY,
        Media.media_type == MediaType.IMAGE,
        Media.location.isnot(None),
        Media.detections.any(Detection.confidence >= HOTSPOT_CONFIDENCE_THRESHOLD),
    )
    videos = (
        select(VideoDetection.media_id, VideoDetection.location)
        .join(Media, VideoDetection.media_id == Media.id)
        .where(
            Media.uploader_id == user_id,
            Media.status == MediaStatus.READY,
            Media.media_type == MediaType.VIDEO,
            VideoDetection.confidence >= HOTSPOT_CONFIDENCE_THRESHOLD,
            VideoDetection.location.isnot(None),
        )
    )
    locations = union_all(images, videos).subquery()
    query = select(
        func.count(func.distinct(func.ST_SnapToGrid(
            locations.c.location, HOTSPOT_GRID_RESOLUTION_DEGREES
        ))).label("hotspot_count"),
        func.count(func.distinct(locations.c.media_id)).label("media_count"),
    )
    # PostGIS is required by the schema. Propagate errors rather than inventing
    # a cluster count and leaving the session in an aborted transaction.
    row = (await db.execute(query)).one()
    return HotspotDensity(
        hotspot_count=row.hotspot_count,
        high_confidence_media_count=row.media_count,
    )
