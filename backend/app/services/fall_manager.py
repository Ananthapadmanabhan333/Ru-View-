"""Fall event processing, debouncing, cooldown, and lifecycle management."""
import asyncio
from datetime import datetime, timezone, timedelta
import logging
from typing import Dict, Optional, Any, Callable
import uuid
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import FallEvent, SystemEvent
from app.schemas import FallStatus

logger = logging.getLogger("ruview.fall_manager")


class NodeFallState:
    def __init__(self, node_id: str):
        self.node_id = node_id
        self.last_fall_detected = False
        self.last_alert_time: Optional[datetime] = None
        self.active_event_id: Optional[str] = None
        self.clear_frame_count: int = 0


class FallManager:
    def __init__(self):
        self._node_states: Dict[str, NodeFallState] = {}
        self._lock = asyncio.Lock()
        self._event_callback: Optional[Callable[[str, Dict[str, Any]], Any]] = None

    def set_event_callback(self, callback: Callable[[str, Dict[str, Any]], Any]):
        self._event_callback = callback

    async def handle_vitals_sample(
        self,
        node_id: str,
        fall_detected: bool,
        presence: bool,
        room_id: Optional[str],
        motion_energy: float,
        presence_score: float,
        session_factory: Callable[[], AsyncSession],
    ) -> Optional[str]:
        """Processes incoming vitals sample with rising edge detection and cooldown."""
        now = datetime.now(timezone.utc)
        node_id_str = str(node_id)
        triggered_event_id: Optional[str] = None
        cleared_event_id: Optional[str] = None

        async with self._lock:
            if node_id_str not in self._node_states:
                self._node_states[node_id_str] = NodeFallState(node_id_str)
            state = self._node_states[node_id_str]

            prev_fall = state.last_fall_detected
            state.last_fall_detected = fall_detected

            if fall_detected:
                state.clear_frame_count = 0
                cooldown_td = timedelta(milliseconds=settings.FALL_COOLDOWN_MS)
                is_cooldown_active = (
                    state.last_alert_time is not None
                    and (now - state.last_alert_time) < cooldown_td
                )

                # Rising edge or outside cooldown
                if not prev_fall and not is_cooldown_active:
                    triggered_event_id = str(uuid.uuid4())
                    state.active_event_id = triggered_event_id
                    state.last_alert_time = now
                    logger.warning(
                        f"FALL DETECTED on node {node_id_str} (room: {room_id})! Event ID: {triggered_event_id}"
                    )
            else:
                # If fall was previously detected, wait for clear debounce (5 frames)
                if state.active_event_id is not None:
                    state.clear_frame_count += 1
                    if state.clear_frame_count >= 5:
                        cleared_event_id = state.active_event_id
                        state.active_event_id = None
                        logger.info(f"Fall alert cleared for node {node_id_str} (Event ID: {cleared_event_id})")

        # Persist and broadcast triggered fall
        if triggered_event_id:
            await self._persist_fall_detected(
                triggered_event_id,
                node_id_str,
                room_id,
                now,
                motion_energy,
                presence_score,
                session_factory,
            )
            if self._event_callback:
                await self._event_callback(
                    "fall_detected",
                    {
                        "event_id": triggered_event_id,
                        "type": "fall_detected",
                        "node_id": node_id_str,
                        "room_id": room_id,
                        "timestamp": now.isoformat(),
                        "confidence": None,  # RuView threshold detector provides no probabilistic confidence
                        "status": "detected",
                        "source": "ruview",
                        "motion_energy": motion_energy,
                        "presence_score": presence_score,
                    },
                )

        # Persist and broadcast cleared fall
        if cleared_event_id:
            await self._persist_fall_cleared(cleared_event_id, now, session_factory)
            if self._event_callback:
                await self._event_callback(
                    "fall_cleared",
                    {
                        "event_id": cleared_event_id,
                        "type": "fall_cleared",
                        "node_id": node_id_str,
                        "room_id": room_id,
                        "timestamp": now.isoformat(),
                        "status": "cleared",
                    },
                )

        return triggered_event_id

    async def _persist_fall_detected(
        self,
        event_id: str,
        node_id: str,
        room_id: Optional[str],
        timestamp: datetime,
        motion_energy: float,
        presence_score: float,
        session_factory: Callable[[], AsyncSession],
    ):
        try:
            async with session_factory() as session:
                if not room_id:
                    from app.models import Node
                    node = await session.get(Node, node_id)
                    if node and node.room_id:
                        room_id = node.room_id

                event = FallEvent(
                    event_id=event_id,
                    type="fall",
                    node_id=node_id,
                    room_id=room_id,
                    timestamp=timestamp,
                    confidence=None,
                    status=FallStatus.DETECTED.value,
                    source="ruview",
                    motion_energy=motion_energy,
                    presence_score=presence_score,
                )
                session.add(event)
                await session.commit()
        except Exception as e:
            logger.error(f"Failed to persist fall event {event_id}: {e}", exc_info=True)

    async def _persist_fall_cleared(
        self,
        event_id: str,
        cleared_at: datetime,
        session_factory: Callable[[], AsyncSession],
    ):
        try:
            async with session_factory() as session:
                event = await session.get(FallEvent, event_id)
                if event:
                    event.status = FallStatus.CLEARED.value
                    event.cleared_at = cleared_at
                    await session.commit()
        except Exception as e:
            logger.error(f"Failed to update cleared fall event {event_id}: {e}", exc_info=True)

    def is_fall_active_for_node(self, node_id: str) -> bool:
        node_id_str = str(node_id)
        return self._node_states.get(node_id_str, NodeFallState(node_id_str)).active_event_id is not None

    def has_active_falls(self) -> bool:
        return any(state.active_event_id is not None for state in self._node_states.values())


fall_manager = FallManager()
