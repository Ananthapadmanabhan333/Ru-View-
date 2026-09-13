#!/usr/bin/env python3
"""ESP32-S3 CSI and Vitals packet simulator.

Sends genuine bit-exact RuView binary packets over UDP to test the backend pipeline:
- ADR-018 raw CSI frames (magic 0xC5110001) at 20 Hz
- ADR-039 edge vitals (magic 0xC5110002) at 1 Hz
- Controlled fall trigger (flags |= 0x02) for testing alerts and WebSockets

Usage:
    python sim_esp32_stream.py --target-ip 127.0.0.1 --target-port 5005 --node-id 1
    python sim_esp32_stream.py --trigger-fall
"""
import argparse
import math
import socket
import struct
import sys
import time

MAGIC_RAW_CSI = 0xC5110001
MAGIC_EDGE_VITALS = 0xC5110002


def build_raw_csi_frame(
    node_id: int,
    seq: int,
    rssi: int = -52,
    n_antennas: int = 1,
    n_subcarriers: int = 56,
) -> bytes:
    """Build ADR-018 raw CSI packet."""
    header = struct.pack(
        "<IBBHIIBBBB",
        MAGIC_RAW_CSI,
        node_id,
        n_antennas,
        n_subcarriers,
        2437,  # Channel 6 (2437 MHz)
        seq,
        rssi & 0xFF,
        (-92) & 0xFF,
        1,     # HE-SU
        0x10,  # sync valid
    )

    # Simulated I/Q carrier pairs (sine perturbation)
    iq = bytearray()
    t = seq * 0.05
    for sc in range(n_subcarriers):
        i_val = int(25.0 * math.sin(t + sc * 0.1))
        q_val = int(25.0 * math.cos(t + sc * 0.1))
        iq.extend(struct.pack("<bb", i_val, q_val))

    return header + bytes(iq)


def build_edge_vitals_packet(
    node_id: int,
    presence: bool = True,
    fall_detected: bool = False,
    motion: bool = True,
    breathing_bpm: float = 16.5,
    heartrate_bpm: float = 72.0,
    rssi: int = -52,
    n_persons: int = 1,
    motion_energy: float = 0.35,
    presence_score: float = 4.2,
) -> bytes:
    """Build 32-byte ADR-039 edge vitals packet."""
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
    timestamp_ms = int(time.time() * 1000) & 0xFFFFFFFF

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
        0,
    )


def main():
    parser = argparse.ArgumentParser(description="Simulate RuView ESP32-S3 UDP Stream")
    parser.add_argument("--target-ip", default="127.0.0.1", help="Backend UDP IP")
    parser.add_argument("--target-port", type=int, default=5005, help="Backend UDP Port")
    parser.add_argument("--node-id", type=int, default=1, help="Simulated Node ID")
    parser.add_argument("--trigger-fall", action="store_true", help="Trigger immediate fall event")
    parser.add_argument("--duration", type=int, default=0, help="Run duration in seconds (0 = forever)")
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dest = (args.target_ip, args.target_port)

    print(f"[*] Simulating ESP32-S3 Node {args.node_id} -> UDP {dest[0]}:{dest[1]}")

    if args.trigger_fall:
        print("[!] Sending simulated FALL event (3 consecutive frames)...")
        for i in range(3):
            pkt = build_edge_vitals_packet(
                node_id=args.node_id,
                presence=True,
                fall_detected=True,
                motion=True,
                motion_energy=1.85,
                presence_score=8.5,
            )
            sock.sendto(pkt, dest)
            time.sleep(0.05)
        print("[+] Fall packet sequence sent.")
        return

    seq = 0
    start_time = time.time()
    last_vitals_time = 0.0

    print("[*] Streaming raw CSI (20 Hz) and vitals (1 Hz). Press Ctrl+C to stop.")

    try:
        while True:
            now = time.time()
            if args.duration > 0 and (now - start_time) > args.duration:
                break

            # Send raw CSI frame (~20 Hz)
            raw_frame = build_raw_csi_frame(node_id=args.node_id, seq=seq)
            sock.sendto(raw_frame, dest)
            seq += 1

            # Send edge vitals every 1.0s (1 Hz)
            if now - last_vitals_time >= 1.0:
                vitals = build_edge_vitals_packet(
                    node_id=args.node_id,
                    presence=True,
                    fall_detected=False,
                    motion=True,
                    breathing_bpm=16.5 + math.sin(seq * 0.1) * 1.5,
                    heartrate_bpm=72.0 + math.cos(seq * 0.1) * 3.0,
                    rssi=-50 - (seq % 8),
                    n_persons=1,
                )
                sock.sendto(vitals, dest)
                last_vitals_time = now
                print(f"[>] Vitals sent (Node {args.node_id}, Seq {seq})")

            time.sleep(0.05)  # 20 Hz cadence
    except KeyboardInterrupt:
        print("\n[*] Stopping simulation.")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
