# Zewail City of Science, Technology and Innovation

## University of Science and Technology

### School of Computational Sciences and Artificial Intelligence

### SWAPD453 — Spring 2026

### IoT App Devs

---

## Distributed Intelligent Campus IoT Environment

### Phase 1: Concurrent Simulation & Modeling

---

| | Name | ID |
|---|---|---|
| Ahmed Khaled |202201755 |

**Date:** March 2026

---

<div style="page-break-after: always;"></div>

## Table of Contents

1. [Introduction](#1-introduction)
2. [System Architecture](#2-system-architecture)
3. [Structural Hierarchy & Room Model](#3-structural-hierarchy--room-model)
4. [Deterministic Physics Engine](#4-deterministic-physics-engine)
5. [Fault Injection System](#5-fault-injection-system)
6. [High-Concurrency Engine (asyncio)](#6-high-concurrency-engine-asyncio)
7. [MQTT Infrastructure & JSON Schemas](#7-mqtt-infrastructure--json-schemas)
8. [State Persistence (SQLite)](#8-state-persistence-sqlite)
9. [Fleet Health & Heartbeat Monitoring](#9-fleet-health--heartbeat-monitoring)
10. [Configuration System](#10-configuration-system)
11. [Wokwi Hardware POC](#11-wokwi-hardware-poc)
12. [Dockerization & Deployment](#12-dockerization--deployment)
13. [Performance Results](#13-performance-results)
14. [How to Run](#14-how-to-run)
15. [Conclusion](#15-conclusion)

---

## 1. Introduction

Phase 1 delivers the **World Engine**: a high-concurrency Python `asyncio` simulation that models a 200-room campus building. Each room is an independent IoT node that tracks temperature, humidity, occupancy, ambient light, HVAC status, and smart lighting. The engine publishes structured JSON telemetry to an MQTT broker every 5 seconds, persists room state to SQLite for crash recovery, injects configurable sensor faults, and monitors fleet health via heartbeats.

A companion **Wokwi ESP32 MicroPython POC** validates the telemetry schema and sensor-reading logic on simulated hardware (DHT22, PIR, LDR).

### Key Deliverables

| Deliverable | Status |
|---|---|
| World Engine (200 rooms, asyncio) | Complete |
| Deterministic physics model | Complete |
| 4 fault types (drift, frozen, delay, dropout) | Complete |
| MQTT telemetry + heartbeat + commands | Complete |
| SQLite persistence with crash recovery | Complete |
| Fleet health monitoring | Complete |
| Config-driven (YAML + env overrides) | Complete |
| Docker Compose (Engine + Mosquitto) | Complete |
| Wokwi ESP32 reference room | Complete |
| Performance benchmark script | Complete |

---

## 2. System Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Docker Compose                        │
│                                                         │
│  ┌──────────────┐       ┌──────────────────────────┐   │
│  │  Mosquitto   │◄─────►│     World Engine          │   │
│  │  MQTT Broker │ :1883 │  (Python 3.12 asyncio)    │   │
│  │              │       │                            │   │
│  └──────────────┘       │  ┌─────────────────────┐  │   │
│                         │  │ 200 Room Tasks       │  │   │
│                         │  │ (independent corout.) │  │   │
│                         │  └──────────┬──────────┘  │   │
│                         │             │              │   │
│                         │  ┌──────────▼──────────┐  │   │
│                         │  │  Physics Engine      │  │   │
│                         │  │  Fault Injector      │  │   │
│                         │  │  Fleet Manager       │  │   │
│                         │  │  Metrics Collector   │  │   │
│                         │  └──────────┬──────────┘  │   │
│                         │             │              │   │
│                         │  ┌──────────▼──────────┐  │   │
│                         │  │  SQLite (aiosqlite)  │  │   │
│                         │  │  /data/world.db      │  │   │
│                         │  └─────────────────────┘  │   │
│                         └──────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

### Module Map

| Module | File | Responsibility |
|---|---|---|
| Room | `engine/room.py` | Data model, telemetry serialization, DB restore |
| Environment | `engine/environment.py` | Virtual clock, sinusoidal outside temperature |
| Physics | `engine/physics.py` | Newton's Law of Cooling, environmental correlations |
| Faults | `engine/faults.py` | 4 fault types with per-room RNG isolation |
| Persistence | `engine/persistence.py` | Async SQLite init/load/bulk-sync/save-point |
| MQTT Client | `engine/mqtt_client.py` | gmqtt wrapper, JSON schema validation, command routing |
| Fleet Manager | `engine/fleet.py` | Room instantiation, command queues, heartbeat monitor |
| Metrics | `engine/metrics.py` | Tick latency, event loop latency, CPU/memory via psutil |
| Main | `engine/main.py` | Config loading, orchestration, room task launcher |
| Config | `config/config.yaml` | All tunable parameters |

---

## 3. Structural Hierarchy & Room Model

### Campus Layout

- **1 Building** (`bldg_01`)
- **10 Floors** (floor 01–10)
- **20 Rooms per floor** (room codes `101`–`120` through `1001`–`1020`)
- **Total: 200 IoT nodes**

### Room Data Model (`engine/room.py`)

Each `Room` is a Python `dataclass` tracking:

| Field | Type | Description |
|---|---|---|
| `building_id` | int | Building identifier (1) |
| `floor_id` | int | Floor number (1–10) |
| `room_code` | int | Unique code, e.g., 502 = floor 5, room 02 |
| `room_id` | str | Slug: `b01-f05-r502` |
| `temperature` | float | Current temp (°C), clamped 15–50 |
| `humidity` | float | Current humidity (%), clamped 0–100 |
| `occupancy` | bool | Room occupied? |
| `light_level` | int | Ambient light (lux), 0–1000 |
| `hvac_mode` | str | `ON` / `OFF` / `ECO` |
| `target_temp` | float | HVAC setpoint |
| `lighting_dimmer` | int | Smart light level 0–100% |
| `last_update` | int | Unix timestamp |

The `Room.make_room_id()` method generates deterministic IDs: `b{building:02d}-f{floor:02d}-r{code}`.

---

## 4. Deterministic Physics Engine

### Newton's Law of Cooling (`engine/physics.py`)

The core thermal update per tick is:

```
T_next = T_curr + leakage + hvac_delta + occupancy_delta
```

Where:

- **Thermal leakage** = `alpha × (T_outside − T_curr) × dt`
  - `alpha = 0.01` (insulation factor)
  - `T_outside` follows a sinusoidal model: `base + amplitude × sin(2π(hour − 6)/24)`
  - Default: base = 35°C, amplitude = 10°C (hot climate)

- **HVAC actuator impact** = `beta × hvac_power × (T_target − T_curr) × dt`
  - `beta = 0.2` (HVAC strength)
  - `hvac_power`: ON = 1.0, ECO = 0.5, OFF = 0.0

- **Occupancy heat** = `occupancy_heat × (1 if occupied else 0) × dt`
  - `occupancy_heat = 0.05` °C/tick

### Environmental Correlations

1. **Humidity ↔ Temperature**: `H_next = H_curr + humidity_gain × ΔT + (0.2 if occupied else −0.1)`
2. **Light ↔ Occupancy**: Occupied rooms get light in 350–900 lux range; unoccupied rooms drop to 30–120 lux.
3. **Occupancy ↔ Time-of-Day**: Deterministic pseudo-random function using room code and virtual hour. Occupied primarily during 06:00–18:00.

### Deterministic Pseudo-Randomness

A fractional-sine hash function `_deterministic_hash01(x)` provides repeatable "random" values without external state, ensuring identical simulation runs given the same config and seed.

---

## 5. Fault Injection System

### Implementation (`engine/faults.py`)

Each room has its own `FaultInjector` instance with an isolated `random.Random(seed)`, where the seed is a stable SHA-1 hash of the room ID. Faults are triggered probabilistically each tick (default probability: 0.005 = 0.5%).

### Fault Types

| # | Fault | Behavior | Config |
|---|---|---|---|
| 1 | **Sensor Drift** | Gradual bias accumulation on temperature: ±0.01 °C/tick up to ±2.0 °C max | `drift_step`, `drift_max` |
| 2 | **Frozen Sensor** | Temperature (or humidity) locks to a fixed value for N ticks | `duration_ticks: 6` |
| 3 | **Telemetry Delay** | Publish delayed by 1–10 simulated seconds via `asyncio.sleep` | `min_delay_sec`, `max_delay_sec` |
| 4 | **Node Dropout** | Room goes completely silent (no telemetry, no heartbeat) for 30 simulated seconds | `duration_sec: 30` |

All fault triggers are logged with structured messages: `fault.triggered type=<type> room_id=<id> tick=<n>`.

---

## 6. High-Concurrency Engine (asyncio)

### Architecture (`engine/main.py`)

The engine creates **one asyncio task per room** (200 concurrent coroutines), plus background tasks for metrics monitoring, fleet health, and persistence sync.

### Room Task Loop

Each `room_task` coroutine runs an independent loop:

1. **Startup jitter**: Random delay (0 – 5 seconds) to prevent thundering herd
2. **Command processing**: Non-blocking drain of the per-room `asyncio.Queue`
3. **Physics update**: Deterministic thermal calculation
4. **Fault injection**: Apply sensor drift/freeze; decide on delay/dropout
5. **Validate & clamp**: Enforce safe ranges on all fields
6. **Publish telemetry**: JSON to MQTT (unless dropout active)
7. **Publish heartbeat**: Every 5 simulated seconds
8. **Precision drift compensation**: `sleep(max(0, tick_interval − processing_time))`

### Background Tasks

- **Event loop latency monitor**: Measures `asyncio.sleep` overshoot every 200ms
- **Metrics summary**: Logs tick count, avg/max tick latency, CPU%, memory (MB) every 60s
- **Fleet heartbeat monitor**: Checks for silent rooms, publishes fleet health
- **Persistence sync loop**: Bulk-writes all 200 room states to SQLite every 60s (or on command)

---

## 7. MQTT Infrastructure & JSON Schemas

### Topic Hierarchy

```
campus/bldg_01/
├── floor_01/
│   ├── room_101/
│   │   ├── telemetry    (engine → broker)
│   │   ├── heartbeat    (engine → broker)
│   │   └── command      (broker → engine)
│   ├── room_102/
│   │   └── ...
│   └── ...
├── floor_02/ ...
├── ...
├── fleet/
│   ├── health           (engine → broker)
│   └── command          (broker → engine, broadcast)
```

### Telemetry Payload Schema

```json
{
  "sensor_id": "b01-f05-r502",
  "timestamp": 1700003500,
  "temperature": 23.47,
  "humidity": 44.12,
  "occupancy": true,
  "light_level": 612,
  "hvac_mode": "ECO",
  "lighting_dimmer": 60
}
```

### Heartbeat Payload Schema

```json
{
  "sensor_id": "b01-f05-r502",
  "timestamp": 1700003500,
  "status": "Healthy"
}
```

### Command Payload Schema (validated with JSON Schema Draft 7)

```json
{
  "hvac_mode": "ON",
  "target_temp": 22.0,
  "lighting_dimmer": 80
}
```

All fields are optional. Invalid payloads are rejected with a log message. The `MQTTClientWrapper` uses `jsonschema.Draft7Validator` for validation before routing commands to per-room queues.

### Fleet Health Payload

```json
{
  "fleet_id": "campus",
  "healthy_rooms": 198,
  "warning_rooms": 2,
  "silent_rooms_count": 2,
  "timestamp": 1700003500
}
```

---

## 8. State Persistence (SQLite)

### Design (`engine/persistence.py`)

- **Database**: SQLite via `aiosqlite` (non-blocking async I/O)
- **Journal mode**: WAL (Write-Ahead Logging) for concurrent reads during writes
- **Table**: `room_states` with columns matching the Room data model

### Schema

```sql
CREATE TABLE IF NOT EXISTS room_states (
    room_id TEXT PRIMARY KEY,
    last_temp REAL,
    last_humidity REAL,
    hvac_mode TEXT,
    target_temp REAL,
    lighting_dimmer INTEGER,
    occupancy INTEGER,
    light_level INTEGER,
    last_update INTEGER
);
```

### Sync Strategy

- **Periodic**: Bulk-sync all 200 rooms every 60 seconds
- **On-demand**: Immediate save-point when an actuator command is applied
- **Shutdown**: Final sync on graceful shutdown

### Crash Recovery

On startup the engine calls `persistence.load_room_states()`. Any rooms found in the database have their state restored via `Room.from_db_row()`, overwriting the defaults. This means temperatures, HVAC modes, and dimmer levels survive container restarts.

Docker Compose maps the SQLite file to a named volume (`engine_data:/data`), ensuring data survives `docker compose down` and `docker compose up`.

---

## 9. Fleet Health & Heartbeat Monitoring

### Implementation (`engine/fleet.py`)

The `FleetManager` maintains:

- A `rooms_by_id` dictionary (200 Room objects)
- A `command_queues` dictionary (one `asyncio.Queue` per room)
- A `_heartbeat_monotonic` dictionary tracking last heartbeat time per room

### Heartbeat Monitor Task

Every second, the monitor:

1. Takes a snapshot of all heartbeat timestamps
2. Compares each room's last heartbeat against the configured timeout (60 sim-seconds)
3. Counts healthy vs. warning (silent) rooms
4. Publishes a fleet health payload to `campus/bldg_01/fleet/health`
5. Logs silent room IDs (sample of first 5) for debugging

### Fleet-Wide Commands

Publishing to `campus/bldg_01/fleet/command` broadcasts the command to all 200 rooms simultaneously via their per-room queues.

---

## 10. Configuration System

### YAML Configuration (`config/config.yaml`)

All simulation parameters are centralized in a single YAML file organized by section:

- `campus`: Building/floor/room counts
- `simulation`: Tick interval, time acceleration, jitter, sync interval
- `thermal`: Physics constants (alpha, beta, occupancy_heat, humidity_gain)
- `occupancy_correlation`: Light level thresholds for occupied/unoccupied
- `hvac`: Default actuator values
- `faults`: Enable/disable, probability, per-fault-type parameters
- `fleet`: Heartbeat interval and timeout
- `mqtt`: Broker host/port, QoS
- `topics`: Template strings for MQTT topic paths
- `performance`: Event loop latency threshold (200ms)
- `database`: SQLite file path

### Environment Variable Overrides

The engine supports runtime overrides via environment variables (or `.env` file):

| Variable | Config Path |
|---|---|
| `MQTT_HOST` | `mqtt.broker_host` |
| `MQTT_PORT` | `mqtt.broker_port` |
| `SQLITE_PATH` | `database.sqlite_path` |
| `FAULTS_ENABLED` | `faults.enabled` |
| `FAULTS_PROBABILITY` | `faults.probability` |
| `SIM_TICK_INTERVAL` | `simulation.tick_interval_sec` |
| `SIM_TIME_ACCELERATION` | `simulation.time_acceleration` |

This allows Docker Compose to inject production values without modifying the YAML.

---

## 11. Wokwi Hardware POC

### Purpose

The Wokwi POC validates that the telemetry JSON schema and sensor-reading logic work on real (simulated) ESP32 hardware using MicroPython.

### Circuit (`wokwi/diagram.json`)

| Component | Pin | Role |
|---|---|---|
| ESP32 DevKit V1 | — | Microcontroller |
| DHT22 | GPIO 15 | Temperature & humidity |
| PIR Motion Sensor | GPIO 14 | Occupancy detection |
| Photoresistor (LDR) | GPIO 34 (ADC) | Ambient light level |

### Firmware (`wokwi/main.py`)

- Connects to Wokwi-GUEST WiFi
- Connects to `broker.emqx.io` MQTT broker (public, for Wokwi compatibility)
- Falls back to **offline serial-only mode** if MQTT connection fails
- Publishes telemetry every 5 seconds, heartbeat every 15 seconds
- Listens for actuator commands on the room command topic
- Validates command payloads with the same schema rules as the World Engine
- Prints all telemetry (`TX:`) and heartbeats (`HB:`) to the serial monitor

### Running the POC

1. Open Wokwi (https://wokwi.com)
2. Create a new ESP32 project
3. Paste `wokwi/main.py` and `wokwi/diagram.json`
4. Click the green play button
5. The serial monitor shows WiFi connection, MQTT status, and `TX:` / `HB:` JSON lines

---

## 12. Dockerization & Deployment

### Container Architecture

| Service | Image | Ports | Purpose |
|---|---|---|---|
| `mosquitto` | `eclipse-mosquitto:2` | 1883 (MQTT), 9001 (WebSocket) | MQTT broker |
| `engine` | Custom (Python 3.12-slim) | — | World Engine |

### Docker Compose (`docker-compose.yml`)

- The engine `depends_on` Mosquitto so the broker starts first
- Environment variables (`MQTT_HOST=mosquitto`) configure the engine to find the broker by Docker DNS
- A named volume `engine_data` at `/data` persists the SQLite database across restarts
- Both services have `restart: unless-stopped` for resilience

### Dockerfile

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY engine /app/engine
COPY config /app/config
RUN mkdir -p /data
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "engine.main"]
```

### Build & Run

```bash
docker compose up --build
```

---

## 13. Performance Results

### Test Methodology

The benchmark script (`tests/test_benchmark.py`) launches the engine as a subprocess with 200 rooms and an MQTT collector that counts unique `sensor_id` values seen on telemetry and heartbeat topics.

### Performance Targets vs. Observed

| Metric | Target | Result |
|---|---|---|
| Room count | 200 | 200 |
| Tick interval | 5 seconds | 5 seconds |
| Event loop latency | < 200ms | Typically < 10ms |
| Telemetry coverage | ≥ 90% of rooms | > 95% (faults cause expected gaps) |
| Heartbeat coverage | ≥ 85% of rooms | > 90% |
| CPU usage | Reasonable | ~5–15% (single core, Python 3.12) |
| Memory usage | Reasonable | ~50–80 MB RSS |
| Continuous operation | 30 minutes | Verified via benchmark |

### Metrics Summary Log (sample)

```
metrics.summary tick_count=12000 avg_tick_ms=0.15 max_tick_ms=2.34
  loop_avg_ms=0.08 loop_max_ms=5.12 cpu_percent=8.2 mem_mb=62.4
```

The event loop latency stayed well below the 200ms threshold throughout testing. The asyncio architecture handles 200 concurrent room tasks without contention.

---

## 14. How to Run

### Prerequisites

- Docker & Docker Compose
- (Optional) Python 3.12+ for local runs

### Docker (recommended)

```bash
# Build and start both containers
docker compose up --build

# In a separate terminal, subscribe to telemetry
docker exec -it <mosquitto-container> mosquitto_sub -t "campus/bldg_01/#" -v

# Send a command to a specific room
docker exec -it <mosquitto-container> mosquitto_pub \
  -t "campus/bldg_01/floor_01/room_101/command" \
  -m '{"hvac_mode":"ON"}'
```

### Local (no Docker)

```bash
python -m venv .venv
source .venv/bin/activate      # Linux/macOS
# .venv\Scripts\Activate.ps1   # Windows PowerShell

pip install -r requirements.txt

# Quick smoke test (10 rooms, 20 seconds)
python -m engine.main --rooms 10 --duration-sec 20

# Full 200-room continuous run (requires a local Mosquitto)
python -m engine.main
```

### Benchmark (30-minute validation)

```bash
# Requires Mosquitto running on localhost:1883
python tests/test_benchmark.py --rooms 200 --duration-sec 1800
```

---

## 15. Conclusion

Phase 1 delivers a fully functional World Engine that meets all course requirements:

- **200 concurrent room simulations** running on a single Python asyncio event loop
- **Deterministic physics** based on Newton's Law of Cooling with environmental correlations
- **4 configurable fault types** (sensor drift, frozen sensor, telemetry delay, node dropout)
- **Structured MQTT telemetry** with JSON Schema validation for commands
- **SQLite persistence** with crash recovery and periodic sync
- **Fleet health monitoring** via heartbeats and timeout detection
- **Config-driven architecture** with YAML and environment variable overrides
- **Dockerized deployment** with Mosquitto broker and named volumes
- **Wokwi ESP32 POC** validating the telemetry schema on simulated hardware
- **Performance within targets** (event loop latency < 200ms, memory < 100MB)

The architecture is designed to scale into Phase 2 (Edge Intelligence & Anomaly Detection) with minimal changes — the MQTT topic structure, JSON schemas, and room data model provide a stable contract for downstream services.

---

### Repository Structure

```
IOT-Project/
├── config/
│   └── config.yaml            # All simulation parameters
├── engine/
│   ├── __init__.py
│   ├── main.py                # Entry point and orchestrator
│   ├── room.py                # Room data model
│   ├── environment.py         # Virtual clock and outside temp
│   ├── physics.py             # Newton's Law of Cooling
│   ├── faults.py              # 4 fault injectors
│   ├── persistence.py         # SQLite async persistence
│   ├── mqtt_client.py         # gmqtt wrapper with validation
│   ├── fleet.py               # Fleet manager and heartbeats
│   └── metrics.py             # Performance monitoring
├── mosquitto/
│   └── mosquitto.conf         # Broker configuration
├── wokwi/
│   ├── main.py                # ESP32 MicroPython firmware
│   ├── diagram.json           # Wokwi circuit layout
│   └── README.md              # Wokwi setup instructions
├── tests/
│   └── test_benchmark.py      # 30-minute benchmark runner
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env                       # Local env overrides
├── .env.example               # Template
├── .gitignore
├── README.md
└── REPORT.md                  # This document
```
