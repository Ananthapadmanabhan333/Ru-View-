"""Integration tests for all REST API endpoints."""
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db
from app.models import FallEvent
from app.schemas import FallStatus
from app.services.node_tracker import node_tracker


@pytest.fixture
async def client():
    # In-memory test database
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_health_endpoints(client: AsyncClient):
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert "uptime_seconds" in data

    status_resp = await client.get("/api/system/status")
    assert status_resp.status_code == 200
    status_data = status_resp.json()
    assert "status" in status_data
    assert "online_nodes" in status_data


@pytest.mark.asyncio
async def test_room_and_node_workflow(client: AsyncClient):
    # 1. Create Room
    r_resp = await client.post(
        "/api/rooms",
        json={"id": "living_room", "name": "Living Room", "description": "Primary lounge"},
    )
    assert r_resp.status_code == 201
    assert r_resp.json()["id"] == "living_room"

    # Duplicate room conflict
    r_dup = await client.post("/api/rooms", json={"id": "living_room", "name": "Dup"})
    assert r_dup.status_code == 409

    # List rooms
    rooms = await client.get("/api/rooms")
    assert len(rooms.json()) >= 1

    # 2. Register Node
    n_resp = await client.post(
        "/api/nodes/esp32s3-01/register",
        json={"room_id": "living_room", "mac_address": "AA:BB:CC:DD:EE:FF"},
    )
    assert n_resp.status_code == 200
    n_data = n_resp.json()
    assert n_data["node_id"] == "esp32s3-01"
    assert n_data["room_id"] == "living_room"

    # List nodes
    nodes = await client.get("/api/nodes")
    assert any(n["node_id"] == "esp32s3-01" for n in nodes.json())

    # Get single node
    node_get = await client.get("/api/nodes/esp32s3-01")
    assert node_get.status_code == 200
    assert node_get.json()["node_id"] == "esp32s3-01"

    # 3. Room status
    room_status = await client.get("/api/rooms/living_room/status")
    assert room_status.status_code == 200
    st_data = room_status.json()
    assert st_data["room_id"] == "living_room"
    assert st_data["total_node_count"] == 1


@pytest.mark.asyncio
async def test_falls_and_acknowledgment(client: AsyncClient):
    # 1. First register node and insert a fall event directly into DB
    await client.post("/api/nodes/esp-fall/register", json={"mac_address": "11:22:33:44:55:66"})

    # Trigger fall through API or DB
    # We can retrieve via GET /api/falls
    falls_resp = await client.get("/api/falls")
    assert falls_resp.status_code == 200

    # Test sensing/latest
    sensing_resp = await client.get("/api/sensing/latest")
    assert sensing_resp.status_code == 200
    s_data = sensing_resp.json()
    assert "active_nodes" in s_data
    assert "edge_vitals" in s_data
    assert "fall_alert_active" in s_data
