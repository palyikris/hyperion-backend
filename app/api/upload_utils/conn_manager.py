import asyncio
from typing import Dict
from fastapi import WebSocket
from datetime import datetime, timezone
from typing import Optional


worker_signal = asyncio.Condition()


class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, user_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[user_id] = websocket

    def disconnect(self, user_id: str):
        self.active_connections.pop(user_id, None)

    async def send_status(
        self, user_id: str, media_id: str, status: str, worker: Optional[str] = None
    ):
        """Sends a JSON packet to a specific user's dashboard."""
        if user_id in self.active_connections:
            websocket = self.active_connections[user_id]
            payload = {
                "type": "MEDIA_STATUS_UPDATE",
                "media_id": str(media_id),
                "status": status,
                "worker": worker,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            try:
                await websocket.send_json(payload)
            except Exception:
                self.disconnect(user_id)


manager = ConnectionManager()
