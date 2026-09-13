"""WebSocket connection manager for real-time event broadcasting."""
import asyncio
from datetime import datetime, timezone
import json
import logging
from typing import Set, Dict, Any
from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger("ruview.ws_broadcaster")


class WebSocketBroadcaster:
    def __init__(self):
        self._active_connections: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        async with self._lock:
            self._active_connections.add(websocket)
        logger.info(f"WebSocket client connected. Total clients: {len(self._active_connections)}")

    async def disconnect(self, websocket: WebSocket):
        async with self._lock:
            self._active_connections.discard(websocket)
        logger.info(f"WebSocket client disconnected. Total clients: {len(self._active_connections)}")

    async def broadcast(self, event_type: str, data: Dict[str, Any]):
        """Broadcasts structured event to all active WebSocket clients."""
        payload = {
            "type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **data,
        }
        text_message = json.dumps(payload)

        async with self._lock:
            dead_connections = set()
            for ws in list(self._active_connections):
                try:
                    await ws.send_text(text_message)
                except Exception as e:
                    logger.debug(f"Failed to send to client ({e}), queueing removal")
                    dead_connections.add(ws)

            for ws in dead_connections:
                self._active_connections.discard(ws)

    @property
    def client_count(self) -> int:
        return len(self._active_connections)


ws_broadcaster = WebSocketBroadcaster()
