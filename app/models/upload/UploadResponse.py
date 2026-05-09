from pydantic import BaseModel
from typing import List, Optional, Dict, Any

class UploadResponse(BaseModel):
    message: str
    media_ids: Optional[List[str]] = None


class RecentsResponse(BaseModel):
    total: int
    image_items: List[Dict[str, Any]]
    video_items: List[Dict[str, Any]]
