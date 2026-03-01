import asyncio

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case, and_
from datetime import datetime, timezone, timedelta
from time import monotonic
from typing import Optional
from collections import defaultdict

from app.database import get_db
from app.api.deps import get_current_user
from app.models.db.Media import Media
from app.models.db.MediaLog import MediaLog
from app.models.db.Detection import Detection
from app.models.db.AIWorker import AIWorkerState
from app.models.upload.MediaStatus import MediaStatus
from app.models.stats import (
    StatsSummaryResponse,
    TrashCompositionItem,
    TrashCompositionResponse,
    EnvironmentalFootprint,
    WorkerEfficiency,
    AIFleetEfficiency,
    TemporalTrend,
    TemporalTrendsResponse,
    ProcessingTime,
    MeanTimeToProcess,
    HotspotDensity,
)
from app.api.dashboard_utils.utils.init_workers import TITAN_FLEET

router = APIRouter()

# ==============================================================================
# CACHE CONFIGURATION
# ==============================================================================
# In-memory cache with 5-minute TTL to reduce database load for expensive
# aggregation queries. Cache key is (user_id, days) tuple.
# Structure: { (user_id, days): (timestamp_monotonic, response_dict) }
STATS_CACHE_TTL_SECONDS = 300
_stats_cache: dict[tuple, tuple[float, dict]] = {}


# ==============================================================================
# KPI 1: TRASH COMPOSITION BREAKDOWN
# ==============================================================================
async def get_trash_composition(
    db: AsyncSession, user_id: str
) -> list[TrashCompositionItem]:
    """
    KPI 1: Trash Composition Breakdown
    
    Calculates the percentage distribution of identified trash types (e.g., Plastic,
    Metal, Glass) based on detection labels. This helps users understand what types
    of environmental waste are most prevalent in their scanned areas.
    
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


# ==============================================================================
# KPI 2: ENVIRONMENTAL FOOTPRINT
# ==============================================================================
async def get_environmental_footprint(
    db: AsyncSession, user_id: str
) -> EnvironmentalFootprint:
    """
    KPI 2: Total Identified Environmental Footprint
    
    Calculates the cumulative physical area (in square meters) of all detected trash,
    along with a total detection count. This metric quantifies the real-world impact
    of the environmental monitoring effort.
    
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


