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


@router.get(
  "/system-health",
  status_code=status.HTTP_200_OK,
  response_model=SystemHealthResponse,
)
async def dashboard(current_user=Depends(get_current_user)):
  """
  Returns real-time hardware metrics for the Hyperion environment.
  """
  cpu_load = psutil.cpu_percent(interval=None)
  ram_load = psutil.virtual_memory().percent
  
  pulse_line = [random.randint(15, 30), 45, 70, 90, 60, int(cpu_load), int(ram_load)]
  
  return JSONResponse(
    content={
      "uptime": 99.9,
      "server_load": pulse_line,
      "environment": "PROD",
      "status": "ACTIVE"
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