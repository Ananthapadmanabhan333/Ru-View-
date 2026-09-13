"""Unit tests for node tracking, heartbeat, and offline transitions."""
import asyncio
from datetime import datetime, timezone, timedelta
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Node
from app.schemas import NodeStatus
from app.services.node_tracker import NodeTracker


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
async def test_node_lifecycle_and_timeout(test_session_factory):
    tracker = NodeTracker()
    events = []

    async def mock_callback(event_type: str, data: dict):
        events.append((event_type, data))

    tracker.set_event_callback(mock_callback)

    # 1. First packet from node -> ONLINE
    await tracker.observe_packet(
        node_id="esp-10",
        packet_type="raw_csi",
        rssi=-54,
        ip_address="192.168.1.105",
        session_factory=test_session_factory,
    )

    state = tracker.get_node_state("esp-10")
    assert state is not None
    assert state.status == NodeStatus.ONLINE
    assert state.rssi == -54
    assert state.ip_address == "192.168.1.105"

    # Allow async task in observe_packet to run
    await asyncio.sleep(0.05)
    assert len(events) == 1
    assert events[0][0] == "node_connected"
    assert events[0][1]["node_id"] == "esp-10"

    # 2. Simulate second packet after 50ms (20 fps)
    await asyncio.sleep(0.05)
    await tracker.observe_packet(
        node_id="esp-10",
        packet_type="raw_csi",
        rssi=-52,
        ip_address="192.168.1.105",
        session_factory=test_session_factory,
    )
    state = tracker.get_node_state("esp-10")
    assert state.csi_fps_ema is not None
    assert 1.0 <= state.csi_fps_ema <= 100.0

    # 3. Simulate node timeout: artificially age last_seen by 6 seconds
    state.last_seen = datetime.now(timezone.utc) - timedelta(seconds=6.0)

    await tracker.check_offline_nodes(test_session_factory)
    assert state.status == NodeStatus.OFFLINE
    assert len(events) == 2
    assert events[1][0] == "node_disconnected"
    assert events[1][1]["node_id"] == "esp-10"
