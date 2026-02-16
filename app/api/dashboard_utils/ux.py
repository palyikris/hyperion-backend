from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from app.api.deps import get_current_user
from app.models.dashboard.ux import UXResponse

import time
from collections import deque
from fastapi import Request

router = APIRouter()

# --- UX Global State ---
active_users = {}
response_times = deque(maxlen=50)
active_trend_history = deque([0, 0, 0, 0, 0, 0, 0], maxlen=7)
daily_history = [0, 0, 0, 0, 0, 0, 0]


def update_active_users(ip: str):
    active_users[ip] = time.time()
    # cleanup users who havent made a request in 5 minutes
    current_time = time.time()
    expired = [
        ip for ip, last_seen in active_users.items() if current_time - last_seen > 300
    ]
    for ip in expired:
        del active_users[ip]


def get_client_ip(request: Request):
    x_forwarded_for = request.headers.get("X-Forwarded-For")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0]
    if request.client:
        return request.client.host
    return "unknown"


async def track_ux_metrics(request: Request, call_next):
    start_time = time.time()

    client_ip = get_client_ip(request)
    update_active_users(client_ip)

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
            "avg_response_time": sum(response_times) / len(response_times) if response_times else 0,
            "daily_activity": list(daily_history),
        }
    )
