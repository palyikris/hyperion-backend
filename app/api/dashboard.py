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


router = APIRouter()


import time
import psutil
from collections import deque
from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
import os

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
    process = psutil.Process(os.getpid())

    cpu_load = process.cpu_percent(interval=0.1)

    mem_info = process.memory_info()
    mem_mb = mem_info.rss / (1024 * 1024)
    limit_mb = 16384  # Adjust based on your HF plan (16GB is standard)
    ram_usage_pct = (mem_mb / limit_mb) * 100

    current_pressure = max(cpu_load, ram_usage_pct)
    load_history.append(round(current_pressure))

    # 4. Update the history with the HIGHER of the two for the 'Pressure' sparkline
    system_status = "STABILIZED"
    if current_pressure > 80:
        system_status = "HEAVY_LOAD"
    elif current_pressure > 30:
        system_status = "ACTIVE"

    return JSONResponse(
        content={
            "uptime": get_formatted_uptime(),
            "server_load": list(load_history),  # Convert deque to list for JSON
            "environment": "PROD",
            "status": system_status,
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
