"""
KPI 1: Trash Composition Breakdown

Calculates the percentage distribution of identified trash types (e.g., Plastic,
Metal, Glass) based on detection labels. This helps users understand what types
of environmental waste are most prevalent in their scanned areas.
"""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.models.db.Media import Media
from app.models.db.Detection import Detection
from app.models.db.VideoDetection import VideoDetection
from app.models.stats import TrashCompositionItem


async def get_trash_composition(
    db: AsyncSession, user_id: str
) -> list[TrashCompositionItem]:
    """
    Calculates the percentage distribution of identified trash types.
    
    SQL Logic:
    - JOIN Detection -> Media to link detections to their parent media
    - Filter by current user's uploads (Media.uploader_id = user_id)
    - GROUP BY detection label to aggregate counts per trash type
    - ORDER BY count DESC to show most common types first
    
    Returns:
        List of TrashCompositionItem with label, count, and percentage (0-100)
    
    Example Output:
        [{"label": "plastic", "count": 150, "percentage": 45.5},
         {"label": "metal", "count": 100, "percentage": 30.3}, ...]
    """
    # Query Detection counts
    detection_query = (
        select(Detection.label, func.count(Detection.id).label("count"))
        .join(Media, Detection.media_id == Media.id)
        .where(Media.uploader_id == user_id)
        .group_by(Detection.label)
    )
    detection_result = await db.execute(detection_query)
    detection_rows = detection_result.all()

    # Query VideoDetection counts
    video_query = (
        select(VideoDetection.label, func.count(VideoDetection.id).label("count"))
        .join(Media, VideoDetection.media_id == Media.id)
        .where(Media.uploader_id == user_id)
        .group_by(VideoDetection.label)
    )
    video_result = await db.execute(video_query)
    video_rows = video_result.all()

    # Combine counts per label
    from collections import defaultdict

    label_counts = defaultdict(int)
    for row in detection_rows:
        label_counts[str(row[0])] += int(row[1])
    for row in video_rows:
        label_counts[str(row[0])] += int(row[1])

    # Sort by count descending
    sorted_items = sorted(label_counts.items(), key=lambda x: x[1], reverse=True)
    total = sum(count for _, count in sorted_items)

    return [
        TrashCompositionItem(
            label=label,
            count=count,
            percentage=round((count / total * 100) if total > 0 else 0, 2),
        )
        for label, count in sorted_items
    ]
