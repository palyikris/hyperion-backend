from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from datetime import datetime


class MapMediaLog(BaseModel):
    action: str
    message: str
    worker_name: Optional[str] = None
    timestamp: datetime


class MapDetection(BaseModel):
    label: str
    confidence: float
    bbox: Dict[str, Any]
    area_sqm: Optional[float] = None


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
    has_trash: Optional[bool] = None
    confidence: Optional[float] = None
    detections: List[MapDetection] = Field(default_factory=list)


class MapResponse(BaseModel):
    items: List[MapItem]
    total: int


class MapLogsResponse(BaseModel):
    media_id: str
    history: List[MapMediaLog]
    total: int


class GridCell(BaseModel):
    lat: float
    lng: float
    density: float
    count: int
    confidence: float
    label: Optional[str] = None


class MapStatsResponse(BaseModel):
    total: int
    items: List[GridCell]
