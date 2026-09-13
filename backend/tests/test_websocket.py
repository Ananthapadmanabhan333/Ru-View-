"""Tests for WebSocket event stream."""
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.services.ws_broadcaster import ws_broadcaster


def test_websocket_connection_and_ping_pong():
    client = TestClient(app)
    with client.websocket_connect("/ws/events") as websocket:
        websocket.send_text("ping")
        data = websocket.receive_text()
        assert data == "pong"


@pytest.mark.asyncio
async def test_websocket_broadcast():
    client = TestClient(app)
    with client.websocket_connect("/ws/events") as websocket:
        # Broadcast test message
        await ws_broadcaster.broadcast(
            "fall_detected",
            {
                "node_id": "esp-test",
                "room_id": "bedroom",
                "confidence": None,
                "status": "detected",
            },
        )
        msg = websocket.receive_json()
        assert msg["type"] == "fall_detected"
        assert msg["node_id"] == "esp-test"
        assert msg["room_id"] == "bedroom"
        assert "timestamp" in msg
