"""Room management and multi-node spatial configuration."""
from datetime import datetime, timezone
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Room, Node, FallEvent
from app.schemas import RoomResponse, RoomCreate, RoomStatusResponse, NodeStatus, FallStatus
from app.services.node_tracker import node_tracker
from app.services.fall_manager import fall_manager

router = APIRouter(prefix="/rooms", tags=["Rooms"])


@router.get("", response_model=List[RoomResponse])
async def list_rooms(db: AsyncSession = Depends(get_db)):
    """List all monitored rooms along with their associated ESP32-S3 nodes."""
    result = await db.execute(select(Room).options(selectinload(Room.nodes)))
    rooms = result.scalars().all()
    return rooms


@router.post("", response_model=RoomResponse, status_code=status.HTTP_201_CREATED)
async def create_room(req: RoomCreate, db: AsyncSession = Depends(get_db)):
    """Create a new room for multi-node sensor coverage."""
    existing = await db.get(Room, req.id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Room with id '{req.id}' already exists",
        )

    now = datetime.now(timezone.utc)
    room = Room(id=req.id, name=req.name, description=req.description, created_at=now, updated_at=now)
    db.add(room)
    await db.commit()
    stmt = select(Room).where(Room.id == req.id).options(selectinload(Room.nodes))
    result = await db.execute(stmt)
    return result.scalar_one()


@router.get("/{room_id}/status", response_model=RoomStatusResponse)
async def get_room_status(room_id: str, db: AsyncSession = Depends(get_db)):
    """Get live presence, fall alert, and occupancy status for a specific room."""
    room = await db.get(Room, room_id)
    if not room:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Room '{room_id}' not found")

    nodes_res = await db.execute(select(Node).where(Node.room_id == room_id))
    nodes = nodes_res.scalars().all()

    active_nodes = 0
    presence = False
    fall = False
    persons_max = 0

    for node in nodes:
        tracked = node_tracker.get_node_state(node.node_id)
        if tracked and tracked.status == NodeStatus.ONLINE:
            active_nodes += 1
            if tracked.latest_vitals:
                if tracked.latest_vitals.get("presence", False):
                    presence = True
                persons = tracked.latest_vitals.get("n_persons", 0)
                if persons > persons_max:
                    persons_max = persons

        if fall_manager.is_fall_active_for_node(node.node_id):
            fall = True

    # Also check if any unacknowledged fall in DB exists for this room
    if not fall:
        unack_fall = await db.execute(
            select(FallEvent)
            .where(FallEvent.room_id == room_id, FallEvent.status == FallStatus.DETECTED.value)
            .limit(1)
        )
        if unack_fall.scalars().first():
            fall = True

    return RoomStatusResponse(
        room_id=room.id,
        room_name=room.name,
        presence_detected=presence,
        fall_detected=fall,
        active_node_count=active_nodes,
        total_node_count=len(nodes),
        persons_estimate=persons_max,
        latest_event="fall_detected" if fall else ("presence_detected" if presence else "idle"),
        last_updated=datetime.now(timezone.utc),
    )
