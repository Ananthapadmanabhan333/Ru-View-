"""Node state management and online/offline monitoring."""
import asyncio
from datetime import datetime, timezone
import logging
from typing import Dict, Optional, Any, Callable
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Node, SystemEvent
from app.schemas import NodeStatus

logger = logging.getLogger("ruview.node_tracker")


class TrackedNodeState:
    def __init__(self, node_id: str):
        self.node_id = node_id
        self.status: NodeStatus = NodeStatus.UNKNOWN
        self.last_seen: Optional[datetime] = None
        self.ip_address: Optional[str] = None
        self.rssi: Optional[int] = None
        self.csi_fps_ema: Optional[float] = None
        self.last_frame_time: Optional[float] = None
        self.room_id: Optional[str] = None
        self.firmware_version: str = "0.8.12"
        self.ruview_version: str = "v2655"
        self.latest_vitals: Optional[Dict[str, Any]] = None


class NodeTracker:
    def __init__(self):
        self._nodes: Dict[str, TrackedNodeState] = {}
        self._lock = asyncio.Lock()
        self._event_callback: Optional[Callable[[str, Dict[str, Any]], Any]] = None
        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None

    def set_event_callback(self, callback: Callable[[str, Dict[str, Any]], Any]):
        """Sets callback for publishing node connection/disconnection events."""
        self._event_callback = callback

    async def initialize_from_db(self, session: AsyncSession):
        """Pre-populate tracker from database nodes on startup."""
        async with self._lock:
            result = await session.execute(select(Node))
            for node in result.scalars().all():
                state = TrackedNodeState(str(node.node_id))
                state.status = NodeStatus(node.status) if node.status in NodeStatus.__members__ else NodeStatus.UNKNOWN
                state.room_id = node.room_id
                state.ip_address = node.ip_address
                state.rssi = node.rssi
                state.csi_fps_ema = node.csi_fps
                state.last_seen = node.last_seen
                state.firmware_version = node.firmware_version or "0.8.12"
                state.ruview_version = node.ruview_version or "v2655"
                self._nodes[str(node.node_id)] = state

    async def observe_packet(
        self,
        node_id: str,
        packet_type: str,
        rssi: Optional[int] = None,
        ip_address: Optional[str] = None,
        vitals_dict: Optional[Dict[str, Any]] = None,
        session_factory: Optional[Callable[[], AsyncSession]] = None,
    ):
        """Record packet arrival from a node and update online status."""
        now = datetime.now(timezone.utc)
        now_ts = now.timestamp()
        node_id_str = str(node_id)

        transition_to_online = False

        async with self._lock:
            if node_id_str not in self._nodes:
                state = TrackedNodeState(node_id_str)
                self._nodes[node_id_str] = state
            else:
                state = self._nodes[node_id_str]

            prev_status = state.status

            # Calculate CSI fps EMA (alpha = 0.125, matches RuView update_csi_fps_ema)
            if state.last_frame_time is not None:
                dt = now_ts - state.last_frame_time
                if 0.001 < dt < 1.0:
                    instant_fps = 1.0 / dt
                    if state.csi_fps_ema is None:
                        state.csi_fps_ema = instant_fps
                    else:
                        state.csi_fps_ema = (0.875 * state.csi_fps_ema) + (0.125 * instant_fps)

            state.last_frame_time = now_ts
            state.last_seen = now
            if rssi is not None:
                state.rssi = rssi
            if ip_address is not None:
                state.ip_address = ip_address
            if vitals_dict is not None:
                state.latest_vitals = vitals_dict

            if prev_status != NodeStatus.ONLINE:
                state.status = NodeStatus.ONLINE
                transition_to_online = True

        if transition_to_online:
            logger.info(f"Node {node_id_str} is now ONLINE (first packet or reconnected)")
            if self._event_callback:
                asyncio.create_task(
                    self._event_callback(
                        "node_connected",
                        {
                            "node_id": node_id_str,
                            "room_id": state.room_id,
                            "ip_address": state.ip_address,
                            "rssi": state.rssi,
                            "timestamp": now.isoformat(),
                        },
                    )
                )

            # Persist to DB
            if session_factory:
                await self._persist_node_status(node_id_str, session_factory)

    async def _persist_node_status(self, node_id_str: str, session_factory: Callable[[], AsyncSession]):
        try:
            async with session_factory() as session:
                node = await session.get(Node, node_id_str)
                state = self._nodes.get(node_id_str)
                if state:
                    if node is None:
                        node = Node(
                            node_id=node_id_str,
                            status=state.status.value,
                            ip_address=state.ip_address,
                            rssi=state.rssi,
                            csi_fps=state.csi_fps_ema,
                            last_seen=state.last_seen,
                        )
                        session.add(node)
                    else:
                        node.status = state.status.value
                        node.ip_address = state.ip_address
                        node.rssi = state.rssi
                        node.csi_fps = state.csi_fps_ema
                        node.last_seen = state.last_seen
                    await session.commit()
        except Exception as e:
            logger.warning(f"Failed to persist node status for {node_id_str}: {e}")

    async def check_offline_nodes(self, session_factory: Optional[Callable[[], AsyncSession]] = None):
        """Scans tracked nodes and transitions timed-out nodes to OFFLINE."""
        now = datetime.now(timezone.utc)
        timeout_s = settings.NODE_OFFLINE_TIMEOUT_S
        nodes_went_offline = []

        async with self._lock:
            for node_id_str, state in self._nodes.items():
                if state.status == NodeStatus.ONLINE and state.last_seen is not None:
                    elapsed = (now - state.last_seen).total_seconds()
                    if elapsed > timeout_s:
                        state.status = NodeStatus.OFFLINE
                        nodes_went_offline.append((node_id_str, state.room_id))
                        logger.warning(f"Node {node_id_str} timed out ({elapsed:.1f}s > {timeout_s}s) -> OFFLINE")

        for node_id_str, room_id in nodes_went_offline:
            if self._event_callback:
                await self._event_callback(
                    "node_disconnected",
                    {
                        "node_id": node_id_str,
                        "room_id": room_id,
                        "timestamp": now.isoformat(),
                        "reason": "heartbeat_timeout",
                    },
                )
            if session_factory:
                await self._persist_node_status(node_id_str, session_factory)

    def get_node_state(self, node_id: str) -> Optional[TrackedNodeState]:
        return self._nodes.get(str(node_id))

    def get_all_node_states(self) -> Dict[str, TrackedNodeState]:
        return dict(self._nodes)

    def associate_room(self, node_id: str, room_id: Optional[str]):
        node_id_str = str(node_id)
        if node_id_str not in self._nodes:
            self._nodes[node_id_str] = TrackedNodeState(node_id_str)
        self._nodes[node_id_str].room_id = room_id

    async def start_monitor_loop(self, session_factory: Callable[[], AsyncSession]):
        """Background heartbeat monitoring loop."""
        self._running = True
        while self._running:
            try:
                await asyncio.sleep(1.0)
                await self.check_offline_nodes(session_factory)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in node offline monitor loop: {e}", exc_info=True)

    def stop_monitor_loop(self):
        self._running = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()


node_tracker = NodeTracker()
