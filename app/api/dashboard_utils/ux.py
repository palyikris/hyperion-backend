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
daily_history = [0, 0, 0, 0, 0, 0, 0]


def update_active_users(user_id: str):
    active_users[user_id] = time.time()
    # cleanup users who havent made a request in 5 minutes
    current_time = time.time()
    expired = [
        ip for ip, last_seen in active_users.items() if current_time - last_seen > 300
    ]
    for ip in expired:
        del active_users[ip]


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
