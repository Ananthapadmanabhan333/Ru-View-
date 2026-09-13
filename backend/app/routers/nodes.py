"""ESP32 Node management REST endpoints."""
from datetime import datetime, timezone
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Node, Room
from app.schemas import NodeResponse, NodeRegisterRequest, NodeStatus
from app.services.node_tracker import node_tracker

router = APIRouter(prefix="/nodes", tags=["Nodes"])


@router.get("", response_model=List[NodeResponse])
async def list_nodes(db: AsyncSession = Depends(get_db)):
    """List all registered and discovered ESP32-S3 nodes with live connection metrics."""
    result = await db.execute(select(Node))
    db_nodes = result.scalars().all()
    out = []

    for node in db_nodes:
        tracked = node_tracker.get_node_state(node.node_id)
        current_status = tracked.status if tracked else NodeStatus(node.status)
        last_seen = tracked.last_seen if tracked and tracked.last_seen else node.last_seen
        rssi = tracked.rssi if tracked and tracked.rssi is not None else node.rssi
        csi_fps = tracked.csi_fps_ema if tracked and tracked.csi_fps_ema is not None else node.csi_fps

        dto = NodeResponse(
            node_id=node.node_id,
            room_id=node.room_id,
            mac_address=node.mac_address,
            firmware_version=node.firmware_version,
            ruview_version=node.ruview_version,
            ip_address=node.ip_address,
            status=current_status,
            last_seen=last_seen,
            rssi=rssi,
            csi_fps=csi_fps,
            created_at=node.created_at,
            updated_at=node.updated_at,
        )
        out.append(dto)

    return out


@router.get("/{node_id}", response_model=NodeResponse)
async def get_node(node_id: str, db: AsyncSession = Depends(get_db)):
    """Get metadata, connection state, and metrics for a specific node."""
    node = await db.get(Node, str(node_id))
    if not node:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Node {node_id} not found")

    tracked = node_tracker.get_node_state(node.node_id)
    current_status = tracked.status if tracked else NodeStatus(node.status)
    last_seen = tracked.last_seen if tracked and tracked.last_seen else node.last_seen
    rssi = tracked.rssi if tracked and tracked.rssi is not None else node.rssi
    csi_fps = tracked.csi_fps_ema if tracked and tracked.csi_fps_ema is not None else node.csi_fps

    return NodeResponse(
        node_id=node.node_id,
        room_id=node.room_id,
        mac_address=node.mac_address,
        firmware_version=node.firmware_version,
        ruview_version=node.ruview_version,
        ip_address=node.ip_address,
        status=current_status,
        last_seen=last_seen,
        rssi=rssi,
        csi_fps=csi_fps,
        created_at=node.created_at,
        updated_at=node.updated_at,
    )


@router.post("/{node_id}/register", response_model=NodeResponse)
async def register_node(node_id: str, req: NodeRegisterRequest, db: AsyncSession = Depends(get_db)):
    """Register an ESP32-S3 node or associate it with a room."""
    node_id_str = str(node_id)

    if req.room_id:
        room = await db.get(Room, req.room_id)
        if not room:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Room '{req.room_id}' does not exist")

    node = await db.get(Node, node_id_str)
    now = datetime.now(timezone.utc)

    if not node:
        node = Node(
            node_id=node_id_str,
            room_id=req.room_id,
            mac_address=req.mac_address,
            firmware_version=req.firmware_version or "0.8.12",
            ruview_version="v2655",
            status=NodeStatus.UNKNOWN.value,
            created_at=now,
            updated_at=now,
        )
        db.add(node)
    else:
        if req.room_id is not None:
            node.room_id = req.room_id
        if req.mac_address is not None:
            node.mac_address = req.mac_address
        if req.firmware_version is not None:
            node.firmware_version = req.firmware_version
        node.updated_at = now

    await db.commit()
    await db.refresh(node)

    # Sync room with in-memory tracker
    node_tracker.associate_room(node_id_str, req.room_id)

    tracked = node_tracker.get_node_state(node_id_str)
    current_status = tracked.status if tracked else NodeStatus(node.status)

    return NodeResponse(
        node_id=node.node_id,
        room_id=node.room_id,
        mac_address=node.mac_address,
        firmware_version=node.firmware_version,
        ruview_version=node.ruview_version,
        ip_address=node.ip_address,
        status=current_status,
        last_seen=node.last_seen,
        rssi=node.rssi,
        csi_fps=node.csi_fps,
        created_at=node.created_at,
        updated_at=node.updated_at,
    )
