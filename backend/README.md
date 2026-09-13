# RuView Production Fall Detection Backend

Production-ready backend service connecting ESP32-S3 CSI sensor nodes to web and mobile fall-detection dashboards using the open-source **[RuView](https://github.com/ruvnet/RuView)** ecosystem.

---

## 1. Architecture Diagram

```
┌──────────────────────────────────────────────────────────────┐
│                    ESP32-S3 SuperMini                        │
│                 (4 MB Flash, 2.4 GHz Wi-Fi)                  │
│  - WiFi CSI Driver (Core 0)                                  │
│  - Edge DSP Pipeline (Core 1): Phase Accel Fall Detector     │
└──────────────┬───────────────────────────────────────────────┘
               │
               │ 2.4 GHz Wi-Fi (UDP Datagrams)
               ▼
┌──────────────────────────────────────────────────────────────┐
│                   Dual-Mode Ingestion Pipeline               │
│                                                              │
│  Mode 1 (Standalone):      Mode 2 (RuView Rust Bridge):      │
│  Async UDP Receiver        Upstream WebSocket Client         │
│  (Port 5005)               (ws://localhost:8765/ws/sensing)  │
└──────────────┬───────────────────────────────┬───────────────┘
               │                               │
               └───────────────┬───────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────┐
│                  FastAPI Backend Service                     │
│  ┌────────────────────┐ ┌──────────────────────────────────┐ │
│  │ Binary Decoders    │ │ Node Tracker & Heartbeat         │ │
│  │ ADR-018 Raw CSI    │ │ (Online / Offline / Degraded)    │ │
│  │ 0xC5110002 Vitals  │ └──────────────────────────────────┘ │
│  └─────────┬──────────┘ ┌──────────────────────────────────┐ │
│            │            │ Fall Event Manager               │ │
│            ▼            │ (Rising edge, debounce, cooldown)│ │
│  ┌────────────────────┐ └──────────────────────────────────┘ │
│  │ SQLite/PostgreSQL  │ ┌──────────────────────────────────┐ │
│  │ Persistence        │ │ WebSocket Broadcaster (/ws/events│ │
│  └────────────────────┘ └──────────────────────────────────┘ │
└──────────────┬───────────────────────────────┬───────────────┘
               │                               │
               │ HTTP REST (:8000/api)         │ WebSocket (:8000/ws/events)
               ▼                               ▼
┌──────────────────────────────────────────────────────────────┐
│                 Web / Mobile Client Dashboard                │
│  🟢 Person Detected | 🔴 Fall Detected | Node Status Live    │
└──────────────────────────────────────────────────────────────┘
```

---

## 2. Hardware Target

- **Board**: ESP32-S3 SuperMini (or ESP32-S3 DevKitC-1)
- **Flash Variant**: 4 MB Flash (Quad SPI / DIO)
- **Wi-Fi**: 2.4 GHz 802.11b/g/n
- **Host**: Linux / Raspberry Pi / macOS / Windows server

---

## 3. RuView & Firmware Versions

- **RuView Core**: `v2655` (commit `33a9e908`)
- **Firmware Version**: `0.8.12` (source in `ruview/firmware/esp32-csi-node`)
- **Verified Pre-built Binary Bundle**: `v0.8.8` / `v0.6.7` in `release_bins/esp32-csi-node-4mb.bin`

---

## 4. Fall Detection Architecture & Invariants

Fall detection is processed across both the edge device and the host:

1. **Edge Node (`edge_processing.c`)**:
   - Calculates inter-frame phase velocity: $\Delta \phi = \phi(t) - \phi(t-1)$
   - Calculates phase acceleration: $\alpha = |\Delta \phi - \Delta \phi_{prev}|$
   - **Threshold**: `CONFIG_EDGE_FALL_THRESH` = `15000` (15.0 rad/s²)
   - **Debounce**: `EDGE_FALL_CONSEC_MIN` = 3 consecutive frames
   - **Cooldown**: `EDGE_FALL_COOLDOWN_MS` = 5000 ms between alerts
   - Emits 32-byte UDP packet (`0xC5110002`) with `flags |= 0x02` (Bit 1)
2. **Backend Service**:
   - Performs rising-edge detection (`!prev_fall && fall_detected`)
   - Re-asserts a 5000 ms cooldown window to eliminate alert storms
   - Records the event into the database with `confidence: None` (truthful to repository: RuView does not fabricate an arbitrary float confidence score)
   - Dispatches `fall_detected` push events over `/ws/events`
   - Automatically handles fall clearance after 5 consecutive normal frames

---

## 5. ESP32-S3 4 MB Flash Instructions

### 5.1 Flash Partition Table (`partitions_4mb.csv`)
```csv
# Name,      Type, SubType, Offset,   Size,      Flags
nvs,         data, nvs,     0x9000,   0x6000,
otadata,     data, ota,     0xF000,   0x2000,
phy_init,    data, phy,     0x11000,  0x1000,
ota_0,       app,  ota_0,   0x20000,  0x1D0000,
ota_1,       app,  ota_1,   0x1F0000, 0x1D0000,
```

### 5.2 Flashing Command (esptool)
Using the pre-built 4 MB binaries located in `ruview/firmware/esp32-csi-node/release_bins`:

```bash
python -m esptool --chip esp32s3 --port COM7 --baud 460800 \
  write_flash --flash_mode dio --flash_size 4MB \
  0x0     ruview/firmware/esp32-csi-node/release_bins/bootloader.bin \
  0x8000  ruview/firmware/esp32-csi-node/release_bins/partition-table-4mb.bin \
  0xf000  ruview/firmware/esp32-csi-node/release_bins/ota_data_initial.bin \
  0x20000 ruview/firmware/esp32-csi-node/release_bins/esp32-csi-node-4mb.bin
```

### 5.3 Building from Source (Docker / ESP-IDF v5.4)
```bash
docker run --rm -v "$(pwd)/ruview/firmware/esp32-csi-node:/project" -w /project \
  espressif/idf:v5.4 bash -c \
  "rm -rf build sdkconfig && idf.py -D SDKCONFIG_DEFAULTS=\"sdkconfig.defaults;sdkconfig.defaults.4mb\" set-target esp32s3 && idf.py build"
```

---

## 6. Node Wi-Fi & Target Provisioning

Use the official RuView provisioning tool (`provision.py`) to write credentials and backend destination to the NVS partition:

```bash
python ruview/firmware/esp32-csi-node/provision.py \
  --port COM7 \
  --chip esp32s3 \
  --ssid "YourWiFiSSID" \
  --password "YourWiFiPassword" \
  --target-ip 192.168.1.50 \
  --target-port 5005 \
  --node-id 1 \
  --edge-tier 2 \
  --fall-thresh 15.0
```

---

## 7. Backend Installation & Run

### 7.1 Local Python Environment
```bash
cd backend
python -m venv .venv
source .venv/bin/activate  # Or on Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 7.2 Configuration (`.env`)
```env
HOST=0.0.0.0
PORT=8000
UDP_ENABLED=true
UDP_PORT=5005
DATABASE_URL=sqlite+aiosqlite:///./fall_detection.db
NODE_OFFLINE_TIMEOUT_S=5.0
FALL_COOLDOWN_MS=5000
```

### 7.3 Run Backend
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```
Interactive OpenAPI documentation will be available at: `http://localhost:8000/docs`.

### 7.4 Run with Docker Compose
```bash
docker compose up -d
```

---

## 8. REST API Documentation

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health` | Service health, uptime, and node counts |
| `GET` | `/api/system/status` | Global operational overview and active alerts |
| `GET` | `/api/nodes` | List all nodes with connection status and metrics |
| `GET` | `/api/nodes/{node_id}` | Detailed status for a specific node |
| `POST`| `/api/nodes/{node_id}/register` | Register node and assign to a room |
| `GET` | `/api/rooms` | List all monitored rooms |
| `POST`| `/api/rooms` | Create a new room |
| `GET` | `/api/rooms/{room_id}/status` | Real-time room occupancy and fall alert status |
| `GET` | `/api/falls` | Paginated fall events with status filters |
| `GET` | `/api/falls/{event_id}` | Detailed event record |
| `POST`| `/api/falls/{event_id}/acknowledge` | Acknowledge and clear an alert |
| `GET` | `/api/sensing/latest` | Real-time aggregate sensing state (presence, vitals) |

---

## 9. WebSocket API (`/ws/events`)

Connect to:
```
ws://localhost:8000/ws/events
```

### Published Event Types

1. **`fall_detected`**:
```json
{
  "type": "fall_detected",
  "timestamp": "2026-09-13T10:30:00Z",
  "event_id": "3c6de57b-03c0-4300-a6b5-0cdaf9a2ce91",
  "node_id": "1",
  "room_id": "living_room",
  "confidence": null,
  "status": "detected",
  "source": "ruview",
  "motion_energy": 1.45,
  "presence_score": 5.8
}
```

2. **`fall_cleared`**:
```json
{
  "type": "fall_cleared",
  "timestamp": "2026-09-13T10:30:15Z",
  "event_id": "3c6de57b-03c0-4300-a6b5-0cdaf9a2ce91",
  "node_id": "1",
  "room_id": "living_room",
  "status": "cleared"
}
```

3. **`presence_changed`**:
```json
{
  "type": "presence_changed",
  "timestamp": "2026-09-13T10:30:00Z",
  "node_id": "1",
  "room_id": "living_room",
  "presence": true,
  "persons_count": 1,
  "presence_score": 4.2
}
```

4. **`node_connected` / `node_disconnected`**:
```json
{
  "type": "node_connected",
  "timestamp": "2026-09-13T10:29:50Z",
  "node_id": "1",
  "room_id": "living_room",
  "rssi": -52
}
```

---

## 10. Testing & Verification

### 10.1 Running Automated Unit & Integration Tests
```bash
cd backend
python -m pytest tests -v
```
All 14 tests cover:
- Bit-exact packet parsers (`0xC5110001`, `0xC5110002`, `0xC5110003`, `0xC511A110`)
- Malformed packet drop safety
- Fall manager rising-edge detection and cooldown
- Node tracker heartbeat timeout and fps EMA
- REST endpoints and WebSocket broadcasting

### 10.2 Live End-to-End Pipeline Verification
```bash
cd backend
python scripts/verify_pipeline.py
```
Exercises the entire chain: UDP datagram transmission $\rightarrow$ bit-exact parsing $\rightarrow$ node online transition $\rightarrow$ fall event detection $\rightarrow$ database commit $\rightarrow$ WebSocket push event $\rightarrow$ REST acknowledgment.

### 10.3 Simulating Hardware Stream
```bash
# Stream continuous CSI (20 Hz) and vitals (1 Hz)
python scripts/sim_esp32_stream.py --target-ip 127.0.0.1 --target-port 5005 --node-id 1

# Trigger an immediate simulated fall sequence
python scripts/sim_esp32_stream.py --trigger-fall --node-id 1
```

---

## 11. Troubleshooting

- **Firewall blocking UDP 5005**:
  - Windows: `netsh advfirewall firewall add rule name="ESP32 CSI" dir=in action=allow protocol=UDP localport=5005`
  - Linux: `sudo ufw allow 5005/udp`
- **Node appears OFFLINE**:
  - Verify node is associated to the same 2.4 GHz subnet.
  - Check serial monitor at 115200 baud for `UDP sender initialized: <target_ip>:5005`.
- **4 MB Flash Bootloop**:
  - Ensure `partitions_4mb.csv` was used and `CONFIG_ESPTOOLPY_FLASHSIZE_4MB=y` is set.
  - Disable display support (`# CONFIG_DISPLAY_ENABLE is not set`).
