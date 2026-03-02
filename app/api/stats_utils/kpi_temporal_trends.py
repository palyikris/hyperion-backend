"""
KPI 4: Temporal Detection Trends

Tracks the number of trash reports generated per day over a selected time window.
This helps users visualize trends and identify peak periods of environmental impact.
"""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from datetime import datetime, timezone, timedelta

from app.models.db.Media import Media
from app.models.stats import TemporalTrend


async def get_temporal_trends(
    db: AsyncSession, user_id: str, days: int
) -> list[TemporalTrend]:
    """
    Tracks the number of trash reports generated per day over a time window.
    
    SQL Logic:
    - Uses DATE_TRUNC('day', created_at) to bucket timestamps into daily intervals
    - Filters for has_trash == True to focus on environmental impact, not just uploads
    - Applies cutoff_date filter based on the 'days' parameter
    - Groups and orders by day for chronological output
    
    Zero-Day Filling:
        The database query only returns days with actual data. To ensure a continuous
        line chart without gaps, we programmatically fill in "zero-days" (days with
        no reports) in the response. This is done by:
        1. Converting query results to a date->count dictionary
        2. Iterating through each day in the window
        3. Using dict.get(date, 0) to default missing days to 0
    
    Args:
        days: Number of days to look back (e.g., 7 for weekly, 30 for monthly)
    
    Returns:
        List of TemporalTrend objects, one per day in the window (including zero-days)
    """
    # Calculate the cutoff date (start of our time window)
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)

    day_bucket = func.date_trunc("day", Media.created_at)

    query = (
        select(
            # Truncate timestamp to day precision for daily bucketing
            day_bucket.label("day"),
            func.count(Media.id).label("count"),
        )
        .where(
            Media.uploader_id == user_id,
            Media.has_trash == True,  # Only count media containing detected trash
            Media.created_at >= cutoff_date,
        )
        .group_by(day_bucket)
        .order_by(day_bucket)
    )

    result = await db.execute(query)
    rows = result.all()

    # Create a lookup dict: date -> count for O(1) access
    data_by_date = {row[0].date(): int(row[1]) for row in rows}

    # Fill in zero-days for continuous chart rendering
    # Start from (days-1) ago to include 'days' total days ending today
    trends = []
    current_date = (datetime.now(timezone.utc) - timedelta(days=days - 1)).date()
    end_date = datetime.now(timezone.utc).date()

    while current_date <= end_date:
        count_val = data_by_date.get(current_date, 0)  # Default to 0 if no data
        trends.append(TemporalTrend(
            date=current_date,
            count=int(count_val)
        ))
        current_date += timedelta(days=1)

    return trends
