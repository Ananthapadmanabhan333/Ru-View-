"""Unit tests for binary CSI and Vitals packet parsing."""
import struct
import pytest
from app.parser import (
    parse_packet,
    parse_edge_vitals,
    parse_raw_csi,
    parse_feature_vector,
    parse_time_sync,
    MAGIC_RAW_CSI,
    MAGIC_EDGE_VITALS,
    MAGIC_FEATURE_VECTOR,
    MAGIC_FUSED_VITALS,
    MAGIC_TIME_SYNC,
    PacketParseError,
    sanitize_edge_person_count,
)


def build_edge_vitals_packet(
    node_id: int = 1,
    presence: bool = True,
    fall_detected: bool = False,
    motion: bool = True,
    breathing_bpm: float = 16.5,
    heartrate_bpm: float = 72.0,
    rssi: int = -50,
    n_persons: int = 1,
    motion_energy: float = 0.35,
    presence_score: float = 4.2,
    timestamp_ms: int = 123456,
) -> bytes:
    flags = 0
    if presence:
        flags |= 0x01
    if fall_detected:
        flags |= 0x02
    if motion:
        flags |= 0x04

    breathing_raw = int(breathing_bpm * 100)
    heartrate_raw = int(heartrate_bpm * 10000)
    rssi_u8 = rssi & 0xFF

    return struct.pack(
        "<IBBHIBB2xffII",
        MAGIC_EDGE_VITALS,
        node_id,
        flags,
        breathing_raw,
        heartrate_raw,
        rssi_u8,
        n_persons,
        motion_energy,
        presence_score,
        timestamp_ms,
        0,  # reserved2
    )


def build_raw_csi_packet(
    node_id: int = 1,
    n_antennas: int = 1,
    n_subcarriers: int = 4,
    freq_mhz: int = 2437,
    sequence: int = 42,
    rssi: int = -55,
    noise_floor: int = -90,
    ppdu_byte: int = 1,  # he_su
    flags_byte: int = 0x10,  # sync_valid
    iq_pairs=None,
) -> bytes:
    if iq_pairs is None:
        iq_pairs = [(10, 20), (-5, 15), (0, -10), (12, -8)]

    rssi_u8 = rssi & 0xFF
    noise_u8 = noise_floor & 0xFF

    header = struct.pack(
        "<IBBHIIBBBB",
        MAGIC_RAW_CSI,
        node_id,
        n_antennas,
        n_subcarriers,
        freq_mhz,
        sequence,
        rssi_u8,
        noise_u8,
        ppdu_byte,
        flags_byte,
    )

    iq_bytes = bytearray()
    for i_val, q_val in iq_pairs:
        iq_bytes.extend(struct.pack("<bb", i_val, q_val))

    return header + bytes(iq_bytes)


class TestPacketParser:
    def test_parse_vitals_normal(self):
        pkt_bytes = build_edge_vitals_packet(
            node_id=2,
            presence=True,
            fall_detected=False,
            breathing_bpm=18.0,
            heartrate_bpm=75.5,
            rssi=-60,
        )
        parsed = parse_packet(pkt_bytes)
        assert parsed.magic == MAGIC_EDGE_VITALS
        assert parsed.node_id == 2
        assert parsed.presence is True
        assert parsed.fall_detected is False
        assert parsed.breathing_rate_bpm == 18.0
        assert parsed.heartrate_bpm == 75.5
        assert parsed.rssi_dbm == -60

    def test_parse_vitals_fall_detected(self):
        pkt_bytes = build_edge_vitals_packet(
            node_id=3,
            presence=True,
            fall_detected=True,
            motion=True,
        )
        parsed = parse_packet(pkt_bytes)
        assert parsed.node_id == 3
        assert parsed.fall_detected is True
        assert parsed.presence is True

    def test_sanitize_edge_person_count(self):
        # When presence is false, person count must be clamped to 0
        count, valid = sanitize_edge_person_count(False, 4)
        assert count == 0
        assert valid is False

        # When presence is true, valid count up to 4 is preserved
        count, valid = sanitize_edge_person_count(True, 3)
        assert count == 3
        assert valid is True

        # Out-of-bounds count (>4) fails closed
        count, valid = sanitize_edge_person_count(True, 6)
        assert count == 0
        assert valid is False

    def test_parse_raw_csi(self):
        pkt_bytes = build_raw_csi_packet(node_id=5, n_antennas=1, n_subcarriers=4, sequence=100)
        parsed = parse_packet(pkt_bytes)
        assert parsed.magic == MAGIC_RAW_CSI
        assert parsed.node_id == 5
        assert parsed.sequence == 100
        assert parsed.n_subcarriers == 4
        assert parsed.ppdu_type == "he_su"
        assert parsed.he_capable is True
        assert parsed.sync_valid is True
        assert len(parsed.amplitudes) == 4
        assert len(parsed.phases) == 4

    def test_parse_time_sync(self):
        buf = struct.pack(
            "<IBBBBQQI4x",
            MAGIC_TIME_SYNC,
            7,      # node_id
            1,      # proto_ver
            0x03,   # flags: leader + valid
            0,      # reserved
            1000000,
            1000005,
            50,     # sequence
        )
        parsed = parse_packet(buf)
        assert parsed.magic == MAGIC_TIME_SYNC
        assert parsed.node_id == 7
        assert parsed.is_leader is True
        assert parsed.is_valid is True
        assert parsed.local_us == 1000000
        assert parsed.epoch_us == 1000005
        assert parsed.sequence == 50

    def test_parse_feature_vector(self):
        buf = struct.pack(
            "<IBBHq8f",
            MAGIC_FEATURE_VECTOR,
            9,  # node_id
            0,  # reserved
            12, # seq
            500000,
            1.0, 0.5, 0.6, 0.7, 0.1, 0.25, 1.0, 0.8,
        )
        parsed = parse_packet(buf)
        assert parsed.magic == MAGIC_FEATURE_VECTOR
        assert parsed.node_id == 9
        assert parsed.seq == 12
        assert parsed.fall_risk == 1.0

    def test_malformed_packets(self):
        with pytest.raises(PacketParseError):
            parse_packet(b"")

        with pytest.raises(PacketParseError):
            parse_packet(b"\x00\x00\x00\x00")

        with pytest.raises(PacketParseError):
            # 0xC5110002 but too short (<32 bytes)
            parse_packet(struct.pack("<I", MAGIC_EDGE_VITALS) + b"\x00" * 10)
