from typing import Optional
from pydantic import BaseModel

class SystemHealthResponse(BaseModel):
    status: str
    environment: str
    uptime: Optional[float] = None
    uptime_formatted: Optional[str] = None
    server_load: Optional[list] = None  
