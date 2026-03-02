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
    query = (
        select(Detection.label, func.count(Detection.id).label("count"))
        .join(Media, Detection.media_id == Media.id)  # Link detections to media
        .where(Media.uploader_id == user_id)           # User-scoped filter
        .group_by(Detection.label)                     # Aggregate by trash type
        .order_by(func.count(Detection.id).desc())     # Most common first
    )
    
    result = await db.execute(query)
    rows = result.all()
    
    # Calculate total for percentage computation
    total = sum(int(row[1]) for row in rows)
    
    # Build response with percentage calculated from total
    return [
        TrashCompositionItem(
            label=str(row[0]),
            count=int(row[1]),
            percentage=round((int(row[1]) / total * 100) if total > 0 else 0, 2)
        )
        for row in rows
    ]
