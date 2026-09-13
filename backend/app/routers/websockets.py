"""WebSocket endpoint for real-time events."""
import asyncio
import logging
from typing import Optional
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, status

from app.config import settings
from app.services.ws_broadcaster import ws_broadcaster

logger = logging.getLogger("ruview.websocket_endpoint")
router = APIRouter(tags=["WebSocket"])


@router.websocket("/ws/events")
async def websocket_events_endpoint(
    websocket: WebSocket,
    token: Optional[str] = Query(None),
):
    """Real-time event stream providing fall_detected, node_connected, etc."""
    # Optional token validation
    if settings.API_KEY and token != settings.API_KEY:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await ws_broadcaster.connect(websocket)

    # Keepalive loop
    try:
        while True:
            # Client can send ping, text, or keepalive pong
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        await ws_broadcaster.disconnect(websocket)
    except Exception as e:
        logger.debug(f"WebSocket client loop exited: {e}")
        await ws_broadcaster.disconnect(websocket)