# ==============================================================================
# KPI 3: AI FLEET EFFICIENCY & SUCCESS RATE
# ==============================================================================
async def get_ai_fleet_efficiency(
    db: AsyncSession, user_id: str
) -> AIFleetEfficiency:
    """
    KPI 3: AI Fleet Efficiency & Success Rate
    
    Calculates the success vs. failure ratio for each AI worker ("Titan") in the fleet.
    This helps identify unreliable workers that may need attention or reconfiguration.
    
    SQL Logic - Two queries:
    
    Query 1 (Media stats per worker):
    - Uses CASE expressions to conditionally count READY (success) vs FAILED media
    - Groups by assigned_worker to get per-worker breakdown
    - Only counts media assigned to a worker (assigned_worker IS NOT NULL)
    
    Query 2 (Worker state):
    - Fetches current AIWorkerState to get tasks_processed_today counter
    - This can be compared against success+failure to verify daily reset logic
    
    Reliability Score Calculation:
        reliability = successes / (successes + failures)
        - Returns 1.0 if no tasks processed (benefit of the doubt)
        - Ranges from 0.0 (all failures) to 1.0 (all successes)
    
    Fleet-wide Metrics:
    - Aggregates all workers to compute overall fleet reliability
    - Includes total successes/failures across all workers
    
    Returns:
        AIFleetEfficiency containing:
        - workers: List of per-worker efficiency metrics
        - fleet_reliability_score: Overall success rate (0-1)
        - total_successes, total_failures: Fleet-wide counts
    """
    # Query 1: Get success/failure counts per worker for this user's media
    # Uses CASE to conditionally count only rows matching each status
    media_query = (
        select(
            Media.assigned_worker,
            # COUNT with CASE: only counts rows where condition is true
            func.count(case((Media.status == MediaStatus.READY, 1))).label("successes"),
            func.count(case((Media.status == MediaStatus.FAILED, 1))).label("failures")
        )
        .where(
            Media.uploader_id == user_id,
            Media.assigned_worker.isnot(None)  # Only media that was assigned to a worker
        )
        .group_by(Media.assigned_worker)
    )
    
    media_result = await db.execute(media_query)
    # Create lookup dict: worker_name -> row with successes/failures
    media_rows = {row.assigned_worker: row for row in media_result.all()}
    
    # Query 2: Get tasks_processed_today from AIWorkerState table
    # This tracks the daily counter which resets at midnight
    worker_query = select(AIWorkerState)
    worker_result = await db.execute(worker_query)
    worker_states = {w.name: w for w in worker_result.scalars().all()}
    
    workers = []
    total_successes = 0
    total_failures = 0
    
    # Iterate through all workers in TITAN_FLEET to ensure complete list
    # (even workers with no tasks for this user are included with zero counts)
    for worker_name in TITAN_FLEET:
        media_data = media_rows.get(worker_name)
        worker_state = worker_states.get(worker_name)
        
        successes = media_data.successes if media_data else 0
        failures = media_data.failures if media_data else 0
        tasks_today = worker_state.tasks_processed_today if worker_state else 0
        
        # Calculate reliability score: successes / total_tasks
        total_tasks = successes + failures
        reliability = successes / total_tasks if total_tasks > 0 else 1.0
        
        workers.append(WorkerEfficiency(
            name=worker_name,
            success_count=successes,
            failure_count=failures,
            tasks_processed_today=tasks_today,
            reliability_score=round(reliability, 4)
        ))
        
        total_successes += successes
        total_failures += failures
    
    # Calculate fleet-wide reliability score
    total_fleet_tasks = total_successes + total_failures
    fleet_reliability = total_successes / total_fleet_tasks if total_fleet_tasks > 0 else 1.0
    
    return AIFleetEfficiency(
        workers=workers,
        fleet_reliability_score=round(fleet_reliability, 4),
        total_successes=total_successes,
        total_failures=total_failures
    )


