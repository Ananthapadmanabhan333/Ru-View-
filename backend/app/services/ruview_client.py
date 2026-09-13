"""Upstream client connecting to RuView sensing-server WebSocket endpoint."""
import asyncio
import json
import logging
from typing import Optional, Callable
import websockets
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.node_tracker import node_tracker
from app.services.fall_manager import fall_manager
from app.services.ws_broadcaster import ws_broadcaster

logger = logging.getLogger("ruview.upstream_client")


class RuViewWebSocketClient:
    def __init__(self, session_factory: Callable[[], AsyncSession]):
        self.session_factory = session_factory
        self.ws_url = settings.RUVIEW_WS_URL
        self._connected = False
        self._task: Optional[asyncio.Task] = None
        self._running = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def start(self):
        if not self.ws_url:
            logger.info("RUVIEW_WS_URL not configured; operating in standalone UDP ingestion mode.")
            return

        self._running = True
        self._task = asyncio.create_task(self._connect_loop())

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def _connect_loop(self):
        reconnect_delay = 2.0
        while self._running:
            try:
                connect_kwargs = {}
                if settings.RUVIEW_API_TOKEN:
                    connect_kwargs["additional_headers"] = {"Authorization": f"Bearer {settings.RUVIEW_API_TOKEN}"}

                logger.info(f"Connecting to upstream RuView WebSocket: {self.ws_url}")
                async with websockets.connect(self.ws_url, **connect_kwargs) as ws:
                    self._connected = True
                    reconnect_delay = 2.0
                    logger.info("Connected to upstream RuView sensing-server!")

                    await ws_broadcaster.broadcast(
                        "system_warning",
                        {"message": "Connected to upstream RuView sensing-server", "level": "info"},
                    )

                    async for message in ws:
                        if not self._running:
                            break
                        await self._handle_message(message)

            except (websockets.ConnectionClosed, ConnectionRefusedError, OSError) as e:
                self._connected = False
                logger.warning(f"RuView upstream WebSocket disconnected ({e}). Reconnecting in {reconnect_delay:.1f}s...")
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                logger.error(f"Unexpected error in RuView client: {e}", exc_info=True)

            self._connected = False
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 1.5, 30.0)

    async def _handle_message(self, raw_message: str):
        try:
            data = json.loads(raw_message)
            msg_type = data.get("type") or data.get("msg_type")

            if msg_type == "edge_vitals":
                node_id_str = str(data.get("node_id", "unknown"))
                presence = bool(data.get("presence", False))
                fall_detected = bool(data.get("fall_detected", False))
                motion_energy = float(data.get("motion_energy", 0.0) or 0.0)
                presence_score = float(data.get("presence_score", 0.0) or 0.0)
                rssi = data.get("rssi")

                tracked = node_tracker.get_node_state(node_id_str)
                room_id = tracked.room_id if tracked else None

                await node_tracker.observe_packet(
                    node_id=node_id_str,
                    packet_type="edge_vitals",
                    rssi=rssi,
                    vitals_dict=data,
                    session_factory=self.session_factory,
                )

                await fall_manager.handle_vitals_sample(
                    node_id=node_id_str,
                    fall_detected=fall_detected,
                    presence=presence,
                    room_id=room_id,
                    motion_energy=motion_energy,
                    presence_score=presence_score,
                    session_factory=self.session_factory,
                )

            elif msg_type == "sensing_update":
                # Multi-node sensing update from RuView
                nodes = data.get("nodes", [])
                for n in nodes:
                    node_id_str = str(n.get("node_id", "unknown"))
                    rssi = n.get("rssi")
                    await node_tracker.observe_packet(
                        node_id=node_id_str,
                        packet_type="sensing_update",
                        rssi=rssi,
                        session_factory=self.session_factory,
                    )

        except json.JSONDecodeError:
            logger.debug(f"Invalid JSON received from RuView: {raw_message[:100]}")
        except Exception as e:
            logger.error(f"Error parsing RuView message: {e}", exc_info=True)
