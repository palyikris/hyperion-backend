from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from app.api.deps import get_current_user
from app.models.dashboard.ai_workers import AIWorkersResponse

import asyncio
import uuid
import time

from datetime import datetime, timezone

# A shared queue for all workers
task_queue = asyncio.Queue()

router = APIRouter()

worker_registry = {
    "Helios": {"last_ping": time.time(), "activity": "Idle"},
    "Eos": {"last_ping": time.time(), "activity": "Idle"},
    "Aethon": {"last_ping": time.time(), "activity": "Idle"},
    "Crius": {"last_ping": time.time(), "activity": "Idle"},
    "Iapetus": {"last_ping": time.time(), "activity": "Idle"},
    "Perses": {"last_ping": time.time(), "activity": "Idle"},
    "Phlegon": {"last_ping": time.time(), "activity": "Idle"},
    "Phoebe": {"last_ping": time.time(), "activity": "Idle"},
    "Theia": {"last_ping": time.time(), "activity": "Idle"},
    "Cronus": {"last_ping": time.time(), "activity": "Idle"},
}


async def ai_worker_process(name: str):
    while True:
        worker_registry[name]["last_ping"] = time.time()

        print(
            f"{name} pinged at {time.strftime('%X')} - Activity: {worker_registry[name]['activity']}"
        )

        try:
            task = await asyncio.wait_for(task_queue.get(), timeout=1.0)

            worker_registry[name]["activity"] = "Working"

            # simulate AI workload
            await asyncio.sleep(25)

            worker_registry[name]["activity"] = "Idle"
            task_queue.task_done()

        except asyncio.TimeoutError:
            # no task
            worker_registry[name]["activity"] = "Idle"
            continue


def get_node_status(node_data: dict):
    last_ping = node_data.get("last_ping", 0)
    activity = node_data.get("activity", "Idle")

    seconds_since_ping = time.time() - last_ping

    if last_ping == 0 or seconds_since_ping > 120:
        return "Offline"

    if activity == "Working":
        return "Working"

    return "Active"


@router.get(
    "/ai-workers",
    status_code=status.HTTP_200_OK,
    response_model=AIWorkersResponse,
)
async def get_worker_status(current_user=Depends(get_current_user)):
    nodes = []
    active_count = 0

    for name, data in worker_registry.items():
        status_label = get_node_status(data)

        if status_label in ["Active", "Working"]:
            active_count += 1

        nodes.append({"name": name, "status": status_label})

    return {
        "total_active_fleet": active_count,
        "cluster_status": (
            "Stressed"
            if active_count >= 8
            else "Optimal" if active_count >= 3 else "Degraded"
        ),
        "nodes": nodes,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/ai-workers/dispatch")
async def dispatch_simulation(
    action: str = "Generic AI Task", current_user=Depends(get_current_user)
):
    """
    Manually injects a task into the fleet's queue.
    """
    task_id = str(uuid.uuid4())[:8]
    task = {"id": task_id, "action": action, "timestamp": time.time()}

    await task_queue.put(task)

    return {
        "message": f"Task {task_id} dispatched to the Titan fleet.",
        "action": action,
        "queue_size": task_queue.qsize(),
    }


async def start_worker_fleet():
    """
    starts the loop
    """
    for name in worker_registry.keys():
        asyncio.create_task(ai_worker_process(name))
