# Real-Time ESP32-S3 Wi-Fi Fall Detection System (Powered by RuView)

A camera-free, real-time fall detection system connecting **ESP32-S3 CSI sensor nodes** to web and mobile monitoring dashboards through the open-source **[RuView](https://github.com/ruvnet/RuView)** ecosystem.

---

## 1. System Architecture

```
ESP32-S3 N16R8 (16 MB Flash, 8 MB Octal PSRAM, GPIO 48 RGB LED)
   │
   ▼  2.4 GHz Wi-Fi (UDP Datagrams / Port 5005 @ 40-50 Hz)
Dual-Mode Ingestion Pipeline
   ├─► Mode 1: Direct Async UDP Ingestion (Lean Edge Mode)
   └─► Mode 2: Upstream RuView Rust Sensing Server Bridge (:8080 / :8765)
   │
   ▼
FastAPI Backend Service
   ├─► Bit-Exact Parsers (ADR-018 raw CSI 0xC5110001, Vitals 0xC5110002)
   ├─► Node Tracker & Heartbeat Monitor (5s timeout)
   ├─► Fall Manager (Rising edge, debounce >= 3 frames, 5s cooldown)
   ├─► SQLAlchemy Persistence (SQLite / PostgreSQL)
   └─► Real-Time WebSocket Broadcaster (/ws/events)
   │
   ▼
Web & Mobile Dashboard
   🟢 Person Detected | 🔴 Fall Detected | Node Status Live
```

---

## 2. Hardware Specification

- **Node**: ESP32-S3 N16R8 (DevKitC-1 v1.1 or compatible N16R8 module)
- **Flash Variant**: 16 MB Flash (`N16`)
- **PSRAM Variant**: 8 MB Octal PSRAM (`R8` / OPI mode)
- **Status Indicator**: Onboard WS2812 RGB LED on **GPIO 48**
- **Wi-Fi**: 2.4 GHz 802.11b/g/n (56 subcarriers, 40–50 fps stream rate enabled by 8 MB PSRAM)
- **Host**: Linux / Raspberry Pi / macOS / Windows Server

---

## 3. RuView Repository & Firmware Details

- **RuView Core**: `v2655` (commit `33a9e908`)
- **Firmware Version**: `0.8.12` (`firmware/esp32-csi-node`)
- **Firmware Binary**: `ruview/firmware/esp32-csi-node/release_bins/esp32-csi-node.bin` (1.12 MB full feature build)
- **Partition Table**: `ruview/firmware/esp32-csi-node/partitions_16mb.csv` (two 4MB OTA slots, 64KB coredump, 8MB FAT storage)

### Flashing 16 MB ESP32-S3 N16R8:
```bash
python -m esptool --chip esp32s3 --port COM7 --baud 460800 \
  write_flash --flash_mode dio --flash_size 16MB \
  0x0     ruview/firmware/esp32-csi-node/release_bins/bootloader.bin \
  0x8000  ruview/firmware/esp32-csi-node/release_bins/partition-table.bin \
  0xf000  ruview/firmware/esp32-csi-node/release_bins/ota_data_initial.bin \
  0x20000 ruview/firmware/esp32-csi-node/release_bins/esp32-csi-node.bin
```

### Provisioning NVS (Wi-Fi, 40-50Hz CSI & Target IP):
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
  --vital-int 1000 \
  --subk-count 56 \
  --fall-thresh 15.0
```

---

## 4. Quick Start (Backend)

### Run with Docker Compose:
```bash
cd backend
docker compose up -d
```

### Run Locally:
```bash
cd backend
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

The backend exposes:
- **REST API & Swagger Docs**: `http://localhost:8000/docs`
- **Real-Time WebSocket**: `ws://localhost:8000/ws/events`
- **UDP CSI Receiver**: Port `5005`

---

## 5. Testing & Verification

Run automated unit and integration tests:
```bash
cd backend
python -m pytest tests -v
```

Run end-to-end simulated pipeline test:
```bash
cd backend
python scripts/verify_pipeline.py
```

Stream simulated ESP32 CSI frames:
```bash
python backend/scripts/sim_esp32_stream.py --target-ip 127.0.0.1 --target-port 5005 --node-id 1 --board esp32s3_n16r8
```

---

## 6. Repository Layout

- `backend/`: Production FastAPI service, database models, wire parsers, services, routers, and test suite.
- `ruview/`: Cloned upstream RuView repository (firmware, DSP pipeline, sensing server).
- `README.md`: System overview and provisioning guide.
