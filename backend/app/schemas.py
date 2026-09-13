"""Pydantic schemas for request and response validation."""
from datetime import datetime
from enum import Enum
from typing import List, Optional, Any, Dict
from pydantic import BaseModel, ConfigDict, Field


class NodeStatus(str, Enum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    DEGRADED = "DEGRADED"
    UNKNOWN = "UNKNOWN"


class FallStatus(str, Enum):
    DETECTED = "detected"
    CLEARED = "cleared"
    ACKNOWLEDGED = "acknowledged"


# --- Node Schemas ---
class NodeBase(BaseModel):
    node_id: str
    room_id: Optional[str] = None
    mac_address: Optional[str] = None
    firmware_version: str = "0.8.12"
    ruview_version: str = "v2655"


class NodeRegisterRequest(BaseModel):
    room_id: Optional[str] = Field(None, description="Room identifier to associate node with")
    mac_address: Optional[str] = Field(None, description="MAC address of the node")
    firmware_version: Optional[str] = Field("0.8.12", description="Firmware version running on node")


class NodeResponse(NodeBase):
    model_config = ConfigDict(from_attributes=True)

    ip_address: Optional[str] = None
    status: NodeStatus = NodeStatus.UNKNOWN
    last_seen: Optional[datetime] = None
    rssi: Optional[int] = None
    csi_fps: Optional[float] = None
    created_at: datetime
    updated_at: datetime


# --- Room Schemas ---
class RoomCreate(BaseModel):
    id: str = Field(..., description="Unique room identifier (e.g. living_room)")
    name: str = Field(..., description="Human readable room name")
    description: Optional[str] = None


class RoomResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: Optional[str] = None
    nodes: List[NodeResponse] = []
    created_at: datetime
    updated_at: datetime


class RoomStatusResponse(BaseModel):
    room_id: str
    room_name: str
    presence_detected: bool
    fall_detected: bool
    active_node_count: int
    total_node_count: int
    persons_estimate: int
    latest_event: Optional[str] = None
    last_updated: datetime


# --- Fall Event Schemas ---
class FallEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_id: str
    type: str = "fall"
    node_id: str
    room_id: Optional[str] = None
    timestamp: datetime
    confidence: Optional[float] = Field(None, description="Confidence score from RuView; None if unavailable")
    status: str = "detected"
    source: str = "ruview"
    motion_energy: Optional[float] = None
    presence_score: Optional[float] = None
    cleared_at: Optional[datetime] = None


class FallEventAcknowledgeRequest(BaseModel):
    notes: Optional[str] = None


# --- Sensing & Telemetry Schemas ---
class EdgeVitalsPayload(BaseModel):
    node_id: str
    presence: bool
    fall_detected: bool
    motion: bool
    breathing_rate_bpm: Optional[float] = None
    heartrate_bpm: Optional[float] = None
    n_persons: int = 0
    motion_energy: float = 0.0
    presence_score: float = 0.0
    rssi: int = 0
    timestamp_ms: int = 0


class SensingLatestResponse(BaseModel):
    timestamp: datetime
    source: str
    active_nodes: List[NodeResponse]
    edge_vitals: Dict[str, EdgeVitalsPayload]
    fall_alert_active: bool
    total_persons_present: int


# --- System Status & Health Schemas ---
class HealthResponse(BaseModel):
    status: str = "healthy"
    version: str
    uptime_seconds: float
    ruview_connected: bool
    udp_listener_active: bool
    online_nodes: int
    total_nodes: int


class SystemStatusResponse(BaseModel):
    status: str
    active_falls: int
    online_nodes: int
    offline_nodes: int
    rooms_monitored: int
    last_fall_event: Optional[FallEventResponse] = None
    last_telemetry_at: Optional[datetime] = None


# --- WebSocket Messages ---
class WebSocketEventMessage(BaseModel):
    type: str  # node_connected, node_disconnected, presence_changed, fall_detected, fall_cleared, sensing_status_changed, system_warning
    timestamp: datetime
    node_id: Optional[str] = None
    room_id: Optional[str] = None
    event_id: Optional[str] = None
    data: Dict[str, Any] = {}
