"""Asyncio UDP datagram ingestion service for ESP32 CSI nodes."""
import asyncio
from datetime import datetime, timezone
import logging
from typing import Optional, Tuple, Callable
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.parser import (
    parse_packet,
    RawCsiFrame,
    EdgeVitalsPacket,
    FeatureVectorPacket,
    TimeSyncPacket,
    PacketParseError,
)
from app.services.node_tracker import node_tracker
from app.services.fall_manager import fall_manager
from app.services.ws_broadcaster import ws_broadcaster

logger = logging.getLogger("ruview.udp_listener")


class CsiDatagramProtocol(asyncio.DatagramProtocol):
    def __init__(self, session_factory: Callable[[], AsyncSession]):
        self.session_factory = session_factory
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.packets_received = 0
        self.parse_errors = 0
        self._prev_presence: dict[str, bool] = {}

    def connection_made(self, transport: asyncio.DatagramTransport):
        self.transport = transport
        logger.info(f"UDP CSI Receiver active on {settings.UDP_BIND_HOST}:{settings.UDP_PORT}")

    def datagram_received(self, data: bytes, addr: Tuple[str, int]):
        self.packets_received += 1
        client_ip, client_port = addr

        try:
            parsed = parse_packet(data)
        except PacketParseError as pe:
            self.parse_errors += 1
            logger.debug(f"Malformed packet from {client_ip}:{client_port}: {pe}")
            return
        except Exception as e:
            self.parse_errors += 1
            logger.warning(f"Unexpected error parsing packet from {client_ip}: {e}")
            return

        asyncio.create_task(self._process_parsed_packet(parsed, client_ip))

    async def _process_parsed_packet(self, parsed: object, client_ip: str):
        try:
            node_id_str = str(getattr(parsed, "node_id", "unknown"))
            tracked = node_tracker.get_node_state(node_id_str)
            room_id = tracked.room_id if tracked else None

            if isinstance(parsed, RawCsiFrame):
                await node_tracker.observe_packet(
                    node_id=node_id_str,
                    packet_type="raw_csi",
                    rssi=parsed.rssi_dbm,
                    ip_address=client_ip,
                    session_factory=self.session_factory,
                )

            elif isinstance(parsed, EdgeVitalsPacket):
                vitals_dict = {
                    "node_id": node_id_str,
                    "presence": parsed.presence,
                    "fall_detected": parsed.fall_detected,
                    "motion": parsed.motion,
                    "breathing_rate_bpm": parsed.breathing_rate_bpm,
                    "heartrate_bpm": parsed.heartrate_bpm,
                    "n_persons": parsed.n_persons,
                    "motion_energy": parsed.motion_energy,
                    "presence_score": parsed.presence_score,
                    "rssi": parsed.rssi_dbm,
                    "timestamp_ms": parsed.timestamp_ms,
                }

                await node_tracker.observe_packet(
                    node_id=node_id_str,
                    packet_type="edge_vitals",
                    rssi=parsed.rssi_dbm,
                    ip_address=client_ip,
                    vitals_dict=vitals_dict,
                    session_factory=self.session_factory,
                )

                # Process fall detection event
                await fall_manager.handle_vitals_sample(
                    node_id=node_id_str,
                    fall_detected=parsed.fall_detected,
                    presence=parsed.presence,
                    room_id=room_id,
                    motion_energy=parsed.motion_energy,
                    presence_score=parsed.presence_score,
                    session_factory=self.session_factory,
                )

                # Presence change broadcast
                prev_pres = self._prev_presence.get(node_id_str, False)
                if prev_pres != parsed.presence:
                    self._prev_presence[node_id_str] = parsed.presence
                    await ws_broadcaster.broadcast(
                        "presence_changed",
                        {
                            "node_id": node_id_str,
                            "room_id": room_id,
                            "presence": parsed.presence,
                            "persons_count": parsed.n_persons,
                            "presence_score": parsed.presence_score,
                        },
                    )

            elif isinstance(parsed, FeatureVectorPacket):
                await node_tracker.observe_packet(
                    node_id=node_id_str,
                    packet_type="feature_vector",
                    ip_address=client_ip,
                    session_factory=self.session_factory,
                )

            elif isinstance(parsed, TimeSyncPacket):
                await node_tracker.observe_packet(
                    node_id=node_id_str,
                    packet_type="time_sync",
                    ip_address=client_ip,
                    session_factory=self.session_factory,
                )

        except Exception as e:
            logger.error(f"Error handling parsed packet: {e}", exc_info=True)

    def error_received(self, exc: Exception):
        logger.warning(f"UDP listener socket error received: {exc}")


class UdpIngestionServer:
    def __init__(self, session_factory: Callable[[], AsyncSession]):
        self.session_factory = session_factory
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.protocol: Optional[CsiDatagramProtocol] = None

    async def start(self):
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: CsiDatagramProtocol(self.session_factory),
            local_addr=(settings.UDP_BIND_HOST, settings.UDP_PORT),
        )
        self.transport = transport
        self.protocol = protocol
        logger.info(f"UDP Ingestion Server started on port {settings.UDP_PORT}")

    def stop(self):
        if self.transport:
            self.transport.close()
            logger.info("UDP Ingestion Server stopped")
