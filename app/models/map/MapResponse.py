from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime


class MapMediaLog(BaseModel):
    action: str
    message: str
    worker_name: Optional[str] = None
    timestamp: datetime


class MapItem(BaseModel):
    id: str
    filename: Optional[str] = None
    status: str
    worker_name: Optional[str] = None
    lat: float
    lng: float
    altitude: Optional[float] = None
    address: Optional[str] = None
    image_url: Optional[str] = None


class MapResponse(BaseModel):
    items: List[MapItem]
    total: int


class MapLogsResponse(BaseModel):
    media_id: str
    history: List[MapMediaLog]
    total: int
