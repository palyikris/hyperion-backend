from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from app.api.deps import get_current_user
from app.models.dashboard.ux import UXResponse
from app.database import AsyncSessionLocal
from app.core.security import SECRET_KEY, ALGORITHM
from app.models.db.User import User

import time
from datetime import datetime, timezone

from collections import deque
from fastapi import Request
from jose import JWTError, jwt
from sqlalchemy import select
from typing import Optional

router = APIRouter()

# --- UX Global State ---
active_users = {}
response_times = deque(maxlen=50)
active_trend_history = deque([0, 0, 0, 0, 0, 0, 0], maxlen=7)
daily_history = deque([0, 0, 0, 0, 0, 0, 0], maxlen=7)
last_trend_update = 0  # Start at 0 so first update happens immediately
daily_user_sessions = {}  # tracks unique users per day
current_day = datetime.now(timezone.utc).date()


def update_metrics():
    """Update trend history and daily metrics."""
    global last_trend_update, current_day, daily_user_sessions
    current_time = time.time()
    today = datetime.now(timezone.utc).date()

    # Update active user trend every 60 seconds (more responsive for monitoring)
    if current_time - last_trend_update > 60:
        active_trend_history.append(len(active_users))
        last_trend_update = current_time

    # Reset daily counter if day changed
    if today != current_day:
        daily_history.append(len(daily_user_sessions))
        daily_user_sessions = {}
        current_day = today


def update_active_users(user_id: str):
    global daily_user_sessions
    active_users[user_id] = time.time()
    daily_user_sessions[user_id] = True  # Track unique users today

    # cleanup users who havent made a request in 5 minutes
    current_time = time.time()
    expired = [
        ip for ip, last_seen in active_users.items() if current_time - last_seen > 300
    ]
    for ip in expired:
        del active_users[ip]

    # Update metrics
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
    start_time = time.time()

    user_id = await get_user_id_from_request(request)
    if user_id:
        update_active_users(user_id)

    response = await call_next(request)

    duration_ms = (time.time() - start_time) * 1000
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
            "daily_activity": list(daily_history),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
    )
