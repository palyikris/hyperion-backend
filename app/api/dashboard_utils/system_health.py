from collections import deque
from datetime import datetime, timezone
import time

import psutil
from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from app.api.deps import get_current_user
from app.models.dashboard.system_health import SystemHealthResponse

router = APIRouter()

START_TIME = time.time()
# Initialize with zeros so the chart does not look broken on first load.
load_history = deque([0, 0, 0, 0, 0, 0, 0], maxlen=7)


def get_formatted_uptime():
    """Calculates uptime as a percentage of a 24-hour window."""
    uptime_seconds = time.time() - START_TIME
    uptime_percent = min(100.0, (uptime_seconds / 86400) * 100.0)
    return round(uptime_percent, 1)


@router.get(
    "/system-health",
    status_code=status.HTTP_200_OK,
    response_model=SystemHealthResponse,
)
async def get_system_health(current_user=Depends(get_current_user)):
    """
    Returns real-time hardware metrics for the Hyperion environment.
    """
    cpu_load = psutil.cpu_percent(interval=0.1)
    ram_load = psutil.virtual_memory().percent

    combined_pressure = max(cpu_load, ram_load)
    load_history.append(round(combined_pressure))

    if combined_pressure > 95:
        system_status = "STRESSED"
    elif combined_pressure > 80:
        system_status = "HEAVY_LOAD"
    elif combined_pressure < 30:
        system_status = "STABILIZED"
    else:
        system_status = "ACTIVE"

    return JSONResponse(
        content={
            "uptime": get_formatted_uptime(),
            "server_load": list(load_history),
            "environment": "PROD",
            "status": system_status,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
    )
