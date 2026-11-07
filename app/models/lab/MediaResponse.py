from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import List, Literal, Optional
from datetime import datetime
from uuid import UUID


class NormalizedBBox(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    x: float = Field(ge=0, le=1, strict=True)
    y: float = Field(ge=0, le=1, strict=True)
    w: float = Field(gt=0, le=1, strict=True)
    h: float = Field(gt=0, le=1, strict=True)

    @model_validator(mode="after")
    def check_image_bounds(self):
        # Normalized coordinates can incur tiny floating-point rounding errors.
        if self.x + self.w > 1 + 1e-9 or self.y + self.h > 1 + 1e-9:
            raise ValueError("Bounding box must stay within the image")
        return self


class DetectionInput(BaseModel):
    """Detection input for PATCH requests"""
    id: Optional[UUID] = None
    label: str = Field(min_length=1)
    bbox: NormalizedBBox
    area_sqm: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)


class DetectionResponse(BaseModel):
    id: str
    label: str
    confidence: float
    bbox: dict
    area_sqm: Optional[float] = None


class MediaResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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


class VideoDetectionResponse(BaseModel):
    id: str

    media_id: str
    lat: Optional[float] = None
    lng: Optional[float] = None
    altitude: Optional[float] = None
    address: Optional[str] = None
    label: str
    confidence: float
    bbox: dict  # {"x", "y", "w", "h"}

    timestamp_in_video: float  # Seconds, including fractional precision

    frame_hf_path: str

    created_at: datetime
    area_sqm: Optional[float] = None


class MediaPatchRequest(BaseModel):
    """Request model for PATCH endpoint"""
    lat: Optional[float] = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    lng: Optional[float] = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    altitude: Optional[float] = Field(default=None, ge=0, le=3000, allow_inf_nan=False)
    address: Optional[str] = None
    detections: Optional[List[DetectionInput]] = None
    item_type: Literal["image", "video"] = "image"
