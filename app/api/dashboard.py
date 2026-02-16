from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.core import security
from app.api.deps import get_current_user
from app.models.dashboard.system_health import SystemHealthResponse
from app.models.dashboard.ux import UXResponse
from app.models.dashboard.ai_workers import AIWorkersResponse
import random
import psutil
from fastapi.responses import JSONResponse
from datetime import datetime, timezone


router = APIRouter()


import time
import psutil
from collections import deque
from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

START_TIME = time.time()
# initialize with some zeros so the chart doesn't look broken on first load
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
            "server_load": list(load_history),  # Convert deque to list for JSON
            "environment": "PROD",
            "status": system_status,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
    )


@router.get(
  "/user-experience",
  status_code=status.HTTP_200_OK,
  response_model=UXResponse,
)
async def user_experience(current_user=Depends(get_current_user)):
  """
  Returns engagement metrics for the dashboard.
  """
  return JSONResponse(
    content={
      "active_now": 1284,
      "active_trend": 12,
      "avg_response_time": 120,
      "daily_activity": [40, 65, 55, 80, 95, 70, 85]
    }
  )

@router.get(
  "/ai-workers",
  status_code=status.HTTP_200_OK,
  response_model=AIWorkersResponse,
)
async def ai_workers(current_user=Depends(get_current_user)):
  """
  Mock endpoint for the upcoming AI Worker fleet implementation.
  """
  return JSONResponse(
    content={
      "total_active_fleet": 12,
      "cluster_status": "Stabilized",
      "nodes": [
          {"name": "Orion", "status": "Active"},
          {"name": "Vega", "status": "Syncing"},
          {"name": "Lyra", "status": "Idle"},
          {"name": "Atlas", "status": "Paused"},
      ]
    }
  )
