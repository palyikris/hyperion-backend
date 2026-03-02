"""
KPI 3: AI Fleet Efficiency & Success Rate

Calculates the success vs. failure ratio for each AI worker ("Titan") in the fleet.
This helps identify unreliable workers that may need attention or reconfiguration.
"""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case

from app.models.db.Media import Media
from app.models.db.AIWorker import AIWorkerState
from app.models.upload.MediaStatus import MediaStatus
from app.models.stats import WorkerEfficiency, AIFleetEfficiency
from app.api.dashboard_utils.utils.init_workers import TITAN_FLEET


async def get_ai_fleet_efficiency(
    db: AsyncSession, user_id: str
) -> AIFleetEfficiency:
    """
    Calculates the success vs. failure ratio for each AI worker in the fleet.
    
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
