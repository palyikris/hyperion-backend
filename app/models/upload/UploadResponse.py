from pydantic import BaseModel
from typing import List, Optional

class UploadResponse(BaseModel):
    message: str
    media_ids: Optional[List[str]] = None