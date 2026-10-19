"""User-scoped success rates and successful completions today (UTC)."""

from datetime import datetime, timezone

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dashboard_utils.utils.init_workers import TITAN_FLEET
from app.api.stats_utils.completed_media import completed_media
from app.models.db.Media import Media
from app.models.stats import AIFleetEfficiency, WorkerEfficiency
from app.models.upload.MediaStatus import MediaStatus


async def get_ai_fleet_efficiency(db: AsyncSession, user_id: str) -> AIFleetEfficiency:
    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    completions = completed_media()
    query = (
        select(
            Media.assigned_worker,
            func.count(case((Media.status == MediaStatus.READY, 1))).label("successes"),
            func.count(case((Media.status == MediaStatus.FAILED, 1))).label("failures"),
            func.count(case((
                (Media.status == MediaStatus.READY)
                & (completions.c.completed_at >= today)
                & (completions.c.completed_at <= now), 1
            ))).label("tasks_today"),
        )
        .outerjoin(completions, completions.c.media_id == Media.id)
        .where(Media.uploader_id == user_id, Media.assigned_worker.isnot(None))
        .group_by(Media.assigned_worker)
    )
    rows = {row.assigned_worker: row for row in (await db.execute(query)).all()}

    # Keep configured idle workers, and retain historical workers in the totals.
    worker_names = list(TITAN_FLEET) + sorted(set(rows) - set(TITAN_FLEET))
    workers = []
    for name in worker_names:
        row = rows.get(name)
        successes = row.successes if row else 0
        failures = row.failures if row else 0
        total = successes + failures
        workers.append(WorkerEfficiency(
            name=name,
            success_count=successes,
            failure_count=failures,
            tasks_processed_today=row.tasks_today if row else 0,
            reliability_score=round(successes / total, 4) if total else 1.0,
        ))

    successes = sum(worker.success_count for worker in workers)
    failures = sum(worker.failure_count for worker in workers)
    total = successes + failures
    return AIFleetEfficiency(
        workers=workers,
        fleet_reliability_score=round(successes / total, 4) if total else 1.0,
        total_successes=successes,
        total_failures=failures,
    )
