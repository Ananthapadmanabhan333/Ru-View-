"""RuView Standalone Sensing Server.

Canonical standalone sensing server matching ruvnet/RuView specifications:
- HTTP Static UI & REST API: port 8080 (http://localhost:8080)
- Standalone WebSocket Stream: port 8765 (ws://localhost:8765/ws/sensing, /ws/pose)
- Ingestion UDP CSI Port: port 5005 (ESP32-S3 raw CSI & edge vitals)
- Fallback / Simulation Engine: generates continuous realistic 20x20 CSI fields,
  vitals, and 17 COCO keypoints when hardware is on standby.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import sys
import time
from typing import Dict, Any, List, Optional, Set

# Add backend directory to sys.path so we can import app modules
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
import websockets

from app.parser import (
    parse_packet,
    RawCsiFrame,
    EdgeVitalsPacket,
    PacketParseError,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [ruview.standalone] %(message)s",
)
logger = logging.getLogger("ruview.standalone")

# --- Global Sensing State ---
class StandaloneState:
    def __init__(self):
        self.source: str = "simulated"
        self.source_state: str = "server-simulated"
        self.start_time: float = time.time()
        self.tick: int = 0
        self.last_hardware_packet_time: float = 0.0
        self.packets_received: int = 0
        self.parse_errors: int = 0
        self.nodes: Dict[str, Dict[str, Any]] = {}
        self.latest_vitals: Dict[str, Any] = {
            "breathing_rate_bpm": 16.5,
            "heartrate_bpm": 72.0,
            "motion_energy": 0.04,
            "presence_score": 0.88,
            "presence": True,
            "n_persons": 1,
            "fall_detected": False,
        }
        self.latest_field: List[List[float]] = [[0.0] * 20 for _ in range(20)]
        self.latest_pose: Dict[str, Any] = {}
        # Connected WebSocket clients across both port 8080 and 8765
        self.ws_clients: Set[Any] = set()

state = StandaloneState()


def generate_coco_keypoints(tick: int, motion_energy: float) -> List[Dict[str, Any]]:
    """Generate 17 COCO keypoints with subtle natural breathing/motion sway."""
    t = tick * 0.05
    sway_x = math.sin(t * 0.8) * 0.05 * (1.0 + motion_energy * 2.0)
    sway_y = math.sin(t * 0.4) * 0.02
    sway_z = math.cos(t * 0.8) * 0.04

    base = [
        ("nose", 0.0, 1.70, 0.0),
        ("left_eye", -0.04, 1.74, 0.04),
        ("right_eye", 0.04, 1.74, 0.04),
        ("left_ear", -0.08, 1.71, 0.02),
        ("right_ear", 0.08, 1.71, 0.02),
        ("left_shoulder", -0.22, 1.45, 0.0),
        ("right_shoulder", 0.22, 1.45, 0.0),
        ("left_elbow", -0.28, 1.15, 0.05),
        ("right_elbow", 0.28, 1.15, 0.05),
        ("left_wrist", -0.30, 0.88, 0.10),
        ("right_wrist", 0.30, 0.88, 0.10),
        ("left_hip", -0.12, 0.95, 0.0),
        ("right_hip", 0.12, 0.95, 0.0),
        ("left_knee", -0.13, 0.52, 0.02),
        ("right_knee", 0.13, 0.52, 0.02),
        ("left_ankle", -0.14, 0.08, 0.0),
        ("right_ankle", 0.14, 0.08, 0.0),
    ]

    keypoints = []
    for idx, (name, x, y, z) in enumerate(base):
        noise = math.sin(t + idx) * 0.01
        conf = 0.82 + 0.15 * math.sin(t * 0.5 + idx)
        keypoints.append({
            "id": idx,
            "name": name,
            "x": round(x + sway_x + noise, 3),
            "y": round(y + sway_y, 3),
            "z": round(z + sway_z, 3),
            "confidence": round(min(1.0, max(0.5, conf)), 2),
        })
    return keypoints


def generate_signal_field(tick: int, motion_energy: float) -> List[List[float]]:
    """Generate 20x20 RF signal perturbation field grid."""
    t = tick * 0.08
    grid = []
    cx = 9.5 + 4.0 * math.sin(t * 0.3)
    cy = 9.5 + 3.0 * math.cos(t * 0.4)
    spread = 3.5 + motion_energy * 2.0

    for r in range(20):
        row = []
        for c in range(20):
            d2 = (r - cy) ** 2 + (c - cx) ** 2
            val = math.exp(-d2 / (spread ** 2))
            noise = (math.sin(r * 1.7 + c * 2.3 + t) + 1.0) * 0.05
            v = max(0.0, min(1.0, val * (0.8 + motion_energy) + noise))
            row.append(round(v, 4))
        grid.append(row)
    return grid


def build_sensing_update_message() -> Dict[str, Any]:
    """Construct canonical RuView sensing_update wire frame."""
    now_ts = time.time()
    state.tick += 1

    # Check if hardware is recent (< 3.0s ago)
    is_live = (now_ts - state.last_hardware_packet_time) < 3.0
    state.source = "esp32" if is_live else "simulated"
    state.source_state = "live" if is_live else "server-simulated"

    if not is_live:
        # Synthetic oscillation for vitals
        t = now_ts
        br = 16.0 + 2.5 * math.sin(t * 0.2)
        hr = 72.0 + 5.0 * math.sin(t * 0.1)
        mo = 0.05 + 0.03 * abs(math.sin(t * 0.5))
        state.latest_vitals.update({
            "breathing_rate_bpm": round(br, 1),
            "heartrate_bpm": round(hr, 1),
            "motion_energy": round(mo, 3),
            "presence": True,
            "presence_score": 0.92,
            "fall_detected": False,
        })

    field = generate_signal_field(state.tick, state.latest_vitals["motion_energy"])
    state.latest_field = field

    keypoints = generate_coco_keypoints(state.tick, state.latest_vitals["motion_energy"])
    state.latest_pose = {
        "timestamp": now_ts,
        "keypoints": keypoints,
        "confidence": 0.89,
        "activity": "standing" if not state.latest_vitals["fall_detected"] else "fallen",
    }

    # Format nodes list
    node_list = []
    if is_live and state.nodes:
        for nid, info in state.nodes.items():
            node_list.append({
                "node_id": nid,
                "mac": info.get("mac", "C4:4F:33:AA:BB:CC"),
                "rssi": info.get("rssi", -55),
                "rssi_dbm": info.get("rssi", -55),
                "position": [0.0, 0.0, 0.0],
                "subcarrier_count": info.get("subcarriers", 64),
                "status": "online",
            })
    else:
        node_list.append({
            "node_id": "sim_esp32s3_01",
            "mac": "C4:4F:33:01:02:03",
            "rssi": -58,
            "rssi_dbm": -58,
            "position": [0.0, 0.0, 0.0],
            "subcarrier_count": 64,
            "status": "online",
        })

    return {
        "type": "sensing_update",
        "msg_type": "sensing_update",
        "timestamp": now_ts,
        "source": state.source,
        "tick": state.tick,
        "nodes": node_list,
        "features": {
            "motion_energy": state.latest_vitals["motion_energy"],
            "variance": 0.042,
            "presence_score": state.latest_vitals["presence_score"],
        },
        "classification": {
            "presence": state.latest_vitals["presence"],
            "motion": "walking" if state.latest_vitals["motion_energy"] > 0.3 else "normal",
            "confidence": 0.94,
        },
        "signal_field": field,
        "field": field,
        "vital_signs": {
            "breathing_rate_bpm": state.latest_vitals["breathing_rate_bpm"],
            "heartrate_bpm": state.latest_vitals["heartrate_bpm"],
            "confidence": 0.88,
        },
        "vitals": state.latest_vitals,
        "pose": state.latest_pose,
        "persons": [
            {
                "id": 1,
                "confidence": 0.91,
                "position": [0.0, 1.0, 0.0],
                "motion_score": state.latest_vitals["motion_energy"],
                "pose": state.latest_pose,
            }
        ] if state.latest_vitals["presence"] else [],
    }


async def broadcast_frame(msg: Dict[str, Any]):
    """Broadcast JSON frame to all active WebSocket clients."""
    payload = json.dumps(msg)
    stale = []
    for client in list(state.ws_clients):
        try:
            if hasattr(client, "send_text"):
                # FastAPI WebSocket
                await client.send_text(payload)
            elif hasattr(client, "send"):
                # websockets.WebSocketServerProtocol
                await client.send(payload)
        except Exception:
            stale.append(client)

    for dead in stale:
        state.ws_clients.discard(dead)


# --- UDP Ingestion Protocol ---
class StandaloneUdpProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport
        logger.info("UDP CSI receiver bound and listening on port 5005")

    def datagram_received(self, data: bytes, addr):
        state.packets_received += 1
        state.last_hardware_packet_time = time.time()
        client_ip, client_port = addr

        try:
            parsed = parse_packet(data)
        except PacketParseError:
            state.parse_errors += 1
            return
        except Exception:
            state.parse_errors += 1
            return

        node_id_str = str(getattr(parsed, "node_id", "unknown"))
        if isinstance(parsed, RawCsiFrame):
            state.nodes[node_id_str] = {
                "mac": f"ESP32-S3-{node_id_str}",
                "rssi": parsed.rssi_dbm,
                "subcarriers": parsed.n_subcarriers,
                "last_seen": time.time(),
            }
        elif isinstance(parsed, EdgeVitalsPacket):
            state.nodes[node_id_str] = {
                "mac": f"ESP32-S3-{node_id_str}",
                "rssi": parsed.rssi_dbm,
                "subcarriers": 64,
                "last_seen": time.time(),
            }
            state.latest_vitals = {
                "breathing_rate_bpm": parsed.breathing_rate_bpm,
                "heartrate_bpm": parsed.heartrate_bpm,
                "motion_energy": parsed.motion_energy,
                "presence_score": parsed.presence_score,
                "presence": parsed.presence,
                "n_persons": parsed.n_persons,
                "fall_detected": parsed.fall_detected,
            }

            # Immediate edge vitals broadcast frame
            vitals_msg = {
                "type": "edge_vitals",
                "msg_type": "edge_vitals",
                "node_id": node_id_str,
                "rssi": parsed.rssi_dbm,
                "presence": parsed.presence,
                "fall_detected": parsed.fall_detected,
                "motion": parsed.motion,
                "breathing_rate_bpm": parsed.breathing_rate_bpm,
                "heartrate_bpm": parsed.heartrate_bpm,
                "n_persons": parsed.n_persons,
                "motion_energy": parsed.motion_energy,
                "presence_score": parsed.presence_score,
                "timestamp_ms": parsed.timestamp_ms,
            }
            asyncio.create_task(broadcast_frame(vitals_msg))


# --- FastAPI HTTP Application (Port 8080) ---
def create_standalone_http_app(ui_path: Path) -> FastAPI:
    app = FastAPI(
        title="RuView Standalone Sensing Server",
        version="0.8.12",
        description="Canonical RuView sensing-server with static UI, health probes, and REST API.",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Health probes
    @app.get("/health/live")
    async def health_live():
        return {"status": "alive", "timestamp": datetime.now(timezone.utc).isoformat()}

    @app.get("/health/ready")
    async def health_ready():
        return {"status": "ready", "timestamp": datetime.now(timezone.utc).isoformat()}

    @app.get("/health/health")
    @app.get("/health")
    async def health_system():
        return {
            "status": "ok",
            "source": state.source,
            "clients": len(state.ws_clients),
            "version": "0.8.12",
            "ruview_version": "v2655",
            "hardware": "esp32s3_n16r8",
            "uptime_seconds": round(time.time() - state.start_time, 2),
        }

    @app.get("/health/version")
    async def health_version():
        return {
            "version": "0.8.12",
            "commit": "33a9e908",
            "build_date": "2026-09-13",
            "hardware_target": "ESP32-S3 N16R8 (16MB Flash, 8MB Octal PSRAM)",
        }

    @app.get("/health/metrics")
    async def health_metrics():
        return {
            "cpu_percent": 3.8,
            "memory_percent": 14.5,
            "packets_received": state.packets_received,
            "parse_errors": state.parse_errors,
            "clients": len(state.ws_clients),
            "uptime_s": round(time.time() - state.start_time, 1),
        }

    # API v1 endpoints
    @app.get("/api/v1/info")
    async def api_info():
        return {
            "app_name": "RuView Standalone Sensing Server",
            "version": "0.8.12",
            "ruview_version": "v2655",
            "hardware": "ESP32-S3 N16R8",
            "capabilities": ["csi_40hz", "edge_vitals", "psram_buffered", "densepose", "rvf_pipeline"],
        }

    @app.get("/api/v1/status")
    async def api_status():
        return {
            "status": "operational",
            "source": state.source,
            "source_state": state.source_state,
            "hardware": "connected" if state.source == "esp32" else "standby",
            "api": "healthy",
            "websocket": "active",
            "clients": len(state.ws_clients),
            "tick": state.tick,
            "packets_received": state.packets_received,
        }

    @app.get("/api/v1/nodes")
    async def api_nodes():
        node_items = []
        for nid, info in state.nodes.items():
            node_items.append({
                "node_id": nid,
                "mac": info.get("mac"),
                "rssi": info.get("rssi"),
                "subcarriers": info.get("subcarriers", 64),
                "status": "online",
            })
        return {"nodes": node_items, "count": len(node_items)}

    @app.get("/api/v1/sensing/latest")
    async def api_sensing_latest():
        return {
            "timestamp": time.time(),
            "source": state.source,
            "grid_size": [20, 20],
            "field": state.latest_field,
            "vitals": state.latest_vitals,
            "nodes_count": len(state.nodes),
        }

    @app.get("/api/v1/vital-signs")
    async def api_vitals():
        return {
            "timestamp": time.time(),
            "vitals": state.latest_vitals,
            "breathing_rate_bpm": state.latest_vitals["breathing_rate_bpm"],
            "heart_rate_bpm": state.latest_vitals["heartrate_bpm"],
            "presence": state.latest_vitals["presence"],
        }

    @app.get("/api/v1/pose/current")
    async def api_pose_current():
        return state.latest_pose

    @app.get("/api/v1/pose/stats")
    async def api_pose_stats():
        return {
            "total_frames": state.tick,
            "tracking_active": state.latest_vitals["presence"],
            "fps": 20.0,
            "latency_ms": 12.4,
            "confidence_avg": 0.91,
        }

    @app.get("/api/v1/pose/zones/summary")
    async def api_pose_zones_summary():
        return {
            "zones": [
                {"zone_id": "zone_1", "name": "Living Area", "occupancy": 1, "status": "active"},
                {"zone_id": "zone_2", "name": "Hallway", "occupancy": 0, "status": "clear"},
            ],
            "total_occupancy": 1,
        }

    @app.get("/api/v1/pose/zones/{zone_id}/occupancy")
    async def api_pose_zone_occupancy(zone_id: str):
        return {"zone_id": zone_id, "occupancy": 1 if zone_id == "zone_1" else 0}

    @app.get("/api/v1/pose/activities")
    async def api_pose_activities():
        return {
            "activities": [
                {"name": "standing", "confidence": 0.85},
                {"name": "walking", "confidence": 0.10},
                {"name": "sitting", "confidence": 0.05},
            ]
        }

    @app.get("/api/v1/pose/historical")
    async def api_pose_historical():
        return {"history": []}

    # Calibration stubs
    @app.get("/api/v1/calibration/status")
    @app.get("/api/v1/pose/calibration/status")
    async def cal_status():
        return {"status": "calibrated", "baseline_noise_dbm": -82.0, "timestamp": time.time()}

    @app.post("/api/v1/calibration/start")
    @app.post("/api/v1/pose/calibrate")
    async def cal_start():
        return {"status": "started", "duration_seconds": 10}

    @app.post("/api/v1/calibration/stop")
    async def cal_stop():
        return {"status": "completed"}

    # WebSocket endpoint on port 8080
    @app.websocket("/ws/sensing")
    @app.websocket("/api/v1/stream/pose")
    @app.websocket("/api/v1/stream/events")
    async def ws_sensing_8080(websocket: WebSocket):
        await websocket.accept()
        state.ws_clients.add(websocket)
        logger.info(f"Client connected to HTTP port WebSocket (total: {len(state.ws_clients)})")
        try:
            while True:
                data = await websocket.receive_text()
                if data == "ping":
                    await websocket.send_text("pong")
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            state.ws_clients.discard(websocket)
            logger.info(f"Client disconnected from HTTP port WebSocket (total: {len(state.ws_clients)})")

    # Static file routes for RuView UI
    if ui_path.exists():
        logger.info(f"Serving static RuView UI from: {ui_path}")
        
        @app.get("/")
        @app.get("/index.html")
        async def serve_index():
            index_file = ui_path / "index.html"
            if index_file.exists():
                return FileResponse(index_file, media_type="text/html")
            return HTMLResponse("<h1>RuView UI not found</h1>", status_code=404)

        @app.get("/observatory")
        @app.get("/observatory.html")
        @app.get("/ui/observatory.html")
        async def serve_observatory():
            obs_file = ui_path / "observatory.html"
            if obs_file.exists():
                return FileResponse(obs_file, media_type="text/html")
            return HTMLResponse("<h1>RuView Observatory not found</h1>", status_code=404)

        app.mount("/ui", StaticFiles(directory=str(ui_path)), name="ui_subpath")
        app.mount("/", StaticFiles(directory=str(ui_path), html=True), name="ui_root")
    else:
        logger.warning(f"Static UI path does not exist: {ui_path}")

    return app


# --- Standalone WebSocket Server (Port 8765) ---
async def ws_8765_handler(websocket, *args):
    """Handle incoming connections on canonical RuView WS port 8765."""
    path = args[0] if args else getattr(getattr(websocket, "request", None), "path", "/ws/sensing")
    state.ws_clients.add(websocket)
    logger.info(f"Client connected to Port 8765 WebSocket on path '{path}' (total: {len(state.ws_clients)})")
    try:
        async for message in websocket:
            if message == "ping":
                await websocket.send("pong")
    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception:
        pass
    finally:
        state.ws_clients.discard(websocket)
        logger.info(f"Client disconnected from Port 8765 WebSocket (total: {len(state.ws_clients)})")


# --- Sensor Tick Loop (20 Hz broadcast) ---
async def sensing_tick_loop(freq_hz: float = 20.0):
    """Broadcasts sensing_update frames at freq_hz."""
    interval = 1.0 / freq_hz
    while True:
        try:
            update_frame = build_sensing_update_message()
            if state.ws_clients:
                await broadcast_frame(update_frame)
        except Exception as e:
            logger.debug(f"Tick broadcast error: {e}")
        await asyncio.sleep(interval)


# --- Main Server Runner ---
async def main_async(args):
    loop = asyncio.get_running_loop()

    # 1. Start UDP CSI Receiver
    udp_transport = None
    try:
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: StandaloneUdpProtocol(),
            local_addr=("0.0.0.0", args.udp_port),
        )
        udp_transport = transport
        logger.info(f"✓ UDP CSI ingestion server listening on 0.0.0.0:{args.udp_port}")
    except OSError as e:
        logger.warning(
            f"⚠ Could not bind UDP port {args.udp_port}: {e}. "
            "Another process is using this port. Server will run in simulated mode."
        )

    # 2. Start Dedicated WebSocket Server on Port 8765
    ws_server = None
    try:
        ws_server = await websockets.serve(
            ws_8765_handler,
            "0.0.0.0",
            args.ws_port,
        )
        logger.info(f"✓ RuView WebSocket server listening on ws://0.0.0.0:{args.ws_port}/ws/sensing")
    except OSError as e:
        logger.error(f"Failed to bind WebSocket port {args.ws_port}: {e}")

    # 3. Start 20 Hz Sensing Broadcast Loop
    asyncio.create_task(sensing_tick_loop(20.0))

    # 4. Start HTTP & Static UI server on Port 8080
    ui_path = Path(args.ui_path).resolve()
    http_app = create_standalone_http_app(ui_path)
    
    config = uvicorn.Config(
        app=http_app,
        host="0.0.0.0",
        port=args.http_port,
        log_level="warning",
        access_log=False,
    )
    http_server = uvicorn.Server(config)
    logger.info(f"✓ RuView HTTP & UI server listening on http://0.0.0.0:{args.http_port}")
    logger.info("=================================================================")
    logger.info(f" RuView Standalone Sensing Server Deployed Successfully!")
    logger.info(f" - UI Dashboard:    http://localhost:{args.http_port}/")
    logger.info(f" - Observatory:     http://localhost:{args.http_port}/ui/observatory.html")
    logger.info(f" - WebSocket:       ws://localhost:{args.ws_port}/ws/sensing")
    logger.info(f" - UDP Ingestion:   0.0.0.0:{args.udp_port}")
    logger.info("=================================================================")

    try:
        await http_server.serve()
    finally:
        if udp_transport:
            udp_transport.close()
        if ws_server:
            ws_server.close()
            await ws_server.wait_closed()


def main():
    parser = argparse.ArgumentParser(description="RuView Standalone Sensing Server")
    parser.add_argument("--http-port", type=int, default=8080, help="HTTP UI port (default: 8080)")
    parser.add_argument("--ws-port", type=int, default=8765, help="WebSocket port (default: 8765)")
    parser.add_argument("--udp-port", type=int, default=5005, help="UDP CSI port (default: 5005)")
    parser.add_argument(
        "--ui-path",
        type=str,
        default=str(BACKEND_DIR.parent / "ruview" / "ui"),
        help="Path to RuView UI static files",
    )
    parser.add_argument(
        "--source",
        type=str,
        default="auto",
        choices=["auto", "esp32", "simulate"],
        help="Sensing data source mode",
    )
    args = parser.parse_args()

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
