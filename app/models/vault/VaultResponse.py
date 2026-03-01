from pydantic import BaseModel
from typing import List, Optional


class VaultItem(BaseModel):
    id: str
    filename: Optional[str] = None
    status: str
    timestamp: str
    image_url: Optional[str] = None
    metadata: Optional[dict] = None
    assigned_worker: Optional[str] = None
    technical_metadata: Optional[dict] = None
    updated_at: Optional[str] = None
    failed_reason: Optional[str] = None


class VaultResponse(BaseModel):
    total: int
    page: int
    page_size: int
    total_pages: int
    items: List[VaultItem]
