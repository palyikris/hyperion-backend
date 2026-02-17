from typing import Optional
from pydantic import BaseModel

class NodeInfo(BaseModel):
    status: str
    name: str
    tasks_processed_today: int
    current_task_id: Optional[str] = None

class AIWorkersResponse(BaseModel):
    total_active_fleet: int
    cluster_status: str
    nodes: list[NodeInfo]
    queue_depth: int
    last_updated: Optional[str]
