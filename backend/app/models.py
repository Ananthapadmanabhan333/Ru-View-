"""SQLAlchemy database models for RuView Fall Detection System."""
from datetime import datetime, timezone
import uuid
from sqlalchemy import (
    Column,
    String,
    Integer,
    Float,
    DateTime,
    ForeignKey,
    Text,
)
from sqlalchemy.orm import relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Room(Base):
    __tablename__ = "rooms"

    id = Column(String(64), primary_key=True, index=True)
    name = Column(String(128), nullable=False)
    description = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    nodes = relationship("Node", back_populates="room", cascade="all, delete-orphan")


class Node(Base):
    __tablename__ = "nodes"

    node_id = Column(String(64), primary_key=True, index=True)
    board_type = Column(String(32), default="esp32s3_n16r8", nullable=False)
    mac_address = Column(String(32), nullable=True)
    ip_address = Column(String(64), nullable=True)
    room_id = Column(String(64), ForeignKey("rooms.id", ondelete="SET NULL"), nullable=True)
    firmware_version = Column(String(32), default="0.8.12", nullable=False)
    ruview_version = Column(String(32), default="v2655", nullable=False)
    status = Column(String(32), default="UNKNOWN", nullable=False)  # ONLINE, OFFLINE, DEGRADED, UNKNOWN
    last_seen = Column(DateTime(timezone=True), nullable=True)
    rssi = Column(Integer, nullable=True)
    csi_fps = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    room = relationship("Room", back_populates="nodes")
    falls = relationship("FallEvent", back_populates="node")


class FallEvent(Base):
    __tablename__ = "fall_events"

    event_id = Column(String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    type = Column(String(32), default="fall", nullable=False)
    node_id = Column(String(64), ForeignKey("nodes.node_id"), nullable=False, index=True)
    room_id = Column(String(64), nullable=True, index=True)
    timestamp = Column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    confidence = Column(Float, nullable=True)  # Nullable: never fabricate if unavailable
    status = Column(String(32), default="detected", nullable=False)  # detected, cleared, acknowledged
    source = Column(String(32), default="ruview", nullable=False)
    motion_energy = Column(Float, nullable=True)
    presence_score = Column(Float, nullable=True)
    cleared_at = Column(DateTime(timezone=True), nullable=True)

    node = relationship("Node", back_populates="falls")


class SystemEvent(Base):
    __tablename__ = "system_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    event_type = Column(String(64), nullable=False, index=True)
    node_id = Column(String(64), nullable=True)
    room_id = Column(String(64), nullable=True)
    payload_json = Column(Text, nullable=True)
