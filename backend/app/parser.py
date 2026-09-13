"""Bit-exact binary packet parsers for RuView ESP32 CSI protocols.

Implements deserialization for:
- ADR-018 Raw CSI frames (magic 0xC5110001)
- ADR-039 Edge Vitals packet (magic 0xC5110002, 32 bytes)
- ADR-069 Feature Vector packet (magic 0xC5110003, 48 bytes)
- ADR-063 Fused Vitals packet (magic 0xC5110004, 48 bytes)
- ADR-110 Time Sync packet (magic 0xC511A110, 32 bytes)
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import struct
from typing import Optional, List, Tuple, Any, Dict


# Wire Magics
MAGIC_RAW_CSI = 0xC5110001
MAGIC_EDGE_VITALS = 0xC5110002
MAGIC_FEATURE_VECTOR = 0xC5110003
MAGIC_FUSED_VITALS = 0xC5110004
MAGIC_TIME_SYNC = 0xC511A110

PPDU_TYPES = {
    0: "ht_legacy",
    1: "he_su",
    2: "he_mu",
    3: "he_tb",
}


class PacketParseError(Exception):
    """Raised when incoming packet data does not match the wire protocol."""
    pass


@dataclass
class RawCsiFrame:
    magic: int
    node_id: int
    n_antennas: int
    n_subcarriers: int
    freq_mhz: int
    sequence: int
    rssi_dbm: int
    noise_floor_dbm: int
    ppdu_type: str
    he_capable: bool
    bw40: bool
    stbc: bool
    ldpc: bool
    sync_valid: bool
    amplitudes: List[float]
    phases: List[float]
    received_at: datetime


@dataclass
class EdgeVitalsPacket:
    magic: int
    node_id: int
    presence: bool
    fall_detected: bool
    motion: bool
    breathing_rate_bpm: float
    heartrate_bpm: float
    rssi_dbm: int
    n_persons: int
    person_count_valid: bool
    motion_energy: float
    presence_score: float
    timestamp_ms: int
    received_at: datetime


@dataclass
class FeatureVectorPacket:
    magic: int
    node_id: int
    seq: int
    timestamp_us: int
    features: List[float]
    fall_risk: float
    received_at: datetime


@dataclass
class TimeSyncPacket:
    magic: int
    node_id: int
    proto_ver: int
    is_leader: bool
    is_valid: bool
    smoothed_used: bool
    local_us: int
    epoch_us: int
    sequence: int
    received_at: datetime


def sanitize_edge_person_count(presence: bool, raw_count: int) -> Tuple[int, bool]:
    """Match RuView invariant: person count is invalid/zero if presence is false."""
    if raw_count > 4 or (not presence and raw_count != 0):
        return 0, False
    return raw_count, True


def parse_packet(buf: bytes, received_at: Optional[datetime] = None) -> Any:
    """Dispatches packet by leading 4-byte little-endian magic."""
    if len(buf) < 4:
        raise PacketParseError(f"Buffer too short ({len(buf)} bytes)")

    if received_at is None:
        received_at = datetime.now(timezone.utc)

    magic = struct.unpack_from("<I", buf, 0)[0]

    if magic == MAGIC_EDGE_VITALS:
        return parse_edge_vitals(buf, received_at)
    elif magic == MAGIC_RAW_CSI:
        return parse_raw_csi(buf, received_at)
    elif magic == MAGIC_FEATURE_VECTOR:
        return parse_feature_vector(buf, received_at)
    elif magic == MAGIC_FUSED_VITALS:
        return parse_fused_vitals(buf, received_at)
    elif magic == MAGIC_TIME_SYNC:
        return parse_time_sync(buf, received_at)
    else:
        raise PacketParseError(f"Unknown packet magic: 0x{magic:08X}")


def parse_edge_vitals(buf: bytes, received_at: datetime) -> EdgeVitalsPacket:
    """Parse 32-byte ADR-039 Edge Vitals packet (magic 0xC5110002)."""
    if len(buf) < 32:
        raise PacketParseError(f"Edge vitals packet too short: {len(buf)} bytes, expected 32")

    magic, node_id, flags, breathing_raw, heartrate_raw, rssi_u8, n_persons_raw = struct.unpack_from(
        "<IBBHIBB", buf, 0
    )

    rssi = rssi_u8 if rssi_u8 < 128 else rssi_u8 - 256
    presence = (flags & 0x01) != 0
    fall_detected = (flags & 0x02) != 0
    motion = (flags & 0x04) != 0

    motion_energy, presence_score, timestamp_ms = struct.unpack_from("<ffI", buf, 16)

    n_persons, person_count_valid = sanitize_edge_person_count(presence, n_persons_raw)

    return EdgeVitalsPacket(
        magic=magic,
        node_id=node_id,
        presence=presence,
        fall_detected=fall_detected,
        motion=motion,
        breathing_rate_bpm=round(breathing_raw / 100.0, 2),
        heartrate_bpm=round(heartrate_raw / 10000.0, 2),
        rssi_dbm=rssi,
        n_persons=n_persons,
        person_count_valid=person_count_valid,
        motion_energy=motion_energy,
        presence_score=presence_score,
        timestamp_ms=timestamp_ms,
        received_at=received_at,
    )


def parse_fused_vitals(buf: bytes, received_at: datetime) -> EdgeVitalsPacket:
    """Parse 48-byte ADR-063 Fused Vitals packet (magic 0xC5110004)."""
    if len(buf) < 48:
        raise PacketParseError(f"Fused vitals packet too short: {len(buf)} bytes, expected 48")
    # First 32 bytes share layout with standard vitals packet
    return parse_edge_vitals(buf[:32], received_at)


def parse_raw_csi(buf: bytes, received_at: datetime) -> RawCsiFrame:
    """Parse ADR-018 raw CSI frame (magic 0xC5110001)."""
    if len(buf) < 20:
        raise PacketParseError(f"Raw CSI header too short: {len(buf)} bytes, expected at least 20")

    magic, node_id, n_antennas, n_subcarriers, freq_mhz, sequence, rssi_u8, noise_u8, ppdu_byte, flags_byte = (
        struct.unpack_from("<IBBHIIBBBB", buf, 0)
    )

    rssi = rssi_u8 if rssi_u8 < 128 else rssi_u8 - 256
    noise_floor = noise_u8 if noise_u8 < 128 else noise_u8 - 256

    n_pairs = n_antennas * n_subcarriers
    expected_len = 20 + n_pairs * 2
    if len(buf) < expected_len:
        raise PacketParseError(f"Raw CSI payload too short: {len(buf)} bytes, expected {expected_len}")

    iq_raw = struct.unpack_from(f"<{n_pairs * 2}b", buf, 20)
    amplitudes = []
    phases = []
    for k in range(n_pairs):
        i_val = float(iq_raw[k * 2])
        q_val = float(iq_raw[k * 2 + 1])
        amplitudes.append(math.sqrt(i_val * i_val + q_val * q_val))
        phases.append(math.atan2(q_val, i_val))

    return RawCsiFrame(
        magic=magic,
        node_id=node_id,
        n_antennas=n_antennas,
        n_subcarriers=n_subcarriers,
        freq_mhz=freq_mhz,
        sequence=sequence,
        rssi_dbm=rssi,
        noise_floor_dbm=noise_floor,
        ppdu_type=PPDU_TYPES.get(ppdu_byte, "unknown"),
        he_capable=ppdu_byte in (1, 2, 3),
        bw40=bool(flags_byte & 0x01),
        stbc=bool(flags_byte & 0x04),
        ldpc=bool(flags_byte & 0x08),
        sync_valid=bool(flags_byte & 0x10),
        amplitudes=amplitudes,
        phases=phases,
        received_at=received_at,
    )


def parse_feature_vector(buf: bytes, received_at: datetime) -> FeatureVectorPacket:
    """Parse 48-byte ADR-069 feature vector packet (magic 0xC5110003)."""
    if len(buf) < 48:
        raise PacketParseError(f"Feature vector packet too short: {len(buf)} bytes, expected 48")

    magic, node_id, reserved, seq, timestamp_us = struct.unpack_from("<IBBHq", buf, 0)
    features = list(struct.unpack_from("<8f", buf, 16))
    fall_risk = features[6] if len(features) > 6 else 0.0

    return FeatureVectorPacket(
        magic=magic,
        node_id=node_id,
        seq=seq,
        timestamp_us=timestamp_us,
        features=features,
        fall_risk=fall_risk,
        received_at=received_at,
    )


def parse_time_sync(buf: bytes, received_at: datetime) -> TimeSyncPacket:
    """Parse 32-byte ADR-110 sync packet (magic 0xC511A110)."""
    if len(buf) < 32:
        raise PacketParseError(f"Time sync packet too short: {len(buf)} bytes, expected 32")

    magic, node_id, proto_ver, flags_byte, _, local_us, epoch_us, seq = struct.unpack_from(
        "<IBBBBQQI", buf, 0
    )

    return TimeSyncPacket(
        magic=magic,
        node_id=node_id,
        proto_ver=proto_ver,
        is_leader=bool(flags_byte & 0x01),
        is_valid=bool(flags_byte & 0x02),
        smoothed_used=bool(flags_byte & 0x04),
        local_us=local_us,
        epoch_us=epoch_us,
        sequence=seq,
        received_at=received_at,
    )