# ==============================================================================
# KPI 4: TEMPORAL DETECTION TRENDS
# ==============================================================================
async def get_temporal_trends(
    db: AsyncSession, user_id: str, days: int
) -> list[TemporalTrend]:
    """
    KPI 4: Temporal Detection Trends
    
    Tracks the number of trash reports generated per day over a selected time window.
    This helps users visualize trends and identify peak periods of environmental impact.
    
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


# ==============================================================================
# KPI 5: MEAN TIME TO PROCESS (MTTP)
# ==============================================================================
async def get_mean_time_to_process(
    db: AsyncSession, user_id: str
) -> MeanTimeToProcess:
    """
    KPI 5: Mean Time to Process (MTTP)

    Calculates the average duration for a media item to move from initial upload
    to READY status. This measures system responsiveness and identifies bottlenecks.

        SQL Logic (Two-stage approach):

        Stage 1 - Subqueries:
        - Worker subquery: grouped by (media_id, worker_name) for per-worker metrics
        - Overall subquery: grouped by media_id only for fleet-wide average
        - Both only include READY media (successful completions)

        Stage 2 - Aggregation:
        - Per-worker: AVG(end_time - start_time) grouped by worker_name
        - Fleet overall: AVG(end_time - start_time) across one row per media item
        - Uses EXTRACT('epoch', ...) to convert interval to seconds

    Processing Time Calculation:
        latency = MAX(timestamp) - MIN(timestamp) for each media_id
        This captures the full pipeline duration from first to last log entry.

    Identifying Bottlenecks:
        By grouping averages by worker_name, we can identify which workers
        are slower than others, helping prioritize optimization efforts.

    Returns:
        MeanTimeToProcess containing:
        - overall_avg_seconds: System-wide average processing time
        - by_worker: List of per-worker processing times and task counts
    """
    # Stage 1a: Subquery to compute start/end times per media per worker
    # Used for per-worker latency metrics
    worker_subquery = (
        select(
            MediaLog.media_id,
            MediaLog.worker_name,
            func.min(MediaLog.timestamp).label("start_time"),  # First log entry
            func.max(MediaLog.timestamp).label("end_time"),  # Last log entry
        )
        .join(Media, MediaLog.media_id == Media.id)
        .where(
            Media.uploader_id == user_id,
            Media.status == MediaStatus.READY,  # Only successful completions
        )
        .group_by(MediaLog.media_id, MediaLog.worker_name)
    ).subquery()

    # Stage 1b: Subquery to compute full pipeline duration per media item
    # Grouping only by media_id avoids multi-worker double counting in fleet average
    overall_subquery = (
        select(
            MediaLog.media_id,
            func.min(MediaLog.timestamp).label("start_time"),
            func.max(MediaLog.timestamp).label("end_time"),
        )
        .join(Media, MediaLog.media_id == Media.id)
        .where(Media.uploader_id == user_id, Media.status == MediaStatus.READY)
        .group_by(MediaLog.media_id)
    ).subquery()

    # Stage 2a: Calculate average latency grouped by worker
    # EXTRACT('epoch', interval) converts PostgreSQL interval to seconds
    worker_query = (
        select(
            worker_subquery.c.worker_name,
            func.avg(
                func.extract(
                    "epoch", worker_subquery.c.end_time - worker_subquery.c.start_time
                )
            ).label("avg_seconds"),
            func.count(worker_subquery.c.media_id).label("task_count"),
        )
        .where(worker_subquery.c.worker_name.isnot(None))  # Exclude unassigned tasks
        .group_by(worker_subquery.c.worker_name)
    )

    worker_result = await db.execute(worker_query)
    worker_rows = worker_result.all()

    # Build per-worker processing time list
    by_worker = [
        ProcessingTime(
            worker_name=row.worker_name,
            avg_processing_seconds=round(float(row.avg_seconds or 0), 2),
            task_count=row.task_count
        )
        for row in worker_rows
    ]

    # Stage 2b: Calculate fleet-wide average from one duration per media item
    overall_query = select(
        func.avg(
            func.extract(
                "epoch", overall_subquery.c.end_time - overall_subquery.c.start_time
            )
        ).label("overall_avg")
    )

    overall_result = await db.execute(overall_query)
    overall_avg = overall_result.scalar() or 0

    return MeanTimeToProcess(
        overall_avg_seconds=round(float(overall_avg), 2),
        by_worker=by_worker
    )


# ==============================================================================
# KPI 6: HOTSPOT DISCOVERED DENSITY
# ==============================================================================
async def get_hotspot_density(
    db: AsyncSession, user_id: str
) -> HotspotDensity:
    """
    KPI 6: Hotspot Discovered Density
    
    Counts the number of geographical "clusters" where trash confidence is >= 80%.
    This helps users identify high-priority zones that require immediate attention.
    
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
        - Divides the map into ~100m x 100m grid cells
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
        from geoalchemy2.functions import ST_SnapToGrid, ST_X, ST_Y
        
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


# ==============================================================================
# INDIVIDUAL KPI ENDPOINTS
# ==============================================================================

