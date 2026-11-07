from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from app.api.deps import get_current_user
from app.models.dashboard.ux import UXResponse
from app.database import AsyncSessionLocal
from app.core.security import SECRET_KEY, ALGORITHM
from app.models.db.User import User

import time
from datetime import datetime, timezone, timedelta

from collections import deque
from fastapi import Request
from jose import JWTError, jwt
from sqlalchemy import select
from typing import Optional

router = APIRouter()

# In-memory metrics are process-local. Multi-worker deployments should move this
# state to shared storage such as Redis to avoid diverging dashboard numbers.
active_users = {}
response_times = deque(maxlen=50)
active_trend_history = deque([0, 0, 0, 0, 0, 0, 0], maxlen=7)
daily_history = deque([0, 0, 0, 0, 0, 0, 0], maxlen=7)
last_trend_update = 0  # Start at 0 so first update happens immediately
daily_user_sessions = {}  # tracks unique users per day
current_day = datetime.now(timezone.utc).date()

ACTIVE_USER_TTL_SECONDS = 300
TREND_UPDATE_INTERVAL_SECONDS = 60


def update_metrics(*, sample_trend: bool = True):
    """Update trend history and daily metrics."""
    global last_trend_update, current_day, daily_user_sessions
    current_time = time.time()
    today = datetime.now(timezone.utc).date()

    expired = [user_id for user_id, last_seen in active_users.items()
               if current_time - last_seen >= ACTIVE_USER_TTL_SECONDS]
    for user_id in expired:
        del active_users[user_id]

    # Close every elapsed day before counting the first request of a new day.
    while current_day < today:
        daily_history.append(len(daily_user_sessions))
        daily_user_sessions = {}
        current_day += timedelta(days=1)

    # Update active user trend every 60 seconds (more responsive for monitoring)
    if sample_trend and current_time - last_trend_update >= TREND_UPDATE_INTERVAL_SECONDS:
        active_trend_history.append(len(active_users))
        last_trend_update = current_time


def update_active_users(user_id: str):
    global daily_user_sessions
    update_metrics(sample_trend=False)
    active_users[user_id] = time.time()
    daily_user_sessions[user_id] = True  # Track unique users today
    update_metrics()


async def get_user_id_from_request(request: Request) -> Optional[str]:
    token = request.cookies.get("access_token")
    if not token:
        return None

    try:
        payload = jwt.decode(token, SECRET_KEY or "", algorithms=[ALGORITHM])
        email: str = payload.get("sub") or ""
        if not email:
            return None
    except JWTError:
        return None

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User.id).where(User.email == email))
        return result.scalar_one_or_none()


async def track_ux_metrics(request: Request, call_next):
    start_time = time.perf_counter()

    user_id = await get_user_id_from_request(request)
    if user_id:
        update_active_users(user_id)

    response = await call_next(request)

    duration_ms = (time.perf_counter() - start_time) * 1000
    response_times.append(duration_ms)

    return response


@router.get(
    "/user-experience",
    status_code=status.HTTP_200_OK,
    response_model=UXResponse,
)
async def user_experience(current_user=Depends(get_current_user)):
    """
    Returns engagement metrics for the dashboard.
    """
    # Ensure metrics are up to date
    update_metrics()

    return JSONResponse(
        content={
            "active_now": len(active_users),
            "active_trend": list(active_trend_history),
            "avg_response_time": (
                sum(response_times) / len(response_times) if response_times else 0
            ),
            "daily_activity": list(daily_history)[-6:] + [len(daily_user_sessions)],
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
    )
