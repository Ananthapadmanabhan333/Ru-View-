"""Real-time sensing telemetry endpoints."""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas import SensingLatestResponse, NodeResponse, EdgeVitalsPayload, NodeStatus
from app.services.node_tracker import node_tracker
from app.services.fall_manager import fall_manager

router = APIRouter(prefix="/sensing", tags=["Sensing"])


@router.get("/latest", response_model=SensingLatestResponse)
async def get_latest_sensing():
    """Retrieve the latest real-time sensing state across all ESP32 nodes."""
    now = datetime.now(timezone.utc)
    all_states = node_tracker.get_all_node_states()

    active_nodes = []
    edge_vitals_map = {}
    total_persons = 0

    for node_id, state in all_states.items():
        if state.status == NodeStatus.ONLINE:
            active_nodes.append(
                NodeResponse(
                    node_id=state.node_id,
                    room_id=state.room_id,
                    board_type=state.board_type,
                    status=state.status,
                    last_seen=state.last_seen,
                    rssi=state.rssi,
                    csi_fps=state.csi_fps_ema,
                    ip_address=state.ip_address,
                    firmware_version=state.firmware_version,
                    ruview_version=state.ruview_version,
                    created_at=now,
                    updated_at=now,
                )
            )

        if state.latest_vitals:
            v = state.latest_vitals
            persons = int(v.get("n_persons", 0))
            if v.get("presence", False):
                total_persons = max(total_persons, persons if persons > 0 else 1)

            edge_vitals_map[node_id] = EdgeVitalsPayload(
                node_id=node_id,
                presence=bool(v.get("presence", False)),
                fall_detected=bool(v.get("fall_detected", False)),
                motion=bool(v.get("motion", False)),
                breathing_rate_bpm=v.get("breathing_rate_bpm"),
                heartrate_bpm=v.get("heartrate_bpm"),
                n_persons=persons,
                motion_energy=float(v.get("motion_energy", 0.0) or 0.0),
                presence_score=float(v.get("presence_score", 0.0) or 0.0),
                rssi=int(v.get("rssi", 0) or 0),
                timestamp_ms=int(v.get("timestamp_ms", 0) or 0),
            )

    return SensingLatestResponse(
        timestamp=now,
        source="ruview",
        active_nodes=active_nodes,
        edge_vitals=edge_vitals_map,
        fall_alert_active=fall_manager.has_active_falls(),
        total_persons_present=total_persons,
    )