@router.get(
    "/stats/trash-composition",
    status_code=status.HTTP_200_OK,
    response_model=TrashCompositionResponse,
)
async def get_trash_composition_endpoint(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    GET /api/stats/trash-composition - Trash Composition Breakdown
    
    Returns the percentage distribution of identified trash types (e.g., Plastic,
    Metal, Glass) based on detection labels for the authenticated user.
    
    Response:
        {
            "items": [{"label": "plastic", "count": 150, "percentage": 45.5}, ...],
            "total_detections": 330
        }
    """
    items = await get_trash_composition(db, current_user.id)
    total = sum(item.count for item in items)
    
    response = TrashCompositionResponse(items=items, total_detections=total)
    return JSONResponse(content=response.model_dump(mode='json'))


@router.get(
    "/stats/environmental-footprint",
    status_code=status.HTTP_200_OK,
    response_model=EnvironmentalFootprint,
)
async def get_environmental_footprint_endpoint(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    GET /api/stats/environmental-footprint - Environmental Footprint
    
    Returns the cumulative physical area (in square meters) of all detected trash,
    along with a total detection count for the authenticated user.
    
    Response:
        {
            "total_area_sqm": 1234.56,
            "total_detections": 330
        }
    """
    footprint = await get_environmental_footprint(db, current_user.id)
    return JSONResponse(content=footprint.model_dump(mode='json'))


@router.get(
    "/stats/ai-fleet-efficiency",
    status_code=status.HTTP_200_OK,
    response_model=AIFleetEfficiency,
)
async def get_ai_fleet_efficiency_endpoint(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    GET /api/stats/ai-fleet-efficiency - AI Fleet Efficiency & Success Rate
    
    Returns success vs. failure ratio for each AI worker ("Titan") in the fleet
    for the authenticated user's media.
    
    Response:
        {
            "workers": [{"name": "Helios", "success_count": 50, ...}, ...],
            "fleet_reliability_score": 0.95,
            "total_successes": 450,
            "total_failures": 23
        }
    """
    efficiency = await get_ai_fleet_efficiency(db, current_user.id)
    return JSONResponse(content=efficiency.model_dump(mode='json'))


@router.get(
    "/stats/temporal-trends",
    status_code=status.HTTP_200_OK,
    response_model=TemporalTrendsResponse,
)
async def get_temporal_trends_endpoint(
    days: int = Query(default=7, ge=1, le=365, description="Time window in days"),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    GET /api/stats/temporal-trends - Temporal Detection Trends
    
    Returns the number of trash reports generated per day over a selected time window
    for the authenticated user. Includes zero-days for continuous charting.
    
    Query Parameters:
        - days (int, default=7): Number of days to look back (1-365)
    
    Response:
        {
            "trends": [{"date": "2026-02-23", "count": 5}, ...],
            "days_window": 7
        }
    """
    trends = await get_temporal_trends(db, current_user.id, days)
    response = TemporalTrendsResponse(trends=trends, days_window=days)
    return JSONResponse(content=response.model_dump(mode='json'))


@router.get(
    "/stats/mean-time-to-process",
    status_code=status.HTTP_200_OK,
    response_model=MeanTimeToProcess,
)
async def get_mean_time_to_process_endpoint(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    GET /api/stats/mean-time-to-process - Mean Time to Process (MTTP)
    
    Returns the average duration for media items to move from upload to READY status,
    both overall and per worker, for the authenticated user.
    
    Response:
        {
            "overall_avg_seconds": 45.5,
            "by_worker": [{"worker_name": "Helios", "avg_processing_seconds": 42.3, "task_count": 50}, ...]
        }
    """
    mttp = await get_mean_time_to_process(db, current_user.id)
    return JSONResponse(content=mttp.model_dump(mode='json'))


@router.get(
    "/stats/hotspot-density",
    status_code=status.HTTP_200_OK,
    response_model=HotspotDensity,
)
async def get_hotspot_density_endpoint(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    GET /api/stats/hotspot-density - Hotspot Discovered Density
    
    Returns the count of geographical "clusters" where trash confidence is >= 80%
    for the authenticated user.
    
    Response:
        {
            "hotspot_count": 12,
            "high_confidence_media_count": 45
        }
    """
    density = await get_hotspot_density(db, current_user.id)
    return JSONResponse(content=density.model_dump(mode='json'))


# ==============================================================================
# MAIN ENDPOINT: GET /api/stats/summary
# ==============================================================================
@router.get(
    "/stats/summary",
    status_code=status.HTTP_200_OK,
    response_model=StatsSummaryResponse,
)
async def get_stats_summary(
    days: int = Query(default=7, ge=1, le=365, description="Time window in days"),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    GET /api/stats/summary - Comprehensive Statistics Dashboard Endpoint
    
    Aggregates all 6 key performance indicators (KPIs) for the authenticated user
    into a single response. This endpoint powers the statistics dashboard UI.
    
    Authentication:
        Requires valid session cookie (access_token). Uses get_current_user
        dependency to extract and validate the user from the JWT token.
    
    Query Parameters:
        - days (int, default=7): Time window for temporal data (1-365 days)
          Only affects KPI 4 (Temporal Detection Trends). Other KPIs use
          all-time data for the user.
    
    Caching Strategy:
        - Uses in-memory cache with 5-minute (300s) TTL
        - Cache key: (user_id, days) tuple
        - Prevents expensive aggregation queries on repeated requests
        - Uses monotonic() for TTL comparison (immune to system clock changes)
    
    Response Structure (StatsSummaryResponse):
        {
            "trash_composition": [...],      # KPI 1: Trash type distribution
            "environmental_footprint": {...}, # KPI 2: Total area & detections
            "ai_fleet_efficiency": {...},     # KPI 3: Worker success rates
            "temporal_trends": [...],         # KPI 4: Daily trend data
            "mean_time_to_process": {...},    # KPI 5: Processing latency
            "hotspot_density": {...},         # KPI 6: High-priority zones
            "days_window": 7                  # Echo of the days parameter
        }
    
    Error Handling:
        - 401 Unauthorized: Missing or invalid authentication
        - 422 Validation Error: Invalid days parameter
    
    Performance Notes:
        - All queries are user-scoped (no cross-user data leakage)
        - Queries run sequentially (could be parallelized with asyncio.gather)
        - Heavy aggregations benefit from database indexes on:
          - Media.uploader_id
          - Media.status
          - Media.created_at
          - Detection.media_id
          - MediaLog.media_id
    """
    # ------------------------------------------------------------------
    # CACHE CHECK
    # ------------------------------------------------------------------
    # Build cache key from user ID and days parameter
    cache_key = (str(current_user.id), days)
    cached = _stats_cache.get(cache_key)
    now = monotonic()  # Use monotonic time for TTL (not affected by clock changes)
    
    # Return cached response if within TTL
    if cached and now - cached[0] <= STATS_CACHE_TTL_SECONDS:
        return JSONResponse(content=cached[1])
    
    # ------------------------------------------------------------------
    # FETCH ALL KPIs IN PARALLEL
    # ------------------------------------------------------------------
    user_id = current_user.id
    
    # Run all KPI queries concurrently for improved performance
    (
        trash_composition,        # KPI 1: Trash Composition
        environmental_footprint,  # KPI 2: Environmental Footprint
        ai_fleet_efficiency,      # KPI 3: AI Fleet Efficiency
        temporal_trends,          # KPI 4: Temporal Trends
        mean_time_to_process,     # KPI 5: Mean Time to Process
        hotspot_density,          # KPI 6: Hotspot Density
    ) = await asyncio.gather(
        get_trash_composition(db, user_id),
        get_environmental_footprint(db, user_id),
        get_ai_fleet_efficiency(db, user_id),
        get_temporal_trends(db, user_id, days),
        get_mean_time_to_process(db, user_id),
        get_hotspot_density(db, user_id),
    )
    
    # ------------------------------------------------------------------
    # BUILD RESPONSE
    # ------------------------------------------------------------------
    response = StatsSummaryResponse(
        trash_composition=trash_composition,
        environmental_footprint=environmental_footprint,
        ai_fleet_efficiency=ai_fleet_efficiency,
        temporal_trends=temporal_trends,
        mean_time_to_process=mean_time_to_process,
        hotspot_density=hotspot_density,
        days_window=days  # Echo the time window for frontend reference
    )
    
    # ------------------------------------------------------------------
    # UPDATE CACHE
    # ------------------------------------------------------------------
    # Serialize response to dict for caching (Pydantic model -> JSON-compatible dict)
    response_dict = response.model_dump(mode='json')
    _stats_cache[cache_key] = (now, response_dict)
    
    return JSONResponse(content=response_dict)
