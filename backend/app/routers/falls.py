"""Fall event queries and acknowledgment endpoints."""
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import FallEvent
from app.schemas import FallEventResponse, FallEventAcknowledgeRequest, FallStatus
from app.services.ws_broadcaster import ws_broadcaster

router = APIRouter(prefix="/falls", tags=["Falls"])


@router.get("", response_model=List[FallEventResponse])
async def list_falls(
    node_id: Optional[str] = Query(None, description="Filter by node ID"),
    room_id: Optional[str] = Query(None, description="Filter by room ID"),
    status: Optional[str] = Query(None, description="Filter by status: detected, cleared, acknowledged"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Retrieve historical and active fall detection events."""
    query = select(FallEvent).order_by(FallEvent.timestamp.desc())

    if node_id:
        query = query.where(FallEvent.node_id == node_id)
    if room_id:
        query = query.where(FallEvent.room_id == room_id)
    if status:
        query = query.where(FallEvent.status == status)

    query = query.offset(offset).limit(limit)
    result = await db.execute(query)
    events = result.scalars().all()
    return events


@router.get("/{event_id}", response_model=FallEventResponse)
async def get_fall(event_id: str, db: AsyncSession = Depends(get_db)):
    """Retrieve details for a specific fall event by its UUID."""
    event = await db.get(FallEvent, event_id)
    if not event:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Fall event '{event_id}' not found")
    return event


@router.post("/{event_id}/acknowledge", response_model=FallEventResponse)
async def acknowledge_fall(
    event_id: str,
    req: FallEventAcknowledgeRequest,
    db: AsyncSession = Depends(get_db),
):
    """Acknowledge a detected fall event from dashboard/mobile app."""
    event = await db.get(FallEvent, event_id)
    if not event:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Fall event '{event_id}' not found")

    event.status = FallStatus.ACKNOWLEDGED.value
    if not event.cleared_at:
        event.cleared_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(event)

    await ws_broadcaster.broadcast(
        "fall_cleared",
        {
            "event_id": event.event_id,
            "node_id": event.node_id,
            "room_id": event.room_id,
            "status": "acknowledged",
            "notes": req.notes,
        },
    )

    return event
