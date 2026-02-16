from typing import Optional
from pydantic import BaseModel


class UXResponse(BaseModel):
    active_now: int
    active_trend: list
    avg_response_time: float
    daily_activity: list
    last_updated: str
