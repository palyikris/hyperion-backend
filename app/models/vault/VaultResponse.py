from pydantic import BaseModel
from typing import List, Optional, Dict, Any

class VaultItem(BaseModel):
    id: str
    uploader_id: str
    status: str
    hf_path: Optional[str] = None
    initial_metadata: Optional[Dict[str, Any]] = None
    technical_metadata: Optional[Dict[str, Any]] = None
    assigned_worker: Optional[str] = None
    created_at: str
    updated_at: str
    lat: Optional[float] = None
    lng: Optional[float] = None
    altitude: Optional[float] = None
    address: Optional[str] = None
    has_trash: Optional[bool] = None
    confidence: Optional[float] = None
    failed_reason: Optional[str] = None


class VideoDetectionItem(BaseModel):
    id: str
    media_id: str
    lat: Optional[float] = None
    lng: Optional[float] = None
    altitude: Optional[float] = None
    address: Optional[str] = None
    label: str
    confidence: float
    bbox: dict
    timestamp_in_video: float
    frame_hf_path: str
    created_at: str
    area_sqm: Optional[float] = None


class VaultResponse(BaseModel):
    total: int
    page: int
    page_size: int
    total_pages: int
    image_items: List[VaultItem]
    video_items: List[VideoDetectionItem]
