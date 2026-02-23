from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from app.api.deps import get_current_user
from app.models.dashboard.ai_workers import AIWorkersResponse

import asyncio
import uuid
import time

from datetime import datetime, timezone, date

router = APIRouter()

from sqlalchemy import select
from app.database import get_db  #
from app.models.db.AIWorker import AIWorkerState
from app.models.db.Media import Media
from app.models.upload.MediaStatus import MediaStatus
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.db.Media import Media
from app.api.dashboard_utils.utils.get_queue_depth import get_queue_depth


@router.get(
    "/ai-workers",
    status_code=status.HTTP_200_OK,
    response_model=AIWorkersResponse,  #
)
async def get_worker_status(
    db: AsyncSession = Depends(get_db), current_user=Depends(get_current_user)  #  #
):
    result = await db.execute(select(AIWorkerState))
    worker_records = result.scalars().all()

    nodes = []
    active_count = 0
    working_count = 0

    for worker in worker_records:
        last_ping = worker.last_ping
        if last_ping is None:
            is_online = False
        else:
            if last_ping.tzinfo is None:
                last_ping = last_ping.replace(tzinfo=timezone.utc)
            is_online = (datetime.now(timezone.utc) - last_ping).total_seconds() < 120
        status_label = worker.status if is_online else "Offline"

        if status_label in ["Active", "Working"]:
            active_count += 1
        if status_label == "Working":
            working_count += 1

        worker_tasks = await db.execute(
            select(Media).where(
                Media.assigned_worker == worker.name,
                Media.status.in_([MediaStatus.EXTRACTING, MediaStatus.PROCESSING]),
            )
        )
        current_task = worker_tasks.scalars().first()

        nodes.append(
            {
                "name": worker.name,
                "status": status_label,
                "tasks_processed_today": worker.tasks_processed_today,
                "current_task_id": str(current_task.id) if current_task else None,
                "current_task_status": (
                    current_task.status.value if current_task else None
                ),
            }
        )

    return {
        "total_active_fleet": active_count,
        "cluster_status": (
            "Stressed"
            if working_count >= 8
            else "Optimal" if active_count >= 3 else "Degraded"
        ),
        "nodes": nodes,
        "queue_depth": await get_queue_depth(db),  #
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
