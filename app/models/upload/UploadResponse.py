from pydantic import BaseModel
from typing import List, Optional

class UploadResponse(BaseModel):
    message: str
    media_ids: Optional[List[str]] = None


class RecentMediaItem(BaseModel):
    id: str
    filename: Optional[str] = None
    status: str
    timestamp: str
    image_url: Optional[str] = None
    metadata: Optional[dict] = None
    failed_reason: Optional[str] = None


class RecentsResponse(BaseModel):
    total: int
    items: List[RecentMediaItem]
