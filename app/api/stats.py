import asyncio

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.api.deps import get_current_user
from app.models.stats import (
    StatsSummaryResponse,
    TrashCompositionResponse,
    EnvironmentalFootprint,
    AIFleetEfficiency,
    TemporalTrendsResponse,
    MeanTimeToProcess,
    HotspotDensity,
    FunFactsResponse,
)
from app.api.stats_utils import (
    get_cached_stats,
    cache_stats,
    get_trash_composition,
    get_environmental_footprint,
    get_ai_fleet_efficiency,
    get_temporal_trends,
    get_mean_time_to_process,
    get_hotspot_density,
    get_fun_facts,
)

router = APIRouter()


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
        - Queries run in parallel via asyncio.gather for performance
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
    cached_response = get_cached_stats(str(current_user.id), days)
    if cached_response:
        return JSONResponse(content=cached_response)

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
    cache_stats(str(user_id), days, response_dict)

    return JSONResponse(content=response_dict)


@router.get(
    "/stats/fun-facts",
    status_code=status.HTTP_200_OK,
    response_model=FunFactsResponse,
)
async def get_fun_facts_endpoint(
    limit: int = Query(
        5, ge=1, le=5, description="Maximum number of fun facts to return"
    ),
    lang: str = Query("en", regex="^(en|hu)$", description="Language code: en or hu"),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    GET /api/stats/fun-facts - Personalized Fun Facts

    Returns entertaining, personalized statistics about the user's trash reporting
    activity with bilingual support (English and Hungarian).

    Query Parameters:
        - limit (int, 1-5, default=5): Maximum number of facts to return
        - lang (str, default="en"): Language code ("en" or "hu")

    Authentication:
        Requires valid session cookie (access_token)

    Response:
        {
            "facts": [
                {
                    "title": "Titan Affinity",
                    "fact": "Titan Helios is your most active worker, handling 42 tasks!",
                    "icon": "cpu"
                },
                ...
            ]
        }

    Fun Facts Include:
        1. Titan Affinity: Most active AI worker
        2. Cleanup Footprint: Area comparison (smartphone screens)
        3. Trash Specialist: Most frequently detected item
        4. Arctic Explorer: Northernmost detection location
        5. Processing Champion: Success rate statistics
    """
    facts = await get_fun_facts(db, current_user.id, lang, limit)
    return FunFactsResponse(facts=facts)
