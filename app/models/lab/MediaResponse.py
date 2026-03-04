from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime


class DetectionInput(BaseModel):
    """Detection input for PATCH requests"""
    label: str
    bbox: dict
    area_sqm: Optional[float] = None


class DetectionResponse(BaseModel):
    id: str
    label: str
    confidence: float
    bbox: dict
    area_sqm: Optional[float] = None


class MediaResponse(BaseModel):
    id: str
    uploader_id: str
    status: str
    hf_path: Optional[str] = None
    initial_metadata: Optional[dict] = None
    technical_metadata: Optional[dict] = None
    assigned_worker: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    lat: Optional[float] = None
    lng: Optional[float] = None
    altitude: Optional[float] = None
    address: Optional[str] = None
    has_trash: bool
    confidence: float
    failed_reason: Optional[str] = None
    original_media_id: Optional[str] = None
    detections: List[DetectionResponse]

    class Config:
        from_attributes = True


class MediaPatchRequest(BaseModel):
    """Request model for PATCH endpoint"""
    lat: Optional[float] = None
    lng: Optional[float] = None
    altitude: Optional[float] = None
    address: Optional[str] = None
    detections: Optional[List[DetectionInput]] = None
