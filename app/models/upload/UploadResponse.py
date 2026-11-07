from pydantic import BaseModel
from typing import List, Optional

from app.models.vault.VaultResponse import VaultItem, VideoDetectionItem


class UploadResponse(BaseModel):
    message: str
    media_ids: Optional[List[str]] = None


class RecentsResponse(BaseModel):
    total: int
    image_items: List[VaultItem]
    video_items: List[VideoDetectionItem]
