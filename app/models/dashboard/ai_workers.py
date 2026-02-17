from typing import Optional
from pydantic import BaseModel

class NodeInfo(BaseModel):
    status: str
    name: str

class AIWorkersResponse(BaseModel):
    total_active_fleet: int
    cluster_status: str
    nodes: list[NodeInfo]
