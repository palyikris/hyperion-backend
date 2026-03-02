"""
KPI 5: Mean Time to Process (MTTP)

Calculates the average duration for a media item to move from initial upload
to READY status. This measures system responsiveness and identifies bottlenecks.
"""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.models.db.Media import Media
from app.models.db.MediaLog import MediaLog
from app.models.upload.MediaStatus import MediaStatus
from app.models.stats import ProcessingTime, MeanTimeToProcess


async def get_mean_time_to_process(
    db: AsyncSession, user_id: str
) -> MeanTimeToProcess:
    """
    Calculates the average duration for a media item to reach READY status.
    
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
