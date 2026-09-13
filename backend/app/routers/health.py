"""Health and system status routes."""
import time
from datetime import datetime, timezone
from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Node, Room, FallEvent
from app.schemas import HealthResponse, SystemStatusResponse, NodeStatus, FallStatus, FallEventResponse
from app.services.node_tracker import node_tracker
from app.services.fall_manager import fall_manager

router = APIRouter(tags=["Health & System"])
_start_time = time.time()


@router.get("/health", response_model=HealthResponse)
async def get_health():
    """Health check endpoint providing uptime and system connectivity status."""
    uptime = round(time.time() - _start_time, 2)
    node_states = node_tracker.get_all_node_states()
    online_count = sum(1 for s in node_states.values() if s.status == NodeStatus.ONLINE)

    return HealthResponse(
        status="healthy",
        version=settings.VERSION,
        uptime_seconds=uptime,
        ruview_connected=bool(settings.RUVIEW_WS_URL),
        udp_listener_active=settings.UDP_ENABLED,
        online_nodes=online_count,
        total_nodes=len(node_states),
    )


@router.get("/system/status", response_model=SystemStatusResponse)
async def get_system_status(db: AsyncSession = Depends(get_db)):
    """System-wide operational overview including active falls, monitored rooms, and node counts."""
    node_states = node_tracker.get_all_node_states()
    online_count = sum(1 for s in node_states.values() if s.status == NodeStatus.ONLINE)
    offline_count = sum(1 for s in node_states.values() if s.status == NodeStatus.OFFLINE)

    room_count_res = await db.execute(select(func.count(Room.id)))
    room_count = room_count_res.scalar() or 0

    active_falls_res = await db.execute(
        select(func.count(FallEvent.event_id)).where(FallEvent.status == FallStatus.DETECTED.value)
    )
    active_falls = active_falls_res.scalar() or 0

    latest_fall_res = await db.execute(
        select(FallEvent).order_by(FallEvent.timestamp.desc()).limit(1)
    )
    latest_fall = latest_fall_res.scalars().first()

    latest_fall_dto = FallEventResponse.model_validate(latest_fall) if latest_fall else None

    # Latest telemetry timestamp across all nodes
    last_telemetry_at = None
    for state in node_states.values():
        if state.last_seen:
            if last_telemetry_at is None or state.last_seen > last_telemetry_at:
                last_telemetry_at = state.last_seen

    overall_status = "CRITICAL_ALERT" if active_falls > 0 else ("ONLINE" if online_count > 0 else "STANDBY")

    return SystemStatusResponse(
        status=overall_status,
        active_falls=active_falls,
        online_nodes=online_count,
        offline_nodes=offline_count,
        rooms_monitored=room_count,
        last_fall_event=latest_fall_dto,
        last_telemetry_at=last_telemetry_at,
    )
