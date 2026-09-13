"""Unit tests for fall event debouncing, rising-edge detection, and cooldown."""
import asyncio
from datetime import datetime, timezone
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import FallEvent
from app.services.fall_manager import FallManager


@pytest.fixture
async def test_session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_fall_rising_edge_and_cooldown(test_session_factory):
    manager = FallManager()
    events_broadcast = []

    async def mock_callback(event_type: str, data: dict):
        events_broadcast.append((event_type, data))

    manager.set_event_callback(mock_callback)

    # 1. Normal frame (no fall)
    res = await manager.handle_vitals_sample(
        node_id="esp-1",
        fall_detected=False,
        presence=True,
        room_id="living_room",
        motion_energy=0.1,
        presence_score=1.0,
        session_factory=test_session_factory,
    )
    assert res is None
    assert len(events_broadcast) == 0

    # 2. Rising edge: fall detected -> event must trigger
    res1 = await manager.handle_vitals_sample(
        node_id="esp-1",
        fall_detected=True,
        presence=True,
        room_id="living_room",
        motion_energy=0.8,
        presence_score=5.0,
        session_factory=test_session_factory,
    )
    assert res1 is not None
    assert len(events_broadcast) == 1
    assert events_broadcast[0][0] == "fall_detected"
    assert events_broadcast[0][1]["node_id"] == "esp-1"
    assert events_broadcast[0][1]["confidence"] is None  # not fabricated

    # 3. Subsequent frame with fall_detected=True (within 5000ms cooldown):
    # Must NOT trigger another alert event!
    res2 = await manager.handle_vitals_sample(
        node_id="esp-1",
        fall_detected=True,
        presence=True,
        room_id="living_room",
        motion_energy=0.7,
        presence_score=4.8,
        session_factory=test_session_factory,
    )
    assert res2 is None
    assert len(events_broadcast) == 1  # Still 1, no duplicate

    # 4. Fall cleared: 5 consecutive normal frames
    for i in range(4):
        await manager.handle_vitals_sample(
            node_id="esp-1",
            fall_detected=False,
            presence=True,
            room_id="living_room",
            motion_energy=0.1,
            presence_score=1.0,
            session_factory=test_session_factory,
        )
    assert len(events_broadcast) == 1

    # 5th normal frame triggers clear
    await manager.handle_vitals_sample(
        node_id="esp-1",
        fall_detected=False,
        presence=True,
        room_id="living_room",
        motion_energy=0.1,
        presence_score=1.0,
        session_factory=test_session_factory,
    )
    assert len(events_broadcast) == 2
    assert events_broadcast[1][0] == "fall_cleared"
