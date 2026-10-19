"""Elapsed time from upload to the first successful READY transition."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.stats_utils.completed_media import completed_media
from app.models.db.Media import Media
from app.models.stats import MeanTimeToProcess, ProcessingTime
from app.models.upload.MediaStatus import MediaStatus


async def get_mean_time_to_process(db: AsyncSession, user_id: str) -> MeanTimeToProcess:
    completions = completed_media()
    durations = (
        select(
            Media.id,
            Media.assigned_worker.label("worker_name"),
            func.extract("epoch", completions.c.completed_at - Media.created_at).label("seconds"),
        )
        .join(completions, completions.c.media_id == Media.id)
        .where(
            Media.uploader_id == user_id,
            Media.status == MediaStatus.READY,
            completions.c.completed_at >= Media.created_at,
        )
        .subquery()
    )
    worker_query = (
        select(
            durations.c.worker_name,
            func.avg(durations.c.seconds).label("avg_seconds"),
            func.count().label("task_count"),
        )
        .where(durations.c.worker_name.isnot(None), durations.c.worker_name != "You")
        .group_by(durations.c.worker_name)
        .order_by(durations.c.worker_name)
    )
    rows = (await db.execute(worker_query)).all()
    overall = (await db.execute(select(func.avg(durations.c.seconds)))).scalar() or 0
    return MeanTimeToProcess(
        overall_avg_seconds=round(float(overall), 2),
        by_worker=[ProcessingTime(
            worker_name=row.worker_name,
            avg_processing_seconds=round(float(row.avg_seconds), 2),
            task_count=row.task_count,
        ) for row in rows],
    )
