## SWAPD453 Phase 1 - World Engine

This repository contains the Phase 1 "World Engine": a high-concurrency Python asyncio simulation of a distributed IoT campus (200 rooms) that:

- Updates deterministic room thermal state per tick
- Injects configurable faults (drift, frozen sensor, telemetry delay, node dropout)
- Publishes telemetry + heartbeats to a local MQTT broker
- Persists "Last Known Truth" per room in SQLite and restores on restart
- Tracks fleet health (rooms silent beyond a timeout trigger warnings)
- Includes a Wokwi reference room for MicroPython validation

### Run locally with Docker (includes Mosquitto)

```powershell
docker compose up --build
```

The World Engine will start publishing telemetry to the MQTT topics defined in `config/config.yaml`.

### Quick smoke test (no Docker)

Create a Python environment and install dependencies:

```powershell
python -m venv .venv
.venv\\Scripts\\Activate.ps1
pip install -r requirements.txt

python -m engine.main --duration-sec 20 --rooms 10
```

### MQTT Topics (Phase 1 defaults)

Base topic prefix: `campus/bldg_01`

- Telemetry: `campus/bldg_01/floor_{floor}/room_{roomCode}/telemetry`
- Heartbeat: `campus/bldg_01/floor_{floor}/room_{roomCode}/heartbeat`
- Fleet health: `campus/bldg_01/fleet/health`
- Fleet command (broadcast): `campus/bldg_01/fleet/command`
- Room command (per-room override): `campus/bldg_01/floor_{floor}/room_{roomCode}/command`

