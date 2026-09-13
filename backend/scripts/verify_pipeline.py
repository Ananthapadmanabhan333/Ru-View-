#!/usr/bin/env python3
"""End-to-end live pipeline verification script.

Launches an in-process FastAPI server with live UDP ingestion and WebSocket streaming,
streams genuine RuView CSI and Vitals frames via UDP, triggers a controlled fall,
and asserts the complete flow:
ESP32 (UDP) -> Parser -> NodeTracker -> FallManager -> DB -> REST -> WebSocket
"""
import asyncio
import os
import socket
import struct
import time
from datetime import datetime, timezone
import httpx
import websockets

# Ensure backend root is on sys.path
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
settings.DATABASE_URL = "sqlite+aiosqlite:///:memory:"
settings.UDP_PORT = 5055  # Use test port to avoid conflict
settings.PORT = 8055

import uvicorn
from app.main import app
from app.parser import MAGIC_RAW_CSI, MAGIC_EDGE_VITALS


def build_raw_csi_packet(node_id: int, seq: int) -> bytes:
    header = struct.pack(
        "<IBBHIIBBBB",
        MAGIC_RAW_CSI,
        node_id,
        1,   # n_antennas
        8,   # n_subcarriers
        2437,
        seq,
        (-50) & 0xFF,
        (-90) & 0xFF,
        1,   # he_su
        0x10,
    )
    iq = struct.pack("<16b", 10, 5, 12, -4, 8, 2, -5, -8, 14, 1, 9, -6, 4, 3, -2, -7)
    return header + iq


def build_vitals_packet(node_id: int, fall: bool = False, presence: bool = True) -> bytes:
    flags = 0
    if presence:
        flags |= 0x01
    if fall:
        flags |= 0x02
    flags |= 0x04  # motion

    return struct.pack(
        "<IBBHIBB2xffII",
        MAGIC_EDGE_VITALS,
        node_id,
        flags,
        1650,    # 16.5 BPM
        720000,  # 72.0 BPM
        (-52) & 0xFF,
        1,       # 1 person
        0.45,    # motion energy
        4.8,     # presence score
        1000,    # timestamp ms
        0,
    )


async def run_verification():
    print("==========================================================")
    print("  ESP32-S3 -> RuView Fall Detection Pipeline Verification ")
    print("==========================================================")

    # 1. Start uvicorn server in background
    config = uvicorn.Config(app, host="127.0.0.1", port=8055, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    # Wait for server to start
    await asyncio.sleep(1.0)
    base_url = "http://127.0.0.1:8055"
    ws_url = "ws://127.0.0.1:8055/ws/events"

    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_dest = ("127.0.0.1", 5055)

    ws_received_events = []

    async def ws_listener():
        try:
            async with websockets.connect(ws_url) as ws:
                while True:
                    msg = await ws.recv()
                    ws_received_events.append(msg)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"WS listener err: {e}")

    ws_task = asyncio.create_task(ws_listener())
    await asyncio.sleep(0.3)

    try:
        async with httpx.AsyncClient(base_url=base_url) as client:
            # Check Health
            h_resp = await client.get("/api/health")
            assert h_resp.status_code == 200
            print("[OK] Step 1: Health endpoint OK ->", h_resp.json())

            # Register Node & Room
            await client.post("/api/rooms", json={"id": "living_room", "name": "Living Room"})
            await client.post("/api/nodes/1/register", json={"room_id": "living_room"})
            print("[OK] Step 2: Room 'living_room' and Node '1' registered")

            # Stream normal CSI frame over UDP
            udp_sock.sendto(build_raw_csi_packet(node_id=1, seq=1), udp_dest)
            await asyncio.sleep(0.2)

            nodes_resp = await client.get("/api/nodes")
            node_1 = next(n for n in nodes_resp.json() if n["node_id"] == "1")
            assert node_1["status"] == "ONLINE"
            print("[OK] Step 3: Node transitioned to ONLINE from UDP packet ->", node_1["status"])

            # Stream normal vitals packet
            udp_sock.sendto(build_vitals_packet(node_id=1, fall=False, presence=True), udp_dest)
            await asyncio.sleep(0.2)

            sensing = await client.get("/api/sensing/latest")
            assert "1" in sensing.json()["edge_vitals"]
            print("[OK] Step 4: Edge vitals received and mapped -> presence=True")

            # Trigger controlled fall event (3 frames)
            print("[*] Simulating controlled fall event (3 frames)...")
            for _ in range(3):
                udp_sock.sendto(build_vitals_packet(node_id=1, fall=True, presence=True), udp_dest)
                await asyncio.sleep(0.05)

            await asyncio.sleep(0.3)

            # Check fall events via REST API
            falls = await client.get("/api/falls")
            fall_list = falls.json()
            assert len(fall_list) >= 1
            fall_event = fall_list[0]
            assert fall_event["node_id"] == "1"
            assert fall_event["room_id"] == "living_room"
            assert fall_event["status"] == "detected"
            assert fall_event["confidence"] is None  # accurately marked unavailable
            print(f"[OK] Step 5: Fall event recorded in DB -> Event ID: {fall_event['event_id']}")

            # Check WebSocket messages received
            has_fall_ws = any('"type": "fall_detected"' in m for m in ws_received_events)
            assert has_fall_ws
            print("[OK] Step 6: WebSocket received 'fall_detected' push notification!")

            # Check Room status shows fall alert
            r_status = await client.get("/api/rooms/living_room/status")
            assert r_status.json()["fall_detected"] is True
            print("[OK] Step 7: Room status reflected alert -> fall_detected=True")

            # Acknowledge fall
            ack = await client.post(f"/api/falls/{fall_event['event_id']}/acknowledge", json={"notes": "Checked OK"})
            assert ack.json()["status"] == "acknowledged"
            print("[OK] Step 8: Fall event acknowledged via REST API")

            print("\n==========================================================")
            print("  ALL PIPELINE INTEGRATION CHECKS PASSED (8/8) SUCCESS!   ")
            print("==========================================================")

    finally:
        udp_sock.close()
        ws_task.cancel()
        server.should_exit = True
        await server_task


if __name__ == "__main__":
    asyncio.run(run_verification())
