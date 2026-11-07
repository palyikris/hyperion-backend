import asyncio
from typing import Dict
from fastapi import WebSocket
from datetime import datetime, timezone, timedelta
from typing import Optional


worker_signal = asyncio.Condition()


class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, set[WebSocket]] = {}
        self.hf_cooldown_until: Optional[datetime] = None

    async def connect(self, user_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.setdefault(user_id, set()).add(websocket)

    def disconnect(self, user_id: str, websocket: WebSocket):
        connections = self.active_connections.get(user_id)
        if connections is not None:
            connections.discard(websocket)
            if not connections:
                self.active_connections.pop(user_id, None)

    async def send_status(
        self,
        user_id: str,
        media_id: str,
        status: str,
        worker: Optional[str] = None,
        img_url: Optional[str] = None,
        address: Optional[str] = None,
        failed_reason: Optional[str] = None,
    ):
        """Sends a JSON packet to a specific user's dashboard."""
        for websocket in tuple(self.active_connections.get(user_id, ())):
            payload = {
                "type": "MEDIA_STATUS_UPDATE",
                "media_id": str(media_id),
                "status": status,
                "worker": worker,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "img_url": img_url,
                "address": address,
                "failed_reason": failed_reason,
            }
            try:
                await websocket.send_json(payload)
            except Exception:
                self.disconnect(user_id, websocket)

    def is_hf_rate_limited(self) -> bool:
        if self.hf_cooldown_until:
            if datetime.now(timezone.utc) < self.hf_cooldown_until:
                return True
            self.hf_cooldown_until = None
        return False

    def set_hf_cooldown(self, hours: int = 1):
        self.hf_cooldown_until = datetime.now(timezone.utc) + timedelta(hours=hours)


manager = ConnectionManager()
